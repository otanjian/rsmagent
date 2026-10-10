## Why

原生 SAP 智能工作台为每个工作台会话启动一个专属 OpenCode 引擎进程（`bun.exe`），并把引擎数据库指向 `scenes/sap_workbench_runtime/<binding>/opencode.db`。引擎启动预算只有 45s（读 port）与 70s（整体），HTTP 等待 125s；部署机冷启动超预算即失败为 `opencode_start_timeout`，前端显示「工作台对话服务启动超时」，表现为工作台「又进不去了」。新建会话还会先关闭同账号其他绑定并连带杀掉旧引擎，`monitor()` 又在空闲后关闭引擎，硬杀后残留的 `creating` / `host_starting` 没有对账，用户看到「一直处于启动状态」。

这些复杂度全部来自「工作台自己持有引擎」这一层。而平台已有成熟的编码智能体入口：`CodingSessionService` 负责平台会话与 OpenCode 会话的关联（`reserve` / `open` / `attach` / `sync`），控制台 `coding.js` 负责挂载内嵌 iframe 并处理 ready/session 通知，历史由平台的会话列表同步。OpenCode 侧也已原生支持项目目录内的 skill 发现与插件工具注册。因此工作台不需要自己的引擎，只需要把自己接到这条既有链路上，并把 SAP 能力以项目目录产物交付。

## What Changes

- **对话改用平台编码智能体入口**：SAP 工作台的页内对话 SHALL 挂载场景配置的 coding 智能体（场景配置已有 `coding_agent_id`），经平台的编码会话入口创建/打开会话并同步历史；MUST NOT 为工作台会话启动专属引擎进程、专属监听端口或专属对话数据库。
- **沿用平台会话粒度**：一个会话对应一个 `request_id`（平台既有语义），用户看到的是普通会话列表并可通过既有同步与 attach 延续；不再要求的「一账号一项目目录一个会话」被本变更取代。
- **SAP 能力以项目目录产物交付**：操作能力 SHALL 由项目目录内的插件工具承载（OpenCode 插件可注册自定义工具，工具上下文带会话与项目信息），业务知识与流程 SHALL 由项目目录内的 skill 承载。MUST NOT 依赖工作台自建宿主注入工具，MUST NOT 要求用户安装场景专用 OpenCode 构建。
- **服务端按会话反查归属**：工具执行时的身份与归属 SHALL 由服务端依据会话标识解析（用户、工作台绑定、浏览器租约、控制代次、动作账本），MUST NOT 采信模型或请求体声明的身份。
- **能力边界如实标注**：左侧 SAP 画面是用户浏览器中的跨域 iframe，可被导航但不可被读取或填写；已完成与未完成的能力 SHALL 分别标注，MUST NOT 以「skill 已就位」宣称页面读写或业务提交可用。
- **可见性收窄**：项目目录内的插件与 skill 对该项目的所有会话可见，SHALL 通过权限规则把 SAP 工具收窄到场景发起的会话，工具内仍逐次服务端校验。
- **标准服务就绪与存活**：打开工作台前 SHALL 探测标准服务就绪，未就绪立即报告可辨识原因；部署 SHALL 守护服务端口并在异常退出后自动恢复，且 MCP 网关失败 MUST NOT 阻断 API 与 Web 嵌入服务的启动。

## Capabilities

### New Capabilities

- `sap-workbench-project-toolkit`：项目目录内交付 SAP 能力的插件工具与 skill、左侧画面的导航通道、按会话标识的服务端归属解析与授权、工具可见性收窄、能力边界与证据标注。

### Modified Capabilities

- `sap-workbench-scene`：页内对话改为平台编码智能体入口，配置投影绑定 OpenCode 服务与项目目录，移除独立引擎与独立对话数据库的表述。
- `sap-workbench-session-binding`：会话关联与幂等创建改为「平台编码会话 + 派生会话标识」，删除引擎所有权与「新建自动替换同账号绑定」语义。
- `opencode-coding-agents`：新增标准 OpenCode 服务的就绪探测与存活保障要求（新增要求，不改动既有三条）。

## Impact

- 接入点：`Scene/sap_workbench/`（页内对话挂载与能力交付；`http.py`、`configuration.py`、`frontend/workbench.js`）、项目目录内的插件与 skill 产物、以及按会话标识解析归属的服务端端点。
- 复用：`agent/coding/sessions.py`（`CodingSessionService`）、`agent/coding/opencode.py`、`agent/coding/__init__.py`（`CodingSettings`）、`channel/web/fork/handlers/coding.py`（四个编码会话端点）、`channel/web/static/js/coding.js`（iframe 挂载与通知）。
- 退役（停用而非删除）：`Scene/sap_workbench/backend/runtime.py` 的专属引擎宿主与读 port 预算、`prewarm.py`、UI 反向代理与事件改写、引擎桥接，以及 `opencode_adapter/` 中仅供自管引擎使用的 `server.ts`、`host.ts`、`native-host.ts`、`credentials.ts`、`context.ts`。默认路径不再选中它们；代码保留在单一回滚开关 `SAP_WORKBENCH_LEGACY_ENGINE` 之后（缺省关），旧库与旧配置不删除，走到旧分支时写 warning 日志。旧按绑定历史按 design 的处置默认不迁移。
- 保留：`navigation-guidance.js` 的项目插件机制与文案方向；`tools.ts` 的工具语义（导航与只读），但改由项目插件注册。
- 部署：新增标准服务看门狗，并修正开机脚本卡在 MCP 网关阶段而不再继续启动 API 与 UI 的行为。
- 已接受限制：共享标准实例使用一份服务凭据，其会话列表在未指定目录时返回全库会话。本变更保证「打开的是自己的会话」，不保证他人无法通过共享凭据浏览会话列表。现行 `sap-workbench-session-binding` 的「OpenCode 上游会话访问受控」要求本次**不修改**，残余差距在 design 中如实记录为未达成项。
- 依赖：`sap-workbench-scene`、`sap-workbench-session-binding`、`opencode-coding-agents`、`opencode-embedded-chat`、`opencode-session-sync`、`scene-skills`、`resource-quota`、`audit-log`、`tenant-resource-isolation`。
- 不涉及：不修改 `rsmcode/opencode` 源码；不把 SAP 工具做成场景宿主桥；不宣称归档中未通过的 SAP 现场验收。
