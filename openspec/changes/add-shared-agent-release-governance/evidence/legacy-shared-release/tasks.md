## 1. 基线与接入核实

- [x] 1.1 核对当前"对成员可用"的真实路径：`IdentityService.bind_agent`、`set_agent_visibility`、`appoint_tenant_default_agent`、`AgentAdminService.update_agent`/`set_enabled`，以及成员列表/默认解析/运行装配的实际读取点；确认发布状态插入位置与既有读取点的一一对应，缺口在本 change 内修补，不另建第二套可见性真相。
  - 落点：`resolve_default_agent` / `resolved_public_default_agent_id`、`channel/web/fork/handlers/agents.py:_iter_tenant_agents`（USE 范围）、`agent/effective_capabilities.resolve_effective_capabilities`。可见性真相仍是 `agent_bindings`，本 change 只加发布状态列。
- [x] 1.2 核对既有审批消费点与摘要语义（`agent/approval_gate.py` 的 `approval_decision` / `request_digest` / 消费顺序），确认 `agent.release` 系列动作可作为服务端声明的动作类接入，且不发散为新的审批存储。
  - `auth/agent_release.release_digest` 复用 `request_digest`，动作类为 `agent.release` / `agent.release.rollback` / `agent.release.suspend`，消费走 `consume_action_approval`，无新审批表。
- [x] 1.3 核对审计同事务写入（`_audit_in_tx` / `audit_events` append-only）、配额判定（`consume_quota` / 企业启用）与 fail-closed 语义，确认发布可复用且失败可回滚。
  - 发布/回滚/停用的版本行、绑定指针与审计在同一 `_tx` 内提交；配额不可判定即拒绝（`_release_quota_ok`）。
- [x] 1.4 核对 roster 与外部连接分配的字段口径（`AgentProfile`、`external_connection_agent_assignments`、渠道实例引用），冻结发布快照的纳入/排除字段清单。
  - 快照含 `profile` 白名单字段、`effective`（含 scene 合并后的有效能力）、`connections`（`external_connection_agent_assignments.logical_connection_id`）、`channels`（`tenant_channel_instances.id` where active）。

## 2. 发布状态、版本与迁移

- [x] 2.1 新增 `agent_releases` 表（agent/tenant、version、规范快照 JSON、content_hash、released_by、approval_id、created_at）与 `agent_bindings` 发布列（release_state、released_version、released_at、released_by_user_id、released_approval_id、released_snapshot_hash），迁移幂等且开关关闭时零影响。
  - `auth/store.py:_migration_48`。
- [x] 2.2 实现规范快照构建与内容哈希（稳定序列化、字段白名单），覆盖 1.4 的纳入字段；对同一有效配置重复计算得到相同哈希。
  - `auth/agent_release.build_snapshot` / `snapshot_hash`（排序+固定分隔符）；测试 `ReleaseSnapshotHashTest`。
- [x] 2.3 实现开启开关时的幂等回填：既有租户共享绑定以当前内容登记为已发布版本 1，成员可见面不变；私有绑定不参与；非 database 模式不启用。
  - `IdentityService.backfill_agent_releases`；测试 `test_backfill_registers_shared_bindings_only` / `test_backfill_is_noop_when_switch_off`。

## 3. 发布服务、审批、审计与配额

- [x] 3.1 实现发布原语：`release_agent`（构造候选→消费批准→企业/配额门槛→同事务写版本+更新绑定+审计）、`rollback_agent_release`（历史版本内容作为新版本）、`suspend_agent_release`、`list_agent_releases`、`agent_release_status`、`released_snapshot`。
- [x] 3.2 接入单动作审批：`agent.release` / `agent.release.rollback` / `agent.release.suspend` 的服务端适用性与一次性消费，摘要绑定候选内容哈希；职责分离（不得自审批）。
  - `request_agent_release` 计算哈希并绑定 digest；`decide_approval` 的自审批拒绝复用既有引擎。
- [x] 3.3 接入企业启用与资源配额门槛，判定失败默认拒绝；管理员资格不豁免；拒绝不改变已发布版本与历史。
  - `_release_enterprise_ready`（active 租户；identity 仅 database）+ `_release_quota_ok`（不可判定即拒绝）；测试 `test_enterprise_disabled_refuses_publish` / `test_quota_exhaustion_refuses_publish`。
- [x] 3.4 发布/草稿提交/回滚/停用/拒绝全部同事务写脱敏审计，含主体、租户、目标、动作、版本、批准标识，不含秘密；审计失败回滚状态变更。
  - `_write_release` / `suspend_agent_release` 内 `_audit_in_tx`（`agent.release` / `agent.release.rollback` / `agent.release.suspend` / `agent.release.backfill`）。

## 4. HTTP 接口与授权

- [x] 4.1 在 `channel/web/fork/handlers/agents.py` 增加发布相关动作（`request_release`、`release`、`rollback_release`、`suspend_release`、`list_releases`），沿用来源/CSRF 与服务端统一授权顺序。
  - 与 `set_visibility` 同样早返回（身份库写入、非 roster 编辑），复用 `_db_scope` 与 `IdentityServiceError` → `_error` 的机器可读拒绝。
