## Context

动机与实测现象见 `proposal.md - Why`。本 change 的接入点现状（已核对的代码位置）：

- `channel/web/route_registry.py`：`/api/agents/([^/]+)/avatar` 的 `GET` 记为 `tenant`，**未**带资源派生标记；同一张表里 `GET /uploads/(.*)`、`GET /api/file`、`GET /stream` 三处已带 `tenant_from_resource=True`。
- `channel/web/fork/handlers/agents.py::AgentAvatarHandler.GET` 以 `_db_scope()` 进入，而 `_db_scope()` → `_require_context(require_tenant=True)` 在缺 `X-Tenant-ID` 时直接 400 `missing_tenant`，`_avatar_path()` 根本不会被调用。
- `auth/http_policy.py::_enforce_context_gate` 用 `require_tenant = policy == "tenant" and not entry.get("tenant_from_resource")` 决定是否强制租户选择：豁免只能由清单显式声明，门禁里没有按路径的特例；且该路由在清单中未声明 permission，门禁只做「上下文存在性 + 身份域」。
- 既有同形解法：`channel/web/fork/handlers/files.py` 的 `_uploads_identity_scope()` / `_file_identity_scope()`（无 token → 401；先 `resolve_context(svc, token, None)` 只认证；由被寻址资源派生租户；显式选择冲突 → 400 `conflicting_tenant`；派生租户 `resolve_context` 校验成员资格 → 403；`use_identity(to_runtime_identity(ctx))` 发布请求作用域身份）。
- 头像资产与身份耦合：文件位于 `<tenant_shared_root>/avatars/<agent_id>.<ext>`，`_avatar_path()` 经 `common.state_dir.shared_root()` 解析，而 `shared_root()` 在 database 模式下由**当前已发布的身份**决定租户共享根（无租户身份即失败，绝不回落全局根）。因此「先有租户身份，才能读到该租户的 avatars 目录」是本修复的硬约束。
- 两端前端都直接把该地址交给浏览器：`channel/web/static/js/views/agents.js`（`<img src="/api/agents/<id>/avatar?v=...">`）与 `desktop/src/renderer/src/components/AgentAvatar.tsx`（`<img src=apiClient.agentAvatarUrl(...)>`）。子资源请求结构上无法附加自定义请求头。

## Goals / Non-Goals

**Goals:**

- 已登录且获权的成员在**不带** `X-Tenant-ID` 的浏览器子资源请求下，能读到本租户智能体的头像字节。
- 头像路由的租户真值来自服务端的智能体绑定，与 `/uploads`、`/api/file`、`/stream` 使用同一模式与同一拒绝顺序。
- 门禁确定性拒绝顺序、对象级 `agent.read` 校验、头像文件名/类型约定均不变。

**Non-Goals:**

- 不放宽跨租户读取：非成员一律 403，无绑定一律 404，且不因状态码差异泄漏资源存在性。
- 不改 `POST /api/agents/([^/]+)/avatar`（上传是 `fetch`，可以带租户头，仍要求显式租户选择）。
- 不改 `_avatar_path()` 的扩展名优先级、`AgentProfile.avatar` 的 `"image"` 标志语义，以及控制台/桌面端的回退样式。
- 不把租户真值交给客户端（查询参数/请求头只用于**冲突检测**）。
- 不为 legacy 身份模式引入新分支（该模式下 `_db_scope()` 的既有行为与本 change 无关，见 Risks）。

## Decisions

**决策 1：头像 `GET` 采用「租户由被寻址智能体派生」，与 `/uploads` 同形。**

`route_registry.py` 给该 `GET` 增加 `tenant_from_resource=True`，注释写明「控制台/桌面端以 `<img>` 读取，无法发送 `X-Tenant-ID`；租户由被寻址智能体的绑定派生」。策略仍为 `tenant`（不是 `public`）：门禁在 database 模式下仍解析并校验凭据，只是不再强制显式租户选择。

**决策 2：新增 `_avatar_identity_scope(agent_id)`，并放在唯一消费者所在的 `fork/handlers/agents.py`。**

步骤（与 `_uploads_identity_scope` 逐条对应）：

