## Why

控制台既无法回答「这个智能体是私有的还是租户共享的」，也没有把私有对象转为租户共享的入口。实测：某智能体的绑定归属于成员 A（`agent_bindings.private_owner_user_id` 非空），管理员在角色权限页把该智能体的 `read`/`use`/`edit`/`enable` 全部授予成员 B，B 仍在智能体管理页和聊天里看不到它——私有对象的可见性只由归属决定，判定先于角色资源授权。要让它对 B 可见，唯一办法是清空归属使其成为租户共享，而当前唯一手段是运维 CLI 或直接改库：控制台既看不到状态，也做不了这件事。管理员因此会把「归属为私有」误判为「授权没保存」。

## What Changes

- 智能体管理页 SHALL 在列表行与详情头显示每个智能体的可见性：`私有` / `租户共享`（与既有「默认」徽标并列）。
- 详情面 SHALL 提供可见性转换动作：
  - **私有 → 租户共享**：由该对象的**归属人本人**（或本租户管理员）发起；成功后该对象对本租户全部成员可见可用。
  - **租户共享 → 私有**：由**本租户管理员**发起，并显式指定归属人（从既有成员列表选择）；目标正被当作租户默认时拒绝，要求先移走默认指针。
- 服务端 SHALL 在智能体管理投影中按行返回 `visibility` 与 `can_share` / `can_unshare`；界面据此启用或禁用动作，MUST NOT 由前端自行推断资格。
- 共享的口径 SHALL 沿用既有语义：`private_owner_user_id` 非空即私有独占，为空即租户共享。本次 **不新增列、不做数据迁移、不改动任何既有授权判定**。
- 归属人共享后 SHALL 不再处于该对象的管理范围与维护权内（该对象自此由租户管理员维护），但 SHALL 仍可在工作台与聊天中继续使用它。因此界面 SHALL 在共享前给出明确的后果提示。
- 私有配额继续按 `private_owner_user_id` 计数，共享会使该成员腾出一个私有名额。此为**已显式接受的已知口径**，本次不修改，也不因此新增列。

## Capabilities

### New Capabilities

（无。）

### Modified Capabilities

- `user-private-agent-management`: 新增「可见性展示与私有↔共享转换」requirement；并在「所有者可以维护和启停本人私有智能体」中明确：对象被转为共享后不再属于本人私有维护范围。

## Impact

- **服务端新增**：
  - `auth/service.py` 新增 `IdentityService.set_agent_visibility(...)`：共享方向要求调用者是该绑定的归属人（或本租户管理员），恢复方向要求调用者是本租户管理员并显式指定归属人。写入复用既有 `make_agent_tenant_shared` 与 `restore_private_agent_owner` 的语义与审计动作，**两个既有原语本身不改动**。
  - `channel/web/fork/handlers/agents.py` 的 `AgentsHandler.POST` 新增 `action: "set_visibility"`，经既有 `_require_tenant_agent_binding` 先挡跨租户（404）。
  - `_tenant_agents_admin_projection` 每行新增 `visibility`、`can_share`、`can_unshare`。
- **前端**：`channel/web/static/js/console.js` 列表行与详情头徽标、详情面转换动作与后果提示；i18n 在 `channel/web/static/js/i18n/agents.js` 补齐三语，并同步 `tests/fixtures/console_i18n_snapshot.json`。
- **明确不改动**：`make_agent_tenant_shared` / `restore_private_agent_owner` 的门禁与审计语义、`auth/object_scope.py` 的对象范围判定、`check_resource_action` / `resource_ids_for`、`agent_bindings` 表结构与私有配额口径。
- **测试**：新增服务端可见性转换（含权限拒绝、租户默认拒绝、跨租户拒绝）与投影字段用例；新增前端徽标与动作可见性用例。
