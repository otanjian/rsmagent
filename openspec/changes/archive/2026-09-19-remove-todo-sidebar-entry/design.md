## Context

动机见 `proposal.md` —— Why。此处只记录影响方案的当前代码事实。

待办在界面上有两处入口，都指向同一视图、同一份 summary：

| 关注点 | 当前位置 |
|---|---|
| 侧栏条目标记 | `channel/web/chat.html` 的 `<a class="sidebar-item" data-view="todo">`，内含 `.nav-badge` |
| 顶栏铃铛 | `channel/web/static/js/todos.js` 的 `mountTodoBell()`，按钮 `#todo-bell-btn`、角标 `#todo-bell-badge` |
| 角标绘制 | `todos.js` 的 `applySummaryBadge()` 一次 summary 经 `paintBadge()` 渲染**两处** |
| 视图地址 | `console.js` 的 `VIEW_META.todo = { group: 'nav_workbench', page: 'menu_todo', console: 'workbench.todos' }` |
| 视图注册 | `todos.js` 的 `registerConsoleView({ id: 'todo', label: 'menu_todo', load: loadTodosView })` |
| 深链地址 | `#view-todo`（`#view-<viewId>` 形式，`console.js` 的 `_bootAreaDefaultView()` 只在**整页加载**时读取；`#todo` 不带 `view-` 前缀会被判为未知视图并回退到区域默认页） |
| 可用性门禁 | `console.js` 的 `_viewNavDenied('todo')` 读后端签发的 `workbench.todos` 投影 |
| 后端签页 | `auth/service.py` 与 `auth/policy.py` 中的 `workbench.todos`（`todo.read` / `scope: self`） |
| 回退页候选 | `console.js` 中 `['chat', 'history', 'agent-workbench', 'todo', ...]` |

约束：侧栏条目的显隐当前**已经**由后端投影驱动（`console.js` 遍历 `#sidebar-nav .sidebar-item[data-view]`，按 `menu_denied` / `capability_disabled` 加 `hidden`）。本 change 要表达的不是「这个身份没有权限」，而是「这个入口不再存在」。

## Goals / Non-Goals

**Goals:**

- 让待办入口唯一为顶栏铃铛，且不改变铃铛的计数来源、点击语义、刷新点与可用性门禁。
- 让侧栏条目的消失是**结构性**的（DOM 中不存在），而不是靠样式或运行时开关遮蔽。
- 保住待办页面自身的全部可达路径：`#view-todo` 深链、`navigateTo('todo')`、面包屑、页面内容。
- 用测试钉住「侧栏不存在待办条目」，防止后续模板改动把它带回来。

**Non-Goals:**

- 不动后端签页、`todo.read` 授权、summary 接口契约。
- 不动 `tasks`（定时任务）入口或其他任何侧栏条目、分组与排序。
- 不为「是否显示侧栏待办」新增配置项或能力开关（见决策 D3）。
- 不改待办页面标题、副标题、列表、表单与详情抽屉。

## Decisions

### D1. 删除 `chat.html` 中的条目标记，而不是用 CSS 或运行时开关遮蔽

**选择**：从 `chat.html` 直接删除 `<a data-view="todo">` 整块（含 `.nav-badge`）。

**理由**：侧栏条目是 fork 已在维护的模板；「入口不存在」最诚实的表达是 DOM 中不存在。否则会留下三个未决问题：`.nav-badge` 仍被 `todos.js` 查询、辅助技术仍可能读到隐藏节点、`console.js` 中按 `.hidden` 跳过条目的逻辑（如 `_renderAdminHomeShortcuts`）会让「隐藏」与「不存在」产生语义歧义。

**备选与否决**：加 `hidden` 类或 `display:none` —— 仍然占用 DOM 与可访问性树，且需要额外的运行时约定来防止其它代码把它重新显示出来。

**配套**：`appearance.css` 中 `html[data-web-palette] #sidebar .nav-badge` 及其 `.hidden` / `.bg-red-500` / `#app.sidebar-collapsed` 四条规则随条目一并删除。全仓检索（含打包产物，排除 `node_modules`）确认已无任何元素带 `nav-badge`；留着只会留下一段解释「侧栏行计数胶囊」的注释，而侧栏已没有这样的行。

### D2. 保留 `VIEW_META.todo`、`registerConsoleView`、`navigateTo('todo')`、`#view-todo` 深链与 `workbench.todos` 签页

**选择**：这些一概不动，只删除侧栏条目标记。

