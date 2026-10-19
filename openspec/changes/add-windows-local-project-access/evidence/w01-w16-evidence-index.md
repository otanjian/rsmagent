# W01–W16 证据索引

对应变更 `add-windows-local-project-access` 的任务 1.3（"建立W01–W16证据索引及平台/路径支持说明，记录原生API封装/威胁模型与限制；缺符号链接权限时配置专用测试环境，不将跳过计为通过"）。

**读法**：每一行给出该验收项**当前实际拥有的证据**、**它落在哪个文件/测试**，以及**还差什么**。"未执行"一律写明未执行——本文件不把跳过、编译级检查或合成测试计为真机通过。

**结论先行**：W01–W16 中，**本机可执行的自动化部分已尽可能落实**；需要真机/真包/签名凭据的项**全部未执行**，且这不是遗漏而是环境约束（见文末）。

## 索引

| 项 | 本机证据 | 落在哪 | 还差什么 |
|---|---|---|---|
| **W01** 选择/打开根、多级中文空格路径、取消与迟到响应 | **部分** | `fs-guard-platform-support.md` §3（中文/空格/长路径往返）；`desktop/native/fs-guard/tests/stdio.rs` 的 `non_ascii_and_spaced_names_round_trip`、`a_deep_and_long_path_still_resolves` | **快速A/B 与迟到响应**需真机 UI 流程；本机只验证了 helper 层 |
| **W02** 根/中间目录 rename/reparse 替换、受控交错 | **集成级已过** | `tests/stdio.rs` 的 `a_swapped_intermediate_directory_cannot_redirect_a_read`、`a_swapped_root_requires_the_user_to_choose_again`；`src/temp.rs` 的 `a_parent_that_moved_away_cannot_redirect_the_deletion`；`tests/write.rs` 的 7 条拒绝路径 + 3 次真实写入后**根外哨兵字节/大小/mtime 全不变** | 真机上"操作进行中的受控交错"（W02 的第二半）未做 |
| **W03** 相对盘符、UNC/设备路径、ADS、尾点空格、保留名、大小写、8.3 别名 | **已过** | 两端各一份 `validate_relative_path` 单元测试；`a_name_that_differs_only_in_case_is_handled_consistently` | 真机卷上的 8.3 别名实测（结构字段已就位，策略为永不按别名解析） |
| **W04** symlink/junction/挂载点、云占位符、多硬链接 | **已过（Windows 侧实测）** | `src/paths/windows.rs` 测试（真实建 reparse point 并断言拒绝）；`a_hard_linked_file_is_refused`；unix 的 `a_device_file_is_refused`（需 `/dev/null`，故 unix-only） | **云占位符**（OneDrive 等）未实测；"无权限建链接"的专用环境未配置（见下） |
| **W05** 新建/覆盖/edit/嵌套/外部编辑器并发 | **已过** | `desktop/native/fs-guard/src/write.rs` 16 项；`a_matching_version_publishes_and_leaves_no_staging_file`；`a_stale_version_is_refused_and_the_newer_content_survives`；4 条写前阻断测试（`command_runner.cjs`） | **同 mtime/size 不同内容**的用例未单列（版本绑定含对象身份，理论上覆盖；未实测该构造） |
| **W06** 暂存/发布期间磁盘满、取消、helper 强杀、父链替换 | **部分** | `fs-guard-error-recovery.md` 的全表（每码的磁盘状态）；`publish_is_ambiguous` 的分类单元测试 | **磁盘满**与 **helper 强杀**未做故障注入——`io_error` 的"不确定"分支目前只有结构保证，无注入证据 |
| **W07** 巨量列表/输出、超大/损坏帧、取消、未知 op | **已过** | `an_oversized_frame_terminates_the_helper_rather_than_desyncing`、`unknown_operations_and_extra_fields_are_refused`、`listing_is_paged_and_reports_a_cursor`、`search_reports_the_bound_it_hit` | — |
| **W08** 只读入口请求 write；写入入口请求 bash/module/env | **已过** | `tests/stdio.rs` 与 `tests/write.rs` 两侧的白名单断言；Cargo 的 `write-ops` feature 使只读版**在类型层面**无法表达写操作 | — |
| **W09** 两模式真实模型依次调用五工具 | **未执行** | — | 需真机 + 真实模型 + 真实安装包 |
| **W10** 面板/@/搜索/预览/编辑交叉验证 | **未执行** | — | 需真机 UI |
| **W11** 丢回执、重复命令、强杀于发布/回执间、合同升级后重投同 ID | **已过（合成）** | `test_desktop_execution_journal.cjs` 23 项（含 `a crash between start and receipt becomes outcome_unknown, and is not retried`、版本隔离 3 项）；`test_desktop_command_runner.cjs` 的 A14/A15/A16 | 真机上"强杀于发布/回执间"未做 |
| **W12** 入队后撤工具/grant/成员资格、切换用户/租户/服务器及重连 | **已过（合成）** | `test_desktop_execution_broker_service.py`（撤权 → `grant_revoked`、跨设备 → `resource_not_found` 且行状态不变）；`test_desktop_execution_broker.py` 的撤权/跨租户用例（**本机现已可运行**） | 真实重连与真实身份切换未做 |
| **W13** 重启桌面/helper、恢复历史候选、同根多 alias 写入 | **部分** | `A20: two aliases of one real root serialise effectful commands`；`journal` 的 `recovery leaves finished commands finished` | **重启桌面/helper** 的端到端未做 |
| **W14** 查看本机产物、切项目/换设备、显式上传 | **未执行** | — | 需真机 + 面板 |
| **W15** v3 与新旧端/版本、operations 缺省/篡改、许可摘要不符、开关/平台状态、helper 缺失、审计配额失败 | **已过（合成）** | `test_desktop_execution_v3_contract.py`、`test_desktop_execution_capability_v3.py`（28 项）、`test_desktop_execution_operations.py`（58 项）、`execution-compatibility-deployment.md` 的八级优先级与 major 矩阵、`test_desktop_execution_v2_contract.py`（v2 门禁未放宽） | 新旧端真实连接未做 |
| **W16** 真 NSIS 安装/升级/回退、普通用户及目标 OS、关闭开关且有在途写入 | **未执行** | — | 需真安装包 + 目标 OS + NSIS（本机未安装） |

