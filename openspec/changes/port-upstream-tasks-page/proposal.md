## Why

定时任务页现在的观感与 `master` 不一致，且成因不是样式走样而是**接线缺失**：fork 的 console 在 `e37df20c` 合并时保留了单体 `channel/web/chat.html`（无 `<!--#include-->`），因此上游已拆分好的定时任务页（`templates/views/tasks.html` + `templates/modals/task-edit.html` + `views/tasks.js` + `views/tasks-modal.js`）从未被引用；fork 用自建的 `functional-scheduler.js` 补了投递实例/接收者选择与运行记录，但该模块在全仓没有任何样式规则（`console.css` 里只有 `.rdai-context-*`，没有一条 `.rdai-scheduler-*`），于是 `label`/`input` 回到默认 `display:inline`，出现「任务名称任务名称」、下拉与 textarea 挤在一起的样子。

上游那 5 个文件在工作树里与 `origin/master` **逐字节相同**，却一行也没被加载；同时 fork 已为这一页验收过一组服务端能力（`auth/capability_matrix.py` 的 8 个 slice、`/api/scheduler/{instances,recipients,create,runs,runs/detail,runs/delete}`），其页面契约与上游前端**已经对齐**（create 请求体形状、运行记录 13 个字段、`initDropdown` 等全局函数签名全部核对通过）。也就是说上游页面可以直接用，缺口只在「接线 + 一层 fork 语义补丁」。

## What Changes

- **服务路径**：`channel/web/fork/handlers/pages.py::ChatHandler.GET` 改为经 `channel/web/core/template.py::render('chat.html')` 组装页面，使 `<!--#include-->` 生效；按文件 mtime 的 `?v=` 戳取代 fork 自己那套 `?v=<epoch>`。
- **页面接线**：`chat.html` 的 `#view-tasks` 与 `#task-edit-modal-overlay` 改为 include 上游片段，并新增上游 `run-detail.html`（运行详情弹窗，fork 现无此弹窗）；装载上游 `assets/js/views/tasks.js`、`assets/js/views/tasks-modal.js`。
- **fork 补丁层（新文件，最后装载）**：以 fork 命名空间并行承载上游逻辑中必须参与方法体中部的部分——逐动作能力门控、`task.capabilities` 驱动的按任务按钮、本人/公共归属标记、功能关闭时呈现原因而非空态或永久 Loading、编辑面把投递目标呈现为只读；并提供上游 `routeNoteTab` 的 fork 实现。
- **移除**：`assets/js/functional-scheduler.js`（584 行，全仓无样式）及其装载；`console.js` 中与上游重复的 13 个顶层符号（其中 `currentEditingTask`、`tasksLoaded` 是词法声明，**与上游同名会让第二个脚本整页 `SyntaxError`**）、`mountTasksModule`/`_schedulerRequest` 与 `#tasks-history` 挂载/teardown。
- **i18n / CSS**：上游该页用到的键补进 `static/js/i18n/tasks-records.js`（`static/js/core/i18n.js` 里虽有同名键但没有任何页面加载它）；`.cfg-dropdown-disabled` 补进 `console.css`（fork 页面唯一加载的样式表）。
- **上游来源登记**：在既有上游漂移守卫 `tests/test_upstream_drift_guards.py` 登记该页的 4 个上游来源与内容摘要，使上游改动可被检测并要求人工重新移植（`fork-upstream-decoupling` 对平行实现的要求）。

**BREAKING**：无。后端零改动——`/api/scheduler/*` 的请求与响应契约、逐动作门控与每次请求的对象授权全部不变。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `database-scheduler-console`: 任务编辑面 SHALL 把投递目标（渠道实例与接收者）呈现为不可编辑的现值；改派只能由服务端按可信目录重新解析，不得由控制台编辑表单提交。该要求此前只以服务端拒绝（`forged_field`）表达，本次把界面义务写入规范。

## Impact

- **前端服务路径**：`channel/web/fork/handlers/pages.py`（`ChatHandler`）、`channel/web/chat.html`（include 与脚本表）、`channel/web/templates/**`（改为被实际引用，内容不变）。
- **前端模块**：新增 `channel/web/static/js/fork/`（fork 命名空间补丁模块）；删除 `channel/web/static/js/functional-scheduler.js`；`channel/web/static/js/console.js` 删除调度段；`channel/web/static/i18n/tasks-records.js` 与 `static/css/console.css` 补键与补规则。
- **测试**：删除 `tests/test_functional_scheduler.cjs`；更新 `tests/test_scheduler_frontend.cjs`、`tests/test_recovered_pages_frontend.cjs`、`tests/test_coding_page_assets.py`、`tests/fixtures/console_i18n_snapshot.json`；在 `tests/test_upstream_drift_guards.py` 增加该页的上游来源登记与逐字节守卫。
- **不改**：`openspec/specs/**` 既有 requirement 之外的规范；后端 `/api/scheduler/*` 路由、授权与数据；`console.js` / `console.css` 单体形态的其余部分（拆分与 `static/js/core|chat|views/**` 的接管由已归档 change `adopt-upstream-web-frontend-split` 承担，本次不落其覆盖映射与 manifest 门禁）。
