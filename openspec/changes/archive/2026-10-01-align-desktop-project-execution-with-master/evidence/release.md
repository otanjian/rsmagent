# A01—A36 覆盖索引与发布状态（任务 10.6 / 10.7）

本文是 `acceptance.md` §3 指定的 `evidence/release.md`：A01—A36 与六份 spec 的逐项覆盖索引、
每个条目的**真实负向证据**、平台/依赖/兼容/回退状态，以及最终 feature flag 口径。

> **先声明本文不主张什么**：全部结论都在**开发机（macOS/posix）+ 产品自身的执行端点**上取得。
> **安装包内（签名后、随包 Python）与 Windows** 的等价验收属任务 10.1—10.3，**未做**。
> 因此本文可以判定"开发机实现与负向证据齐备"，**不可**作为本 change 整体结项的依据
> （见 §7 与 `tasks.md` 中 10.1—10.3、11.6 保持未勾选）。

## 0. 口径

- **「真实负向证据」**指两类，二者必须能对照同一条断言：
  1. **实现走样（变异）**：故意改坏实现，确认对应用例**判失败**，再还原并确认恢复绿；
  2. **真实拒绝**：真实内核/真实进程/真实 HTTP 的拒绝，且断言的是**可观测结果**
     （退出码、errno、状态码、库表字节），不是"函数被调用过"。
- 「用例缺测不能通过总体百分比掩盖」：下表逐条列到**条目**，不按套件通过率折算。
- 标注 **未测** 的条目不计为通过；标注 **不支持** 的条目是**设计决定**，不是缺口。

### 0.1 变异/注入总量（14 份日志）

| 日志 | 走样数 | 日志 | 走样数 |
| --- | --- | --- | --- |
| `artifact-source-mutations.log` | 10 | `project-source-mutations.log` | 10 |
| `compatibility-matrix-mutations.log` | 6 | `project-watching-mutations.log` | 16 |
| `history-cards-mutations.log` | 16 | `release-gates-mutations.log` | 10 |
| `isolated-preview-mutations.log` | 35 | `skill-dependency-mutations.log` | 22 |
| `journal-fault-mutations.log` | 12 | `skill-package-mutations.log` | 12 |
| `materialize-mutations.log` | 16 | `skill-transfer-mutations.log` | 6 |
| `project-native-actions-mutations.log` | 23 | `source-resolution-mutations.log` | 5 |
| | | **合计** | **199** |

全部 199 类走样均被对应用例判为失败，且还原后源文件逐字节一致。脚本在 `evidence/scripts/`
（**15 个** `mutate_*.py`，由 `tests/test_mutation_evidence_scripts.py` 做结构完整性登记：
锚点必须命中、必须唯一、只能落在登记的源文件上、每条必须写明期望失败的用例）。

**本轮的两处更正**（原先的数字有误，已按日志逐份重数）：

- `source-resolution-mutations.log` 实为 **5**（原表记 4）；
- 新增 `compatibility-matrix-mutations.log`（**6**，A29）；
- 合计 192 → **199**（192 − 4 + 5 + 6）。

新脚本 `mutate_compatibility_matrix.py` 覆盖 A29 的四行矩阵与跨四行性质，6 处走样分别是：
旧服务器被当成可用、主版本不匹配不拒绝、服务端把新协议声明成 `required`、
原因判序把"没验收"排在"没实现"前、**把写工具加进 v1 的 `commands.ops`**
（老通道变成写通道 → "不可用"就有了退路）、只读目标照常委派。

**由这次变异检查改强的一处用例**：`test_row_4_a_missing_implementation_is_blamed_first`
原先只用 `implemented=False, accepted=True`——此时两个条件并不同时未满足，
**任何**判序都会答 `not_implemented`，断言即便把优先级写反也照样通过。现已对
`accepted` 取 `(True, False)` 两个值，只有这样才能真正区分先后。
这正是变异检查的用处：它找出的不是实现缺陷，而是一条**名为断言、实则空转**的用例。

---

## 1. A01—A36 覆盖索引

