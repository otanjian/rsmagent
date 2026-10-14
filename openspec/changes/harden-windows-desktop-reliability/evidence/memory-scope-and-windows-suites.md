# 阶段 3 证据：记忆保存/清空/索引/账号切换与本人作用域（任务 3.5，部分完成）

日期：2026-10-13。任务原文：*复用受影响的Windows保存/清空、索引恢复及账号切换测试；本机安装包复验修复路径，remote只访问服务器本人记忆，证据证明无旧数据复活/作用域污染。*

## 1. 已复用的受影响套件（行尾修复后复跑，本机实跑）

| 套件 | 结果 |
|---|---|
| `test_account_memory_management.py` | **21 passed** |
| `test_personal_memory_console.py` | **30 passed** |
| `test_memory_path_marks_dirty.py` | **19 passed** |
| `test_personal_memory_tool_execution.py` | **18 passed** |
| `test_memory_storage_tenant_scope.py` | **5 passed** |
| `test_tenancy_isolation_acceptance.py` | **6 passed** |
| `test_memory_index_rel_path.py` | 5 passed / **1 failed**：`test_a_root_reached_through_a_symlink_still_matches` 抛 `WinError 1314`（本会话无创建符号链接权限）→ 环境 |

## 2. "无旧数据复活 / 无作用域污染"的直接用例

`test_account_memory_management.py` 中与本任务同名的性质：

- `test_stale_batch_cannot_overwrite_edit_or_revive_clear` —— 过期批次既不能覆盖编辑，也**不能复活**已清空内容；
- `test_migration_is_idempotent_owner_scoped_and_rollback_preserves_edits` —— 迁移幂等、按属主限定，回滚保留编辑；
- `test_producer_identity_mismatch_is_refused` —— 生产者身份不符即拒。

## 3. "remote 只访问服务器本人记忆"

- 代码层：`agent/memory/personal.py:105` 对他人账号文件直接 `PersonalMemoryError('不可访问其他账号的个人文件', code='forbidden', status=403)`；个人记忆的读写都在 `scope_transaction(user_root)` 内完成（同一文件多处），根由身份决定而非请求参数。
- 模式层：remote 模式下**不启动本机 Python 后端**（`index.ts` 的 remote 分支只打日志，见 6.2 证据），记忆属服务器业务，由容器携带的会话按认证用户访问服务器数据 → 不会落到本机后端的另一份记忆上。
- 测试层：`test_personal_memory_console.py` 30、`test_memory_storage_tenant_scope.py` 5、`test_tenancy_isolation_acceptance.py` 6、`test_account_memory_management.py` 21 均通过；账号切换/身份失效另有 `test_desktop_signout.cjs` 9/0 与 `test_desktop_web_session.py` 30 passed。

## 3.1 随包复验（2026-10-14，真实安装包）

安装包已真实安装（见 `release-smoke-windows-installer.md`）。为验证"修复随包发布、且守卫在读取数据前拦下"，**直接启动包内后端**（`%LOCALAPPDATA%\Programs\cowagent-desktop\resources\backend\cowagent-backend\cowagent-backend.exe`，31 MB，独立进程、不开窗口），与 dev 后端做同构探测：

| 探测 | dev 后端（源码，9899） | **包内后端（安装包，9876/9899）** |
|---|---|---|
| `GET /api/desktop/meta` | **200** | **200** |
| `GET /api/memory`（未认证） | **400 `missing_tenant`** | **400 `missing_tenant`** |
| `GET /api/memory/personal`（未认证） | — | **400 `missing_tenant`** |
| `POST /api/memory/save|personal|delete|clear`（未认证） | 全部 **400 `missing_tenant`** | 同构（路由与守卫一致） |

即：**记忆读写接口在任何数据访问之前就要求租户/身份选择**，拒绝理由带稳定 `code`（`missing_tenant`），并且这套守卫**确实随安装包发布**（不是只在源码里）。

**顺带观察（记录）**：包内后端启动时报告监听 **9899**，而当时 dev 后端已占用该端口——Windows 的 `SO_REUSEADDR` 语义允许两个进程都 bind 成功（这与本 change 任务 5.1/`backend-port.ts` 关注的"端口探测不可靠"同源）。停止包内后端后复核：9899 由 dev 后端独占、`meta` 200、窗口正常，未造成干扰。

## 4. 判定与未完成

- **仍未勾选，但阻塞已换**：安装包**已构建并真实安装**，且已对**包内后端**复验守卫与错误码（见 §3.1：`meta` 200；记忆读写未认证一律 `400 missing_tenant`）。剩余未做的是**需要登录态的端到端**：在已安装应用上用真实账号走"保存 → 清空 → 重试索引 → 切换账号"，并确认清空后不复活、切号后看不到他人记忆、远程只读写服务器本人记忆。**这需要用户在自己的账号下登录一次**（我没有凭据）。
- 补齐路径：用户在已安装应用里登录并授权本客户端后，我可直接驱动 `http://127.0.0.1:<port>` 的记忆接口（或用 CDP 读会话 cookie）完成上述端到端复验并留证。
