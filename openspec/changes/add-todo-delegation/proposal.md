## Why

待办目前只承载「本人事项」：`todo-management` 明文禁止指派与转交，`todo-conversation-integration` 把 Agent 工具限制在 create/list/get，`todo-workbench` 禁止页面出现成员选择入口。这三条限制是有意为之的第一期边界，但真实协作里「把这件事交给别人跟进」是最高频诉求之一，当前的替代做法（口头交代后再各建一条）导致责任主体不清、交接过程无法追溯。

现在具备放开的前置条件：委派链可以完全落在既有 `todo_events` 版本历史上，不需要新建责任域；审计能力（`audit-log`）已能承载委派动作的留痕与跨租户尝试告警。

## What Changes

- **所有权与处理权分离**：`owner_id` 保持为「委托人」且**不可变**（沿用既有「所有者不可通过接口修改」），新增 `assignee_id` 表示「当前处理人」，默认等于 `owner_id`。委派只移动 `assignee_id`，不动 `owner_id`。
- **委派动作**：指派（把本人待办交给他人）、转交（接收人再交给第三人，形成可追溯链）、收回（委托人取回）、退回（接收人退还给委托人）。四者均只允许在 `pending` 状态发生，均不改动 `status` 与 `due_at`。
- **可见性变化（用户可见行为变更）**：委派生效后该事项从委托人的「我的待办」列表消失，转入其「我委派的」分区；接收人成为唯一可完成/取消的行为主体。
- **新增权限 `todo.assign`**：控制所有委派动作。选定「同租户任意成员之间可互相委派」，故默认授予内置 `member` 与 `tenant_admin`，并需一次性回填已开通租户。
- **新增窄目录接口**：为接收人选择提供同租户可委派成员的最小投影（用户名 + 显示名），**不**扩大 `tenant.members.read` 的授权面。
- **Agent 工具扩展**：`todo` 工具新增 `assign` 动作，接收人只能由用户在本轮明确点名，禁止扫描或推断；单次调用最多 1 个接收人。`recall` / `reject` 本期仍只在人工界面。
- **数据迁移**：既有待办回填 `assignee_id = owner_id`，迁移后行为与现状一致。

## Capabilities

### New Capabilities

- `todo-delegation`: 待办委派能力——所有权（`owner_id`，不可变）与处理权（`assignee_id`）的分离模型、四个委派动作的状态与授权边界、同租户可委派成员的最小投影、委派链条目，以及委派动作的审计要求。

### Modified Capabilities

- `todo-management`: 「个人范围与可信身份仅真实账号主体」中「本期不提供指派、转交、创建人旁观」的限制需放开为「在委派关系内允许委托人读取与收回」，同时保留禁止管理员全量范围、禁止匿名主体、禁止默认 Agent 回退的既有约束；并新增 `assignee_id` 可经委派接口修改（而 `owner_id` 仍不可修改）的边界。
- `todo-conversation-integration`: Agent 工具的能力集从 create/list/get 扩展为包含 `assign`，并把「用户本轮明确点名接收人」列为工具使用的前置条件。
- `todo-workbench`: 放开页面上的成员选择与转交/收回入口，保留「不提供租户全量范围、定时生成、自动提醒」的禁令。

## Impact

**权限与身份**
- `auth/policy.py`：`PERMISSION_CATALOG`、`PERMISSION_METADATA`、`MEMBER_DEFAULT_PERMISSIONS`、`TENANT_ADMIN_DEFAULT_PERMISSIONS` 新增 `todo.assign`。
- `auth/store.py`：新增 `_migration_31`，对既有内置 `member` / `tenant_admin` 角色做 union 式补授（幂等、`version+1`、自定义角色不动）。补授范围**仅** `todo.assign`；`tenant.members.read` 保持不授予成员，`tests/test_builtin_role_editing.py` 的既有断言因此不受影响。

**数据**
- `agent/todo/store.py`：`SCHEMA_VERSION` 1 → 2，新增 `assignee_id` 列与 `(scope_id, assignee_id, status)` 索引，回填 `assignee_id = owner_id`。`todo_events` 结构不变，委派链以新增 action `assign` / `recall` / `reject` 记录。

**服务与接口**
- `agent/todo/service.py`：读授权基准扩为「`assignee_id = 我` 或 `owner_id = 我`」；写授权仍限 `assignee_id = 我`；新增委派方法与 `todo.assign` 校验。
- `channel/web/todo_handlers.py`、`channel/web/route_registry.py`：新增委派动作路由与窄目录路由。

**Agent 与前端**
- `agent/tools/todo/todo_tool.py`：新增 `assign` 动作，接收人以用户名给出并由服务端在同租户内解析。
- `channel/web/static/js/todos.js`、`channel/web/static/js/i18n/todos.js`：新增「我委派的」分区、接收人选择与委派来源展示。

**前置与依赖**
- 委派动作的留痕与跨租户尝试告警以 `audit-log` 能力为前置。
- 跨租户委派一律硬拒，不在本变更内放开；平台/租户管理员的既有授权语义不变，管理员仍不获得他人待办的访问权。

**未纳入本变更**
- 租户全量范围的代看待办、委派通知与提醒、委派审批流、批量委派。
