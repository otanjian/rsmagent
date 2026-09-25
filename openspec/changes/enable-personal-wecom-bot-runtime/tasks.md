# Tasks: 登记 wecom_bot 为本人渠道已验收类型

> 依据：`unify-console-by-data-scope` 任务 7.6「显式启用要求同一提交内既有真实证据、又把类型加入
> `PERSONAL_RUNTIME_ACCEPTED_TYPES`」。证据在 `evidence/1-runtime-acceptance.md`。

## 1. 前置只读复核（改前）

- [x] 1.1 确认「进程里没有这条渠道」而非「消息被吞」：`[App] Starting channels: ['web']`，实例行创建时间（17:36:46）晚于进程启动（17:13:13），且该进程对 443 无出站连接
- [x] 1.2 逐门复核本人实例的三项运行前判定：目标 `state=ok`（私有 Agent 归属、registry 存在、owner 有 `agent.use`）、owner 为有效成员、`personal_channel_ready('wecom_bot')=(True,'')`、`inbound_identity_admissible('wecom_bot')=True`
- [x] 1.3 确认唯一阻塞为 `personal_runtime_enabled('wecom_bot') is False`（总开关关 + 验收集为空），且凭据可解密（新机器人 id `aibhXy…`，与旧绑定的 `aibeJr…` 不同 ⇒ 需重新绑定）

## 2. 登记与开关（同一提交）

- [x] 2.1 `channel/channel_instances.py`：`PERSONAL_RUNTIME_ACCEPTED_TYPES = frozenset({const.WECOM_BOT})`，注释写明登记日期、证据路径与「登记≠打开部署」
- [x] 2.2 `config.json` 打开部署总开关 `"personal_channel_runtime": true`
- [x] 2.3 不改出箱默认值：`config.py` 与 `auth/policy.py` 仍为 `False`；`PUBLIC_PERSONAL_INGRESS_TYPES` 仍为空集
- [x] 2.4 更新 `TestPersonalExecutionSwitch`：断言登记事实，并钉住「撤回总开关即关闭已登记类型」「打开总开关也不放行未登记类型」
- [x] 2.5 规范增量：`specs/personal-channel-configuration/spec.md` 新增 requirement「本人运行验收按类型登记且以真实入站证据为前置」，写明登记以真实入站证据为前置、与配置面就绪集合相互独立、登记≠打开部署总开关、≠开放共享实例本人路由、部署级收窄只减不增

## 3. 运行面观测（真实进程）

- [x] 3.1 重启后端，启动合成纳入该实例：`[ChannelInstances] loaded 1 tenant-owned channel instance(s)` + `[App] Starting channel_instances: [('chan_W7lQMzHzbKbY6USA', 'wecom_bot', 'my-assistant-admin-test15-RC005')]`
- [x] 3.2 长连接建立：`[WecomBot] WebSocket connected, sending subscribe...` + `[WecomBot] ✅ Subscribe success`，且进程出现 `Established → :443` 出站连接
- [x] 3.3 反向验证收窄仍生效：同一进程里 `[ChannelInstances] personal instance 'chan_HzOTq6ix_28QHuZg' (feishu) not started: personal runtime is not enabled for this channel type`
- [x] 3.4 健康自检全绿（后端直连 / 9899 入口 / `/help/`）

## 4. 测试回归

- [x] 4.1 `tests/test_personal_channel_inbound.py` 41 passed
- [x] 4.2 `tests/test_wecom_inbound_attachment_scope.py`、`tests/test_wecom_bot_inbound_identity.py` 11 passed
- [ ] 4.4 与开关/验收集直接相关的两个套件（`test_weixin_qr_flow.py`、`test_personal_capability_switches.py`）**改动前后失败集合逐一相同**（23 项，全部为既存失败），证明本次登记未引入新失败
- [x] 4.5 `tests/test_personal_console_pages.py`、`test_personal_console_acceptance.py`、`test_personal_delivery_drill.py`、`test_personal_channel_console.py`、`test_personal_switch_entry_point_enforcement.py` **171 passed**（5 subtests），在「总开关打开 + `wecom_bot` 已登记」的部署姿态下运行

## 5. 真实入站往返（需成员本人账号）

- [ ] 5.1 在「我的渠道」对该实例发起绑定（`POST /api/personal/channels/<id>` `{"action":"start_binding"}`），拿到一次性绑定码
- [ ] 5.2 用成员本人的企业微信账号向机器人私聊发送该绑定码 ⇒ 期望回复「已绑定」，`personal_channel_links` 出现 `(instance, provider=wecom_bot, issuer=aibhXy…, subject=<成员 userid>)`
- [ ] 5.3 再发一条普通消息 ⇒ 期望路由到私有 Agent 并按该 Agent 回复；`run.log` 出现 `[chat_channel] personal route scoped user=… instance=chan_W7lQMzHzbKbY6USA`
- [ ] 5.4 负向验证：非 owner 账号发消息被拒；`unlink`/停用后立即失效；撤回总开关后连接停下
- [ ] 5.5 把 5.1–5.4 的实测输出补进 `evidence/1-runtime-acceptance.md` 后本 change 才算收口
