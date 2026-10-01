# 阶段一证据 — 复现与接口核对（任务 1.1 / 1.2）

Change: `fix-desktop-local-context-and-tool-calls` · 记录日期 2026-09-29

| 项 | 值 |
|---|---|
| OS / 架构 | macOS 26.4 arm64 |
| 服务器侧 | 本仓库工作树（`build_web_app()`），Python 3.14.3（`.venv`） |
| 客户端 | 源码模式 Electron 33（`desktop/package.json` `^33.2.0`，本机 33.4.11） |
| Node | v24.14.1 |
| 本机 helper | `desktop/native/fs-guard`（Rust）已构建 `target/release/fs-guard` |
| 数据 | 合成数据；本地目录为临时目录，服务器默认目录另置不同文件 |

## 1.1 已确认的断点与现有接缝

断点来自 design.md Context（2026-09-29 08:18 的实际日志与代码复核），逐条如下。每条的"现在"列是本 change
的修复落点，"证据"列是钉住它的回归用例（全部已执行通过，见 `verification.md`）。

| # | 修复前 | 现在 | 证据 |
|---|---|---|---|
| 1 | `wsSelChooseLocalDir` 只改标签：原生返回 grant 后就写 `_wsSelState.current` | 先经 `CowDesktopHost.bindContext()` 确认（`console.js:7274+`，调用点 `:7298`），确认成功才发布引用与标签（`:7315`）；取消/失败保留原状态并提示原因（`:7330`） | `tests/test_desktop_host_frontend.cjs`（适配器面）；`tests/test_desktop_workspace_menu_frontend.cjs`（入口面）；`tests/test_desktop_local_files.cjs:44-47`（`bindContext` 拒绝过期 generation / 无活动 grant） |
| 2 | remote host IPC 的 `bindContext` 为占位，本机 `grant.id` 与服务端 `workspace_id` 没有映射 | `confirmLocalContext` / `localSetupPoster` 依序完成"登记设备 → 注册 workspace → 创建绑定 → 关联授权版本"（`desktop/src/main/remote/binding-setup.ts:186`），并坚持"没有 workspace id 就不是成功"（`remote-host-ipc.ts:294-301`） | `tests/test_desktop_local_files.cjs`；`tests/test_desktop_local_read.cjs`（映射与撤销） |
| 3 | `client_files` 创建命令后立即返回 pending 的 success | `_await_terminal` 轮询既有命令到终态（`agent/tools/client_files/client_files.py:242`） | `tests/test_desktop_local_context.py`（`test_waits_for_succeeded_and_returns_device_payload`、`test_leased_device_without_an_answer_hits_the_deadline`、`test_no_live_lease_times_out_as_device_offline`、`test_pre_cancelled_run_does_not_wait`） |
| 4 | 模型以 DSML 文本代替结构化调用后本轮照常结束 | 无结构化调用且正文含执行封装时，在写历史前抛 `ToolProtocolError`（`agent/protocol/agent_stream.py:1937-1952`） | `tests/test_tool_protocol_guard.py`（12 例） |

**设备侧消费接缝复核**（任务 2.4 的前置）：`gateway.py` 只提供 `/healthz` 与 `/api/desktop/connect`（aiohttp），
由 `python -m integrations.desktop --bind … --port 9877` 单独启动，需反向代理做 Upgrade 才能与 Web 同源
（`openspec/changes/add-desktop-remote-web-workbench/docs/ops-runbook.md`「同源网关与反向代理」）。
主进程侧的消费此前只有连接辅助函数（`device-connection.ts`），没有进程持有 socket —— 这正是本 change 补齐的部分，
见 `phase-2.md`。

## 1.2 模型出口核对

**做了什么（可复现）**：核对本轮实际请求/响应的形状，而不是先假定模型责任。

- 请求侧：`models/deepseek/deepseek_bot.py:284-286` 在 `tools` 非空时写 `tools` 与 `tool_choice`
  （默认 `"auto"`），`_convert_tools_to_openai_format` 负责把内部工具定义转成 OpenAI 形状；
  工具集为空时不写这两个字段。
- 响应侧：`AgentStreamExecutor._call_llm_stream` 逐块累积 `content` 与 `tool_calls`，`finish_reason`
  与结构化调用分开处理；正文与结构化调用都能到达同一接缝。
- 合成响应 fixture：`tests/test_tool_protocol_guard.py` 用最小 SSE 片段
  （`_content_chunk` / `_tool_call_chunk` / `_finish_chunk`）复现"有结构化调用"与"只有 DSML 正文"两种响应，
  并覆盖"分片拼接后才完整"的情形。

**结论与边界**：

- 标准结构化调用路径在本地接缝上可用：`test_structured_tool_call_still_flows` 证明合法 `tool_calls` 正常流入执行，
  `test_a_real_call_beside_a_text_marker_is_not_discarded` 证明旁边的文本标记不会作废合法批次。
- 本环境**没有**捕获真实 provider 的原始 SSE（08:18 的现场日志只有"正文后直接结束"的结果，未留原始响应），
  因此本 change **不把责任归于模型或代理**：修复落在本地出口的一致性检查（`phase-3.md`），
  并且"真实模型出口冒烟"仍记录为未完成（见 `verification.md`）。

## 未完成

- 1.2 的"实际模型出口"真机冒烟需要可用的真实 provider 与凭据，本环境不可调用 → 保持未完成，未据模拟结果宣称兼容。
