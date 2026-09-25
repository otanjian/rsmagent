## 1. 服务端

- [x] 1.1 `auth/service.py` 新增 `IdentityService.set_agent_visibility(*, agent_id, actor_user_id, visibility, owner_user_id=None)`；`visibility="tenant"` 方向要求调用者是该绑定的归属人本人或持有本租户控制资格（`_is_control`），写入复用 `make_agent_tenant_shared` 的语义（清空 `private_owner_user_id` + 审计 `agent.make_tenant_shared`），已共享时幂等返回 `changed: false`。（设计 D2）
- [x] 1.2 同一方法 `visibility="private"` 方向：要求 `_require_tenant_admin(actor_user_id, tenant_id)` 且必须显式给出 `owner_user_id`；校验复用 `restore_private_agent_owner` 的规则（目标为租户默认 → 409 `agent_is_tenant_default`；归属人须为本租户有效成员），审计沿用 `agent.restore_private_owner`。（设计 D2）
- [x] 1.3 非法入参（未知 `visibility`、缺 `owner_user_id`、空 `agent_id`、绑定不存在）返回稳定错误码且不产生任何写入；两个既有原语 `make_agent_tenant_shared` / `restore_private_agent_owner` MUST NOT 被修改门禁或审计语义（约 10 个既有测试以「非归属人管理员」为夹具依赖该门禁）。（设计 D2、规范「转换失败 SHALL NOT 留下部分状态」）
- [x] 1.4 `channel/web/fork/handlers/agents.py` 的 `AgentsHandler.POST` 新增 `action: "set_visibility"` 分支：先经既有 `_require_tenant_agent_binding(ctx, agent_id)` 按 404 挡跨租户，再调用 `set_agent_visibility`；业务拒绝（如 `agent_is_tenant_default`）经既有 `_error` 走结构化响应，MUST NOT 降级为 200 状态包装的泛化错误。（设计 D3）
- [x] 1.5 该分支 MUST NOT 调用 `_reload_agent_runtime`：转换写入 `identity.db` 的绑定行而非 `team.json`，不参与 roster `revision` 校验，也不触发运行时重载。（设计 D6）
- [x] 1.6 `_tenant_agents_admin_projection` 每行新增 `visibility`、`can_share`、`can_unshare`，三者均由绑定与服务端资格派生，界面 MUST NOT 自行推断。（设计 D4）

## 2. 前端

- [x] 2.1 `channel/web/static/js/console.js` 智能体管理列表行增加「私有」/「租户共享」徽标，与既有「默认」徽标并列展示，标识与授权判定同源。（规范「列表与详情展示可见性」）
- [x] 2.2 `renderAgentDetail` 的详情头增加同一徽标与转换入口：`can_share` 为真时出「转为租户共享」，`can_unshare` 为真时出「恢复为私有」，两者皆否时 MUST NOT 渲染任何转换入口。（设计 D4）
- [x] 2.3 「转为租户共享」提交前 MUST 弹出确认并明确告知后果：本租户全部成员可见并可按各自授权使用；共享后该对象由租户管理员维护，归属人不再独占维护，但仍可在工作台与聊天中继续使用。（规范「归属人转为租户共享」）
- [x] 2.4 「恢复为私有」MUST 要求选择归属人，复用既有 `GET /api/tenant/members`（不新增成员查询管道）；未选择归属人时不提交。（设计 D3、风险「管理员替他人恢复私有需要选择归属人」）
- [x] 2.5 提交失败按服务端 `code` 给出可读原因，至少覆盖 `agent_is_tenant_default`、`forbidden`、`not_found`；失败 MUST NOT 改写列表与详情的既有状态或把失败呈现为成功。
- [x] 2.6 `channel/web/static/js/i18n/agents.js` 补齐 zh-CN / zh-TW / en 三语文案，并同步 `tests/fixtures/console_i18n_snapshot.json`；MUST NOT 删除或改写既有 key。

## 3. 测试

- [x] 3.1 先写红：新增服务端用例覆盖六种情形——归属人共享成功；非归属人非管理员共享被拒且归属与可读范围不变；管理员恢复为指定归属人成功且其他成员范围立即收窄；目标为租户默认时拒绝且默认指针与归属均不变；归属人非本租户有效成员时拒绝；重复提交同一目标幂等返回 `changed: false`。（规范全部 Scenario）
- [x] 3.2 先写红：断言共享后 `ObjectScope.allows_agent(action=manage)` 对原归属人为否、对其他成员为是，且既有默认解析（`resolved_default_agent_id`）不受转换影响。（规范「共享后本人不再持有维护资格」）
- [x] 3.3 先写红：新增投影用例断言 `visibility` / `can_share` / `can_unshare` 在「本人私有」「本人可达的共享」「他人私有（不出现于投影）」三类行上的取值。
- [x] 3.4 先写红：新增路由用例断言跨租户 `set_visibility` 返回 404，且响应体不含该对象的归属或存在性信息。
- [x] 3.5 先写红：新增前端用例断言两类徽标文案、`can_share`/`can_unshare` 为否时不渲染入口、恢复为私有未选择归属人时不提交。
- [x] 3.6 回归：确认 `tests/test_default_agent_tenant_shared.py`、`tests/test_private_agent_owner_reachability.py`、`tests/test_private_agent_lifecycle.py`、`tests/test_private_agent_quota.py`、`tests/test_management_restore_private_owner.py` 全绿——本 change 不改两个既有原语，这些夹具语义必须保持不变。
- [x] 3.7 全量前端回归（`node --test tests/*.cjs`）：与干净基线的失败集合对比，新增失败为 0。

## 4. 文档与验收

- [x] 4.1 在 `docs/design/role-resource-authorization-plan.md` 的「功能、资源、数据范围分别计算」一节补注：正式控制台已提供私有 ↔ 租户共享自助转换入口，由本 change 覆盖历史说法，并说明归档记录按规则保持原样。原任务文字指向该 doc 中的历史说明，实际该说法只存在于 `openspec/changes/archive/2026-09-13-add-user-personal-agent-provisioning/design.md`；按「不修改归档历史」规则不在归档内回写，改在当前 design doc 记录取代关系。
- [x] 4.2 复核并写明**已显式接受的口径**：共享会使该成员腾出一个私有配额名额（`member_agent_limit` / `tenant_agent_limit` 按 `private_owner_user_id` 计数），因此「创建 → 共享 → 再创建」可绕过该上限；本 change 不修改配额口径、也不为此新增创建者列，若需收紧须另起 change。已写入 `design.md` 的 Non-Goals 与 Risks。
- [x] 4.3 复核零数据面改动：`agent_bindings` 表结构与既有列语义无变化、无新增迁移函数、无回填；`git diff` 中 `auth/store.py` 不含表结构改动。
- [x] 4.4 写入 `evidence.md`：红/绿输出、既有原语回归对比、投影与路由用例输出、`openspec validate --strict` 输出。
- [ ] 4.5 端到端确认（需具备登录会话的人执行，本机 `localhost:9899`）：对一个「归属他人私有」的智能体转为共享后，被角色授权的成员确实能在智能体管理与工作台看到并进入对话；反向恢复为私有后该成员即时不再可见，且归属人恢复管理入口。
- [x] 4.6 `openspec validate show-and-toggle-agent-visibility --strict` 通过。
