# 阶段 8 证据：切片证据登记（8.1）与基线/A01–A36 复核（8.2）

日期：2026-10-13。

## 1. 任务 8.1：隔离/审计/凭据/审批/配额的适用证据登记

五个切片都已是 `openspec/specs/` 下登记的能力；下表的"当前版本证据"是本机**实跑**结果（复用有效结果），"后果"是任务要求的门禁语义。

| 切片 | 登记规范 | 当前版本证据（实跑） | 判定与后果 |
|---|---|---|---|
| execution-isolation | `openspec/specs/execution-isolation` | `tests/test_execution_isolation.py`：**14 passed / 8 failed**（5 项"读写越界必须被拒"在 Windows 上不生效——`iso._check_bash` 的判定是 POSIX 取向；1 项 `WinError 1314` 符号链接权限；1 项拒绝文案为空）；能力清单 `flags.execution_isolation = "default"`；归档 change `2026-10-08-harden-production-readiness` 自身 **20/38 完成**（Linux bubblewrap/seccomp 等仍未做） | **本平台证据不足** → 只阻止**对应执行面**：该部署里 `desktop_project_execution_enabled=false`、`desktop_project_scripts_enabled=false`，且 `test_desktop_execution_v2_contract.py`（63 passed）断言不可用时 `files_write_verified/scripts_verified=false` |
| audit-log | `openspec/specs/audit-log` | `tests/test_audit_query.py` **19 passed**、`tests/test_identity_audit.py` **6 passed** | 有效，可复用 |
| credential-management | `openspec/specs/credential-management` | `tests/test_identity_credential.py` **17 passed** | 有效，可复用 |
| action-approval | `openspec/specs/action-approval` | `tests/test_action_approval_consumer.py` **26 passed** | 有效，可复用 |
| resource-quota | `openspec/specs/resource-quota` | `tests/test_private_agent_quota.py` **16 passed**、`tests/test_stage2_migration_and_quota_evidence.py` **17 passed** | 有效，可复用 |

**"不足只阻止对应执行面，不阻止阶段 9 代际或非执行选择修复"已核实**：9.2 的代际守卫复验（`test_desktop_remote_host.cjs` 34/0 等）与 9.3 的非执行 choose→bind/取消/迟到回调（`test_desktop_local_selection.cjs` 29/0 等）都在**不打开执行开关**的前提下全绿（见 `generation-reverification.md`、`selection-without-execution.md`）。

**归属说明**：本次改动的 25 个文件中 **0 个**涉及 isolation/sandbox 代码（`git diff --name-only` 核对），上述隔离项失败与本 change 无关，属平台与既有实现范围。

## 2. 任务 8.2：基线与 A01–A36 索引复核（重新执行）

| 复核项 | 命令 / 方法 | 结果 |
|---|---|---|
| 规范校验 | `openspec validate harden-windows-desktop-reliability --strict` | **Change is valid**（在本次勾选十余项任务之后重跑） |
| 仓库 delta/接缝 | `python scripts/check_change_deltas.py harden-windows-desktop-reliability` | **OK (proposed): deltas consistent with the baseline, every conflicted file covered** |
| merged-source 快照 | 逐文件重算 SHA-256 对比 `evidence/merge-manifest.json` | **13/13 一致**（requirements/scenarios 均在当前增量中保留） |
| A01–A36 索引 | 解析 tasks.md（展开 `A08–A13` 等区间）与 `acceptance-local-project-execution.md` | tasks **36/36**、矩阵 **36/36**，无缺项 |
| Windows 不启用 | `contracts/desktop/v2.json` | `win32.supported = **false**`；本次改动对 `contracts/` 的改动数 **0** |
| 不改归档历史 | `git diff --name-only` + 快照哈希 | `openspec/changes/archive/` 下改动 **0** 个文件；13 份快照 SHA 未变 |
| 只改支持声明？ | 本次改动清单 | 未修改任何支持声明（`contracts/` 0 改动），仅新增实现、测试、文档与证据 |

规模复核（与 `planning-validation.md` 一致）：10 份 capability 增量、45 条 requirement、55 项任务。

## 3. 判定与未验证

- **8.1 勾选**：五个切片的适用证据已登记，当前版本有效结果已复用；隔离面在本平台证据不足，门禁语义（只阻止对应执行面）已由 v2 契约与 9.2/9.3 的独立通过共同证明。
- **8.2 勾选**：strict、delta/接缝、快照哈希、A01–A36 索引、Windows 支持声明与归档历史全部复核通过。
- **未验证**：POSIX（Linux/macOS）上的隔离正向证据与真实平台验收——需目标平台，归 stage 11–14 真机；本机仅证明"Windows 上不足即阻止对应执行面"这一门禁。
