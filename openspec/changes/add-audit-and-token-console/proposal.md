## Why

本仓库的 OneAgent 源码（`C:\oneagent-multi-rc`）里「平台运维」分组带两个只读运维页：**审计日志**与 **Token 消耗**。`rsmagent` 之前没有它们：审计数据其实一直在写（`auth/audit.py` 的 `audit_events`，与本仓库的身份库同库同事务），只是没有页面能读；LLM 用量则完全没有记录，因此「这个租户这个月花了多少 token、谁花的、哪个模型」在控制台无法回答。

两个页面在源实现里有两条本仓库不能照抄的前提，本次的取舍都由此而来：

- **数据源不同**。源实现的审计是各租户库里的会话审计，token 用量是每个租户工作区里的 `conversations.db`（`manager._resolve_stores` 在读取时扇出合并）。本仓库的租户身份、审计与用量都在同一张 `identity.db` 里，所以「当前租户」是一次带 `WHERE` 的查询而不是扇出合并——扇出在这里只会带来「某个租户工作区恰好缺失就静默漏数」这条源实现没有的失败路径。
- **计量点不同**。源实现逐个 provider 在各自的 `call_with_tools`/`reply_text` 里埋点，结果它自己的十个 provider 一个都不上报。本仓库所有 provider 都经过 `AgentLLMModel.call` / `call_stream` 两个分发点，所以埋在这两个点上等价于「覆盖全部 provider，且以后新增的也自动覆盖」——这正是源实现丢掉的性质。

## What Changes

- 新增控制台页面 **审计日志**（page id `admin.audit`）与 **Token 消耗**（`admin.token_usage`），置于控制台第四个分组 `nav_group_platform_ops`（显示名现为「平台管理」，见下条）。该分组的平台边界**判在逐项上而非分组上**：租户管理员因此能看到这两项（接口本就把他们收窄到本租户），而同组的租户管理 / 平台用户管理 / 品牌设置 / 运行日志仍只对平台管理员开放。分组显示名由「平台运维」改为「平台管理」，条目 `menu_platform` 由「系统设置」改为「平台用户管理」（它与 `nav_system` / `admin.settings` 这个部署级配置页重名，而实际打开的是全局账号目录页）。
- 新增只读接口 `GET /api/admin/audit/events`、`GET /api/admin/audit/actions`、`GET /api/admin/token-usage`。**读取范围由调用方资格推导，MUST NOT 由请求参数推导**：平台管理员跨租户读，租户管理员只读自己所选租户，其余一律 403；平台管理员可用 `?tenant=` 收窄，租户管理员该参数 SHALL 被忽略（不是被拒绝——403 会确认该租户 id 存在，正是越权探测想要的信息）。
- 审计日志页 SHALL 支持时间区间、动作、操作者、结果四类过滤与分页；结果过滤的「失败」SHALL 表达为「不等于成功」（`denied`/`error` 对运维都是失败，硬编码非成功值列表会在新增结果值时静默失效）；操作者 SHALL 按**记录在行上的用户名**匹配（改过名的账号，历史行留着旧名，先解析成 user id 再查会在最需要它的场景返回空）。
- 在既有审计写入之外补齐 `auth.login`、`auth.logout` 与智能体管理动作（`agent.create` / `agent.update` / `agent.archive` / `agent.delete` / `agent.knowledge_mode.set` / `agent.bind_channel_instance`），**被拒绝与被拒绝的写尝试同样留痕**（`result="denied"`），动作名沿用本仓库既有的 `资源.动作` 点分层级。
- LLM 调用统一计量：在 `AgentLLMModel.call` / `call_stream` 两个分发点埋点，记录 prompt/completion/total、provider、model、租户、主体、会话、耗时与输入/输出摘要；**流式、非流式但返回生成器、以及抛异常/被提前放弃的调用都要结算**（异常记为 `status="error"`，否则控制台会显示「发生过一次调用」却看不到解释它的失败）。
- 用量写入走队列 + 后台 flush 线程（LLM 调用 MUST NOT 等待 SQLite）；同键事件先在内存合并再 upsert。
- Token 消耗页 SHALL 提供汇总卡片与「明细 / 按用户汇总 / 调用日志」三个页签，按时间、模型、供应商、主体过滤；模型与供应商下拉 SHALL 从既有记录取值。
- 页面 SHALL 把「读失败」与「没有记录」分开呈现，MUST NOT 把查询故障渲染成空列表。
- 控制台页面可用性投影（`/auth/context`）SHALL 对这两页按同一资格作答，MUST NOT 在页面已可服务时报告 `consumer_closed` / `deferred`（那是关于**部署**的断言）。

