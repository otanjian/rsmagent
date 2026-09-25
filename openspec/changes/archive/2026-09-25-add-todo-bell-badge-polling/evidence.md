# add-todo-bell-badge-polling 验收证据

运行环境：本机 dev 实例 `http://localhost:9899`（`channel/web` 静态资源由运行中的进程直接读取磁盘，改后无需重启即可生效）。

**本次验收的浏览器 profile 没有会话**（自动化标签页停在登录浮层，`window.requestAuthGatedStart` 实测为 `undefined`）。这既是必须记录的局限（见「未覆盖项」），也是 Decision 3 停表条件的实测来源：未登录时 `GET /api/todos/summary` 的回答是 **400 `missing_tenant`**，不是 401。

## 1. 前端用例

```
$ node --test tests/test_todo_frontend.cjs
✔ bell mounts before the tenant selector and is idempotent
✔ bell count shows the capped summary total and hides at zero
✔ bell entry hides on an authoritative denial, survives a transient failure
✔ clicking the bell navigates to the todo view
✔ window focus refreshes the summary without loading the list
✔ the summary drives the bell badge only, never a sidebar badge
✔ the bell count refreshes one interval at a time without pulling the list
✔ polling pauses while the tab is hidden and refreshes when it returns
✔ polling stays parked until the login gate opens
✔ an explicit refresh restarts the interval instead of stacking a poll
✔ a slow summary read never stacks a second request
✔ a superseded summary response never paints over a newer one
✔ a lost session stops the cadence instead of polling with no cookie
✔ a restored session resumes the cadence at the next refresh point
✔ a tab with no tenant context parks the cadence instead of reading every interval
✔ user activity revives a parked cadence, at most once per interval
✔ a visibilitychange that is not a resume does not read
ℹ tests 27 / pass 27 / fail 0
```

新增的 8 个轮询用例在实现前全部失败（RED 基线：`tests 24 / pass 20 / fail 4` 一类的现场读数），实现后全部通过，既有 19 个用例无回归。

## 2. 后端待办用例（契约与授权未变）

本 change 不含任何 Python 改动。验收期间工作树里并存另一个进行中的 change（`add-todo-delegation`，改 `auth/service.py` 等），该未提交编辑一度使 `auth/service.py` 无法编译，故就地运行无法收集用例；因此在 HEAD 的干净检出中取证：

```
$ git worktree add <tmp> HEAD && cd <tmp>
$ .venv/bin/python -m pytest tests/test_todo_web_database.py tests/test_todo_service.py tests/test_todo_private_state.py tests/test_todo_tool_identity.py -q
59 passed in 12.48s
```

- 更早一次就地运行（该并行编辑落盘之前）同为 `59 passed in 6.03s`，两次读数一致。

## 3. 未引入第二套推送/计时机制

```
$ grep -n "setInterval|EventSource|WebSocket|longPoll" channel/web/static/js/todos.js
none (setTimeout chain only)
```

## 4. 浏览器运行时读数

### 4.1 未登录标签页停表（无 cookie 时不按间隔请求）

页面重载后静置 82 秒，无任何交互，`performance.getEntriesByType('resource')`：

```json
{"ageMs": 81948, "reads": [{"u": "todos/summary?agent_id=default", "at": 308}]}
```

- 82 秒内只有页面加载那一次读取（本次改动之前就有的行为），零列表请求。停表生效。
- 对照：停表条件补上 `missing_tenant` **之前**，同一页面每 30 秒发一次被拒请求。

### 4.2 停表条件的实测来源（修复前诊断读数）

`window.fetch` 钩子记录调用栈与响应体（`?agent_id=default` 是 `console.js` 既有 fetch 包装追加的，非本模块新增参数）：

