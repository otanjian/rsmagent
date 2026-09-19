# add-todo-header-bell 验收证据

运行环境：本机 dev 实例 `http://localhost:9899`（`channel/web` 静态资源由运行中的进程直接读取磁盘，改后无需重启即可生效）。

## 1. 前端用例

```
$ node --test tests/test_todo_frontend.cjs
✔ storage unavailable shows only a localized failure and retries on revisit
✔ server error shows only a localized failure and retries on revisit
✔ legacy HTTP 200 error body shows only a localized failure and retries on revisit
✔ non-JSON response shows only a localized failure and retries on revisit
✔ network failure shows only a localized failure and retries on revisit
✔ unauthorized retains its localized message without an empty result
✔ todo_disabled retains its localized message without an empty result
✔ failed refresh clears stale results and pagination, then retries on revisit
✔ bell mounts before the tenant selector and is idempotent
✔ bell count mirrors the sidebar badge with a 99+ overflow
✔ bell entry hides only on an authoritative capability fact
✔ clicking the bell navigates to the todo view
✔ window focus refreshes the summary without loading the list
ℹ tests 13 / pass 13 / fail 0
```

## 2. 关联前端用例（回归）

```
$ node --test tests/test_console_view_registry.cjs tests/test_console_i18n_parity.cjs tests/test_nav_area_frontend.cjs
ℹ tests 17 / pass 17 / fail 0
```

## 3. 后端待办用例（契约未变）

```
$ .venv/bin/python -m pytest tests/test_todo_web_database.py tests/test_todo_service.py tests/test_todo_private_state.py tests/test_todo_tool_identity.py -q
59 passed in 7.62s
```

## 4. 浏览器实测（真实登录会话，窗口宽 1912）

`Runtime.evaluate` 读取真实 DOM（装载位置与可访问名称）：

```json
{
  "exists": true,
  "parent": "workbench-header h-14 flex items-center gap-3 px-4 border-b ...",
  "prev": "flex-1",
  "next": "tenant-selector",
  "headerChildren": ["menu-toggle","workbench-context","flex-1","todo-bell-btn","tenant-selector","workspace-toggle-btn"],
  "tooltip": "我的待办",
  "aria": "我的待办"
}
```

- 入口落在 `.workbench-header` 内，前一个兄弟是标题区末端的 `flex-1`，后一个兄弟是 `#tenant-selector`，即需求红框位置。
- `data-tooltip` / `aria-label` 已由 `menu_todo` 填充。

`GET /api/todos/summary` 实测返回 `{"enabled":true,"bound":true,"open":4,"overdue":1}`，同一次页面状态下两处角标读数一致：

```json
{ "bellExists": true, "bellHidden": false,
  "bellBadgeText": "4", "bellBadgeHidden": false, "bellBadgeOverdue": true,
  "sidebarBadgeText": "4", "sidebarBadgeRed": true }
```

- 顶栏角标与侧栏角标同屏同为 `4`，且都带上逾期红色样式，未出现两处口径分歧。

点击接线（同一会话，点击后等待待办视图加载完成）：

```json
{ "breadcrumb": "我的待办", "todoActive": true, "sidebarSelected": true,
  "listRendered": true, "emptyHidden": true }
```

- 点击铃铛后面包屑切到「我的待办」，`#view-todo` 与侧栏条目同时置为 active，列表渲染出真实数据 —— 与选择侧栏「我的待办」等效。

渲染度量（`getBoundingClientRect` + `getComputedStyle`）：

```json
{ "button": {"x":1685,"y":15.5,"w":30,"h":32},
  "badge":  {"x":1699,"y":13.5,"w":18,"h":18},
  "badgeStyle": {"bg":"rgb(226, 232, 240)","color":"rgb(51, 65, 85)","font":"10px","radius":"999px"} }
```

- 角标贴在入口右上角，18×18 圆角胶囊；铃铛图标像素存在（Font Awesome 6 Free 已加载）。
- 逾期态像素采样：角标主色为 `rgb(202, 58, 50)`（`#dc2626`）配白色文字，与侧栏逾期角标一致。

截图：`evidence/bell-chat-view.png`（对话视图下的铃铛与计数）、`evidence/bell-opens-todo-view.png`（点击后到达的待办视图）。

## 5. 规范校验

```
$ openspec validate add-todo-header-bell --strict
Change 'add-todo-header-bell' is valid
```

## 未覆盖项

- 计数 `0`（隐藏角标、保留入口）与 `120`（显示 `99+`）两种口径由第 1 节的 vm 用例覆盖；真实登录数据的现场读数恰为 `open: 4 / overdue: 1`，未在真实数据上复现 0 与 `99+`，未做人为造数。
- 深色主题与繁体/英文下的角标外观未逐项截图核对（tooltip 与可访问名称由 `data-tip-key` / `data-i18n-aria-label` 走既有本地化通路，与其它顶栏控件同一机制）。
