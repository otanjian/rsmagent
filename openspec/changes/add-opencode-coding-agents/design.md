## Context

动机及范围见 `proposal.md`。本设计是实施约定，尚未完成运行验证。

- `agent/registry.py` 的 `AgentProfile` 尚无类型；`agent/admin.py` 集中创建、复制及更新。`bot_type` 是模型提供方，不能复用为智能体类型。
- 平台权限入口在 `channel/web/fork/common.py::_workbench_chat_readiness`；会话接口在 `channel/web/fork/handlers/sessions.py`，跨智能体列表在 `channel/web/fork/runtime.py::_list_sessions_across_agents`。
- 当前 fork 的 `ChatHandler` 实际读取 `channel/web/chat.html`，该页面加载 `static/js/console.js`。虽有 `templates/views/`、`static/js/chat/` 等上游拆分文件，不能只修改未被当前页面装载的文件。
- 本地 OpenCode 基线：`/Users/jiantan/ai_assistant/rsmcode/opencode`，检查到 commit `5a8335857b`、app 版本 `1.18.31`。当前 Web 创建会话走 `/api/session`（V2），重命名/删除等管理能力仍有 `/session` 兼容接口。V2 创建没有自定义 `metadata`，不能据旧接口推断支持。

## Goals / Non-Goals

**Goals:**
- 用户从获分配的 coding 智能体进入嵌入页面，在平台统一历史中找到、恢复、重命名和删除编码会话。
- 采用一个共享服务、一个轻量关联表、现有列表缓存和 5 秒查询刷新；尽量复用 OpenCode 自身交互。
- 程序员按 `tasks.md` 顺序可完成两仓库配套交付。

**Non-Goals:**
- 本期不做 OpenCode 执行隔离、多实例管理、自动启动进程、通用 provider/事件框架、SSE 同步后台、完整消息复制或全文检索。
- 不做 Desktop、渠道、群聊、委派、定时任务的 coding 执行，也不做普通人设/技能/知识库向 OpenCode 的转换。
- 不重构现有前端拆分、身份系统或配置中心；不承诺 OpenCode 免二次登录。外部服务沿用自身认证，统一 SSO 不属于本目标。

## Decisions

### 1. 数据字段与配置

智能体新增两个字段：

| 字段 | 约定 |
| --- | --- |
| `agent_type` | `normal` 或 `coding`；缺省 `normal`；创建后不可变 |
| `coding_project_dir` | coding 必填，OpenCode 服务器上的绝对目录；normal 不使用 |

`workspace` 继续是平台状态目录，与项目目录分开。不得对远端项目执行平台本地 `Path.resolve()`、存在性检查、空目录检查或目录初始化。表单必填名称、类型、项目目录；单一服务只读展示，ID 自动生成，描述/头像可选。类型切换仅限未保存的创建表单；coding 编辑界面隐藏普通模型、技能、知识、人设配置。复制 coding 只复制档案和项目配置，不复制会话、关联、凭据和项目内容。

全局 `config-template.json` 增加一个 `opencode` 配置对象，不建设服务管理页面：

```json
{
  "opencode": {
    "enabled": false,
    "service_id": "default",
    "api_url": "http://127.0.0.1:4096",
    "web_url": "https://code.example.com",
    "username": "opencode",
    "password_env": "RSM_OPENCODE_PASSWORD"
  }
}
```

地址/服务标识由部署管理员配置，租户创建表单不能提交任意上游地址。密码从指定环境变量读取，只在服务端 HTTP 请求中使用，不返回页面、拼接 URL、写入会话或日志。无认证服务可不配置密码。`enabled=false` 时保留已有配置和缓存，只关闭新建、打开及远端管理，不回退为普通智能体。

`service_id` 表示一个持久数据实例：迁移地址但保持同一数据库时不变；切换到另一份 OpenCode 数据库时必须改值。旧关联与新配置不匹配返回 `coding_service_changed`，不能把同名 ID 当成新服务的会话。

### 2. 接入只分两条分支

Web 显式选择 coding 时调用 coding 会话接口并挂载 iframe；普通智能体走现有流程。不能把 OpenCode 包装成模型，不能把它接入普通 `ChatService` 的流式消息转换。

首版 coding 仅作为显式 Web 选择项，不设为全局/租户/用户通用默认，不参加个人助理自动创建、团队、委派或渠道候选。后端在普通初始化入口拒绝 coding，scheduler 启动/重载跳过 coding。直接伪造普通聊天、绑定或工具调用同样返回 `coding_web_only`，不静默选择默认智能体。此限制避免把 Web 嵌入能力扩散到所有消费者。

