# 验收证据：add-opencode-coding-agents

本文件记录**实际执行过**的命令与观测结果。未执行的部分明确标注为「未验证」，不得把设计中的预期写成通过。

## 0. 环境与版本

| 项目 | 值 |
| --- | --- |
| 平台仓库 | `/Users/jiantan/ai_assistant/rsmagent`，分支 `rdai`，基线 commit `e37df20c` |
| OpenCode 仓库 | `/Users/jiantan/ai_assistant/rsmcode/opencode`，commit `5a8335857b`，app 版本 `1.18.31` |
| bun | `1.3.14` |
| 用户既有服务 | 平台开发服务 `http://127.0.0.1:9899`（PID 22149，`app.py`）。**未重启、未改动。** |
| OpenCode 验证实例 | 独立实例 `http://127.0.0.1:4096`（无认证）与 `http://127.0.0.1:4099`（Basic Auth）。数据目录隔离在 `/tmp/rsm-opencode-verify/data*`，与用户 `~/.local/share/opencode` 无关。 |
| OpenCode app 验证实例 | `bun --cwd packages/app dev` → `http://127.0.0.1:3000`（本地源码构建/开发服务器，非官方远程 UI） |
| 模拟平台 origin | `http://127.0.0.1:8123`（本地 bun 静态页，仅用于验证跨 origin iframe 嵌入） |

启动命令（独立验证实例，未触碰用户服务）：

```bash
XDG_DATA_HOME=/tmp/rsm-opencode-verify/data \
  bun run packages/opencode/src/index.ts serve --port 4096 --hostname 127.0.0.1
XDG_DATA_HOME=/tmp/rsm-opencode-verify/data-auth \
  OPENCODE_SERVER_PASSWORD=<本地测试口令> OPENCODE_SERVER_USERNAME=opencode \
  bun run packages/opencode/src/index.ts serve --port 4099 --hostname 127.0.0.1
```

> 说明：`4097` 被本机 ssh 隧道占用，服务启动失败（`ServeError`），改用 `4099`。

## 1.1 V2 固定 ID 创建 / 读取 / 活跃状态 / 重命名 / interrupt / 删除

目录参数使用 `?directory=<绝对路径>`（源码确认 `packages/opencode/src/server/routes/instance/httpapi/middleware/workspace-routing.ts:87` 读取 `directory` 查询参数或 `x-opencode-directory` 头，缺省 `process.cwd()`）。

| 操作 | 请求 | 实际响应 |
| --- | --- | --- |
| 创建（自定义 ID） | `POST /api/session` `{"id":"ses_rsmverify00000000000000001","location":{"directory":"..."}}` | `200`，`{"data":{"id":"ses_rsmverify00000000000000001","projectID":"global","title":"New session - ...","time":{"created":1789887569063,"updated":1789887569063},"location":{"directory":"/tmp/rsm-opencode-verify/project"},"subpath":"tmp/rsm-opencode-verify/project",...}}` |
| 同 ID 再次创建 | 同上 | `200`，`time.created` 与首次**完全一致** → 复用已有会话，不新建 |
| 读取 | `GET /api/session/{id}` | `200`，`data.title` / `data.time` / `data.location.directory`；根会话**不含** `parentID` |
| 活跃状态 | `GET /api/session/active` | `200`，`{"data":{}}`；运行中会话以 `id -> {"type":"running"}` 出现 |
| 重命名（兼容接口） | `PATCH /session/{id}?directory=...` `{"title":"平台重命名标题"}` | `200`，返回 V1 形状对象，`title` 为新值 |
| 重命名后读取 | `GET /api/session/{id}` | `200`，`data.title` 已是新标题 |
| 中止（空闲） | `POST /api/session/{id}/interrupt` | `204`，空响应体（空闲会话为 no-op） |
| 列表 | `GET /api/session?directory=...` | `200`，`{"data":[...],"cursor":{"previous":...,"next":...}}`，条目含 `title`、`time` |
| 删除（兼容接口） | `DELETE /session/{id}?directory=...` | `200`，响应体 `true` |
| 删除后读取 | `GET /api/session/{id}` | `404` `{"_tag":"SessionNotFoundError","sessionID":"...","message":"Session not found: ..."}` |
| 重复删除 | `DELETE /session/{id}` | `404` `{"name":"NotFoundError","data":{"message":"Session not found: ..."}}` → 平台可把「确认不存在」视为删除完成 |
| 省略 ID 创建 | `POST /api/session` `{"location":{...}}` | `200`，服务端生成 `ses_f4262fe22ffep5LNcS1EjTWI8f` |

ID 形状实测：

- `ses_rsm_0123456789abcdef0123456789abcdef`（平台计划使用的 `ses_rsm_<摘要>` 形状）→ `200`，被接受。
- 含空格/符号的 ID（`ses_bad id!`）→ 同样 `200`。**结论：上游不校验 ID 形状，稳定性必须由平台自己保证。**
- 指向**不存在**的目录 → 同样 `200` 创建成功。**结论：上游不校验目录存在性**，`attach` 的「真实目录」校验只能基于「远端会话 `location.directory` 与来源平台会话项目是否一致」来做，不能假设上游拒绝。

认证（`packages/opencode/src/server/auth.ts`，Basic；`username` 缺省 `opencode`）：

| 请求 | 结果 |
| --- | --- |
| 无 `Authorization` | `401`，响应头 `www-authenticate: Basic realm="Secure Area"` |
| 口令错误 | `401` |
| 用户名错误（口令正确） | `401`（用户名字符串必须完全一致） |
| 正确凭据 | `200` |

## 1.2 从平台 origin 嵌入 OpenCode 根路径

