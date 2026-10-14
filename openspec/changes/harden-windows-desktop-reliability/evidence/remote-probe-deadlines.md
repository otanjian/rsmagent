# 阶段 6 证据：HTTPS/PKCE/协议/配对/重连的期限与原因（任务 6.1）

日期：2026-10-13。规范依据：`specs/desktop-tenant-context/spec.md` 第 21 行（"Desktop SHALL 对服务器 metadata、必需协议、证书、浏览器授权和 Web 配对使用有期限的探测或操作，并区分网络不可达、TLS 拒绝、协议不兼容、身份失效、服务能力关闭与可选设备通道故障……"）与第 35 行场景（"探测挂起或授权取消"）。

## 1. 复验结论（既有实现，已跑通）

| 规范要素 | 既有实现 | 本次复验 |
|---|---|---|
| 浏览器授权有期限 | `auth-broker.ts`：`AUTHORIZE_TIMEOUT_MS = 5min`、`REQUEST_TIMEOUT_MS = 60s`；取消与超时是**不同码**：`authorization_cancelled` / `authorization_timeout` | `test_desktop_auth_flow.py` **51 passed**；渲染层 `LoginGate.tsx` 对 `authorization_cancelled` **不显示为错误**，对 `authorization_timeout` 用 `login_browser_timeout` 文案 |
| 证书/TLS 拒绝不降级 | `classifyFetchError` 依 TLS 错误码/名称判定 `tls`；`probeServer` 对非 HTTPS 直接 `downgrade`，`redirect: 'error'` | 同上 + `test_desktop_context_frontend.cjs` **14/0**（文案含"不会降级为 HTTP"） |
| 协议不兼容 | metadata 校验失败 → `kind: 'protocol'`；`contract.ts` 亦有 `protocol_incompatible` | `test_ws_client_compat.py` **5 passed**；`test_desktop_execution_v2_contract.py` 63 passed（早前轮次） |
| Web 配对与身份 | `binding-setup.ts` 序列（devices→workspaces→bindings→attach）走**注入的传输**，期限由传输实现承担：`materialize-transport` 120s、`http-relay` 10s、broker 请求 60s | `test_desktop_web_session.py` **30 passed**、`test_desktop_remote_host.cjs` **34/0** |
| 重连与设备通道 | `device-client.ts`（`IDLE_TIMEOUT_SECONDS=90`、重连定时器）、`ws-client.ts`（`WebSocketError`） | `test_desktop_device_client.cjs` **22/0**、`test_desktop_device_connection.cjs` **7/0** |
| 原因分类齐全 | `FailureKind` = tls / network / http / protocol / identity / downgrade；渲染层每种 kind 有独立文案 | `test_desktop_remote_config.cjs` **43/1**（唯一失败见第 4 节，与本次无关） |

## 2. 补的唯一缺口：metadata 探测**没有期限**，也没有"超时"这个原因

`probeServer()` 的 `fetch(...)` 既没有 signal 也没有任何 deadline：服务器接受连接后不回应时，探测会一直挂着——正是规范第 35 行场景要禁止的。而即便有界，原来的分类也只会给 `network_unreachable`（"无法连接到服务器"），把"服务器不回应"说成"网络不通"，会把人引向错误的方向。

改动（最小）：

| 位置 | 改动 |
|---|---|
| `desktop/src/main/remote/connection.ts` | 新增 `PROBE_TIMEOUT_MS = 10_000` 与 `probeSignal(timeoutMs)`；metadata fetch 带上 `signal: AbortSignal.timeout(...)`（`timeoutMs` 可由调用方/测试调低）；`classifyFetchError` 先判超时：`TimeoutError` / `AbortError` / `ABORT_ERR` / `ETIMEDOUT` → `{kind:'network', code:'network_timeout'}` |
| `desktop/src/renderer/src/pages/RemoteConnectPage.tsx` | 先按 **code** 判 `network_timeout` → `remote_err_timeout`，再走原有 kind 分支（一个 kind 覆盖两种情形，必须按码区分） |
| `desktop/src/renderer/src/i18n.ts` | 新增 `remote_err_timeout`（简中 + 英文）：明说"服务器未在规定时间内回应，这不一定是网络问题" |

未重写探测框架、未新建仪表盘、未改协议或配对流程；`AbortSignal.timeout` 缺席的运行时下 `probeSignal` 返回 `undefined`（仍可工作，只是无界——因此它可被 `timeoutMs` 覆盖）。

## 3. 验证

新增 `tests/test_desktop_probe_deadline.cjs`（**7/7 通过**，测编译产物）：

1. **不回答的服务器有界返回且报超时**：注入一个"只等 abort"的 fetch，`timeoutMs: 50` → 断言返回耗时 < 1.5s、`code=network_timeout`、`kind=network`，且失败对象里**不含**origin（不泄露地址）；
2. 正常探测仍成功，并且 `probeServer` **确实**把 `AbortSignal` 传给了 fetch，默认预算 `PROBE_TIMEOUT_MS === 10000`；
3. `TimeoutError`/`AbortError`/`ABORT_ERR`/`ETIMEDOUT` 四种形态都判为超时；
4. 原有分类不变：TLS 码 → `tls`，`ECONNREFUSED` → `network_unreachable`；
5. 非 HTTPS origin 仍直接 `downgrade` 且**完全不发请求**；
6. 元数据缺字段仍是 `protocol`；
7. 文案与映射：`remote_err_timeout` 在两种语言里都有、英文写明"did not answer in time"，页面里出现 `network_timeout` 映射。

复验套件合计（本机）：`test_desktop_auth_flow.py` 51、`test_desktop_web_session.py` 30、`test_ws_client_compat.py` 5、`test_desktop_context_frontend.cjs` 14、`test_desktop_device_client.cjs` 22、`test_desktop_device_connection.cjs` 7、`test_desktop_remote_host.cjs` 34、`test_desktop_remote_config.cjs` 43（1 项环境失败，见下）；`tsc -p tsconfig.main.json` 与 `tsc -p tsconfig.json`（渲染层）均 **exit 0**。

## 4. 既有环境问题（非本次引入）

- `test_desktop_remote_config.cjs` 唯一失败：`config must not be world-readable: actual 438 (0o666) expected 384 (0o600)` —— Windows 上 POSIX 权限位不生效，属环境差异。
- 该类测试的 `before()` 用 `execFileSync('npx', ['tsc', …])` 在"产物比源码旧"时编译；本会话 `npx` 只有 PowerShell shim（`npx.ps1`），`spawnSync npx` 直接 `ENOENT`，会让整个文件 44 项全红。**本次新增的测试改用本地 `typescript/bin/tsc`**（并先编译一次产物）；其余既有测试文件的脚手架未改，登记为独立范围。

## 5. 判定与未验证

- **6.1 勾选**：既有 HTTPS/PKCE/协议/配对/重连已复验（上表 + 套件结果），缺失的**探测期限**与**超时原因**已补齐并有测试守住，未重写框架、未新建仪表盘。
- **未验证**：真实 HTTPS 服务器下的证书拒绝/旧协议/真实浏览器配对与真实重连（需要服务器与浏览器）→ 归 6.4/6.5 真机验收；本机验证到分类、期限与文案三层。
