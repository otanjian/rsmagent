## Why

database 身份模式下，已上传的智能体头像在控制台不显示：控制台以 `<img>` 子资源读取 `GET /api/agents/<agent_id>/avatar`，而该请求结构上无法携带 `X-Tenant-ID`；该路由在权威清单中未声明「租户由被寻址资源派生」，门禁在进入 handler 前即按「缺租户选择」返回 400 `missing_tenant`，头像永远取不到。同一类问题已有先例（`GET /uploads/(.*)`、`GET /api/file`、`GET /stream`），头像读取是同类遗漏；不修则任何租户上传的头像都不可见，控制台只能退回首字母圆盘。

## What Changes

- 在权威路由清单中把 `GET /api/agents/([^/]+)/avatar` 标记为「租户由被寻址资源派生」（与 `/uploads`、`/api/file`、`/stream` 同类）：门禁仍 SHALL 要求有效凭据，但不再要求请求头给出租户，豁免以显式标记声明而非路径特例。
- 为头像读取新增资源派生租户解析：凭据有效 → 由被寻址 `agent_id` 的租户绑定派生租户 → 显式租户选择与派生值冲突时 400 → 调用者对该租户的成员资格校验 403 → 保留对象级智能体读取授权校验。
- 头像响应在**未**提供租户选择时返回图片内容而非 400；无法由智能体绑定解析出租户时返回 404，不泄漏资源存在性。
- 非目标：不改动头像上传语义与文件名/类型约定，不改动控制台头像回退样式，不改动工作台列表的身份范围过滤。

## Capabilities

### New Capabilities

（无。本 change 只修正既有路由的租户派生行为，不引入新能力。）

### Modified Capabilities

- `console-route-lifecycle`: 「tenant/platform 路由在进入 handler 前解析并校验请求上下文」中，资源派生租户路由的枚举与场景 SHALL 覆盖智能体头像读取 `GET /api/agents/<agent_id>/avatar`（浏览器子资源、无法附加自定义请求头）。

## Impact

- `channel/web/route_registry.py`：`/api/agents/([^/]+)/avatar` 的 `GET` 条目新增 `tenant_from_resource=True` 及说明注释，成为权威清单中该豁免的唯一声明位置。
- `channel/web/fork/handlers/agents.py`：`AgentAvatarHandler.GET` 接入资源派生身份解析（新增 `_avatar_identity_scope`，与 `_uploads_identity_scope`、`_file_identity_scope` 同构）。
- 测试：路由清单断言与头像读取用例（header-less 读取成功、成员资格、显式冲突、未知绑定 404）。
- 数据唯一归属：头像文件仍归租户共享根目录 `<tenant_shared_root>/avatars/<agent_id>.<ext>`，`AgentProfile.avatar` 为 `"image"` 时生效；租户归属由 `agent_bindings` 派生，本 change 不新增存储。
- 跨 change 依赖：租户与成员关系的权威定义沿用 `identity-session`、`user-membership`、`tenant-agent-provisioning`；本 change 只消费，不重定义。
- 无数据库 schema 变更、无控制台前端契约变更（前端已按 `/api/agents/<id>/avatar` 读取并在失败时回退）。