| ID | 关联能力 | 证据 | 真实负向证据 | 开发机结论 | 缺口 / 未测 |
| --- | --- | --- | --- | --- | --- |
| A01 | project-execution | `desktop-e2e-a01-a02.md`、`e2e-a01-a02-run.log` | **修复前可复现**：`Illegal invocation` 与安装 ID 不持久各自先红后绿，真实 Electron 复跑 3 次一致 | 通过 | 安装包内复跑未做 |
| A02 | project-execution | 同上 | 代次/取消/切换的 `superseded` 分支用例；旧项目保留时 UI 显示仍在生效 | 通过 | 安装包内复跑未做 |
| A03 | scoped-project-browser | `target-resolution-acceptance.md` | 伪造 chooser handle、复用他人选择记录、本机绝对路径三路均被拒 | 通过 | — |
| A04 | project-execution | `local-e2e.md`、`local-e2e-run.log` | 服务器工作区目录与哨兵文件**字节未变**（不触发复制导入） | 通过 | 安装包内复跑未做 |
| A05 | project-execution / execution-delivery | `remote-e2e.md` §6.3、`governance.md` | `desktop_commands` 恰增 6 条、服务器哨兵不变、master 工具未被改指向设备 | 通过 | **真实模型**未参与（注入的是工具调用）；安装包内未做 |
| A06 | skill-runtime / project-artifacts | `skill-execution-acceptance.md` §1、`p3-artifacts-and-actions.md` | 汇总 350 由测试**自己解 zip 读单元格**核对；输入工作簿前后 sha256 逐字节相同；产出在项目内且**不在缓存** | 通过（**本地**） | 远程模式安装件内未做 |
| A07 | skill-runtime | `skill-execution-acceptance.md` §1 | 查**已部署版本**（pin 目录）的模板与资源，字段齐、无 `{{` 残留；技能/记忆目录未进项目；项目外缓存未写入 | 通过（**本地**） | 同上 |
| A08 | project-execution / project-artifacts | `local-e2e.md` §4、`target-resolution-acceptance.md` | 两轮各钉各自目录、真实文件互不覆盖；重选后旧运行被拒；旧卡片仍指原设备/项目 | 通过 | — |
| A09 | execution-delivery / skill-runtime | `governance.md` A09、`remote-e2e.md` | 撤权/离线/旧纪元/许可过期/许可二次消费逐条拒绝；技能未选中即拒 | 通过 | Membership 行删除与 Agent.use 撤销**无专门用例**（已如实登记） |
| A10 | project-execution / skill-runtime | `governance.md` A10、`remote-e2e.md` | **变异命中**：去掉 `plan` 的 `local_execution_refusal` 分支 → 2 项 A10 用例失败（这是本轮**修掉的真缺陷**：远程路径此前不读本轮拒绝） | 通过 | — |
| A11 | skill-runtime | `skill-resources.md`（12/12）、`skill-transfer-mutations.log`（6/6）、`skill-execution-acceptance.md` §3 | 坏包（摘要/路径/越界/半包）不发布不使用；运行中升级仍钉旧版本；跨作用域命中为 0；越界技能根被启动器拒 | 通过 | 安装件内未做 |
| A12 | skill-runtime | `prompt-priority.md`（12/12）、`skill-execution-acceptance.md` §1 | 用**真实锁定清单**证明声明被满足、再用空清单证明缺项被指名（技能名+模块+发行名）；`client_platform()` 未知即未知，不用服务器平台冒充（M3 判失败） | 通过 | **Windows 客户端 Shell 语义未验证**；Linux 服务器驱动 Windows 设备未测 |
| A13 | project-execution / skill-runtime | `resource-landing.md`（19/19） | `resource:<id>[@ver][#digest]` 三层校验后原子落地；无落地传输或落地失败**按工具名拒绝**而非回退服务器路径；裸绝对路径与 `backend:` 抛 `server_path_not_local` | 通过 | `client_files` 结果路径的落地未接线（见该证据 §7） |
| A14 | execution-delivery | `faults.md`（12/12 注入） | 重复投递只产生一次副作用、返回**原**回执；聊天不追加第二条成功 `tool_result` | 通过 | 安装件内未做 |
| A15 | execution-delivery | `faults.md` | 开始意图落盘后强杀 → `unknown`+`effects:unknown`，**禁止自动重跑**；M10（报成 succeeded）被判失败 | 通过 | 同上 |
| A16 | execution-delivery | `faults.md` | 同 ID 改参数/摘要/作用域 → 冲突拒绝；过期清理后留**墓碑**，键仍占位；重连 epoch 不把旧命令当新调用 | 通过 | 同上 |
| A17 | execution-delivery / 隔离 | `faults.md`、`isolation-acceptance.md` §3、`remote-e2e.md` | 真实 `exit_code === 3`、`truncation.truncated === true`、超时后命令与后台子进程**轮询确认消失**；跨设备句柄 kill 被拒 | 通过（进程级） | **内存/CPU 无硬上限**（`RLIMIT_AS` 在 macOS+CPython 不可靠，如实记录）；Windows 未测 |
| A18 | project-execution / execution-delivery | `faults.md`（M8/M9 命中） | 取消**等进程树真正结束**才返回结论；已写文件保留并报部分效果；迟到结果不污染新会话 | 通过 | 安装件内未做 |
| A19 | execution-delivery | `faults.md` | 短断线不终止、**恰好等于边界也不终止**；超过才终止并保留已写内容；断线期间不受理新帧也不转投服务器 | 通过 | 同上 |
| A20 | execution-delivery | `faults.md`（M6/M7 命中） | 同一实际根（含两个 alias）串行；外部修改报 `file_changed` 并标注部分效果；不承诺 Bash 事务回滚 | 通过 | 同上 |
| A21 | project-artifacts | `project-watching.md`（16/16）、`project-source-adapter.md`（10/10）、`p3-artifacts-and-actions.md` | 面板/`@`/工具参数同一来源；刷新先重验；失效订阅被清理；刷新不上传且订阅有界 | 通过 | 安装件内未做 |
| A22 | project-artifacts | `history-cards.md`（16/16） | 切到 B 后仍定位**生成时**的设备/项目；变化显示 `resolution:"missing"`；不对服务器同名路径 stat 或下载；跨项目重解析被 `wrongProject` 拒绝 | 通过 | 同上 |
| A23 | project-artifacts | `history-cards.md` | 另一台机器的路径命中不了任何注册，**永不**被声称为本机产出；重启后历史只作候选 | 通过 | 同上 |
| A24 | project-artifacts | `isolated-preview.md`（35/35） | 五层（决策/票据/策略/桥与容器/页面）逐层走样均判失败；含脚本 HTML 无原生桥、无秘密、无法越界 | 通过 | 安装件内未做 |
| A25 | project-artifacts | `materialize-and-data-flow.md`（16/16）、`project-native-actions.md`（23/23） | 系统打开/所在文件夹/复制路径/另存为/明确上传逐项真实到达宿主；**未操作时不上传**；无关联应用真实提示 | 通过 | 面板上无「上传到服务器」按钮（触发点是用户明确要求）；安装件内未做 |
| A26 | execution-isolation | `isolation-acceptance.md` §2、`platform-probes.md` §2 | **真实内核拒绝**：`os.open` 动态路径、项目内软链接替换、孙进程、越界读写；`(subpath "/")` 出现即判失败 | 通过（**macOS**） | **Windows 未测**（该平台 `unsupported_platform` 明确拒绝） |
| A27 | execution-isolation / skill-runtime | `isolation-acceptance.md` §2、`platform-probes.md`、`prerequisite-slices.md` §5 | 注入的 `COW_DESKTOP_TOKEN`/`OPENAI_API_KEY`/`SESSION_TOKEN`/`COW_IDENTITY_DB` 在 worker env 中均不出现；身份库/主进程配置 `cat` 为内核拒绝；技能缓存改写/新建/删除均非零退出；**真实监听 socket 也连不上** | 通过（**macOS**） | Windows 未测 |
| A28 | 依赖切片 | `governance.md` A28、`remote-e2e.md` | 未批准/额度耗尽/磁盘不足/审计失败/许可过期逐条失败；磁盘不足报 `resource_unavailable` 且**行状态 `failed`**；审计失败整个 start 事务回滚（变异命中）；退费后可再放进 5 次 | 通过 | 真实内核 `ENOSPC` 未跑（覆盖的是"设备如实上报时服务端不谎报成功"） |
| A29 | execution-delivery | `compatibility-matrix.md` §2（11 项） | 四行组合各自给出**具名**理由（`not_implemented`/`disabled_by_deployment`/`not_accepted`）；v1 op 面不含写工具（无降级通道）；只读目标把开关**强行置开**仍不委派 | 通过 | **本文件未做变异检查**（新增文件的诚实缺口）；安装件/Windows 未测 |
| A30 | project-execution | `compatibility-matrix.md` §3.1、`target-resolution-acceptance.md`、`local-e2e.md` §6 | 无项目**不是拒绝**；进入本机作用域不改工具策略（`knowledge_write` 前后皆 `False`）；关闭项目回到 no-op | 通过 | 安装件内未做 |
| A31 | project-execution / execution-delivery | `compatibility-matrix.md` §3.2、`target-resolution-acceptance.md` | 四种后台触发全部 `REFUSAL_NEEDS_INTERACTIVE`；页面不能给 bridge 递 shell/模块；preload 面无通用 exec IPC | 通过 | 同上 |
| A32 | execution-delivery / project-artifacts | `release-gates.md` §四（10/10，含 R8/R9） | 关开关后四条路径全 503；三张表逐字段不变；重开回答原样（含 `reconcile` 的"不要自动重跑"）；未知状态连查两次答复不变 | 通过 | 设备侧 journal 在设备上，本进程碰不到，主张限为"服务器不删不改不补发" |
| A33 | 全部 | `platform-probes.md`、`isolation-acceptance.md` §6、`packaged-bundle.md` §4.4—4.7、`prerequisite-slices.md` §5 | 开发机 macOS 真实内核拒绝齐备；**A26/A27 已在真实安装件内重跑**（dmg 只读挂载卷 / zip / `.app`，随包冻结 Python，`COW_A26_BACKEND` → 44 passed / 0 failed，越界仍为内核拒绝） | **未测（部分覆盖）** | 仍**必须**在**签名**安装包内重复 A04—A07/A12/A18/A24/A26—A27：属 10.2（macOS）与 10.3（Windows）。已做的是**未签名**安装件上的 A26/A27；A04—A07/A12/A18/A24 未在安装件内重跑 |
| A34 | project-artifacts | `materialize-and-data-flow.md`、`isolated-preview.md`、`history-cards.md` | 三份词典的数据流说明挂在本机项目面包屑上（服务器路径不带这句）；用例同时断言那句谎**绝不能被写**；无整目录默认同步 | 通过 | 安装件内未做 |
| A35 | project-artifacts | `history-cards.md`、`isolated-preview.md`、`artifact-source.md`（10/10） | 模型只声称生成 → 无卡片（`safe_build_artifact` 失败即丢整条）；越界/不存在/设备自报未验证 → 不发布；失败运行的半成品不发布 | 通过 | 同上 |
| A36 | project-execution | `resource-landing.md`（19/19）、`execution-boundary-map.md` | 逐项分类（本机执行 / 显式传输 / 明确不支持）；**未分类的新落盘工具会被 `capability` 拒绝派发**，不静默写服务端 | 通过 | 安装件内未做 |

