# remove-todo-sidebar-entry 验收证据

运行环境：本机 dev 实例 `http://localhost:9899`（`channel/web` 静态资源由运行中的进程直接读取磁盘，改后无需重启即可生效）；浏览器为 Cursor 内置浏览器，真实登录会话（`admin`）。

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
✔ bell count shows the capped summary total and hides at zero
✔ bell entry hides on an authoritative denial, survives a transient failure
✔ clicking the bell navigates to the todo view
✔ window focus refreshes the summary without loading the list
✔ the sidebar template no longer offers a todo entry
✔ the sidebar badge styling is gone with the entry it dressed
✔ the summary drives the bell badge only, never a sidebar badge
ℹ tests 16 / pass 16 / fail 0
```

新增的三条即本 change 的判定点：模板无待办条目、样式表无侧栏角标规则、summary 不再把角标画到侧栏。改动前它们分别为红（见第 5 节的 HEAD 对照）。

## 2. 关联前端用例（回归）

```
$ node --test tests/test_console_view_registry.cjs tests/test_nav_area_frontend.cjs \
    tests/test_channel_scope_nav_frontend.cjs tests/test_admin_area_group_gating.cjs \
    tests/test_workbench_menu_grant_frontend.cjs tests/test_external_connections_frontend.cjs
ℹ tests 52 / pass 52 / fail 0
```

## 3. 后端菜单映射（签页未变）

```
$ .venv/bin/python -m pytest tests/test_console_menu_mapping.py -q
33 passed in 11.15s
```

## 4. 浏览器实测

### 4.1 侧栏（对话视图下的真实 DOM）

`Runtime.evaluate` 读取 `#sidebar-nav`：

```json
{
  "sidebarTodoEntries": 0,
  "sidebarTodoNavBadges": 0,
  "visibleSidebarViews": [
    { "view": "chat", "label": "对话" },
    { "view": "agent-workbench", "label": "智能体" },
    { "view": "tasks", "label": "定时任务" },
    { "view": "knowledge", "label": "知识库" },
    { "view": "scenes", "label": "场景应用" },
    { "view": "admin-home", "label": "控制台概览" },
    { "view": "agents", "label": "智能体管理" },
    { "view": "skills", "label": "工具与技能" },
    { "view": "memory", "label": "记忆管理" },
    { "view": "config", "label": "模型服务" },
    { "view": "channels", "label": "消息渠道" },
    { "view": "external_connections", "label": "外部系统接入" },
    { "view": "system_user", "label": "成员管理" },
    { "view": "org", "label": "组织架构" },
    { "view": "roles", "label": "角色权限" },
    { "view": "tenant", "label": "租户管理" },
    { "view": "platform", "label": "系统设置" },
    { "view": "branding", "label": "品牌设置" },
    { "view": "logs", "label": "运行日志" },
    { "view": "audit", "label": "审计" }
  ]
}
```

- `data-view="todo"` 条目与 `.nav-badge` 计数胶囊均为 0 —— 入口从 DOM 中消失，而非被 `hidden` 遮蔽。
- 「定时任务」紧随「智能体」，即红框位置现在的邻居，与变更前截图一致（顺序未动）。

### 4.2 顶栏铃铛（位置与可访问名称未变）

```json
{
  "bell": { "exists": true, "hidden": false, "aria": "我的待办", "tooltip": "我的待办",
            "x": 1685, "y": 16, "w": 30, "h": 32 },
  "bellBadge": { "text": "4", "hidden": false },
  "todoViewInDom": true
}
```

铃铛几何量与前一 change（`add-todo-header-bell`）验收记录中的 `x:1685, y:15.5, w:30, h:32` 一致；`data-tooltip` / `aria-label` 仍由 `menu_todo` 填充；`#view-todo` 节点仍在 DOM 中。

### 4.3 深链

整页加载 `http://localhost:9899/chat#view-todo`：

```json
{ "hash": "#view-todo", "activeViewIds": ["view-todo"],
  "breadcrumb": "工作台 / 我的待办", "sidebarTodoEntry": "ABSENT" }
```

待办页面照常到达，且此时侧栏仍无待办条目。

**一处口径修正**：整页加载 `http://localhost:9899/chat#todo`（不带 `view-` 前缀）**不会**到达待办页，实测 `activeViewIds` 为空、`sidebarTodoEntry: "ABSENT"`，侧栏「对话」为 `current`，即回退到区域默认页。原因是 `console.js:1614` 的 `_bootAreaDefaultView()` 只剥 `#view-` 前缀：

```javascript
const hashView = _normalizeViewId(String(location.hash || '').replace(/^#view-/, '').split(/[?/]/)[0]);
```

`#todo` 因此不被识别为视图，`VIEW_META[hashView]` 为假值而落到区域默认页。这是**既有行为，与本 change 无关**：`git diff -- channel/web/static/js/console.js` 为空，路由代码一行未动；`webhelp` 之外的导航也一直以 `#view-` 写入（同文件 1911 行的遗留转发判断同样以 `location.hash.indexOf('#view-') === 0` 为条件）。本 change 的 proposal / spec / design / tasks 原先写的「`#todo` 深链」已全部改为 `#view-todo`，并在 design 的 Context 表中补记了该地址格式。

截图：`evidence/sidebar-without-todo-entry-chat-view.png`（对话视图，红框位置已无待办条目，顶栏铃铛带 `4`）、`evidence/todo-page-via-view-todo-hash.png`（`#view-todo` 整页加载后到达的待办页）。

## 5. 侧栏角标样式的删除（RED → GREEN）

```
$ git show HEAD:channel/web/static/css/appearance.css | rg -c '\.nav-badge'
4
$ rg -c '\.nav-badge' channel/web/static/css/appearance.css
0
```

改动前 4 条规则（基础胶囊 / `.hidden` / `.bg-red-500` / `#app.sidebar-collapsed`），改动后 0 条，即第 1 节新增用例在改动前为红、改动后为绿。删除依据：全仓检索（含打包产物、含忽略文件，排除 `node_modules`）确认除该样式表、本 change 的文档与测试桩外，无任何元素带 `nav-badge`。

## 6. 规范校验

```
$ openspec validate remove-todo-sidebar-entry --strict
Change 'remove-todo-sidebar-entry' is valid
```

## 未覆盖项

- 计数 `0`（隐藏角标）与 `120`（显示 `99+`）两种口径由第 1 节的 vm 用例覆盖；真实登录数据的现场读数恰为 `open: 4`，未在真实数据上复现 0 与 `99+`，未做人为造数。
- 「能力关闭 / `bound:false` 时铃铛入口隐藏」在真实浏览器上未复现（当前 admin 身份为可用态），由第 1 节的 vm 用例覆盖；`summary` 接口与签页契约本 change 未改。
- 深色主题、`classic` / `sidebar-collapsed` 等外观组合下未逐项截图。需注意：第 5 节删除的 `.nav-badge` 规则中包含 `#app.sidebar-collapsed #sidebar .nav-badge { display: none; }`，该规则随元素消失已无作用对象，折叠侧栏下的观感不受影响。
- 繁体 / 英文下侧栏文案未逐项核对；本次只删条目、未新增或删除 i18n 文案键（`menu_todo` 保留，供铃铛与面包屑继续使用），i18n 三语快照不变。
