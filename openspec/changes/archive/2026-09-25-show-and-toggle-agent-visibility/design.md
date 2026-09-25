## Context

动机见 `proposal.md`。与实现有关的现状与约束：

- **判定唯一来源**：`auth/object_scope.py` 的 `ObjectScope.allows_agent` 是「某个智能体是否在调用者范围内」的唯一权威。私有分支由归属决定（`owner == self.user_id`，先于任何管理员例外）；共享分支要求 `is_admin`（action 为 `manage` 时）。`auth/service.py` 的 `check_resource_action` 对 `agent` 有同构的私有短路。
- **既有服务原语**：`IdentityService.make_agent_tenant_shared`（清空 `private_owner_user_id`、审计 `agent.make_tenant_shared`）与 `IdentityService.restore_private_agent_owner`（写回归属、审计 `agent.restore_private_owner`）。二者的门禁都是 `_require_tenant_admin`；后者已实现本次需要的大部分反向规则：只修「无归属」对象、拒绝租户默认、要求归属人是本租户有效成员、`dry_run` 与幂等。
- **既有测试依赖原语门禁**：约 10 个测试文件故意用「非归属人的管理员」调用 `make_agent_tenant_shared`，用来验证「共享后门禁立即生效」。因此原语的门禁语义不能改动。
- **既有挂载点**：`channel/web/fork/handlers/agents.py` 的 `AgentsHandler.POST` 已是对象状态转换动作的集合（`create` / `update` / `archive` / `delete` / `set_default` / `set_user_default` / `set_knowledge_mode`）；`_tenant_agents_admin_projection` 是管理页每行的投影来源；`_require_tenant_agent_binding` 是既有的跨租户 404 守卫。
- **`get_agent_binding` 使用 `SELECT *`**，绑定行的新读字段无需改仓储层。
- **成员列表已存在**：`GET /api/tenant/members`（`tenant.members.read`），可用于为「恢复为私有」选择归属人，无需新增查询管道。

## Goals / Non-Goals

**Goals:**

- 让管理页能直接看到每个智能体是私有还是租户共享，且标识与授权判定同源。
- 给归属人一个自助的「转为租户共享」入口，给租户管理员一个「恢复为私有并指定归属人」入口。
- 零数据面改动：不加列、不加迁移、不改变任何既有授权判定与既有服务原语的门禁。
- 界面只提供后端会接受的动作。

**Non-Goals:**

- 不引入「共享后原归属人保留维护权」。共享即交出独占，维护权转由租户管理员承担（已显式确认）。
- 不在智能体管理页为租户管理员增加「他人私有对象」的治理元数据行；他人私有对象仍不出现在该页。
- 不修改私有配额口径，也不为堵配额绕过而新增创建者列。
- 不新增平台跨租户控制台入口；平台侧仍走既有 `cow management` CLI。
- 不在工作台/聊天卡片上增加可见性徽标（本次范围是智能体管理页）。

## Decisions

### D1 共享沿用 `private_owner_user_id` 为空的口径，不新增可见性列

该字段已经是全部读路径（`object_scope`、`check_resource_action`、`_private_agent_owned_by_another`、默认解析、配额）与 20 余个测试文件的唯一事实源。引入独立的可见性维度需要同时改写这些读路径，收益仅是命名更整齐，而漏改一处就是可见性漏洞。

**备选（已评估并明确放弃）**：新增 `agent_bindings.creator_user_id`「创建者/原归属人」。它能同时支持「共享后原归属人保留维护权」与「反向可预填归属人」，代价是加列 + 回填迁移 + 配额计数口径改动 + `object_scope` / `check_resource_action` / `resource_ids_for` 三处判定改动。已向用户说明代价并获得确认，选择放弃该能力以换取零数据面改动。

### D2 新增独立服务方法，既有两个原语不改动

新增 `IdentityService.set_agent_visibility(*, agent_id, actor_user_id, visibility, owner_user_id=None)`：

