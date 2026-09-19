## 1. 移除侧栏待办入口

- [x] 1.1 删除 `channel/web/chat.html` 中 `data-view="todo"` 的 `.sidebar-item` 整块（含图标、`menu_todo` 标签文本与 `.nav-badge` 节点），不动相邻的 `chat` / `agent-workbench` / `tasks` / `knowledge` / `scenes` 条目与其顺序
- [x] 1.2 确认 `console.js` 无需改动：`VIEW_META.todo`、`_consolePageForView`、回退页候选列表 `['chat', 'history', 'agent-workbench', 'todo', ...]` 与 `_viewNavDenied('todo')` 全部保留，侧栏遍历逻辑自然不再遇到该条目
- [x] 1.3 确认后端签页与授权不动：`auth/service.py`、`auth/policy.py` 中的 `workbench.todos` 与 `todo.read` 保持原样，`GET /api/todos/summary` 契约不变
- [x] 1.4 全仓检索 `data-view="todo"` 与 `.sidebar-item[data-view="todo"]`，确认除本 change 已处理的 `chat.html`、`todos.js`、测试外无其他引用
- [x] 1.5 删除 `channel/web/static/css/appearance.css` 中已无消费者的 `.nav-badge` 规则（基础胶囊、`.hidden`、`.bg-red-500`、`#app.sidebar-collapsed` 四条）与对应注释；依据是 1.4 同口径的全仓检索（含打包产物）无任何元素带该 class

## 2. 角标绘制收敛为铃铛一处

- [x] 2.1 `channel/web/static/js/todos.js` 的 `applySummaryBadge()` 删除侧栏那次 `paintBadge(document.querySelector('.sidebar-item[data-view="todo"] .nav-badge'), ...)` 调用，只保留 `#todo-bell-badge` 的绘制
- [x] 2.2 删除 `paintBadge()` 中仅侧栏调用可达的 `bg-red-500` 回退分支，保留 `overdueClass` 分支与 `!badge` 守卫；确认调用点仅剩铃铛一处
- [x] 2.3 更新失真注释：「一次 summary 喂两处入口」「small count shown next to the sidebar todo item」「mirrors how console.js hides the sidebar item」「Same destination as the sidebar 我的待办 item」等改为只描述顶栏铃铛这一唯一入口
- [x] 2.4 确认 `mountTodoBell()`、`setBellOffered()`、`summaryDenied()`、`refreshSummary()` 的刷新点与失败语义**未变**（页面加载 + 窗口聚焦，不引入轮询/SSE/推送）
- [x] 2.5 保留 `menu_todo` 文案键与 `registerConsoleView({ id: 'todo', label: 'menu_todo', load: loadTodosView })`，i18n 三语快照与文案键不新增、不删除

## 3. 测试

- [x] 3.1 更新 `tests/test_todo_frontend.cjs` 中依赖侧栏角标的用例（现「bell count mirrors the sidebar badge with a 99+ overflow」）：断言收敛为只校验 `#todo-bell-badge` 的 0 隐藏 / 计数显示 / `99+` 省略，移除对 `.sidebar-item[data-view="todo"] .nav-badge` 的断言
- [x] 3.2 新增用例：直接读取 `channel/web/chat.html` 文本，断言不存在 `data-view="todo"` 与对应的待办 `.nav-badge`，与 DOM 桩用例互补（桩无法证明模板里没有该条目）
- [x] 3.3 保持既有断言不变并通过：能力不可用（`enabled:false` / `bound:false` / 401/403）时铃铛入口隐藏、读取故障时入口保留且角标清空、点击调用 `window.navigateTo('todo')`、`navigateTo` 未定义时不抛错
- [x] 3.4 确认视图注册与导航相关既有用例不受影响：`tests/test_console_view_registry.cjs`（`registerConsoleView({id:'todo'})`）、`tests/test_nav_area_frontend.cjs`（`workbench.todos` 投影）、`tests/test_console_menu_mapping.py`（`workbench.todos` 映射）
- [x] 3.5 新增用例：读取 `channel/web/static/css/appearance.css` 文本，断言 `.nav-badge` 规则数为 0，锁住 1.5 的删除（该断言在改动前为红：HEAD 版本含 4 条）

## 4. 验证与收尾

- [x] 4.1 运行 `node --test tests/test_todo_frontend.cjs` 全绿
- [x] 4.2 运行受影响的导航/视图用例（`tests/test_console_view_registry.cjs`、`tests/test_nav_area_frontend.cjs`、`tests/test_console_menu_mapping.py`）确认无回归
- [x] 4.3 `openspec validate remove-todo-sidebar-entry --strict` 通过
- [x] 4.4 浏览器验收（dev 实例，真实登录会话）：任意视图下侧栏不出现「我的待办」条目与角标；顶栏铃铛位置、tooltip、可访问名称不变；点击后面包屑切到「我的待办」且列表渲染
- [x] 4.5 浏览器验收续：直接访问 `#view-todo` 深链仍到达待办页面；`tasks` 等相邻侧栏条目与分组显示、顺序不变
- [x] 4.6 验收证据（命令输出、运行时读数、截图，含侧栏无待办条目的取证与铃铛单点角标读数）记录于 `evidence.md` 与 `evidence/`；在「未覆盖项」中声明未在真实浏览器覆盖的能力关闭路径
