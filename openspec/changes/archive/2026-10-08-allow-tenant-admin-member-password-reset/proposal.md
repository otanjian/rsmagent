## Why

成员管理原先仅允许平台管理员重置密码，租户管理员无法处理本租户普通成员的密码恢复。用户已确认开放这项能力，并指定该租户重置流程的临时密码固定为 `123456`；本 change 补录本次已实现的行为、验证证据及待完成的运行验收。

## What Changes

- 为有效租户管理员增加本租户独占普通成员的密码重置接口和成员行按钮；共享账号、平台管理员、租户管理员账号不在此接口授权范围，本人使用个人改密。
- 租户重置临时密码固定为 `123456`，24 小时有效，登录后强制改密，重置立即撤销旧会话并记录租户内可见的脱敏审计。新建账号、永久密码的强度规则及平台重置的随机密码策略保持原状。
- 复核操作者本次密码，在写事务内重新校验会话、资格、账号版本及成员归属，拒绝并发撤权、跨租户共享、提权、停用或版本冲突。
- 成员列表增加 `user_version` 和 `can_reset_password`，前端提交账号版本；重置结果独立显示，关闭清空，列表刷新失败不丢失成功结果。
- 补齐服务、HTTP、前端及语言文案回归，并记录服务加载和真实浏览器验收尚未完成的状态。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `account-administration`: 明确租户管理员重置独占普通成员的例外范围、固定临时密码、并发校验及结果展示契约。

## Impact

- 接入点：`auth/service.py`、`channel/web/admin_handlers.py`、`channel/web/route_registry.py`、`channel/web/web_channel.py`、`channel/web/static/js/identity-admin.js` 和管理界面三语文案。
- 新增 `POST /api/tenant/members/{id}/password`；`GET /api/tenant/members` 增加非敏感投影字段，已有成员更新和平台重置路径兼容。
- 数据唯一归属：密码、强制改密标记、到期时间与账号版本属于 `users`；租户归属和角色属于现有成员及角色关联表；会话和审计仍由 `auth_sessions`、`audit_events` 承载。不建新表、不新增可配置权限点或 feature flag。
- 依赖既有 `identity-session`、密码哈希及审计事务能力，不扩大其责任域。相邻 `move-password-reset-to-tenant-management` 负责平台管理员卡片及菜单，本 change 负责租户成员重置；共享文件与弹窗完成回调需要兼容，但相邻 change 整体不是启用前置。
- 主规范已在实现轮同步，本次 delta 按当前主规范使用 `MODIFIED Requirements` 补齐验收场景；不重复归属其他 change 的平台卡片、导航或样式改动。