**逐条统计**：A01—A32、A34—A36 共 **34 条**在开发机上通过；**A33 未测**——本轮已把其中
**A26/A27 在真实（未签名）安装件内**重跑通过（dmg 只读挂载卷 / zip / `.app`，见
`isolation-acceptance.md` §6 与 `packaged-bundle.md` §4.7），但 A33 还要求
**签名**安装包与 A04—A07/A12/A18/A24，故整体仍记未测；
无**不支持**条目（Windows 脚本能力的"不支持"是平台矩阵结论，不是 A 编号条目）。

---

## 2. 六份 spec 的覆盖对应

| spec | 主要承载的 A 编号 |
| --- | --- |
| `desktop-project-execution` | A01—A05、A08、A10、A18、A26、A30、A31、A36 |
| `desktop-skill-runtime` | A06、A07、A09、A11、A12、A13、A27 |
| `desktop-project-artifacts` | A06、A08、A21—A25、A32、A34、A35 |
| `desktop-execution-delivery` | A05、A09、A14—A20、A28、A29、A31、A32 |
| `execution-isolation`（MODIFIED） | A17、A26、A27 |
| `scoped-project-browser`（MODIFIED/ADDED） | A03 |

A28 的归属是 **`acceptance.md` 标注的"依赖切片"**（身份/授权/审批/审计/凭据/配额），
其证据在 `governance.md` 与 `prerequisite-slices.md`，不由本 change 新建 capability 承载。

