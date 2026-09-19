# rdai 功能整合快交付 Implementation Plan

> 本文保留总体范围与早期评估。开发请以 [OpenSpec 开发任务书](../../../openspec/changes/integrate-upstream-core-capabilities/implementation.md) 和 [44 项实施任务](../../../openspec/changes/integrate-upstream-core-capabilities/tasks.md) 为准；tasks.md 是唯一进度清单。

**Goal:** 分两批交付可验证的上游功能：首批接通上下文与模型配置，第二批接通调度功能；Web 与 Desktop 的入口、接口和授权保持一致。

**Architecture:** 沿用 rdai 的身份、租户、资源授权与现有 Web shell。上游实现作为业务逻辑和响应格式的参考，适配代码放入 fork 模块；通过现有 capability matrix 和 route registry 声明服务能力。前端结构迁移作为单独交付项，按功能移植的记录继续推进。

**Tech Stack:** Python / web.py / SQLite，Web 经典 JavaScript，Desktop Electron / React / TypeScript，pytest / node:test。

**Spec:** `openspec/specs/fork-upstream-decoupling/spec.md`、`openspec/specs/database-scheduler-console/spec.md`、`openspec/specs/tenant-resource-isolation/spec.md`；本文件为概要；已生成 change `integrate-upstream-core-capabilities`，本轮只完成规划产物。

## 设计决策与范围

推荐采用“按功能接通、分批交付”。另外两条路径是：先完成全部 109 处前端裁定再发布，或只关闭不兼容入口。前者首批交付周期较长；后者能够止住失败请求，但没有完成新增功能整合。

本方案让首批用户得到上下文用量、手动压缩、模型目录编辑和有序回退链；第二批得到任务创建与运行历史。每批均有独立验收结果。

现有规范要求最终退出上游已废弃的前端单体形态。本方案保留单体仅是阶段性安排，不能据此关闭前端迁移事项或宣称规范全部满足。109 处裁定及模块切换继续独立跟踪，每个提前移植的功能登记原始上游提交、目标函数与验收用例，以免后续模块迁移覆盖这些修复。

### 已核实的基线

- 上游 `8f1b19f1`；本地 rdai `f5d7d764`；上游已是 rdai 的祖先。
- 本地尚有未提交修改，实施时从确认后的提交建立隔离分支；不把在途的待办 UI 和归档修改混入本方案。
- 后端 `channel/web/api/**`、`channel/web/core/**` 与该上游提交一致；服务中的定制实现位于 `channel/web/fork/**`。
- 干净检出测试：5778 passed / 27 failed / 31 skipped。27 项由 26 项普通失败与 1 项源文件校验子测试失败组成，均在合并前版本复现。
- 专项回归 114 passed / 2 skipped，独立前端测试 17 passed；路由覆盖和后端模块边界检查通过。
- 前端迁移有 109 处未裁定区域、29 项未完成任务。

## Global Constraints

- 保持单一权威路由清单：新增路由只在 `channel/web/route_registry.py` 登记，由它派生 URL 和策略。
- 不改写 `channel/web/api/**`、`channel/web/core/**` 的上游实现，不直接把其中的 `_require_auth()` 当作 rdai 授权。
- 每次请求独立验证身份、当前租户、Agent 和资源归属；前端显示状态不授予权限。
- 能力状态复用 `auth/capability_matrix.py`。能力不存在、未验收或旧服务器未提供声明时，客户端关闭对应入口及后台轮询。
- capability 声明描述服务可用性；用户操作仍按已有权限投影和服务端检查决定，不能把“功能存在”解释为“当前用户获准”。
- Web 保留现有脚本装载次序。新功能使用独立命名空间，通过明确调用点接入，禁止同时装载上游整套视图再覆盖同名全局变量。
- 首批不交付 Web 一键更新、微信个人渠道开放、整个前端 shell 切换；它们分别保留明确的后续交付记录。
- 不改变身份模型，不引入共享密码或 renderer 持有的 desktop token。
- 逐批验收后回退代码即可撤销新增入口；如第二批需要增加数据结构，必须采用向后兼容扩展，独立提供恢复步骤。

## 交付节奏与估算

按一名熟悉仓库的工程师、现有开发环境可用估算，不是完成时间承诺。

