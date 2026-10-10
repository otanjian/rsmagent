## Why

macOS 桌面已打通 SAP 当前页面读取与登录记忆；同一 Electron 宿主桥在 Windows 上可用，但被平台门禁挡住。需要补齐 Windows，使两端桌面能力一致。

## What Changes

- 将 `sap_page_read` 与 SAP 登录记忆的宿主门禁从仅 `darwin` 扩展为 `darwin` 与 `win32`。
- 登录密文仍只落在本机 `safeStorage`（Windows 为 DPAPI）；对话文案与技能说明改为桌面平台表述，不再写死 macOS。
- 普通 Web、旧桌面与 Linux 宿主继续返回不支持；不扩大填写、点击或提交能力。

## Capabilities

### Modified Capabilities

- `desktop-sap-page-reading`: 启用范围从 macOS 扩展到 Windows 桌面。
- `sap-workbench-scene`: 能力说明与开关文案覆盖 macOS / Windows。

既有绑定模型复用 `sap-workbench-session-binding` 的 heartbeat / pending 读取，不新增绑定语义，故该能力不列入本 change。

## Impact

- 桌面：`sap-page-read.ts`、`remote-host-ipc.ts`、`remote-container-ipc.ts`、`sap-login-memory.ts`
- 场景：`workbench.js`、项目插件、skill / boundaries、`project_toolkit.py` 可升级摘要
- 文档：`docs/desktop-sap-page-reading.md`、`docs/desktop-sap-login-memory.md`
