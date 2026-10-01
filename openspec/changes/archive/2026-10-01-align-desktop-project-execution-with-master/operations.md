# 本机项目执行：用户说明、运维参数与故障恢复

本文件是 change `align-desktop-project-execution-with-master` 的交付文档（任务 11.5），
也是运维在**打开开关之前**必须读的那一份。它只描述**已经实现并有用例证据**的行为；
每条结论的证据位置见文末「证据索引」，未实现的范围集中在「已知边界」一节，不在这里
用「即将支持」之类的话糊过去。

> 生效前提：本文描述的 v2 能力**默认关闭**，且其能力切片 `accepted=False`。仅当
> 部署同时打开开关**并且**该切片验收完成后，相关入口才会真正可用（见「三、运维参数」）。

---

## 一、用户说明：两种模式，数据各在哪一边

### 1.1 一张表说清谁在干活

打开本机项目之后，"Agent 在谁的机器上干活"取决于**这一轮用的是哪个工具**，而不是
取决于用户在哪台机器上聊天：

| 用户看到的事 | 谁执行 | 文件/数据在哪 | 是否离开本机 |
| --- | --- | --- | --- |
| 读/写/改/列举/搜索**项目里的文件** | **本机**（桌面端 worker） | 用户自己选的目录，原地读写 | 只有工具结果与必要的文件引用回传 |
| 在项目里跑 **bash / 脚本** | **本机**（平台沙箱内） | 项目目录 + 项目内的运行临时目录 | 命令输出回传；命令文本本身来自服务器这一轮的授权 |
| 知识库检索、MCP 工具、企业 API、记忆 | **服务器** | 服务器侧租户数据 | 不下载到本机，也不在本机执行 |
| 模型推理 | **服务器**（远程模式下） | — | 会话上下文按原有规则上行 |

**关键区分**：本机执行不等于"全部工具都搬到客户端"。只有已适配的项目工具在本机跑；
其余资源跨端时**必须显式落地**（把服务器附件/技能包传到本机缓存）或**明确拒绝**，
不存在"悄悄回退到服务器目录执行"这条路径。

### 1.2 用户侧的三个概念

- **执行目标**：本机项目（`project-execution`）或只读来源（`readonly-input`）。
  两者不是"权限高低"，而是**根在哪**：前者在本机目录里读写，后者仍然在服务器 cwd 里工作。
- **打开项目**：通过原生目录选择器**显式**选一个本机目录。这是唯一产生
  `project-execution` 授权的动作，重启后需要重新选择（历史记录只作为**候选**，不自动恢复授权）。
- **只读 vs 可写**：目录授权分只读与可写两种。只读授权**不会**因为"选了项目"而升级成可写；
  技能缓存**永远只读**，项目产出不进缓存。

### 1.3 必须记住的四条用户可见边界

1. **本机根路径不出机器**：目录的绝对路径只保存在客户端；服务器与模型只拿到工作区标识
   与相对路径。用户显式让 Agent 跑 `pwd` 这类命令，输出的内容里可能有路径——那属于
   "用户主动索取的命令输出"，**不等于**产品对绝对路径做了脱敏保证。
2. **缺依赖就是明确失败**：技能声明的依赖没打进安装包时，会报
   `dependency_missing` / `dependency_undeclared` / `python_incompatible`，
   **不会**静默降级、也不会声称成功。
3. **结果不明不等于没发生**：本机命令在回执丢失、进程失联或启动窗口崩溃后可能落在
   `outcome_unknown`。此时的正确做法是**先看原文件和客户端 journal** 再决定，
   系统**不会**自动重跑（自动重跑会让副作用发生两次）。
4. **撤销即时生效、但收回不了已经离开的字节**：撤销本地授权会立刻停止新的读取并取消
   未完成的读取；已经返回或上传的字节收不回来，产品不做这种承诺。

---

## 二、运维参数

### 2.1 三个开关（`config.available_setting`）

| 设置键 | 默认 | 打开后得到什么 | 关掉会怎样 |
| --- | --- | --- | --- |
| `desktop_project_execution_enabled` | **`False`** | v2 文件工具（read/write/edit/ls/search_files）可走本机项目 | 新调用全部按 `503 feature_unavailable` 拒绝；已有命令记录不删不改（含 `running` / `succeeded` / `outcome_unknown`） |
| `desktop_project_scripts_enabled` | **`False`** | v2 `bash` 帧可在执行端（本机 worker）运行 | 同上；脚本能力独立于文件工具，可以只开前者 |
| `desktop_local_files_enabled` | `True` | v1 只读本机文件能力（列目录/元数据/受限搜索/读取/导出） | **与本 change 无关**，含义与默认值都未改动；打开 v2 不会收窄它 |

