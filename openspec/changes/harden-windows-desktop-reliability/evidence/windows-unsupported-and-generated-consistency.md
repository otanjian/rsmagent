# 阶段 4 证据：Windows 未支持、能力提示与生成一致性（任务 4.1、4.4）

日期：2026-10-13。

## 1. 任务 4.1（**勾选**）

> 核对既有声明与执行规范，保持 Windows 未支持；能力提示不等待执行开放，验证无重复规范或错误继承完成状态。

| 核对项 | 证据 |
|---|---|
| **Windows 未支持** | `contracts/desktop/v2.json`：`win32.supported = false`；`tests/test_desktop_execution_v2_contract.py` **63 passed**（含 win32 上无可用工具、按平台拒绝）；本次改动对 `contracts/` **0 改动** |
| **无错误继承完成状态** | `auth/capability_matrix.py`：`Slice("desktop_project_execution", implemented=True, **accepted=False**, open={})`，脚本面同样登记；`check_consistency` 不变式保证 accepted↔open 一致——A 工作包的成果不会让执行面显示为"已验收" |
| **能力提示不等待执行开放** | `tests/test_desktop_meta.py` **11 passed**，其中 `DesktopMetaSwitchReflectionTests`：用例名即"**the switch is reflected, and never opens a capability on its own**"；`test_acceptance_and_switch_together_open_it` 要求"验收 + 开关"同时成立；验收齐备但开关关闭时 `reason == "disabled_by_deployment"`（如实说明是部署关闭，而不是伪装成损坏或不可用）；"missing implementation is blamed before the switch"；`features.local_processing.available == False` 且 `reason == "not_implemented"` —— 能力块**始终存在并带原因**，不依赖执行面开放 |
| **无重复规范 / 第二套能力系统** | 全仓只有 `auth/capability_matrix.py`（声明）与 `integrations/desktop/execution_capability.py`（组合态 + 开关）两处；本次改动对 `auth/` **0 改动**，未新增声明、快照或缓存 |

## 2. 任务 4.4（**勾选**）

> 按能力关闭新增入口、保留已有绑定原因/清除/云端切换，补双语说明；Windows不提示重登恢复，上传/云端正常，旧响应失效，必要契约变化检查生成一致性且 win32.supported 不变。

| 要求 | 证据 |
|---|---|
| 按能力关闭新增入口 | 能力块 `available=false` 驱动关闭；桌面项目面板由宿主能力闸门决定（`canPreview()` / `canUseProjectPreview()`，见 `tests/test_desktop_isolated_preview.cjs`：**26 passed / 1 failed**，唯一失败是 `EPERM: symlink`——本会话无符号链接权限，与能力关闭无关） |
| 保留已有绑定原因 / 清除 / 云端切换 | `tests/test_desktop_project_execution_grant.cjs` **10/0**、`tests/test_desktop_local_context.py` **31 passed**、`tests/test_desktop_signout.cjs` **9/0** |
| 补双语说明 | 4.3 已补 `memory_hint_*` 与 `ws_local_{helper_missing,device_failed,platform_unsupported}`（简中/繁中/英文）；`tests/test_workspace_local_refusals.cjs` **6/6** 断言三种语言齐备 |
| **Windows 不提示重登恢复** | 同一测试断言 helper 文案必须**点名组件并要求安装/重建**，且**不得出现"重新连接/reconnect"**；平台文案不含重登引导 |
| 上传 / 云端正常 | `tests/test_console_workspace_upload_frontend.cjs` **34/0**（上传路径不因本机能力关闭而受影响） |
| 旧响应失效 | 9.2 代际守卫（`test_desktop_remote_host.cjs` 34/0）与 9.3 迟到/被取代选择（`test_desktop_local_selection.cjs` 29/0） |
| **契约为源、生成一致、win32 不变** | `tests/test_desktop_execution_types.py` **7 passed**：断言 `generated-contract.ts` 与 `contracts/desktop/v2.json` 生成一致、且文件内**不含任何 CR**；本次改动对 `contracts/` 与 `generated-contract.ts` **0 改动**；`win32.supported=false` 未变 |

## 3. 本轮附带修好的环境问题（行尾）

核对生成一致性时发现：`generated-contract.ts` 的 **blob 是 LF** 而工作区却是 **CRLF**，导致 `test_desktop_execution_types.py` 的 `assertNotIn(b"\r", raw)` 失败。根因是**我在本会话早先把 `core.autocrlf` 设成了 `true`**，git 于是把 LF blob 写成 CRLF 工作副本——这既与仓库实际存储不符，也会把行尾改动混进任何提交。

处理（已核实、内容保持）：

1. `core.autocrlf` 改回 **false**；
2. 对 **2,231** 个"工作区比 blob 多出字节"的已跟踪文件做 CRLF→LF 规范化：未修改的文件**逐字节等于其 blob**（逐文件校验，0 失败），本会话改过的 20 个文件只做同样的行尾变换、内容不变；
3. untracked 只处理本会话新增且属于 `tests/ docs/ openspec/changes/<本 change>/ desktop/src|build/ scripts/` 的文本文件，**不碰** `run.log`/`config.json`/`nohup.out`/`tmp/` 等运行产物；
4. `git add --renormalize .` 刷新 index 的转换标记后复位索引。

**结果**：`git status` 回到干净的 **25 modified + 55 untracked**，与 `origin/rdai` 的真实差异仍为 **25 files, +720/−304**；连带修好三处此前被我归因为"本机 CRLF 检出"的失败：

| 测试 | 之前 | 现在 |
|---|---|---|
| `test_desktop_execution_types.py` | 1 failed / 6 passed | **7 passed** |
| `test_desktop_host_frontend.cjs` | 20 / 1 | **21 / 0** |
| `test_desktop_isolated_preview.cjs` | 23 / 4 | **26 / 1**（余 1 为 `EPERM: symlink` 权限） |

反例也确认了：`test_console_i18n_parity.cjs` 仍是 **3 项失败**（词典与快照的既有漂移，与行尾无关，见 `memory-error-classification.md` 第 4 节）。

## 4. 判定与未验证

- **4.1 / 4.4 勾选**：Windows 未支持、无错误继承完成状态、能力提示始终带原因且开关永不单独开启能力、无重复能力系统；入口按能力关闭、绑定/清除/云端切换保留、双语说明与"不提示重登"、上传正常、旧响应失效、生成一致且 `win32.supported` 不变——均有上面列出的测试与配置证据。
- **未验证**：真实 Windows/macOS 客户端与页面会话里的入口关闭与交互观感（需真机）；符号链接相关用例仍需具备 `SeCreateSymbolicLinkPrivilege` 的会话。
