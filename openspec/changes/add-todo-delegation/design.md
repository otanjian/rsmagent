## Context

动机见 `proposal.md`。这里只记录决定方案形状的现状与约束。需求以 `specs/todo-delegation/spec.md` 及三个 MODIFIED delta 为准，本节与 Decisions 不重复规范条文。

**现状接入点**

- `agent/todo/store.py`
  - `SCHEMA_VERSION = 1`；`_MIGRATIONS` 为「`user_version` → SQL 脚本」映射，启动时按序推进（约 162 行）。
  - `todo_items` 无处理人列；唯一约束为 `UNIQUE (scope_id, owner_id, create_key)`；索引为 `idx_todo_items_owner_status`、`idx_todo_items_owner_due`。
  - `todo_events` 含 `item_id`、`scope_id`、`owner_id`、`version`、`operator_id`、`operator_kind`、`action`、`changed`、`note`。
  - 全部读取入口以 `(scope_id, owner_id, …)` 为键：`get_item`(218)、`get_item_by_create_key`(229)、`count_open`(321)、`count_overdue`(333)。
- `agent/todo/service.py`
  - `TodoActor`(205) 携带 `scope_id` / `owner_id` / `permissions`；未绑定身份时 `bound=False` 且必须拒绝。
  - `resolve_todo_database_path`(163) 与 `memory_db_path`(183) **不把 scope/owner 编入路径**，恒为 `<数据根>/todo/todos.db`：即**同一租户共用一个库**，隔离靠行上的 `scope_id` / `owner_id` 列。委派因而是同库内的行级操作，不需要跨库访问。
  - 所有调用点以 `self.actor.scope_id, self.actor.owner_id` 取值（404、464、504、533、534、540、549、553、572、650、692）。
  - 事件主体判定为 `operator_kind="human" if operator == self.actor.owner_id else "agent"`（732、754）。
- `channel/web/todo_handlers.py`：`owner_id` 一律取自 `ctx.user_id`，不接受请求参数。
- `agent/tools/todo/todo_tool.py`：动作集为 create / list / get。
- 权限：`auth/policy.py` 的 `PERMISSION_CATALOG`(28)、`PERMISSION_METADATA`(64)、`MEMBER_DEFAULT_PERMISSIONS`(200)、`TENANT_ADMIN_DEFAULT_PERMISSIONS`(228)。
- 迁移：`auth/store.py` 的 `_migrations` 已到 30，`_migration_30`(1762) 是补授权限的范式——union 合并、幂等、`version+1`、自定义角色不动、无 menu 授权者不新开菜单。
- 菜单：`auth/service.py`(79) 的 `workbench.todos` 以 `todo.read` 把关、`scope` 为 `self`。

**决定方案的硬约束**

1. `owner_id` 必须保持不可变（既有规范明文要求），因此委派不能靠改写 `owner_id` 实现。
2. 成员默认权限集是**显式枚举**的，新增 catalogue 权限不会自动生效，必须显式加入默认集并回填既有租户。
3. `todo.assign` 的载体不能扩大成员目录授权：`tenant.members.read` 同时把关 `/api/tenant/members`、`/api/tenant/members/<id>/external-identities`（成员外部身份绑定）与 `/api/tenant/roles`，把它加进成员默认集会一次性放开这三者，并使 `tests/test_builtin_role_editing.py:52-54` 的既有断言失效。
4. 审计（`audit-log`）是委派留痕与跨租户告警的前置能力，须按能力名引用，不等整份消费者 change 完成。

## Goals / Non-Goals

**Goals:**

- 在单一租户库内完成行级委派，读路径不引入 join，并保持既有 `todo_events` 版本历史语义。
- 让「哪些人能看到/能改哪条待办」有一处可读的判定，而不是散落在各调用点。
- 迁移后**既有数据的可见行为与迁移前完全一致**，使上线可回滚且不产生数据分叉。
- 权限、成员投影、审计三者各自 fail-closed，任一缺失都不放行委派。

**Non-Goals:**

