## Why

桌面端「选择本机目录 → 确认绑定 → 在本机项目内读写/执行」的代码已随归档 change `2026-10-01-align-desktop-project-execution-with-master` 落地，但它的需求从未进入基线：

- 4 个 capability（`desktop-project-execution`、`desktop-skill-runtime`、`desktop-execution-delivery`、`desktop-project-artifacts`）在 `openspec/specs/` 中**不存在**，只留在该 change 的增量里；
- 两个既有能力 `scoped-project-browser`、`execution-isolation` 的追加场景也未应用；
- 能力在 `auth/capability_matrix.py` 中保持 `accepted=False`、`open={}`，部署开关默认关闭，`contracts/desktop/v2.json` 中 `win32.supported=false`。

后果是功能既不可验收也不可宣称可用，且当下**选择目录这一步就无法完成**：Electron 主进程把 generation 在 `did-navigate` 与 `did-finish-load` 上各加一次，而 preload 只握一次手并永久缓存，两者时序一错，该文档的所有本机调用都被 `host-bridge.ts` 的 `stale_context` 拒绝（成员侧表现为点击「选择本机目录」弹 `the calling document belongs to an earlier generation`，对话中也读不到所选本机项目文件）。需求基线缺失，使这个问题既没有可验收的口径，也没有可归档的承载。

本 change 的目标：**把需求补进基线、修掉阻塞选择的缺陷、按发布策略放开能力，并用真实安装包产出验收证据**；在证据齐备前不改变 `accepted` 的对外含义。

## What Changes

- **补基线**：把归档增量中未应用的 4 个新能力规范与 2 个既有能力的追加场景，以增量（ADDED / MODIFIED）纳入本 change，并同步到 `openspec/specs/`。
- **修阻塞**：消除文档 generation 的重复自增，使一次加载只推进一代；保留"文档被导航替换后旧文档不得再调用"的守卫语义不变。
- **开能力**：在能力声明与部署开关上放开桌面项目文件执行（含脚本面按需），并按本期发布范围**保持 Windows 不启用**——Windows 继续如实报告 `platform_unsupported`，不得以声明放宽冒充平台已支持。
- **同步门禁**：把断言"本能力保持未验收"的发布门禁测试更新为本 change 的口径，并在真实安装包上执行验收矩阵，证据落入 `evidence/`。
- **不改数据归属**：`identity.db` 继续唯一持有身份、设备、绑定与连接租约；本机绝对根只存在于本机注册表，不上传、不入服务器。

## Capabilities

### New Capabilities

- `desktop-project-execution`：本地与远程桌面会话打开本机项目后的执行位置、技能延续、工作目录与产出规则。
- `desktop-skill-runtime`：本机执行端的技能包版本化、资源完整性、目录分离与凭据范围。
- `desktop-execution-delivery`：执行协议协商、身份双端重验、幂等、取消与离线真实状态。
- `desktop-project-artifacts`：业务产出定位、工件来源记录、文件面板/引用一致性与安全预览。

### Modified Capabilities

- `scoped-project-browser`：区分"原地打开本机项目"与"复制导入"，明确普通浏览接口不因本机能力被放宽，并追加本机绑定重验场景。
- `execution-isolation`：明确 OS 执行边界在多租户语境下的收紧与追加场景。

## Impact

- 桌面接入点：`desktop/src/main/remote/container.ts`、`remote-preload.ts`、`remote/host-bridge.ts`、`remote/remote-container-ipc.ts`、`project-browser/`、`local-files/`、`project-execution/`。
- 服务端接入点：`auth/capability_matrix.py`、`integrations/desktop/`（`execution_capability`、`local_root`、`devices`、`execution_broker`）、`channel/web/fork/handlers/desktop.py`。
- 契约与配置：`contracts/desktop/v2.json`（本期**不改** `win32`）、`config.json` 中的部署开关（**不改** `config.py` 的默认值）。
- 发布门禁测试：`tests/test_desktop_release_gates.py`、`tests/test_desktop_execution_v2_contract.py`、`tests/test_desktop_execution_broker.py`、`tests/test_desktop_meta.py`、`tests/test_desktop_compatibility_matrix.py`。
- 数据唯一归属：身份/设备/绑定/租约归 `identity.db`；本机目录的绝对路径只在本机注册表与主进程内存，绝不出现在请求体、响应或服务器存储中。
- 跨 change 依赖与协调：`harden-production-readiness`（P0 执行隔离、P1 真实安装包发布验收与 Windows 处置口径）、`fix-desktop-relogin-session-sync`（重登与上下文同步）；依赖切片 `execution-isolation`、`audit-log`、`credential-management`、`action-approval`、`resource-quota`，并按切片引用其证据，不代勾他人未完成项。