| 检查项 | 结果 |
| --- | --- |
| 本地 app 构建 | `bun --cwd packages/app dev`（`vite v7.1.4`）在本机 3000 端口渲染成功，标题 `OpenCode`，可交互元素（主页 / 新建会话 / DIR）均在快照中出现 → 是本地源码构建，不是 `app.opencode.ai` 远程 UI |
| 响应头 | `HTTP/1.1 200`、`Content-Type: text/html`、`Vary: Origin`、`Cache-Control: no-cache`。**没有** `X-Frame-Options`，**没有** `Content-Security-Policy` → 未禁止被跨 origin 嵌入 |
| 跨 origin iframe | 模拟平台页 `http://127.0.0.1:8123/` 内 `<iframe src="http://127.0.0.1:3000/">` 正常渲染 OpenCode 应用，页面快照与截图均确认 |
| SSE | 服务端日志出现多次 `message="global event connected"`，与 app 加载/重复加载对应 → 实时事件流可建立 |
| 终端 WebSocket（可交互） | `POST /pty` → `200`；`POST /pty/{id}/connect-token`（需请求头 `x-opencode-ticket: 1`）→ `200 {"ticket":"...","expires_in":60}`；`GET /pty/{id}/connect?ticket=...` 原始握手 → `HTTP/1.1 101 Switching Protocols`；随后收到 PTY 输出帧；发送掩码文本帧 `echo RSM_TERMINAL_OK` 后回显确认 `saw echo output: true` |
| 认证流程 | 浏览器侧支持 `?auth_token=<base64(user:pass)>`（`packages/app/src/utils/server.ts::authFromToken`，`src/entry.tsx:154`）或 app 自带的服务器连接界面；Basic 凭据以 `Authorization: Basic` 头发出 |

未验证 / 需在生产部署复核：

- 反向代理后的真实响应头（`frame-ancestors`、SSE 是否缓冲、WebSocket 是否可升级）——本机 dev server 无代理，必须在实际部署上复核。
- HTTPS/WSS 组合与实际域名角色（`api_url` vs `web_url`）。

补充发现（影响设计落地）：

- `packages/server/src/cors.ts::isAllowedCorsOrigin` 只放行 `http://localhost:*`、`http://127.0.0.1:*`、`oc://renderer`、`tauri://localhost`、`*.opencode.ai` 以及 `--cors` 显式列出的 origin。iframe 内 app 访问的是**自身 origin**（同源，`sameHost` 放行），因此终端 WS 不受影响；但若未来需要跨 origin 直连 API，必须显式配置 `--cors`。
- PTY 连接令牌是**一次性**的（`tickets.consume`），且必须带 `x-opencode-ticket: 1` 才能签发。

## 1.3 ChatHandler 实际页面与脚本列表、会话存储维度

`curl -s http://127.0.0.1:9899/chat`（`HTTP 200`，`236819` 字节）中实际被 `ChatHandler` 版本化替换的资源：

```
assets/js/doc-editor.js            assets/js/console.js              assets/js/scenes/index.js
assets/js/appearance.js            assets/js/workspace.js            assets/js/identity-admin.js
assets/js/todos.js                 assets/js/fragments.js            assets/js/channel-workbench.js
assets/js/functional-capabilities.js  assets/js/functional-models.js
assets/js/functional-context.js       assets/js/functional-scheduler.js
assets/js/i18n/*.js（23 个）        assets/css/console.css            assets/css/appearance.css
assets/fragments/appearance-dialog.html
```

- **载荷顺序**：`channel-workbench.js`、`functional-*.js`、`i18n/*.js`、`fragments.js` 都在 `console.js` **之前**；`console.js` 之后是 `scenes/index.js`、`workspace.js`、`identity-admin.js`、`todos.js`。新模块必须遵循同样顺序（defer、在 `console.js` 之前）。
- `assets/js/external-connections.js` 在页面中处于注释内（`-->`），未被加载 → 佐证「不能只改拆分文件」。
- `ChatHandler.GET` 位于 `channel/web/fork/handlers/pages.py:40`，硬编码资源清单在 `pages.py:58-61`，`favicon.ico` 等由 `AssetsHandler` 直接返回。

`get_conversation_store()` 现状（`agent/memory/conversation_store.py`）：

- `_resolve_global_binding(workspace_root) -> (db_path, agent_id)`（`:2380`）：所有 Agent 收敛到**同一个**默认 Agent 数据库文件，按 `agent_id` 维度区分；默认 Agent 使用空字符串 `""`（历史未打标行兼容），其他 Agent 使用各自 id。
- `ConversationStore.__init__(db_path, agent_id=None)`（`:512`），`_dimensions()`（`:536`）产出 `agent_id`/`tenant_id` 等维度；`list_sessions`（`:1754`）的 WHERE 起始子句为 `owner = ?` + `dimension_clause(..., keys=("agent_id","tenant_id"))`。
- 结论：coding 关联表应挂在**同一**数据库上，通过 `get_conversation_store(profile.workspace)` 取得；不得另建数据库，不得重写 `sessions` 主键。

## 2. 类型、配置及入口边界

### 2.1 / 2.2 类型字段、序列化与白名单投影

- `agent/registry.py`：`AGENT_TYPE_NORMAL`/`AGENT_TYPE_CODING`/`AGENT_TYPES`；`AgentProfile.agent_type`、`coding_project_dir`、`is_coding`；`to_dict()` 仅在非 normal 时写 `agent_type`（旧档案读取不产生写入副作用），`coding_project_dir` 仅在存在时写；新增 `AgentRegistry.normal(include_disabled=False)`。
- 解析规则：缺省 `normal`；未知值报错而非回退；字符串去空白后小写比较；coding 缺 `coding_project_dir` 报配置错误；normal 提交 `coding_project_dir` 一律丢弃（不落盘）。
- `agent/admin.py`：`_build_profile` 接受两字段；`_api_projection` 保证 API 响应恒含 `agent_type` 与 `coding_project_dir`（normal 为 `None`）；`update_agent` 拒绝类型变更（同值放行）、coding 必须有项目；`make_default=True` 指向 coding 时明确拒绝并给出类型化消息（底层 `registry.set_default` 同样拒绝）。
- 配置：`config.py` 的 `available_setting` 与 `config-template.json` 仅新增 `opencode` 对象（`enabled=false`、`service_id`、`api_url`、`web_url`、`username`、`password_env`）；密码只从环境变量读取，控制台投影不含密码及其变量名。

### 2.3 入口边界与稳定错误

`agent/coding/__init__.py` 定义 `CODING_DISABLED`/`CODING_WEB_ONLY`/`CODING_SERVICE_CHANGED`/`CODING_UPSTREAM_UNAVAILABLE`/`CODING_NOT_LINKED` 与 `CodingError(status)`，一律不复用 401/403。已接入的拒绝点：

