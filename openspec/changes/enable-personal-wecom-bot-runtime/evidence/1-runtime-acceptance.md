# 证据 1：本人 `wecom_bot` 渠道运行面验收

时间：2026-09-20（本机 Windows 单机部署，`C:\rdai\rsmagent`，后端 `127.0.0.1:9900`，nginx 入口 9899/443）
对象：实例 `chan_W7lQMzHzbKbY6USA`（`scope=user`、`channel_type=wecom_bot`、owner `usr_vGsjRbnoRRYIttSN` / RC005、
绑定 Agent `my-assistant-admin-test15-RC005`，租户 `tnt_EA3qM-lHPLD8ZPwW` / test15）

## 1. 问题现象与三处原因

现象：控制台该实例显示「已启用」，但从企业微信向机器人发消息**毫无反应**（17:30 左右）。

| # | 原因 | 观测 |
| --- | --- | --- |
| 1 | 运行进程里从未加载该实例 | `[App] Starting channels: ['web']`（pid 3948，17:13:13 启动）；实例行创建于 17:36:46（审计 `channel.instance.create` = success）；该进程仅监听 9900，**对 443 无任何出站连接** |
| 2 | 发送方账号没有身份关联 | `external_identities` 唯一 `wecom_bot` 绑定为 `issuer=aibeJr…`/`subject=TanJian`/`usr_9ZxVPz7M2FuOro1q`（旧机器人，实例已停用）；新实例机器人 id 为 `aibhXy…`；`personal_channel_links` 空、`binding_challenges` 空 |
| 3 | 本人连接被双重关闭（本 change 处理） | `personal_runtime_enabled('wecom_bot') = False`（总开关出厂 `False` 且 `PERSONAL_RUNTIME_ACCEPTED_TYPES` 为空集）⇒ 写入返回 `{applied: false, pending: true, error: "personal runtime is not enabled for this channel type"}` |

## 2. 改前只读判定（除第 3 点外全通）

以仓库自身的服务/判定函数读取（未写入任何业务数据）：

```
target state      : {'agent_id': 'my-assistant-admin-test15-RC005', 'state': 'ok', 'reason': '', 'enabled': True}
owner is active   : True
personal_channel_ready(wecom_bot)   : (True, '')
inbound_identity_admissible         : True
personal_runtime_enabled(wecom_bot) : False      <- 唯一阻塞
binding status                      : {'link': None, 'challenge': None}
```

通道能力对照（同一进程）：

```
weixin    | ready: (False, 'no_inbound_identity') | multi: True | stampable: False | declared: True
wecom_bot | ready: (True, '')                      | multi: True | stampable: True  | declared: True
feishu    | ready: (True, '')                      | multi: True | stampable: True  | declared: True
ACCEPTED: ['wecom_bot']   （改动后）
```

凭据经同一把 `COW_CREDENTIAL_MASTER_KEY` 解密成功（`wecom_bot_id` 长度 35、`wecom_bot_secret` 长度 43），
排除「主密钥不匹配导致渠道起不来」这一常见阻塞。

## 3. 改动

1. `channel/channel_instances.py`：`PERSONAL_RUNTIME_ACCEPTED_TYPES = frozenset({const.WECOM_BOT})`
2. `config.json`（本机、不入库）：`"personal_channel_runtime": true`
3. `tests/test_personal_channel_inbound.py`：`TestPersonalExecutionSwitch` 断言改为新的登记事实 + 总开关语义

回滚：把 `config.json` 的开关改回 `false`（一次配置变更即停掉本人连接，登记本身不打开部署）。

## 4. 运行面实测（重启后端后，pid 8720）

```
[INFO][18:56:11][channel_instances.py:1079] - [ChannelInstances] loaded 1 tenant-owned channel instance(s)
[INFO][18:56:11][app.py:131] - [App] Starting channel_instances: [('chan_W7lQMzHzbKbY6USA', 'wecom_bot', 'my-assistant-admin-test15-RC005')]
[INFO][18:56:11][app.py:903] - [App] Starting channels: ['web', ChannelInstance(instance_id='chan_W7lQMzHzbKbY6USA', channel_type='wecom_bot', agent_id='my-assistant-admin-test15-RC005', ...)]
[INFO][18:56:12][wecom_bot_channel.py:184] - [WecomBot] WebSocket connected, sending subscribe...
[INFO][18:56:12][wecom_bot_channel.py:488] - [WecomBot] ✅ Subscribe success
[INFO][18:56:22][channel_instances.py:1034] - [ChannelInstances] personal instance 'chan_HzOTq6ix_28QHuZg' (feishu) not started: personal runtime is not enabled for this channel type
```

