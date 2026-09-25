## Why

成员的「企微个人助手」（`scope='user'`、`channel_type=wecom_bot`）在控制台上显示「已启用」，但从企业微信发消息**完全没有反应**。只读复核把原因定位到三处，**前两处是环境/事实，第三处才是本 change 要处理的口径**：

1. **运行进程里从来没有这条渠道**：后端（pid 3948，17:13:13 启动）启动时只解析出 `[App] Starting channels: ['web']`；实例行 `chan_W7lQMzHzbKbY6USA` 是 17:36:46 才创建的（审计 `channel.instance.create` success），晚于进程启动，且该进程对 443 无任何出站连接 —— 长连接不存在，消息自然无人接收。
2. **发送方账号没有任何身份关联**：`external_identities` 里唯一的 `wecom_bot` 绑定挂在旧机器人 `aibeJr…`（`subject=TanJian`、用户 `usr_9ZxVPz7M2FuOro1q`）上，而新实例的机器人 id 是 `aibhXy…`；`personal_channel_links` 为空。即使连上，入站也会被判 UNBOUND 并要求重新绑定。
3. **本人连接被双重关闭**：`personal_runtime_enabled()` = 部署总开关 `personal_channel_runtime`（出厂 `False`）**且** 该类型在 `PERSONAL_RUNTIME_ACCEPTED_TYPES` 中，而后者出厂为空集。因此保存成功只得到 `{applied: false, pending: true, error: "personal runtime is not enabled for this channel type"}` —— 这是「配置就绪 ≠ 执行就绪」的设计，不是缺陷。

实测确认：**除第 3 点外，其余门全部已开**（见 `evidence/1-runtime-acceptance.md` 的只读判定：目标智能体 `state=ok`、owner 为有效成员、`personal_channel_ready('wecom_bot')=(True,'')`、`inbound_identity_admissible('wecom_bot')=True`）。所以本 change 按 `unify-console-by-data-scope` 任务 7.6 的要求，把 `wecom_bot` 记为**已验收类型**，并在同一提交内留下真实证据。

## What Changes

- `channel/channel_instances.py`：`PERSONAL_RUNTIME_ACCEPTED_TYPES` 由 `frozenset()` 改为 `frozenset({const.WECOM_BOT})`，注释指向本次证据文件。**登记只是必要条件**：部署总开关仍然独立决定能否连接，「按类型登记不会自动打开部署总开关」的既有断言保留。
- `tests/test_personal_channel_inbound.py`：`TestPersonalExecutionSwitch::test_the_shipped_switch_is_closed` 改为断言新的登记事实（`== frozenset({WECOM_BOT})`），并把「总开关撤回后连已登记类型也关闭 / 打开后只有已登记类型可连」显式钉住（原先只断言「全空、全关」）。
- `config.json`（机器本地、不入库）：打开部署总开关 `"personal_channel_runtime": true`。**回滚就是把它改回 `false`**：撤下开关即停掉本人连接，且不动任何已验收目录与 owner 检查。
- MUST NOT 改动：出箱默认值（`config.py`、`auth/policy.py` 仍为 `False`）、`PUBLIC_PERSONAL_INGRESS_TYPES`（保持空集：共享实例上的本人路由仍未验收）、`PERSONAL_READY_CHANNEL_TYPES` 与任何适配器范围。

## Capabilities

### New Capabilities
<!-- 无新增 capability：本人渠道的「已验收类型登记」属于既有 channel-runtime 责任域 -->

### Modified Capabilities
- 本人渠道执行面（`channel/channel_instances.py` 的 `PERSONAL_RUNTIME_ACCEPTED_TYPES`）：`wecom_bot` 由「未验收、不连接」变为「已登记、可按部署开关连接」；`feishu`/`dingtalk`/`weixin` 等仍为未验收，`weixin` 另因 `inbound_identity_admissible('weixin')=False` 连配置面都未开放。

## Impact

- **行为受影响**：`wecom_bot` 的本人实例在总开关打开时会被启动合成纳入并建立长连接；控制台对该类型的投影从 `runtime_not_open` 变为可按连接状态读取。
- **不受影响**：其它渠道类型的本人连接（仍是「已保存未连接」）、公共（`scope='tenant'`）实例、共享实例上的本人路由（`PUBLIC_PERSONAL_INGRESS_TYPES` 仍空）、`personal_channel_ready` 的配置面判定。
- **代码面**：`channel/channel_instances.py`（一个常量 + 注释）；`config.json`（本机开关）。
- **测试面**：`tests/test_personal_channel_inbound.py`（1 处断言改为登记事实）；其余涉及该常量的用例均以 `patch` 注入，不受影响。**未新增**：无新接口、路由、开关、适配器。
- **未覆盖（登记为残留）**：真实入站往返（成员发绑定码 → 链接建立 → 消息路由到私有 Agent → 回复）需要成员本人的企业微信账号，见证据文件「未覆盖项」。
