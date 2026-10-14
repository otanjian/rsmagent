# 阶段 5 证据：命名窗口与落地路由（任务 5.2）

日期：2026-10-13。对应报告遗留问题：`docs/design/windows-desktop-verification-2026-10-13.md` 第 290 行（第 4 条）"命名窗口导航不一致"。

## 1. 已复现差异

> 「控制台」为 `<a href="/admin" target="cow-admin">`、「工作台」为 `<a href="/chat" target="cow-workbench">`，以命名窗口另开窗口而非应用内导航；且控制台窗口最终落在 `/scheduler` 而非 `/admin`。

定位：`channel/web/chat.html:373`、`:380`（全仓仅此两处命名 target，已核对 `channel/web/**.html|js`）。

## 2. 机制（三段证据）

1. **带 `target` 的点击等于弹窗**，而客户端本就实现了同窗口切换：`console.js` 对 `.sidebar-item` 绑定 `click` → `if (item.id === 'nav-open-admin') { event.preventDefault(); _openNavArea('admin') }`；`_openNavArea` 的注释写着"Switch in the SAME window so workbench <-> admin never triggers a full page load"。`target` 只在**处理器未生效**时起作用：中键/Ctrl+点击、脚本尚未加载、或任何非 JS 触发的导航。
2. **桌面容器一律拒绝弹窗**：`desktop/src/main/remote/container.ts` 的 `setWindowOpenHandler` 对同源入口路径既不外开也不进内容窗口，最终 `return { action: 'deny' }` —— 即桌面端点这两个入口时**什么都不会发生**。
3. **`/scheduler` 的来源**：`_bootAreaDefaultView` 在 `/admin` 下优先消费 `sessionStorage['cow_admin_pending_view']`（区域切换需要指定视图时由 `console.js:2762` 写入），否则才 `navigateTo('admin-home')`。新开的命名窗口**继承了 sessionStorage 的副本**，于是消费了残留的 pending 视图，落到 `/scheduler` 而不是 `/admin`。

## 3. 改动（最小）

| 位置 | 改动 |
|---|---|
| `channel/web/chat.html` | 两个入口去掉 `target="cow-admin"`/`target="cow-workbench"` 与随之无意义的 `rel="noopener"`，并在原处写明原因；`href` 保持登记路径 `/admin`、`/chat` |
| `docs/console-navigation-and-windows.md`（新增） | 操作说明：首次/重复点击行为、键盘、中键与无脚本回退；为什么不带 `target`；排查（多余窗口、`cow_admin_pending_view` 的一次性语义、深链接与内容窗口的边界） |

未改动 `console.js` 的导航逻辑、路由登记与拒绝规则（报告差异的根因在标记，不在逻辑），也未新增窗口类型。

## 4. 契约对照

| 契约条目 | 对照结果 |
|---|---|
| `console-entry-consistency`："首页直接导航动作 SHALL 使用「打开」类文案并跳转获准页面" | 两个入口现在都跳转获准页面，且同窗口完成 |
| `console-route-lifecycle`："页面与页签具有稳定地址" | 点击后地址为 `/admin`（管理首页）或 `/`（工作台）；不再由继承的 pending 视图决定 |
| `console-route-lifecycle`："重复选择同一页面 SHALL 不重复添加历史项" | 重复点击不再新开窗口；同窗口切换由 `_openNavArea` 的 `pushState`/`navigateTo` 记账 |
| `console-route-lifecycle` 旧链接兼容 | `href="/chat"` 保留为登记/兼容路径，按既有规则规范化为 `/`（本次不改规范化逻辑） |
| 桌面容器"页面不能弹窗"（既有实现） | 标记不再要求容器做它明确拒绝的事 |

## 5. 验证

新增 `tests/test_console_entry_named_windows.cjs`（**6/6 通过**）：

1. 两个入口不带 `target`；
2. 整个控制台外壳不再有 `target="cow-*"`；
3. 两个入口保留登记路径 `/admin`、`/chat`；
4. 点击处理器 `preventDefault` + `_openNavArea`，且 `_openNavArea` 内**没有** `window.open`、有 `pushState`/`navigateTo`；
5. `/admin` 直开时无 pending 即落 `admin-home`，且 pending 键"读取即删除"（不会越过一次切换存活）；
6. 桌面容器确实 `deny` 弹窗（说明为什么标记不能依赖命名窗口）。

回归（本机）：

| 测试 | 结果 |
|---|---|
| `test_nav_area_frontend.cjs` | 7 / 0 |
| `test_channel_scope_nav_frontend.cjs` | 11 / 0 |
| `test_console_workspace_frontend.cjs` | 34 / 0 |
| `test_admin_area_group_gating.cjs` | **既有环境失败**：`spawnSync D:\AIPJ\rsmagent\.venv\bin\python ENOENT`（本 checkout 的 `.venv` 是 macOS 专用） |
| `test_workbench_menu_grant_frontend.cjs` | **既有期望过期**：`console.js` 中已无 `function loadSidebarRecentSessions`、`chat.html` 中已无 `id="sidebar-recent-list"`；与本次改动无关（本次对 `chat.html` 的 diff 仅为两处 `target` 移除 + 注释） |

## 6. 判定与未验证

- **5.2 勾选**：命名窗口与落地路由已记录（本文件 + `docs/console-navigation-and-windows.md`），已复现差异按两份契约修复，首次/重复点击行为有回归测试守住，操作说明已补。
- **未验证**：真实浏览器里"重复点击只追加一个历史项"的逐条历史断言（需要页面会话/CDP 巡检），以及真实安装包中的桌面点击行为（需要打包应用）；本机验证到标记、处理器与容器策略三层。
