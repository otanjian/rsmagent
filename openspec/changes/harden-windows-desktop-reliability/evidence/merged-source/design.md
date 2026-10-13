## 背景与范围

本 change 只做三件事：(1) 把既有代码对应的需求补进基线；(2) 修掉让"选择本机目录"必然失败的文档 generation 竞态；(3) 在证据齐备的前提下按发布策略放开能力声明与部署开关。**不改**产品行为边界（不新造能力、不改权限模型、不改数据归属），也**不改** `contracts/desktop/v2.json` 的 Windows 平台判定。

## 当前代码接入点

**文档代际（本 change 的核心修复点）**

- `desktop/src/main/remote/container.ts:155-161`：`registerDocument` 自增 `generation` 并登记 `shellFrame`（取 `view.webContents.mainFrame` 的 process/routing id）。**当前只在 `did-navigate` 上挂载**——先前同时挂在 `did-finish-load` 上，导致一次加载自增两代；该缺陷已由上游在 `gitea/rdai` 修正（去掉 `did-finish-load` 监听）。
- `desktop/src/main/remote-preload.ts:27-64`：`generation` 在**第一次** `call()` 时经 `CHANNEL_HELLO` 学到，并被 `handshake` Promise 记忆，**不刷新**；此后每次调用都带上这个缓存值。
- `desktop/src/main/remote/host-bridge.ts:231-254`：`checkSender` 逐项校验（webContents → 主帧 → 帧 id → `generation` → origin）。`generation` 不等即拒（`:242-244`，`stale_context` / `the calling document belongs to an earlier generation`）。
- 缺陷时序（修复前的成因记录）：页面首个 bridge 调用落在 `did-navigate` 之后、`did-finish-load` 之前 → 学到 N → `did-finish-load` 推到 N+1 → 该文档后续调用全被拒。这解释了"能力查询成功（菜单出现『选择本机目录』）但点击 `chooseWorkspace` 失败"的组合现象。

**能力与开关（三处，缺一不可）**

- `auth/capability_matrix.py:677-700`：`desktop_project_execution`、`desktop_project_scripts` 两个 Slice 为 `open={}`、`accepted=False`。
- `integrations/desktop/execution_capability.py:100-132`：`available = files.available ∧ 本平台存在可用工具`；`reason` 优先级 not_implemented → not_accepted → disabled_by_deployment。
- `channel/web/fork/handlers/desktop.py:705-724`：`_execution_enabled()` 以组合态为准；broker 各端点在失败前不改状态。
- `config.py:492-493`：`desktop_project_execution_enabled` / `desktop_project_scripts_enabled` 默认 `False`（部署开关）。
- `agent/desktop_remote/mode.py`：`readonly-input` 目标不委派（文件**引用**而非执行授权），故"选择本机目录"得到的只读引用不会让对话读取本机项目。

**平台**

- `contracts/desktop/v2.json:19-22`：`win32.supported=false`（注释：Windows 启动器与探针尚未验收）。
- `auth/desktop_contracts_v2.py:261-283`：`tool_supported_on` / `platform_supported`；win32 未支持 → 无可运行工具 → `platform_unsupported`。
- `desktop/src/main/remote/remote-container-ipc.ts:651`：`seatbeltAvailable` 依赖 `/usr/bin/sandbox-exec`，Windows 恒为 false。

**本机文件 helper（fs-guard）**

- `desktop/src/main/local-files/fs-guard-binary.ts:20-39`：候选路径为已打包资源与 `native/fs-guard/target/{release,debug}/fs-guard`。本检出无 `target/`（其 `.gitignore` 忽略 `/target`），本机也无 Rust 工具链，故 `resolveGuardBinary()` 返回 `null`。
- `desktop/src/main/remote/remote-container-ipc.ts:626-630`：`null` 时 `spawnGuard` 抛 `the local file helper (fs-guard) is not installed in this build`；`project-browser/browser.ts:316-320 / 435-438` 把该失败归一为 `errorCode: 'device_error'`。
- `desktop/native/fs-guard/src/ops.rs:17,65-68,182-241`：helper 使用 `std::os::unix` / `libc::openat` / `O_NOFOLLOW` / `fstatat` / `fdopendir`——**在 Windows 上无法编译**。因此本机项目读写在 Windows 属功能缺口，而非配置或环境问题。
- `channel/web/static/js/workspace.js:514` + `i18n/core.js:255`：`device_error`（本机失败）与 `device_offline`（确实未连接）被折叠为同一句「本机未连接，暂时无法读取或保存项目文件」，会掩盖真实原因。实测与排除过程见 `evidence/local-read-windows.md`。

**本机读路径与"未连接"提示的来源**

