## 1. 失败测试优先（RED）

- [x] 1.1 在 `tests/` 新增用例，钉住「绑定租户 Agent 的 wecom_bot 渠道实例收到文件附件时，落点在该 Agent 的 workspace 之下」。断言方向须为**不落在进程全局默认智能体的 workspace**，而非仅断言落在某目录。
- [x] 1.2 新增反向控制用例：`bound_agent_id` 为空的传统单实例渠道实例收到附件时，解析沿用既有语义并正常完成下载，不被拒绝、不改落到其他 Agent。
- [x] 1.3 新增用例覆盖图片与文件两类媒体落点一致，防止只修一类。
- [x] 1.4 新增用例覆盖长连接与回调两条接收路径落点一致（`_build_context` 的两个调用点）。
- [x] 1.5 运行上述用例，确认**修复前失败**且反向控制用例在修复前即通过（防止把「一律拒绝」当成修复）。

## 2. 实现（GREEN）

- [x] 2.1 在 `channel/wecom_bot/wecom_bot_channel.py` 引入 `RuntimeIdentity, use_identity`，在 `_build_context` 解析与下载的外层施加身份作用域。
- [x] 2.2 身份来源取渠道实例已登记值（`getattr(self, "bound_agent_id", "")` / `getattr(self, "tenant_id", "")`），不采信 context 自报，不新增第二套归属来源。
- [x] 2.3 覆盖 `_build_context` 的全部调用点（长连接 `:522` 与回调节点 `:315`），确认两条路径都进入该作用域。
- [x] 2.4 确认 `use_identity` 仅包住解析与下载，不包住 `self.produce(context)` 的路由调用；`bound_agent_id` 为空时沿用既有下落语义。
- [x] 2.5 复核 `channel/wecom_bot/wecom_bot_message.py` 的 `_get_tmp_dir()` 在两种身份状态下解析出预期路径，并确认图片（构造期解密）与文件（`prepare()`）两条落盘路径都被覆盖。
- [x] 2.6 归属不可解析时失败关闭：捕获 `StateDirError`，记录可诊断原因并拒绝该消息，MUST NOT 让异常冒泡到收发循环（否则重现「机器人静默失效」这一同类故障）。

## 3. 验证与回归

- [x] 3.1 运行新增用例，确认 1.1–1.4 全部通过且 1.2 反向控制仍通过。
- [x] 3.2 运行 wecom 渠道与 `state_dir` 相关的既有测试套件，确认无回归。
- [ ] 3.3 以实际运行进程复核：向绑定租户智能体的企业微信机器人发送一份文件，确认附件落在该 Agent 的工作区而非全局默认智能体。
- [x] 3.4 记录复核证据（落点路径、时间戳、身份状态），并明确列出本次**未**验证的项。

## 4. 文档与交付边界

- [x] 4.1 在交付表述中明确：本 change 只覆盖 `wecom_bot`，`feishu` / `weixin` / `qq` / `slack` / `telegram` / `discord` 的同一缺陷尚未修复，MUST NOT 声称已覆盖。
- [x] 4.2 补充运维说明：历史上已落在全局默认智能体 `tmp/` 下的渠道附件需手工归位，本 change 不自动搬运。
- [x] 4.3 运行 `openspec validate scope-inbound-attachment-to-bound-agent --strict` 并通过。
