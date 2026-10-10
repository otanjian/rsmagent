# 证据：第 4 组（场景中介的业务数据通道 `sap_data_call`）

日期：2026-10-06。改动文件：`Scene/sap_workbench/backend/mcp_business.py`（新增）、`backend/mcp_login.py`、`backend/mcp_runtime.py`、`backend/mcp_worker.py`、`backend/runtime.py`、`backend/http.py`、`backend/configuration.py`、`channel/web/route_registry.py`、`channel/web/web_channel.py`、`frontend/workbench.js`、`backend/project_toolkit.py`、`agent/coding/permissions.py`、`project/plugins/rsm-sap-workbench-navigation.js`、`project/skills/sap-workbench/{SKILL.md,references/boundaries.md}`、`tests/test_sap_workbench_business_channel.py`（新增）、`tests/test_sap_workbench_project_plugin.cjs`、`tests/test_sap_workbench_project_toolkit.py`、`tests/test_coding_session_routes.py`、`tests/test_sap_workbench.py`、`tests/test_sap_workbench_frontend.cjs`。

## 目的

导航通道（`evidence/group4-session-resolution.md`）只送导航指令。业务数据原本没有通道：模型要么拿不到凭据，要么拿到凭据（把 SAP 账号口令交给模型与插件进程，且模型能自己选连接）。本通道把"凭据"和"业务意图"分开——模型只描述**想要什么业务数据**，凭据与连接由服务端在会话归属确定之后附加。

与导航通道同一条所有权规则：插件只能出示 OpenCode `sessionID`，请求体声明的用户/绑定/SAP 账号一律拒绝。

## 前提被逆转（如实记录）

任务 3.5 的原始前提是**只读**。本轮按用户决定改为**读写皆可**，并放开 BAPI 提交。理由：只读并不构成安全边界——能做什么由 SAP 账号自身的授权决定，动词表收窄只让"本该写"的场景拿不到能力，却挡不住任何越权。边界因此改为**参数受限 + 凭据不交给模型 + 全程审计**。代价见下方「失败语义」。

## 参数边界（`mcp_business.py`）

校验发生在**写入 MCP worker 之前**，因此被拒的调用不会连上 SAP。

| tool | 形状 | 上界与拒绝 |
|---|---|---|
| `read_table` | `{table_name, fields?, where?, row_count, row_skip?}` | 表名/列名为单个 SAP 标识符；`fields` ≤ 64 且不重复；`row_count` 1–500；`row_skip` 0–100000；`where` ≤ 512 且不含语句分隔符、注释起始或控制字符 |
| `run_query` | `{sql_query, row_count}`（键集精确） | 必须以 `SELECT` 开头（单条只读语句）；`sql_query` ≤ 2000；`row_count` 1–500 |
| `call_rfc` | `{function_name, parameters_json?}` | 函数名限普通名或 `/NS/NAME` 形式；`parameters_json` 须为 JSON 对象且 ≤ 8000 字节 |

- 连接固定为 `sap-pyrfc`，`connection` 由插件常量给出并**由服务端独立校验**；身份键 `connection_id`/`user`/`password`/`client`/`host`/`url`/`tenant_id`/`session_id` 出现在参数里一律拒绝（与 `SapMcpLogin._call` 同向的纵深防御）。
- **ADT 系统变更工具不在工具面内**：改源码、激活、传输创建/放行都无法经本通道到达。本通道面向业务数据，不是改 SAP 系统本身。
- `run_query` 的只读限制是**通道自身的界**，不是 SAP 账号授权的替代：账号有哪些授权仍由 SAP 决定。

## 失败语义（`unknown` 而非 `failed`）

`call_rfc` 可能调用会写业务数据的 BAPI。这类调用与查询语义不同：传输失败或被拒时，SAP **可能已经写入**。因此 `sap_data_call` 不在 `READ_ACTIONS` 内，失败一律记为 `unknown`；记为 `failed` 等于声称"确认 SAP 什么都没做"，而传输层结果无法确立这一点。界面与 skill 均要求人工核对。