| 位置 | 行为 |
| --- | --- |
| `bridge/agent_initializer.py::initialize_agent` | 任何非 Web 入口的统一收口，coding 在建立工作区/进程前抛 `CODING_WEB_ONLY`（400）；`enabled_agents` 改用 `registry.normal()` |
| `bridge/agent_bridge.py`、`channel/web/api/agents.py` | scheduler 初始化与重载只遍历 `registry.normal()` |
| `channel/web/fork/handlers/agents.py` | `set_default`/`set_user_default`/`bind_channel_instance` 复用 `_reject_coding_agent`；租户/管理工作台投影含 `agent_type`（管理投影含 `coding_project_dir`） |
| `channel/web/fork/handlers/chat.py` | 先做权限判定再判类型：未授权调用者拿到普通 403，不把类型差异当作提示 |
| `channel/web/fork/handlers/sessions.py` | 团队成员/设置入口拒绝 coding，并 re-raise `web.HTTPError` |
| `agent/personal_assistant.py` | 个人助理模板来源排除 coding |

### 2.4 验证结果

```bash
.venv/bin/python -m pytest -q -p no:randomly tests/test_agent_registry.py tests/test_agent_admin.py \
  tests/test_agent_initializer_routing.py tests/test_coding_agent_routes.py \
  tests/test_coding_settings.py tests/test_agent_workbench.py tests/test_private_agent_owner_reachability.py
# 151 passed in 7.19s
```

覆盖点：旧配置缺省 normal 且不重写、coding 项目必填、远端项目原样保留且不触发本地创建、类型不可变、复制保留类型/项目但不复制会话、coding 不能成为通用默认（配置期与运行期两条路径）、伪造普通发送被拒、无使用授权者得到普通 403、关闭开关时 `coding_disabled`（503）、服务实例改变 409。

回归范围：`agent|bridge|team|personal|scheduler|channel|tenant|web|session|delegat|default` 命中的 172 个测试文件，分批（每批 8 个、每批独立进程）与「还原全部产品改动」的同一批命令对比，失败集合一致，无新增失败。

已知既有失败（与本次改动无关，已用还原产品改动的对照实验证明）：

- `tests/test_external_channel_propagation.py` 的 4 个用例、`tests/test_web_consumer_closure.py` 的 7 个、`tests/test_web_multipart_agent_scope.py` 的 5 个：仅在多文件同进程时出现，单文件运行全部通过；还原产品改动后同样失败（`4 failed, 63 passed` 两次一致）。
- `tests/test_personal_console_frontend.py::test_frontend_behavior`：单文件确定性失败，还原产品改动后同样失败。
- 全量单进程收集存在既有 `ModuleNotFoundError` 收集错误，故按文件分批执行。

（本节不含任何密码或上游凭据。）

## 3. 轻量会话适配与存储（3.1–3.7 已完成）

### 3.1 OpenCode HTTP 客户端（`agent/coding/opencode.py`）

请求形状全部取自 1.1 的实测响应，未按文档猜测：

| 方法 | 请求 | 处理 |
| --- | --- | --- |
| `create_session(id, dir)` | `POST /api/session`，体 `{"id":…,"location":{"directory":…}}` | 解析 V2 `data` 信封；同 ID 采用已有会话 |
| `get_session(id)` | `GET /api/session/{id}` | 404 → `None`（单个会话的「已不存在」是正常状态，不是服务故障） |
| `active_sessions()` | `GET /api/session/active` | 返回 running 集合，一轮刷新只问一次 |
| `rename_session(id,title,dir)` | `PATCH /session/{id}?directory=…`，体 `{"title":…}` | 兼容路由；204/空体视为成功 |
| `interrupt_session(id)` | `POST /api/session/{id}/interrupt` | 204 空体 |
| `delete_session(id,dir)` | `DELETE /session/{id}?directory=…` | 确认 404 视为已完成（幂等） |

分类：连接失败/5xx/非 404 的 4xx（含上游 401/403）→ `coding_upstream_unavailable` + 502；超时 → 504；JSON 不可解析 → 502。上游 401/403 绝不透传为平台 401/403。每次调用带 `timeout=(3,10)`，凭据只走 `Authorization` 头，不进入 URL。

### 3.2 关联表与缓存（`opencode_session_links`）

- 通过既有 `conversation_schema` 接缝新增**能力表**（`CAPABILITY_TABLES`，不参与维度合成，因此 `dimensions=()` 的历史形状断言改为按表比对，`tests/test_conversation_schema_seam.py` 同步收紧为逐表断言）。
- 列：`agent_id, session_id, service_id, external_session_id, project_dir, state, request_id`；主键 `(agent_id, session_id)`，唯一 `(service_id, external_session_id)`。不存 owner/tenant，一律经 `sessions` 行关联，避免两处owner。
- `ConversationStore` 新增小范围方法：`create_coding_link`（同一事务预建关联 + 缓存行）、`get_coding_link`、`find_coding_link_by_external`、`list_coding_links`（owner 过滤 + `session_id` 稳定游标，默认 50）、`set_coding_link_state`、`touch_coding_link_cache`（只 UPDATE，不 upsert；远端时间更旧则保留较新标题）、`delete_coding_link`（同事务删除关联与缓存）。
- 标题与时间落在既有 `sessions` 行，`msg_count=0` 的 coding 会话照常出现在既有列表/搜索/分页中。

### 3.3 预留 / 创建 / 重试（`agent/coding/sessions.py`）

- `derive_ids(service_id, tenant_id, user_id, agent_id, request_id)`：各字段长度前缀后拼接做 SHA-256，取 32 位十六进制 → `oc_<摘要>` / `ses_rsm_<摘要>`。身份来自**已验证身份**（`current_identity()`），不是请求体。
- `reserve()`：ready 直接返回（重复 POST 同 `session_id`）；creating 复用同外部 ID 重试；插入冲突按主键/唯一键分别处理（同请求并发收敛到同一关联）；未完成请求换项目 → 400 拒绝；`enabled=false` 或未配置地址 → `coding_disabled`（503），且不落任何行。
- 失败保留 creating：超时/5xx 后重试沿用同一外部 ID，上游按「同 ID 采用已有会话」返回同一会话。
- `session_url()`：`web_url` + OpenCode 自有路由 `/{base64url(目录,无填充)}/session/{外部ID}?rsm_embed=1`，编码规则取自 `packages/core/src/util/encode.ts`（`base64Encode`），未自创 slug。

