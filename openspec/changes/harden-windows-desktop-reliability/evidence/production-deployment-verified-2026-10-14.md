# 生产部署核验（2026-10-14，用户完成部署后）

用户于 2026-10-14 完成 `rd.rsmxm.com.cn` 的部署（提交 `f3703e6b` / `2cf61276`）。本文件记录**部署核验**的已得部分与待办部分。

## 0. 域名澄清（重要）

| 名称 | 状态 | 用途 |
|---|---|---|
| **`rd.rsmxm.com.cn`** | **本机可解析**（150.158.141.232 等），`/api/version` → **200** | **业务服务 + Gitea 远端**（用户确认："用 https://rd.rsmxm.com.cn/"） |
| `rd.rsxm.com.cn` | 本机 `DNS name does not exist` | 客户端登录页预填的地址（**不是**要核验的服务；登录时应改为 `rd.rsmxm.com.cn`） |

> 后续所有生产探测一律使用 `rd.rsmxm.com.cn`。

## 1. 无需登录即可确认的部分（已通过 ✓）

| 检查 | 结果 |
|---|---|
| `/api/version` | **200** `{"version":"2.2.0","install_kind":"git","platform":"win32","update_state":"unavailable"}` |
| `/api/desktop/meta` | **200**（`remote_web` implemented+accepted+configured+available） |
| `/auth/check` | **200** `{"auth_required":true,"authenticated":false,"identity_mode":"database"}` |
| `/`、`/memory` | **200**（控制台页面，资源前缀 `/assets/…`） |
| **静态资源指纹比对** | **四个前端文件与提交内容逐字节一致**（见下表）→ **证明部署确实含本 change** |
| `/api/desktop/connect` | **404 `not found`** → 反代仍未补（与"只部署代码、未补设备通道路由"一致） |

| 服务器资源 | HTTP | 与提交内 `channel/web/static/…` 比对 |
|---|---|---|
| `/assets/js/fork/views/memory.js` | 200 | **一致 ✓**（换行归一后逐字节相等） |
| `/assets/js/i18n/memory-account.js` | 200 | **一致 ✓** |
| `/assets/js/workspace.js` | 200 | **一致 ✓** |
| `/assets/js/i18n/core.js` | 200 | **一致 ✓** |

服务器 HTML 中该文件的缓存指纹为 `views/memory.js?v=1a139c629b7`（部署侧构建戳，与本仓库内容哈希不是同一套算法，仅记录）。

## 2. 仍需登录态的部分（待用户登录后执行）

| 待验证 | 期望 | 说明 |
|---|---|---|
| `GET /api/memory?scope=personal` 本人 | **200**（部署前为 **403 `unsafe_path`「个人记忆目录被替换」**） | 3.5 的生产正向证据 |
| `/memory` 页面渲染 | 正常列出条目、无报错 | 用户可见的修复效果 |
| 跨租户/他人作用域 | **403** 保持拒绝 | 反向隔离未被削弱 |
| 客户端登录 | 登录页地址需改为 **`https://rd.rsmxm.com.cn`** | 客户端当前预填的是无法解析的域名 |

执行方式：客户端登录后，用 CDP 在该页做**同源**读取（与我方此前生产核验同一手法，只读、不写）。

## 3. 结论（当前）

- **部署已确认含本 change**（前端资源指纹一致 + 服务可达 + 控制台正常）✓
- **记忆 403 修复的行为验证**待登录态确认（预期 200）；
- 设备通道（6.4 配对）仍缺反代与网关（`/api/desktop/connect` 404）。

---

## 2b. 登录态内实测（已完成 ✅，取代上方 §2 的"待执行"）

在客户端（已登录工作台，容器页 `https://rd.rsmxm.com.cn/`）内经 CDP 做**同源只读 fetch**；会话身份 `/auth/me` → `RC004` / `tanjian`（`usr_hcWvTEbXtYDWq1ho`），租户 `tnt_EA3qM-lHPLD8ZPwW`。

| 请求 | 部署前 | **部署后（实测）** |
|---|---|---|
| 本人 `GET /api/memory?scope=personal&category=memory&page=1&page_size=20` | **403 `unsafe_path`**「个人记忆目录被替换」 | **200** ✓ `total: 1`，条目 `memory/2026-09-26.md` |
| 跨租户（`X-Tenant-ID: tnt_N8gC2oB4jo5flatC`） | 403 `forbidden` | **403 `forbidden`** ✓ 隔离未削弱 |
| 不带租户头（会话默认租户） | — | **200**，同一份本人记忆 ✓ |
| `/memory` 页面渲染 | 显示"个人记忆目录被替换" | **正常**：「记忆管理」列出 `memory/2026-09-26.md`（2.5 KB，2026-09-26 16:56:34），「清空我的记忆」「刷新」按钮在位 —— 截图 `production-memory-page-after-deploy.png` |

**结论：本 change 修复的原始故障在生产上已消失** ✓（反向隔离保持拒绝 ✓）。核对全程**只读**（未写入、未上传、未清空任何记忆），浏览后已把容器页切回首页。

执行脚本：`%TEMP%\cow-pack\prod-memory-inpage.py http://127.0.0.1:9232 <foreign-tenant> <screenshot.png>`