反之，传输成功也只是传输成功：返回值是 MCP 传输结果，**不等于**单据已创建/已过账/已审批，skill 与工具描述均禁止据此声称业务已完成。

## 本次修正的三处实现缺陷

1. **`FUNCTION` 正则与自身文档矛盾**：注释举 `/SAPBOQ/...` 为例，但正则要求首字符为 `[A-Za-z]`，带前导 `/` 的命名空间函数模块被一律拒绝。改为"普通名或 `/NS/NAME`"两选一。
2. **工具越界被报成"连接不可用"**：`adt_activate` 这类不在工具面内的动作原返回 `mcp_connection_unavailable`，把"这个通道不做这件事"说成"连不上系统"。现区分：连接不属于本通道 ⇒ `mcp_connection_unavailable`；工具不在允许集内 ⇒ `mcp_action_forbidden`。
3. **载荷形状错误仍扣配额、写审计**：`dispatch` 原先在 `admit_action` **之后**才校验 `sap_data_call` 的载荷形状，于是一个根本不是合法调用的请求体也会消耗一次配额、写一条 `action.dispatch` 并留下账本行。已按 `transaction_open` 的既有次序把形状校验移到账本**之前**——形状错误不产生任何副作用。

## 测试

| 范围 | 结果 |
|---|---|
| `tests/test_sap_workbench_business_channel.py`（新增） | **45 passed** |
| `tests/test_sap_workbench_project_plugin.cjs` | **14 passed** |
| `tests/test_sap_workbench_project_toolkit.py` | **14 passed** |

新用例覆盖（要点）：

- **参数校验**：上表每一类接受与拒绝各成参数化用例；身份键、非法表名/列名、重复列、超界行数、语句分隔符与注释起始、非 JSON、非对象参数、非 `SELECT` 前缀均拒绝。
- **系统变更工具不可达**：对 `adt_write_source`/`adt_activate`/`adt_create_transport`/`adt_release_transport`/`bw_create_object`/`healthcheck` 逐一断言拒绝。
- **拒绝不启动 worker**：`call_business` 校验在 `_start()` 之前，被拒调用后 `process is None` 且 `resolve_mcp_credentials` 未被调用——"拒绝"不会先建好凭据连接再回退。
- **账本/配额/审计仍然生效**：成功调用记 `sap_data_call`/`succeeded`；授权撤销后账本为空且未调用 MCP；业务失败记 `unknown`；载荷形状错误在账本之前被拒。
- **iframe 模式可用**：`sap_data_call` 属后端动作，不被 `iframe_page_control_unavailable` 拦下（它不驱动左侧画面）。
- **桥接端点的调用方与归属拒绝**：无凭据 ⇒ `bridge_unauthorized`；未归属 ⇒ `session_not_bound` 403；未运行 ⇒ `session_not_running` 409 且账本为空；请求体含 `user_id`/`binding_id` ⇒ `invalid_request`。
- **插件契约**：参数面只有 `tool`/`arguments`，`connection` 由插件常量给出；只发送五个字段；拒绝码原样回报且不表述为业务结论；账本标识区分**参数**（读 EKKO 与 EKPO 不同、键序不影响、同一参数共享标识以去重）、权限键为 `sap_data_call`。

## 环境性失败（均为既有问题，非本次改动引入）

沿用 `evidence/group4-session-resolution.md` 已记录的同类现象，本轮在同一环境复现并额外确认：

