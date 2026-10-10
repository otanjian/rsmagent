## Context

`add-desktop-sap-page-reading` 已在 macOS 验收页面只读链路；登录记忆同属原生宿主能力。Windows 与 macOS 共用 Electron `WebFrameMain` / `safeStorage`，差异主要是先前显式 `process.platform === 'darwin'` 门禁与用户文案。

## Goals / Non-Goals

- Goals：Windows 桌面在开关开启且宿主方法可用时报告 `sapPageRead` / `sapLoginMemory`；页面读取与记住登录行为与 macOS 对齐。
- Non-Goals：Linux 宿主、截图/OCR、页面填写提交、真实 SAP 现场验收证据（自动化与 macOS 已有联调覆盖主路径；Windows 以同源宿主桥 + 自动化为准）。

## Decisions

1. 统一 `isSapDesktopHost()`（`darwin` | `win32`），页面读取与登录记忆共用，避免两端门禁漂移。
2. 保留错误码 `sap_keychain_unavailable`（兼容前端映射）；用户可见文案改为“本机加密存储”。
3. 配置键 `desktop_sap_page_read_enabled` 不改名；界面标签改为“桌面当前页面读取”。
4. Requirement 与 Scenario 标题**不改名**：`scripts/check_change_deltas.py` 把 `RENAMED` 视为未识别操作块并一律报错（`archive/2026-09-11-fork-decoupling-and-tenant-hardening` 任务 10.5，`tests/test_change_delta_check.py` 反向锁定），且 `MODIFIED` 必须与基线标题逐字匹配、保留基线 scenario 标题。因此本次只改正文，把平台范围写进既有 requirement 正文与 scenario 条件；标题中残留的 `macOS` 字样属已知文档债，待闸门支持改名后另行处理。

## Risks

- Windows DPAPI / 企业策略可能导致 `safeStorage` 不可用 → 已有失败关闭路径，不退回明文。
- 嵌套 frame 行为依赖 Electron 版本 → 复用既有 fixture；不宣称未经实机的 SAP 现场验收。
