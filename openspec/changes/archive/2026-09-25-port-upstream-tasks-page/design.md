## Context

### 现状与接入点（已核对，非推测）

| 事实 | 证据 |
| --- | --- |
| 服务的页面是 fork 单体 | `channel/web/fork/handlers/pages.py::ChatHandler.GET` 直接读 `chat.html` 并按 `int(time.time())` 追加 `?v=<epoch>`；`rg "include" channel/web/chat.html` 为空 |
| 上游已拆分该页且 fork 树内逐字节相同 | `channel/web/{templates/views/tasks.html,templates/modals/task-edit.html,templates/modals/run-detail.html,static/js/views/tasks.js,static/js/views/tasks-modal.js}` 五者 `git hash-object` == `origin/master` 同路径 |
| 该页从未被引用 | `chat.html` 无 `views/tasks.js` / `templates/views/tasks.html` 引用；`#view-tasks` 只有 标题 + 刷新 + 空态 + 列表 + `#tasks-history` |
| fork 自建模块无样式 | `rg -o "\.rdai-[a-z-]+" static/css/console.css` 只有 `.rdai-context-*`；`rdai-scheduler` 仅出现在 `functional-scheduler.js` 与其单测 |
| 服务端契约已对齐 | create 请求体 == `_create_whitelisted_action`/`_create_task_data` 的输入；运行记录 13 字段 == `agent/tools/scheduler/run_access.py` 的 `_RUN_FIELDS` + `_EXTRA_FIELDS`；`initDropdown(el, options, selectedValue, onChange, opts)` 等 12 个外部全局在 fork `console.js` 均存在 |
| 逐动作门控已存在 | `auth/capability_matrix.py` 的 8 个 slice（`scheduler` + 6 个 `scheduler_*` + context 两个）均 `open` + `accepted`，键为 `scheduler.create` 等 |
| fork 无页签路由 | `console.js::_bootAreaDefaultView` 以 `.split(/[?/]/)[0]` 丢弃 hash 的页签段；`rg "tab=" console.js` 为空 |
| fork 已有页面级资格门控 | `console.js::_viewNavDenied` 读 `ctx.console_pages[VIEW_META.tasks.console='workbench.schedules']` |

### 为什么不能直接照抄上游页面

上游 `views/tasks.js` 与 fork 的已验收行为有四处实质冲突，前三处有测试守着：

1. `loadTasksView` 在 `data.status !== 'success'` 时 `return`，不写任何原因 → 页面永久停在 `Loading...`（`tests/test_scheduler_frontend.cjs` 锁的就是这条：关闭态必须给出可读原因并停止转圈）。
2. 忽略 `task.capabilities`，一律渲染「立即执行 / 启停」→ 渲染服务端必然拒绝的按钮；且丢掉本人/公共归属标记（`tests/test_recovered_pages_frontend.cjs` 锁定「按任务动词取服务端投影」）。
3. 不认 `feature_actions`：`scheduler.create` 关闭时仍渲染新增任务、`scheduler.runs.list` 关闭时仍拉记录、`scheduler.runs.delete` 关闭时仍渲染删除。
4. 编辑弹窗对 IM 任务允许改派并提交 `action.receiver/channel_type`；fork 的 `TaskAccessService.update_task` 对这两个字段的**变更**一律 `forged_field`（`FORBIDDEN_ACTION_FIELDS`，仅在值有变时触发）→ 用户在 fork 上保存必然失败。

另外上游 `switchTasksTab` 调 `routeNoteTab('tasks', tab)`，fork 无此函数，缺失即 `ReferenceError`（切换页签直接报错）。

## Goals / Non-Goals

**Goals**

- 定时任务页的 DOM 结构、样式与交互回到上游形态，且 `?v=` 版本戳随文件 mtime 移动。
- 保留 fork 已验收的语义：逐动作门控、按 `task.capabilities` 的按钮、本人/公共归属、关闭态原因、编辑不可改派。
- 上游 5 个文件保持上游形态（逐字节），fork 定制位于 fork 命名空间的平行实现并登记上游来源。
- 后端零改动。

