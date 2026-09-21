## 1. 守卫与失败基线（先测试后实现）

- [x] 1.1 在 `tests/test_upstream_drift_guards.py` 增加 `TasksPagePortDriftTests`：登记该页 4 个上游来源（`templates/views/tasks.html`、`templates/modals/task-edit.html`、`templates/modals/run-detail.html`、`static/js/views/tasks.js`、`static/js/views/tasks-modal.js` 的路径 + sha256）与其平行 fork 模块，摘要不符时失败并给出「需人工重新移植」消息
- [x] 1.2 同上文件断言 fork 补丁模块不在上游路径（`static/js/{core,chat,views}/`）之下，且其头部声明所平行承载的上游来源路径
- [x] 1.3 核对 `desktop/build/cowagent-backend.spec` 已把 `templates`（含新增的 `modals/run-detail.html` 所在目录）纳入 datas；缺则补 spec 而非改打包脚本 —— 已纳入，`tests/test_task_page_assets.py` 新增断言把它钉住（组装后的页面缺一个 include 来源就是发布版里的 500）
- [x] 1.4 更新 `tests/test_recovered_pages_frontend.cjs`：把调度段的 id/装载点断言镜像到新页面结构（`tasks-pane` / `runs-pane` / `tasks-tab-records` / `task-edit-modal-overlay`）—— 该文件只保留「按任务能力出按钮」一条（`tests/_tasks_page.cjs` 驱动）；容器 id 断言改为「chat.html include 了 `templates/views/tasks.html`，且该 fragment 里有 `#view-tasks`」
- [x] 1.5 更新 `tests/test_scheduler_frontend.cjs`：改为驱动新的 fork 补丁模块（能力关闭 → 渲染原因、不发无效请求、不停留在 Loading）
- [x] 1.6 更新 `tests/test_coding_page_assets.py` 的戳断言：由 `\?v=\d+` 放宽为「必须带 `?v=` 戳」（保留「必须带戳」的强度）—— 实际改为 `\?v=[0-9a-f]+`：戳的形态改由 `core/template.py` 定义（文件自身 mtime 的十六进制），与 `test_task_page_assets.py` 一致
- [x] 1.7 删除 `tests/test_functional_scheduler.cjs`（被替换模块的单测）
- [x] 1.8 更新 `tests/fixtures/console_i18n_snapshot.json` 与 `tests/test_console_i18n_coverage.cjs` 覆盖的键集合，使新增键纳入运行时解析检查 —— 快照补 12 个键（`task_instance_tip` 三语 + 9 个 zh-Hant 键，取值与上游逐字相同）；coverage 的 markup 键集合改为读**组装后的页面**（展开 `<!--#include-->`），否则搬进 fragment 的 30 个 `data-i18n` 键会静默失去检查
- [x] 1.9 新增装载顺序与重名守卫：比较上游模块顶层声明与页面其余脚本顶层声明，交集必须为空；断言补丁模块排在上游模块**与 `console.js` 之后**（它接管 `console.js` 的 `initDropdown` 并捕获上游的同名函数，两者都必须先装载）

## 2. 服务路径与页面接线

- [x] 2.1 `channel/web/fork/handlers/pages.py::ChatHandler.GET` 改为经 `channel/web/core/template.py::render('chat.html')` 组装，删除自建 `?v=<epoch>` 追加逻辑，保留 `no-store` 头与 `_web_navigation_mode` 行为
- [x] 2.2 `channel/web/chat.html`：`#view-tasks` 整块替换为 `<!--#include templates/views/tasks.html-->`，`#task-edit-modal-overlay` 替换为 `<!--#include templates/modals/task-edit.html-->`，新增 `<!--#include templates/modals/run-detail.html-->`
- [x] 2.3 `channel/web/chat.html` 装载 `assets/js/views/tasks.js` 与 `assets/js/views/tasks-modal.js`（排在 `console.js` 之前），并移除 `assets/js/functional-scheduler.js`
- [x] 2.4 装载新的 fork 补丁模块（顺序：上游模块 → `console.js` → 补丁），路径沿用同目录其余资产的相对写法（`assets/js/fork/tasks-console.js`）
- [x] 2.5 删除 `channel/web/static/js/functional-scheduler.js`
- [x] 2.6 保留 fork 运行时 fragment 的戳：`fragments.js` 走 markup 属性取 `static/fragments/*.html`，组装器的戳只覆盖 js/css，因此 `ChatHandler` 在组装后按目录给已声明的 fragment 补 `?v=<hex mtime>`（复用 `template.asset_version`，不改 `core/template.py`）