---

## 3. 平台矩阵

| 平台 | 本机文件读写 | 本机脚本 | 依据 | 状态 |
| --- | --- | --- | --- | --- |
| macOS（开发机，源码 + `.venv`） | 通过 | 通过（真实 `sandbox-exec`，内核拒绝为真实进程断言） | `platform-probes.md` §2.1、`isolation-acceptance.md` | ✅ 开发机 |
| macOS（**安装形态**：未签名 `.app` 内 bundle / 随包冻结 Python） | 通过 | 通过（同一套断言换运行时；内核拒绝仍为真实进程断言） | `isolation-acceptance.md` §6、`packaged-bundle.md` §4.4—4.6 | ✅ 未签名安装形态 |
| macOS（**签名/公证后**） | **未测** | **未测** | — | ❌ A33 / 10.2 |
| Windows | **未测** | **不支持**：`unsupported_platform`，无降级路径 | 任务 4.5；`resolveScriptSupport` | ❌ 10.3 |
| 支持表上其他平台 | 未测 | 未测 | — | ❌ 10.3 |

**不得**用 `execution_isolation` 的配置值代替平台证据。

"安装形态"这一行不是"开发机"那一行的复述，而是**另一种运行时**，两者不可互换：
源码树跑 `python -m agent.desktop_local.worker`（`.venv` 的 `base_prefix`/`site-packages`
是真实可读目录），安装形态只有一个冻结可执行文件、没有 `.venv`，且 `interpreter.ts`
对 frozen 分支**刻意只授予 `bundleRoot`**（因为 `sysconfig` 在冻结构建上仍会报**构建机**路径）。
因此"源码树里成立的 profile"在冻结后可能起不来，而后者才是用户拿到的那一种。
实测：源码形态 **54 passed / 0 failed**，安装形态 **44 passed / 0 failed / 15 skipped**
（两种形态各自只跳过对方专属用例），并已作为门槛接入 `release.yml` /
`release-overlay.yml` 的 macOS leg。仍**未测**的是**签名/公证后**的等价探针（A33）。

