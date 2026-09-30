# 阶段二第 9 组运维说明（task 9.7）

本文件记录网关的受监督部署约定。口径：只写已落地的默认行为与配置键，未在本机
跑过的生产反向代理演练不记为通过。

## 1. 进程入口

```bash
python -m integrations.desktop.gateway --bind 127.0.0.1 --port 9877
```

* 默认只绑定环回。非环回地址需要显式设置环境变量
  `DESKTOP_GATEWAY_ALLOW_NONLOCAL=1`，否则进程以退出码 2 拒绝启动。
* 健康检查：`GET /healthz` → `{"status":"ok","gateway_id":...}`。
* WSS 入口：`GET /api/desktop/connect`（Upgrade）。

## 2. 与 Web 的分工

| 路径 | 处理方 |
| --- | --- |
| `/api/desktop/connect` | 本网关（Upgrade） |
| 其余 `/api/desktop/*` | 既有 Web（web.py） |

反向代理必须：

* 仅将 `/api/desktop/connect` 的 Upgrade 请求转给网关；
* 保留 `Host` 与可信代理来源头，**禁止**客户端通过 `Forwarded` 伪造精确 origin；
* 空闲超时 ≥ 90 秒；数据块请求体上限 ≥ 4 MiB + 协议头。

示例（nginx，示意）：

```nginx
location = /api/desktop/connect {
    proxy_pass http://127.0.0.1:9877;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 90s;
}
location /api/desktop/ {
    proxy_pass http://127.0.0.1:9899;  # 既有 Web
}
```

## 3. 单主机多进程

V1 为同一主机上的多进程部署，共享本机 `identity.db`。连接租约通过
`desktop_connection_leases` 的部分唯一索引 + epoch fencing 协调；网关崩溃只
能重投同一命令 ID，不会新建业务 ExecutionRun。

## 4. 本轮验证

* `tests/test_desktop_gateway.py`：租约 CAS、outbox fencing、WSS hello/heartbeat、
  拒 Cookie-only、拒 query token —— **14 passed**（含真实 aiohttp WSS smoke）。
* 生产反向代理与监督进程（systemd/launchd）**未在本机演练**，不记为通过。
