## Why

渠道入站附件的下载发生在消息解析阶段，早于 `chat_channel._handle` 建立身份作用域（`use_identity(_identity_for(context))`）。此时 `state_dir.tmp_dir()` 读不到 ambient 身份，按下落规则回退到**进程全局默认智能体**的工作区。真实后果：绑定到租户智能体 `tax-health-check-test15` 的企业微信机器人，把 9 份客户资料下载到 `/Users/jiantan/cow/agents/my-assistant-admin/tmp/`；租户智能体受执行隔离约束，既读不到该路径，也不会在自己的收件箱看到文件，只能回报「未投递到本工作区」。

`chat_channel._identity_for` 的文档字符串已经把这个坑写明（"Without this the bridge would serve the bound Agent while workspace paths still resolved to the default one"），但该保障只覆盖回复处理路径，未覆盖附件下载路径。这直接违反既有规范 `tenant-resource-isolation` 的「路径解析不回退全局存储」及其「跨租户回退尝试」场景。

## What Changes

- 企业微信（`wecom_bot`）入站消息的解析与附件下载 SHALL 在**该渠道实例绑定 Agent** 的身份作用域内执行，而不是依赖 ambient 身份。
- 覆盖 websocket 长连接与 webhook 回调两条接收路径（`_build_context` 的两个调用点：`:314` 与 `:521`）。
- 修复后，入站附件落到绑定 Agent 的 workspace（租户实例即租户 Agent 工作区），不再落到全局默认智能体。
- 行为边界不变：`bound_agent_id` 为空的传统单实例渠道保持既有语义，不引入新的失败模式。
- 本 change 只覆盖 `wecom_bot`。`feishu` / `weixin` / `qq` / `slack` / `telegram` / `discord` 存在同一缺陷（同样在解析阶段调用裸的 `state_dir.tmp_dir()`），SHALL 在后续变更中按同一模式处理，本 change MUST NOT 声称已修复这些渠道。

## Capabilities

### New Capabilities
<!-- 无新增 capability -->

### Modified Capabilities
- `tenant-resource-isolation`: 在「路径解析不回退全局存储」之下补充**入站渠道附件落点**的规范化要求——附件下载 SHALL 按渠道实例的绑定 Agent 解析工作区，MUST NOT 在身份缺失时回退到全局默认智能体；并补充身份缺失时失败关闭而非默认回退的可验收场景。

## Impact

- **行为受影响**：`channel/wecom_bot/wecom_bot_channel.py` 的 `_build_context`（解析 + `WecomBotMessage.prepare()` 下载）与 `channel/wecom_bot/wecom_bot_message.py` 的 `_get_tmp_dir`（经 `common/state_dir.tmp_dir` 解析）。
- **不受影响**：`state_dir` 的解析语义本身、`apply_instance` / `bound_agent_id` 契约、租户与 Agent 绑定模型、`file_cache` 机制、Web 渠道（其请求级身份在 handler 中同步建立）。
- **数据面**：不再产生落在全局默认智能体 `tmp/` 下的渠道附件；既有错位文件需运维侧手工归位，本 change 不改写历史文件。
- **平台配置**：`~/cow`（`agent_workspace` 缺省值）仍是 `tenant-resource-isolation` 认可的可信实例根之一，本 change 不改动可信根定义。
- **测试面**：以失败测试钉住「租户渠道实例的入站附件落在绑定 Agent workspace」，并以传统单实例渠道保持原语义作为反向控制。