**Non-Goals（显式不做，理由随附）**

- **不落上游前端覆盖映射与 `static/js/fork/manifest.json` 漂移门禁**：属已归档 change `adopt-upstream-web-frontend-split` 任务 1.1/3.1。本 change 只用既有守卫文件登记来源。
- **不删除 `console.js` / `console.css` 单体**：拆分由上述 change 的 5.1/5.2 承担，前置是 109 处前端裁定。本 change 只**缩小**单体（删除调度段）。
- **不解 `tests/test_web_console_assets.py` 的 skip**：其断言（拆分模块装载顺序、`boot.js` 位置、`js/core|chat|views` 全量接管）以整体接管为前提。
- **不实现页签地址化**：`console-route-lifecycle` 要求「需要保持选择的页签 SHALL 使用已登记的 tab 参数」，但 fork 的 router 对 config/memory/knowledge 三个有页签的视图同样不反射地址，且其 hash 模型（`#view-<id>`）与规范描述的 `#/workbench/<page>` 本就不一致——这是既有缺口，属 router 对账，归上述 change。本 change 以会话级记忆满足「刷新后恢复页签」，地址复制一半不满足，**在此明确登记**，不假装已满足。

## Decisions

### D1：上游文件原样复用，fork 定制走平行实现 + 装载顺序

**选择**：`chat.html` 用 `<!--#include-->` 引入上游片段；装载上游 `views/tasks.js`、`views/tasks-modal.js`；fork 补丁模块在其**之后**装载，用函数声明重声明的方式接管需要改写的函数。

**理由**：`fork-upstream-decoupling` 的「定制逻辑位于稳定接缝而非上游核心文件内」明确允许该形态——「当 fork 因语义需要而平行承载某上游实现的完整逻辑时（例如授权与数据作用域必须参与方法体中部），该平行实现 SHALL 位于 fork 命名空间、与上游路径不重合、上游模块保持上游形态、并登记其上游来源」。本 case 正是「门控与归属必须参与渲染方法体中部」，无法用包装或数据适配实现（除非改写上游文件或改写 `escapeHtml` 之类公共函数，两者都不接受）。

**替代方案与否决原因**：
- *fork 自有副本（markup 内联、逻辑并入 `console.js`）*：满足「风格一致」，但 fork 与上游该页从此两份实现，上游改动无法 merge 带入，且违背接缝要求里「MUST NOT 把 fork 实现与上游实现合并回同一文件」。
- *实现服务端覆盖映射（原计划 1.1）*：那是整页接管的机制，工量与本 change 不成比例，且需先有 manifest 与门禁。

**代价（如实记录）**：顶层符号同名即 `SyntaxError`，所以 `console.js` 里与上游重名的 13 个声明必须删净（其中 `currentTaskEditing`… 见下），这是硬约束而非清理洁癖。门控因此落在「上游函数被重声明」这一层，上游若改变这些函数的**内部契约**，重声明版本会静默过期——由 D4 的登记与守卫缓解，但不消除。

### D2：删除的顶层符号清单（机械枚举，非人工挑选）

上游两文件声明 49 个顶层名字，`console.js` 声明 1049 个，交集 13：

```
closeTaskEditModal currentEditingTask deleteTask loadTaskChannelOptions loadTasksView
openTaskEditModal refreshTasksView renderTaskOwnerChip runTaskNow saveTaskEdit
tasksLoaded updateTaskActionLabel updateTaskScheduleFields
```

其中 `currentEditingTask`、`tasksLoaded` 在 `console.js` 是 `let` → 与上游同名会让后加载的脚本整页 `SyntaxError`；其余 11 个是 `function`（重声明合法但会静默覆盖，按 D1 必须删）。同时删除 `_tasksModuleHandle` 的挂载/teardown、`_schedulerRequest`、`mountTasksModule` 及所有调用点。