1. 取 `_session_token()`，无凭据 → 401 `unauthorized`；
2. `resolve_context(svc, token, None)`：**只认证**，不受任何租户头影响（因此「无绑定 404」对匿名调用者不可观测）；
3. `svc.get_agent_binding(agent_id)` 派生租户；无绑定 / 绑定无租户 → 404（不泄漏存在性）；
4. 显式选择（`X-Tenant-ID` 请求头或查询 `tenant_id`）与派生租户不一致 → 400 `conflicting_tenant`；
5. `resolve_context(svc, token, derived_tenant)` 校验有效成员资格（非成员 → 403）；
6. `must_change_password` → 403 `password_change_required`；
7. `use_identity(to_runtime_identity(ctx))` 发布请求作用域身份，`yield (ctx, agent_id)`。

`AgentAvatarHandler.GET` 改用该 scope 取代 `_db_scope()`，并**保留** `_require_tenant_agent_binding(ctx, agent_id)` 与 `_require_agent_action(ctx, resolved, "read", "agent.read")`：scope 保证上下文存在，对象级授权仍留在 handler，符合 `console-route-lifecycle` 的「门禁不替代对象级校验」。

- 为什么 scope 必须**发布身份**而不只是返回 `ctx`：`_avatar_path()` 走 `shared_root()`，后者从已发布身份取租户共享根。若不发布，读取会退回全局默认根——那是跨租户读取，而不是修好读取。
- 为什么 `agent_id` 由参数传入：头像的智能体标识来自 URL 路径（`/api/agents/<agent_id>/avatar`），而 `/uploads` 读取的是 `agent_id` 查询参数，两者来源不同，不能照搬。
- 备选：前端改成 `fetch` 取 blob 再赋给 `<img>`。否决——Web 与桌面两端都要改，且把「谁是租户真值」的判断挪到了客户端。
- 备选：让 `?tenant_id=` 成为权威。否决——把租户真值交给客户端；仅用于冲突检测才是安全的。
- 备选：把 scope 放进 `fork/authorization.py`（与通用 `_db_scope` 同处）。未采纳——既有两个资源派生 scope 都随其消费者放在 handlers 模块中，此处遵循同一归属。

**决策 3：把该路由登记进 `console-route-lifecycle` 的资源派生枚举。**

该 spec 的 requirement 已用「至少包含两类」列举 `GET /stream`、`GET /uploads/(.*)`、`GET /api/file`；头像读取是同一类的遗漏实例。它由浏览器子资源发起、结构上无法带头，且其租户可由「绑定的智能体」解析——正是 requirement 已认可的两种来源之一。登记方式沿用既有做法：清单显式声明（`MODIFIED` 该 requirement 的枚举 + 新增场景），而不是在门禁里加路径特例。

## Risks / Trade-offs

- [「智能体 id → 租户」派生可被用于探测] → 先认证再派生；无法派生一律 404，不区分「不存在」与「不可见」；成员资格由 `resolve_context` 强制，非成员 403。
- [显式租户头与绑定不一致时的错误码由 `400 missing_tenant` 一类的缺选择错误变为 `400 conflicting_tenant`] → 仅影响陈旧/错误选择；同租户正常调用（头值与绑定一致）行为不变，成功路径不受影响。
- [发布身份改变了同请求内的 `shared_root()` 解析] → 这正是修复所需；`use_identity` 只在 `with` 块内生效，门禁身份与其一致（同一派生租户）。
- [头像响应可缓存 1 天，换头像后可能读到旧图] → 沿用既有 `Cache-Control: private, max-age=86400` 与前端 `?v=<avatar_rev|revision>` 版本参数；本次不改缓存策略。
- [legacy 身份模式] → 该模式不经过门禁，且本 change 不新增分支，行为与既有 `_uploads_identity_scope` / `_file_identity_scope` 同形；template 中不宣称对 legacy 模式的能力。

## Migration Plan

无数据迁移、无 schema 变更、无前端改动。部署后重启进程即生效（路由清单在导入期构建）。回滚只需还原 `channel/web/route_registry.py` 与 `channel/web/fork/handlers/agents.py`（并移除本 change 新增的测试文件），回滚后回到「头像请求 400 `missing_tenant`、控制台回退首字母圆盘」的既有状态。

## Open Questions

无。
