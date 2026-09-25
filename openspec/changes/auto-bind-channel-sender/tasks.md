# Tasks: 渠道发送者身份自动绑定

> 用户裁定（本 change 的口径来源）：
> 1. 「请实现自动绑定，那个账号扫描或者新增消息渠道的就自动绑定那个账号。」
> 2. 共享/租户实例「跟个人渠道一样即可」—— 首条私聊发送者绑到**创建者**（`created_by`）。
> 3. 「一并实现扫码时就用提供方返回的扫码人身份直接绑定。」
>
> 代价已知情并接受：先给共享机器人发消息的陌生账号会以创建者身份被服务。第 2 节的四道约束是为此加的，不是可选项。

## 1. 认领语义与存储

- [x] 1.1 `auth/store.py` 迁移 32：`tenant_channel_instances.sender_binding_at INTEGER`（可空，无默认）。回填 `personal_channel_links` 现有行 ⇒ 升级前**在绑**的实例不可能被陌生人认领；升级前已解绑的行与「从未绑定」不可区分，作为残留写在 proposal
- [x] 1.2 `auth/service.py` `_stamp_sender_binding_in_tx`：用 `COALESCE(sender_binding_at, unixepoch())` 记录**最早**一次，重连不刷新；由 `link_personal_channel` 与两条自动绑定路径共用
- [x] 1.3 `get_tenant_channel_instance_row` 增列 `created_by` / `sender_binding_at`：共享实例的负责人锚点与「可否认领」都从这里读，规则不出现第二份定义

## 2. 自动绑定（本人实例 + 共享实例）

- [x] 2.1 `claim_instance_for_sender`：返回 `{"claimed","reason","user_id","scope","agent_id"}`；**预期内的拒绝一律以 reason 返回而不抛异常**（调用方在决定一条消息，需要的是理由而不是栈）；并发写入冲突先回滚事务再返回拒绝，不会被当成成功。非预期异常仍然冒出（把 bug 静默成「不可认领」只会把它藏在一条操作者无法处置的提示后面）
- [x] 2.2 约束一：**仅私聊**（`is_group` ⇒ `group_not_personal`）
- [x] 2.3 约束二：**仅从未绑定过的实例**（`sender_binding_at IS NOT NULL` ⇒ `already_bound`），且在写锁内**重读**该戳 —— 两个发送者抢同一个"处女"实例时只会有一个被告知成功
- [x] 2.4 约束三：**已属他人的三元组绝不改指**（`identity_conflict`；同一负责人复用既有行）
- [x] 2.5 约束四：**负责人必须真的可用**（实例启用 + 治理未停 + 凭据未撤 + 类型运行已开放 + 成员有效 + 个人实例目标仍为本人私有 Agent 且有 `agent.use`），否则不写任何东西，让调用方保留它原本的拒绝
- [x] 2.6 一个事务内完成：身份绑定（三元组为新时）+ 个人路由 + `sender_binding_at` + 审计行 `channel.sender.auto_bind`；锚点 = 个人实例 `owner_user_id` / 共享实例 `created_by`
- [x] 2.7 `resolve_personal_channel_inbound`：`not_linked` 前尝试认领并按新路由**重跑全部原闸门**（认领是前置，不是绕过）
- [x] 2.8 `channel/external_identity.py` `resolve_actor_for_context`：`UNBOUND` 前尝试同一认领，成功后重查用户；失败仍回 `UNBOUND`（未绑定提示与待绑定登记不变）
- [x] 2.9 共享实例**不**新增个人路由：`PUBLIC_PERSONAL_INGRESS_TYPES` 仍为空集（该切片另需验收），共享实例以自己的 Agent 服务被绑定的成员

## 3. 扫码人身份绑定（创建时）

- [x] 3.1 `weixin_scan_adapter.scanner_identity(answer)`：从 `ilink_bot_id` / `ilink_user_id` 取 `(provider, issuer, subject)`；缺任一半返回 `None` —— 绑一个永远匹配不上的事实比不绑更糟（会关掉首条认领却仍拒绝本人）
- [x] 3.2 `IdentityService.bind_scanner_identity`：与认领同一套写入，`reason=scanner_identity` 区分来源；幂等（重复提交 ⇒ `already_bound`）
- [x] 3.3 只在 `inbound_identity_admissible(channel_type)` 为真时绑定，否则 `type_has_no_inbound_identity` 跳过并记日志
- [x] 3.4 `weixin_scan_adapter.bind_scanner_identity` 包装：**绝不抛出**（提交已成功，绑定失败不能把「已保存」变成「扫码失败」），并把跳过原因写进日志
- [x] 3.5 `channels.py::_confirm_and_commit` 在提交成功后调用（厂商应答在此才可读；重试轮询没有应答时跳过，实例仍可由首条认领）

## 4. 测试

- [x] 4.1 `tests/test_personal_channel_inbound.py` +10：首条私聊被认领、锚点是 owner 而非"谁先打字"、第二条账号被拒、群聊不可认领、已属他人不认领（且不消耗那一次认领）、治理停用不可认领、解绑后陌生人与 owner 都不再被认领、共享实例首条认领（锚点为创建者）且第二条回 `UNBOUND`、共享实例群聊不可认领
- [x] 4.2 `tests/test_scanner_identity_binding.py` +14（新套件）：应答字段解析、五类残缺应答返回 `None`、可打戳类型绑定成功、非打戳类型跳过且不写绑定不打戳、扫码绑定后首条认领被关、重复绑定幂等且不刷新时间戳、已属他人不改指、实例不存在是拒绝不是异常、包装层失败不冒泡、跳过原因可读
- [x] 4.3 `tests/test_tenant_channel_inbound_anchor.py`：`test_an_unbound_sender_is_still_refused` 改写为 `test_a_stranger_claims_an_unbound_instance_and_the_next_one_is_refused`（首条被认领 + 第二条仍拒绝）
- [x] 4.4 回归：`test_personal_channel_inbound.py`、`test_personal_channel_binding.py`、`test_personal_instance_policy.py`、`test_external_im_gate.py`、`test_tenant_channel_inbound_{anchor,closure,isolation}.py`、`test_tenant_channel_instances_service.py`、`test_scan_onboarding_state.py`、`test_scan_authorization.py`、`test_scanner_identity_binding.py` = **307 passed, 0 failed**（唯一按新口径改写的断言见 4.3）
- [x] 4.5 `tests/test_weixin_qr_flow.py`：改动前后失败集合**逐一相同**（22 项，与 `enable-personal-wecom-bot-runtime` 记录的既存失败一致），证明本次未引入新失败

## 5. 规范与证据

- [x] 5.1 `specs/external-identity-binding/spec.md`、`specs/personal-channel-configuration/spec.md`、`specs/tenant-channel-configuration/spec.md`、`specs/channel-scan-onboarding/spec.md` 四处 MODIFIED delta：未绑定默认处理增加「从未绑定实例的首条私聊认领」例外，以及扫码人身份绑定的承认
- [x] 5.2 `external-identity-binding` 的 MODIFIED 块按整块替换规则补回既有场景「已绑定但成员停用或无权」：MODIFIED 会替换整个 requirement，漏抄该场景会在归档时把它丢掉
- [x] 5.3 `evidence/1-verification.md`：改前基线、改后回归、未覆盖项与残留

## 6. 未完成（需真实条件）

- [ ] 6.1 真实 IM 账号的首条私聊认领往返（需要真实提供方账号；本副本无可用账号）
- [ ] 6.2 微信扫码人身份绑定的真实验收（需先另立 change 为微信补 `stamp_external_identity`，并使微信实例可被创建）