出站连接（说明是真正活着的长连接，而不是「日志说订阅成功」）：

```
State       LocalPort RemoteAddress RemotePort
Established     63164 1.12.92.122             443
Listen           9900 0.0.0.0                   0
```

健康自检（`start-all.ps1 -Service backend`）：后端直连 / 9899 入口 / `/help/` 全部 HTTP 200。

**收窄仍然生效**：同一进程里另一个成员的 `feishu` 本人实例被明确拒绝启动 —— 登记只放行被登记的类型。

## 5. 测试回归

| 套件 | 结果 |
| --- | --- |
| `tests/test_personal_channel_inbound.py` | 41 passed（含改写后的 `TestPersonalExecutionSwitch`） |
| `tests/test_wecom_inbound_attachment_scope.py`、`tests/test_wecom_bot_inbound_identity.py` | 11 passed |
| `tests/test_weixin_qr_flow.py` + `tests/test_personal_capability_switches.py` | 23 failed / 45 passed，**改动前后失败集合逐一相同**（`Compare-Object` 双向为空） |
| `tests/test_personal_console_pages.py`、`test_personal_console_acceptance.py`、`test_personal_delivery_drill.py`、`test_personal_channel_console.py`、`test_personal_switch_entry_point_enforcement.py` | **171 passed**（+5 subtests），运行姿态＝总开关打开 + `wecom_bot` 已登记 |

既有失败与本 change 无关，逐项已定位：

- `weixin` 在本工作副本里 `inbound_identity_admissible('weixin') = False` ⇒ `personal_channel_ready('weixin') = (False, 'no_inbound_identity')`
  ⇒ 所有「扫码创建本人微信实例」用例在**未作任何改动时**同样失败（同一用例在 `git stash` 后复跑，失败一致）。
- `test_compat_surface_closure.py::CapabilitySwitchReaderTests::test_every_capability_switch_has_exactly_the_recorded_readers`
  在基线即失败：Windows 下 `os.path.relpath` 产出反斜杠（`auth\service.py`）而台账登记为正斜杠（`auth/service.py`），属路径分隔符差异。

## 6. 未覆盖项（需成员本人账号，登记为残留）

以下必须用**真实企业微信账号**完成，本机无法代做，故本 change 在补齐前不视为收口：

1. 在「我的渠道」对该实例发起绑定（`POST /api/personal/channels/chan_W7lQMzHzbKbY6USA` + `{"action":"start_binding"}`）拿到一次性绑定码；
2. 成员用**本人企微账号**向机器人私聊发送该码 ⇒ 期望「已绑定」且 `personal_channel_links` 落一条 `provider=wecom_bot`、`issuer=aibhXy…` 的链接；
3. 再发普通消息 ⇒ 期望 `[chat_channel] personal route scoped user=… instance=chan_W7lQMzHzbKbY6USA` 并回复；
4. 负向：非 owner 被拒、`unlink`/停用/撤回总开关后立即失效。

其中第 2 步是第 1 节原因 2 的**必须动作**：新机器人的 issuer 与既有绑定不同，不绑定则消息仍会被判 UNBOUND 并回「账号尚未绑定」提示（有回复、但不是想要的答案）。

补充事实（控制台口径）：`instance_runtime_state()` 是**进程内**观测值（`channel_instances._runtime_state`），只在
`apply_tenant_instance_runtime` / `reconcile_instance_runtime` 写入，启动合成路径不写。因此重启后即使连接已建立，
控制台投影仍会读到「尚无观测」⇒ `state=unknown`（而非 `connected`），`talkable` 为 false。在控制台对该实例做一次
**停用→启用**（或保存）即触发 reconcile，投影变为 `connected`；`talkable` 还需第 6 节的绑定完成。
本 change 不修改该投影（它刻意不把「未观测」读成「成功」）。