约定（与 v1 阶段开关同一套，任务 11.2 已用用例钉住）：

- 取值**不认识或无法解析**时一律当作**关闭**，不当作打开；
- 键缺失时回落到**声明默认**，不是 `None`；
- `desktop_local_files` **不出现**在 v2 的开关表里——新增能力时顺手收窄或改名既有的只读面
  是回归，不是收紧。

### 2.2 为什么"打开开关"还不够：三重门与解释码

一个能力是否可用，是**三个独立输入的交集**：

```
可用 = implemented（代码在） × accepted（批次验收过） × configured（部署打开）
       × platform（本平台支持）
```

- 只开开关、切片未验收 → 仍然不可用，且理由必须是 `not_accepted`；
- 切片已验收但开关关掉 → `disabled_by_deployment`；
- 代码确实不存在（例如 `desktop_local_processing`）→ `not_implemented`。

**判序固定为 `not_implemented` → `not_accepted` → `disabled_by_deployment`**，
顺序颠倒会把"还没验收"说成"被开关关掉"，让运维去翻一个不存在的配置项。

**报告 ≠ 授予**：`/api/desktop/meta` 的 `features` 块是**报告**；handler 读的是切片
`enabled` 的实际授予（`open` 集合）。两个 v2 切片的 `open` 当前为空集，因此即便
`accepted` 被测试补丁置真、开关全开，也**没有任何动作被打开**。

### 2.3 平台矩阵

| 平台 | 本机文件读写 | 本机脚本（bash） | 依据 |
| --- | --- | --- | --- |
| macOS | 已验收（真实内核拒绝断言） | 已验收（seatbelt 沙箱，含派生进程与越界读写） | `evidence/platform-probes.md` §2.1 |
| Windows | 未在真机验证 | **明确拒绝**：`unsupported_platform`，不提供"先跑起来"的降级路径 | 任务 4.5 未完成；`resolveScriptSupport` |
| 其他发布平台 | 依支持表逐平台落实，未落实的平台不声明脚本可用 | 同左 | 任务 4.5/4.9 |

**不要**用 `execution_isolation` 的配置值代替平台证据：内核拒绝必须是**真实进程**断言。

### 2.4 配额与审计（运维需要知道的量）

- 有副作用的命令**独占**执行（同一实际项目根上 `effectful_per_project = 1`），只读共享 4 条；
  调度键取**解析后的实际根**，所以同一目录的两个别名会互相串行。
- 硬配额用的是既有 `quota_usage` 表，**没有第二套业务配额**。prepare 与 start 各查一次。
- **退费只在"没买到机器时间"时发生**：设备没看到这条命令（规划拒绝、设备离线、句柄对外/过期、
  队列拒绝、开始前取消）才释放；`started_at` 有值的命令**永不退费**，取消也一样计费。
- 审计三笔：`desktop.execution.start` / `terminal` / `outcome_unknown`，均带
  `run_id` / `tool_call_id` / `journal_id` / `permit_id` / `connection_epoch` / `grant_version`，
  且**不含**路径、令牌或环境变量。

---

## 三、故障恢复台账

### 3.1 错误码 → 含义 → 处置

服务端 broker 的错误码（HTTP 状态见括号）：

