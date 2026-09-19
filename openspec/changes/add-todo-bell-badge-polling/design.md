## Context

动机见 `proposal.md`；要落地的行为契约见本 change 的 `specs/todo-workbench/spec.md`（要求「本人角标按固定间隔与明确刷新点更新」与「顶栏铃铛入口一键进入个人待办」）。

当前接入点：

- 顶栏铃铛由 fork 自有模块 `channel/web/static/js/todos.js`（IIFE，`defer`，页面里最后加载）在 `initTodosView()` 里经 `mountTodoBell()` 挂载到 `.workbench-header` 的 `#tenant-selector` 之前；角标节点是 `#todo-bell-badge`。
- 计数唯一来源是 `GET /api/todos/summary`（`channel/web/todo_handlers.py::TodoSummaryHandler` → `agent/todo/service.py::summary`，返回 `enabled` / `bound` / `open` / `overdue`），由 `refreshSummary()` 读取并交给 `applySummaryBadge()` 渲染（`todos.js:157-190`）。
- 现有刷新点：初始化（`todos.js:860`）、`window` 的 `focus`（`:862`）、`loadTodosView()` 成功后（`:360`）、待办状态写入成功后（`:724`）。没有任何计时器。
- `loadTodosView()` 已有模块内世代守卫 `_generation`，但 `refreshSummary()` 没有：晚到的 summary 响应会直接落盘。
- 登录门 `requestAuthGatedStart()` 在全局作用域（`core/auth.js`，随 console 在 `todos.js` 之前求值）；`chat/scheduler-notify.js` 是既有范式：`setTimeout` 链 + 登录门。
- 顶栏租户切换是整页导航（`console.js::_setupHeaderTenantSelector` 里 `window.location.assign`），因此跨租户的陈旧响应在页面级被结构性切断；页面内的上下文切换只剩登录期租户选择与登出。

## Goals / Non-Goals

**Goals:**

- 铃铛角标在任意视图下最迟一个轮询间隔内自更新（其他标签、Agent 从会话创建的待办都能反映出来）。
- 稳态成本可控且可预测：可见标签页每 30 秒一次 `GET /api/todos/summary`，后台标签页零请求。
- 复用既有机制（登录门、世代守卫、既有 `refreshSummary`/`applySummaryBadge`），不新增接口、路由、配置项或第二个计数来源。
- 轮询失败与既有失败语义完全一致：非权威失败只清角标、不隐藏入口，并能在下一个间隔自愈。

**Non-Goals:**

- 不做 SSE / WebSocket / 长轮询 / 推送通道，不做通知中心、消息流、已读未读。
- 不做 ETag / `If-None-Match` 条件请求或 `summary` 契约改造（30 秒间隔下收益可忽略，见 Decisions 6）。
- 不做自适应退避轮询（见 Decisions 6）。
- 不改 `console.js` 既有 `/poll` 与 scheduler 轮询的任何行为，不改上游共享顶栏标记。

## Decisions

### 1. `setTimeout` 链而非 `setInterval`，间隔常量 30 秒

用「本轮结束后再排下一轮」的链式定时（与 `scheduler-notify.js` 一致），而不是 `setInterval`：请求慢或网络抖动时不会叠加出并发请求，且每次都能按最新状态决定是否继续。间隔取单一常量 `SUMMARY_POLL_MS = 30000`，规范只规定下限（不短于 30 秒），实现参数集中在一处。

- **替代方案**：`setInterval` —— 与请求耗时不相关，慢响应下会出现重叠与乱序落盘，被否决。

### 2. 只在标签页可见时轮询，恢复可见立即补一次

监听 `visibilitychange`：隐藏时清除计时器、不排下一轮；恢复可见时立刻 `refreshSummary()` 一次并按整间隔重新计时。

- **为什么和 `console.js` 的 `/poll` 相反**：`/poll` 在后台也轮询，因为它承载的是**消息投递**，隐藏标签正要靠它把消息交给系统通知。角标是**计数**，不是投递：回前台补一次即可，任何时刻最多滞后一个间隔；后台保持零流量。
- **替代方案**：隐藏时继续轮询 —— 后台标签的请求对用户不可见，收益是「回前台时角标已是 30 秒前的值」而不是「回前台即最新」，被否决。