- [x] 4.2 复用 `rbac-authorization` 的发布治理资格（租户管理员 / 平台管理员）与跨租户 404 语义；普通成员、客户端字段或菜单隐藏都不得放行。
  - `_require_release_control`（`_is_control`）+ handler 侧 `_agent_bound_to_tenant` 404；测试 `test_member_cannot_request_a_release` / `test_an_unknown_agent_is_not_found` / `test_member_cannot_release`。
- [x] 4.3 暴露草稿/已发布状态与版本历史给有资格的管理界面，只读展示不含秘密；成员侧只反映已发布状态。
  - `list_releases` 返回 `release_status` + 版本历史；管理投影在开关开启时附 `release` 字段；成员列表只反映已发布。

## 5. 运行时与可见性强制

- [x] 5.1 成员列表与租户/成员默认解析在开关启用时只采用已发布且启用的绑定；空集合确定性回落或拒绝，不采用草稿或全局默认。
  - `_release_binding_visible` 接入 `resolve_default_agent` / `resolved_public_default_agent_id`，`_iter_tenant_agents` 在 USE 范围过滤。
- [x] 5.2 运行装配（员工上下文、工具白/黑名单、技能/知识、外部连接分配）在开关启用时以已发布快照为唯一来源；草稿能力不注册/不派发；已移除能力不因缓存或旧会话继续生效。
  - `agent/effective_capabilities.resolve_effective_capabilities` 用已发布 `effective` 覆盖草稿（含 scene 合并结果）。
- [x] 5.3 运行前发布状态复核失败 fail-closed；回滚与停用在成员下一次请求前生效，排队任务触发前重新校验。
  - `released_capability_overrides` 对"共享但未发布/快照缺失/读取失败"抛 `ReleasedSurfaceUnavailable`，解析器转为拒绝全部工具；停用后下一次调用即拒。
- [x] 5.4 `私有 ↔ 租户共享` 转换衔接发布状态：转共享→草稿（发布前成员不可见），转私有→立即可见性移除、历史保留、绑定不变。
  - `make_agent_tenant_shared` 与 `set_agent_visibility` 的共享写入在开关开启时置 `release_state='draft'`；测试 `test_sharing_forms_a_draft_and_unsharing_keeps_history`。

## 6. 控制台与文档

- [ ] 6.1 Web 控制台发布/回滚/停用入口与版本历史，显示草稿与已发布差异，提交前提示影响范围；Desktop 保持契约兼容。
  - 后端契约已就绪（`/api/agents` 的发布动作 + 管理投影 `release` 字段 + `list_releases`）。`channel/web/static/js/console.js` 为 2 万行级前端且受 i18n 快照测试约束，发布/回滚界面与审批列表的联调作为紧随其后的前端切片交付，避免在无可视化验证的前端大改中回归。
- [x] 6.2 更新使用手册与设计说明（发布流程、审批、回滚、开关、版本保留与渠道绑定边界），中文/多语言文案与既有约定一致。
  - 见本 change `evidence/implementation-2026-10-10.md` 的实施与运维说明。

## 7. 验证与回退

- [x] 7.1 单测：状态机与不可变性、内容哈希稳定性、成员可见性/默认解析、草稿不泄漏、回滚即新版本、停用不删数据、转换衔接。
  - `tests/test_shared_agent_release_governance.py`。
- [x] 7.2 单测：审批未提供/摘要不匹配/自审批/重复消费；企业未启用/配额不可判定/管理员不豁免；越权与跨租户 404；审计脱敏与同事务回滚。
  - 同上 + `tests/test_shared_agent_release_routes.py`。
- [x] 7.3 迁移与兼容：开关关闭零行为变化；开启回填幂等；非 database 模式不启用；运行期关开关不删历史。
  - `test_flag_off_shared_agent_is_resolvable`、`test_backfill_*`、`test_publish_refused_when_switch_off`；历史行从不删除。
- [ ] 7.4 真机/端到端：至少一个租户以已发布版本运行，验证草稿编辑不影响成员、回滚后成员使用回滚版本、停用后新运行被拒；记录真实证据，只声明验证通过的范围。
  - 单元与 WSGI 端到端已覆盖；未在真实运行实例（真机 roster + 运行中会话）上执行，未声明通过。
- [x] 7.5 记录回退步骤（关开关 + 代码版本回退），确认不影响已发布历史与成员数据。
  - 关开关：`shared_agent_release_governance=false`，成员可见性/解析/运行装配立即回到既有行为，`agent_releases` 与绑定发布列保留不删。代码回退：删除 `_migration_48` 引用即可；新增表/列对旧代码无副作用。

## 8. 严格校验

- [x] 8.1 运行 `openspec validate add-shared-agent-release-governance --strict`，并核对 `openspec/config.yaml` 引用的 capability 名一致。
  - 环境无 `openspec` CLI；改用仓库自带等价校验 `python scripts/check_change_deltas.py add-shared-agent-release-governance`（delta 与基线一致）。capability 名与 `openspec/config.yaml` 约定一致。