## Capabilities

### New Capabilities

- `token-usage-console`: LLM 调用的统一计量（覆盖全部 provider 的两个分发点、流/异常/放弃调用的结算、队列化写入）与「Token 消耗」只读页面的读取范围、页签、过滤与展示口径。

### Modified Capabilities

- `audit-log`:
  - MODIFIED「审计查询有边界」——把「审计列表」落成控制台页面：读取范围由资格推导而非请求参数、平台管理员的收窄语义、租户管理员的参数忽略语义、四类过滤与分页的确切含义（含「失败=非成功」与「按记录的用户名匹配」），以及读故障不得呈现为空。
  - ADDED「审计页面的动作清单由记录派生并同范围过滤」——`GET /api/admin/audit/actions` 的范围、派生来源与「不另立一份动作名清单」。

## Impact

- **行为受影响**：新增两个菜单与三个只读接口；登录、登出与智能体管理写入新增审计事件（各租户既有审计行数会增长，属预期）。未改动任何既有授权判定与写路径的语义。
- **不受影响**：`auth/audit.py` 的追加式语义与脱敏、`audit_events` 表结构与既有查询（`query_tenant`/`query_platform` 保持原样，新增的是分页过滤的 `query_events`）；既有审计动作的名称与结果值；LLM 调用的请求/响应契约（埋点只观察，不改写返回值——流式路径把返回的迭代器包一层再交回调用方，异常原样重抛）；identity 内存库以外的数据布局。
- **代码面**：新增 `agent/token_usage/{store,manager,recorder,instrument}.py`、`channel/web/admin_audit_handlers.py`、`channel/web/admin_token_usage_handlers.py`、`channel/web/static/js/audit-console.js`、`channel/web/static/js/i18n/audit-console.js`；改动 `auth/audit.py`（`query_events` 增 `actor_username`/`exclude_result`）、`auth/service.py`（登录审计、`_SIGNED_CONSOLE_PAGES` 两页与可用性分支、`query_audit_events`/`audit_action_names` 两个读缝）、`auth/store.py`（迁移 33）、`bridge/agent_bridge.py`（两处埋点）、`channel/web/{route_registry,web_channel}.py`（路由登记）、`channel/web/chat.html`（菜单与两个视图）、`channel/web/static/js/console.js`（`VIEW_META` 两行）、`channel/web/static/js/identity-admin.js`（让出旧审计视图）、`channel/web/static/js/i18n/navigation.js`。无新增配置项、无新增前端依赖。
- **测试面**：`tests/test_audit_query.py`、`tests/test_token_usage.py`、`tests/test_token_usage_instrument.py`、`tests/test_admin_audit_console_http.py`（新增：范围判定、过滤语义、投影）、`tests/test_audit_token_console_wire.py`（新增：真实 WSGI 应用上的跨租户隔离与页面投影）、`tests/test_token_usage_tabs_frontend.cjs`（新增：三个页签的可达性与切换行为——标记与 JS 的接缝，见 design D9）、`tests/test_console_view_registry.cjs`（视图注册迁移）、`tests/test_console_i18n_*.cjs` 与 `tests/fixtures/console_i18n_snapshot.json`。
- **i18n 面**：新增 `channel/web/static/js/i18n/audit-console.js` 命名空间，提供 `zh` / `zh-Hant` / `en` 三语；`menu_audit` 由「稽核」改为「审计日志」并新增 `menu_token_usage`（在 `navigation.js`，因为菜单名属导航命名空间）。
