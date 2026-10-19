# 原生脚本隔离 launcher：已实现 / 未实现

对应变更 `add-windows-local-project-access` 的任务 **7.4**（原生隔离 launcher）与 design **D8**。

**读法**：本文件把「已经做到」和「还没做」分开写，并且**未实现的部分一律不写进"已支持"**。上一节是实测行为，第二节是明确的缺口——**当前状态不足以上线脚本功能**，理由是访问边界还缺一半。

## 1. 已实现并实测

位置：`desktop/native/script-launcher/`（Rust，产物 `script-launcher.exe`）。一个 CLI + 一个库，隔离策略只有**一份**实现。

### Job Object 承担的部分（7.4 的资源与生命周期）

| 能力 | 实现方式 |
|---|---|
| **进程树归属** | 创建 Job 后**先把子进程挂起创建**，赋入 Job，**才恢复**（`CREATE_SUSPENDED` → `AssignProcessToJobObject` → `ResumeThread`） |
| **launcher 崩溃不残留** | `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`——内核在句柄关闭时杀掉整棵树，不依赖 launcher 活着做清理 |
| **取消/超时终止全树** | `TerminateJobObject`；实测 `timeout_ms=1500` → `status: timed_out`、`terminated: true`，约 2.5 秒返回 |
| **进程数上限** | `JOB_OBJECT_LIMIT_ACTIVE_PROCESS` + 轮询 `QueryInformationJobObject` 把越界转成具名的 `limit_exceeded` 而不是神秘非零退出 |
| **内存上限** | 同时设 `JOB_MEMORY`（整树）与 `PROCESS_MEMORY`（单个成员）——**只设前者会放任一个吃满的大子进程** |
| **输出有界** | 每流 1 MiB 上限，超限置 `*_truncated` 而不是无限读入内存 |
| **输出不阻塞** | stdout/stderr 各由一个线程并发排空：管道缓冲填满会阻塞写端，串行等待会**死锁**并表现为"卡住" |
| **环境白名单** | 环境块**只由调用方给的清单构成**，不与父进程环境合并（合并过的白名单不是白名单） |
| **无控制台窗口** | `CREATE_NO_WINDOW` |
| **命令行引号** | 自行实现 `CreateProcess` 的引号规则（`Command` 无法挂起创建）；带空格+引号的参数会正确处理，否则就是**参数注入** |
| **拒绝即不执行** | Job 创建失败 / 赋值失败 → 返回错误并终止；**没有任何路径会恢复一个未受约束的子进程** |

### 实测输出

```json
// 正常
{"status":"exited","exit_code":0,"stdout":"hello-from-launcher \r\n","stderr":"err-line \r\n",...,"terminated":false}
// 超时
{"status":"timed_out","exit_code":null,"stdout":"","stderr":"",...,"terminated":true}
// 请求访问边界（未实现）
{"refused":"unsupported: the AppContainer access boundary is not implemented in this build"}
```

第三条是关键设计：**请求隔离而拿不到时，拒绝，不静默降级**。降级是唯一用户无法从结果里察觉的失败。

## 2. **未实现**（缺口，不得读作已支持）

### 2.1 访问边界（LPAC / AppContainer）——**已实现"创建"，未打通"可用"**

**已落地**（`src/appcontainer.rs`）：

- `DeriveAppContainerSidFromAppContainerName` + `CreateAppContainerProfile`（profile 名 `cow-project-script`，**一个共享 profile**而非每运行一个——因为将来 ACL 要写的是一个 SID，每运行换 SID 就是每运行撒一个 ACE）
- `InitializeProcThreadAttributeList`（两趟，先问大小）→ `UpdateProcThreadAttribute(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)` → `STARTUPINFOEXW` + `EXTENDED_STARTUPINFO_PRESENT` 传给 `CreateProcessW`
- `lib.rs` 里原先"AppContainer 一律 `unsupported`"的拒绝**已移除**：代码真的会尝试创建受限进程了

**实测证据**：profile **确实建成**（`%LOCALAPPDATA%\Packages\cow-project-script` 存在）。

**未打通**：`CreateProcessW` 在 AppContainer 路径下失败。**这不是配置差错，是一个尚未查清的缺口**，如实记录：

| 尝试 | 结果 |
|---|---|
| 直接创建 | `error 87`（`ERROR_INVALID_PARAMETER`） |
| 修掉一个真实 bug 后（见下） | `error 31`（`ERROR_GEN_FAILURE`） |
| 显式设 `lpDesktop = "WinSta0\Default"` | `error 203`（`ERROR_ENVVAR_NOT_FOUND`） |
| 环境块补 `=C:` 驱动器当前目录变量 | 仍 203 |
| 环境块补 `SystemRoot`/`windir`/`TEMP`/`USERPROFILE`/`LOCALAPPDATA`/`APPDATA`/`ComSpec` | 仍 31/203 |
| `cwd` 改为 AppContainer 可读目录（System32） | 无变化 |