1. `tests/test_sap_workbench.py::test_session_rejects_invalid_request_key_and_password`、`::test_platform_agent_routing_metadata_is_ignored`：`OSError [Errno 10048]` 绑定 `127.0.0.1:9911` 失败。9911 由**正在运行的真实应用**（`python -u app.py`，pid 748 于 7:06 启动，早于本次改动）占用。设 `SAP_WORKBENCH_PROXY_PORT=19911` 后**两用例均通过**（`test_sap_workbench.py` 全文件 103 passed），确认与本改动无关。
2. `tests/test_sap_workbench_mcp_data.py` 6 个参数化用例 ERROR：`ValueError: the environment variable is longer than 32767 characters`。这些用例刻意用超长环境变量投喂 worker，Windows 的单进程环境块上限为 32767，Linux 下不触发。平台限制，与本改动无关。
3. `tests/test_sap_workbench_display_deployment.py`、`..._display_process_cleanup.py` 收集期 ERROR：`module 'os' has no attribute 'killpg'`，POSIX 专有 API。

定向回归（排除上述环境项）：`runtime` / `dispatcher` / `mcp_runtime` / `mcp_data` / `sap_workbench` / `access` / `legacy_gate` / `iframe_navigation` / `bridge_boundary` / `purchase_order_bridge` 共 **285 passed**。

4. **`sap-abap` 网关（127.0.0.1:8110）在 `sap_connect` 上失败，且失败是致命的**。用原始 MCP 客户端（而非经场景包装）直接对两个网关发 `sap_connect`，同一账号 `S2385` / client `200`：

   | 网关 | 原始响应 |
   | --- | --- |
   | `sap-pyrfc`（8200，python） | `isError: false`，返回 `connection_id`，`adt.user=S2385`、`client=200`、`ping.status=ok` |
   | `sap-abap`（8110，`node src/index.mjs`） | `isError: true`，正文仅 `MCP error -32000: Connection closed` |

   `sap-abap` 的 MCP 握手与 `tools/list` 都正常（70 个工具），失败只发生在 `sap_connect` 的工具调用上；进程本身不退出。场景登录是**失败即整体回滚**的（`connect()` 内 `close()` 后抛 `mcp_login_failed`），因此只要 `sap-abap` 处于启用状态，**整个场景的 MCP 通道都不可用**——不只是业务通道，原有的采购订单读取同样被阻断。

   三组对照（场景自身的 `SapMcpLogin.connect`）：仅 `sap-pyrfc` → **OK**；仅 `sap-abap` → `mcp_call_failed`；两者同时 → `mcp_call_failed`。

   另需注意：业务通道的 `BUSINESS_TOOLS = {read_table, run_query, call_rfc}` 是按 **`sap-pyrfc` 的工具名**定义的；`sap-abap` 上对应的工具叫 `adt_read_table`，不在该集合内。因此**业务通道实际上只走 `sap-pyrfc`**，`sap-abap` 对本通道并不提供取数能力。

   配置层面：`configuration.py` 只允许逐连接改 `enabled`（其余字段必须与固定预设一致），并要求至少一个连接启用（`any(c["enabled"])`）。所以「停用 `sap-abap`、保留 `sap-pyrfc`」是一条**合法且最小**的可用性恢复路径——但会改动用户已持久化的场景配置，需用户确认后再做。

## 现场验证（已做）

应用服务已按指示重启（旧 pid 748 → 新 pid 8184，`RDAI-AppWatchdog` 每 20 秒守护），端点生效后对**真实 SAP S/4H 系统、真实解密凭据**执行：

| 调用 | 参数（模型侧只给这些） | 结果 |
| --- | --- | --- |
| `read_table` | `EKKO` + 显式字段 `EBELN,BSART,LIFNR,BEDAT,WAERS` | 4 行，含 `4500000388` / `NB` / `USSU-VSF01` / `20190929` / `USD`，并带 `column_meta` 类型与描述 |
| `read_table` | `EKPO` + 显式字段 `EBELN,EBELP,MATNR,MENGE,NETPR` | 4 行，含 `4500000004` / `00001` / `MZ-RM-M550-01` / `35.000` / `669.08` |
| `run_query` | `SELECT COUNT(*) AS ROW_TOTAL FROM EKKO` | `ROW_TOTAL = 2694` |

**本次现场验证覆盖的边界（严格区分）**：

