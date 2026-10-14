# 远程桌面部署：先把 Web 与设备网关分开

对应 change `harden-windows-desktop-reliability` 任务 6.3；契约依据：`production-deployment`（"远程桌面部署提供兼容的同源安全入口"、"远程设备网关显式代理并保持身份一致"、"远程桌面运维记录可重现的连接与恢复条件"）。

## 1. 两个进程，两件事

| 进程 | 命令 | 默认监听 | 负责 |
|---|---|---|---|
| Web（业务与页面） | `python app.py`（或容器/服务方式） | `0.0.0.0:9899`（`config.json` 的 `web_port`） | 页面、`/api/*` 业务、`/api/desktop/meta`、SSE 流 |
| 设备网关（可选通道） | `python -m integrations.desktop.gateway --port 9877` | `127.0.0.1:9877` | `/api/desktop/connect`（WebSocket Upgrade）、`/healthz` |

**Web 就绪不等于网关就绪。** 两者各自起停、各自诊断；网关只是在服务器侧给"已配对设备"提供通道，聊天与页面**不依赖**它（规范场景"设备网关不可达但服务器聊天正常"）。

网关默认只绑回环；要绑非回环必须显式 `DESKTOP_GATEWAY_ALLOW_NONLOCAL=1`（`gateway.py` 会拒绝并给出退出码 2）。公网入口只有反向代理。

## 2. 反向代理（nginx）

`docker/nginx.conf.example` 已包含两段：`location /` → Web，以及 **`location = /api/desktop/connect` → 网关**。第二段是必需的，因为它做了 `location /` 不能做的事：

```nginx
location = /api/desktop/connect {
    proxy_pass http://127.0.0.1:9877;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";     # 必须覆盖 location / 里的 Connection ""
    proxy_buffering off;
    proxy_read_timeout 3600s;                  # 心跳与空闲租约
    proxy_send_timeout 3600s;
}
```

要点：

- `/api/desktop/connect` **只能**由网关提供：Web 应用是 WSGI，无法完成 Upgrade（`channel/web/fork/runtime.py` 里对此有明确说明）。把它转给 9899、或被 `Connection ""` 抹掉 Upgrade，会让每次设备连接都握不上手。
- `/healthz` **不在代理里发布**：它是回环诊断（`curl http://127.0.0.1:9877/healthz`），公开它等于把内部端口暴露在正式域名下。验收时分**三项**检查：`/api/desktop/meta`（探测）、页面/聊天（业务）、WSS `/api/desktop/connect`（设备通道）。
- 代理保留既有边界：`/api/desktop/meta` 直接返回 JSON 且 `no-store`；SSE 走 `proxy_buffering off`；`gzip_types` 不含 `application/json` 与 `text/event-stream`；上传大小由 `client_max_body_size` 与后端两侧共同约束。

## 3. 依赖与网络

- **依赖**：Python 运行环境与 `requirements.txt`（含 `aiohttp`，网关用）；页面所需的模型/工具凭据在服务器侧配置，客户端不落盘（桌面端只保存 origin 与原生会话）。
- **回环**：Web 与网关都按回环部署，公网只经代理；网关内部端口不要求客户端入站可达（规范："不要求客户端公网入站"）。
- **网络/证书**：正式域名需完整证书链；企业 CA 或经 VPN 的内网域名，要**分别**验证客户端探测、容器页面与适用 WSS 的信任与可达性，不以"浏览器能打开"当作全部通过，也**不关闭证书校验**。

## 4. 持久化：同一套身份与命令存储

Web 与网关必须使用**同一权威身份库与命令存储配置**（`config.json` 的 `identity_db_path`，默认 `<data_root>/identity.db`）。若网关指向了另一个库，症状是"身份明明有效却被拒"，而正确做法是修配置，**不是让用户重新登录**。数据归属：

| 数据 | 归属 |
|---|---|
| 账号、租户、设备与绑定、命令与执行记录 | 服务器（身份库/命令存储） |
| 会话 cookie、容器分区 | 桌面客户端内存（重启即空，必须重新授权） |
| 本机授权根（目录授权） | 客户端本机；**绝不上传服务器**（只上报授权根的标识，不传绝对路径） |

## 5. 启动、诊断与恢复

```powershell
# Web
python app.py                     # 或按部署方式交给服务管理器
# 网关（独立进程，可与 Web 分开重启）
python -m integrations.desktop.gateway --port 9877
# 就绪诊断
curl -sS http://127.0.0.1:9877/healthz          # {"status":"ok","gateway_id":"gw_..."}
curl -sS -o /dev/null -w '%{http_code}\n' https://agent.example.com/api/desktop/meta
```

- **重建 Web 或网关后重连客户端**：配置、账号、已提交业务数据与上传文件都应保留；客户端按当前会话重新验证身份，**不得用旧缓存伪造就绪**。
- **网关重启**：当前身份被重新校验；连接可恢复；旧连接不会扩大目录或工具授权；Web 的服务状态不被假定等于网关就绪。
- **误配排查顺序**：先看 WSS 是否转到了正确端口（`Upgrade`/`Connection`），再看两侧的 `identity_db_path` 是否一致；两者之外不要用"重新登录"掩盖代理问题。

## 6. 升级与回退

1. 记录三样版本：**应用**（`git rev-parse HEAD` 或发布包版本）、**客户端**（安装包版本）、**代理配置**（nginx 配置文件的校验和）。
2. 升级前备份 `<data_root>`（身份库、命令存储、上传目录）。
3. 升级后先做协议兼容检查（`/api/desktop/meta` 的 `protocols` 与客户端 `CLIENT_PROTOCOLS`），再做设备通道检查（`/healthz` + 一次真实设备连接）。
4. 回退时用同一份代理配置与同一份数据目录；**不混用未验收的产物**（客户端旧版 + 新网关等组合需重新验证）。