验证：

```bash
.venv/bin/python -m pytest -q -p no:randomly tests/test_opencode_client.py tests/test_coding_session_store.py \
  tests/test_coding_session_lifecycle.py tests/test_coding_settings.py tests/test_coding_agent_routes.py \
  tests/test_conversation_schema_seam.py
# 101 passed
.venv/bin/python scripts/check-route-coverage.py   # OK（187 routes）
```

### 3.4 四个 Web 接口与路由登记（`channel/web/fork/handlers/coding.py`）

新增 `CodingSessionsHandler` / `CodingSessionOpenHandler` / `CodingSessionAttachHandler` / `CodingSessionSyncHandler` / `CodingSettingsHandler`，路由登记在 `route_registry.py`（字面量路径先于 `([^/]+)/open`，两者均为 `^{pattern}\Z` 锚定，无重叠）：

| 路由 | 方法 | 权限 |
| --- | --- | --- |
| `/api/coding/sessions` | POST | 路由策略 `P("tenant")`；handler 内 `chat.use` + `agent.use` + 绑定 + private-owner + coding 类型 |
| `/api/coding/sessions/([^/]+)/open` | GET | 同上，另加 `history.read`；**不创建任何远端状态** |
| `/api/coding/sessions/attach` | POST | 先证明 source 会话归属，再校验远端 |
| `/api/coding/sessions/sync` | POST | `history.read` + 显式 agent_id |
| `/api/coding/settings` | GET | `agent.read`（在路由层声明） |

- 授权顺序与既有 chat/session 入口一致：路由策略 → `_db_scope()` → 权限 → tenant 绑定 → private-owner → 会话归属。类型判定放在权限门之后，因此无授权者得到的仍是普通 403（`test_a_caller_without_the_agents_use_grant_is_refused`），类型不会变成提示。
- **owner 只来自已验证身份**：`tenant_id` / `user_id` 取自 `RequestContext` 并向下传；四个接口都不接受凭据、上游 URL 或客户端 owner（`_project_dir` 只从 Agent 档案读项目）。
- `attach` 逐项验证并全部有测试：source 归属（别人的 source → 404 `coding_not_linked`）、远端存在（不存在 → 404）、真实目录（不符 → 400 `coding_project_mismatch`）、根会话（`parentID` 非空 → 400）、已有归属（别人的已有 → 404，本人的已有 → 幂等返回同一 `session_id`）。平台 ID 由外部 ID 派生，故同一远端会话重复 attach 幂等。
- `open` 只读；`creating` 返回 `retryable=true` 交给 create 接口续做（`test_open_never_creates_a_remote_session`）。

### 3.5 分批刷新（`CodingSessionService.sync`）

- 每批 50（`SYNC_BATCH`）、`session_id` 稳定游标、`ThreadPoolExecutor(max_workers=4)` 限制在途读取（`test_a_refresh_reads_at_most_four_sessions_at_once` 用计数器实测 `max_reads_in_flight <= 4`）、每轮只读一次 active 集合（`test_the_running_set_is_read_once_per_round`）。
- 返回 `changed / removed / unavailable / next_cursor`，逐条规则与理由：

| 远端结果 | 关联状态 | 处理 | 理由 |
| --- | --- | --- | --- |
| 明确 404 | `ready` | `removed` + 原子删除 | 唯一可视为「已删除」的证据 |
| 明确 404 | `creating` | 保留，`state=creating` | 创建可能尚未落地，删掉会丢失重试所依赖的外部 ID |
| 超时/5xx/不可达 | 任意 | `unavailable`，缓存不动 | 连接失败不能作为任何会话的结论 |
| 服务标识不符 | 任意 | `unavailable` | 其 ID 指向本服务没有的会话 |

- 迟到刷新不会复活删除项、也不会覆盖更新的标题：缓存写入只走 `touch_coding_link_cache`（纯 UPDATE，且远端时间更旧时保留较新标题），两项各有测试。

### 3.6 列表投影与远端管理分流（`handlers/sessions.py`）

- 列表：`_agent_badge()` 增加 `agent_type`（`runtime.py`），会话行沿用既有 `agent` 徽标 → 列表即可区分 coding 会话，无需每行二次请求。这是精确而非提示：coding Agent 的普通执行路径都在 INSERT 前被拒，coding 入口一定写关联。另以 `coding_link_states()` 一次性批量查回关联状态并写为 `sync_state`（`creating | ready`），使列表能标出「创建未完成、可重试」而不是把预留当成可打开的会话。**运行中状态刻意不进列表**：设计 D3 明确其为刷新临时结果，故 `running|idle` 只由 sync 返回。
- `PUT /api/sessions/{id}`：有 title 且为 coding 关联 → 先 `rename` 远端，成功后写缓存；远端拒绝则本地标题保持原样（`test_a_refused_rename_keeps_the_old_title`，502 + `coding_upstream_unavailable`）。`pinned` / `archived` 仍为平台本地语义，期间零远端调用（`test_pinning_and_archiving_stay_local`）。
- `DELETE`：coding 会话走「运行中先 interrupt → 远端删除 → 成功后原子删除关联与缓存行」；远端拒绝保留记录（`test_a_refused_delete_keeps_the_session`）；重复删除幂等且只下发一次远端删除（`test_deleting_twice_is_idempotent`）；项目目录与文件不被触碰（`test_deleting_a_coding_session_never_touches_the_project`）。
- 不适用于 coding 的入口明确关闭为 400 `coding_web_only`：清空上下文、删除单条消息、按消息生成标题（平台侧没有对应镜像，返回成功等于说谎）。
测试同时覆盖：列表按 Agent 投影 `agent_type` + `sync_state`（`creating` 标记与「同一次预留可续做」）、`running` 不出现在列表、普通 Agent 不带任何 coding 标记。

普通会话路径未改变（`test_an_ordinary_session_is_untouched_by_the_coding_path`）。

### 3.7 回归与真实闭环

