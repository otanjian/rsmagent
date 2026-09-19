## 1. 测试桩与失败测试（先行）

- [ ] 1.1 扩展 `tests/test_todo_frontend.cjs` 的 vm 沙箱：注入可控假计时器（`setTimeout`/`clearTimeout`，暴露手动 `tick(ms)` 以便推进时间而不真实等待）
- [ ] 1.2 沙箱补齐可见性桩：`document.visibilityState`（可写）+ `document.addEventListener('visibilitychange')` 与 `window.addEventListener('visibilitychange')` 的监听收集，使隐藏/恢复可被驱动
- [ ] 1.3 新增用例：假计时器推进一个间隔后只发出一次 `GET /api/todos/summary`，且不发任何 `/api/todos?` 列表请求
- [ ] 1.4 新增用例：切到后台（hidden）后推进多个间隔不发出请求；切回可见时立即刷新一次并按整间隔重新计时（下一次请求出现在整间隔之后，而不是紧接着）
- [ ] 1.5 新增用例：登录门未打开（无 `requestAuthGatedStart` 或未调用其回调）时不发请求；summary 返回 401 后停止轮询，且既有「入口保留」行为不变
- [ ] 1.6 新增用例：窗口聚焦/写入成功等明确刷新点成功后轮询计时被重置；同一时刻只存在一个在途 summary 请求（慢响应期间不并发）
- [ ] 1.7 新增用例：前一次请求晚于后一次返回时，角标显示后一次结果（旧响应不覆盖新值）
- [ ] 1.8 运行 `node --test tests/test_todo_frontend.cjs`，确认 1.3–1.7 在实现前失败而既有断言仍通过（RED 基线）

## 2. 轮询实现（`channel/web/static/js/todos.js`）

- [ ] 2.1 新增间隔常量 `SUMMARY_POLL_MS = 30000` 与轮询状态（计时器句柄、在途标志、世代计数器），集中在一处，不对外暴露
- [ ] 2.2 实现轮询调度：`setTimeout` 链（本轮结束后再排下一轮）而非 `setInterval`，每轮受「可见性 + 在途」双条件约束，且只调用既有 `refreshSummary()`，不触发列表加载
- [ ] 2.3 `refreshSummary()` 增加世代守卫：请求前取号，响应回来号不一致即丢弃且不写角标；补上窗口聚焦与轮询响应乱序时旧值覆盖新值的缺口
- [ ] 2.4 `refreshSummary()` 成功路径重置轮询计时；权威失败（401/403）时停止轮询并保持既有「清角标、按 `summaryDenied` 决定是否隐藏入口」语义
- [ ] 2.5 注册 `visibilitychange` 监听：隐藏时清除计时器且不排下一轮，恢复可见时立即刷新一次并按整间隔重新计时
- [ ] 2.6 经既有 `requestAuthGatedStart()` 启动轮询（函数不可用时降级为直接启动），避免无 cookie 请求；确认未登录路径不发请求
- [ ] 2.7 不新增接口、路由、配置项、能力开关或第二个计数来源；不修改上游共享顶栏标记与 `console.js`

## 3. 测试与回归

- [ ] 3.1 `node --test tests/test_todo_frontend.cjs` 全绿，含 1.3–1.7 新增用例与原有用例（角标 0/`99+`、能力隐藏、失败不清入口、聚焦刷新不拉列表、列表/失败模式/分页）全部通过
- [ ] 3.2 运行后端待办用例（`tests/test_todo_web_database.py`、`tests/test_todo_service.py`、`tests/test_todo_private_state.py`、`tests/test_todo_tool_identity.py`）确认 `summary` 契约与授权策略未变
- [ ] 3.3 确认未引入 `setInterval`、SSE、WebSocket 或长轮询：以文本检索核对新增代码只使用 `setTimeout` 链

## 4. 规范与文档

- [ ] 4.1 `openspec validate add-todo-bell-badge-polling --strict` 通过，且 RENAMED + MODIFIED 的 delta 与 `openspec/specs/todo-workbench/spec.md` 现状一致
- [ ] 4.2 若工作台前端说明文档（如前端模块装载顺序/后台轮询清单）列出既有轮询循环，同步补入待办角标轮询；不存在该类文档时在 evidence 中声明并跳过

## 5. 浏览器验收与收尾

- [ ] 5.1 在 dev 实例（真实登录会话）观察 Network：对话视图下 `GET /api/todos/summary` 约每 30 秒一次，无列表请求
- [ ] 5.2 切到后台标签后再切回：后台期间无 summary 请求，切回立即出现一次请求，随后回到约 30 秒节奏
- [ ] 5.3 由 Agent 从会话创建一条待办（或在另一标签处理掉一条）后，顶栏角标最迟一个间隔内自增/自减；点击铃铛仍进入待办页面
- [ ] 5.4 复核失败与权限边界行为不变：登出/无权限时入口隐藏，503/网络中断只清角标、保留入口并在下一次成功轮询后恢复
- [ ] 5.5 无 feature flag 需求与无数据迁移：回滚方式（移除计时器与 `visibilitychange` 监听，角标回到四个明确刷新点）在 `evidence.md` 中记录
- [ ] 5.6 验收证据（命令输出、运行时读数、截图）记录于 `evidence.md` 与 `evidence/`，并声明未覆盖项
