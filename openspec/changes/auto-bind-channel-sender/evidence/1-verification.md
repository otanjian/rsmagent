# 证据：渠道发送者身份自动绑定

改前基线 → 改动 → 改后回归，命令与结果逐条可复现。所有命令在 `C:\rdai\rsmagent` 下执行，
解释器为 `python`（3.12）。

## 1. 改动面

| 文件 | 改动 |
| --- | --- |
| `auth/store.py` | 迁移 32：`tenant_channel_instances.sender_binding_at` + 现有 `personal_channel_links` 行回填 |
| `auth/service.py` | `_validated_identity_triple` / `_resolve_identity_binding` / `_bind_external_identity_in_tx` 拆分；`claim_instance_for_sender`；`bind_scanner_identity`；`_stamp_sender_binding_in_tx`；`resolve_personal_channel_inbound` 接认领；`get_tenant_channel_instance_row` 增列 |
| `channel/external_identity.py` | `resolve_actor_for_context` 在 `UNBOUND` 前接同一认领 |
| `channel/weixin_scan_adapter.py` | `scanner_identity()` + `bind_scanner_identity()` |
| `channel/web/fork/handlers/channels.py` | `_confirm_and_commit` 提交后绑定扫码人身份 |
| `tests/test_personal_channel_inbound.py` | +10 例 |
| `tests/test_scanner_identity_binding.py` | 新套件，14 例 |
| `tests/test_tenant_channel_inbound_anchor.py` | 1 例按新口径改写 |

## 2. 改前基线（证明「新失败」为空）

`test_weixin_qr_flow.py` 在本工作副本里有一批**既存**失败（`weixin` 类型未打戳 ⇒ 扫码创建本人微信实例被拒），
与本次改动无关。为排除「本次改动引入了新失败」，把本次改动单独 `git stash` 后复跑同一文件：

```powershell
git stash push -m "auto-bind-wip" -- auth/service.py auth/store.py channel/external_identity.py
python -m pytest tests/test_weixin_qr_flow.py -q     # 22 failed, 18 passed in 227.58s
git stash pop
```

改后同一文件（与 `test_personal_scan_scope.py` 同批）：`22 failed, 38 passed`，其中
`test_personal_scan_scope.py` 全绿（20 passed）。**失败集合 22 项逐一同名**，与
`enable-personal-wecom-bot-runtime` 记录的既存失败一致 ⇒ 本次改动**未引入新失败**。

## 3. 改后回归

```powershell
python -m pytest tests/test_personal_channel_inbound.py tests/test_personal_channel_binding.py `
  tests/test_personal_instance_policy.py tests/test_external_im_gate.py `
  tests/test_tenant_channel_inbound_anchor.py tests/test_tenant_channel_inbound_closure.py `
  tests/test_tenant_channel_inbound_isolation.py tests/test_tenant_channel_instances_service.py `
  tests/test_scan_onboarding_state.py tests/test_scan_authorization.py -q
```

- 首轮：`1 failed, 292 passed`。唯一失败是 `test_tenant_channel_inbound_anchor.py::test_an_unbound_sender_is_still_refused`
  —— 它逐字钉住本次要改掉的口径（「未绑定发送者一律拒绝」）。按新口径改写为
  `test_a_stranger_claims_an_unbound_instance_and_the_next_one_is_refused`（首条被认领 + 第二条仍拒绝）。
- 复跑该套件：`10 passed`；合并回归（含新增套件）：**307 passed, 0 failed in 890.01s**。

专项：

```powershell
python -m pytest tests/test_scanner_identity_binding.py -q        # 14 passed
python -m pytest tests/test_personal_channel_inbound.py -q -k "claim or first_sender or ..."   # 10 passed
python -m pytest tests/test_scanner_identity_binding.py tests/test_personal_channel_inbound.py `
  tests/test_tenant_channel_inbound_anchor.py -q                  # 74 passed（并发写入保护加入后复跑）