| 批次 | 内容 | 估算 | 用户能得到什么 |
| --- | --- | --- | --- |
| R1 | 能力状态、上下文两接口、模型配置入口、必要回归 | 2–3 人日 | 已支持功能能实际操作，未支持入口有明确状态 |
| R2 | 六个调度接口、资源授权、两端验收 | 2–4 人日 | 可创建任务、选择合法投递目标、查看和删除有权访问的运行记录 |
| R3 | 109 处前端裁定、模块装载切换、旧单体退出 | 独立估算 | 完成结构收敛，降低后续上游同步成本 |

以上为初步粗估。详细任务书已确定增加运行归属扩展表及压缩并发保护，更新估算为 **5–8.5 人日**（含验收）；按 change 的分项排期执行。

## Task 1：能力声明与客户端一致性（R1）

**修改范围：** `auth/capability_matrix.py`、`channel/web/route_registry.py`、现有能力投影处理器、`desktop/src/renderer/src/api/client.ts`、`desktop/src/renderer/src/pages/TasksPage.tsx`、`desktop/src/renderer/src/components/ContextUsagePopover.tsx`、`desktop/src/renderer/src/hooks/useSchedulerNotifyPoll.ts`。

- 为 `scheduler.create`、`scheduler.instances`、`scheduler.recipients`、`scheduler.runs.list`、`scheduler.runs.detail`、`scheduler.runs.delete`、`session_context.usage`、`session_context.compact` 建立逐项交付表，与现有 capability 声明关联；尚未验收的项不开放。
- 从现有登录/上下文刷新链路消费能力投影。能力缺失时不发对应请求；切换租户、退出登录时清除缓存并停止旧轮询。
- 客户端区分“当前版本未开放”“没有操作权限”“服务请求失败”“没有数据”。不把 403/404/405/500 一律转换为空列表。
- 增加 `tests/test_desktop_feature_availability.cjs`：覆盖旧服务器无声明、能力关闭、登录后开启、租户切换清空、错误可见，以及关闭后不再轮询。
- 实测失败用例后实现，复跑两项既有门禁与客户端测试，单独提交。

**交付判据：** 同一能力在能力声明、路由、两端入口中状态一致；未支持功能不再产生循环错误请求。此项完成只代表兼容性闭环，不算该功能已经整合。

## Task 2：上下文用量与手动压缩（R1）

**修改范围：** 新增 `channel/web/fork/handlers/context.py`、`channel/web/web_channel.py` 的 fork 导出、`channel/web/route_registry.py`、两端对应入口。

**参考：** `channel/web/api/sessions.py::SessionContextUsageHandler` / `SessionCompactContextHandler`、现有 `_require_session_scope` / `_require_session_owner` 和 `bridge/agent_bridge.py::peek_agent`。

- 在 fork 内实现 `GET /api/sessions/<id>/context_usage` 和 `POST /api/sessions/<id>/compact_context`；两条具体路由置于 `/api/sessions/(.*)` 之前，避免再次被通配路由吞掉。
- 先进入现有身份/租户作用域，再验证 session 与 Agent 的关系；通过权限检查后才调用 `peek_agent`。复用现有运行时会话寻址方式，不根据请求中的 Agent ID 猜测默认实例。
- 保持上游响应字段与 Desktop 调用约定。合法空会话返回 `available=false`；用量查询不创建 Agent、不调用模型。压缩写操作继承现有写请求来源检查，并验证活动生成期间的并发处理。
- 新增 `tests/test_session_context_scope.py`：owner 正向、同租户其他成员、跨租户、伪造 Agent、匿名、无实时上下文、非法 Origin、活动生成期间压缩。
- 将 `test_web_database_capability_acceptance.py` 中这两项从“预期 405”替换为正向及拒绝矩阵；其余未交付接口继续保留缺口断言。
- 验收通过后开放能力，两端各完成一次真实会话的用量查询和压缩；单独提交。

**交付判据：** owner 正常读取和压缩；非 owner 无法读取或触发压缩；非默认 Agent 的实际上下文能被正确定位。

## Task 3：把已有模型后端接入当前 Web（R1）