**已排除**：属性列表未填（两趟调用正确）、`cb` 大小、`EXTENDED_STARTUPINFO_PRESENT` 缺失、`cwd` 不可读、以及上面逐项的环境变量。

**修复过程中发现并修掉的一个真实内存安全 bug**（值得单独记）：`SECURITY_CAPABILITIES` 原本是 `attribute_list()` 的**局部变量**，而 `UpdateProcThreadAttribute` 存的是**指针**。函数返回后该指针悬空，`CreateProcess` 稍后才读它——**use-after-free**，且症状只是一个"参数无效"错误码。已改为 `Box` 并由 `AttributeList` 持有。同一类问题也修在 `StartupInfoEx._desktop`（`lpDesktop` 指向的字符串必须比 `info` 活得久）。

**最可能的下一步**（未验证，写给接手者）：`ERROR_ENVVAR_NOT_FOUND` 在设了 `lpDesktop` 时出现、不设时变成 `ERROR_GEN_FAILURE`，指向 **profile 的环境/注册状态**而非进程创建参数。建议先用一个**最小原生 C 程序**（不经 Rust 与环境块构造）验证 `CreateProcessW` + AppContainer 能否跑通 `cmd /c echo`；若能，问题在本实现的参数构造；若不能，问题在 profile 或 OS 状态。

**另一件必须说清的事**：即使进程能创建，**它仍然读不到项目目录**——AppContainer 默认什么都不允许，必须由 7.2 在该目录上写一个指名该 SID 的 ACE。所以本模块单独存在时，受限子进程会启动后立刻失败。这正是"边界有了、授权还没有"的诚实状态。

**仍然成立的原结论**：`Access::Inherited` 是当前唯一可用的取值。

`Access::AppContainer` 存在，但**任何取值都会返回 `unsupported`**。也就是说：

> **当前 launcher 只管住"进程树与资源"，不管住"能碰到什么"。** 脚本仍以**当前用户自己的文件权限**运行。

design D8 明确「Job 只管理资源/生命周期，**LPAC 才承担访问边界，二者不可替代**」。所以按 D8 的定义，**7.4 只完成了一半**，而缺的是承担隔离的那一半。

已实测确认**本机可以创建 AppContainer**（`CreateAppContainerProfile` 对标准用户返回 `hr=0`），所以这条路在本机是通的，缺的是实现。

### 2.1b AppContainer **能跑，但不稳定**——量化结果

上一节说"进程创建未打通"。**现在更准确的结论是：打通了，但只有约一半的启动成功。**

用同一个作业连续尝试 6 次（`cwd` = System32，`cmd /c echo x`）：

| 路径 | 结果 |
|---|---|
| `access=app_container` | **成功 3 / 失败 3** |
| `access=inherited`（对照） | **成功 6 / 6** |

失败是 `ERROR_NOT_APPCONTAINER`（4250）与 `ERROR_GEN_FAILURE`（31）**交替**出现，而同一命令在手工运行时曾连续成功。**非确定性**，不是参数配错。

**故意不把它算作已完成**：一个一半时间失败的访问边界不是边界——它会让"隔离了"变成随机事件。因此三条使用 AppContainer 的测试标为 `skip`，并写明启用条件；`inherited` 路径的测试仍然**硬失败**，不受影响。

**已确定可用的部分**：

- profile 创建与 SID 派生**稳定成功**（`%LOCALAPPDATA%\Packages\cow-project-script` 存在）
- **受限进程确实能启动并正确输出**（成功的那 3 次，stdout/stderr/退出码都对）
- **环境要求已量化**：AppContainer 需要 `LOCALAPPDATA` + `APPDATA`（加 `SystemRoot`/`windir`），否则 `CreateProcess` 报 `ERROR_ENVVAR_NOT_FOUND`。仅 `USERPROFILE` 不够。launcher 已**只对受限路径**注入这组系统变量
- **项目目录需要 ACL 授权才能作为 `cwd`**：未授权时启动直接失败（`ERROR_NOT_APPCONTAINER`）。这不是 bug，而是 **7.2 之所以是硬前置的证据**——AppContainer 连自己的 cwd 都读不了

**已排除**：属性列表构造（`SECURITY_CAPABILITIES` 悬垂指针已修）、`cb`、`EXTENDED_STARTUPINFO_PRESENT`、`lpDesktop`、驱动器变量、以及上面那组环境变量。

