## 1. 资产前提（离线生成，复核即可）

- [x] 1.1 复核 `AI租户1`（`tnt_EA3qM-lHPLD8ZPwW`，共享根 `/Users/jiantan/.cow/tenant-roots/tenants/test15`）`avatars/` 下 69 张头像齐备：该租户绑定 70 个智能体，按既定范围跳过默认智能体（实测 `ls | wc -l` = 69）
- [x] 1.2 复核 `team.json` 中这 69 个智能体为 `avatar: "image"`，默认智能体 `my-assistant-admin-test15` 保持原值（保留产品 logo）（实测 `avatar=image` 计数 69，默认智能体为 `null`）
- [x] 1.3 复核生成脚本 `tmp/generate_agent_avatars.py` 可重跑（`--tenant` + `--skip` 幂等），且不触碰其他租户目录（实测 `--no-write` 渲染 69 张耗时 1.4s，文件数与既有目录均未变化；其他租户根下无 `avatars/`）

## 2. 回归测试（RED）

- [x] 2.1 新增 `tests/test_console_agent_avatar_transport.py`：`GET /api/agents/<本租户非默认 agent>/avatar` 仅带 cookie（无租户头）返回 200 与图片字节
- [x] 2.2 新增：他租户绑定 / 无绑定的智能体返回 404，响应体不含图片字节
- [x] 2.3 新增：无凭据返回 401（资源派生不是认证绕过）
- [x] 2.4 新增：请求头租户与派生租户冲突返回 400 `conflicting_tenant`
- [x] 2.5 新增：绑定存在但无头像文件时仍 404，且不因该分支改变成员/授权语义
- [x] 2.6 新增：`tests/test_route_registry.py` 断言 `/api/agents/([^/]+)/avatar` 的 `GET` 策略仍为 `tenant` 且带 `tenant_from_resource`；并断言同一路径的 `POST`（上传）**不**带该标记
- [x] 2.7 确认 2.1 至 2.6 在实现前失败（实测 8 failed / 27 passed；失败原因均为 `400 missing_tenant` 与 `tenant_from_resource` 缺失，`test_read_is_unchanged_when_the_caller_sends_the_matching_header` 在实现前即通过，作为「带正确租户头的既有调用不变」的对照）

## 3. 实现（GREEN）

- [x] 3.1 `channel/web/route_registry.py`：`/api/agents/([^/]+)/avatar` 的 `GET` 增加 `tenant_from_resource=True`，注释说明「控制台/桌面端以 `<img>` 读取，无法发送 `X-Tenant-ID`，租户由被寻址智能体的绑定派生」
- [x] 3.2 `channel/web/fork/handlers/agents.py`：新增 `_avatar_identity_scope(agent_id)`——无凭据 401 → `resolve_context(svc, token, None)` 仅认证 → 由 `get_agent_binding(agent_id)` 派生租户（无绑定 404）→ 显式选择冲突 400 → 派生租户 `resolve_context` 校验成员资格 403 → `must_change_password` 403 → `use_identity` 发布身份
- [x] 3.3 `AgentAvatarHandler.GET` 改用该 scope，保留 `_require_tenant_agent_binding` 与 `_require_agent_action(..., "read", "agent.read")` 对象级校验
- [x] 3.4 不改动 `POST`（上传）语义、`_avatar_path()` 的扩展名优先级与响应缓存头

## 4. 验证

- [x] 4.1 `tests/test_console_agent_avatar_transport.py` 全通过（8 passed）
- [x] 4.2 回归：`test_route_registry.py`、`test_http_policy.py`、`test_http_gate.py`、`test_console_upload_transport.py`、`test_console_file_transport.py`、`test_web_module_seams.py`、`test_identity_scope_gate.py`、`test_tenant_read_scoping.py`、`test_private_agent_file_scope.py`、`test_private_resource_acceptance.py`、`test_chat_identity_context.py` 共 179 passed；另 `test_agent_workbench.py`、`test_tenant_channel_console_scope.py`、`test_session_idor_closure.py`、`test_web_sse_replay.py`、`test_identity_web_handlers.py` 共 142 passed；另 `test_agent_web_management.py`、`test_agent_clone_primitives.py`、`test_direct_addressing.py`、`test_private_agent_owner_reachability.py`、`test_branding.py`、`test_user_avatar.py`、`test_session_store_resolution.py` 共 180 passed
- [x] 4.3 `scripts/check-web-module-seams.py` 0 findings（22 upstream module(s), 276 fork-only symbol(s)）
- [x] 4.4 `openspec validate fix-agent-avatar-render-in-tenant-mode --strict` 通过
- [x] 4.5 真实实例：重启服务端（新 PID，10:30:01 启动）后，控制台内以无自定义头的 `<img>`/`fetch` 读取 `GET /api/agents/bug-butler-test15/avatar`、`GET /api/agents/knowledge-qa-test15/avatar` 均为 200 + `image/png`，`naturalWidth=512`；响应字节数 47739 与该租户 `avatars/bug-butler-test15.png` 的 `stat -f %z` 完全一致；`GET /api/agents/nope-not-bound-agent/avatar` 仍为 404，默认智能体（无头像文件）仍 404
- [x] 4.6 控制台实测：`AI租户1` 智能体墙渲染 206 张头像子资源（200 张已加载），`/api/agents/<id>/avatar` 均为 512×512 人像；默认智能体「智能办公助理」仍显示产品 logo（截图已留存）

## 5. 收尾

- [x] 5.1 记录验收证据（见 4.1–4.6 实测数值；RED 阶段 8 failed/27 passed，GREEN 阶段全通过）
- [x] 5.2 确认路由基线无需变更（策略仍为 `tenant`，仅新增资源派生标记，基线快照格式不含该字段）
- [x] 5.3 全量套件对照：`pytest tests/ -q` 得 33 failed / 6318 passed；对 33 个失败用例做「暂存本 change 改动 → 单独重跑 → 恢复改动 → 再跑」对照，两次均为 **28 failed / 5 passed 且新增失败集合为空**，即 33 项失败与本 change 无关（来自工作树中其他未提交改动与用例顺序依赖：`test_weixin_qr_flow`、`test_external_*`、`test_channel_startup_open`、`test_help_site`、`test_personal_console_frontend`）

## 6. 备注（本次不改动、需知悉）

- `my-assistant-admin-test15-RC001` 对当前登录账号仍返回 403：该智能体是 `RC001`(Rock) 的私有智能体（`agent_bindings.private_owner_user_id = usr_2EaG0EhEqh85w60w`），既有「私有内容管理员不放行」规则在**带**与**不带**租户头时都返回 403，本 change 未改变该语义；是否对管理员展示他人私有智能体头像属于既有策略范围，不在本 change 内。
