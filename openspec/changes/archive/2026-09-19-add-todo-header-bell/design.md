## Context

工作台是「经典脚本 + 无打包器」的单页应用：`channel/web/chat.html` 是静态标记，`channel/web/static/js/console.js` 是上游共享的巨型模块，fork 自有前端模块（如 `channel/web/static/js/todos.js`）以 `defer` 在其后加载并自行注册视图。

当前相关接入点：

- 顶栏在 `chat.html` 的 `.workbench-header`（约 622–666 行），右侧依次是 `#tenant-selector`（数据库身份模式下可见，`onclick="toggleTenantMenu(event)"`）与 `#workspace-toggle-btn`（仅对话视图可见，注释明确「它是顶栏最后一个条目，以便当前视图拥有的动作占据右边缘」）。
- 视图导航是全局 `navigateTo(viewId)`（`console.js:1892`），带可用性门禁、离开确认与跨区域切换；侧栏「我的待办」条目是 `.sidebar-item[data-view="todo"]`。
- 计数来自 `GET /api/todos/summary`（`channel/web/todo_handlers.py` 的 `TodoSummaryHandler`，服务端 `agent/todo/service.py::summary` 返回 `enabled` / `bound` / `open` / `overdue`），前端只在 `channel/web/static/js/todos.js::refreshSummary` 里读取一次并交给 `applySummaryBadge` 渲染侧栏角标。
- tooltip 走浮层 portal：`console.js::installCfgTipPortal` 以**事件委托**监听 `[data-tip-key],[data-tip-float]`，`applyI18n()` 把 `data-tip-key` 的翻译写进 `data-tooltip`。
- 约束：`fork-upstream-decoupling` 要求 fork 定制前端「经独立模块并在稳定挂载点装载」，不得在上游共享文件中原地改写。

## Goals / Non-Goals

**Goals:**

- 让本人在**任意视图**下都能看到未完成待办数量并一键进入待办页面。
- 计数只有一份来源（既有 summary），刷新点与既有角标口径一致，不引入第二轮询/推送机制。
- 挂在 fork 自有模块里，冲突面最小：不改顶栏既有控件的行为与顺序语义。

**Non-Goals:**

- 不做通知中心、消息流、已读未读、免打扰或任何推送通道。
- 不新增后端路由、接口、配置项或能力开关，不改 `summary` 的契约与授权策略。
- 不改待办列表/详情/表单行为，不改侧栏入口与路由表。
- 不把铃铛做成「任务」「审批」「消息」的聚合入口——本 change 只服务个人待办。

## Decisions

### 1. 由 `todos.js` 在稳定锚点动态装载铃铛，而不是改 `chat.html`

`todos.js` 是待办自己的 fork 模块，已经承担待办相关的前端职责并注册到 console 的视图注册表。装载顺序取 `#tenant-selector`（存在则插到它前面，正好落在标题区与租户选择之间）；它不存在时回退 `#workspace-toggle-btn`；两者都不存在时追加到 `.workbench-header` 末尾。锚点缺失不抛错，静默跳过。

- **为什么不是直接写进 `chat.html`**：顶栏标记是上游共享文件，原地插入 fork 专有块会持续制造合并冲突，违反 `fork-upstream-decoupling` 的「前端经独立模块并在稳定挂载点装载」。`chat.html` 在本 change 中只被读取，不被修改。
- **为什么不用 `data-fork-fragment`**：该机制需要先在标记里放一个挂载点元素，仍然要改上游文件；顶栏目前没有挂载点，为此新增一个还不如按 id 锚点动态插入。

### 2. 计数复用 `refreshSummary()` 与 `applySummaryBadge()`，两处角标同源同刷

把角标渲染收敛到一个函数：同一次 summary 结果同时写侧栏角标与铃铛角标，并用同一个 `formatBadgeCount()` 施加「0 隐藏 / 超过 99 显示 `99+`」口径。**同时补上既有侧栏角标缺失的 `99+` 省略**——规范早已要求该口径，现有实现直接把原数写进 `textContent`，属于规范与实现漂移，本 change 一并拉平，避免两处角标出现 `120` 与 `99+` 的分歧。