**下一步最可能的方向（未验证）**：失败交替出现，指向 **profile 的首次初始化 / 并发**而非参数——例如第一次启动时容器目录尚未就绪，或同一 profile 的连续启动互相干扰。建议：启动前先等 profile 目录出现；或改用 `CreateAppContainerProfile` 返回值区分"新建"与"已存在"并对新建的情况做一次预热。

### 2.1c 修不稳定的尝试：**逐个排除，未找到根因**

量化基准（清掉机器上的残留后台进程后）：

| 路径 | 结果 |
|---|---|
| `inherited`（对照） | **15 / 15**、**6 / 6**、**20 / 20** |
| `app_container` | **9 / 15**、**13 / 20**、**19 / 25** |

**约 60–80%，且 `inherited` 始终满分**——所以问题在本实现，不在机器。

**逐个排除的变量**（都做了开关化对比测量）：

| 假设 | 做法 | 结果 |
|---|---|---|
| 环境块构造错误 | `COW_LAUNCHER_NULL_ENV` 传 `NULL` | NULL 10/15 vs 真实 12/15 → **无关** |
| `CREATE_NO_WINDOW` | 开关对比 | 12/20 vs 16/20 → **噪声内，无关** |
| Job Object 赋值（探针没有这步） | `COW_LAUNCHER_SKIP_JOB` | 16/20 → **无关** |
| 失败是瞬时的 | `CreateProcess` 重试 4 次 + 20ms | **19/25，无改善** → **按次复现，非瞬时** |

**同时排除的更大范围**：属性列表构造（`SECURITY_CAPABILITIES` 悬垂指针已修）、`cb`、`EXTENDED_STARTUPINFO_PRESENT`、`lpDesktop`、驱动器变量、`USERPROFILE`/`LOCALAPPDATA`/`APPDATA`/`SystemRoot`（这些是"能否启动"的门槛，不是"是否稳定"的原因）。

**最锐利的一条线索**：**同一个 AppContainer 启动，探针程序 20/20，launcher 9/15**。探针与本实现的差异已被逐个关掉（环境、`CREATE_NO_WINDOW`、Job），**仍未找到那一个差异**。

**重试代码已删除**：测出来无效，留着只会让代码看起来"处理过了"而掩盖问题。诊断开关也已移除。

**诚实的结论**：**根因未定位。** 边界在 60–80% 的时间生效，**不足以称作边界**——因此三条 AppContainer 测试保持 `skip`，并在原因里写明「测得 3/6 与 9/15，一半的成功率不是隔离」。

### 2.2 未接线到生产路径

launcher 目前是一个**独立可执行文件**。桌面主进程仍走原有路径，**没有调用它**。即：**脚本功能没有因此变得可用**，本轮交付的是隔离件的本体。

### 2.3 ACL 生命周期（任务 7.2 / 7.3）

`desktop/native/` 下**没有任何**添加/跟踪/恢复 ACE 的代码。所以：

- 没有给隔离身份加项目访问权，也就**没有需要恢复的状态**；
- 7.2 要求的原型（显式/继承/受保护、NULL/空 DACL、硬链接、移出根、并发改 ACL、崩溃重启、大目录成本）**全部未做**；
- 7.2 是「正式 ACL 实现与脚本业务接入的**硬前置**」——**未通过前不得接业务**。

### 2.4 运行时选择（任务 7.1）

`JobSpec.executable` 由调用方给定。**7.1 要求的比较**（复用扩展冻结 worker vs 最小随包 CPython 的隔离资源、依赖适配、包体、维护成本）**未做**，也没有决策记录。因此：

- 没有任何随包运行时；
- **没有阻止调用方指向系统 PATH 上的解释器**。当前 launcher 不搜索 PATH（只接受绝对路径），但也不保证该路径是受信运行时——那需要 7.1 的结论。

### 2.5 stdin 未接通

子进程的 stdin 被关闭（读到 EOF），`JobSpec.stdin` 被忽略。明确行为而非挂起，但**是一个缺口**。

### 2.6 未做真机验证

只在本机（Windows 11，标准用户）实测。**Win10 22H2 未跑**；S04/S06/S07 编号的验收未接。

## 3. 下一步的顺序（D8 规定）

1. **7.1 决策**：比较运行时方案并记录（硬前置）。
2. **7.2 原型**：证明 ACL 可被精确跟踪与恢复；**证明不了的组合在改动前拒绝**。
3. **2.1 的实现**：把 AppContainer 接进本 launcher，替换 `Access::AppContainer` 的 `unsupported`。
4. **7.3 + 接线**：身份/临时资源生命周期，然后才接业务。
5. 真机 + 真包证据齐备后，才按语言开放 Windows 脚本能力（8.3）。
