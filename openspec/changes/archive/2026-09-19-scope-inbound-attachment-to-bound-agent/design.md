## Context

动机见 `proposal.md - Why`。设计所需的当前状态：

- 身份作用域唯一的施加点是 `channel/chat_channel.py:203` 的 `with use_identity(self._identity_for(context))`，它包住的是 `_generate_reply` / `_send_reply`，即**回复处理**。
- 附件下载发生在这之前：`channel/wecom_bot/wecom_bot_channel.py` 的 `_build_context` 构造 `WecomBotMessage`（图片在构造中解密落盘）并调用 `wecom_msg.prepare()`（文件在此时落盘），随后 `_build_context` 才返回，再由调用方 `self.produce(context)` 进入 `_handle`。
- 落点由 `channel/wecom_bot/wecom_bot_message.py:78` 的 `_get_tmp_dir()` → `common/state_dir.py:409` 的 `tmp_dir(identity=None)` → `state_root(None)` → `current_identity().agent_id` 决定。无 ambient 身份时按 `state_dir` 既有下落规则取默认智能体工作区。
- 渠道实例**已经**持有归属：`channel/channel.py:57` 的 `apply_instance(instance_id, bound_agent_id, credentials, members, tenant_id)` 写入 `self.bound_agent_id` / `self.tenant_id`，由 factory/manager 在 multi-instance 路径调用；传统单实例启动不调用，二者为空。
- 约束：`self.produce(context)` 的路由依赖 context，而 context 尚未构建，因此不能在下载前用 `_identity_for(context)`；归属只能取渠道实例自身登记值。

## Goals / Non-Goals

**Goals:**

- 把 `wecom_bot` 的附件解析与下载纳入该渠道实例绑定 Agent 的身份作用域，覆盖长连接与回调两条路径。
- 保持身份来源单一：复用 `apply_instance` 已登记的 `bound_agent_id` / `tenant_id`，不新增第二套归属来源。
- 无绑定 Agent 时行为与变更前逐字一致。

**Non-Goals:**

- 不修改 `state_dir` 的解析下落规则或可信根定义。
- 不修改 `apply_instance` / `bound_agent_id` 契约、租户与 Agent 绑定模型、`file_cache` 机制。
- 不处理 `feishu` / `weixin` / `qq` / `slack` / `telegram` / `discord` 的同类缺陷（见 Decisions D3）。
- 不搬运或改写历史上已错位的附件文件（见 Migration Plan）。

## Decisions

### D1：在渠道侧包裹解析，而不是收紧 `state_dir` 的缺省下落

选择在 `wecom_bot_channel._build_context` 外层施加身份作用域。

- **备选 A：让 `state_dir.tmp_dir()` 在无身份时失败关闭。** 拒绝。`tmp_dir` 有大量启动期与后台调用点（skill 同步、scheduler boot 等）本就合法地无 agent 身份，收紧会把既有可用路径一并打断，超出本 change 的范围与风险预算。
- **备选 B：把 channel 实例引用传进 `WecomBotMessage`，由 `_get_tmp_dir` 自行解析。** 拒绝。让消息对象反向依赖渠道实例，耦合方向与既有分层相反，且需为每个渠道各写一份。
- **备选 C（采用）：在 `_build_context` 外层 `with use_identity(RuntimeIdentity(...))`。** 与 `chat_channel.py:203` 的既有范式一致，改动点集中、可被单测直接钉住，且天然覆盖构造期的图片解密与 `prepare()` 的文件下载。

### D2：身份来源取渠道实例登记值，不采信 context 自报

身份由 `getattr(self, "bound_agent_id", "")` 与 `getattr(self, "tenant_id", "")` 构造。理由：`_build_context` 执行时 context 尚未产出，且 `tenant-resource-isolation` 已确立「请求自报的 Agent 标识 MUST NOT 作为授权来源」；渠道实例登记值是该实例的权威归属。

### D3：本 change 只覆盖 `wecom_bot`

其余 6 个渠道存在同一缺陷，但本 change 只声明并修复 `wecom_bot`，避免把跨渠道改动与单一根因的验收混在一起。其余渠道在后续变更中按同一模式处理，且本 change 的交付表述 MUST NOT 声称已修复它们。

### D4：无绑定 Agent 时保持原语义

`bound_agent_id` 为空时仍构造身份并进入作用域（`agent_id=None`），使 `state_root` 沿用既有的默认下落逻辑。这是刻意的：传统单实例渠道依赖该下落，改变它属于 D1 备选 A 的风险。据此，`tenant-resource-isolation` 新增要求中「未登记绑定 Agent 时保持既有语义」的场景是对既有部署的保护，不构成新的放宽。

## Risks / Trade-offs

- **作用域未被真正生效**（例如某条接收路径绕开 `_build_context`）→ 以「长连接与回调落点一致」的场景逐路径钉住；实现后复核 `_build_context` 的全部调用点。
- **误把全局默认工作区当作合法落点** → 该路径正是当前缺陷的表现，测试以「不落在全局默认智能体 workspace」为断言方向，而非仅断言落在某个目录。
- **作用域泄漏到 `self.produce(context)` 的路由** → `use_identity` 只包住解析，`produce` 在其外调用；实现时以代码结构保证，回归由既有渠道测试覆盖。
- **单实例语义被无意改变** → 以「传统单实例渠道保持既有语义」作为反向控制场景，防止修复退化为「一律拒绝」。

## Migration Plan

1. 先落失败测试（见 tasks），确认在修复前失败、修复后通过，且反向控制场景在两个状态下都通过。
2. 部署即生效，无 schema 变更、无配置变更、无需数据迁移；`bound_agent_id` 为空的部署行为不变。
3. **回滚**：还原该处身份作用域即可，无持久化副作用。
4. **历史错位文件**：不自动搬运。运维侧把已落在全局默认智能体 `tmp/` 下的渠道附件手工归位到对应租户 Agent 的工作区；本 change 不读写、不改写这些文件。

## Open Questions

无。其余渠道的处理顺序属实施排期，不影响本 change 的规范、方案与任务分解。