### 3. 启动复用 `requestAuthGatedStart()`，401 时停表

轮询不自行判断登录态，而是像 `scheduler-notify.js` 一样经 `requestAuthGatedStart()` 在鉴权门打开后再启动，避免无 cookie 的请求刷服务端日志。鉴权失效（summary 返回 401）时停止轮询并保持既有「清角标、保留入口」的处理，避免在登录浮层背后继续发请求。

### 4. 每次请求带世代，晚到的响应不落盘

`refreshSummary()` 增加一个模块内单调世代 `_summaryGen`：请求前取号，响应回来时号不一致就丢弃（不发不写）。这补上现有缺口——例如窗口聚焦触发的请求与一次轮询响应乱序返回时，旧值不会覆盖新值。登出或整页切租户会重建整个模块状态，页面级切换不依赖该守卫。

- **替代方案**：读取 `identity-admin.js` 的 `bumpTenantGeneration()` 代次 —— 它只暴露 bump，没有读取入口；为一个已由整页导航覆盖的场景去扩展它的对外契约，收益不足，被否决。

### 5. 明确刷新点成功后重置轮询计时

`loadTodosView()` 成功、`operateTodo()` 成功、窗口聚焦、进入待办页这些既有刷新点，在成功拿到 summary 后把计时器清零重排。这样「刚手动刷过」不会紧接着再来一次轮询；重置逻辑只放在 `refreshSummary()` 的成功路径里，调用点不感知。

### 6. 明确不做条件请求与自适应退避

- **条件请求**：`summary` 的响应体只有 `enabled/bound/open/overdue` 几十字节，304 省下的传输量在 30 秒间隔下无感，却要动 `TodoSummaryHandler` 与后端测试面。属于 YAGNI，留作后续独立 change。
- **自适应退避**：有变化时快、长期无变化时退到数分钟，会让「我在看对话、Agent 刚建了一件待办」这个最该及时的场景变慢，且引入更多待测参数。先固定间隔，需要时再加。

### 7. 测试桩先补齐假计时器与可见性

`tests/test_todo_frontend.cjs` 在 `vm.runInNewContext` 的沙箱里没有 `setTimeout` / `clearTimeout`，也没有 `visibilitychange` 的桩（现有 `ctx.addEventListener` 只收集 `focus`）。因此实施顺序是：先给沙箱注入可控的假计时器（手动 `tick()`）与可见性状态，再写断言，最后改实现——否则 `initTodosView()` 一排计时器就直接抛 `setTimeout is not defined`。

## Risks / Trade-offs

- [后台标签的角标滞后] → 隐藏期间不轮询，回前台立即补一次；规范只承诺「有界自更新」，不承诺实时。
- [轮询放大 401 噪声] → 经登录门启动，且 401 时停表；这是 `scheduler-notify.js` 已经验证过的组合。
- [多标签线性放大请求] → 每标签每 30 秒一次，量级为「可见标签数 × 2 次/分钟」，远低于既有 scheduler 轮询（10 秒）。不做跨标签去重，避免引入第二个共享状态。
- [瞬时失败清空角标的观感] → 这是既有规范语义；轮询把恢复时间从「下次手动触发」缩短到一个间隔，方向是改善而不是新增问题。
- [测试环境缺计时器导致既有用例报错] → 先补桩再实现（Decision 7），并要求既有「聚焦刷新不拉列表」「失败不清入口」等断言不回归。
- [入口隐藏依赖一次异步判定] → 沿用现状：只有 `enabled:false` / `bound:false` / 401 / 403 才隐藏，其余失败不动入口可见性。

## Migration Plan

纯前端增量：无数据迁移、无配置变更、无路由表变更、无 feature flag。

- 发布即生效；首帧行为不变（铃铛仍先出现、角标随后填充）。
- 回滚：移除计时器与 `visibilitychange` 监听，角标回到「页面加载 + 窗口聚焦 + 进入待办页 + 成功写入」四个刷新点；服务端契约与既有入口不受影响。

## Open Questions

无。轮询间隔（30 秒）、隐藏行为（暂停）、启动门（登录门）、失败语义（沿用现状）均已确定，且不构成未决实施参数。