复用现有身份、`agent.read`、`agent.use`、`chat.use`、`history.read` 和会话 owner 检查；不新增权限目录。coding 模型由 OpenCode 管理，不要求平台为它匹配普通 `model.use`。现有租户/owner 数据过滤继续有效，本期省略的是外部运行环境隔离。

### 3. 会话关联与列表缓存

通过 `get_conversation_store()` 取得现有按 Agent 作用域访问的会话数据库，增加 `opencode_session_links`，使用同一连接进行关联与缓存事务，不增加数据库服务。当前函数实现已经把会话收敛到全局数据库、按 `agent_id` 区分；部分文件头和旧规范仍描述分库，不能据旧注释另建每 Agent 数据库。

```text
agent_id, session_id                  PRIMARY KEY
service_id, external_session_id       UNIQUE(service_id, external_session_id)
project_dir                          创建时记录的远端目录
state                                creating | ready
request_id                           平台创建的重试标识；attach 记录可为空
```

owner、tenant、标题、创建/活动时间、置顶与归档复用现有 `sessions` 行。关联表不另存一份 owner；`agent_id` 使用现有 store 的规范维度值，不能绕过默认 Agent 的空字符串兼容规则。`sessions` 中 coding 行允许没有 `messages`；历史正文从 iframe 获取。持久的运行状态无必要，刷新结果临时返回 `running|idle|unavailable`；执行错误使用 OpenCode 页面原有展示，不把连接失败当成任务失败。

平台新建会话时前端产生一个 `request_id`（UUID）并在请求结果明确前保持不变。服务端将已验证的 `(service_id, tenant_id, user_id, agent_id, request_id)` 规范编码后取 SHA-256，生成 `session_id=oc_<摘要>`、`external_session_id=ses_rsm_<摘要>`，在同一事务中预建 `creating` 关联与缓存。这样不同主体复用客户端 UUID 也不会指向同一远端会话。重试还须验证原项目未改变，不能接管别人的记录。调用 V2 创建时传固定外部 ID，成功后改为 `ready`。当前 V2 实现存在同 ID 采用已有会话的行为，阶段 1 必须实测这一点；超时保留 `creating`，重试复用 ID，不盲目创建第二个会话。

创建中的记录在列表标记“创建未完成，可重试”，点击恢复同一次创建，不调用模型。同步查询跳过 creating 关联的删除判定，避免远端创建尚未完成时的 404 清掉预留。明确输入错误可清理尚未建立远端会话的预留；超时/5xx 不做推断删除。

平台只管理建立关联的会话，不全量导入共享 OpenCode 中的其他历史。OpenCode 内正常导航到另一个会话时通过小型前端桥接登记：同一会话已关联且属于当前 owner/Agent 则复用；未关联则经后端读取真实会话，确认目录与来源平台会话的项目匹配后登记。来源会话必须归当前 owner/Agent，不能用新修改的智能体默认目录覆盖旧会话项目；已有其他归属的目标不重新认领。分叉后的新根会话可登记，带 `parentID` 的内部子任务默认不加入独立历史。

编辑智能体默认目录只影响未来新会话；历史会话使用映射中的目录及远端返回的真实位置，不能重新绑定到新目录。删除智能体沿用已有“存在会话需先处理”的阻断，禁止默认递归删除外部代码或会话。

### 4. HTTP 适配契约

新增 `agent/coding/opencode.py`（同步 HTTP 客户端）、`agent/coding/sessions.py`（关联/刷新/管理）和 `channel/web/fork/handlers/coding.py`（HTTP 授权入口）。沿用现有 `requests` 和会话数据库，不建立抽象 provider 基类。

| 平台接口 | 输入 / 输出与职责 |
| --- | --- |
| `POST /api/coding/sessions` | `{agent_id, request_id}`；返回 `{status, session_id, agent_id, iframe_url}`；创建或重试 |
| `GET /api/coding/sessions/{sid}/open?agent_id=...` | 校验关联与权限，返回当前 `iframe_url` 和会话摘要；creating 时返回可重试状态而不以 GET 创建远端 |
| `POST /api/coding/sessions/attach` | `{agent_id, source_session_id, external_session_id}`；从当前嵌入页登记，后端核验来源和目标后返回平台摘要 |
| `POST /api/coding/sessions/sync` | `{agent_id, cursor?}`；按关联表稳定游标每批最多 50 条，返回 `{status, changed, removed, unavailable, next_cursor}` |
| 既有 `/api/sessions` | 继续返回普通与 coding 缓存；增加 `agent_type`、`sync_state`，保留现有分页/排序/搜索/归档契约 |
| 既有会话重命名/删除接口 | 查到 coding 关联时直接调用远端管理后更新缓存，普通分支保持原行为 |

