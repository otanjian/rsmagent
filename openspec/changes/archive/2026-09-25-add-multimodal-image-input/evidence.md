# 证据：图片以图像内容进入对话

记录本变更的可复核证据。每条都标注了它覆盖什么、以及**不**覆盖什么。

## 1. 修复前：图片只是文本标记

现场：Web 控制台的智能体回答「我收到的只是一个路径字符串，不是图像本身」，并在多轮里
反复声称图片已读或无法读取。

代码层根因（变更开始时的现状）：

- `channel/web/fork/runtime.py` 把附件一律拼成 `[图片: <本地路径>]` 文本。
- `channel/wecom_bot/wecom_bot_channel.py`、`channel/weixin/weixin_channel.py` 等同构。
- `models/deepseek/deepseek_bot.py` 的 `supports_vision` 把 `deepseek-v4-flash` 判为不可读图，
  依据是一句「遇图返回 400」的注释断言。

## 2. 修复后：真实提供方往返（脚本）

`.venv/bin/python scripts/verify_image_input.py`

图片内写有基准文字，因此答对即证明模型看到的是像素而非路径。

```
model            : deepseek-v4-flash
image            : /tmp/verify_image_input.png (1874 bytes)
attachments read : [{'path': '/tmp/verify_image_input.png', 'media_type': 'image/png'}]
supports_vision  : True
content blocks   : ['text', 'image_url']
notices          : []
errors           : []
thinking         : …识别结果是“RONGDA-42”…
answer           : 图片中的文字内容是：RONGDA-42
PASS: the model read the image itself
```

覆盖：能力判定 → 附件读取 → 内容块构造 → 提供方转换 → 真实 HTTP 请求 → 模型读图。

## 3. 修复后：平台自身 Agent 栈（脚本）

`.venv/bin/python scripts/verify_platform_image_turn.py`

走平台生产用的 `AgentInitializer`（经真实 `Bridge`），针对企业微信所绑定的那个智能体
（`tax-health-check-test15`）跑一轮。

```
agent            : tax-health-check-test15
image            : /tmp/verify_platform_image.png (1788 bytes)
attachments      : [{'path': '/tmp/verify_platform_image.png', 'media_type': 'image/png'}]
model            : deepseek-v4-flash
tools loaded     : 18
answer           : RONGDA-42
PASS: the platform's own agent stack read the image
```

同时说明「不再依赖工具读文件」：该轮 `[Agent] Turn 1` 即完成，模型直接读图作答，
没有调用任何文件读取工具。

覆盖：agent bootstrap、真实工具集、真实模型、图像内容投递。
**不覆盖**：把图片送进来的 HTTP / 渠道入口。

## 4. 运行中部署状态

重启后（进程启动时间晚于代码修改）：

```
[App] Starting channels: ['web', ChannelInstance(feishu, chan_FBlxbCbCv0EZajdZ),
                              ChannelInstance(wecom_bot, chan_dESCeabBWsskMEvi)]
[WecomBot] WebSocket connected, sending subscribe...
[WecomBot] ✅ Subscribe success
[FeiShu] ✅ Websocket thread started, ready to receive messages
```

- 部署下 `conf()['model'] = 'deepseek-v4-flash'`，`supports_vision = True`。
- 生效策略：`AttachmentPolicy(max_bytes=4194304, max_count=4, max_edge=1568, jpeg_quality=85)`。

**注意**：平台必须带 `COW_CREDENTIAL_MASTER_KEY` 启动，否则租户渠道凭据解不开，只会启动
`web`（本机表现为 `Starting channels: ['web']` + `channel credential decrypt failed`）。
不带密钥的重启会静默丢掉企业微信入站。

## 5. 单元与集成测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_image_attachment_transport.py` | 编码、尺寸归一、字节上限、数量上限、每种降级原因、配置回退、`Context` 附件读取 |
| `tests/test_agent_image_turn.py` | 该轮内容块构造：可读图模型收到图像块；纯文本模型不收到且被明说原因；接线到 `run_stream(attachments=...)` |
| `tests/test_deepseek_vision_capability.py` | 能力判定契约、判定来源为 bot 自身模型、视觉工具路由到主模型 |
| `tests/test_context_image_blocks.py` | `image_url` 令牌计价、压缩时留显式占位 |
| `tests/test_wecom_image_delivery.py` | 企业微信把缓存图片作为结构化附件交给该轮，并保留既有合并/清空语义 |

关键断言均以**变异测试**验证过确实能捕获缺陷（临时回退实现后对应测试必须失败）：
能力判定集、字节上限的缩放路径、配置回退。

## 6. 尚未取得证据的部分

- 真实企业微信客户端发送图片的往返（需要真实渠道操作）。
- Web 控制台经 HTTP 上传图片的端到端（控制台需要登录凭据）。
- 含图长会话超出上下文预算后的真实压缩表现。
- 图片持久化与历史回放重建（尚未实现，见 `tasks.md` 第 6 组）。
