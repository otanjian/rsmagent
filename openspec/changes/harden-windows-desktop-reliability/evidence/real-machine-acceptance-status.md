# 阶段 6 状态：任务 6.4 / 6.5 的真机依赖与本机可复现部分

日期：2026-10-13。**结论：6.4 与 6.5 均不勾选**——两者都要求"真实 Windows 安装包 + HTTPS 服务器 + 真实设备/网络"的统一验收，本机没有这些条件。本文记录本机已复现的部分，以及必须留到真机的部分，避免以后把"跑过一半"当成"验收通过"。

## 1. 为什么 6.4 不能在本机执行

6.4 要求：真实 Windows x64 安装包经 HTTPS 服务器完成探测、浏览器确认、配对、聊天、流式回复与合成附件上传回读，并记录版本、代理与 PID，证明无本机 Python。

本机现状：

- **没有安装包**：`desktop/package.json` 有 `dist:win`（`electron-builder --win`），但仓库内不存在构建产物（全盘只找到 pip 自带的 `t64.exe` 之类无关文件）。
- **没有 HTTPS 服务器/证书链**，也没有可配对的真实设备与会话。
- 因此"探测 → 浏览器确认 → 配对 → 聊天 → 流式 → 附件回读"整条链路无法启动，更无法记录版本/代理/PID。

真机执行清单（供有环境时照做，勿以本机结果替代）：`npm run dist:win` → 在 Windows x64 安装 → 配置 HTTPS origin → 完成原生授权与 Web 配对 → 依次验证聊天、流式回复、合成附件上传回读 → 记录安装包版本、代理配置版本与两端 PID、确认无本机 `python` 进程启动。

## 2. 6.5 的本机可复现矩阵（已跑）

6.5 要求统一验证：断网、网关/Web 重启、撤销、两租户两成员、模式/来源切换、证书拒绝/适用 CA 和 VPN；分别检查探测/页面/WSS；不降级 TLS、不复用旧身份、数据持久化且不上传绝对授权根。

| 子项 | 本机证据 | 结果 |
|---|---|---|
| 断网/重连（有界退避、身份重验、不重发未确认写） | `test_desktop_device_client.cjs`、`test_ws_client_compat.py`、本轮新增 `test_desktop_remote_gateway_isolation.cjs`（停止后不再尝试） | 22 / 5 / 4 全通过 |
| 撤销与登出 | `test_desktop_signout.cjs`、`test_desktop_web_session.py` | 9 / 30 全通过 |
| 两租户两成员与作用域隔离 | `test_tenancy_isolation_acceptance.py`、`test_desktop_file_access.py`、`test_desktop_local_context.py` | 6 / 18 / 31 全通过 |
| 模式/来源切换 | `test_desktop_remote_config.cjs` | 43 通过 / 1 失败（见 §3，环境） |
| 不降级 TLS、不引导旧身份 | `test_desktop_probe_deadline.cjs`、`test_desktop_context_frontend.cjs`、`test_desktop_auth_flow.py` | 7 / 14 / 51 全通过 |
| 数据持久化（重启后仍在） | `test_desktop_execution_journal.cjs` | 19 通过 / 1 失败（见 §3，环境） |
| 不上传绝对授权根 | 路由登记（`route_registry.py`）：`/api/desktop/local-roots` 的说明为 **"register the absolute root the native picker returned with this same-machine backend; loopback + per-launch desktop token + native bearer + owned device/workspace/binding; never persisted and never echoed back"**；`test_desktop_local_executor_endpoint.cjs` 16 通过；`test_desktop_local_e2e.py` 断言 `@` 里的绝对路径"不解析、不上传" | 通过 |
| **真实断网 / 网关与 Web 重启 / 证书拒绝 / 企业 CA / VPN / 真实安装包** | — | **本机无法执行**：没有部署、没有证书链、nginx 未运行（代理仅在 6.3 做了配置契约与独立进程的真实端口验证） |

## 3. 环境性失败（既有，非本次引入）

两个套件各 1 项失败，原因相同：**Windows 不落实 POSIX 权限位**，断言 `0o600` 实际得到 `0o666`：

- `test_desktop_remote_config.cjs`：`config must not be world-readable: actual 438 expected 384`；
- `test_desktop_execution_journal.cjs`：`receipts carry run and tool-call ids; keep them owner-only`（同 438 vs 384）。

这两项在 POSIX 上才有意义；在 Windows 上应用实际依赖 ACL 与用户目录继承。本 change 未修改这两个测试（登记为独立范围）。

## 4. 判定

- **6.4 不勾选**：缺安装包、HTTPS 服务器与真实设备。
- **6.5 不勾选**：本机部分已跑并有证据，但"统一验证"还包含只能在真机执行的项目（真实断网、网关/Web 重启、证书拒绝、企业 CA、VPN），按任务要求"未执行如实记录"，不以部分结果冒充通过。