```bash
.venv/bin/python -m pytest -q -p no:randomly tests/test_coding_session_routes.py tests/test_coding_session_sync.py \
  tests/test_coding_session_lifecycle.py tests/test_coding_session_store.py tests/test_coding_settings.py \
  tests/test_opencode_client.py tests/test_coding_agent_routes.py tests/test_session_context_scope.py \
  tests/test_agent_workbench.py tests/test_agent_admin.py tests/test_agent_registry.py tests/test_agent_initializer_routing.py
# 253 passed
.venv/bin/python scripts/check-route-coverage.py   # route-coverage: 192 routes (68 upstream, 124 fork), OK
```

结构确认（非新库、非镜像、非队列、非第二份列表）：coding 关联与缓存都落在既有 `conversations.db`（`opencode_session_links` + 既有 `sessions` 行），刷新由前端按 5 秒轮询调用，未引入后台线程/队列；coding 会话不写 `messages` 行；列表仍由既有 `/api/sessions` 返回。真实闭环实测（脚本 `/tmp/rsm-coding-live-cycle.py`，服务 `http://127.0.0.1:4096`，项目 `/tmp/rsm-opencode-verify/project`）：

```
created:            oc_bb22f4c5ff9323fd58bf6a1da183c94a / ses_rsm_bb22f4c5ff9323fd58bf6a1da183c94a, state=ready
iframe_url:         http://127.0.0.1:3000/L3RtcC9yc20tb3BlbmNvZGUtdmVyaWZ5L3Byb2plY3Q/session/ses_rsm_bb22…?rsm_embed=1
remote_after_create: id=ses_rsm_bb22…, directory=/tmp/rsm-opencode-verify/project
repeat_same_ids:    true（同一 request 重放未产生第二个远端会话）
remote_after_rename: "live cycle rename"（回读远端确认）
sync_after_rename:  title="live cycle rename", state=idle, last_active=1789897044
open:               state=ready, retryable=false
deleted:            deleted=true；回读远端为 None；关联与缓存行均消失
sessions_after_delete: []
messages_written:   0；project_still_there: true
```

未完成：无。

## 4. OpenCode 嵌入与平台界面（4.1–4.5 已完成）

### 4.1 嵌入上下文（`packages/app/src/context/rsm-embed.ts`）

- 只在 `rsm_embed=1` 时进入嵌入模式；`rsm_parent_origin` / `rsm_channel` 缺失时仍是嵌入（隐藏导航成立），但不发送任何通知。
- 嵌入状态在窗口挂载时**捕获一次**，因此 SPA 内部导航丢掉查询串后依然隐藏导航，且二次挂载无法改写它。
- 隐藏项仅为重复导航与全局服务设置；输入、工具确认、差异、文件、终端、分叉全部保留。正常访问 `embedHides` 为空。

### 4.2 页面通知（`entry.tsx` / `layout.tsx` / `session.tsx`）

- `entry.tsx` 挂载时捕获嵌入状态并 `notifyReady()`；`session.tsx` 的 `AnnounceEmbedSession` 在 `sessionID` 变化时 `notifySession(id)`（覆盖内部新建、分叉与导航）。
- 父页只接受**本次挂载的 iframe**、**配置的 origin**、**本次随机通道**三者同时匹配的消息（`coding.js::parseNotification/handleMessage`）；三者任一不符即忽略，当前选择与关联不变。

### 4.3 / 4.4 平台模块与刷新策略（`channel/web/static/js/coding.js`，`console.js`，`chat.html`，`css/coding.css`）

- `window.CodingChat`：`launch/open/refresh/leave`（另含 `retry`、`setVisibility`、`_state`）在既有对话区域内挂载/卸载 iframe；不引入新的通用前端通信总线。
- 类型分流：`console.js` 按 `agent_type` 决定走普通还是 coding 路径；coding 时隐藏平台输入、模型/权限模式与团队入口；类型与项目信息的展示位置经浏览器验收核对为：**智能体管理详情表单**显示类型 / 项目目录 / 编码服务，**工作台卡片与新对话选单**显示 `编码（Opencode）` 类型徽标（工作台卡片原先不显示，浏览器验收发现后补齐，见第 6 节）；切回普通智能体时被隐藏的控件**逐个恢复原状**。
- 刷新：`setTimeout` 5000ms、可见性暂停/恢复、在途防重、稳定游标逐批、每次挂载的身份/页面世代号丢弃迟到响应；管理成功后即时刷新。历史页沿用既有列表重绘；临时不可达保留缓存；未收到 `ready` 显示加载，15 秒后给出重试，重试只重开同一会话。

### 4.5 前端测试

```bash
node --test tests/test_coding_frontend.cjs tests/test_agent_workbench_frontend.cjs tests/test_user_default_agent_frontend.cjs tests/test_console_i18n_coverage.cjs
# tests 74 / pass 74 / fail 0
```

`tests/test_coding_frontend.cjs` 覆盖：普通↔coding 切换、重复点击不重复挂载、切换时隐藏并停止旧表、恢复后单次刷新、迟到响应不强制导航、错误消息来源、iframe 新会话通知只登记一次且不重载、服务失败后恢复。

**新资源确实被服务**（无法只靠拆分文件证明，另加 Python 侧验收 `tests/test_coding_page_assets.py`，4 passed）：

```
js/coding.js       -> 1 occurrence, stamped ?v=
css/coding.css     -> 1 occurrence, stamped ?v=
js/i18n/coding.js  -> 1 occurrence, stamped ?v=
console.js stamped: True；script order: coding.js 先于 console.js
```

`js/i18n/coding.js` 与 i18n 命名空间由现有目录自动发现机制纳入（与 `functional-*.js`、`fragments/*.html` 同一路径），`js/coding.js` 与 `css/coding.css` 为显式清单新增项。

### 4.6 嵌入页只留当前会话（标题栏与开发诊断条）

浏览器验收暴露的缺口：嵌入页除会话本体外还渲染了 OpenCode 自己的外壳——V2 标题栏（渠道徽标 `DEV` 与会话标签页）与底部的开发诊断条（`NAV/FPS/JANK/INP/CLS/…`）。二者都在平台已提供身份与会话导航的前提下重复出现，且标签页能在平台不知情时改变 iframe 指向。

