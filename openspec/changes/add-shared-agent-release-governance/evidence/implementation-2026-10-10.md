# 实施与验证证据 — add-shared-agent-release-governance

> 本文保留自远端 `7f303f25`，记录较早的仅共享发布切片。文中的任务编号对应 [旧任务清单](legacy-shared-release/tasks.md)，不对应当前统一治理方案；现有代码和历史证据予以保留，不据此宣称当前方案已实现或验收。差异见 [历史方案说明](legacy-shared-release/merge-notes.md)。

日期：2026-10-10
范围：租户共享智能体的**发布治理**（审批 / 审计 / 版本不可变 / 权限冻结 / 配额），
默认开关关闭（`shared_agent_release_governance=false`），开启后对成员只暴露"已发布"版本。

## 1. 交付物（代码落点）

| 关注点 | 位置 |
| --- | --- |
| 开关（默认关） | `config.py` → `available_setting["shared_agent_release_governance"]` |
| 迁移：`agent_releases` 表 + `agent_bindings` 发布列 | `auth/store.py` → `_migration_48` |
| 状态/快照/哈希/可见性/运行时覆盖查找 | `auth/agent_release.py`（新增） |
| 发布原语与门禁 | `auth/service.py` → `release_agent` / `rollback_agent_release` / `suspend_agent_release` / `list_agent_releases` / `agent_release_status` / `released_snapshot` / `request_agent_release` / `backfill_agent_releases` |
| 成员可见性接入 | `resolve_default_agent` / `resolved_public_default_agent_id` / `_release_binding_visible`；`channel/web/fork/handlers/agents.py:_iter_tenant_agents`（USE 范围） |
| 运行装配冻结 | `agent/effective_capabilities.resolve_effective_capabilities`（已发布 `effective` 覆盖草稿；不可核验即拒绝全部工具） |
| 私有↔共享衔接 | `make_agent_tenant_shared` / `set_agent_visibility` |
| HTTP 动作 | `channel/web/fork/handlers/agents.py`：`request_release` / `release` / `rollback_release` / `suspend_release` / `list_releases`；管理投影附 `release` 字段 |

## 2. 语义要点

- **不可变版本**：每次发布/回滚追加一条 `agent_releases`（`UNIQUE(agent_id, version)`），
  绑定指针指向当前 `released_version`；内容由规范序列化计算 `content_hash`。
- **一处可见性真相**：仍是 `agent_bindings`；治理只新增 `release_state` 与发布指针，
  不复制第二套可见性来源。私有绑定不受治理影响（owner 视角不变）。
- **审批绑定内容**：`request_agent_release` 计算候选哈希并作为审批 digest；
  内容事后变化即 digest 不匹配，批准不能被挪用于另一份内容；不得自审批。
- **企业/配额门槛**：`_release_enterprise_ready`（存在且 active 的租户；身份仅 database）
  与 `_release_quota_ok`（读不到配额即拒绝）；管理员无豁免。
- **fail-closed 运行时**：共享绑定处于草稿/停用/快照缺失/读取失败时，
  `released_capability_overrides` 抛 `ReleasedSurfaceUnavailable`，解析器拒绝全部工具，
  绝不回落到草稿。
- **默认关闭零影响**：开关关闭时，迁移只加空列；可见性/解析/运行装配与既有行为逐字节一致；
  从不把既有共享绑定降级为草稿（只有开启后的回填才登记为 version 1）。

## 3. 运行与运维

- 开启：配置 `shared_agent_release_governance: true` 后，需对既有共享绑定调用一次回填
  `IdentityService.backfill_agent_releases()`（幂等，把当前内容登记为已发布 version 1），
  使成员可见面在开启瞬间保持不变。
- 发布流程：`request_release`（生成待批）→ 另一名有资格用户 `decide_approval`（批准）
  → `release`（消费批准并写入新版本）。回滚：`request_release(kind="rollback", version=N)`
  → 批准 → `rollback_release`（历史内容作为新版本再发布）。停用：`request_release(kind="suspend")`
  → 批准 → `suspend_release`。
- 回退：置 `shared_agent_release_governance=false` 即恢复既有可见性/解析/运行行为；
  `agent_releases` 与绑定发布列保留、不删除，历史与成员数据不受影响。

## 4. 验证命令与结果

```
python scripts/check_change_deltas.py add-shared-agent-release-governance
  → OK (proposed): deltas consistent with the baseline
  （环境无 openspec CLI，使用仓库自带等价校验；--strict 由该脚本的 delta 契约覆盖）

python -m pytest tests/test_shared_agent_release_governance.py \
                 tests/test_shared_agent_release_routes.py \
                 tests/test_agent_capability_enforcement.py \
                 tests/test_effective_capabilities.py \
                 tests/test_agent_visibility_routes.py \
                 tests/test_agent_visibility_toggle.py \
                 tests/test_identity_agent_bindings.py \
                 tests/test_desktop_target_resolution_acceptance.py \
                 tests/test_action_approval_consumer.py
  → 全部通过（治理 25 + 路由 6 + 邻接回归 ~100）
```

## 5. 未声明通过的范围

- 任务 6.1（控制台发布/回滚/停用界面与版本差异展示）：后端契约（动作 + 管理投影 `release`
  + `list_releases` 版本历史）已就绪；`channel/web/static/js/console.js` 前端界面作为紧随其后的
  前端切片交付。
- 任务 7.4（真机/运行实例端到端）：单元与 WSGI 端到端已覆盖；未在真实运行实例
  （真实 roster + 运行中会话）上执行，未声明通过。
