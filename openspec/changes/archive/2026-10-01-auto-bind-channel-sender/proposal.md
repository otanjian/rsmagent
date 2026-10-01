## Why

渠道实例创建好之后**从它自己的账号发消息不会被执行**，只得到一句「你的账号尚未绑定这个个人渠道」。用户的原话是：

> 请实现自动绑定，那个账号扫描或者新增消息渠道的就自动绑定那个账号。

也就是说：**谁扫码 / 新建了这条消息渠道，就应该自动绑定成那个账号**，而不是让他先去控制台取一个绑定码、再回到 IM 里发那串码。今天做不到，是因为三处独立的「先证明再放行」串在一起：

1. **入站拒绝**：`resolve_personal_channel_inbound` 在 `personal_channel_links` 无行时直接回 `not_linked`（个人实例）；共享实例在 `resolve_actor_for_context` 里回 `UNBOUND`（提示「请联系管理员完成绑定」）。而链接行只能由绑定码兑换或控制台写入产生。
2. **扫码人身份被主动丢弃**：`weixin_scan_adapter.provider_result` 只保留渠道类型声明的凭据字段，厂商应答里的 `ilink_bot_id` / `ilink_user_id`（**谁扫的码**）既不落库也不回显。扫码其实同时证明了「本人在场」和「本人是哪个账号」，第二半被丢掉了。
3. **现状被规范明文固定**：`channel-scan-onboarding` 的场景「机器人接入后陌生用户发消息」写着「不将其自动绑定为扫码发起者」，`personal-channel-configuration`、`tenant-channel-configuration`、`external-identity-binding` 的未绑定场景同样只描述拒绝。

本 change 按用户裁定改掉这个口径：**渠道的发送者身份在创建时或首条私聊时自动建立，锚点是「这条渠道的负责人」而不是「谁先打字」**（个人实例 = owner，共享实例 = 创建者 `created_by`）。同时按用户选择保留两条独立入口：

- **扫码人身份绑定（创建时）**：提供方若在扫码应答里给出扫码账号，就在实例落地后绑定该账号，本人第一条消息即可执行。
- **首个发送者绑定（首条私聊时）**：从未绑定过的实例，把第一条**私聊**的发送者绑到该实例的负责人名下。

**代价必须写清楚**（用户已知情并选择）：共享实例上，先给机器人发消息的陌生人会以**创建者**的身份被服务（若创建者是管理员即等同提权）；个人实例上同理。因此实现里加了四道必须同时成立的约束，见 `tasks.md` 第 2 节：仅私聊、仅从未绑定过的实例、已属他人的三元组绝不改指、全程审计 + 控制台可解绑。

## What Changes

- `auth/store.py`：新增迁移 32，`tenant_channel_instances.sender_binding_at`。**任何**路径建立过发送者身份即打戳（绑定码兑换、控制台关联、扫码人绑定、自动认领自己）；`NULL` 是唯一可被认领的状态。打戳让「首个发送者」是**一次性**的 —— 否则 owner 解绑后，下一个发消息的人就会接管这条渠道。
- `auth/service.py`：
  - 拆出 `_validated_identity_triple` / `_resolve_identity_binding` / `_bind_external_identity_in_tx`，使「在调用方自己的事务里建立绑定」与管理员插入走**同一套**校验、审计与待绑定清理（`_bind_external_identity_row` 行为不变）。
  - 新增 `claim_instance_for_sender`：从未绑定的实例 + 首条私聊 + 负责人可用 ⇒ 一个事务内写入全局身份绑定（三元组为新时）、个人路由、`sender_binding_at`、审计行 `channel.sender.auto_bind`。
  - 新增 `bind_scanner_identity`：扫码人身份的同类写入；**只绑定该类型入站真的能带上戳的标识**，否则跳过并给出原因（绑一个永远匹配不上的事实，比不绑更糟：它会关掉首条认领却仍然拒绝本人）。
  - `resolve_personal_channel_inbound`：`not_linked` 之前先尝试认领，认领后重读路由并**继续走原有全部闸门**。`link_personal_channel` 在同事务内打戳；`get_tenant_channel_instance_row` 增列 `created_by` / `sender_binding_at`。
