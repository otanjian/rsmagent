## MODIFIED Requirements

### Requirement: 敏感操作记录审计且防篡改

系统 SHALL 对授权变更、凭据使用、审批、配额调整、代码执行、跨租户访问、租户内跨 owner 的历史读取（`history.read_all`）及访谈报告生成（`session.interview_report.generate`）等敏感操作记录审计事件（时间、主体、租户、资源、动作、结果）；跨 owner 读取事件 SHALL 至少记录目标 Agent、会话标识与读取范围（列表/搜索/只读详情），MUST NOT 记录问答正文。审计记录 MUST 防篡改（追加式，不可由业务代码静默改写），敏感字段脱敏。

#### Scenario: 敏感动作落审计

- **WHEN** 角色权限被修改或凭据被使用
- **THEN** 生成含主体、租户、资源与结果的审计记录，且不含明文凭据

#### Scenario: 跨 owner 历史读取留痕

- **WHEN** 获权成员读取另一 owner 的会话列表、搜索结果或只读详情
- **THEN** 生成可归属的审计事件（主体、租户、目标 Agent、会话、动作 `session.history.read_all`、结果），不含问答正文；被拒绝的同类请求同样留痕且不泄露目标是否存在

#### Scenario: 业务代码不能静默删除审计

- **WHEN** 普通业务路径尝试覆盖或删除审计记录
- **THEN** 系统拒绝或仅追加，不丢失历史