### D3：补丁模块的内容边界

只承载「上游实现里缺的 fork 语义」，不复制上游已有的：

1. `routeNoteTab(viewId, tab)`：以会话级状态记忆页签（`sessionStorage`），进入视图时恢复；不做地址反射（见 Non-Goals）。
2. `loadTasksView`：fork 渲染——`task.capabilities` 决定按任务按钮、本人/公共归属标记、非成功响应渲染原因（区分未开放 / 无权限 / 故障，遵循 `database-runtime-consumers`「MUST NOT 把所有错误转换成空列表」）。
3. `loadRunsView`：`scheduler.runs.list` 不可用时渲染原因，不渲染空态。
4. 逐动作门控：`scheduler.create` 关 → 隐藏新增任务；`scheduler.runs.list` 关 → 隐藏执行记录页签；`scheduler.runs.delete` 关 → 隐藏删除按钮。
5. 编辑弹窗：把 `#task-edit-instance` / `#task-edit-recipient` 置为 `initDropdown(..., {readOnly:true})` 的现值展示，仅新建模式可选目标。

### D4：上游来源登记（`fork-upstream-decoupling` 要求的可检测形式）

**选择**：在既有 `tests/test_upstream_drift_guards.py` 增加一例，登记该页 4 个上游来源（路径 + 内容 sha256）与平行它的 fork 模块，摘要不符即失败并在消息里给出「需人工重新移植」的路径；另断言 fork 模块不在上游路径下、且模块头部声明其上游来源。

**理由**：该文件本就是「同步后必须重跑的上游漂移语义守卫」（task 10.5），语义最贴；不新建机制文件，避免与 3.1 的 manifest 重复。**代价**：每次上游改动该页都要更新摘要常量，这是刻意的（要求人工重新移植）。

### D5：服务路径切换的兼容面

`ChatHandler` 改用 `template.render` 后：

- `?v=` 由「每次请求一个新 epoch」变为「按文件 mtime 的十六进制戳」——这同时修掉整页资产每次刷新都重下的问题（上游印记的既有性质，`test_an_assets_version_moves_with_the_file_and_not_with_the_clock` 即为此写）。
- 受影响的既有断言：`tests/test_coding_page_assets.py` 的 `assets/<path>\?v=\d+` 改为 `?v=[0-9a-f]+`（戳的形状由 `core/template.py` 定义，仍要求「必须带戳」，不放宽强度）。
- 新页面引用的 `assets/js/views/**`、`assets/js/fork/**` 会被 `render()` 自动打戳，无需维护清单。
- **fork 运行时 fragment 仍需自行打戳**：`fragments.js` 取的是 `data-fork-fragment` 属性里的 URL，`render()` 只扫 `js/**`、`css/**`，所以 `ChatHandler` 在组装后按 `static/fragments/` 目录给「页面确实声明了」的 fragment 补戳，复用上游 `template.asset_version`（不改 `core/template.py`，那条 `no-cache` + ETag 路径不变；缺戳时 fragment 只退化为每次 304 复核）。
- `templates/modals/run-detail.html` 使 `templates/` 成为必需运行时资源——`test_web_console_assets.py::test_the_desktop_bundle_ships_everything_the_page_is_assembled_from`（当前 skip）覆盖桌面 spec 打包，本 change 在 `tests/test_task_page_assets.py` 里对 spec 的 `datas` 做了不依赖 skip 的核对。

## Risks / Trade-offs

| 风险 | 缓解 |
| --- | --- |
| 漏删某个重名声明 → 整页白屏 | 机械求交集（D2）＋ 新增守卫测试比较「上游模块顶层声明 ∩ 页面其余脚本顶层声明」为空 |
| 补丁函数随上游内部契约漂移而静默过期 | D4 的上游来源登记与逐字节守卫（检测到即失败，要求人工重新移植） |
| 编辑面只读被理解成能力退化 | 服务端本来就拒绝改派（`forged_field` 有测试），只读是把现状搬到界面；规范 delta 已写明 |
| 资产戳形状变化影响其他测试 | 逐个跑受影响测试；只放宽「戳的数字形状」，不放宽「必须带戳」 |
| 桌面打包缺 `templates/` → 打包后 500 | tasks 中列核对项；`spec` 文件在仓库内可静态核对 |

