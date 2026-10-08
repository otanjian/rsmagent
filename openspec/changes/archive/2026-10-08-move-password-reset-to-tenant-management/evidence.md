# 操作与验收说明 — move-password-reset-to-tenant-management

## 操作

- **入口位置**：管理控制台 → 租户管理 → 编辑租户 → 「租户管理」标签 → 「当前租户管理员」卡片右侧。
- **停用/启用账号**：按钮文案随账号状态切换（`tenant_admin_disable_account` / `tenant_admin_enable_account`）。点击后确认目标登录名与「影响该账号所有租户」的提示，复核操作者本次密码，提交目标 `active`、原 `is_platform_admin` 与 `expected_version=account_version`。成功只刷新卡片，不动租户编辑草稿。
- **重置密码**：点击后同样复核操作者密码，提交账号版本；成功后以独立结果弹窗展示一次性临时密码，可「复制」，关闭、关闭编辑器、切换编辑目标或切换租户/身份即清空。重置自己会被引导到本人改密流程。
- **全局影响**：账号状态与密码属于 `users`，两个操作都影响该账号在所有租户的登录；停用会撤销其会话。
- **最后管理员保护**：停用会破坏最后有效平台/租户管理员的请求被服务端拒绝，界面提示先配置其他管理员，不假报成功、不自动改版本重试。
- **平台用户管理**：已退出正常侧栏，也不再出现在控制台首页快捷入口。兼容维护页保留原地址（`#view-platform`）与标题/面包屑，平台 API 与平台权限校验不变。

## 保留 / 回退

- 保留：视图注册、`#view-platform` 页面内容、`/platform` 地址与平台账号 API；与租户页共用的 `admin.tenants` 资格不变。
- 无数据迁移、无新角色、无新 feature flag。
- 回退：使用上一兼容代码版本即可；不回滚已提交的账号状态/密码，也不恢复已撤销会话。

## 验证证据

自动化（本次相关子集；命令固定为 `-q -p no:randomly`，前端用 `node --test`）：

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 管理员读取/写入服务 | `.venv/bin/python -m pytest tests/test_identity_service_writes.py tests/test_identity_web_handlers.py -q -p no:randomly` | 135 passed, 7 subtests |
| 租户编辑卡片（按钮/版本/资格/草稿/迟到响应/一次性结果/上下文清理） | `node --test tests/test_tenant_tabbed_editor_frontend.cjs` | 78 passed |
| 平台账号管理前端 | `node --test tests/test_identity_admin_frontend.cjs` | 35 passed |
| 导航与三语文案 | `node --test tests/test_nav_area_frontend.cjs tests/test_admin_area_group_gating.cjs tests/test_channel_scope_nav_frontend.cjs tests/test_i18n_tenant_editor_keys.cjs tests/test_console_i18n_coverage.cjs` | 112 passed |
| 成员密码重置（相邻改动，未被本 change 修改） | `.venv/bin/python -m pytest tests/test_tenant_member_password_reset.py -q -p no:randomly` | 18 passed |
| OpenSpec 严格校验 | `openspec validate move-password-reset-to-tenant-management --strict` | valid |

## 未完成 / 既有失败（非本次引入）

- **手动浏览器验收未执行**：任务 3.1 要求以测试账号在 classic/split 两种呈现下人工核对两个按钮、跨租户影响说明、最后管理员拒绝、密码结果与隐藏菜单。本次仅完成自动化验证与源码核对，浏览器人工验收待有测试账号时补做；证据不保存密码。
- 以下失败在**变更前的 `HEAD`** 用同一命令同样出现，属既有缺陷，不随本次修改：
  - `tests/test_console_i18n_parity.cjs`：`coding` 命名空间含 `coding_send_in_pane`，但 `tests/fixtures/console_i18n_snapshot.json` 缺该键（快照漂移）。
  - `tests/test_workbench_menu_grant_frontend.cjs`：断言 `chat.html` 含 `id="sidebar-recent-list"`，而该 id 在 `HEAD` 已不存在。
  - `tests/test_forced_password_gate.cjs`、`tests/test_recovered_pages_frontend.cjs`、`tests/test_execution_permission_ui.cjs` 共 4 例在 `HEAD` 同样失败。