新增路由登记在 `route_registry.py` 并通过 fork 装配导出，不手写第二份权限表。创建、attach、open、sync 及管理操作都验证目标权限；用户/租户来自请求身份，不能取请求体提供的归属。sync 只处理调用者自己的关联。`iframe_url` 只根据全局 `web_url` 和真实远端会话构造。成功沿用 `{status:"success", ...}`；错误沿用 `{status:"error", code, message}`，认证/授权使用现有 401/403/404，类型或输入错误 400，服务标识冲突 409，关闭能力 503，上游认证/服务故障统一 502（超时 504），避免与平台用户会话失效混淆。

初始 OpenCode 适配表（以阶段 1 的真实响应固定解析，不猜测字段）：

| 行为 | 本地源码已有接口 |
| --- | --- |
| 创建并可重试 | `POST /api/session`，传 `id` 和 `location.directory` |
| 读取会话 | `GET /api/session/{id}`；解析 `data.title`、`data.time`、`data.location`、`data.parentID` |
| 活跃状态 | `GET /api/session/active`，每轮/服务查询一次，返回集合中的 ID 视为 running，其余为 idle |
| 重命名 | `PATCH /session/{id}`，`{title}`，并携带映射目录 |
| 停止运行 | `POST /api/session/{id}/interrupt` |
| 删除 | `DELETE /session/{id}`，并携带映射目录；已确认 404 可视为已删除 |

读取后只按当前授权和关联更新缓存；时间统一转换到现有列表使用的单位。刷新只 UPDATE 仍有关联的行，不用 upsert 复活已删除缓存；远端更新时间早于缓存的结果不覆盖较新标题。OpenCode 403/401/超时/5xx 映射 `coding_upstream_unavailable`，保留缓存；只有具体会话 GET/DELETE 的确认 404 能清理对应关联。列表缺失、分页截断和连接异常不是删除证据。

重命名和删除均先完成远端操作，再提交本地缓存；失败可重试。删除运行中会话先 interrupt，再 delete；不走普通取消注册器。远端成功而本地更新失败时，下一次 GET 或重复操作修复；不增加补偿队列。远端确认删除后在同一事务删除关联及该会话缓存，保留代码项目文件。

### 5. 轮询刷新，保持实现小

新增前端 `window.CodingChat` 一个命名空间，管理 iframe、当前上下文和唯一轮询器；接口采用 `launch(agentId)`、`open(sessionId, agentId)`、`refresh()`、`leave()`。不复制普通消息渲染和输入逻辑。

- 单条 `setTimeout` 链，当前请求结束后 5000ms 再刷新；coding 对话或历史列表可见且浏览器标签页可见时运行。
- 进入页面、隐藏转可见和远端管理成功后立即刷新，刷新成功重置计时；任何时刻最多一轮在途。
- sync 每批最多 50 个关联，前端顺序消费 `next_cursor` 到结束；服务端读取并发最多 4，HTTP connect/read timeout 使用 `(3, 10)` 秒，active 接口每个轮次复用一次。无需为所有智能体并发启动计时器。
- 只刷新当前用户有权查看的 coding 智能体；历史页刷新全部相关 Agent，聊天页优先当前 Agent。完成后重取已有列表接口，让既有排序/搜索正确反映缓存。
- 使用世代号和页面/Agent/身份快照丢弃迟到结果；401/403 停表并移除对应 iframe，服务暂不可用保留缓存并下轮重试。
- 取消、隐藏、切换页面只停本地刷新/展示，不向 OpenCode 发中止执行请求。再次打开历史会话恢复远端当前内容。

列表同步的目标是“健康服务下下一个成功刷新轮次可见”；5 秒是调度间隔，不是包含网络耗时的强 SLA。选择此方案是为了免除事件去重、持久化游标和重放服务，用户功能保持不变。

### 6. OpenCode 嵌入改动与实际装载点

OpenCode `packages/app` 新增一个小的 embed 上下文模块，集中识别 `rsm_embed=1`、`rsm_parent_origin` 和每次 iframe 挂载生成的 `rsm_channel`。上下文存活于该次 app 挂载，不能因内部路由丢失 query 而消失。只在 embed 模式隐藏项目/服务器切换、全局设置及重复会话导航；保留输入、工具权限确认、差异、文件、终端和分叉功能。正常访问 OpenCode 不改变。

优先使用现有 `/:dir/session/:id` 路由（目录按 OpenCode 的 base64 URL 工具编码）；适配新旧布局对该路由的重定向，不能在平台手工复刻 `serverKey` 算法。embed 模式固定到当前网页对应的服务，避免 OpenCode 本地存储中上次连接的其他服务抢占当前会话。

