# 执行层版本与平台兼容部署说明

本文件说明本次变更后，**服务端与桌面端在「协议 major × 平台 × 开关 × 接受态」各组合下的行为**，以及**部署方需要知道什么**。对应变更 `add-windows-local-project-access` 的任务 3.5（"在合成测试环境验证现有开关/平台证据/协商/授权组合并补兼容部署说明；不为测试放宽旧 v2 门禁或提前验收 Windows"）。

**读法约定**：下表每一行都对应仓库里可运行的测试，文件末尾给出对应关系。未写明的组合视为**未支持**。

## 1. 三个各自独立的概念

混为一谈会直接导致误部署，所以先分开：

| 概念 | 由谁决定 | 典型取值 |
|---|---|---|
| **合同是否准入该平台** | `contracts/desktop/v{2,3}.json` 的 `platforms` | v2：`win32` 不受准入；v3：`win32` 受准入，但只给文件工具 |
| **本部署是否接受该平台/表面** | 设置 `desktop_project_execution_platforms`、`desktop_project_scripts_platforms` 等 | 缺省**全部为空**，即"什么都没接受" |
| **本构建是否具备该组件** | 装配了哪个 helper、协商到哪个 major | 打包注入的 `fs-guard` / `project-fs-guard`；协商出的 major |

**关键**：合同准入**不等于**本部署接受。v3 准入 Windows 是"这个平台在协议上被承认"，而不是"这台机器上 Windows 写文件已经可用"——后者还要接受态与真机证据。

## 2. 协议 major

| 对端 | 服务端返回 | 桌面端 |
|---|---|---|
| v2 对端 | v2 能力块（含其对 Windows 的整体拒绝） | 按 v2 校验与构造帧 |
| v3 对端 | v3 能力块 | 按 v3 校验与构造帧 |
| 本构建不认识的 major（如 4） | 拒绝，`protocol_incompatible` | 拒绝，并**点名可服务的集合**（`3, 2`） |
| 未协商 | v2（缺省） | v2（缺省） |

**一条容易踩的规则**：**帧不能自己选择用哪套规则判它**。判定用的是**连接协商出的 major**；帧里声明的 major 必须与之一致，不一致即报错。这条是刻意的：否则宽松的那套合同会由**被检查方**挑选——一个 v3 帧发到 v2 连接上会被按 v3 校验并**通过**。

`meta` 端点是无设备、公开的，因此**只能携带一个 major**（2）。v3 通过 `hello` **按设备**协商，所以同一台服务器可以同时正确服务 v2 与 v3 设备。

## 3. 平台 × 工具的可用性

| major | 平台 | 平台准入 | 可用工具 | 说明 |
|---|---|---|---|---|
| v2 | posix | 是 | 六个（`read`/`write`/`edit`/`ls`/`search_files`/`bash`） | 与本次变更前完全一致 |
| v2 | win32 | **否** | 无 | 保持原样：v2 客户端在 Windows 上仍然整体拒绝 |
| v3 | posix | 是 | 六个 + `run_script` | 脚本面仍需开关与逐语言验证 |
| v3 | win32 | 是 | **只有文件工具** | `launcher_kinds.scripts` 在 win32 为空，所以 `bash`/`run_script` 被**点名拒绝** |

v3 的 win32 是**平台准入但工具受限**，所以校验必须问两张表。只问平台准入会让 Windows 上的 `bash` 通过——这正是本次变更修掉的一处缺口。

## 4. 开关与接受态的优先级

能力块报告"为什么不可用"时，按**操作者能行动的顺序**归因（前面的先报）：

1. 声明未实现 → `not_implemented`
2. 声明未接受 → `not_accepted`
3. 部署开关关闭 → `disabled_by_deployment`
4. 平台不受准入 → `platform_unsupported`
5. 该平台未在 `desktop_project_execution_platforms` 中列出 → `not_accepted`
6. 脚本面未在 `desktop_project_scripts_platforms` 中列出 → `not_accepted`（文件面可以单独开着）
7. 该平台无已验证语言 → `runtime_unavailable`
8. helper 不可用 → `feature_unavailable`（并附 `helper_available: false` 与 `helper_reason`）

**helper 排在最后**是有意的：只有前面全部打开之后，"重装应用"才是答案。

**空 vs 缺省**：`accepted_runtimes` 区分"平台根本没配置过"（posix 保留其能力切片已接受的脚本面）与"配置为空列表"（操作者写下"这里没有已验证语言"，关面）。把两者混同会**静默关掉每一个从未写过该设置的部署的脚本面**。

## 5. 部署方需要做的（Windows）

按顺序，且**每一步都以证据为前提**：

1. **不要**通过设置 `desktop_project_execution_platforms` 之类去"打开" Windows，除非该平台的验收证据（W02–W08）已经产出。本变更**不**附带该证据。
2. 打包需注入 **两个** helper：`fs-guard`（只读）与 `project-fs-guard`（含写入）。只注入前者时写入路径不可用，而读取照常——这是刻意分开的两个入口。
3. helper 若缺失或不可信，设备应如实上报；能力块会以 `feature_unavailable` 加 `helper_reason` 说明。
4. 脚本面在 Windows 上**依合同即关闭**，任何接受清单都打不开它——合同没有为 win32 命名脚本启动器。这是合同的性质，不是配置疏漏。

## 6. 明确不做的事（本次变更的边界）

- **不为让测试通过而放宽旧 v2 门禁。** v2 在 win32 上整体拒绝的答案、以及它的消息文本，都逐字保持不变（有测试钉住）。
- **不提前验收 Windows。** 合同准入 win32 是协议层面的事实；`files_write_verified`/`scripts_verified` 与接受态仍必须由真机证据给出。
- **不在未协商时执行。** 未协商或协商失败一律按不可用处理，不回退到 v1 的只读操作。
- **不让帧自选规则**（§2 末）。
- **不把"读"与"写"合成一个入口**：只读 helper 里没有写操作可用，因为那些操作不在该构建中。

## 7. 对应测试

| 本文件的结论 | 钉住它的测试 |
|---|---|
| §2 major 选择、帧不能自选、不可服务 major | `test_desktop_execution_contract.cjs`（26 项） |
| §3 平台 × 工具矩阵（含 v2 不变） | 同上，`v3 admits win32 as a platform but not every tool on it` |
| §4 优先级、空 vs 缺省、helper 位置 | `test_desktop_execution_capability_v3.py`（28 项） |
| §4 按 major 返回不同能力块 | 同上，`VersionSelectionTests` |
| §6 v2 门禁未放宽 | `test_desktop_execution_v2_contract.py`（v2 合同套件；与 capability_v3 合计 90 项全过） |
| §6 未协商不执行、v2 语义不变 | `test_desktop_command_runner.cjs` 的 `a v2 frame keeps the report-and-apply semantics`、`without the seam the runner behaves exactly as it always did` |
| §6 两个 helper 入口分离 | `test_desktop_guard_binary_roles.cjs`（9 项）与 `openspec/.../evidence/fs-guard-platform-support.md` |

## 8. 未验证 / 不得读作已支持

- **Windows 真机与真包未验收**：本文件描述的是代码在这些组合下的行为，不是"Windows 上已可用"。W02–W08 需要 Win10 22H2 与 Win11 实机。
- **POSIX 运行级回归未做**：本机无 POSIX 主机，unix 侧只到编译级（`cargo check`）。
- **`device-client` 与 `local-read-assembly` 的 major 处理已实现但未经真机连接验证**：它们的测试是进程内/合成级别的。