---

## 4. 依赖切片状态

六个被真实消费的切片逐项结论见表 `prerequisite-slices.md`。摘要：

| 切片 | 真实消费 | 门槛性缺项 |
| --- | --- | --- |
| `desktop-tenant-context`（身份） | ✅ 原生 bearer、四条端点、跨作用域拒绝 | 真机双用户双租户（10.2/10.3） |
| `resource-execution-authorization` | ✅ prepare/start 两处独立重验（变异 R10 命中） | 无 |
| `action-approval` | ✅ 真实 `approvals` 行 + 真实 `approval_gate` | 经真实审批控制台的端到端（10.2） |
| `audit-log` | ✅ 六处 `_audit.record`，带关联 ID，不含路径/令牌/环境 | 无 |
| `credential-management` | ✅ 白名单 + 具名 deny pass，两端各洗一次 | Windows 侧同口径（4.5/10.3） |
| `resource-quota` | ✅ 同一条 `quota_usage`，只退"没买到机器时间"的调用 | 真实并发压测（10.x 加固） |

（配额拒绝的线上错误码是 **`limit_exceeded` 413**；用例名里的 `quota_exhausted` 只是别名。）

---

## 5. 兼容、迁移与回退

| 项 | 结论 | 证据 |
| --- | --- | --- |
| A29 新旧混合矩阵 | 通过（四行各自具名理由；无 v1 降级通道） | `compatibility-matrix.md` §2 |
| A30/A31 旧行为回归 | 通过；Agent Registry / Bridge / ExecutionRun / scheduler / 普通 Web / Channel 回归 **312 + 110 + 155 + 51** 项通过 | `compatibility-matrix.md` §3.3 |
| 迁移 41—44 | 每个迁移体与版本标记**同一事务**；44 个版本逐个 subTest 验证"体抛异常→标记未写→库仍可打开"；半应用库可自愈 | `release-gates.md` §一 |
| 升级不扩权 | 旧 target 读回 `readonly-input`、不委派、拿不到本机目录；`project_mode` 未知值被拒 | `release-gates.md` §一.2/§三 |
| A32 回退 | 关开关即封锁新调用、在途/已完/unknown 逐字段不变、重开回答原样、不自动重跑、不改投服务器 | `release-gates.md` §四 |

