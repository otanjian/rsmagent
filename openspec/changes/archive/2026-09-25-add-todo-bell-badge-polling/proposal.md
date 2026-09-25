## Why

顶栏铃铛（`todo-workbench`）的角标只在四个明确刷新点更新：页面加载、窗口重新获得焦点、进入待办页/显式刷新、写入成功后。用户长时间停留在对话视图时，**由 Agent 从会话创建、或另一个标签页处理掉的待办都不会反映到角标上**——角标会一直停在打开页面那一刻的数字，直到用户手动切窗或切页。铃铛是待办在界面上的唯一入口，一个长期不更新的计数会直接误导「我还有几件要跟进」。

`add-todo-header-bell` 当初刻意排除周期轮询（Non-Goals 明列、spec 以 MUST NOT 钉住）。当前需求改为「角标要有界地自更新」，因此这是一次**规范层面的口径变更**：把「只在明确刷新点更新」改成「固定间隔轮询 + 明确刷新点」，并继续保持不做 SSE、推送与通知中心。

## What Changes

- 顶栏铃铛角标 SHALL 在入口可见期间按固定间隔轮询既有 `GET /api/todos/summary`，间隔默认 30 秒、不得短于该值；MUST NOT 新增第二个计数来源、接口、路由或配置项。
- 轮询 SHALL 只在标签页可见（`document.visibilityState === 'visible'`）时进行；标签页隐藏时 SHALL 暂停，恢复可见时 SHALL 立即刷新一次并按整间隔重新计时。
- 轮询 SHALL 复用既有登录门（`requestAuthGatedStart`）：未登录/未通过鉴权前 MUST NOT 发出请求。
- 轮询 SHALL 使用不重叠的定时链（`setTimeout` 链，而非 `setInterval`），并保证同一时刻最多一个在途 summary 请求。
- 既有明确刷新点（页面加载、窗口聚焦、进入待办页、显式刷新、写入成功）SHALL 保留，并在刷新成功后**重置轮询计时**，避免紧接着产生一次多余请求。
- 刷新结果 SHALL 沿用既有角标口径与失败语义：0 隐藏、超过 99 显示 `99+`、单一口径；能力关闭/无有效主体/401/403 隐藏入口；503、网络中断等非权威失败只清空角标并保留下一次恢复机会。
- 晚到的 summary 响应（登出、账号或租户切换之后返回）SHALL 被丢弃，MUST NOT 落到当前上下文的角标上。
- MUST NOT 引入 SSE、WebSocket、长轮询或任何推送通道；MUST NOT 打开通知中心、消息流或已读未读语义。
- 影响范围为 fork 自有前端模块 `channel/web/static/js/todos.js`，不修改上游共享顶栏标记，不改动 `summary` 接口契约与授权策略。

## Capabilities

### New Capabilities
<!-- 无新增 capability：定时刷新与角标口径同属 todo-workbench 责任域 -->

### Modified Capabilities
- `todo-workbench`: 「本人角标只在明确刷新点更新」改为「本人角标按固定间隔与明确刷新点更新」——新增 30 秒下限的 summary 轮询、可见性暂停/恢复即刷、登录门与单在途请求约束，并把「其他标签或 Agent 新建的事项在下一次规定刷新点加载，不启动周期轮询」改写为「在下一个轮询周期或刷新点出现」；同 capability 的「顶栏铃铛入口一键进入个人待办」中「不引入通知中心与推送」的 Scenario 保留「不打开通知列表/浮层、不新增接口」，把「不启动周期轮询」收敛为「只按规定间隔轮询既有 summary」，SSE 与推送通道的 MUST NOT 不变。

## Impact

- **行为受影响**：任意视图下顶栏铃铛角标的时效性从「用户主动触发才更新」变为最迟一个轮询间隔内自更新；可见标签页每 30 秒产生一次极轻的 `GET /api/todos/summary`。
- **不受影响**：`GET /api/todos/summary` 的请求/响应契约与授权策略、待办列表/详情/表单行为、侧栏与路由、`navigateTo` 的可用性门禁与离开确认语义、`console.js` 既有 `/poll` 与 scheduler 轮询。
- **代码面**：`channel/web/static/js/todos.js`（轮询计时器、可见性监听、世代守卫、刷新点重置）；`channel/web/chat.html` 与上游共享顶栏标记 MUST NOT 改动。
- **测试面**：`tests/test_todo_frontend.cjs` 需注入假计时器与可见性桩，钉住「到点只读 summary 不读列表 / 隐藏暂停 / 恢复可见立即刷新 / 刷新后计时重置 / 登录门未开不发请求 / 晚到响应不落盘」。
- **规范面**：`openspec/specs/todo-workbench/spec.md` 两条 requirement 被 MODIFIED；`add-todo-header-bell` 等归档 change 保持原样，不作为现行基线。
- **不新增**：无新接口、新路由、新配置项、新能力开关、无 SSE/推送通道。
