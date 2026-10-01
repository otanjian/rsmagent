# 阶段二证据 — 本地目录接通（任务 2.1 – 2.6）

Change: `fix-desktop-local-context-and-tool-calls` · 环境见 `phase-1.md`

一条真实链路按数据流排列如下。每一段都注明落点与钉住它的用例；用例名与执行结果见 `verification.md`。

| 段 | 落点 | 行为 | 证据 |
|---|---|---|---|
| 选择目录 | `channel/web/static/js/console.js:7274` | 原生选择只产生本机 grant；随后调用 `CowDesktopHost.bindContext` | `tests/test_desktop_workspace_menu_frontend.cjs` |
| 桥适配 | `channel/web/static/js/fork/desktop-host.js:281` | 暴露 `bindContext`；无宿主时 `no_host` 兜底，不抛异常 | `tests/test_desktop_host_frontend.cjs` |
| 桥校验 | `desktop/src/main/remote-preload.ts:98`、`desktop/src/main/remote/host-bridge.ts:261` | 校验 scope 各字段与 `installationId`/`label`/`agentId`/`businessSessionId`/`contextNonce`，缺一即 `invalid_request` | `tests/test_desktop_local_files_bridge.cjs` |
| 四步确认 | `desktop/src/main/local-files/bind-context.ts`、`desktop/src/main/remote/binding-setup.ts:186` | ①`POST /api/desktop/devices`（原生 bearer，返回**服务端** device id）②`POST /api/desktop/workspaces`（label + grant_version）③`POST /api/desktop/bindings`（配对 Web 子会话 Cookie）④`PUT /api/desktop/bindings/{id}/workspaces/{ws}`；generation/范围/活动 grant 仍由 `bindContext` 判定，页面无法绕行 | `tests/test_desktop_local_files.cjs`（generation 过期、无 grant、四步顺序与"无 workspace id 不算成功"） |
| 启动设备连接 | `desktop/src/main/remote/remote-host-ipc.ts:306-313` → `local-read-assembly.ts:146` | 确认成功即绑定 workspace→本机 grant 映射并启动设备连接（不是应用启动时就连接） | `tests/test_desktop_local_read.cjs`（`bindDevice starts one client…`、`a workspace with no server id or no live grant is not bound`、`dispose drops every workspace and the connection`） |
| 设备连接 | `desktop/src/main/remote/device-client.ts`、`ws-client.ts` | 用原生 bearer 连 `wss://<origin>/api/desktop/connect`，先 hello 再应答；心跳、空闲超时重连、陈旧 epoch 丢弃、同 request id 不重复应答、结束时不重发 | `tests/test_desktop_device_client.cjs`（14 例）、`tests/test_desktop_ws_client.cjs` |
| 命令执行 | `desktop/src/main/remote/device-ops.ts` + `desktop/src/main/local-files/fs-guard.ts` | 白名单 `list`/`stat`/`search`/`read_text`，按契约夹取限额，经 4 字节长度前缀 + JSON 与 `fs-guard` 进程通信；路径逃逸由 helper 拒绝 | `tests/test_desktop_local_read.cjs`（真实 helper 往返、逃逸拒绝、未知 op、截断/分页标记）、`desktop/native/fs-guard` 内 23 例 |
| 每轮引用 | `console.js:5913-5922` → `channel/web/fork/runtime.py:1728-1758` → `bridge/agent_bridge.py:1695` | `/message` 携带非秘密引用；服务端用当前身份/配对/Agent/业务 session 重验（`integrations/desktop/session_context.py:56`），失败**显式报错**而不是回落默认目录；引用通过 `_attach_desktop_context_to_tools` 注入，且每轮重置 | `tests/test_desktop_local_context.py`（`ParseReferenceTests`、`VerifyReferenceTests`、`AttachToToolsTests`） |
| 工具消费 | `agent/tools/client_files/client_files.py:128`、`:242` | 只读服务端注入的引用；无引用即 `invalid_request`；`_await_terminal` 有界等待真实终态 | `tests/test_desktop_local_context.py`（`test_model_supplied_binding_id_cannot_swap_the_target`、`test_failed_command_surfaces_the_device_error`、`test_cancelled_command_reports_cancelled`、`test_expired_command_reports_deadline_exceeded`、`test_revoked_grant_fails_before_enqueue`） |

## 逐条对应

- **2.1** 真实登记设备与可信配对身份：`ensureDevice` 用原生 bearer 刷新/建立本机设备行并取回服务端 id；
  `registerWorkspaceId` 只上报 label 与 `grant_version`（绝对路径不离开本机）；本机 `grant.id` 与服务端
  `workspace_id` 的映射由 `LocalReadAssembly` 在内存维护，执行时按服务端 workspace 查表，未知即 `stale_context`。
  未新增数据库表，也没有新的并发版本协议。
- **2.2** 占位替换与"确认后才生效"：四步序列 + `remote-host-ipc.ts:294` 的"没有 workspace id 就不是成功"；
  前端只在 `confirmed.bindingId && confirmed.workspaceId` 齐备时更新状态，取消保留原状，失败清引用并提示。
- **2.3** 每轮引用：字段为 `binding_id`/`workspace_id`/`grant_version`（无绝对路径）；缺失=普通浏览器/服务器项目，
  形状非法或校验不过=显式错误；模型参数不能替换绑定；服务器 cwd 仍按既有项目解析，未被改写。
- **2.4** 设备连接与受控操作装配：`LocalReadAssembly` 把 grant 表、helper 进程与设备连接接在一起；
  ws 明确要求 wss，只有主进程登记的 loopback 本机后端可用明文（`local-read-assembly.ts:177`）。
- **2.5** 有界等待：默认上限取契约 `read_deadline_seconds`（60s），设备无活动租约时收缩到 `offline_wait_seconds`（30s），
  命令自带 deadline 时取更小者；`cancel_event` 置位即返回 `cancelled`；超时区分 `device_offline` 与 `deadline_exceeded`；
  成功结果原样透传设备 payload（分页/截断标记不被抹平）。
- **2.6** 清理与迟到回调：引用随 `activeAgentId`+`sessionId` 记住（`console.js:7052`），选择器/会话/Agent/登出/租户切换
  调用点清理（`:7494`、`:12311`、`:21962`、`:23004`）；确认回调比对请求键，落在切换之后即丢弃（`:7292`、`:7315`）；
  服务端与主进程各自按 generation/范围/撤销拒绝过期引用；重启后内存映射为空，候选不恢复授权。
