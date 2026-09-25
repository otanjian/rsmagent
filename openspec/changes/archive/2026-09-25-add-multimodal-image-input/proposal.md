## Why

用户在 Web 控制台发送图片后，智能体回答「我收到的只是一个路径字符串，不是图像本身」，并在多轮里反复声称图片已读取或无法读取，最后把责任推给用户去粘贴文字。真实原因不是模型能力不足，而是平台把图片降级成了文本标记：

- `channel/web/fork/runtime.py:1592-1627` 把附件一律拼成 `[图片: <本地路径>]` 文本，图像字节从不进入请求。
- `channel/wecom_bot/wecom_bot_channel.py:594-632`、`channel/weixin/weixin_channel.py:620-632` 等 IM 渠道用同一「路径标记」模式（经 `channel/file_cache.py` 缓存后附加到文本）。
- `models/deepseek/deepseek_bot.py:90-98` 的 `supports_vision` 只承认 `deepseek-flash` 与 `deepseek-v4-flash-vision-exp`，把当前配置的 `deepseek-v4-flash` 判为不可读图。**实测该模型对本例图片返回 200 并准确描述了画面**（`reasoning_content` 与正文均非空），即能力表与实际行为不符，导致 `agent/tools/vision/vision.py` 的主模型通道被关闭。

后果有三个，都属于产品级缺陷：用户发送的图片在对话里永远不可见；模型只能靠路径幻觉作答；用户被要求手工转换输入形式。同一缺陷同时影响 Web 与企业微信（用户正在使用的主要渠道）。

## What Changes

- 新增图片**多模态投递**：图片以图像内容块与该轮文本一起送达模型，覆盖 Web 控制台与企业微信，其它 IM 渠道复用同一机制。
- 修正模型图像能力判定，使其与该轮实际选定模型的真实行为一致；实测可用者不得被静态表判为不可用。
- 图片上限（单张字节、单轮数量、支持格式）与尺寸归一，超限按**明确降级**处理。
- 图片未被送达时，该轮文本必须明确说明，使模型能如实报告，杜绝「假装看过」。
- 含图片会话的历史回放与上下文裁剪：可重建图像内容；不可回放时显式占位；压缩时先替换较早图片并保留文本与工具配对。
- 不改变回复方向能力（企业微信流式回复仍为文本），不引入渠道专有的图片解析分支。

## Capabilities

### New Capabilities

- `multimodal-image-input`: 图片以图像内容进入对话的投递契约、上限与降级、模型能力判定一致性、失败可见性、历史回放与上下文裁剪处理，以及渠道间复用边界。

### Modified Capabilities

无。既有 `session-context-controls` 只约束手动压缩的授权与并发语义，本变更不改变该契约，故不修改其规范；与图片相关的裁剪行为放在新能力内表达，避免复制其它责任域的规范。

## Impact

- 受影响代码：`channel/web/fork/runtime.py`、`channel/wecom_bot/wecom_bot_channel.py`、`channel/file_cache.py`、`channel/chat_channel.py`（上下文装配）、`bridge/agent_bridge.py`（`agent_reply`/`run_stream` 之间的传递）、`agent/protocol/agent_stream.py`（用户轮次内容块构造）、`models/deepseek/deepseek_bot.py`（能力判定）、`models/openai_compatible_bot.py`（已能透传 `image_url` 块，需回归确认）、`agent/tools/vision/vision.py`（主模型通道受能力判定影响）。
- 受影响数据：会话消息需能表达「该轮含图片」并在回放时重建；图片存储位置需脱离会被清理的临时目录。
- 受影响的既有行为：附件文本标记（`[图片: path]`）在历史展示中的呈现；上下文裁剪对含图轮次的处理；非视觉模型的入站行为必须保持今天的路径引用，不得回归。
- 配置：新增图片上限与尺寸归一参数（默认值需选定）。
- 验收重点：企业微信为 IM 侧首发与实测渠道；其它 IM 渠道只要求复用同一机制，不要求逐一个人工实测。