## 原生 API 封装与威胁模型（1.3 的后半句）

- **封装**：`paths/{unix,windows}.rs` 各自实现"根句柄 + 逐组件相对打开"，共享层 `ops/mod.rs` 承载全部逻辑，因此列目录/有界/取消只有**一份**受审实现。NT 细节（`NtCreateFile` 不接受 DOS 路径、相对打开拒绝 `"."`、`FileIdBothDirectoryInfo` 不能用作 `NtQueryDirectoryFile` 的信息类）记录在 `fs-guard-platform-support.md` §2。
- **威胁模型**：授权根在原生选择时打开并持续持有；路径只用于**到达**对象，绝不用于**授权**它；reparse/硬链接/设备一律拒绝；清理只按**本任务已记录的暂存对象身份**执行，绝不通过 dirname 向上推算（`fs-guard-error-recovery.md` §4）。
- **限制**：POSIX 侧只有编译级验证（无 POSIX 主机）；Windows 10 22H2 未实机；安装包路径未验证。

## 缺符号链接权限时的专用测试环境——**未配置**

`a_device_file_is_refused`（unix）与 Windows 侧的 reparse 测试**都在本机跑过**。夹具的 `Sandbox::symlink` 在 unix 上用真符号链接，在 Windows 上改用 **junction**（普通用户可建 `mklink /J`，无需 `SeCreateSymbolicLinkPrivilege`），源码注释明确写着这是为了让这些测试"tests real on Windows instead of skipping them"。唯一无法在 Windows 复现的是 `/dev/null` 那一项（字符设备），它标为 `#[cfg(unix)]` 且注释说明"this is a platform-specific *fixture*, not a skipped guarantee, and it is not counted as a Windows pass"。

因此本仓库**目前没有需要"缺符号链接权限"专用环境的测试**。若将来加入需要 `SeCreateSymbolicLinkPrivilege` 的用例，必须**为该用例单独配置环境并记录**，而不得以 `skip` 计为通过——当前无任何用例处于该状态（已逐一核对，见上）。

## 环境约束（为什么 W09/W10/W14/W16 等未执行）

| 约束 | 实测 |
|---|---|
| 本机 OS | **Windows 11**（10.0.26200），**非** Win10 22H2——W02–W08 的目标 OS 之一未覆盖 |
| 管理员权限 | **无**（`IsInRole(Administrator)` = False） |
| 签名凭据 | **无**（`CSC_*`/`SIGN*`/`CERT*` 环境变量为 0 个） |
| NSIS | **未安装**（`C:\Program Files (x86)\NSIS` 不存在） |
| POSIX 主机 | **无**——`wsl` 可执行文件存在但**没有安装任何发行版**（`wsl --install` 需管理员并重启），因此 POSIX 运行级回归不可行 |

这三项（真机目标 OS、真包、签名凭据）与目标约束一致：**不勾选，且不装作已通过**。
