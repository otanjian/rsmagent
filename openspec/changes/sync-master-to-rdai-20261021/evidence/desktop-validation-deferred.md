# 桌面验证延期清单

## 2026-10-21 用户停止桌面测试

用户最新指令为「停止桌面端的测试」。12.1–12.4 保持未勾选，后续不再运行桌面测试、构建或安装包验收。已完成的部分结果保留在 desktop-followup-validation.md；它们不能代表桌面整体验收。Web 和 CI 收尾继续。

延期依据：2026-10-21 用户明确要求先保证 Web，可暂不测试桌面端。以下任务仍未验证，不阻塞本轮 Web 验收、同步 PR 或合入；不是通过或永久取消。

| 后补任务 | 本轮延期范围 | 原计划涉及位置 | 当前状态 |
| --- | --- | --- | --- |
| 12.1 | Desktop 依赖、类型检查、Electron/renderer 构建、原生 helper 和包资源核验 | 原 8.5；混合 production gate 的 Desktop build | deferred / not verified |
| 12.2 | 原生容器、main/preload/IPC、登录/配对/恢复、D1–D6、真实模型与设备文件、原生路径授权 | 原 3.4、8.2、8.4、10.4；native container 用例 | deferred / not verified |
| 12.3 | Desktop 页面/命名/滚动、产物/菜单/技能/API 配置及真实 Web/Desktop 双端同步 | 原 3.4、4.3–4.4、5.5、6.2、8.3 | deferred / not verified |
| 12.4 | Windows/macOS OS 隔离、v2/v3/旧端兼容、真实安装包安装/升级/回退/恢复 | 原 8.4–8.5、10.4–10.5 | deferred / not verified |

## 本轮仍需完成

- 对 desktop 变更逐路径审查、解决冲突并保留原有身份/合同/开关和双方有效代码；代码已整合不等于运行已验证。
- 验证 Web 可达的后端授权、文件来源、会话及传输拒绝边界，不按路径或测试文件名粗略排除 shared backend。
- 在 PR/能力清单中将 Web 状态和 Desktop 状态分开。Web 通过不自动设置桌面 accepted，不发布未验证的桌面包，也不删除原桌面 change 的未完成项。

## 后补条件

后续安排桌面测试时，重新记录实际候选树/提交、服务端版本、平台与运行时、依赖/构建/安装包摘要和测试配置。按 tasks 12.1–12.4 执行，失败保留并修复；后补期间代码变化需要重跑受影响 Web 回归。

原 change `harden-windows-desktop-reliability` 与 `add-windows-local-project-access` 的未验收能力和开关继续按其真实状态维护。本 change 不把任何已有未验证项改为通过；桌面任务尚未完成时不能宣称本 change 全端验收完成。