- **替代方案**：给铃铛单独再请求一次 summary —— 会产生两个可能互不同步的计数来源，且在一个页面里对同一接口发两次请求，被否决。

### 3. 入口可见性由「服务端事实」决定，失败时不隐藏

- **权威事实**才隐藏入口：`summary.enabled === false` / `summary.bound === false`（能力关闭 / 无有效主体），以及 401、403 与能力关闭的 404（`todo_disabled`）——即服务端对**这个身份**给出的判定。这与 console.js 对 `.sidebar-item[data-view="todo"]` 的 `capability_disabled` / `menu_denied` 隐藏口径一致。
- **读取故障**不隐藏入口：503、非 JSON 响应、网络中断等只清空角标、保留入口原有可见性，避免一次瞬时故障把唯一的跨视图入口永久抹掉。判定收敛在 `summaryDenied(err)` 一个函数里，`refreshSummary()` 失败时先清角标、再按该判定决定是否隐藏入口，并返回 `null`（返回值的三个既有调用点都不使用它，改动不影响现有行为）。
- 计数为 0 时**保留入口、隐藏角标**：铃铛是入口而非提醒，0 件待办不该让入口消失。

### 4. 刷新点：页面加载 + 窗口聚焦，另加既有三处

在 `todos.js` 初始化时读一次 summary，并注册 `window` 的 `focus` 监听重复读取；进入待办页面、显式刷新、成功写入三处沿用既有调用。这样用户在对话等视图也能看到数量，同时满足「不新增周期轮询、SSE 或实时推送」。

- **替代方案**：进入任意视图时都刷一次 —— 会把 summary 的读取放大成视图切换的函数，收益不足且让「只在明确刷新点更新」的口径变模糊，被否决。

### 5. 点击语义就是 `navigateTo('todo')`

不新开浮层、不跳过门禁：交给既有 `navigateTo` 处理可见性、离开确认与跨区域切换，行为与选择侧栏「我的待办」完全一致。

### 6. 文案复用 `menu_todo`，可访问名称与 tooltip 同一来源

tooltip 用 `data-tip-key="menu_todo"`（portal 是事件委托，动态插入的元素同样生效），同时在插入时显式写入 `data-tooltip` / `aria-label` / `title`，因为 `applyI18n()` 在 `console.js` 顶层执行早于 `todos.js`，不显式写会有一段没有 tooltip 的空窗。语言切换由 `applyI18n()` 扫 `[data-tip-key]` / `[data-i18n-aria-label]` 自动更新，无需新增词条。

## Risks / Trade-offs

- [动态装载晚于顶栏首帧，可能出现入口「后出现」] → 铃铛不参与首屏布局，且插入位置固定；不做骨架占位，避免为一个图标增加布局抖动。
- [隐藏入口依赖一次异步请求，网络慢时会先显示后用掉] → 只有服务端给出针对该身份的判定（`enabled:false` / `bound:false`、401/403、能力关闭的 404）才隐藏；其余失败路径只清角标、不动入口，减少误隐藏。
- [同时修 99+ 省略会改变侧栏既有观感] → 规范本就要求该口径，变更方向是把实现拉回规范；`tests/test_todo_frontend.cjs` 新增断言钉住两处一致。
- [测试环境用 `vm` 跑 `todos.js`，DOM 是极简桩] → 测试里把铃铛节点放入桩的 `getElementById` 表并断言角标文本与可见性；`querySelector` 桩需能返回侧栏角标节点，保持既有用例不回归。
- [在 `todos.js` 里新增全局 `focus` 监听] → 只做一次 summary 读取，不触发列表加载，避免离开待办页后仍被拉取列表。

## Migration Plan

纯前端增量，无数据迁移、无配置变更、无路由变更。回滚方式为移除 `todos.js` 中的铃铛装载与刷新调用，不影响任何服务端契约与既有入口；无 feature flag 需求。

## Open Questions

无。位置、计数口径、点击语义、刷新点与隐藏条件均已由本设计与 delta spec 确定，实施参数（锚点回退顺序、`99+` 阈值）不构成未决项。
