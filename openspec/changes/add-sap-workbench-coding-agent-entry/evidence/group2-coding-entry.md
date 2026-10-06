# 证据：第 2 组（对话切换到平台编码智能体入口）

日期：2026-10-05。改动文件：`Scene/sap_workbench/frontend/workbench.js`、`frontend/workbench.css`、`backend/runtime.py`、`backend/http.py`、`tests/test_sap_workbench_frontend.cjs`、`tests/test_sap_workbench_native_embed.py`。

## 目的

把页内对话从「场景自管的每绑定 Bun/OpenCode 引擎」换成「平台既有的编码智能体入口
（`channel/web/fork/handlers/coding.py` 的 `CodingSessionService`）」。这样打开工作台不再
等待冷启动，也没有百秒级引擎超时；SAP 能力由项目产物（第 3 组）承载，导航由按会话解析的
服务端通道（第 4 组）承载。

## 职责重新划分

| 资源 | 归属 | 说明 |
|---|---|---|
| 场景会话 `POST /api/scenes/sap-workbench/sessions` | 左侧 SAP 画面 + 凭据边界 | 只做轻量登记，不再启动引擎 |
| 页内对话 `POST /api/coding/sessions` | 平台编码入口 | 预留/打开会话并返回 `iframe_url`；打开动作不发提示词、不调模型 |

## 实现

    20|### 配置投影（2.1）

`backend/http.py` 的 `_coding()` 经平台授权链（`_require_tenant_agent_binding` /
`_require_private_owner` / `_require_agent_action(use)`）解析 `coding_agent_id` 对应智能体，
读取其 `coding_project_dir` 作为项目目录唯一来源。场景不再自管项目绑定；`_projection()`
把该结果以 `coding` 字段返回（含 `project_dir`）。

### 页内对话经平台入口（2.2）

`frontend/workbench.js`：

- `openCodingSession(session, requestId)`：
  - 有新 `request_id` ⇒ `POST /api/coding/sessions`（预留一个会话）；
  - 恢复历史 ⇒ `GET /api/coding/sessions/{coding_session_id}/open?agent_id=…`。
- `mountPlatformCode()`：取 `payload.iframe_url`，经 `codingFrameUrl()` 附加
  `rsm_embed=1` / `rsm_parent_origin` / `rsm_channel` 后挂载为 iframe；
  **打开动作不发送提示词、不调用模型**。
- `codingRequest()` 把拒绝当作人话上浮：`coding_disabled` / `coding_upstream_unavailable` /
  `coding_not_linked` / `coding_invalid_request` 各有本地化文案。

`backend/runtime.py` 的 `projection()` 在 `display_mode == 'iframe'` 时**不再下发**
`iframe_url` 与任何令牌，只返回 `binding_id` / `remote_session_id` / `coding_session_id` /
`agent_id` / `display_mode` / `sap_url` / `gui_automation: False` / `control: 'manual'`。原因：
客户端必须自行经编码入口解析嵌入地址，投影里再放一份地址会诱使调用方跳过「预留」这一步，
从而拿到一个没有会话的 frame。

    40|### 历史与 attach（2.3）

平台编码入口自身维护会话列表与同步；用户在 OpenCode 内切换/新建的会话，iframe 经
`rsm.opencode.session` 回报新 `session_id`，前端 `attachCodingSession()` 调
`POST /api/coding/sessions/attach`（body 含 `agent_id` / `source_session_id` /
`external_session_id`），把新上游会话登记到当前绑定的历史下。`attachSent` 去重，同一挂载
内同一外部会话只登记一次。

### coding 类型约束（2.4）

约束仍由平台入口承担，本次未放宽：

- 非编码智能体：`channel/web/fork/handlers/coding.py:_require_coding_profile()` 对
  `not profile.is_coding` 返回 400 `coding_invalid_request`；
- 编码智能体不从普通运行路径启动：`tests/test_coding_agent_routes.py::
  test_a_coding_agent_cannot_become_a_generic_default`、
  `::test_a_forged_normal_send_to_a_coding_agent_is_refused`。

### 就绪/重试状态机（2.5）

移除「引擎启动进度轮询」与 `opencode_start_timeout` 文案，改为：

- `showCodingState(text, {retry})` / `clearCodingState()` / `markCodingReady()`；
- 挂载前 `sessionPreparing`，等待 iframe 就绪时 `codingLoading`；
- 短超时内未收到 `rsm.opencode.ready` ⇒ `codingNotReady` + 重试按钮；
- 重试 `retryCodingSession()` 用同一个 `binding.requestId` 重跑 `mountCode`，不新建会话。
- 关闭对话清空状态（`clearCodingState`），避免下次打开看到过期提示。
- **就绪等待单飞（1.4）**：每次挂载取递增的 `codeMount`，`unmountCode()` 先自增使在途挂载失效；`mountPlatformCode` 在 `open` 返回后与超时回调里核对 `mount === codeMount`，被取代的挂载不追加 iframe、不武装第二个超时。重试仍复用同一 `requestId`，因此不会新建会话。用例：`two retry clicks that overlap mount one frame and arm one readiness wait`（把该判断去掉即复现「2 个 iframe」，确认断言有效）。

## 测试

| 范围 | 结果 |
|---|---|
| `tests/test_sap_workbench_frontend.cjs` | **62 passed** |
| `tests/test_sap_workbench_native_embed.py` | **17 passed** |
| `tests/test_sap_workbench_plugin_bridge.py` | **27 passed** |

    70|前端新增用例：

- `the conversation pane names its coding state, offers one retry and clears on close`
- `resuming a history entry reopens its own coding session instead of creating one`
- `a conversation started inside the embed is registered once, for this mount only`
- `a refused coding open states the reason and the retry resumes the same request`
- `two retry clicks that overlap mount one frame and arm one readiness wait`

iframe URL 断言覆盖 `rsm_embed=1` / `rsm_parent_origin` / `rsm_channel` 三个参数。
`test_sap_workbench_native_embed.py` 的
`test_native_projection_names_the_coding_session_and_withholds_every_grant` 断言
`coding_session_id` / `agent_id` 下发、令牌与地址不下发。

## 回滚

旧路径仍保留：`display_mode !== 'iframe'` 时 `mountCode()` 走 `mountLegacyCode()`（自管引擎、
一次性 grant 表单投递），服务端投影仍下发 bootstrap/view 令牌。第 7 组统一处置退役与开关。

## 未覆盖 / 下一步

- 未在**运行中的真实平台服务**上跑通「预留 → 挂载 → attach」全链路；属第 8 组端到端。
- 「同项目普通编码会话不调用 SAP 工具」的权限收窄属第 5 组。
- 旧 `opencode_start_timeout` 仍在 `store.py` 的合法错误码集合中（兼容旧库读取），
  runtime 已不再产生它；随第 7 组一并清理。
