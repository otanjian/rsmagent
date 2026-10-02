# 依赖接缝索引（任务 1.4）

列出本 change 阶段一实际消费的既有能力接缝（文件:行），供各阶段引用。凡未在此索引的接缝即视为不在本 change 范围内，不因「别处已有」而假定通过。

| 依赖 capability | 实际接缝（代码位置） | 阶段一消费点 |
|---|---|---|
| `identity-session` | `auth/session.py`（`SessionStore`、`generate_token`、`hash_token`、`session_ttl_seconds`）；`auth/store.py:_migration_1` 的 `auth_sessions` 表；`auth/service.py` 的 `login`/`logout` | 3.2 PKCE 成功事务登记原生来源；3.3 创建独立子 `AuthSession`；3.5 通用身份解析 |
| `desktop-tenant-context` | `auth/capability_matrix.py` 切片 `desktop_tenant_context`（当前 `open={}`，`implemented=True`、`accepted=False`）；`auth/desktop_auth.py`（546 行，PKCE 事务）；`desktop/src/main/auth-broker.ts`（主进程 broker） | 3.2/3.3/3.7；本 change 对该 capability 有 MODIFIED delta，见 `specs/desktop-tenant-context/spec.md` |
| PKCE HTTP 端点 | `channel/web/auth_handlers.py`（`DesktopAuthorizeHandler` GET/POST、`DesktopTokenHandler` POST）；`channel/web/route_registry.py:131-132`（`fork:desktop-auth`，policy `public`） | 3.2 在既有 token 成功事务内扩展，不改动单次 code/loopback 约束 |
| `audit-log` | `auth/audit.py`（`audit_event`、`denied_event`、`AuditStore`、`sanitize_payload`）；表 `audit_events`（`auth/store.py:_migration_1`） | 3.3 引导事务审计与 link 创建同事务；5.x 动作审计 |
| `resource-quota` | `auth/service.py`：`set_quota`/`quota_status`/`consume_quota`（约 11136+）；表 `quota_limits`/`quota_usage` | 阶段二 10.x 传输预留（本索引先登记，不在阶段一实现） |
| `action-approval` | `auth/service.py`：`request_approval`/`decide_approval`（约 10809+）；`agent/approval_gate.py`（`approval_decision`、`request_digest`、`tool_action_id`）；表 `approvals`（`_migration_5`） | 5.x 适用性记录；阶段二 12.3 |
| `resource-execution-authorization` / 工具授权 | `agent/tools/tool_manager.py`（工具发现/执行）；`agent/protocol/agent_stream.py`；资源 grant 表 `tenant_resource_grants`/`role_resource_grants`（`_migration_1`/`_migration_11`） | 阶段二 11.4 `client_files`；本 change 不新建第二套授权 |
| `tenant-resource-isolation` | `auth/service.py` 的 tenant/membership 解析；`X-Tenant-ID` 逐请求重验 | 3.5 子会话解析时同时验父会话与租户 |
| Web 路由唯一清单 | `channel/web/route_registry.py`（`ROUTES`，`derive_web_urls()`、`derive_route_policy()`）；覆盖率校验 `scripts/check-route-coverage.py` | 2.5 逐方法登记 meta 与阶段一认证端点 |
| Web 装配接缝 | `channel/web/core/template.py`（classic/split 对前端不可见）；`channel/web/static/js/console.js` 的模块装载；seam 校验 `scripts/check-web-module-seams.py` | 5.1 `fork/desktop-host.js` 装配 |
| 能力矩阵 | `auth/capability_matrix.py`（`Slice`、`finalize`、`check_consistency`、`feature_action_availability`） | 2.4 登记四个缺省关闭开关与新切片 |
| 配置键 | `config.py` 默认字典（约 100–460 行，如 `knowledge_conversion_enabled`） | 2.4 新增四个键 |

## 依赖证据状态（不代替验收）

- `desktop-tenant-context` 当前 `accepted=False`：`design.md` 明确「本 change 不能沿用『已有 broker』就认定该依赖通过」。阶段一门槛需要真实打包客户端切片（`acceptance.md` A12 + W01–W20）。
- 其余切片（scheduler/memory/project_browse/external_connections/session_context）当前 `accepted=True`、`check_consistency()==[]`，作为阶段一 W 矩阵的现行基线。
- 知识转换（`knowledge_conversion_enabled=False`）、源上传（`knowledge_source_upload_enabled=False`）默认关闭，W07 记「正确不可用」。