- 不改动 `todo_events` 表结构、不新增委派专用表。
- 不放开 `tenant.members.read`，不新建成员目录。
- 不引入委派通知、提醒、审批、时限、自动收回或批量委派。
- 不改动跨租户隔离、`scope_id` 语义与既有待办配额。
- 不为委派新增数据库分片或第二个库文件。

## Decisions

### D1 用 `todo_items.assignee_id` 单列承载处理权，而非独立委派表或复制行

备选与取舍：

- **独立 `todo_delegations` 表**：能记录更丰富的委派元数据（期限、状态、撤销原因），但每条读路径都要 join，且与 `todo_items.version` 的乐观并发形成双写一致性问题。本期所需元数据一个 `changed` 载荷即可承载，收益不足。
- **给接收人复制一条新行**：实现最省事，但接收人持有的是副本，「同一事项」身份丢失，收回与退回无法联动，且会产生两份互相漂移的完成状态。
- **单列 `assignee_id`（选定）**：读路径零 join；复用既有行级 `version` 与 `todo_events` 版本历史；因 `owner_id` 不变，`UNIQUE (scope_id, owner_id, create_key)` 的去重口径自然保持，不需要改动创建键语义。

### D2 读授权扩为「处理人 ∪ 委托人」，写授权收紧为「仅处理人」

这是本变更改动面最大的一处，不能只加过滤条件了事：

- 沿用 `owner_id` 作唯一授权键 → 接收人永远读不到被委派的事项，功能不成立。
- 把 `owner_id` 改写为接收人 → 同时破坏「所有者不可改」与创建键去重口径。
- 因此（选定）新增一组以处理人为键的访问方法，并把读判定集中为一条：`scope_id = :actor_scope AND (assignee_id = :actor OR owner_id = :actor)`。写判定为 `scope_id = :actor_scope AND assignee_id = :actor`。

`scope_id` 必须继续参与两个判定，且委派目标的解析必须限制在同一 `scope_id` 内——跨租户因此是硬拒而非过滤，符合既有跨租户口径。

服务层的 `(scope_id, owner_id, …)` 调用点需要逐个改为按用途选择「读判定」或「写判定」；`count_open` / `count_overdue` 改用处理人维度，直接决定角标与 summary 口径。

### D3 委派链沿用 `todo_events`，以新增 action 与 `changed` 载荷表达

新增 action `assign` / `recall` / `reject`，`changed` 记录 `{"from_assignee": …, "to_assignee": …}`。`todo_events.owner_id` 继续保存事项的委托人（稳定值），链按 `(item_id, version)` 顺序即可还原多跳。

`operator_kind` 的现有表达式 `operator == actor.owner_id ? "human" : "agent"` 在多人场景下仍然成立：operator 语义是**动作主体**而非所有者，人类委托人/接收人操作时 `operator_id` 等于当前 actor，判定为 `human`；后台或 Agent 代操作时传入的 operator 标识不等于 actor，判定为 `agent`。风险在于该表达式隐式依赖调用点传参，因此委派路径必须显式传 `operator_kind`，并补一条「Agent 代用户委派时事件主体为 agent」的测试。

### D4 新增 `todo.assign`，默认授予内置角色，但不触碰 `tenant.members.read`

- `todo.assign` 进入 `PERMISSION_CATALOG`、`PERMISSION_METADATA`（group「待办」）、`MEMBER_DEFAULT_PERMISSIONS` 与 `TENANT_ADMIN_DEFAULT_PERMISSIONS`。
- 新增 `_migration_31` 按 `_migration_30` 范式回填既有内置 `member` / `tenant_admin`：仅并集补入 `todo.assign`，幂等，`version+1`，自定义角色与未持有 menu 授权的角色不受影响。**只补 `todo.assign` 一项**。
- 接收人选择走受 `todo.assign` 控制的窄投影来源，只返回用户名与显示名。

备选「给成员默认加上 `tenant.members.read`」被否：如 Context 约束 3 所述，该权限的实际把关面远大于成员名单，且会推翻一条既有的显式安全断言。窄投影来源能以更小授权面满足同一功能目标。

### D5 Agent 工具新增 `assign`，接收人由服务端解析且必须由用户明确点名