**修改范围：** 新增 `channel/web/static/js/functional-models.js` 和 `tests/test_functional_models.cjs`；修改 `channel/web/static/js/console.js` 的明确调用点、`channel/web/chat.html`、`channel/web/fork/handlers/pages.py` 的资产登记以及已加载的模型配置 i18n 文件。

**已有服务：** `channel/web/fork/handlers/models.py` 的 `save_catalog`、`_chat_fallback_capability`、`_set_chat_fallback`。现有 Web 仍以单备用模型表单渲染，需要接入链式编辑。

- 新模块使用唯一命名空间 `window.RdaiFunctionalModels`，只承载目录编辑和回退链编辑；在旧控制台的两个现有入口显式调用，不动态替换或重新声明整个上游视图。
- 提供回退链的添加、删除、上下排序和启停。读取、保存、重新进入页面后，所有节点及顺序保持一致；不得把后端链式配置重新保存成单节点。
- 提供按 provider 的模型目录编辑、隐藏/恢复与保存回读；保留后端平台配置权限，不扩大普通成员的编辑权限。
- 复用现有后端动作和参数。Tavily / SearXNG / Keenable 已接通，保留并回归，不重复实现。
- 前端测试覆盖链式配置无损回写、自定义 provider、目录删除后的状态、拒绝与请求失败提示；后端复跑 `test_model_catalog_api.py`、`test_chat_model_fallback.py`。
- 为两个功能登记来源模块、上游 SHA、fork 入口和测试，供 R3 对照迁移；单独提交。

**交付判据：** 在当前 Web 页面完成配置—保存—刷新—再次读取，数据一致；凭据不出现在浏览器日志或错误提示中。

## Task 4：六个调度接口与记录作用域（R2）

**修改范围：** `channel/web/fork/handlers/scheduler.py`；新增 `agent/tools/scheduler/run_access.py`；必要时扩展 `agent/tools/scheduler/authorization.py` 的领域操作；同步 fork 导出、权威路由与能力声明。

| 路由 | 操作 | 必须落实的约束 |
| --- | --- | --- |
| `/api/scheduler/instances` | GET | 只列出当前租户、当前身份有权使用的投递实例，个人实例隔离 |
| `/api/scheduler/recipients` | GET | 按已授权实例和可信接收者目录列出，禁止跨租户联系人枚举 |
| `/api/scheduler/create` | POST | 从可信实例/接收者推导投递目标，调用 `TaskAccessService.create_task`，保留 owner、quota、审计 |
| `/api/scheduler/runs` | GET | 授权范围进入查询条件，再执行排序和分页；空 Agent 参数不得表示全局无范围查询 |
| `/api/scheduler/runs/detail` | GET | 校验运行记录和关联会话的读取权限后，才读取完整输出 |
| `/api/scheduler/runs/delete` | POST | 校验删除权限与记录归属，在同一限定条件下删除台账，不删除会话消息 |

### 明确的实现边界

`ConversationStore.list_runs()` 当前不自动继承 store 的 Agent 维度，`get_run_detail()` 又通过 Agent/session 拼接消息。因此直接包装上游 handler，或者仅先检查“可使用 Agent”，都不足以隔离同 Agent 下不同成员的数据。

新增的 `run_access.py` 承载运行记录的授权、限定查询和详情读取，HTTP handler 只负责请求与响应转换。禁止先读取全局结果再在浏览器过滤，也禁止无权限范围时回退到全局台账。

优先从现有任务 owner、租户绑定和会话所有权取得可信范围。已删除任务、Agent 重新绑定、历史记录缺少归属证明时，默认不展示详情、不允许删除；列表不得据当前 Agent 绑定把历史记录重新归属给新租户。若现有字段不能证明归属，添加 fork 所有的归属扩展记录并同步运行写入链路，只回填可证明归属的历史记录。

