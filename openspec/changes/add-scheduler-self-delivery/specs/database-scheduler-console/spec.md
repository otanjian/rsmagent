## ADDED Requirements

### Requirement: 站内自推送目标由认证会话解析创建

系统 SHALL 允许成员创建一个把结果投递到其自身当前 Web 对话会话的定时任务（下称「站内自推送」），并 SHALL 把它作为与跨渠道目标并列的一等创建路径。站内自推送的投递目标 MUST 由服务端从已认证的当前会话解析：租户、owner、`channel_type='web'` 与 `session_id` SHALL 全部来自可信会话校验，客户端的 `receiver`、`owner`、`tenant_id`、`scope` MUST NOT 作为归属或目标证据；客户端请求中的 `receiver` 至多用于存在性校验，MUST NOT 成为最终写入值。

Agent 的定时工具 SHALL 在未给出显式跨渠道目标时默认按站内自推送处理（投回当前会话），MUST NOT 因缺少渠道或接收人而拒绝创建，也 MUST NOT 把创建责任转回用户手动操作。跨渠道接收者目录 SHALL 继续排除 web：站内自推送 MUST NOT 通过接收者目录实现，MUST NOT 使任一成员的 web 会话出现在他人的可选接收人列表中。

创建 SHALL 仍经既有的任务创建授权、配额与审计链路写入本人任务；站内自推送 MUST NOT 绕过 owner 快照、revision、执行时重验或资源授权。

#### Scenario: 对话中一句话创建站内自推送

- **WHEN** 成员在 Web 会话中要求「每天 9:00 把未完成的项推给我」，且未指定渠道与接收人
- **THEN** 系统直接创建成功，任务的 `channel_type='web'`、接收者与服务会话一致、归属为本人，无需成员提供渠道或接收人

#### Scenario: 控制台创建站内自推送

- **WHEN** 成员在控制台创建弹窗选择「站内消息（推送给当前会话）」
- **THEN** 创建成功，投递目标为当前会话，接收者由服务端解析，界面不要求选择外部渠道联系人

#### Scenario: 伪造会话或归属

- **WHEN** 请求引用非本人、非本租户或非 web 的 `session_id`，或伪造 `receiver` / `owner` / `tenant_id` / `scope`
- **THEN** 拒绝且不写入任务，不按默认会话或默认接收人补齐目标

#### Scenario: web 不进入跨渠道接收人目录

- **WHEN** 任一成员查看跨渠道接收人目录
- **THEN** 结果不包含任何 web 会话，站内自推送目标只对发起创建的本人可见可用

#### Scenario: 配额与审计不受削弱

- **WHEN** 成员在配额耗尽后创建站内自推送，或在正常状态下创建站内自推送
- **THEN** 前者与跨渠道创建同样被拒绝并可诊断，后者与跨渠道创建走同一 owner 快照、`scope`、revision 与审计链路