---

## 6. 最终 feature flag 状态

| 开关 | 出货默认 | v2 切片 `accepted` | 默认部署下的 meta 理由 |
| --- | --- | --- | --- |
| `desktop_project_execution_enabled` | `False` | `False`（本轮**没有**修改任何验收状态） | `not_accepted` |
| `desktop_project_scripts_enabled` | `False` | `False` | `not_accepted` |
| `desktop_local_files_enabled` | `True`（**未改动**） | `True` | 可用（v1 只读面未被 v2 收窄） |

- 三重门 `implemented × accepted × configured × platform`，判序固定
  `not_implemented → not_accepted → disabled_by_deployment`；
- **报告 ≠ 授予**：两个 v2 切片的 `open` 是空集，所以即便把 `accepted` 与两个开关都置真，
  handler 侧仍没有任何动作被打开；
- 真正把 `accepted` 置真是 10.x 验收通过后的**运维动作**，不是本轮的代码改动。

---

## 7. 未测与不支持（不得记为通过）

**未测（阻塞本 change 整体结项）**

1. **A33**：签名安装包内重复 A04—A07/A12/A18/A24/A26—A27 —— 10.2（macOS）/10.3（Windows）。
2. **Windows 全部**：启动器、进程树、A12 的 Shell 语义、A26/A27 —— 4.5/4.9/10.3。
3. **支持表上其他平台** —— 10.3。
4. **真实模型**：A05/A06/A07/A11 等的"模型"是注入到工具分派的工具调用，
   不是字面 LLM —— 10.2/10.3。
5. **A17 的资源上限维度**：内存/CPU 无硬上限（`RLIMIT_AS` 在 macOS + CPython 下不可靠）。
6. **A28 的真实内核 `ENOSPC`**：只覆盖了"设备如实上报时服务端不谎报成功"。
7. ~~**A29 未做变异检查**~~ —— **已补**：新增
   `evidence/scripts/mutate_compatibility_matrix.py`（6 类走样，日志
   `evidence/compatibility-matrix-mutations.log`），6/6 被对应用例判为失败；
   结构完整性已登记进 `tests/test_mutation_evidence_scripts.py`。该检查并发现
   `test_row_4_a_missing_implementation_is_blamed_first` 原先是**空转**用例
   （只取 `accepted=True`，任何判序都通过），已改为对 `accepted` 取两个值。
8. ~~**随包 Python 3.11 上的实机迁移**~~ —— **已补**：本轮真的用 **CPython 3.11.15**
  构建了 PyInstaller onedir bundle，并让**冻结后的二进制**以 worker 身份完成 stdio 握手、
   实跑 master 的六个工具（`verify-backend-bundle.py`）；同时 `check-python-syntax.py --floor 3.11`
   对 685 个随包 Python 文件逐个 compile，`--floor 3.8`（Win7 线）同样全绿。
   过程中抓到的两个 3.11 语法缺陷见 `packaged-bundle.md` §2、§3。

**不支持（设计决定，非缺口）**

- Windows 本机脚本能力：`unsupported_platform`，**不提供**"先跑起来"的降级路径，
  也不把 POSIX 语法翻译过去。

**明确不做（另行跟进，已登记）**

- `integrations/desktop/publish.py::delete_run_input` 的 `artifact_rel` 基准目录不一致
  （既有缺陷，删除/过期不释放磁盘）—— 见 `resource-landing.md` §7。
- `client_files` 结果路径的落地（A13 的远程半边）—— 同上。