已证实：场景持久化连接配置 → 凭据解密 → 以内存方式注入 MCP worker → `sap-pyrfc` 登录真实 SAP → 业务取数 → 结果回传。调用侧只有业务参数（表名/字段/语句），**没有出现任何连接名、主机、客户端号或凭据**，且 `mcp_business.py` 的字段/行数/语句校验在实际取数路径上被执行。该次运行需要在内存中排除 `sap-abap`，原因见上「环境性失败」第 4 条。

**桥接 HTTP 入口的现场验证（对运行中的 9900 服务实发请求）**：

用本机的 coding-service 凭据（`agent.coding.resolve_settings()`）直接对 `POST /api/scenes/sap-workbench/bridge/data` 发请求，逐道闸门实际命中：

| 场景 | 实测响应 |
| --- | --- |
| 不带 `Authorization` | `401 bridge_unauthorized` |
| 口令错误 / 用户名错误 | `401 bridge_unauthorized` |
| 凭据正确但请求体多一个 `user_id`（试图指定归属） | `400 invalid_request` |
| 凭据正确但缺 `call_id` | `400 invalid_request` |
| 凭据正确但 `arguments` 不是对象 | `400 invalid_request` |
| 凭据正确但空请求体 | `400 invalid_request` |
| 凭据正确、`session_id` 无法解析 | `403 session_not_bound` |
| 凭据正确、`session_id` 是库中真实的 `ready` 会话 | `409 session_not_running` |

这几条一起证明了：**先认证调用方、再校验报文形状、最后才解析会话归属**这个顺序在真实进程上成立；调用方即使拿着合法的服务凭据，也无法通过多传 `user_id` 来指定「读谁的 SAP」，而且无法解析的会话是被拒绝而不是就近借用别的绑定。

**仍未证实的只剩一步：正向取数**。`409 session_not_running` 说明该会话在内存中的 runtime 已随应用重启消失，而 runtime 只能由应用自己启动。因此「账本落账 + worker 拉起 + 真实取数」这一段仍未经真实会话跑通——需要先在 UI 里真正开一个会话。

**为什么没有用 UI 去开**：本机的自动化浏览器是**远程实例**，与运行应用的主机不共处一地——它能加载 `https://example.com`（有外网），但访问 `127.0.0.1:9900` 与 `rd.rsmxm.com` 一律返回 `chrome-error://chromewebdata/`。因此无法用该浏览器驱动左侧面板启动会话。你截图里的画面来自你本机的浏览器，不是这个实例。

## 未覆盖 / 下一步

- **正向取数未经真实会话跑通**：桥接入口的认证、报文、归属三道闸门已在真实进程上实测命中（见上节表格），但「账本落账 → worker 拉起 → 真实取数 → 结果回传」仍缺一次带活跃 runtime 的调用。前置条件是先在 UI 里真正开一个会话，然后重放同一条请求（`connection=sap-pyrfc`、`tool=read_table`）。
- **即便开了会话，正向调用当前也会失败**，因为场景登录被 `sap-abap` 拖垮（上「环境性失败」第 4 条）。要拿到一次成功的端到端记录，必须先处理 `sap-abap`。
- `call_rfc` 的**业务**正确性（BAPI 参数语义、返回结构、是否需要 `BAPI_TRANSACTION_COMMIT`）未验证；通道只保证传输与参数边界。
- 现场取数验证走的是一条**内存中排除 `sap-abap`** 的临时连接组合，用以证明取数链路与凭据注入可用；**未改动持久化的场景配置**。
- 诊断脚本里 `resolve_settings()` 一度报 `enabled=False`，而运行中的应用显然按 `enabled=True` 处理（否则会返回 `503 bridge_unavailable` 而不是 400）。已查明原因，非缺陷：`enabled` 来自配置文件的 `opencode` 段，而 `password` 来自环境变量 `RSM_OPENCODE_PASSWORD`。诊断脚本以不同工作目录运行，读到的是不含该段的配置，但环境变量仍在，所以凭据对、`enabled` 读成了默认关闭。
