# 证据：第 5 组（工具可见性收窄）

日期：2026-10-05。改动文件：`agent/coding/permissions.py`（新增）、`agent/coding/sessions.py`、`agent/coding/opencode.py`、`channel/web/fork/handlers/coding.py`、`Scene/sap_workbench/backend/project_toolkit.py`、`Scene/sap_workbench/project/plugins/rsm-sap-workbench-navigation.js`、`Scene/sap_workbench/frontend/workbench.js`、`tests/test_coding_session_routes.py`、`tests/test_coding_session_lifecycle.py`、`tests/test_coding_session_sync.py`、`tests/test_sap_workbench_project_toolkit.py`、`tests/test_sap_workbench_project_plugin.cjs`、`tests/test_sap_workbench_frontend.cjs`、`scripts/verify_coding_upgrade.py`。

## 目的

项目目录内的插件与 skill 对该项目的**所有**会话可见，包括用户自己发起的普通编码对话。
任务 3.2 让 SAP 工具成为一个普通的项目插件工具，因此它默认也会出现在普通会话的工具面里。
本组把 `sap_transaction_open` 收窄到**场景发起的会话**，并明确：这只是减少误调用，**不是**
授权依据——服务端仍按会话归属逐次校验（第 4 组）。

## 机制（逐行核对 `rsmCode/opencode`）

OpenCode 只在一种情况下会把工具从模型的工具面里去掉：

- `packages/opencode/src/permission/index.ts:204` `disabled()`：规则集里存在
  `pattern: "*" && action: "deny"` 的匹配项时，工具被隐藏；
- `packages/opencode/src/session/llm/request.ts:210` `resolveTools()` 用
  `merge(agent.permission, session.permission)` 求值，`findLast` 决定优先级
  （`permission/index.ts:29` `evaluate()`）。

也就是说：**项目级规则**进 `agent.permission`，**会话级规则**后合并、优先级更高。于是

| 层 | 规则 | 效果 |
|---|---|---|
| 项目配置 `.opencode/opencode.json` | `permission.sap_transaction_open = "deny"` | 该项目所有会话默认看不到 SAP 工具 |
| 场景会话（平台入口下发） | 会话级 `{sap_transaction_open, *, allow}` | 只有这个会话重新看到工具 |

`create_session` 的 `CreateInput` 与 `PATCH /session/:id` 都接受 `permission` 规则集
（`packages/opencode/src/session/session.ts:260`、`server/.../groups/session.ts:50`），
规则集随会话持久化，重开读回。

## 实现

### 服务端定义的权限档案（`agent/coding/permissions.py`）

`PROFILES = {"sap_workbench": ({...allow sap_transaction_open...},)}`。**规则集在服务端**：
调用方只能**命名**一个档案，不能提交规则内容。未知的非空名字按 `coding_invalid_request`
（400）拒绝，而不是静默放行。命名与规则分离，客户端就没有"给自己授权任意工具"的入口。

### 会话级 allow 下发

- `CodingSessionService.reserve(..., permission_profile=...)`：解析档案（未知名字 400），
  在创建调用里带上 `permission`（`OpenCodeClient.create_session` 新增该参数）。
- `CodingSessionService.attach(..., permission_profile=...)`：用户在 OpenCode 内新建/派生的
  会话是**没有**该档案地创建的，采纳时用 `OpenCodeClient.set_permission`
  （`PATCH /session/{id}`）把规则集补上去；这一步在写链接之前，失败即整体失败、不落链接。
- `open` 保持只读：场景会话都经 `reserve` 创建，规则集已随会话持久化，重开不需要再写。
- handler（`channel/web/fork/handlers/coding.py`）从 body 读 `permission_profile`，
  只取名字；`_permission_profile()` 对非字符串输入归零。

### 项目级默认 deny

`Scene/sap_workbench/backend/project_toolkit.py` 的 `_ensure_hidden_tool()` 把
`{sap_transaction_open: "deny"}` 合并进 `<project>/.opencode/opencode.json`：

