# 阶段 6 证据：Web/网关独立启动、代理与部署说明（任务 6.3）

日期：2026-10-13。规范依据：`specs/production-deployment/spec.md` 第 5–9（同源入口）、15–25（网关显式代理与身份一致）、27–37 行（运维记录与升级回退）。

## 1. 发现的真实缺口

`docker/nginx.conf.example` 把**所有**请求转发到 Web 应用（`location / → 127.0.0.1:9899`），而 `/api/desktop/connect` **只能由独立网关提供**：Web 应用是 WSGI，无法完成 WebSocket Upgrade（`channel/web/fork/runtime.py` 明确写着 "WSGI cannot upgrade /api/desktop/connect"）。更糟的是 `location /` 里有 `proxy_set_header Connection "";`——即便转对了端口，Upgrade 头也会被抹掉。后果正是规范要避免的那一对状态：**设备通道全部握手失败，而服务器业务看起来正常**。

## 2. 改动

| 位置 | 改动 |
|---|---|
| `docker/nginx.conf.example` | 新增 `location = /api/desktop/connect` → `127.0.0.1:9877`，带 `Upgrade`/`Connection "upgrade"`、`proxy_read_timeout 3600s`、`proxy_send_timeout 3600s`、`proxy_buffering off`；`location /` 保持转 Web 且不升级；`/healthz` **不在代理里发布**（回环诊断），并在注释里写明"Web 就绪≠网关就绪" |
| `docs/desktop-remote-deployment.md`（新增） | 两个进程与各自端口/职责；代理片段与要点；依赖与回环/网络/证书；**同一权威身份库与命令存储**（`identity_db_path`，不一致时修配置而不是让用户重新登录）；持久化归属（账号/命令在服务器，会话 cookie 在客户端内存，本机授权根不上传）；启动、诊断命令与恢复步骤；升级/回退（记录应用/客户端/代理配置三样版本、备份数据目录、先协议兼容后设备通道、不混用未验收产物） |

未改动网关的鉴权、帧协议或 Web 应用的路由；只补了"部署侧把它接对"。

## 3. 验证

**真实端口（本机实跑）**：用隔离的身份库把网关作为独立进程起在 **9878**（不触碰用户数据目录）：

| 请求 | 结果 |
|---|---|
| `GET /healthz` | **200** `{"status":"ok","gateway_id":"gw_b3NsmPQV5_U"}`（独立就绪诊断） |
| `GET /api/desktop/connect`（无凭据） | **401** |
| `GET /api/desktop/connect?token=abc` | **400**（令牌不得出现在 URL） |
| `GET /api/desktop/connect`（仅 Cookie） | **401**（不接受纯 Cookie） |
| `GET /api/desktop/connect` + `Upgrade/Connection` 头（无 bearer） | **401**（有界返回，不挂起） |

同一次运行里：监听 9878 的是**独立 python 进程**（PID 30308），而 **9899 上没有任何监听** —— 即网关确实**不需要 Web 应用**也能起、能自诊；验证后已停止进程、释放端口、清理临时目录（校验过路径）。

**部署契约测试**：`tests/test_desktop_gateway_deployment.cjs`（新增，**6/6 通过**）：代理把设备通道转给 9877 且 Must-have 的 Upgrade/超时/不缓冲都在；其余流量仍转 9899 且保持 `Connection ""`；`gzip_types` 不含 `application/json`/`text/event-stream`；`/healthz` 未被发布（但文档给出回环检查命令）；网关 CLI 默认回环、非回环需显式开关，且确实注册 `/api/desktop/connect` 与 `/healthz`；两侧同用 `identity_db_path`/`identity.db`；运维说明覆盖客户端版本、启动方式、实际代理端口、证书链、模型/工具依赖、持久化、升级与回退，并含"Web 就绪不等于网关就绪""不上传授权根""不关闭证书校验"。

**既有边界套件**（保留来源、meta no-store、SSE、gzip/cache、上传）：`test_deployment_response_compression.py` **3 passed**、`test_desktop_meta.py` **11 passed**、`test_desktop_gateway.py` **15 passed**（后者含带凭据的握手与命令流，in-process）。

## 4. 判定与未验证

- **6.3 勾选**：Web/网关独立启动与就绪诊断（真实端口验证 + 文档）、共享身份/命令存储（代码 + 文档 + 契约测试）、connect 代理（示例配置 + 契约测试）、部署说明（依赖/回环/网络/持久化/升级回退齐备）。
- **未验证**：**带真实设备凭据**的 WSS 握手、心跳与"网关重启后重连"发生在真实部署（需要已配对设备与真实域名/证书）；本机验证到独立进程/真实端口/未授权路径与 in-process 的鉴权握手（`test_desktop_gateway.py`）。nginx 未在本机运行，代理片段以"配置契约 + 语义要点"验证，真实转发留待 6.4/6.5。
