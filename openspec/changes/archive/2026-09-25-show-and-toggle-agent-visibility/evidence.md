# show-and-toggle-agent-visibility 验收证据

日期：2026-09-24。记录本 change 的红/绿核验、回归对比、数据面零改动复核与规范校验输出。

## 1. 规范校验

```
$ openspec validate show-and-toggle-agent-visibility --strict
Change 'show-and-toggle-agent-visibility' is valid
```

## 2. 红：新断言在改动前失败

在干净检出上运行（`git worktree add --detach /tmp/rsm-baseline-vis HEAD`，HEAD = `6a8ebccd`），把三个新用例文件拷入后执行：

```
$ .venv/bin/python -m pytest tests/test_agent_visibility_toggle.py tests/test_agent_visibility_routes.py -q
FAILED tests/test_agent_visibility_toggle.py::GrantAppliesOnlyAfterSharingTests::test_the_grant_applies_once_the_owner_shares_it
FAILED tests/test_agent_visibility_toggle.py::GrantAppliesOnlyAfterSharingTests::test_the_owner_loses_the_management_range_after_sharing
FAILED tests/test_agent_visibility_toggle.py::AgentVisibilityProjectionTests::test_after_sharing_the_admin_sees_it_as_restorable
FAILED tests/test_agent_visibility_toggle.py::AgentVisibilityProjectionTests::test_after_sharing_the_owner_no_longer_sees_it
FAILED tests/test_agent_visibility_toggle.py::AgentVisibilityProjectionTests::test_an_admin_sees_the_shared_object_as_restorable
FAILED tests/test_agent_visibility_toggle.py::AgentVisibilityProjectionTests::test_the_owner_sees_their_own_object_as_private_and_sharable
FAILED tests/test_agent_visibility_routes.py::test_the_owner_shares_their_own_agent_over_the_wire
FAILED tests/test_agent_visibility_routes.py::test_a_fellow_member_cannot_share_it
FAILED tests/test_agent_visibility_routes.py::test_an_admin_restores_it_to_a_named_member
FAILED tests/test_agent_visibility_routes.py::test_restoring_the_tenant_default_reports_its_own_reason
FAILED tests/test_agent_visibility_routes.py::test_another_tenants_agent_is_not_found
FAILED tests/test_agent_visibility_routes.py::test_an_unknown_visibility_is_a_bad_request
FAILED tests/test_agent_visibility_routes.py::test_a_missing_id_never_falls_back_to_the_tenant_default
FAILED tests/test_agent_visibility_routes.py::test_sharing_does_not_reload_the_agent_runtime
28 failed, 3 passed

$ node --test tests/test_agent_visibility_frontend.cjs
✖ tests/test_agent_visibility_frontend.cjs
ℹ tests 1  ℹ pass 0  ℹ fail 1
```

基线里通过的 3 条是「非法/未知入参不产生写入」一类：改动前路由根本不认 `set_visibility`，它们断言的拒绝行为恰好也成立，因此不算覆盖。

## 3. 绿：改动后

```
$ .venv/bin/python -m pytest tests/test_agent_visibility_toggle.py tests/test_agent_visibility_routes.py -q
31 passed in 11.68s

$ node --test tests/test_agent_visibility_frontend.cjs
✔ the badge names which of the two states the object is in
✔ a row the server did not describe renders no badge at all
✔ the detail header takes the same pill without the corner positioning
✔ the conversion entry follows the server-derived eligibility
✔ neither eligible direction means no entry, not a disabled one
✔ the list row carries the visibility chip beside the default chip
✔ a shared row is labelled shared and an archived one keeps its own chip
✔ sharing first asks, and the question spells out what actually changes
✔ confirming the share writes exactly one tenant conversion
✔ a row the server did not authorize offers no action and posts nothing
✔ restoring refuses to submit until an owner is chosen
✔ choosing an owner posts it and re-reads the list
✔ a refusal is reported by its own reason and never reads as success
✔ every refusal code the route can send has its own wording
ℹ tests 14  ℹ pass 14  ℹ fail 0
```

