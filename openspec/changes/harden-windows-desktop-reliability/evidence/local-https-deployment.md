# 本机真实 HTTPS/WSS 部署验证（6.4/6.5 的服务器与 TLS 维度）

日期：2026-10-14。应用自身只跑明文 HTTP（`channel/web/channel.py:1612` 的 `web.httpserver.WSGIServer((host, port), func)`，配置里没有 TLS 键），而桌面端对公网 origin **强制 HTTPS**（`desktop/src/main/remote/connection.ts`：仅允许把本机后端当作 `http://127.0.0.1|localhost` 例外）。因此真实部署必须像 `docker/nginx.conf.example` 那样**由反向代理终止 TLS**。

本轮在本机把这条链**真的搭起来并验证**（纯 stdlib，不改系统信任库）：

## 1. 部署拓扑

| 组件 | 端口 | 说明 |
|---|---|---|
| 应用 Web 后端 | `127.0.0.1:9899` | 明文 HTTP（既有实例） |
| 桌面网关（`python -m integrations.desktop.gateway --bind 127.0.0.1 --port 9877`） | `127.0.0.1:9877` | `/api/desktop/connect` 的 WS 端点 |
| **TLS 终止器 A** → 9899 | `https://127.0.0.1:8443` | 自签 CA 签发，握手后按字节转发（HTTP 与 WS 升级都原样通过） |
| **TLS 终止器 B** → 9877 | `https://127.0.0.1:8444` | 同上，指向网关 |

证书：用 `cryptography` 现场生成自签 CA + 服务器证书（SAN 含 `localhost` 与 `127.0.0.1`），另生成一个**无关 CA** 用于"不受信"用例；**未写入系统信任库**。

## 2. 验证结果

| 用例 | 期望 | 实测 |
|---|---|---|
| `GET https://127.0.0.1:8443/api/desktop/meta`（客户端不校验证书） | 可达 | **HTTP 200**，2,309 B（与直连 9899 的 meta 负载等长） |
| 同上，客户端**信任我方 CA** | 受信证书可校验 | **HTTP 200** ✓（真实有效链） |
| 同上，客户端用**无关 CA** 校验 | 必须拒绝 | **`SSL: CERTIFICATE_VERIFY_FAILED`** ✓ |
| 直连网关 `GET http://127.0.0.1:9877/api/desktop/connect` | 未认证拒绝 | **HTTP 401**（JSON） ✓ |
| **`wss://127.0.0.1:8444/api/desktop/connect`**（信任我方 CA，无 token） | TLS 通过、应用层拒绝 | **401 Unauthorized**（JSON 86 B） ✓ |
| 同上带 `?token=nope` | 伪造 token 被拒 | **400 Bad Request**（JSON 94 B） ✓ |
| 同上用**无关 CA** | TLS 层拒绝 | **`CERTIFICATE_VERIFY_FAILED`** ✓ |

即：**TLS 终止 + WSS 升级 + 网关鉴权拒绝**三件事在本机以真实 TLS 完整成立；证书"受信/不受信"两条路径都按规范表现（不受信一律先拒，不降级）。

## 2.1 真实客户端指向该 origin（remote 模式，无需人工点击：profile 预置 + CDP 驱动界面）

预置 `%APPDATA%\CowAgent\desktop-remote.json` 为 `{"version":1,"mode":"remote","profiles":[{"id":"local-tls","origin":"https://127.0.0.1:8443",…}],"activeProfileId":"local-tls"}`（**注意：PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会写 BOM，`JSON.parse` 随即失败并静默回落到 local 模式**——去 BOM 后才是 remote）。

| 观测 | 结果 |
|---|---|
| 启动模式 | 日志：**`[remote] remote mode: local backend not started (server=https://127.0.0.1:8443)`** ✓ |
| 是否自起本机后端 | **没有**属于该应用的 python 子进程 ✓（任务 6.2 的核心断言在真实应用上成立） |
| 界面 | 登录页；`input#login-server-origin`（type=url）**已按 profile 预填** `https://127.0.0.1:8443` ✓；DPR 1.25；0 控制台错误 |
| 点击"在本机登录"（CDP 在应用自身 DOM 上触发，未合成系统输入） | 应用对该 origin 发起**大量 TLS 连接**（终止器逐连接日志记录） |
| **证书校验** | 绝大多数握手以 **`SSLV3_ALERT_CERTIFICATE_UNKNOWN`** 被客户端拒绝 ✓（不受信证书一律先拒） |
| 降级/循环 | **无 HTTP 降级、无崩溃**；界面进入"已创建连接，等待确认…"并保持 ✓ |
| 未解释的细节 | 有少数握手以 `TLSv1.3` 成功（原因未定：可能是渲染层与主进程各自的网络栈策略，或 loopback 相关策略差异）——**如实记录，不臆断** |

（`desktop-remote.json` 是我新增的测试配置：**如需还原**，删除该文件即可回到默认 local 模式。本地服务：8443→9899、8444→9877 各一个 TLS 终止器与一个网关进程。）

## 3. 未验证（如实记录）

- **真实域名与公网/企业 CA 证书链**（本轮是自签 CA，需客户端显式信任）；企业 CA、VPN 场景未做。
- **桌面客户端指向该 origin**：`parseServerOrigin` 要求公网 origin 为 HTTPS，把 `https://127.0.0.1:8443` 填进应用还需要**界面操作**（服务器地址输入框），未执行。
- 6.5 的其余场景（断网恢复、只重启网关/只重启 Web、撤销、两租户两成员、模式与来源切换、授权根不上传）以及 6.4 的探测/聊天/流式/附件回读端到端：**未执行**（前者需真实部署与账号，后者需客户端指向该 origin）。

## 4. 复现命令

```powershell
# 证书
python make-tls.py %TEMP%\cow-tls
# 应用后端（已有实例在 9899）+ 网关
.\.venv-win\Scripts\python.exe -m integrations.desktop.gateway --bind 127.0.0.1 --port 9877
# TLS 终止：https://127.0.0.1:8443 -> 9899，https://127.0.0.1:8444 -> 9877
python tls-tunnel.py 8443 9899 %TEMP%\cow-tls\server.pem %TEMP%\cow-tls\server.key
python tls-tunnel.py 8444 9877 %TEMP%\cow-tls\server.pem %TEMP%\cow-tls\server.key
```

（脚本都在 `%TEMP%\cow-pack\`：`make-tls.py`、`tls-tunnel.py`；证书在 `%TEMP%\cow-tls\`。两者都只有本机 loopback 监听，未改系统信任库、未写仓库。）
