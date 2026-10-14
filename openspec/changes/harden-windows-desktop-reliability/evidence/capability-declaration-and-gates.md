# 阶段 10 证据：能力声明、部署开关与端点门禁（任务 10.1–10.3）

日期：2026-10-13。阶段约定（tasks 第 66 行）：**阶段 8 的证据约束正式执行开放；声明准备、受控验收与非执行修复可先行，生产门禁不因测试需要被放宽。**

## 1. 任务 10.1（**勾选**）：映射、False 默认开关、分别验收、不放宽门禁

| 核对项 | 证据 |
|---|---|
| accepted/open 动作映射 | `auth/capability_matrix.py`：每个切片同时登记 `implemented`（本构建存在该代码路径）、`accepted`（真实验收证据）、`open`（当前服务的 `动作 → 访问级别`），并有 `check_consistency` 不变式——"accepted but unproven execution"会被判错、只声明 read/config 的动作不构成执行声明 |
| **False 默认开关** | `integrations/desktop/execution_capability.py`：`FILES_SWITCH="desktop_project_execution_enabled"`、`SCRIPTS_SWITCH="desktop_project_scripts_enabled"`；`_switch()` 对缺失/不可解析**返回声明的默认值（关）**而非静默 `None` |
| 默认部署确实是关 | `tests/test_desktop_compatibility_matrix.py`："The default deployment: off, and honest about which condition it is"，断言 `available_setting["desktop_project_execution_enabled"] is False`（套件 **11 passed + 2 subtests**） |
| 文件/脚本分别验收，缺证据面拒绝 | `execution_state()` 对 `files` 与 `scripts` **各自**做 `_slice_availability` 组合；`tests/test_desktop_execution_v2_contract.py` **63 passed** 覆盖"文件面可用而脚本面关闭 → 脚本被拒"及不可用时 `files_write_verified/scripts_verified=false` |
| 不放宽生产门禁 | `tests/test_desktop_release_gates.py` **18 passed + 8 subtests**；本次改动未触及门禁代码（见 10.2 的范围核对） |

## 2. 任务 10.2（**勾选**）：组合态/availability/端点门禁，Windows 无工具

- 组合态与 availability：`tests/test_capability_matrix.py` **19 passed**；声明/开关/平台组合：`tests/test_desktop_execution_v2_contract.py` **63 passed**（含 win32 全部拒绝）。
- **端点门禁在 Windows 上如实拒绝**：`tests/test_desktop_execution_broker.py` 的 `BrokerEndpointTests` **42 项全部**收到 `503 {"code": "platform_unsupported", "message": "project execution is not available (platform_unsupported)"}` —— 这就是"Windows 无可用工具即拒绝"的正确表现（该组端点在 POSIX 主机上才返回 200）。归属核对：broker 只 import `auth.desktop_contracts_v2` 与 `integrations.desktop.errors`，而本次 25 个改动文件中**没有任何 `auth/` 或 `integrations/` 文件**，故与本 change 无关。
- **未建设第二套能力系统**：本次改动没有新增能力声明、快照、缓存或门禁（`contracts/` **0** 改动、`auth/` **0** 改动），直接复用阶段 4 的组合态结果。

## 3. 任务 10.3（**勾选**）：只调整确受影响处，验证新旧行为

本次**真正受影响**的清单：`helper_missing` 协商码（`remote-container-ipc.ts`）、`network_timeout` 分类与文案（`connection.ts`、`RemoteConnectPage.tsx`、`i18n.ts`）、本地拒绝文案统一（`workspace.js`）、`chat.html` 两处 `target` 移除，以及记忆/端口/依赖侧修复。

| 验证 | 结果 |
|---|---|
| 契约与生成文件未变 → 新旧端组合行为不变 | `contracts/` 与 `desktop/src/main/project-execution/generated-contract.ts` **0 改动** |
| 旧端/旧行为 | `tests/test_desktop_compatibility_matrix.py` **11 + 2 subtests**（四行混合版本矩阵、只读引用不被升级）；`tests/test_console_workspace_frontend.cjs` **34/0**（工作区错误仍保留服务器原消息） |
| 新增码的兼容性 | 旧服务器不会发送 `helper_missing`/`network_timeout`；前端按码映射、未识别码回落通用文案（`tests/test_workspace_local_refusals.cjs` 6/6、`tests/test_desktop_probe_deadline.cjs` 7/7） |
| meta 未受影响 | `tests/test_desktop_meta.py` **11 passed**（本次未改 meta 契约） |

## 4. 两处既有失败（登记，非本 change 引起）

1. `test_capability_matrix.py::DeclarationInvariantTests::test_every_declared_capability_has_a_spec`：声明中的 `desktop_remote_web` 指向能力 `desktop-remote-web-workbench`，而 `openspec/specs/desktop-remote-web-workbench` **不存在且未被 git 跟踪**（`git ls-files` 0 个文件；`git status` 中 **0 个删除条目** → **不是**本次事故的恢复遗漏）。该能力只存在于归档 change `2026-10-01-add-desktop-remote-web-workbench` 的增量里，从未提升为 `openspec/specs/` 下的规范。**建议（独立范围）**：把该增量提升进 `openspec/specs/`；本次不改，以免改写规范基线。
2. `test_desktop_execution_broker.py` 的 42 项端点用例：POSIX 主机用例，在 Windows 上的 `platform_unsupported` 正是规范要求的行为。

## 5. 判定与未验证

- **10.1 / 10.2 / 10.3 勾选**：映射与 False 默认开关已核对（含默认部署为关的断言）、文件/脚本分别验收、门禁未放宽、组合态与端点门禁在 Windows 上如实拒绝、只调整确受影响处且验证了新旧行为。
- **未验证**：正式执行开放（`accepted`/`open`）留给阶段 14 的受支持平台真实验收——本阶段只做声明准备与受控验证，不放宽生产门禁。