| 现状 | 结果 | 说明 |
|---|---|---|
| 文件不存在 | `installed` | 新建，带 `$schema` |
| 是程序化写入且已含 deny | `unchanged` | 幂等 |
| 纯 JSON、无该键 | `installed` | **合并**：保留 `model` 等其他键 |
| 纯 JSON、键已被设成非 deny | `user_set` | 不覆盖用户选择 |
| JSONC（含注释）或 `permission` 是整串动作 | `unmergeable` | 不盲改（会毁注释），如实报告 |

`MUST NOT 静默覆盖用户改动` 在这里同样成立：能合并就合并，合并不了就报差异，界面不得据此
宣称工具已收窄。

### 插件同一套规则

`rsm-sap-workbench-navigation.js` 的 `execute()` 先 `context.ask({permission:
"sap_transaction_open", ...})`。插件工具的 `ctx.ask` 用的正是
`merge(agent.permission, session.permission)`（`session/tools.ts:87`），因此"可见"与
"可调用"经过同一套规则：会话有 allow 则直接放行不再弹窗；某会话把规则设成 `ask` 就变成
一次人工确认；设为 `deny` 则工具根本不出现。空事务码在 `ask` 之前就返回。

### 场景侧

`workbench.js` 新增常量 `CODING_PERMISSION_PROFILE = 'sap_workbench'`，在 `POST
/api/coding/sessions`（新建）与 `POST /api/coding/sessions/attach`（登记页内新会话）里带上。
恢复（open）不带——会话创建时已持久化。

## 测试

| 范围 | 结果 | 覆盖 |
|---|---|---|
| `tests/test_coding_session_routes.py` | **46 passed** | 命名档案随创建下发；不带档案则无 allow；未知档案 400 且不预留/不创建；attach 把档案写到被采纳会话 |
| `tests/test_coding_session_lifecycle.py` + `..._sync.py` + `tests/test_opencode_client.py` | passed | 服务层签名与调用记录；客户端仍只在有档案时带 `permission` |
| `tests/test_sap_workbench_project_toolkit.py` | **13 passed** | 新建写入 deny；幂等 `unchanged`；用户选择 `user_set` 不被覆盖；既有 JSON 配置合并且保留其他键；JSONC `unmergeable` 且字节不变 |
| `tests/test_sap_workbench_project_plugin.cjs` | **7 passed** | 新增用例断言 `ask` 以 `sap_transaction_open` 调用、空事务码不 ask |
| `tests/test_sap_workbench_frontend.cjs` | **61 passed** | 创建/attach 请求体带 `permission_profile` |

其余 SAP 回归：`test_sap_workbench*.py` 145 passed，另有 2 个既有的固定端口 9911
被运行中的应用占用导致的失败（`screen gateway startup failed`），与本次改动无关。

## 5.2 的结论（如实）

- **普通编码会话不呈现场景 SAP 工具**：项目配置 deny + 该会话没有会话级 allow ⇒
  `resolveTools` 把工具移出工具面。由 `test_no_permission_profile_leaves_the_session_without_one`
  与 toolkit 的配置断言共同证明。
- **直接构造调用仍被服务端拒绝**：`tests/test_sap_workbench_plugin_bridge.py`（27 项）覆盖
  未归属 403、未运行 409、授权撤销、身份伪造 400、非 iframe 等；工具即使可见也拿不到别人的
  浏览器或凭据。
- **权限规则不是授权依据**：档案命名权在调用方，但 allow 只影响工具**是否出现**；每次执行
  仍走服务的归属解析与授权链。

## 未覆盖 / 下一步

- 未在运行中的真实 OpenCode 服务上观察"普通会话工具面里没有 `sap_transaction_open`、场景
  会话里有"这一步；属第 8 组工具面实测。
- `user_set` / `unmergeable` 两种结果只做到如实报告，界面尚未把该状态呈现给管理员（界面
  呈现属第 6 组的能力标注）。