- 新增两个嵌入部分 `titlebar`、`debug-tools`（`packages/app/src/context/rsm-embed.ts`），与既有 `session-nav` 等一起只在嵌入模式下隐藏。
- V2 布局（`pages/layout-new.tsx`）与旧布局（`pages/layout.tsx`）都按 `embedHides` 决定是否渲染 `<Titlebar>` 与 `<DebugBar>`；诊断条的唯一开关就在被隐藏的徽标上，因此一并去掉。
- 会话标签的登记不受影响：`pages/session.tsx` 自身会 `addSessionTab`，嵌入页仍能得到会话标签状态。
- 独立访问不受影响：`embedHides` 在非嵌入挂载下恒为假。

```bash
cd /Users/jiantan/ai_assistant/rsmcode/opencode/packages/app
bun test --conditions=solid --preload ./happydom.ts ./src/context/rsm-embed.test.ts   # 15 pass / 0 fail
bun run typecheck                                                                      # tsgo -b 通过
bunx prettier --check src/context/rsm-embed.ts src/context/rsm-embed.test.ts src/pages/layout-new.tsx src/pages/layout.tsx
# All matched files use Prettier code style!
```

实现版本：

```
rsmcode/opencode  5a8335857b（分支 dev）+ 本地未提交的 rsm-embed 定制
```

### 4.x OpenCode 侧

```bash
bun test --conditions=solid --preload ./happydom.ts ./src/context/rsm-embed.test.ts   # 15 pass / 0 fail（原 14 条 + 本次新增 1 条）
bun typecheck                                                                          # 通过
bun run build                                                                          # ✓ built in 8.44s
```

## 5. 联调、迁移与交付

### 5.1 聚焦测试（全部通过）

```bash
.venv/bin/python -m pytest -q -p no:randomly tests/test_agent_registry.py tests/test_agent_admin.py \
  tests/test_agent_initializer_routing.py tests/test_coding_agent_routes.py            # 78 passed
.venv/bin/python -m pytest -q -p no:randomly tests/test_opencode_client.py tests/test_coding_session_store.py \
  tests/test_coding_session_sync.py tests/test_route_registry.py \
  tests/test_conversation_schema_seam.py tests/test_history_agent_workspace.py         # 100 passed
.venv/bin/python -m pytest -q -p no:randomly tests/test_coding_agent_type_boundary.py \
  tests/test_coding_session_routes.py tests/test_coding_page_assets.py                 # 55 passed
node --test tests/test_coding_frontend.cjs tests/test_agent_workbench_frontend.cjs \
  tests/test_user_default_agent_frontend.cjs tests/test_console_i18n_coverage.cjs      # 74 pass / 0 fail
# OpenCode app：14 pass / typecheck 通过 / build 通过（见 4.x）
# 默认链回归：tests/test_{user,tenant}_default_agent*.py tests/test_default_agent_*.py 等 94 passed
# 工作台/默认/复制等 11 个既有套件：270 passed
```

本次未新增失败；5.1 未删除任何普通行为或权限断言。既有失败沿用第 2.4 节记录的清单（多文件同进程的 4/7/5 个用例、`test_personal_console_frontend.py::test_frontend_behavior`、全量单进程收集错误），本组命令不触发这些文件。

### 5.4 文档与迁移验证

新增 `docs/opencode-coding-agents.md`（全局配置、远端项目含义、配套 Web 根路径部署、现有浏览器认证、共享服务边界、升级/回滚与自检）。

四项迁移结论用 `scripts/verify_coding_upgrade.py` 在临时目录实测（不接触生产数据）：

```
legacy_type: normal                     legacy_is_normal: True
legacy_projection_keys: ['description', 'enabled', 'id', 'name', 'workspace']
legacy_saved_has_no_type: True          legacy_entry_unrewritten: True
legacy_file_unrewritten: True           legacy_still_unrewritten: True
legacy_second_load_type: normal
link_table_after_first_open: True       link_table_after_reopen: True
link_rows_after_reopen: 1               link_intact_after_reopen: True
store_open_count_is_idempotent: True
disabled_raised: coding_disabled        disabled_wrote_no_row: True
disabled_called_nothing: True           disabled_kept_the_existing_link: True
reenabled_state: ready                  reenabled_old_link_still_there: True
reenabled_row_count: 2
project_untouched: True                 project_files: ['main.py']
messages_rows: 0
```

要点：旧档案按 `normal` 读取且保存不写回类型/项目字段；关联表反复初始化后行数与数据不变（同一 `CREATE TABLE IF NOT EXISTS` 路径，无第二个迁移工具）；`enabled=false` 在触库之前拒绝且不调用上游，同时保留既有档案、关联与缓存；重新启用后旧关联仍在并可创建新会话；项目目录内容（`main.py` 摘要）与 `messages` 行数均未变化。

### 5.2 浏览器真实验收与 5.3 权限/故障矩阵（隔离实例实测）

验收环境：平台实例 `http://127.0.0.1:9900`（独立 `COW_DATA_DIR=/tmp/rsm-coding-acceptance/data`、独立身份库与工作区），OpenCode 服务 `http://127.0.0.1:4199`（第二个实例，带可用模型凭据；用户正在使用的 9899 实例**全程未重启、未改配置**）。验收项目目录在 5.2 内初始化为 git 仓库并提交基线（差异视图需要）。

**5.2 会话全生命周期（API 级，33/33）** —— `/tmp/rsm-coding-acceptance/accept.py` 逐条对照 OpenCode 与平台数据库，不只看单侧响应：

```
39 项断言 / 33 项检查全过，覆盖：
创建→ready→远端目录与 root 会话；同一 request_id 重放不产生第二个远端会话；
open 返回嵌入 URL（含 rsm_embed=1，且 Web 根 = 配置项）；open 不创建远端会话；
统一历史显示所属智能体与类型；重命名落到远端回读与本地缓存；sync 报告 changed/unavailable；
远端删除后列表收敛、关联行移除、再次 open 返回 404 而不重建；
内部新会话（fork）经 attach 进入历史、与源会话不同行、可再次 open、重复 attach 幂等；
平台删除同时删除远端会话、关联与缓存行。
```

**5.3 权限与故障矩阵（27/27）** —— `/tmp/rsm-coding-acceptance/accept_permissions.py`：

