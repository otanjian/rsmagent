## Context

`agent_bindings`（`auth/store.py` 迁移 1/8/17）是"哪个租户能看到哪个智能体"的唯一权威；成员可见性、默认解析与运行装配都从这里出发。roster（`config.json` 的 `agents[]`，由 `AgentAdminService` 维护）保存行为与配置。两者在读取时汇合，`IdentityService.bind_agent`（`auth/service.py`）是"使智能体对租户可见"的那一次写入，且已与 `_audit_in_tx` 同事务。

本 change 的发布动作选择落在这一接缝旁边：发布是一个新的、受治理的写入，它把 roster 与有效连接分配的内容固定为一个版本，并决定该绑定是否对成员可见。

## Goals / Non-Goals

- **Goal**：让"对成员可用"成为显式、经审批、可审计、版本化、可回滚且能力面冻结的发布；成员运行只取已发布版本；默认关闭、平滑迁移。
- **Non-Goal**：不改私有智能体与外部 coding 的既有行为；不新建审计/审批/配额/授权体系；不做跨租户发布；不引入智能体市场、灰度发布、A/B 或按用户差异发布；不重写历史发布。

## Decisions

### D1. 版本化不可变发布，而非"启用即上线"

发布固定一份**只读快照**，内容哈希（规范序列化）作为版本摘要；后续编辑形成草稿。理由：没有不可变版本就无法可靠回滚，也无法把审批摘要绑定到"将被发布的到底是什么"。已在 `user-private-agent-management`、`agent-capability-bindings` 中确认不存在"冻结/快照"概念，故本能力新增，而不是改写。

### D2. 复用既有治理，不自建

- 审批：`action-approval` 引擎与 `agent/approval_gate.py` 的"适用范围由部署声明、摘要绑定动作/目标/参数、一次性消费、每个拒绝码可区分"的既有语义。发布动作以 `agent.release` / `agent.release.rollback` / `agent.release.suspend` 接入。
- 审计：`IdentityService._audit_in_tx`，与状态变更同事务；沿用 `audit-log` 的 append-only 与脱敏要求。
- 配额/企业启用：沿用 `resource-quota` 与 `enterprise-access-enforcement`；判定失败默认拒绝（与既有 `_quota_fail_open` 的默认相反侧，见 D5）。
- 授权：沿用 `rbac-authorization` 的管理资格与服务端统一授权顺序。
- 运行时裁剪：沿用 `agent-runtime-capability-enforcement`，只把来源从"当前配置"换成"已发布快照"。

### D3. 发布状态放在 `agent_bindings`，版本历史放 `agent_releases`

发布是绑定的属性（同一 `agent_id` 对唯一租户），故状态列随绑定；版本历史单独成表以保留追溯与回滚。`agent_releases` 保存 `version`、规范快照 JSON、`content_hash`、`released_by_user_id`、`approval_id`、`created_at`。绑定记录当前 `release_state`、`released_version`、`released_at`、`released_by_user_id`、`released_approval_id`、`released_snapshot_hash`。

### D4. 迁移：既有绑定按当前内容登记为已发布

升级开启时，把每个既有租户共享绑定以其当前有效配置的哈希登记为 `version=1` 且 `release_state=released`，使成员可见面与升级前完全一致；私有绑定不参与。迁移幂等。非 database 身份模式不启用。

### D5. 配额/企业启用判定失败默认拒绝

发布是打开成员可见面的动作，因此判定不可用（配额计量不可读、企业状态无法解析、存储异常）时默认拒绝，而不是放行；这与 `resource-quota` 的"无法判定即默认拒绝"一致，且不得被管理员资格绕过。

### D6. 默认关闭的开关

`shared_agent_release_governance`（`config.py`）默认关闭：关闭时成员可见性、默认解析与运行装配与引入前逐字节一致，且不把既有绑定置为草稿。开关运行期关闭只停止强制新发布，不删除历史、不改写成员可见面。

### D7. 运行时来源切换是"真裁剪"，不是"提示"

发布治理启用时，`agent-runtime-capability-enforcement` 的装配以已发布快照为唯一来源：快照未包含的工具/连接/技能对成员不可用；发布状态复核失败 fail-closed，不回退草稿或全局默认。这避免了"草稿改了工具但对成员仍然生效"的泄漏。

## Risks / Trade-offs

- **内容哈希稳定性**：快照需规范序列化（排序、去空白、去掉非语义字段），否则"未变更"也会被判定为新版本。设计取"规范 JSON + 明确纳入/排除字段清单"。
- **草稿/已发布的双写复杂度**：roster 仍是当前配置的存储，快照是发布时的投影。实现必须保证成员运行读快照、管理界面读草稿，二者的字段口径一致。
- **渠道绑定纳入快照**：渠道实例（`tenant_channel_instances`）是租户级对象，快照只固定"该智能体在发布时被哪些渠道实例引用"，不复制实例本身；实例之后被改指不在本快照范围内，需在实现与文档中说明边界。
- **回滚语义**：回滚 = 用历史版本内容作为新的当前发布，历史版本保留；因此"版本号单调递增、内容可重复"。这避免重写历史与审计断链。

## Migration Plan

1. 迁移新增 `agent_releases` 表与 `agent_bindings` 发布列，默认 `release_state=NULL`/未发布，保证开关关闭时零影响。
2. 启用开关时执行幂等回填：既有租户共享绑定 → `released` 版本 1（当前内容哈希）。
3. 回退：关开关即回到既有行为；发布历史保留，不删列（SQLite 迁移实践）。

## Open Questions

- 回滚资格是否要求与发布相同的资格，还是允许"发布者之外的第二人"（设计暂取相同资格 + 审批职责分离）。
- 是否允许对**已停用**的智能体发布（当前设计：不允许，停用状态下发布无意义）。
- 版本保留上限（暂定保留全部，后续如需再引入）。

## Dependencies

- 上游能力：`action-approval`、`audit-log`、`resource-quota`、`enterprise-access-enforcement`、`rbac-authorization`、`agent-runtime-capability-enforcement`、`agent-capability-bindings`、`tenant-resource-isolation`、`user-private-agent-management`。
- 实现接缝：`auth/service.py`（`bind_agent` / `set_agent_visibility` / `resolve_default_agent` / `_agent_is_usable`）、`auth/store.py`（迁移）、`auth/audit.py`、`agent/approval_gate.py`、`agent/admin.py`、`agent/registry.py`、`integrations/external/service.py`、`channel/web/fork/handlers/agents.py`、`channel/web/route_registry.py`、`config.py`。
