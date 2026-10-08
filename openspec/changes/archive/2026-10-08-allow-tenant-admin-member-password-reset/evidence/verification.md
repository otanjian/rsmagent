# 验证记录

记录日期：2026-10-08（Asia/Shanghai）。本文件补录本会话先前实现轮已执行的工具结果；本次补 change 不重跑业务测试、不重置真实成员密码、不重启服务。共享工作区后续仍有其他改动，因此以下记录不代表最新工作区全量回归通过。

## 已有实现证据

| 内容 | 接入点 |
| --- | --- |
| 固定临时密码及受限对象的事务重置 | `auth/service.py`：`TENANT_RESET_TEMPORARY_PASSWORD`、`reset_tenant_member_password`、`_reset_account_password_in_tx` |
| 成员能力和账号版本投影 | `auth/service.py`：`list_members`，`channel/web/admin_handlers.py`：`TenantMembersHandler` |
| 来源、资格、输入校验及真实路由接入 | `TenantMemberPasswordHandler`、`channel/web/route_registry.py`、`channel/web/web_channel.py` |
| 按能力显示、账号版本及成功结果 | `channel/web/static/js/identity-admin.js`：`resetMemberPassword`、`showPasswordResetResult` |
| 行为测试 | `tests/test_tenant_member_password_reset.py`、`tests/test_identity_web_handlers.py`、`tests/test_identity_admin_frontend.cjs` |
| 文案与规范 | 管理界面三语文案、本次翻译快照字段、`openspec/specs/account-administration/spec.md` |

## 实现轮已通过的检查

```text
.venv/bin/python -m pytest -q tests/test_tenant_member_password_reset.py tests/test_platform_user_admin.py tests/test_identity_web_handlers.py tests/test_route_registry.py --disable-warnings --maxfail=3
162 passed, 7 subtests passed in 24.94s

.venv/bin/python -m pytest -q tests/test_identity_service.py tests/test_identity_service_writes.py tests/test_identity_concurrency_acceptance.py tests/test_identity_auth_security.py tests/test_identity_credential.py tests/test_admin_write_origin_guard.py --disable-warnings --maxfail=2
100 passed in 9.25s

node --test --test-reporter=tap tests/test_identity_admin_frontend.cjs
tests 35, pass 35, fail 0

git diff --check
通过
```

前端针对成员重置的最终检查在通用弹窗传递提交结果、成功结果先展示后刷新列表的实现合入后执行。Python 262 项与前端 35 项是不同命令的通过数，不代表整个仓库测试数。

## 未通过或未完成的检查

- 实现轮扩大前端回归时，相邻平台卡片改动尚未接完，出现 `tenantCardToggleAccount` / `tenantCardResetPassword` 未导出，以及相关文案未齐的失败。该 change 后续有继续修改；本记录不将当时结果当作其当前结论。最新共享文件兼容仍需执行任务 4.3。
- `test_console_i18n_parity.cjs` 的全量快照检查存在既有差异。实现轮以 HEAD 与工作区分别比较，均发现 coding 的 `coding_unavailable`、`coding_send_in_pane` 及 home-scenes 的 `scenes_configure`、`scenes_opening` 三语差异，共 12 处；本次新增的四个 reset 文案字段已同步。后续差异可能随其他修改变化。
- Ruff 检查未执行成功：当前项目虚拟环境没有安装 `ruff`；不将其计为通过。
- 上次运行检查中，9899 服务的新重置路径 GET 探测返回 404，说明当时未加载新路由。未重启该服务；这是实现轮观察，不是本次补文档时的实时状态。
- 浏览器打开成员页只到登录界面，没有可用的已登录验证会话；未对真实成员执行改密或重置。任务 5.3、5.4 保持未完成。

## 本次 OpenSpec 检查

2026-10-08 执行 `openspec validate allow-tenant-admin-member-password-reset --strict`，返回 `Change 'allow-tenant-admin-member-password-reset' is valid`，严格校验通过。proposal、design、specs、tasks 四项产物齐全；任务 5.2 已完成。

## 完成标准

文档和既有实现补录完成后，继续保留最新集成回归、目标服务加载、真实浏览器验收三项待办。只有取得相应证据才勾选，不归档为全部完成。
