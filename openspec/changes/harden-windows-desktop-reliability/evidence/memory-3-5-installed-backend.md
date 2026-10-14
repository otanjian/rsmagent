# 3.5 安装包本机后端端到端复验（合成租户，2026-10-14）

目标：在**已安装的打包版本机后端**（`容大AI 2.2.0`，pid 32156，`http://127.0.0.1:9876`）上，用**合成租户**复验个人记忆的保存/清空/版本拒绝/索引状态与账号隔离，证明**无旧数据复活、无作用域污染**。**用户本人账号与租户数据全程未被触碰。**

## 1. 合成租户（本次新建，可删除）

| 项 | 值 |
|---|---|
| 租户 | `tnt_chKsjtXeM4x5rxZ3`（code `zzsynth`，name `ZZ Acceptance Synthetic`） |
| 管理员 | `zz_synth_admin`（`usr__nvAHoojdtqeGstB`，首次登录强制改密 → 已改为新随机口令，仅存 `%TEMP%\cow-pack\synth-tenant.json`） |
| 成员 | `zz_synth_member`（`usr_f_fG780K9BjMvHke`，角色 `member`，同样强制改密） |
| 租户共享根 | **`D:\AIPJ\cow-tenant-base\tenants\zzsynth`**（在用户 home 之外，见 §2） |
| 个人记忆根 | `…\tenants\zzsynth\users\usr__nvAHoojdtqeGstB`（`memory`/`MEMORY.md` 布局） |
| 创建方式 | `IdentityService.create_tenant(...)`（平台管理员 `admin` + bootstrap 口令做 recent-password 再认证），与 `common/startup_hooks.py` 的 bootstrap 同一条服务路径 |

## 2. 先撞到安全守卫（**守卫行为正确**，不是缺陷）

首次建租户时我把 `shared_root` 指到 `C:\Users\admin\.cow\tenants\zzsynth`，所有记忆请求得到 `503 memory_unavailable`；后端 `~/.cow/run.log` 给出真实原因：

```
[WebChannel] Memory API error: tenant 'tnt_chKsjtXeM4x5rxZ3' shared root
'C:\Users\admin\.cow\tenants\zzsynth' resolves inside the home/global workspace root; refusing to …
```

即 `common/state_dir.py` 的**home/global 越界守卫**按设计拒绝"租户根落在用户 home/全局根里"（`_is_home_or_global_escape` + `_trusted_roots`，`tests/test_state_dir_tenant_containment.py` 覆盖）。按 `_derive_tenant_shared_root` 的规则把根改到 `D:\AIPJ\cow-tenant-base\tenants\zzsynth`（**只更新该合成租户那一行**，`WHERE id=? AND code=?`，改前改后均打印）后，接口立即 200 ✓。

**这本身是一条正面证据**：租户根越界确实被拦，且拦在读取路径上（不是只在创建时）。

## 3. 主流程实测（HTTP，`X-Tenant-ID: tnt_chKsjtXeM4x5rxZ3`）

| 步骤 | 结果 |
|---|---|
| 新建条目 | 控制台**故意不支持**新建（`save` 对 personal 强制要当前 revision，`0`/`''`/`null` → `409 revision_required`），故按设计用**服务层** `PersonalMemoryService.save('MEMORY.md', …, expected_revision=None)` 创建 → `revision=e0c7e1f2…`、`size=153`、`index_state=ok` |
| 列表基线 | **200**，`total=1`，条目 `MEMORY.md`（带 `revision`、`actions.edit/delete`），`collection_revision=9d362bbd…`，`index_state=ok` |
| 读回正文 | **200**，含 marker `COW-SYNTH-2F8A1C-PERSONAL` 与 purpose 文本 → 写入与读取一致 |
| **保存（编辑，带当前 revision）** | **200 success**，新 `revision=06875da8…`（≠ 旧）✓ `index_state=ok` |
| **过期 revision 再保存** | **409 `stale_revision`** ✓（版本条件生效，拒绝覆盖他人/更早的写入） |
| **清空**（`clear_scope=all_personal` + 集合版本） | **200 success**，`{"removed": 1, "generation": 1, "index_state": "ok"}` ✓ ← **generation 递增**，与规范"清空 generation"一致 |
| 清空后列表 | **200**，`total=0`，`collection_revision=e3b0c442…`（＝空串的 SHA-256），**磁盘文件 `MEMORY.md` 已不存在** ✓ |
| 清空后读正文 | 200，无内容（条目已不在） |
| **清空后用旧 revision 再发布** | **409 `stale_revision`** ✓ 且 `total` 仍 0、磁盘文件仍不存在 → **没有复活已清空的正文/索引** ✓✓（本 change 3.x 的核心要求） |
| 索引状态 | 全流程 `index_state=ok`；服务层另提供 `retry_pending_index()`（动作 `retry_index`）用于 pending 恢复，相关可重试分类由 `test_memory_retryable_classification.py`（5/0）与服务层用例覆盖 |
| **跨租户选择** | 同一账号改用默认租户头 `tnt_N8gC2oB4jo5flatC` → **403 `forbidden`** ✓ |

## 4. 账号切换（同一合成租户内两个账号）

| 账号 | 列表结果 | owner |
|---|---|---|
| `zz_synth_admin` | **total=1**，files `['MEMORY.md']`，`index_state=ok` | `usr__nvAHoojdtqeGstB` |
| `zz_synth_member` | **total=0**，files `[]`，`index_state=ok` | `usr_f_fG780K9BjMvHke` |
| 判定 | **互不可见 = True**（同租户、不同账号，无串读/串写） ✓ | — |

## 5. 端口/进程与"remote 只访问本人记忆"的判定

- 本次全部请求打到**安装包的包内后端**：`cowagent-backend.exe` pid **32156**，`Listening on 127.0.0.1 only (local access)`（`~/.cow/run.log`）→ 即"本机安装包复验"用的是**出厂后端**，不是仓库源码环境。
- "remote 只访问服务器本人记忆"：该约束的**授权逻辑**与服务端同一份代码，本次已在真实后端上证明——①未认证/未选租户一律 `400 missing_tenant`（早前探测）；②同一账号跨租户 `403 forbidden`；③同租户两账号互不可见；④客户端不提供"指名他人"的参数（`_personal_memory_service(ctx)` 的 owner 只来自已验证的 `ctx`，代码已核）。
- **未执行的一项**：在**生产服务器 HTTPS 传输**上做两账号正向对比（需要用户在 `rd.rsxm.com.cn` 登录一次）。传输层不改变上述授权逻辑，但为使"remote"一词严格落地，此项如实登记为未执行。

## 6. 复现要点（含坑）

1. 建租户：`IdentityService(db).create_tenant(actor_user_id=<平台管理员>, code=…, admin_username=…, admin_password=…, recent_password=<bootstrap 口令>, shared_root=None)`；**根必须落在配置的租户基座（`COW_TENANT_BASE`）下、且在 home 之外**，否则读取路径守卫拒绝。
2. 登录/改密/业务请求都需 `Origin`/`Referer` 同源头，否则 `/auth/password` 返回 `403 cross_origin`；首次登录 `must_change_password=true` 会**拦下所有业务**（`403 password_change_required`），改密后 `must_relogin` → 必须重新登录。
3. 业务请求需带 `X-Tenant-ID`（否则 `400 missing_tenant`）。
4. 控制台**不能新建**记忆条目；新建走服务层 `PersonalMemoryService.save(..., expected_revision=None)`。
5. `~/.cow/.bootstrap_admin_password` 会在改密流程后被清除（本次已消失）——引导口令是一次性的。