- 接收人以**用户名**而非用户标识传入，由服务端在同一租户、同一 `scope_id` 内解析，沿用既有「不接受模型自报用户/租户/目录作为授权」的口径。
- 「用户本轮明确点名」是工具使用的前置条件，必须写进工具描述与校验；禁止扫描会话参与方、成员目录或历史推断接收人；单次调用至多一个接收人。
- 备选「放开 recall / reject」本期不做：收回与退回发生在人工界面，语义清晰且不需要模型判断，放开只会扩大误操作面。

### D6 迁移与分阶段门槛

数据迁移（`agent/todo/store.py`）：

1. `SCHEMA_VERSION` 1 → 2。
2. `ALTER TABLE todo_items ADD COLUMN assignee_id TEXT NOT NULL DEFAULT ''`（常量默认值，SQLite 允许）。
3. `UPDATE todo_items SET assignee_id = owner_id WHERE assignee_id = ''`（幂等，重跑无副作用）。
4. 新增索引 `(scope_id, assignee_id, status)`。

关键性质：回填后 `assignee_id = owner_id`，使 D2 的两条判定退化为旧行为，**既有数据在迁移后可见行为不变**。

启用门槛（按序，前一项未取得证据不进入下一项）：

1. `audit-log` 能力的切片验收通过——委派留痕与跨租户告警有真实落点。
2. 数据迁移在既有库上完成并通过回填幂等验证（含 `user_version` 未推进时的可重跑性）。
3. 权限补授后，无 `todo.assign` 的角色在页面与接口两侧均 fail-closed；`tests/test_builtin_role_editing.py` 的既有断言仍通过。
4. 读写授权扩权通过越权用例（委托人代为处理、跨租户、他人事项 ID）后，才开放前端入口与工具动作。

## Risks / Trade-offs

- [转出后事项从委托人列表消失，用户可能误判为被删除] → 「我委派的」分区固定展示，且转出即时反馈说明去向；`todo-workbench` delta 已把该分区列为规范要求。
- [接收人被动堆积任务，缺少拒收路径] → 提供退回动作并把退回入链留痕；本期不做通知，靠列表可见性。
- [`_migration_31` 让内置成员在升级后立即获得委派能力] → 这是选定「同租户任意成员」的预期结果；迁移只动内置角色、不动自定义角色，需要收紧的管理员可在自定义角色层收回。
- [`operator_kind` 被后续调用点误标为 human] → 委派路径显式传 `operator_kind` 并加断言测试，不依赖隐式比较。
- [读写判定散落导致越权漏判] → 把判定收敛为 D2 的两条集中表达式，并针对「委托人代为处理」「跨 scope」补回归用例。
- [新增索引影响写放大] → 仅增一个索引，且待办量级小、写入低频；不引入额外表。
- [回填与加列在旧库上失败] → 迁移在 `user_version` 推进前完成，失败则不推进版本，重跑幂等；加列与索引均为可重复安全操作。
- [回滚需求] → 行为回滚只需关闭前端入口与工具动作：此时 `assignee_id = owner_id`，服务层与旧行为等价，无需回退 schema。

## Migration Plan

1. **前置**：确认 `audit-log` 切片验收已通过（门槛 1）。
2. **数据层**：发布 `SCHEMA_VERSION` 2 迁移与索引；在既有库上验证加列、回填幂等与版本推进（门槛 2）。
3. **权限层**：发布 `todo.assign` 与 `_migration_31`；验证无该权限者在页面与接口两侧均不可委派，且既有权限断言不受影响（门槛 3）。
4. **服务与接口层**：发布 D2 的读写判定与窄投影来源；跑越权与非回归用例（门槛 4）。
5. **展现层**：开放前端委派入口与 Agent `assign` 动作。
6. **回滚**：关闭第 5 步入口即回到旧行为；数据层保留新列与索引，不做破坏性回退。

## Open Questions

- 「我委派的」分区的分页与排序是否与「我的待办」完全一致（实现参数，不影响规范）。
- 委派动作是否需要沿用既有待办配额或额外限流（安全加固项，可后置）。
- 委派发生时是否需要在接收人侧产生一次可见的未读提示（本期 Non-Goal，需要时可另起 change）。