- 新增 `tests/test_scheduler_run_access.py`，覆盖两个租户、同 Agent 下两名成员、公共任务与个人任务、任务已删除、实例重绑、无归属历史记录、分页和删除。
- 先实现 `instances` / `recipients` / `create` 三接口。复用可信接收者校验和 `TaskAccessService.create_task`；拒绝请求伪造 owner、租户、接收者属性及其他受保护字段。
- 再实现 `runs` 三接口及运行记录领域服务。新增一次有权执行任务的记录，通过真实请求验证其列表、详情、删除全链路。
- 新增 `tests/test_scheduler_fork_api.py` 验证六接口响应与 Desktop 现有字段兼容；逐项替换已有的“预期 404”缺口断言。
- 两端完成真实创建与任务执行验证后，逐项开放能力；不可用的投递渠道保持关闭并显示原因。
- 将任务创建、运行历史拆成两个可独立审核和回退的提交。

**交付判据：** 六接口业务成功路径可用，跨租户和同租户非 owner 访问被拒；任务列表、后台执行和运行历史读取的是一致的业务记录。

## Task 5：发布门槛、既有失败和回退（随 R1 / R2 分别执行）

- 优先审定两项外部连接权限失败：普通成员管理与目录读取的预期授权。若为真实越权，阻断发布；若为过时断言，依据现行规范修正测试并保留拒绝覆盖。
- 修复三个明确的一致性问题：菜单迁移角色版本断言、读取已退役 `personal-console.js` 的测试、manifest 引用却被 `.gitignore` 排除的 SAP README。文档纳入 Git 或从 manifest 移除必须与资源交付要求一致，不可只为让测试通过删除校验。
- 微信扫码 22 项失败按现行能力边界裁定。首批不扩大渠道范围；关闭状态、页面提示、后端拒绝必须一致。不能通过简单跳过这批测试宣称微信功能已通过。
- 运行新增用例及受影响模块，之后运行下列检查；Desktop 使用已验证的后端构建进行真实验收。

```bash
.venv/bin/python scripts/check-route-coverage.py
.venv/bin/python scripts/check-web-module-seams.py
.venv/bin/python -m pytest tests/test_web_database_capability_acceptance.py tests/test_scheduler_task_authorization.py tests/test_conversation_runs.py tests/test_model_catalog_api.py tests/test_chat_model_fallback.py tests/test_no_resurrection_legacy_identity.py -q -p no:randomly
node --test tests/test_console_search_providers.cjs tests/test_execution_permission_ui.cjs tests/test_desktop_context_frontend.cjs
npm --prefix desktop ci
npm --prefix desktop run build
.venv/bin/python -m pytest tests/ -q -p no:randomly --ignore=tests/e2e
```

- Web 与 Desktop 分别验证登录、租户切换、流式对话、上传回读、模型设置，以及该批新增能力；真实模型压缩、实际调度执行单独记录结果，模拟用例不替代实机结果。
- 只接受明确列出原因和产品关闭状态的既有失败；新增失败、权限隔离失败、数据损坏和已开放功能失败均阻断对应批次交付。
- 最终证据保存精确提交、命令、失败与跳过清单、真实入口截图/日志。包含 subtest 失败，避免只统计以 `FAILED` 开头的行。
- 每批按已验收的同一后端与客户端组合交付。回退顺序为先关闭该批能力与入口，再回退该批提交；任何数据扩展保持旧版可读，不采用破坏性降级。

## 两批验收清单

| 检查 | R1 | R2 |
| --- | --- | --- |
| 关闭能力不发后台请求，切租户不沿用旧缓存 | 必须通过 | 必须通过 |
| 上下文用量、压缩与会话所有权 | 必须通过 | 回归通过 |
| 模型目录、回退链保存回读 | 必须通过 | 回归通过 |
| 任务创建、合法投递目标 | 明确未开放 | 必须通过 |
| 运行记录列表、详情、删除 | 明确未开放 | 必须通过 |
| 两租户、同租户不同成员隔离 | 必须通过 | 必须通过 |
| 两端真实操作、原有核心功能回归 | 必须通过 | 必须通过 |
| 新增失败为零、已开放功能无失败 | 必须通过 | 必须通过 |

## 后续前端结构迁移的退出条件（R3）

按模型设置、调度、会话、其余视图分组推进既有 109 处裁定，每组对照 R1/R2 的来源与行为测试。全量裁定完成、模块装载顺序验证通过、两端回归通过后，才删除 `console.js` / `console.css`，解除对应的前端跳过项。Web 一键更新另做权限、部署类型和回退验收，不在本方案中自动开放。