```
两种智能体共存（普通 helper + coding erp-coder），租户默认 = helper
持有 coding 授权的成员可创建 coding 会话
撤销授权后：下一个请求 403、打开本人既有会话 403
无授权成员：403，且错误码为权限类而非 coding_disabled（不泄漏能力状态）
OpenCode 停机：历史仍在（跨页 56/56 行、标题保留）、open 仍返回调用者本人会话（本地读，
  不把用户会话变成 404）、重命名/删除如实报 502 coding_upstream_unavailable、
  sync 将其列入 unavailable 而非 removed、关联行保留
服务恢复后：sync 成功且不再报 unavailable
创建响应丢失后以同一 request_id 重试：同一会话，未写重复关联行
历史超过一页：按 page 逐页走完 56/56、页间不重不漏、每条 coding 行都有各自远端关联
普通智能体的历史是它自己的列表；重新登录后关联一致（平台重启后同样成立）
```

**5.2 浏览器真实验收** —— 由浏览器子代理 [Browser acceptance (re-run)](ce033716-7a46-4d25-9972-cadec1502b81) 在对同一实例上执行（创建→分配→发送消息→工具确认/差异/终端→标题同步→历史重开→普通智能体并行对话）。首轮发现 3 项产品缺陷（第 6 节已修），修复后复验结论见第 6 节。

复验前先把验收环境本身的三处偏差纠正，否则测的不是本 change 的行为：

1. 验收项目 `/tmp/rsm-coding-acceptance/project` 之前不是 git 仓库，OpenCode 无法为编辑生成快照，差异视图自然是空的（服务端 `edit` 部件不带 `diff` 字段，客户端按 git 状态计算）。已 `git init` 并提交基线，服务端随后出现快照跟踪日志（`tracking hash=... git=.../snapshot/...`）。
2. 独立 OpenCode 实例原先没有权限配置，`edit`/`bash` 直接执行、不会询问。已写入 `permission: {edit: "ask", bash: "ask"}` 并重启该独立实例（仅该实例；用户在用实例未动）。
3. 实例数据里 coding 智能体仍是租户默认。已改为普通 `helper` 为默认，`erp-coder` 保持 coding；实测：

```bash
GET /api/agents ->
  helper     普通助手     normal  is_default=True
  erp-coder  ERP 编码助手 coding  is_default=False
  default_agent_id -> helper        default_resolution -> {"agent_id":"helper","source":"tenant"}
```

服务端可判定证据（本代理直接读取服务日志与 API，不依赖界面观察）：

```
13:55:50  evaluated permission=bash pattern="echo permission-probe-42" action.action=ask
13:55:50  asking id=per_0bf1a0d7b001etSXw9yAR8jwE2 permission=bash
          -> 该期间工具部件状态为 status=running（挂起等待，未执行）
13:56:38  批准后执行；部件收敛为 status=completed, output="permission-probe-42\n"
```
即确认门是真实生效的：未决期间命令不执行，批准后才产生输出；提示与批准的界面侧观察由浏览器验收记录。

运行实例确实在跑修好的前端（避免"改了没生效/缓存旧包"）：以登录会话抓取对话页实际引用的资源并核对修复标记，6/6 命中：

```
GET /chat -> 200；页面引用 assets/js/coding.js?v=…、assets/css/coding.css?v=… 等
PASS coding.js   撤下 chat-home（历史重开）
PASS coding.js   仅补一次 rsm_embed
PASS console.js  已挂载 pane 时不再套首页布局
PASS console.js  工作台卡片类型徽标
PASS coding.css  占满对话区
PASS console.css 徽标容器
```

复验结论（[Browser acceptance (re-run)](ce033716-7a46-4d25-9972-cadec1502b81)，同一实例）：

| # | 项 | 结果 |
| --- | --- | --- |
| 1 | 普通智能体对话（`helper`，租户默认） | PASS——两轮真实回复，服务端日志 `[Routing] → 🤖 普通助手(helper)`，无 `coding_web_only` |
| 2 | coding 面板挂载与 URL | PASS——iframe 1680×1016、`#chat-input-area` 隐藏、`chat-main … coding-active`、`rsm_embed=1` **恰好 1 次** |
| 3 | 历史重开 / 新建对话 | 见下（首轮发现的塌陷与卡死已不复现；复验又暴露两处新问题，均已处理） |
| 4 | 工具确认 | PASS——`需要权限 / 运行 shell 命令 / echo permission-probe-42`，`拒绝 / 始终允许 / 允许一次`；批准后才产出 `permission-probe-42` |
| 5 | 差异视图 | PASS——`.diffs-container` shadow DOM 内出现可读 `+` 行（如 `+def mod(a, b): return a % b`） |
| 6 | 工作台卡片类型 | 首轮 FAIL（见缺陷 2），修复后复测 PASS：`普通助手→[默认]`、`ERP 编码助手→[编码（Opencode）]` |

复验暴露的两处问题（均非既有回归，而是修复/环境未覆盖到的缺口）：

1. **工作台卡片仍不显示类型（前端缺陷，已修）** —— `console.js::fetchAgentWorkbench()` 用字段白名单重排服务端行为，漏了 `agent_type`，于是卡片徽标、新建会话行与 `agentTypeOf()` 的兜底全都拿不到类型（服务端 `GET /api/agents?view=workbench` 一直返回 `"agent_type":"coding"`）。已补 `agent_type: a.agent_type`，并在 `tests/test_agent_workbench_frontend.cjs` 增加**走读取路径**的用例（原有用例直接把手搓对象喂给渲染函数，正是这个盲区漏掉的原因）。停用该行 → 用例变红（已核验）。浏览器复测：`agentTypeOf('erp-coder') = coding`、`('helper') = normal`，工作台卡片徽标恰好 1 个且文案为 `编码（Opencode）`。
2. **面板停在加载态（验收环境部署偏差，非产品缺陷）** —— 该实例的 `web_url` 填成了 `api_url`（`http://127.0.0.1:4199`），而 4199 是 OpenCode **服务自身附带的未适配界面**（产物内不含嵌入上下文）；子页因此不会回发 `rsm.opencode.ready`，平台侧停在「正在打开编码会话…」。已改为配套定制构建的地址（本机 `bun run --cwd packages/app dev`，3000），并按 `docs/opencode-coding-agents.md` §3 新增的说明记录该判据与症状。

修正后在本机亲测该面板（浏览器 + CDP，只读）：