- **`visibility="tenant"`（私有 → 共享）**：要求调用者满足 `binding["private_owner_user_id"] == actor_user_id`，或持有本租户控制资格（`_is_control`）。写入复用 `make_agent_tenant_shared` 的语义（清空归属 + 审计 `agent.make_tenant_shared`），已共享时幂等返回 `changed: false`。
- **`visibility="private"`（共享 → 私有）**：要求 `_require_tenant_admin(actor_user_id, tenant_id)`，且必须显式给出 `owner_user_id`；校验复用 `restore_private_agent_owner` 的规则——目标为租户默认时拒绝（409 `agent_is_tenant_default`），归属人必须是本租户有效成员。
- 跨租户由路由层 `_require_tenant_agent_binding` 先按 404 挡掉，服务方法不承担该职责。

**为什么把资格判断放在新方法里**：`restore_private_agent_owner` 的窄化语义（只修无归属对象、拒绝租户默认、要求本租户成员）正好等于本次的反向需求，`make_agent_tenant_shared` 的写入正好等于正向需求；但两者的门禁都被既有测试当作「管理员或 repair」夹具使用。把「归属人自助」表达在新方法中，可以同时复用写入语义与保持既有夹具不变。

**备选**：把归属人资格直接写进原语门禁。放弃原因是它会改变约 10 个既有测试的夹具语义，并让一个「运维修复」原语同时承担「用户自助」语义。

### D3 用一个 HTTP 动作表达转换，而不是两个

`POST /api/agents` 增加 `action: "set_visibility"`，请求体 `{id, visibility: "private"|"tenant", owner_user_id?}`。与同为对象状态转换的 `set_knowledge_mode` 同形，避免为同一件事引入两个动作与两套错误码。

### D4 资格由服务端投影给出，界面不推断

`_tenant_agents_admin_projection` 每行新增 `visibility`（`private` / `tenant`）、`can_share`、`can_unshare`，与既有 `can_write_knowledge`、`tenant_default_manageable` 同一模式。这样界面不会渲染出一个请求必定被 403 的按钮。

### D5 「共享后从归属人的管理页消失」不写新逻辑

它是既有判定 `allows_agent(action=manage)` 的直接结果（共享对象要求管理员）。本次不修改该判定，只把它写进规范并在界面提交前明确告知。

### D6 转换不参与 roster 乐观并发，也不触发运行时重载

两个方向写入的都是 `identity.db` 的绑定行，不是 `team.json`，因此不参与 roster `revision` 校验，也不应调用 `_reload_agent_runtime`（该调用只服务 roster 变更）。写事务走既有 `_tx()`（`BEGIN IMMEDIATE`）。

## Risks / Trade-offs

- **归属人共享后失去管理入口** → 界面在提交前明确提示后果；恢复路径是管理员在控制台指定归属人（本 change 提供），或既有的 `cow management restore-private-owner`。已确认接受。
- **共享会释放一个私有配额名额**，`member_agent_limit` 可被「创建 → 共享 → 再创建」绕过 → 本次显式接受并把该口径写进规范，不新增创建者列。若后续需要收紧，应作为独立 change 引入创建者计数口径，而不是在本 change 内夹带。
- **「共享」被误读为「只授权给某个人」** → 徽标与提示文案明确写「本租户全部成员可见可用」，并在共享按钮旁说明这是公开动作。
- **管理员替他人恢复私有需要选择归属人** → 复用既有 `/api/tenant/members` 列表，不新增成员查询管道。
- **存量已共享对象没有创建者记录** → 本次不依赖创建者（反向由管理员显式指定归属人），因此存量对象无需回填，也不需要迁移。

## Migration Plan

- **无数据迁移**：`agent_bindings` 表结构不变，无回填、无清空。既有租户的私有/共享分布原样保留。
- **部署顺序无约束**：新增投影字段与新增动作对旧客户端是附加项，不改变既有响应结构与既有动作语义。
- **回滚**：前端撤下动作入口、服务端移除 `set_visibility` 分支即可。数据面无需回退——共享是既有的合法状态，且 `restore_private_agent_owner` 与 `cow management restore-private-owner` 始终可用于反向修复。