| 码 | 含义 | 处置 |
| --- | --- | --- |
| `feature_unavailable` (503) | 开关关闭；该能力未开放 | 这是**预期**的关闭行为，不是故障。要开就先确认切片已验收，再打开对应开关。 |
| `protocol_incompatible` (400) | 客户端/服务端协议版本不匹配 | 升级客户端或服务端；**不要**试图用旧端继续 |
| `device_offline` (503) | 设备不在活连接上 | 唤醒/重连设备后重试；系统**不会**改投其他设备或全局目录 |
| `stale_context` (409) | 连接纪元已过期（换绑、重连、切租户） | 重新建立绑定后重试，旧纪元不得继续 |
| `grant_revoked` (403) | 项目授权已被撤销 | 重新显式选择目录；**不**要用旧授权继续 |
| `approval_required` (403) | 该工具动作需要单动作审批，而命令没有有效审批 | 走既有审批流程重新取得审批（绑定实际命令/摘要） |
| `permission_denied` (403) | 资格不足（成员/Agent/工具/技能） | 检查工具白名单、Agent 工具范围与技能授权 |
| `limit_exceeded` (413) | 硬配额用尽（`tool_calls` 并发额度），或超过传输/展开/文件数上限 | 等窗口重置或提额；预留已在失败路径释放。文件名里的 `quota_exhausted` 是对同一条拒绝的**别名说法**，线上码是 `limit_exceeded` |
| `permit_expired` (409) | 开始许可过期 | 重新 `prepare` → `start`；许可不可续用 |
| `already_started` (409) | 同一条命令的许可被二次消费 | 不要重试启动，去查该命令的终态 |
| `command_conflict` (409) | 同 ID 不同摘要 | 这是**未授权**的重放，不要放宽校验去"让它过" |
| `queue_full` (429) | 设备队列满 | 稍后重试；不要无界堆积 |
| `deadline_exceeded` (504) | 前台超时 | 按超时处理；已写入的文件**保留** |
| `resource_not_found` (404) | 引用的命令/句柄/许可不存在 | 核对作用域（跨会话/跨项目的引用按设计查不到） |
| `resource_unavailable` | 设备侧磁盘不足等资源问题 | 清理磁盘后重试；**绝不**当作成功 |
| `file_changed` | 写入前检测到文件被外部修改 | 重新读取内容后再决定；**不**静默覆盖 |
| `unsupported_platform` (422) | 该平台不支持此能力 | 换平台或放弃；不做降级 |
| `incompatible_skill` (422) | 技能与设备平台/版本不兼容 | 换技能版本；不要假定它能跑 |
| `internal` (500) | 未预期错误 | 带 `run_id`/`tool_call_id` 报障 |

本机侧（worker / 缓存 / 依赖）的稳定错误码：

| 码 | 含义 | 处置 |
| --- | --- | --- |
| `not_authorized` | 缓存中仍有字节，但本轮授权回调拒绝 | 重新授权；**不**回落使用其他已缓存版本 |
| `dependency_missing` / `dependency_undeclared` / `python_incompatible` | 技能依赖缺失 / 未声明 / 解释器不兼容 | 报给技能维护者补声明并重新打包；消息里带技能名、模块名与发行名 |
| `secret_refused` / `path_outside_package` / `size_mismatch` / `digest_mismatch` / `undeclared_file` / `missing_file` | 技能包校验不通过 | 按码定位；不跳过校验 |
| `platform_unsupported` | 清单平台与**设备**平台不匹配 | 换设备或换技能版本 |

### 3.2 结果不明（`outcome_unknown`）——最需要谨慎的一类

出现条件：开始意图已落盘但回执从未到达（回执丢失、启动窗口崩溃、进程失联）。

- 记录会持久化为**失败 + `code=outcome_unknown`**，`effects=unknown`，
  审计写独立的 `outcome_unknown` 动作，**绝不当成功，也绝不当"什么都没发生"**；
- `status` 会返回 `reconcile` 提示：先看原文件与客户端 journal 再决定，**不要自动重跑**；
- 设备恢复后重连**先查后跑**：不会重复启动已有 `started` 的记录；迟到的回执只能**补充**核对结果，
  不能重开运行或重复发布成功消息；
- journal 保留期 7 天。**`unknown` 记录按状态存活，不按时间回收**——回收它等于销毁
  "可能已生效"的唯一证据；被回收的记录留**墓碑**，键继续占位，所以"证据过期"不会让命令重新可执行；
- journal 读不出来时**fail closed**：原字节改名隔离为 `.corrupt`，在运维核对前**拒绝所有副作用命令**。

### 3.3 回退演练（A32）结论

关闭 `desktop_project_execution_enabled` 之后：

- `prepare` / `start` / `heartbeat` / `status` 四条路径**全部**以 `503 feature_unavailable`
  拒绝（在任何授权判断之前）；
- 在途（`running`）、已完成（`succeeded`）与 `outcome_unknown` 的命令记录、
  许可与句柄**逐字段不变**——被拒绝的调用不得顺手终止在途行；
- 重新打开后回答**原样回来**：`running` 仍带 `reconcile`，`succeeded` 不带，
  `unknown` 仍 `outcome_unknown` + `effects=unknown`，且**没有**任何自动补发；
- 关闭期间既**不委派**，也不把工作改投某个服务器目录（解析不到本机目录时返回**拒绝**，
  而不是退回 `.`）。