- `desktop/src/main/remote/local-files-bridge.ts:31-39`：`remoteLocalFilesEnabled` 默认 false，由 attach 时探测 `features.local_files.available` 置位。
- `desktop/src/main/remote/remote-container-ipc.ts:703-714`：探测 meta 后 `setRemoteLocalFilesEnabled`；detach 置回 false。
- `desktop/src/main/remote/local-read-assembly.ts`（模块头注释）：`workspace_id → {grant, helper root}` 为**内存态，重启即空**；设备连接由"确认目录"这一步启动。
- `channel/web/static/js/console.js:7919-7975`：菜单先渲染不含本机目录项，仅当 `CowDesktopHost.canChooseWorkspace()` 为真才补上。

## 方案与取舍

1. **generation 去重（已由上游采用"仅挂 `did-navigate`"）**：当前代码只挂 `did-navigate`，一次加载只推进一代；守卫语义（导航替换后的旧文档不得再调用）不变。本 change 曾自实现的"帧标识比较"变体（`(frameProcessId, frameRoutingId)` 去重、保留两个监听）已 stash 备查，未采用。
2. **preload 可刷新握手（不采用，仅记录）**：收到 `stale_context` 后重新握手并重试一次。改动面更大，且会把"被导航替换的旧文档"重新登记，削减守卫本意；仅在方案 1 无法覆盖时复议。
3. **能力放开采用"声明 + 部署开关"双条件**：`accepted=True` 并给 `open` 动作，默认开关仍为 `False`，由部署显式打开，避免新装环境误触。Windows 不在本期放宽：保持 `platform_unsupported`，并在文档中明示"未执行"。
4. **不修改归档**：`changes/archive/**` 保持原样；本 change 以**新增**增量承载（4 个新能力 ADDED；2 个既有能力 MODIFIED + ADDED），从而把基线补齐。

## 分阶段门槛

- **P0 执行边界**（依赖 `execution-isolation`，由 `harden-production-readiness` 承担）：进入 P1 前须核对该切片证据真实存在，不使用 mock 或文档完成状态代替。
- **P1 本地模式选择/确认/读写**：前置是 generation 修复；对应验收 A01–A04。
- **P2 远程模式与命令交付**：复用既有 ExecutionRun、epoch/租约、幂等与取消；对应 A05、A08–A20。
- **P3 技能运行与产物**：技能包版本/摘要、预览隔离、历史卡片定位；对应 A06–A07、A21–A25。
- **P4 打包/平台/降级**：真实安装包复跑；Windows 明确"未执行、不宣称已验证"；对应 A26–A33。
- **放开 `accepted` 的最后一道门**：`evidence/` 中 A01–A33 已在真实安装包上复现。在此之前 `accepted` 保持 `False`（`tests/test_desktop_release_gates.py:176` 即此约束的载体）。

## 迁移与恢复

- **关闭开关的降级**：有在途运行与历史记录时关闭开关，须封锁新任务、保留原文件/回执/历史，并使旧客户端可解释地显示不可用（对应 A32）。
- **重启后的本机授权**：grant 与设备连接为进程内状态，重启即失效；成员须重新通过「选择本机目录」选择一次，不得直接复用历史候选（历史候选仅作为路径提示）。
- **失败恢复**：确认失败保留原绑定并显示未就绪原因；取消不改变当前会话的有效目录，不创建授权。

## 未决实施参数

- Windows 是否纳入本期发布矩阵：当前按 `harden-production-readiness` 的范围约束为**暂缓**，主规范与 meta 继续如实报告 `platform_unsupported`。硬约束是：本机文件 helper（`fs-guard`）为 POSIX 专用实现（见「当前代码接入点 · 本机文件 helper」），Windows 需另行实现，不能靠放开声明或开关达成。
- `device_error` 与 `device_offline` 的文案是否分开，以及在哪一层区分（bridge 保留原因 vs 前端映射）：当前两者折叠成同一句，实测会把"平台不支持本机 helper"误报为「本机未连接」（`evidence/local-read-windows.md`）。
- 脚本面（`desktop_project_scripts`）是否随本期一并放开，或与文件面分阶段。
- `open` 动作集合的确切取值（读取/写入/执行各自的动作名）。
- generation 去重**已由上游实现**：`gitea/rdai`（`b5b5e7fe` 区间）在 `container.ts` 去掉 `did-finish-load` 监听、只保留 `did-navigate`——即本文件「方案与取舍」中的备选写法（仅挂 `did-navigate`）。本 change 原先自实现的"帧标识比较"版本已 **stash**（`stash@{0}`），未提交。实测见 `evidence/generation-fix.md`（需在重构建后复跑以绑到当前代码）。若真实安装包上的 A01/A02 显示仅挂 `did-navigate` 不足以识别文档替换，则复议帧标识比较。
