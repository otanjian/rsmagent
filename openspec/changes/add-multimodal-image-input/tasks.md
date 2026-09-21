## 1. 投递契约与前置（后续任务的前置）

- [x] 1.1 定义结构化附件描述（本地路径、媒体类型、原始文件名）并在 `Context` 上承载，保持既有 `content` 文本与路径标记兼容；不改动现有调用方的默认行为。（用 `Context.kwargs` 承载 `attachments`，无需改类定义）
- [x] 1.2 在 `agent/protocol/agent_stream.py` 的用户轮次构造处接受可选结构化附件，按能力判定决定是否附加 `image_url` 块；字符串调用方式保持可用。
- [x] 1.3 钉住 `models/openai_compatible_bot.py` 对 `image_url` 块的透传：`scripts/verify_image_input.py` 以真实提供方请求证明图像块被接受并返回真实画面描述。
- [x] 1.4 选定并写入配置项：单张 4 MiB、单轮 4 张、最大边 1568、JPEG 质量 85；默认值写入 `config-template.json`，非法值回退且不失效（`tests/test_image_attachment_transport.py`）。

## 2. 模型图像能力判定（阶段 1 门槛）

- [x] 2.1 修正 `models/deepseek/deepseek_bot.py` 的 `supports_vision`：判定集改为模块常量 `_VISION_MODELS`，覆盖 `deepseek-v4-flash` / `deepseek-flash` / `deepseek-v4-flash-vision-exp`，并删除「遇图必 400」的旧断言。
- [x] 2.2 回归测试 `tests/test_deepseek_vision_capability.py`：可读图模型判为真、纯文本模型判为假；已用变异测试确认该契约确实能捕获回归。
- [x] 2.3 判定来源改为 **bot 自身实际发送的模型**（`self.args["model"]`，即 `call_with_tools` 放进请求体的那个），全局配置仅作兜底：避免配置缺失时把「正要发图的 bot」判成不可读图，也避免构造后配置变更导致判定与实际请求脱节。测试见 `_CapabilitySourceOfTruth`。
- [x] 2.4 视觉工具主模型通道：同文件测试断言能力判定修正后 `_resolve_providers()` 首选 `MainModel`（前缀启发式不再是唯一依据）。
- [x] 2.5 真实图片端到端：`scripts/verify_image_input.py` 输出 `supports_vision: True`、内容块 `['text','image_url']`、模型读出图中文字 `RONGDA-42`；`scripts/verify_platform_image_turn.py` 再上一层，经平台生产 `AgentInitializer` 跑通一轮（见 `evidence.md`）。

## 3. 图片编码、上限与降级（阶段 2 门槛）

- [x] 3.1 单一编码实现 `agent/attachments.py`：格式校验 → 等比归一 → 字节上限（质量阶梯 + 递降缩放）→ data URL；渠道不重复实现。
- [x] 3.2 上限与降级：超单张字节、超单轮数量、格式不受支持、无法解码、文件缺失均不投递并产出可读原因。
- [x] 3.3 未投递原因写入该轮文本（`append_notices`），模型无法在不知情下声称已读图；模型不支持图像输入时同样给出说明。
- [x] 3.4 测试覆盖每种降级原因，并断言该轮不含 `image_url` 块且文本含说明。

## 4. Web 控制台接入（阶段 1）

- [x] 4.1 改造 `channel/web/fork/runtime.py` 附件装配：图片在保留路径标记文本的同时产出结构化附件，非图片类型行为不变。
- [x] 4.2 桥接 `bridge/agent_bridge.py`：从 `Context` 读取结构化附件并传到 `Agent.run_stream(attachments=...)`，旧调用方签名兼容。
- [ ] 4.3 前端附件选择、上传与历史展示保持现状：前端未改动，但尚未实跑确认 `console.js` 的路径标记解析不受影响。
- [x] 4.4 平台自身 Agent 栈端到端：`scripts/verify_platform_image_turn.py` 经生产 `AgentInitializer`（真实工具集 18 个）对 `tax-health-check-test15` 跑通一轮，模型读出 `RONGDA-42` 且未调用任何文件读取工具。**仍缺**经 HTTP 上传的那一段（控制台需登录凭据）。

## 5. 企业微信接入（IM 侧首发与实测渠道）

- [x] 5.1 改造 `channel/wecom_bot/wecom_bot_channel.py` 装配：缓存图片作为结构化附件传入该轮，同时保留路径标记文本。
- [x] 5.2 既有语义保持：「图片先到、文字后到」合并、缓存消费后清空、群聊/单聊前缀处理；`tests/test_wecom_image_delivery.py` 钉住前两条。
- [ ] 5.3 真实企业微信客户端实测未执行（需要真实渠道往返，当前环境无法完成）。
- [ ] 5.4 真实渠道上的失败路径实测（超限图片、已被清理的图片）未执行。
- [ ] 5.5 真实渠道往返证据落盘未完成。

## 6. 历史回放与上下文裁剪（阶段 3 门槛）

- [ ] 6.1 图片稳定存储与临时清理复核未做（当前仍落在既有 `tmp/`，依赖其保留策略）。
- [ ] 6.2 会话消息记录附件描述、重新加载重建图像内容未做（旧消息按现状回放）。
- [ ] 6.3 回放时图片缺失的显式占位未做。
- [x] 6.4 上下文裁剪先剥离较早图片块并留显式占位（`agent/protocol/message_utils.py`），文本与工具配对保留；`tests/test_context_image_blocks.py` 覆盖。
- [ ] 6.5 含图长会话超预算的真实证据未采集。

## 7. 其它渠道复用与交付

- [ ] 7.1 其余 IM 渠道接入同一机制未做（企业微信已接入，其余仍为纯路径标记）。
- [ ] 7.2 文档更新未做。
- [ ] 7.3 交付门槛：阶段 1 的核心证据已具备（能力判定 + 编码 + 真实提供方往返），阶段 2/3 未齐备。
- [ ] 7.4 回滚演练未做。

## 备注：未纳入的第二个 Web 栈

`channel/web/core/channel.py`（上游独立装配栈）含同一处 `[图片: <path>]` 逻辑，但按
`channel/web/web_channel.py` 的说明它是「fork 未加载时才使用」的备用栈，且受
`tests/test_upstream_drift_guards.py` 漂移守卫约束，故本变更不改动它。若该栈被启用，
必须补上同一机制，否则会重现本次缺陷。
