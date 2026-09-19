## 1. 顶栏铃铛入口

- [x] 1.1 在 `channel/web/static/js/todos.js` 新增铃铛装载函数 `mountTodoBell()`：按 `#tenant-selector` → `#workspace-toggle-btn` → `.workbench-header` 的顺序取稳定锚点，幂等插入 `#todo-bell-btn`（重复调用不产生第二个入口）
- [x] 1.2 铃铛按钮复用顶栏既有外观与 tooltip 机制：`data-tip-key="menu_todo"`、`data-tooltip-pos="bottom"`，并在插入时显式写入 `data-tooltip` 与 `aria-label`（弥补 `applyI18n()` 早于本模块执行的空窗）；不设原生 `title`，避免与浮层 tooltip 重复
- [x] 1.3 铃铛角标节点 `#todo-bell-badge` 默认隐藏，带可读的计数文本容器；0 时只隐藏角标、保留入口
- [x] 1.4 点击铃铛调用既有 `navigateTo('todo')`（函数缺失时静默跳过），不新开浮层/抽屉/通知列表，不绕过 `navigateTo` 的可用性门禁与离开确认
- [x] 1.5 锚点缺失时不抛错、不插入游离 DOM，`todos.js` 在无顶栏页面（若被复用）保持可加载

## 2. 计数口径与刷新点

- [x] 2.1 抽取 `formatBadgeCount(count)` 统一「0 隐藏 / 超过 99 显示 `99+`」，供侧栏角标与铃铛角标共用
- [x] 2.2 重构 `applySummaryBadge(summary)`：同一次 summary 结果经 `paintBadge()` 同时渲染侧栏角标与铃铛角标，逾期红色样式同步（铃铛用自有 `todo-bell-badge-overdue` 类，不依赖工具类优先级）
- [x] 2.3 `refreshSummary()` 失败时返回 `null`（不再伪造 `enabled:false`），使入口可见性只由服务端权威事实决定；确认 3 个既有调用点均未使用返回值
- [x] 2.4 入口可见性：`summary.enabled === false || summary.bound === false` 时隐藏铃铛；读取失败、计数为 0 时保留入口、隐藏角标
- [x] 2.5 初始化时读取一次 summary，并注册 `window` 的 `focus` 监听重复读取；不新增周期轮询、SSE、推送，`focus` 不触发列表加载

## 3. 测试

- [x] 3.1 扩展 `tests/test_todo_frontend.cjs` 的 DOM 桩：支持 `document.createElement`、`insertBefore`/`appendChild`、选择器注册与 `getElementById` 的「尚未创建」语义，使铃铛装载可被断言
- [x] 3.2 新增用例：summary 返回 3 时两处角标都显示 `3`；返回 0 时角标隐藏且铃铛入口仍在
- [x] 3.3 新增用例：summary 返回 120 时两处角标都显示 `99+`
- [x] 3.4 新增用例：summary 返回 `enabled:false` / `bound:false` 时铃铛入口隐藏；读取失败（403）时入口保留、计数清空
- [x] 3.5 新增用例：点击铃铛调用 `window.navigateTo('todo')`；`navigateTo` 未定义时不抛错
- [x] 3.6 新增用例：`window` 聚焦重新读取 summary 且不触发列表请求；既有列表/失败模式/分页用例（8 个）全部保持通过

## 4. 验证与收尾

- [x] 4.1 运行 `node --test tests/test_todo_frontend.cjs` 全绿（13/13）
- [x] 4.2 运行后端相关用例（`tests/test_todo_web_database.py`、`test_todo_service.py`、`test_todo_private_state.py`、`test_todo_tool_identity.py`）确认接口契约未变（59 passed）
- [x] 4.3 `openspec validate add-todo-header-bell --strict` 通过
- [x] 4.4 浏览器验收（dev 实例，真实登录会话）：铃铛装载在标题区与 `#tenant-selector` 之间、tooltip/可访问名称正确、点击后面包屑切到「我的待办」且列表渲染
- [x] 4.5 真实登录会话下的两处角标同屏复核：summary 返回 `open:4 / overdue:1` 时顶栏与侧栏角标同为 `4` 且都用逾期红色；角标几何与配色经像素采样确认
- [x] 4.6 验收证据（命令输出、运行时读数、截图）记录于 `evidence.md` 与 `evidence/`；`open:0` 与 `99+` 仅有 vm 用例覆盖，已在 evidence 的「未覆盖项」中声明
