## Why

SAP 工作台（场景 `sap_workbench`）的对话每次从卡片进入都会新建工作台会话，用户上一次的对话无法直接继续：即使该登录用户仍有一条可续的工作台绑定，卡片入口也走新建路径，把本来还能续的旧绑定顶成 `closed`。用户反馈「不用每次进来都新建对话」，希望先按场景与当前登录用户判断是否已有旧会话，有则打开最新一条继续对话，没有才新建。

入口所需的能力本来就已存在：`GET /api/scenes/sap-workbench/sessions` 按租户与登录用户返回其绑定（最新在前），`POST` 既支持 `binding_id` 复用也支持 `request_id` 新建。缺的是把「卡片入口」接到复用路径上，并把这一行为写进规范 —— 现行规范写的恰好相反（`scene-application-console` 要求卡片入口「直接新建」、新建与恢复「由明确入口区分」；`agent-chat-launch` 要求「再次使用同一个智能体」时新建且不自动恢复）。

## What Changes

- 自动入口（场景卡片、`/场景` 选择器、已登记的工作台直达）在配置与能力就绪后 SHALL **先复用该登录用户最新的可续工作台会话**，只在没有可续会话时才新建。
- 显式「新建工作台会话」入口 SHALL 始终新建。复用不得把「恢复」做成隐式，也不得取消显式新建入口。
- 复用范围 SHALL 限定为服务端解析的当前租户与当前登录用户（`tenant_id` + 登录用户），MUST NOT 跨用户或跨租户，MUST NOT 采信请求体声明的身份；只复用仍可续（`state != 'closed'`）的绑定。
- 配置版本变化或复用被拒（`config_conflict` / `session_not_running` / `session_closed`）时 SHALL 回退为新建，MUST NOT 静默续到已失效的 SAP 目标，MUST NOT 把用户停在错误上。
- 与普通智能体卡片区分：`agent-chat-launch` 的「再次使用同一个智能体即新建」只约束工作台**智能体卡片**的普通聊天入口，不适用于场景工作台入口。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `scene-application-console`：修订「场景选择器与激活交互」，把 SAP 工作台自动入口从「直接新建」改为「先复用最新可续会话，没有才新建」，同时保留配置入口、显式新建与显式恢复语义。
- `sap-workbench-session-binding`：在「幂等创建与部分失败恢复」中补充自动入口的复用选择规则、身份范围与拒绝后的回退行为。
- `agent-chat-launch`：在「会话与工作空间归属一致」中限定「再次进入即新建」的适用范围，避免与场景工作台入口的复用规则互相冲突。

## Impact

- 接入点：`Scene/sap_workbench/frontend/workbench.js`（自动入口 `open({view:'new-session'})` → `startSession(true)`：先 `GET /sessions` 取最新非 `closed` 绑定，命中则 `{binding_id}` 续会话，未命中才以 `{request_id}` 新建；显式「新建工作台会话」仍为 `startSession(false)`）。`Scene/_shared/frontend/adapter.js` 的卡片分发不变。
- 服务端不新增接口与字段：继续使用既有 `GET` / `POST /api/scenes/sap-workbench/sessions` 及既有绑定生命周期；`store.sessions(tenant_id, user_id)` 的归属范围不变，已 `closed` 的终态语义不变。
- 测试：`tests/test_sap_workbench_frontend.cjs` 覆盖「卡片入口续最新会话」「没有可续会话才新建」，并修正既有用例对「卡片入口只发起一次 POST」的假设。
- 不涉及：不改变普通智能体卡片与其他场景入口的既有新建行为；不要求复用被替换的终态记录；不新增数据库字段、迁移或功能开关。
- 与既有 change 的接缝：与 `enable-sap-rfc-bapi-channel` 只在同一前端文件重叠，互不修改对方的业务逻辑。