## Migration Plan

1. 先落守卫与测试（含删除 `tests/test_functional_scheduler.cjs`、更新三个前端测试与 i18n 快照），确认按预期失败。
2. 落 `ChatHandler` 与 `chat.html` 接线、i18n 与 CSS 补键。
3. 落 fork 补丁模块，删 `console.js` 调度段与 `functional-scheduler.js` 装载。
4. 全量跑 Python 与 `.cjs` 测试；启动本地 console 人工核对任务页（新建 / 编辑 / 记录 / 详情 / 删除 / 关闭态）。
5. 回退：本 change 的全部改动集中在页面接线、一个新增文件、两处小补键与测试；`git revert` 该 change 的提交即可回到现状（后端与数据未动，无迁移步骤）。

## 验证与偏差

### 计划外的测试改动（同一原因：把「断言实现细节」换成「断言页面/契约」）

页面从「fork 单体直接读文件」变成「组装 + 上游模块」，任何断言「`pages.py` 里那份手写资产清单」的测试都会因为清单消失而失败，且新的正确断言点在别处：

| 文件 | 处理 |
| --- | --- |
| `tests/test_doc_edit.py::test_document_editor_is_loaded_before_its_users` | 第二条断言由「`pages.py` 含 `js/doc-editor.js`」改为「服务的页面里有带戳的 `assets/js/doc-editor.js`」；顺带补 fork fragment 与 loader 的戳断言 |
| `tests/test_fork_fragments.cjs` | 由「`pages.py` 有 `cache_bust` 清单」改为按新归属断言：loader 是一方脚本引用（`render()` 打戳），fragment 由 fork 目录发现后补戳 |
| `tests/test_scheduler_web_update.py::test_manual_run_is_exposed_by_explicit_web_and_desktop_controls` | 读「页面装载的脚本集合」而不是 `console.js`：手动执行按钮随该页搬到补丁模块 |
| `tests/test_coding_frontend.cjs` | 删除与 `tests/test_coding_page_assets.py` 重复、且断言已删清单的那一例，保留「页面引用即会被打戳」的说明 |
| `tests/test_sidebar_account_frontend.cjs` | `section()` 增加「无 end 即切到文件末」：原来以 `// Task Edit Modal` 为末段的切片边界随调度段一起消失（`setup()` 被 54 例共用，不修则整文件报错） |

### 全量回归的判定方式

`node --test tests/*.cjs`（847 例）与 `git worktree` 出的 HEAD 基线（780 例）逐条比对失败清单：本 change 的树里**没有任何**基线不同时失败的用例（本轮剩下 45 例失败在两侧完全相同，属其它在进行中的改动：认证/账号菜单/上传/品牌会话语义等）。Python 侧调度与 console 相关套件 342 passed / 24 skipped（`test_web_console_assets.py` 的 skip 依 Non-Goals 保留）；`scripts/check-route-coverage.py` 192 routes OK。

### 未完成项

- tasks 6.4 的人工核对未做（需要起的本地 console 与 database 身份态）；其余自动化验收已完成。

## Open Questions

- 页签地址化（`console-route-lifecycle` 的 `tab` 参数）是否由 `adopt-upstream-web-frontend-split` 的 router 对账一并收口，或需要单独 change——本 change 以会话级记忆兜住刷新场景，已在 Non-Goals 登记。
- 上游 `views/tasks.js` 里「列表显示 `agent_id=''` 聚合全队」是否应在多智能体租户下加范围提示——留待页面按 `console_pages` 与 owner 归属实际展示后的观察，本 change 不改其语义。