```

## 4. 关键断言（拒绝的理由可复现）

| 断言 | 位置 | 证据 |
| --- | --- | --- |
| 首条私聊被认领且消息被服务（不是"先回一句绑定成功"） | `test_a_personal_instance_is_claimed_by_its_first_private_message` | `consumed is False`、`channel.sent == []`、`runtime_identity.user_id == owner` |
| 锚点是负责人而非"谁先打字" | `test_the_first_sender_is_bound_to_the_instances_owner`、`test_a_shared_instance_is_claimed_by_its_first_private_sender` | 个人实例 ⇒ `owner_user_id`；共享实例 ⇒ `root`（`created_by`） |
| 一次性：第二条账号被拒 | `test_a_second_account_is_refused_once_the_instance_is_claimed`（`PERSONAL_SENDER_MISMATCH`）、§共享 `UNBOUND` | 需要 `sender_binding_at` 才成立 |
| 解绑不重开 | `test_unbinding_does_not_reopen_the_instance_to_a_stranger` | 解绑后陌生人与 owner 均 `PERSONAL_NOT_LINKED`，`personal_channel_links` 仍无行 |
| 群聊永不认领 | `test_a_group_message_cannot_claim_an_instance` / `..._a_shared_instance` | `sender_binding_at IS NULL`，且无绑定行 |
| 已属他人绝不改指 | `test_an_account_bound_to_someone_else_cannot_claim_an_instance`、`test_an_account_owned_by_someone_else_is_never_repointed` | `identity_conflict`，`find_user_for_external_identity` 仍指向原账号 |
| 容器不可用即不认领 | `test_a_governance_stopped_instance_cannot_be_claimed` | `PERSONAL_UNAVAILABLE`，无绑定行 |
| 扫码人身份绑定幂等且关掉首条认领 | `test_a_repeated_bind_is_idempotent`、`test_binding_the_scanner_closes_the_first_sender_rule` | 第二次 `already_bound`、时间戳不变；`claim_instance_for_sender` ⇒ `already_bound` |
| 非打戳类型跳过而非盲绑 | `test_a_type_without_an_inbound_stamp_is_skipped_not_bound`、`test_the_adapter_reports_a_skip_instead_of_swallowing_it` | `type_has_no_inbound_identity`，无绑定行、无戳记 |
| 绑定失败不影响已提交的创建 | `test_a_failing_bind_does_not_fail_the_scan` | 包装层返回 `bind_failed` 而非抛出 |

## 6. 现场复现（test15 生产副本，2026-09-21 13:35–13:45）

用户在 13:23 向 `aster 机器人` 连发两条「你好」**毫无反应**。只读复核（identity.db 只读连接 + `run-console.log`
+ 进程/端口）定位到**两个与本 change 无关的现场前置条件**，二者都必须先成立才谈得上首条认领：

1. **活着的进程跑的是改动前的代码**：`app.py` PID 4984 启动于 **09:36:03**，而本次代码改动落在 10:05–11:30。
   该进程只可能按旧规则回 `not_linked`（日志里 09:40–11:57 的 5 条 `personal instance inbound denied
   reason=not_linked instance=chan_W7lQMzHzbKbY6USA` 即旧规则）。
2. **用户发的机器人属于「进程启动之后才创建、且从未连接」的实例**：同一租户下有两个本人 wecom_bot 实例——
   `chan_W7lQMzHzbKbY6USA`（`V1`，**active=0**，13:31:13 被停用）与 `chan_BV-SwiaVtFQ33gKe`
   （**active=1**，**创建于 13:23:46**，正是截图 13:23 那一刻）。进程启动时只有前者存在，所以后者根本没有连接：
   13:23 的两条消息在日志里**一条记录都没有**（不是被拒，而是无人接收），而旧实例被停用后也不会再收。
   另有一个 **`python app.py` PID 2872**（9/20 10:55 手工启动、占用 9898、持有一条腾讯 443 连接）是残留副本，
   它的日志不落 `run-console.log`，因此它的判定过程完全不可见——这类「看不见的第二个进程」本身就是隐患。

**处理**：停掉 PID 4984 与 2872，由既有 `RDAI-AppWatchdog` 计划任务拉起唯一一份新进程（PID 7068，13:42:44）。
重启后日志证实合成器只加载**当前启用**的实例：

```
[ChannelInstances] loaded 1 tenant-owned channel instance(s)
[App] Starting channel_instances: [('chan_BV-SwiaVtFQ33gKe', 'wecom_bot', 'my-assistant-admin-test15-RC005')]
[WecomBot] WebSocket connected, sending subscribe...
[WecomBot] Subscribe success
```

**认领前置于现场真实库的预检**（在 `identity.db` 的**字节副本**上跑真实判定函数，活库只读、未被写入）：

```
personal_channel_runtime: True
personal_runtime_enabled('wecom_bot'): True
inbound_identity_admissible('wecom_bot'): True
member_active / credential_active / private_agent_owner / member_can_use_agent: 均 true
claim_instance_for_sender           -> {"claimed": true, "user_id": "usr_vGsjRbnoRRYIttSN", ...}
resolve_personal_channel_inbound    -> {"allowed": true, "agent_id": "my-assistant-admin-test15-RC005"}
```

即：新进程 + 活动实例 + 首条私聊 ⇒ 认领成立并放行。**这条现场往返仍需用户用真实账号发一条私聊完成**（见 §7.1）。

## 7. 未覆盖项

1. **真实 IM 账号的首条私聊往返**：用真实提供方账号验证「首条消息被认领 → 以负责人身份执行 → 回复」。
   本副本没有任何可用的真实提供方账号（微信实例当前不可创建、其它类型无真实凭证），因此
   §4 的链路止于 `ChatChannel._preflight_external_inbound` 与真实身份库，未经过真实厂商链路。
   §6 已把现场前置条件与预检结果备好，只差用户侧发一条消息。
2. **微信扫码人身份绑定的真实验收**：微信入站不携带身份戳（`inbound_identity_admissible('weixin')=False`），
   且目录准入性收窄后微信实例不可创建，故该入口对当前唯一会返回扫码账号的提供方**不生效**。
   代码与 14 例测试把它作为**防御性**入口保留（保护「某类型日后失去打戳能力」与「未来接入既打戳又返回扫码人身份的提供方」）。

## 8. 残留（已登记，未修）

- 迁移 32 之前**已解绑**的历史实例与「从未绑定」不可区分，仍可被认领一次。有审计行
  `channel.sender.auto_bind` 可追溯，控制台可解绑；升级时在绑的行已回填，不受影响。
- 共享实例的自动绑定成员获得的是「该实例 + 该租户 Agent」的服务面，未新增个人私聊路由
  （`PUBLIC_PERSONAL_INGRESS_TYPES` 保持空集，该切片另需验收）。
