## Why

租户共享智能体（tenant-shared Agent）是成员每天进入对话的入口，但当前"对成员可用"只是创建、绑定（`agent_bindings`）与 `enabled` 开关的副作用：没有"发布"这一显式动作，没有版本，没有审批，改一次配置就对全体成员立即生效。这带来三个真实风险：

- **改动即上线**：一次误编辑（模型、工具、外部连接、渠道绑定、人设）可以直接改变所有成员的运行面，且无法回滚到上一个可用状态。
- **无审批、无版本**：高风险变更没有第二人复核，也没有"这一版是谁、依据哪次批准、发布了什么内容"的可追溯记录。
- **能力面漂移**：成员实际可用的工具/MCP/技能取决于"此刻的配置"，而不是某个经过验收的快照，事后无法复现。

本 change 为共享智能体引入**发布治理**：把"对成员可用"变成一个显式、经审批、可审计、版本化、可回滚且冻结能力面的动作，并让成员的运行只使用已发布版本。治理本身不新建审计/审批/配额/授权体系，全部复用既有能力。

## What Changes

- **发布状态与不可变版本**：为租户共享绑定引入发布状态（`draft` / `released` / `suspended`）与已发布版本；每次发布固定一份不可变快照（人设与配置属性、模型、技能/知识/SOP 绑定、工具白名单与黑名单、有效外部连接分配、渠道绑定）。已发布版本只读，后续编辑形成草稿，成员运行不受草稿影响。
- **成员只使用已发布版本**：本能力启用时，成员列表、租户/成员默认解析、会话启动与运行时工具/能力装配只采用已发布版本；草稿、停用、从未发布 MUST NOT 对成员可见或可用。
- **发布/回滚/停用经单动作审批**：复用 `action-approval` 引擎与消费点，审批绑定租户、发起人、动作身份、目标智能体与候选版本摘要；职责分离（不得自审批），未批准/被拒/被撤/过期/摘要不匹配均不产生副作用。
- **发布受企业启用与配额约束且 fail-closed**：发布只在租户企业能力启用且资源配额条件满足时成功；判定失败默认拒绝；管理员资格不豁免租户配额。
- **全程审计**：发布、草稿提交、回滚、停用与被拒尝试在同一事务内写脱敏审计，含主体、租户、目标、动作、版本与批准标识，不含秘密。
- **回滚即新发布、停用不删数据**：回滚以历史版本的副本作为新的当前发布，不重写/删除历史；回滚与停用在成员下一次请求前生效，停用不删除智能体、工作区、会话或记忆。
- **默认关闭的部署开关 + 平滑迁移**：由 `shared_agent_release_governance` 控制，默认关闭；关闭时行为与引入前一致。开启升级时，既有租户共享绑定以其当前内容哈希登记为已发布版本，不产生成员可见变化；非 database 身份模式不启用。

## Capabilities

### New Capabilities

- `shared-agent-release-governance`：共享智能体发布的显式状态机、不可变版本、审批/配额/企业启用门槛、审计、回滚与停用、运行时只取已发布版本，以及默认关闭的启用开关与升级迁移。

### Modified Capabilities

- `action-approval`：新增智能体发布治理动作类（`agent.release` / `agent.release.rollback` / `agent.release.suspend`）及其适用性与消费语义；沿用既有引擎与审计。
- `agent-runtime-capability-enforcement`：发布治理启用时，成员会话的员工上下文、工具白/黑名单、技能/知识绑定与外部连接分配以**已发布版本快照**为唯一来源，草稿不得泄漏到成员运行。
- `tenant-resource-isolation`：默认智能体解析与租户智能体列表在发布治理启用时只采用已发布且启用的绑定，MUST NOT 以草稿或停用绑定兜底。
- `user-private-agent-management`：`私有 ↔ 租户共享` 转换与发布状态的衔接——转为租户共享后需经发布才对成员可用，转回私有即对成员不可用。

## Impact

- **身份/存储**：新增 `agent_releases` 表，并在 `agent_bindings` 增加发布状态与已发布版本列（`auth/store.py` 迁移）；发布写入与审计同事务（`IdentityService._audit_in_tx`）。
- **服务层**：`IdentityService`（`auth/service.py`，绑定/可见性/默认解析 `bind_agent` / `set_agent_visibility` / `resolve_default_agent` / `_agent_is_usable`）新增发布原语；发布候选快照从 roster（`agent/admin.py`、`agent/registry.py`）与外部连接分配（`integrations/external/service.py`）汇总。
- **审批/配额/企业启用**：复用 `agent/approval_gate.py` 与 `IdentityService.request_approval/consume_action_approval`、`consume_quota`、企业启用判定；不新建额度或审批表。
- **HTTP/控制台**：`channel/web/fork/handlers/agents.py` 增加发布相关动作，`channel/web/route_registry.py` 复用既有受控路由与 `require_management_write`；Web 控制台与 Desktop 只展示已发布/草稿状态与回滚入口。
- **配置**：`config.py` 增加 `shared_agent_release_governance`（默认关闭）。
- **不改变**：私有智能体、外部 coding 智能体、既有审计/审批/配额/授权规则；不新增凭据、不迁移成员可见面、不改渠道实例与工作区归属。