**这是一处取舍，要明确告诉用户**：关闭开关时 broker 的**全部**路径（含 `status`）一律 503，
目的是让客户端立刻拿到可解释的原因；代价是关闭期间服务器侧也不回答。
行本身没丢，重开即原样读回。

### 3.4 迁移与升级

- v2 的增量迁移编号为 **41—44**，在既有 head 40 之上顺序追加。每个迁移体与其版本标记
  在**同一个事务**里落地（`sqlite3.executescript` 会先隐式提交，故迁移体改由
  `_MigrationConnection` 用 `sqlite3.complete_statement` 逐条执行），因此进程在两者之间被杀
  不会把库留在"结构改了一半、没有记录"的永久打不开状态；
- `_add_missing_column` 作为纵深防御，救的是**已经被旧版本弄成半应用**的真实机器；
- 出包优先 **Python 3.11**，迁移实现刻意避开 3.12+ 才有的 `autocommit`；
- 升级**不撤销也不重建**已有注册表行（`installation_id` 不轮换、`grant_version` 不变、
  `revoked_at` 仍为 `NULL`），旧设备/绑定/授权保留原 id；
- 变更前的只读目录授权升级后读成 `readonly-input`，**不会**凭空产生任何
  `project-execution` 授权；用户必须**显式**打开本机项目才获得执行授权。

---

## 四、跨 change 归档顺序（摘要）

完整表与判定口径见 `integration-map.md` §3 与 §5.1–5.3。要点：

1. 五个相关 change（`add-desktop-remote-web-workbench`、
   `fix-desktop-local-context-and-tool-calls`、`use-personal-workspace-for-shared-agents`、
   `land-shared-agent-panel-on-own-files`、`guard-shared-knowledge-skill-writes`）
   **当前全部未归档**，所以主规范里没有"本机能力一律只读"的净效果，与本 change 不冲突。
2. 但它们原文里有三条只以「首版」限定、归档后容易被读成**全局禁令**的表述
   （`desktop-local-file-access` 的"不提供删除/覆盖/Shell/任意脚本"、
   `desktop-local-processing` 的"不得传入脚本"、`desktop-file-transfer` 的自动写回禁令）。
   归档时必须**保留禁令、加模式限定**，不能删改。
3. 推荐顺序：先归档 v1 三个 change（加限定），最后归档本 change；若顺序被迫反转，
   逐条加限定并保留本 change 新能力的全部 requirement。每次归档后重跑
   `openspec validate <change> --strict`。

---

## 五、已知边界（不声称已完成）

- **未在安装包内验证**：本文与相关证据都在开发机（macOS/posix）上完成；
  签名后、随包 Python 的真机验收属任务 10.1–10.3，**未做**。
- **Windows 未验证**：脚本能力在 win32 明确拒绝，进程树停止按平台分支实现但未在真机验证。
- **无硬性内存/CPU 上限**：`RLIMIT_AS` 在 macOS + CPython 下不可靠，故资源预算的缺口如实记录，
  不假装有硬上限。
- **真实模型未参与**：多数编号证据里的"模型"是注入到工具分派的工具调用，
  不是字面 LLM；真机 + 真模型验收属第 10 组。
- **`accepted=True` 尚未置真**：本轮没有修改任何切片的验收状态，只由测试补丁驱动。

---

## 六、证据索引

| 主题 | 文件 |
| --- | --- |
| 开关默认值、三重门与解释码判序 | `evidence/release-gates.md` §二；`tests/test_desktop_release_gates.py` |
| 迁移原子性、回读与升级不扩权 | `evidence/release-gates.md` §一；`tests/test_desktop_project_migration_drill.py` |
| A32 回退演练 | `evidence/release-gates.md` §四；`tests/test_desktop_execution_broker.py::RollbackDrillTests` |
| 授权/资格/审计/配额（治理切片） | `evidence/governance.md` |
| 平台边界与真实内核拒绝 | `evidence/platform-probes.md`、`evidence/isolation-acceptance.md` |
| 故障注入 A14—A20 与 unknown/恢复 | `evidence/faults.md` |
| worker 环境剥离与凭据边界 | `tests/test_desktop_local_worker.py::WorkerEnvTests`；`desktop/src/main/local-execution/env.ts` |
| 技能依赖与打包守卫 | `evidence/skill-dependencies.md` |
| 产出卡片与来源 | `evidence/artifact-source.md` |
| 跨 change 归档顺序 | `integration-map.md` §3、§5 |