小型通知只需要两种：

```text
{type: "rsm.opencode.ready", channel}
{type: "rsm.opencode.session", channel, session_id}
```

子页完成初始化发送 ready；用户操作导致当前会话改变时发送 session（包括分叉和页面内新建）。父页同时校验 `event.origin`、`event.source===iframe.contentWindow`、当前 channel 和消息结构，再携带自己的当前平台 session_id 调用 attach。当前远端 ID 未改变时直接忽略；登记成功只更新父页选中项，不重载已经处于目标会话的 iframe，避免通知循环。不能依通知直接认领、改标题或授予权限；旧 iframe 消息丢弃。切换父页历史记录直接更换 iframe URL，无需双向指令总线。

rsmagent 新增 `static/js/coding.js`、必要的局部样式及中文/i18n 文案；经真实 `chat.html` 装载。`console.js` 仅在智能体表单、工作台启动、会话打开/管理与导航退出位置调用该模块；禁止在它之后重新声明同名全局函数。上游拆分模块只有在实际参与当前 fork 页面装配时才同步接入，不在本 change 完成全量前端拆分迁移。

### 7. 部署约定与认证边界

默认把定制 app 构建部署在独立 Web origin 的根路径，例如 `https://code.example.com/`。同一 origin 的 API、SSE、WebSocket 请求由现有反向代理转到同一 OpenCode 服务。这样沿用 app 的 `location.origin`，本期不改 Vite/Router 支持任意子路径。平台 backend 使用 `api_url`，浏览器使用 `web_url`；远程用户不能收到 `http://localhost:4096` 作为生产 iframe 地址。

使用本地 app 构建并固定前后端配套版本；不能依赖无内置资源时代理 `app.opencode.ai` 的默认行为。iframe 父页允许配置的 frame origin，子页 `frame-ancestors` 允许平台 origin；检查实际代理响应头而非只看源码。SSE 不缓冲、WebSocket 可升级，HTTPS 页面配套 HTTPS/WSS。

浏览器访问使用部署现有的 OpenCode 认证流程；如只有 Basic Auth，使用 OpenCode 原有连接登录界面，首次可能需要登录。rsmagent 服务器密码不通过 URL 或 postMessage 下发。此设计没有统一免登需求，不引入新票据系统或自建 WebSocket 网关；如部署要求无感 SSO，另行变更范围。

分配权限约束平台入口和平台会话管理；共享 OpenCode 自身可见数据及原始地址由其部署认证控制。已经进入共享服务不等于具备平台的逐用户执行隔离。由用户明确接受的本期范围，对应 `execution-isolation` delta；不要求额外建设执行隔离才可验证这条外部路径。

## Risks / Trade-offs

- [两版本 API 并存] → 阶段 1 用真实服务验证 create/read/rename/interrupt/delete 与 iframe 展示同一条会话后再编码；客户端固定这些具体接口，不修改 OpenCode 后端补统一 API。
- [前端源码文件与实际页面不一致] → 先确认 `ChatHandler` 输出和实际脚本列表；新模块必须出现在浏览器 Network。不能以修改了拆分文件替代真实验收。
- [查询刷新成本随会话数增加] → 关联分页、并发上限和页面可见性控制；本期不引入后台队列。以后确有规模问题再评估事件方案。
- [OpenCode 内创建后父页正好关闭] → 下一次显式打开该远端会话由 attach 补登记；不扫入共享服务其他历史。主新建入口由平台发起，避免常规路径依赖通知。
- [旧规范对所有代码执行要求隔离] → 本 change 显式限定外部共享 OpenCode 为已批准的例外，保留现有平台授权和普通执行器隔离，不能宣称它满足租户隔离。

## Migration Plan

1. 阶段 1 只验证真实服务和浏览器嵌入条件，记录配套版本及接口响应；不重启开发者现有服务，使用其已运行服务或单独验证实例。
2. `agent_type` 通过缺省值兼容，正常加载不得重写所有旧档案。会话关联表通过现有 schema 初始化流程 `CREATE TABLE IF NOT EXISTS` 添加；普通会话行与消息不改写。
3. 先部署 OpenCode embed 前端，再部署平台代码；配置 `enabled=false` 时执行普通智能体回归，之后启用并执行真实端到端验收。
4. 关闭开关后保留 coding 档案、关联、列表缓存及 OpenCode 数据，页面标记不可用；重新启用可恢复。回滚平台程序前关闭开关，并保留新增配置字段和关联表，禁止通过删除远端会话回滚。
5. 实施过程中新增的证据放本 change 的 `evidence.md`：记录实际测试命令、结果和浏览器验收；禁止把本设计中的预期填写为通过。
