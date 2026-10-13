# 选择本机目录后仍报「本机未连接」的原因（Windows）

## 结论

不是设备未连接。**本机读文件所需的 helper `fs-guard` 在本平台不可用**，读取失败被归一为 `device_error`，而 UI 把 `device_error` 与 `device_offline` 折叠成同一句文案，于是显示成「本机未连接，暂时无法读取或保存项目文件」。

## 证据

### 1. 该文案只能由 `device_offline` / `device_error` 产生

`channel/web/static/js/workspace.js:514-515`：

```js
if (code === 'device_offline' || code === 'device_error') return t('ws_local_offline');
if (code === 'stale_context' || code === 'grant_revoked') return t('ws_local_stale');
```

`channel/web/static/js/i18n/core.js:255-256`：

- `ws_local_offline` = 「本机未连接，暂时无法读取或保存项目文件」← 用户所见
- `ws_local_stale` = 「本机项目授权已失效，请重新打开项目」← 另一种码的文案，**不是**用户所见

### 2. 出问题那次进程里，设备是在线的

- `identity.db` / `desktop_connection_leases`：旧租约 `lease_hTRaiHIU...` 于 15:54:26 以 `reason=replaced` 撤销；新租约 `lease_VvDaMxOG...` 于 15:55:06 续期、15:56:06 过期 —— 当时 `LIVE leases = 1`。
- 桌面主进程日志（`b12nl5bdy`，110 行）：`[desktop] device connection ready`。

因此 `device_offline` 的条件（`project-browser/browser.ts:349-350` 的 `!binding.connected || !binding.connectionEpoch`）不成立 ⇒ 用户所见的码是 **`device_error`**。

### 3. 绑定确实建立，面板不会走到 `stale_context`

同一次进程在 15:54:26 写入了 `desktop_workspaces`（`ws_kmE9jJGCeKfTqMtqZJAWsPbD`）、`desktop_bindings`（`bind_PbK_m9IM3SU126tE-32CXXXJ`）、`desktop_binding_workspaces`。这些行与 `projectBindings.set(...)`（`remote-container-ipc.ts:721-746`）由**同一个**确认 IPC 写入，所以在该进程内面板的绑定查找会命中。

### 4. helper 在这个检出里不存在

`local-files/fs-guard-binary.ts:20-39` 的候选路径全部缺席：

```
native/fs-guard/target/release/fs-guard   missing
native/fs-guard/target/debug/fs-guard     missing
```

`desktop/native/fs-guard/.gitignore` 忽略 `/target`；本机 **没有 cargo/rustc**。于是 `resolveGuardBinary()` 返回 `null`，`spawnGuard` 抛 `the local file helper (fs-guard) is not installed in this build`（`remote-container-ipc.ts:626-630`）；异常被 `project-browser/browser.ts:316-320 / 435-438` 归一为 `errorCode: 'device_error'`。

### 5. 该 helper 在 Windows 上无法编译（平台性质，不是环境问题）

`desktop/native/fs-guard/src/` 通篇为 POSIX 专有：

- `ops.rs:17` `use std::os::unix::io::{AsRawFd, FromRawFd};`
- `ops.rs:65-68` `libc::O_NOFOLLOW` / `libc::openat(...)`
- `ops.rs:182-241` `libc::fdopendir` / `readdir` / `fstatat` / `libc::S_IFMT`
- `Cargo.toml` 依赖 `libc`

与既有平台闸门一致：`contracts/desktop/v2.json:19-22` `win32.supported=false`（"the Windows launcher and probes are not accepted yet"）、`remote-container-ipc.ts:651` 的 `seatbeltAvailable` 依赖 macOS 的 `/usr/bin/sandbox-exec`。

## 复现探针（并说明它为什么不是本症状）

在**重启后的新进程**里经 CDP 调 `projectSource` / `projectTree`（workspace `ws_kmE9jJGCeKfTqMtqZJAWsPbD`）：

```json
{"projectSource":{"ok":false,"code":null,"message":"this session has no live local project on this device"},
 "projectTree":{"ok":false,"code":null,"message":"this session has no live local project on this device"}}
```

该消息是 `browser.ts:427-428` 的 `staleContext()`：`projectBindings` 是**进程内内存态**（`local-read-assembly.ts` 模块头注释："a restart starts empty"），重启后为空。它对应 `ws_local_stale`（「本机项目授权已失效」）——**与用户所见文案不同**，故可排除。

## 运行时确认（当前进程，16:04 启动）

同一进程内四项同时成立，因此用户截图所见只能是 `device_error`：

| 观测 | 值 |
|---|---|
| 容器 attach | 日志第 107 行 `[remote] local web auto-bound to http://localhost:9899` |
| 设备连接 | 日志第 108 行 `[desktop] device connection ready` |
| 连接租约 | `lease_k5YeLEALG2Z-CWT7dpsV4nS4` 于 16:15:34 续期、16:16:34 过期；查询时 `LIVE = 1` |
| 本进程内的选择 | 16:07:13 新建 `desktop_workspaces` / `desktop_bindings`（`ws_aOFHryZNidJhHMIqtUQQ5D7H` / `bind_7ClYDO6rOQhQUX46mz1aywke`）——与 `projectBindings.set(...)` 同一条确认 IPC，故该进程内有活绑定 |

由 1. 的映射：该文案只对应 `device_offline` 或 `device_error`；而设备就绪、租约存活、绑定在本进程内有效，`device_offline` 的两个条件（`!connected || !connectionEpoch`）都不成立 ⇒ 实际码为 **`device_error`**，即本机读/写执行失败，而本平台唯一原因是 helper `fs-guard` 不可用（第 4、5 条）。

## 判定边界（如实标注）

- 已确证：文案的唯一来源（第 1 条）；出问题进程内设备在线（租约 + `device connection ready`）；绑定在同进程内建立并有效（第 3 条与「运行时确认」）；helper 候选缺席且源码仅 POSIX（第 4、5 条）。
- 仍属推断的只有一环：`spawnGuard` 抛出的**原始异常文本**未被捕获——本机主进程不打印它。上面的四项合起来只允许「`device_error` ← helper 缺失」这一条解释。
- 升级为实测的路径：在 macOS 上 `cargo build --release`（或用随包分发的二进制）后复跑同一次本机读；若成功，则本条推断闭环。

## 影响

- **Windows**：本机项目读写按设计不可用（缺 Windows 版 helper）。UI 应如实报"该平台不支持本机文件 helper"，而不是「本机未连接」。
- **macOS**：构建 helper（或使用随包分发的已签名二进制）后本机读路径可用；编辑与对话内读取仍受 `desktop_project_execution` 能力闸门（见 `design.md` 与 tasks 阶段 3）。
- **文案缺陷**：`device_error`（本机失败）与 `device_offline`（确实未连接）被折叠成同一句，会把平台/依赖缺失误报成连接问题。
