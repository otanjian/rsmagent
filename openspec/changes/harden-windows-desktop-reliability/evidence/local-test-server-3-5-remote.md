# 本地合成服务器：3.5 远程半段 + 设备通道路由验证（2026-10-14）

按用户决定"**能做的先做**"，在本机起了一个**隔离的合成服务器**（任务 10.1 允许："隔离合成环境用明确测试配置验证"），在**真实 HTTPS 传输**上跑完 3.5 的远程半段。**不触碰生产**，也不改动仓库的 `config.json`。

## 1. 环境（可复现）

| 组件 | 值 |
|---|---|
| 数据根 | `%TEMP%\cow-local-server\.cow`（全新：自动 bootstrap 平台管理员 + 口令文件） |
| Web 后端 | 仓库 `app.py`，`127.0.0.1:9899`（`COW_DATA_DIR` 指向上面） |
| 桌面网关 | `python -m integrations.desktop.gateway --bind 127.0.0.1 --port 9877` |
| TLS 分流终止器 | `%TEMP%\cow-pack\tls-route.py 8443 9899 9877 …`：`/api/desktop/connect` → 网关，其余 → Web（等价于部署里的反代） |
| 证书 | 自签（`%TEMP%\cow-tls\server.pem`）/客户端信任 `ca.pem` |
| **测试配置**（写在数据根 `config.json`，从仓库 `config-template.json` 生成，**不含你的密钥**） | `web_host=127.0.0.1`、`web_port=9899`、**`trusted_proxies=["127.0.0.1"]`**、`tenant_shared_base=<测试基座>`、`desktop_project_execution_enabled=true`、`desktop_project_scripts_enabled=true`（后两项为 10.1 的"明确测试配置"：部署开关打开） |
| 合成租户 | `zzlocal`（`tnt_CtBD8jYuVeNb_7LT`），两个成员账号（口令由服务层设为已知值，幂等） |

## 2. 两条"部署才会踩到"的经验（本次实测）

1. **反代必须让后端知道公网 scheme**：只转发请求而不带 `X-Forwarded-Proto`/`X-Forwarded-Host` 时，后端把请求看成 `http://<host>`，于是 cookie 写的同源门直接 **403**；而且**光带头还不够**——`auth_handlers.py:1033` 要求来源地址在 `conf()["trusted_proxies"]` 里，否则忽略该头（本次先 403、补 `trusted_proxies` 后立即 200 ✓）。`docker/nginx.conf.example` 已设置这些头，`docs/operations/production-readiness.md` 也提到 `trusted_proxies` ✓。
2. **租户根位置**：租户共享根必须落在**home 之外**，或落在已登记的 **`tenant_shared_base`** 下；否则读取路径的 home/global 守卫直接拒（表现为 `503 memory_unavailable`，真正原因只在 `run.log` 的 `Memory API error:` 行里 —— 又一次证明"错误面要能说出真实原因"的价值）。

## 3. 3.5 远程半段结果（全部通过 ✓）

| 步骤 | 结果 |
|---|---|
| 两个成员账号 HTTPS 登录（走 TLS 终止器 + 反代头 + 可信代理） | 均 **200**，`must_change_password=false` |
| 账号 A：列表 | **200**，`total=1`，`index_state=ok`，`collection_revision` 为内容哈希 |
| 账号 A：读回正文 | **200**，含 marker `COW-LOCAL-REMOTE-OK` ✓ |
| 账号 A：过期 `revision` 保存 | **409 `stale_revision`** ✓（版本条件生效） |
| **账号 B（同租户另一账号）** | **200 `total=0`**，A `['MEMORY.md']` / B `[]` → **互不可见** ✓ |
| 账号 A：清空（`clear_scope=all_personal` + 集合版本） | **200** `{"removed": 1, "generation": 1, "index_state": "ok"}` ✓（**generation 递增**） |
| 清空后列表 | **200 `total=0`** ✓ |
| **清空后用旧 revision 再发布** | **409 `stale_revision`** ✓ → **未复活已清空正文/索引** |
| 跨租户选择 | **403 `forbidden`** ✓ |
| **设备通道路由** | `GET /api/desktop/connect` → **401 `auth_required`** ✓（**路由已发布**；对比：生产上该路径 **404**） |
| 能力块 | `GET /api/desktop/meta` → **200** |

## 4. 与生产阻塞的关系

- 生产服务器**未含本 change 的 3.x 修复**，故其"本人记忆"仍 403 `unsafe_path`（见 `production-reproduces-memory-403.md`）；本地合成服务器跑的是**修复后代码**，同一套动作全部通过 ✓ → **进一步确证修复有效**，也说明生产侧只差部署。
- 生产上的正向对比（用户账号 + 真实域名）仍在"部署后复跑"清单里；本次结论**不冒充生产结果**。

## 5. 复现命令

```powershell
# 1) 数据根 + 测试配置（模板生成，含 trusted_proxies/tenant_shared_base/两个执行开关）
python %TEMP%\cow-pack\local-server-config.py ; python %TEMP%\cow-pack\local-server-config2.py
# 2) 后端（数据根指向测试目录）+ 网关 + TLS 分流终止器
$env:COW_DATA_DIR="$env:TEMP\cow-local-server\.cow" ; python app.py
python -m integrations.desktop.gateway --bind 127.0.0.1 --port 9877
python %TEMP%\cow-pack\tls-route.py 8443 9899 9877 %TEMP%\cow-tls\server.pem %TEMP%\cow-tls\server.key %TEMP%\cow-local-server\tls-route.log
# 3) 合成租户/账号 + 3.5 远程半段
python %TEMP%\cow-pack\local-server-accounts.py
python %TEMP%\cow-pack\local-server-3-5.py
```