## 3. i18n 与样式补键

- [x] 3.1 把上游该页用到的键补进 `channel/web/static/js/i18n/tasks-records.js`（zh / zh-Hant / en 三份，含 `records_*`、`task_instance_*`、`task_recipient_*`、`task_action_*`、`task_run_*`、`task_schedule_*`、`tasks_tab_records`、`task_*_title`、`record_detail_*`）
- [x] 3.2 `channel/web/static/css/console.css` 补 `.cfg-dropdown-disabled`（fork 页面唯一加载的样式表；上游该规则在 `components.css`）

## 4. fork 补丁模块（平行实现边界见 design D3）

- [x] 4.1 建立 fork 命名空间模块（`channel/web/static/js/fork/`），头部登记所平行承载的上游来源与移植要求
- [x] 4.2 `routeNoteTab(viewId, tab)`：会话级记忆并恢复页签；不做地址反射，注释写明该收口归属
- [x] 4.3 逐动作门控：`scheduler.create` 关 → 不渲染新增任务；`scheduler.runs.list` 关 → 不渲染执行记录页签且不拉取；`scheduler.runs.delete` 关 → 不渲染删除入口
- [x] 4.4 `loadTasksView`：按 `task.capabilities` 决定按任务按钮、本人/公共归属标记；非成功响应渲染原因（区分未开放 / 无权限 / 故障），不使用客户端归属推断
- [x] 4.5 `loadRunsView`：`scheduler.runs.list` 未开放时渲染原因而非空态；运行详情读取失败时保留预览并说明
- [x] 4.6 编辑弹窗：已有任务的渠道实例与接收者以只读现值呈现，仅新建模式可选目标；不提交 `receiver`/`channel_type` 变更
- [x] 4.7 迟到响应守卫按 `_authEpoch` + `_authContextSeq` 复合判定（进入应用即换租户），任一改变即丢弃在飞的列表/记录响应

## 5. 单体收缩（删除重名声明）

- [x] 5.1 机械求交集并删除 `console.js` 与上游重名的顶层声明：`closeTaskEditModal currentEditingTask deleteTask loadTaskChannelOptions loadTasksView openTaskEditModal refreshTasksView renderTaskOwnerChip runTaskNow saveTaskEdit tasksLoaded updateTaskActionLabel updateTaskScheduleFields`
- [x] 5.2 删除 `_tasksModuleHandle` 挂载/teardown、`_schedulerRequest`、`mountTasksModule` 及其全部调用点与 `#tasks-history` 依赖
- [x] 5.3 确认 `console.js` 不再引用被删符号（含 `refreshTasksView` 的调用点与视图切换处的调度段）—— `runTaskNow` 调用点改为存在性守卫（补丁模块未装载时不会炸）

## 6. 验收

- [x] 6.1 运行 `node --test tests/test_recovered_pages_frontend.cjs tests/test_scheduler_frontend.cjs tests/test_console_i18n_coverage.cjs` 与新增守卫，全部通过
- [x] 6.2 运行调度相关 Python 套件（`tests/test_scheduler_*.py`、`tests/test_upstream_drift_guards.py`、`tests/test_web_console_assets.py` 保持既有 skip、`tests/test_coding_page_assets.py`、`tests/test_doc_edit.py`）：342 passed / 24 skipped
- [x] 6.3 复跑全量回归与 `scripts/check-route-coverage.py`，确认后端路由与授权语义未变 —— 192 routes OK；`node --test` 的失败集合与 HEAD 基线逐条比对后无本 change 引入的失败（清单见 design「验证与偏差」）
- [ ] 6.4 本地 console 人工核对：任务列表、新增任务（实例 + 接收者选择）、编辑（目标只读）、执行记录分页、运行详情、删除记录、关闭态原因；记录成功与拒绝两侧结果
- [x] 6.5 确认 `rg -o "\.rdai-scheduler-" channel/web` 为空（旧模块与其样式依赖已不存在）