- `channel/external_identity.py`：`resolve_actor_for_context` 在 `UNBOUND` 之前尝试同一认领（共享实例路径）。
- `channel/weixin_scan_adapter.py`：新增 `scanner_identity()`（从扫码应答读取扫码账号）与 `bind_scanner_identity()`（提交后绑定，**绝不抛错**：实例已提交，绑定写不进去不能把「已保存」变成「扫码失败」）。
- `channel/web/fork/handlers/channels.py`：`_confirm_and_commit` 在提交成功后调用扫码人绑定。
- 测试：`tests/test_personal_channel_inbound.py` 新增 10 例（个人/共享认领、二次发送拒绝、群聊不可认领、已属他人不认领、停用不可认领、解绑不重开）；新增 `tests/test_scanner_identity_binding.py` 14 例；`tests/test_tenant_channel_inbound_anchor.py` 的 `test_an_unbound_sender_is_still_refused` 改写为新规则（首条认领 + 第二条拒绝）。
- MUST NOT 改动：绑定码与管理员绑定路径（保留为恢复手段）、`PERSONAL_RUNTIME_ACCEPTED_TYPES`、`PUBLIC_PERSONAL_INGRESS_TYPES`（共享实例仍未新增本人路由）、任何渠道类型目录准入性。

## Capabilities

### New Capabilities
<!-- 无新增 capability：自动绑定是既有 `external-identity-binding` / `personal-channel-configuration` / `tenant-channel-configuration` 责任域内的一次口径修改 -->

### Modified Capabilities
- `external-identity-binding`：未绑定入站的默认处理由「一律拒绝并登记待绑定」改为「**从未绑定过的实例**上，首条私聊发送者绑到该实例负责人名下（一次性、可审计），其余情况仍拒绝并登记」。
- `personal-channel-configuration`：个人实例的入站所有者校验增加「从未绑定 ⇒ 首条私聊认领（锚点为 owner）」这一前置，其余闸门（成员、目标私有归属、授权、群聊、治理停用）不变。
- `tenant-channel-configuration`：共享实例的入站租户边界不变；未绑定发送者的拒绝增加同一例外，绑定的成员仍是**该实例所属租户**的成员。
- `channel-scan-onboarding`：扫码控制权仍不证明「任意消息发送者等于成员」，但明确承认两件**不是**消息推断的事实 —— 提供方点名的扫码账号可在创建时绑定；从未绑定实例的首条私聊可认领。

## Impact

- **行为受影响**：从未绑定过的个人实例与共享实例，其**第一条私聊**不再被拒；发第一条私聊的账号成为该渠道的发送者身份。解绑后不再可被认领（需走绑定码）。
- **不受影响**：已建立绑定的实例（一切照旧）、群聊（永不认领）、已绑定其他账号的三元组（永不改指）、绑定码 / 管理员绑定 / 控制台解绑。
- **安全面**：新增 `channel.sender.auto_bind` 审计行（含 provider/issuer/subject/scope/reason）与一行 INFO 日志；`sender_binding_at` 让「一次性」不依赖日志即可判定。
- **测试面**：11 个套件 **307 passed, 0 failed**（改后，14 分 50 秒）；`test_tenant_channel_inbound_anchor.py` 的既存断言按新口径改写；`test_weixin_qr_flow.py` 改动前后失败集合逐一相同（22 项，全部为既存失败）。新增测试 24 例（首条认领 10 + 扫码人身份 14）。
- **未覆盖（残留）**：
  1. **扫码人身份绑定对当前唯一会返回扫码账号的提供方（微信）不生效**：`inbound_identity_admissible('weixin')=False`（微信入站不打身份戳），且微信实例当前**根本不能创建**（目录准入性收窄后 creatable ⊆ admissible）。该分支因此是**防御性**的：它保护的是「某类型日后失去打戳能力」或「未来接入既打戳又返回扫码人身份的提供方」两种情形，已由 14 例测试直接驱动。
  2. 真实入站往返（用真实 IM 账号验证首条消息被认领）需要真实提供方账号，见 `evidence/1-verification.md`「未覆盖项」。
  3. 迁移 32 之前**已解绑**的历史实例无法与「从未绑定」区分，仍可被认领一次（有审计行、可解绑）。