**理由**：它们服务的是**页面**而不是**侧栏入口**。铃铛的点击就是 `navigateTo('todo')`；hash 引导需要 `VIEW_META` 能解析 `todo`；`_viewNavDenied('todo')` 是铃铛的门禁来源；`registerConsoleView` 的 `label` 提供面包屑。删掉任一都会同时打断铃铛与深链。

**备选与否决**：把 `todo` 从 `VIEW_META` 摘除以「彻底移除视图」—— 会打断铃铛导航、深链和可用性门禁，与「页面仍可达」的目标直接冲突。

### D3. 不借后端投影（`menu_denied` / `capability_disabled`）表达「侧栏无此入口」

**选择**：后端继续签发 `workbench.todos`，不新增投影标志。

**理由**：`workbench.todos` 是**权限与可用性**语义，被 `_viewNavDenied` 用来决定铃铛点击是否放行。移除侧栏入口是**产品结构**语义。把两者混用会立刻产生错误：一旦让后端停止签发，`_viewNavDenied('todo')` 会判定页面不可用，铃铛与深链同时失效。

**备选与否决**：新增 `hidden_in_sidebar: true` 之类的投影字段 —— 为一次性 UI 收敛扩大权限契约面，且该字段只有单一消费者；YAGNI。

### D4. 角标绘制收敛为铃铛一处，删掉随之失效的侧栏分支

**选择**：`todos.js` 的 `applySummaryBadge()` 删除侧栏那次 `paintBadge(...)` 调用，只保留 `#todo-bell-badge` 的绘制；同步删除 `paintBadge()` 中仅在侧栏调用下才会走到的 `bg-red-500` 回退分支（铃铛始终传 `todo-bell-badge-overdue`）。

**理由**：目标节点消失后 `paintBadge(null, ...)` 会静默 early-return，保留调用不会报错，但它是死代码，且会让「一次 summary 喂两处」的注释失真。删掉无调用方的分支可避免读者误以为还有第二个绘制点。

**保留**：`formatBadgeCount()`、`paintBadge()` 本身与 `!badge` 守卫（`#todo-bell-badge` 在 `mountTodoBell()` 装载前可能不存在）、`setBellOffered()` 与 `summaryDenied()` 的全部逻辑 —— 这些是铃铛行为，本 change 不改。

### D5. 不新增文案键，`menu_todo` 保留

**选择**：`menu_todo`（「我的待办」）继续作为铃铛的可访问名称与 tooltip 来源，也继续作为视图注册的 `label`（面包屑）。

**理由**：移除的是侧栏的**那个节点**，不是这个词条。i18n 三语快照与 `menu_todo` 键不动。

## Risks / Trade-offs

- **既有操作习惯与录屏/文档失效** → 属有意的破坏性 UI 变更：在验收记录中显式声明，并在 tasks 中要求人工复核侧栏不再有待办、铃铛行为不变。
- **遗漏的侧栏引用**（例如别处仍按 `.sidebar-item[data-view="todo"]` 取节点）→ 删除前先全仓检索该选择器与 `data-view="todo"`；已知引用只有 `todos.js` 的角标绘制与 `chat.html` 的标记，二者在本 change 内一并处理。
- **测试桩与真实 DOM 的差异**：`tests/test_todo_frontend.cjs` 用 DOM 桩而非真实页面 → 桩无法证明「`chat.html` 里没有该条目」。缓解：新增一条直接读取 `chat.html` 文本、断言不存在 `data-view="todo"` 的防回归用例，与桩用例互补。
- **`paintBadge` 回退分支被误当作公共能力**：删除 `bg-red-500` 分支后若有隐藏调用方会静默失效 → 删除前确认调用点仅剩铃铛一处（当前 grep 已确认）。
- **角标「两处一致」的既有验收证据**（归档 change 的 `evidence/`）在移除后会与现状不符 → 归档记录保持原样（规范要求不改归档），新 change 的验收证据重新采集铃铛单点读数。

## Migration Plan

无数据或接口迁移。发布即生效：

1. 删除侧栏条目标记与侧栏角标绘制，同步更新注释与测试。
2. 人工复核：任意视图下侧栏无「我的待办」条目；顶栏铃铛计数、tooltip、点击跳转、能力关闭时隐藏均与变更前一致；`#view-todo` 深链仍到达待办页面。
3. 回滚：本 change 只涉及模板与一个前端模块的渲染分支，`git revert` 即可恢复侧栏条目，无残留状态。