## 4. 既有原语回归（任务 3.6）

本 change 不改 `make_agent_tenant_shared` / `restore_private_agent_owner` 的门禁与审计语义，因此这些以「非归属人的管理员」为夹具的用例必须逐字不变地全绿：

```
$ .venv/bin/python -m pytest tests/test_default_agent_tenant_shared.py \
    tests/test_private_agent_owner_reachability.py tests/test_private_agent_lifecycle.py \
    tests/test_private_agent_quota.py tests/test_management_restore_private_owner.py \
    tests/test_identity_resource_authorization.py -q
127 passed in 25.80s
```

更大范围的智能体/身份/租户筛选用例 2324 passed、7 failed。7 条失败全在外部渠道（`test_external_connection_service`、`test_external_connections_api`、`test_session_idor_closure`、`test_weixin_qr_flow`）与渠道投递（`test_external_channel_propagation`，随执行顺序污染而失败，单独跑通过）。把 `auth/service.py`、`channel/web/fork/handlers/agents.py` 暂存回 HEAD 后，同一组用例的失败集合与改动后完全一致（6 failed / 1 passed），确认与本 change 无关。

## 5. 全量前端回归（任务 3.7）

```
$ node --test tests/*.cjs        # 改动后
$ diff <(改动前失败集合) <(改动后失败集合)
10d9
< ✖ tests/test_agent_visibility_frontend.cjs
```

唯一的差异是本次新增用例由红转绿，**新增失败为 0**。改动前后都失败的集合（`test_sidebar_account_frontend`、`test_appearance_browser`、`test_personal_console_frontend`、workbench registry 等）与本 change 无关。

### 一个踩坑记录：新渲染函数放在哪一段

`tests/*.cjs` 用 `source.indexOf(...)` 切片加载 `console.js` 的片段，切片边界就是文件里的函数名，所以**函数落在哪两个既有函数之间**决定了别的用例是否能看见它。

`test_tenant_default_agent_frontend.cjs`、`test_user_default_agent_frontend.cjs`、`test_agent_config_fields_hidden_frontend.cjs` 都会切 `section('function renderAgentDetail()', 'function renderAvatarPicker(')`，再加上各自 stub 的 `renderAgentDetail` 依赖。第一版把 `agentVisibilityBadgeHTML` / `agentVisibilityActionsHTML` 放到 `loadAgentCatalog` 与 `renderAgentsGrid` 之间，结果这些用例切到的 `renderAgentDetail` 里引用了切片里没有的徽标函数：

```
ReferenceError: agentVisibilityBadgeHTML is not defined
    at tests/test_user_default_agent_frontend.cjs:85
    at tests/test_tenant_default_agent_frontend.cjs:88
    at tests/test_agent_config_fields_hidden_frontend.cjs:128
```

放回 `renderAgentDetail` 与 `renderAvatarPicker` 之间（即详情面板那一段）后全部恢复，本 change 自己的用例改用同一段边界（`agentVisibilityBadgeHTML` → `renderAvatarPicker`）取徽标代码。**后续若再挪动这两个渲染函数，必须先确认这三处切片边界。**

## 6. 数据面零改动复核（任务 4.3）

```
$ git diff --stat -- auth/store.py auth/service.py channel/web/fork/handlers/agents.py
 auth/service.py                     | 84 ++++++++++++++++++++++++++++++
 channel/web/fork/handlers/agents.py | 75 ++++++++++++++++++++++++++++++
 2 files changed, 159 insertions(+)
```

`auth/store.py` 无 diff：`agent_bindings` 表结构不变、无新增迁移函数、无回填。转换只写既有的 `private_owner_user_id` 列。

## 7. 未执行

任务 4.5 的端到端确认需要具备登录会话的人在本机 `localhost:9899` 上操作，本次未执行。