```json
[
 {"at": 6479,  "status": 400, "body": "{\"status\":\"error\",\"message\":\"tenant selection required\",\"code\":\"missing_tenant\"}", "frames": "window.fetch < apiFetch < refreshSummary < pollSummary"},
 {"at": 12495, "status": 400, "body": "{\"status\":\"error\",\"message\":\"tenant selection required\",\"code\":\"missing_tenant\"}", "frames": "window.fetch < apiFetch < refreshSummary < HTMLDocument.onSummaryVisibility"}
]
```

- 未登录时服务端以 400 `missing_tenant` 拒绝，因此只按 401 停表不会生效。
- 可见性事件此前会在标签页本就可见时也触发读取（第 2 条调用栈），这正是 Decision 3a 只认「隐藏 → 可见」跳变的依据。

### 4.3 活动试探与节流

连发 4 个 `keydown`/`pointerdown`（模拟输入密码），随后再等一个间隔多活动一次：

```json
{"reads": [308, 87599, 138129], "list": 0}
```

- 4 个事件只产生 1 次请求（节流），间隔过后的一次活动产生第 3 次；全程零列表请求。

### 4.4 试探成功后的整间隔节奏与角标重绘

活动试探命中一个 200 summary 响应（该 profile 无会话，故以「被满足的响应」替代真实后端；`open: 3`）：

```json
{"reads": [{"wall": 78469, "perf": 226312}, {"wall": 108658, "perf": 256501}, {"wall": 139656, "perf": 287499}],
 "deltas": [30189, 30998],
 "badge": "3", "badgeHidden": false}
```

- 活动试探立即读取（第 1 次），成功后回到整间隔：30.19 秒、31.00 秒，均不短于 30 秒下限。
- 角标按 summary 的 `open` 重绘为 `3`，入口未被隐藏。
- 更早一次同构实测（未改动的节奏路径）读数为 30004 / 30002 / 30002 毫秒，零列表请求。

## 5. 规范校验

```
$ openspec validate add-todo-bell-badge-polling --strict
Change 'add-todo-bell-badge-polling' is valid
```

## 回滚方式（无 feature flag、无数据迁移）

纯前端增量：删除 `todos.js` 中的计时器、`visibilitychange` 监听与活动试探监听（`pointerdown`/`keydown`），角标即回到「页面加载 + 窗口聚焦 + 进入待办页 + 成功写入」四个明确刷新点。服务端契约、路由表与既有入口不受影响，无需回滚步骤。

## 未覆盖项

- **真实登录会话下对真实后端的 30 秒节奏未实测**。自动化 profile 无会话，且 `document.visibilityState` 在该构建里是非可配置的自有访问器、无法伪造登录态；节奏与角标重绘因此在真实浏览器中以「被满足的 200 summary 响应」观察（4.4），真实后端的响应契约由第 2 节 59 个用例保证。
- **隐藏标签页期间零请求未在真实浏览器中复现**。自动化环境的标签页切换不改变 Chromium 的可见性模型（`browser_tabs` 新建/切换后实测 `visibilityState` 仍为 `visible`），`Page.setWebLifecycleState('frozen')` 亦未使其变为 `hidden` 且令 `performance.now()` 跳变约 65 分钟（该次读数已作废、未采用）。该分支只在 vm 用例中受控验证（第 1 节 `polling pauses while the tab is hidden and refreshes when it returns`、`a visibilitychange that is not a resume does not read`）。
- 计数 `0`（隐藏角标、保留入口）与 `120`（`99+`）、能力不可用隐藏入口、非权威失败不清入口等既有口径沿用归档 change `2026-09-19-add-todo-header-bell` 的验收结论，本次未在真实数据上重跑。
- 「Agent 新建一条待办 → 角标最迟一个间隔内自增」的端到端未在真实会话中实测；对应的自更新路径由 vm 用例的间隔读取断言覆盖。
- 无截图：本次改动不改变铃铛与角标的任何外观（装载位置、尺寸、配色、tooltip 均未触碰），可核对的视觉结论见归档 change 的 `evidence/`；本 change 的证据是计时与网络读数。
