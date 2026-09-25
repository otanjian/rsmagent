# Tasks: 顶栏铃铛角标定时刷新（30 秒轮询）

## 1. 测试桩与失败测试（先行）

- [x] 1.1 扩展 `tests/test_todo_frontend.cjs` 的 vm 沙箱：注入可控假计时器（`setTimeout`/`clearTimeout`，暴露 `advance(ms)` 以便推进时间而不真实等待）
- [x] 1.2 沙箱补齐可见性桩：`document.visibilityState`（可写）+ `document.addEventListener('visibilitychange')` 与 `window.addEventListener('focus')` 的监听收集，使隐藏/恢复/聚焦可被驱动
- [x] 1.3 新增用例：假计时器推进一个间隔后只发出一次 `GET /api/todos/summary`，且不发任何 `/api/todos?` 列表请求
- [x] 1.4 新增用例：切到后台（hidden）后推进多个间隔不发出请求；切回可见时立即刷新一次并按整间隔重新计时（下一次请求出现在整间隔之后，而不是紧接着）
- [x] 1.5 新增用例：登录门未打开时不发请求；summary 返回 401 后停止轮询，且既有「入口保留/隐藏」行为不变
- [x] 1.6 新增用例：窗口聚焦/写入成功等明确刷新点成功后轮询计时被重置，下一次轮询落在完整间隔之后；上一轮读取未结束时该轮轮询被跳过而不并行发起
- [x] 1.7 新增用例：前一次请求晚于后一次返回时，角标显示后一次结果（旧响应不覆盖新值）
- [x] 1.8 新增用例：会话失效（401）后轮询停表，重新建立会话后的刷新点读取成功即恢复整间隔节奏
- [x] 1.9 新增用例：无租户上下文（400 `missing_tenant`）与丢失会话（401）同属「无会话」，停表后不按间隔读取
- [x] 1.10 新增用例：标签页本就可见时到达的可见性事件不触发读取（只有隐藏→可见跳变才算恢复）
- [x] 1.11 新增用例：停表期间用户活动触发一次试探读取，成串活动（如输入密码）被节流为每间隔至多一次，试探成功后恢复整间隔并重绘角标
- [x] 1.12 沙箱补齐 `Date` 桩：`Date.now()` 跟随假时钟推进，使以墙钟计时的活动节流可被确定性地测试
- [x] 1.13 运行 `node --test tests/test_todo_frontend.cjs`，确认 1.3–1.11 在实现前失败而既有断言仍通过（RED 基线）

## 2. 轮询实现（`channel/web/static/js/todos.js`）

- [x] 2.1 新增间隔常量 `SUMMARY_POLL_MS = 30000` 与轮询状态（计时器句柄、在途标志、世代计数器、暂停标志、上一次可见性、活动试探时刻），集中在一处，不对外暴露
- [x] 2.2 实现轮询调度：`setTimeout` 链（本轮结束后再排下一轮）而非 `setInterval`，每轮受「可见性 + 在途」双条件约束，且只调用既有 `refreshSummary()`，不触发列表加载
- [x] 2.3 `refreshSummary()` 增加世代守卫：请求前取号，响应回来号不一致即丢弃且不写角标；补上窗口聚焦与轮询响应乱序时旧值覆盖新值的缺口
- [x] 2.4 `refreshSummary()` 成功路径重置轮询计时；权威失败时保持既有「清角标、按 `summaryDenied` 决定是否隐藏入口」语义
- [x] 2.5 注册 `visibilitychange` 监听：隐藏时清除计时器且不排下一轮；只有隐藏→可见跳变才立即刷新一次并按整间隔重新计时
- [x] 2.6 经既有 `requestAuthGatedStart()` 启动轮询（函数不可用时降级为直接启动，由停表兜底，见 `design.md` Decision 3）
- [x] 2.7 停表条件取实测语义：`summaryNoSession()` 覆盖丢失会话的 401 与无租户上下文的 400 `missing_tenant`；不把 403 / `todo_disabled` 混进来，它们按既有语义只清角标
- [x] 2.8 停表可自行解除：活动（`pointerdown`/`keydown`，捕获阶段）按 `SUMMARY_POLL_MS` 节流各触发一次试探读取，任一读取成功即恢复整间隔
- [x] 2.9 不新增接口、路由、配置项、能力开关或第二个计数来源；不修改上游共享顶栏标记与 `console.js`

## 3. 测试与回归

- [x] 3.1 `node --test tests/test_todo_frontend.cjs` 全绿（27/27），含 1.3–1.11 新增用例与原有用例（角标 0/`99+`、能力隐藏、失败不清入口、聚焦刷新不拉列表、列表/失败模式/分页）全部通过
- [x] 3.2 运行后端待办用例（`tests/test_todo_web_database.py`、`tests/test_todo_service.py`、`tests/test_todo_private_state.py`、`tests/test_todo_tool_identity.py`）确认 `summary` 契约与授权策略未变（59 passed）
- [x] 3.3 确认未引入 `setInterval`、SSE、WebSocket 或长轮询：以文本检索核对新增代码只使用 `setTimeout` 链

## 4. 规范与文档

- [x] 4.1 `openspec validate add-todo-bell-badge-polling --strict` 通过，且 RENAMED + MODIFIED 的 delta 与 `openspec/specs/todo-workbench/spec.md` 现状一致
- [x] 4.2 仓库内不存在「前端模块装载顺序/后台轮询清单」文档（无 `channel/web/README.md`，`tests/test_web_console_assets.py` 因上游仍服务单体 console 而跳过）：不补充清单，已在 `evidence.md` 声明
- [x] 4.3 `design.md` 记录现行构建下登录门对 fork 模块不可达的实测事实、400 `missing_tenant` 的停表依据、活动试探与可见性跳变三个决策及后续升级路径

## 5. 浏览器验收与收尾

- [x] 5.1 在 dev 实例观察 Network：可见标签页的 summary 读取按整间隔重复（实测 30004/30002 与 30189/30998 毫秒），全程无 `/api/todos?` 列表请求
- [x] 5.2 可见性事件与间隔的关系按设计生效：本就可见时的可见性事件不再读取（该分支的隐藏侧由 vm 用例受控验证，真实浏览器无法制造 hidden 状态，见 `evidence.md` 未覆盖项）
- [x] 5.3 未登录（无 cookie）会话：82 秒静置只有 1 次 summary 读取，不按间隔重复被拒请求；活动后按节流各试探 1 次
- [x] 5.4 复核失败与权限边界行为不变：401/403 时入口隐藏，非权威失败只清角标、保留入口并在下一次成功轮询后恢复
- [x] 5.5 无 feature flag 需求与无数据迁移：回滚方式（移除计时器、`visibilitychange` 与活动试探监听，角标回到四个明确刷新点）在 `evidence.md` 中记录
- [x] 5.6 验收证据（命令输出、运行时读数）记录于 `evidence.md` 与 `evidence/summary-cadence-readings.json`，并声明未覆盖项（真实登录会话下的后端节奏、隐藏标签页零请求）