```
CodingChat.open('erp-coder','oc_623fade3…') -> state "ready"
iframe_url = http://127.0.0.1:3000/L3RtcC9yc20…/session/ses_rsm_623fade3…?rsm_embed=1
挂载后 1.5s 采样：loading=null、frame=656px、chat-main="chat-main relative coding-active"、
  #chat-input-area display=none、CodingChat.isActive()=true
CodingChat.current() -> {ready: true, channel: "ch-mu9xb3gi-q9dgq5il", external_session_id:
  "ses_rsm_623fade3…"}
  —— 「加载提示消失」只有 markReady（收到并校验 ready 通知）与卸载两条路径，面板仍在挂载，
     故可直接判定 ready 握手成立
面板内子文档（CDP 隔离世界读取）：title=OpenCode、会话标题、输入框占位、模型
  "DeepSeek V4 Pro" 均已在平台对话区内渲染
```

保留说明：自动化**无法在跨 origin iframe 内输入**（浏览器工具不进入 iframe），因此「面板内敲字触发一次确认」仍是以同一地址顶层打开的页签验证的（第 4、5 项）；面板本身已证明可挂载、可置 ready、内容已渲染。

逐条对照 spec 时的补测（5.5 核对发现的唯一缺口）：`opencode-session-sync` 的「默认项目改变」原先无用例。行为本身正确（`open` 读关联行里创建时的 `project_dir`，`create` 取智能体当前配置），但缺断言。已补 `test_an_old_session_keeps_its_project_after_the_agent_is_moved`：创建会话后在平台上把智能体指到另一个目录，旧会话仍以原目录构造地址、新会话使用新目录。把该处换成任何非关联值即变红（已核验）。其余 32 个 scenario 均有对应用例或验收记录。

### 6. 验收中发现并修复的产品缺陷

浏览器验收与 API 验收各暴露出真实缺陷（非验收脚本问题），逐条记录根因、修法与回归：

1. **coding 智能体会被自动任命为租户默认（高）** —— 空租户创建的第一个智能体被 `_adopt_agent` 直接任命为租户默认，coding 智能体因此成为所有普通会话的落点，随后每条消息被 `coding_web_only` 拒绝；规范要求 coding MUST NOT 被设为通用默认。
   - 写入侧 `channel/web/fork/handlers/agents.py`：任命前排除 coding 类型；读取侧 `auth/service.py::_agent_is_usable`：coding 智能体不参与默认解析链（含 `shared`/`own` 兜底）。
   - 回归：`tests/test_coding_agent_type_boundary.py` 新增 3 例（首个 coding 不被任命、coding 永不被解析为默认、普通智能体仍被任命）。两处守卫分别回退 → 对应用例变红（已核验）。

2. **从历史重开 coding 会话会把嵌入面板压成 49px 并卡住（高）** —— `chat-home`（空对话布局）在挂载 pane 时未撤下：它让消息列表 `display: contents` 并给容器加内边距，帧与欢迎标记共用同一列被挤扁，加载提示不消失，平台输入也被隐藏，用户只能整页刷新。
   - 挂载/卸载时由 `coding.js::applyChrome` 撤下并在卸载时**按原状**恢复；`console.js::syncChatHomeLayout` 同步排除「已挂载 coding pane」，避免后续重渲染把布局放回去；`coding.css` 补一条同优先级的兜底重置。
   - 回归：`tests/test_coding_frontend.cjs` 新增 2 例（挂载撤下 / 卸载恢复、原本非首页不会被改成首页）。

3. **iframe URL 重复携带 `rsm_embed=1`（低）** —— 服务端 `session_url` 已经负责该标记，页面又追加一次。
   - `coding.js::frameUrl` 改为仅在缺失时补上；回归 `tests/test_coding_frontend.cjs`（标记恰好一次，两条来源路径都覆盖）。

4. **工作台卡片不显示 coding 类型（低）** —— 管理详情表单与新建选单都显示类型，工作台卡片不显示，用户看不出这张卡会进入嵌入面板。
   - 首次修复只改了渲染函数；复验发现真正的缺口在读取侧：`fetchAgentWorkbench()` 的字段白名单漏掉 `agent_type`，徽标分支永远拿不到数据（新建会话行与 `agentTypeOf()` 兜底同样受影响）。已补该字段，并新增走读取路径的用例（见第 5.2 节复验记录）；停用该行 → 用例变红（已核验）。

5. **同一项目被判定为"不同项目"而拒绝 fork（高，API 验收发现）** —— `attach` 用「远端回读目录 == 平台配置目录」做字面比较，但服务返回的是**会话创建时使用的路径字符串**：平台自己创建的会话报 `/tmp/...`，而用户在同一项目内 fork 出来的会话报 `/private/tmp/...`（同一 checkout），于是用户自己的 fork 被以 `coding_project_mismatch` 拒绝。
   - 以服务自身的项目标识为准：`RemoteSession` 增加 `project_id`（解析 `projectID`），`attach` 在两侧都有 id 时按其判定，缺失时才回退到目录比较。平台**不对远端项目做本地 `Path.resolve()`**（设计约束），比较是两个远端读之间的比较。
   - 回归：`tests/test_coding_session_routes.py` 新增 2 例（同一 projectID、两种路径写法 → 接受；路径相同但 projectID 不同 → 拒绝）；旧的「无 id、目录不同 → 拒绝」用例继续覆盖回退路径。停用 id 比较 → 两例变红（已核验）。

另记（**非本 change 引入、未修**）：`auth/service.py::create_role` 对重复角色编码抛出未捕获的 `sqlite3.IntegrityError`（UNIQUE 约束），表现为 500 HTML 而非 409。角色管理不在本 change 范围内，仅记录。

另记（**环境偏差，非产品缺陷；界面复验见 5.2**）：首轮"差异视图为空"来自验收项目不是 git 仓库 —— 服务端 `edit` 部件不含 `diff` 字段，差异由客户端按 git 状态计算，无仓库即无内容。项目已初始化为仓库并提交基线，服务端已出现快照跟踪；界面侧是否恢复由浏览器复验确认，未确认前不写成通过。

验收脚本（均在隔离实例上重跑通过，可重复执行）：

```bash
/tmp/rsm-coding-acceptance/accept.py             # 会话生命周期 33/33
/tmp/rsm-coding-acceptance/accept_permissions.py # 权限与故障 27/27
/tmp/rsm-coding-acceptance/check_assets.py       # 运行实例确在提供修好的前端（6/6）
```
