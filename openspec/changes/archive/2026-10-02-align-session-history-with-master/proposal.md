## Why

历史会话应直接展开在左侧「历史会话」菜单下，保留 master 的完整列表、空间组织、类型标识和紧凑密度，为对话和文件区释放原中间历史栏的宽度。以 2026-10-02 核实的 `origin/master` 提交 `48c0d79c36146950667f8d1884ab823deae62305` 为基线，对齐聊天中的历史展示和菜单布局密度。

## What Changes

- 将完整分页历史内嵌在主导航「历史会话」菜单下，点击菜单展开／收起；去掉中间栏、重复标题和专用遮罩，保留紧凑行及滚动。聊天时新建沿用左侧主导航，不在历史面板重复放置按钮。现有完整历史页和归档弹窗保留，通过面板入口可达。
- 直接复用已有列表读取、分页、分组、项目管理和会话操作；面板与完整历史页互斥显示，共用一份列表和活动查询状态，不新增双列表缓存或同步机制。
- 基于 master 的模板与样式对齐主导航、历史行、分组标题、头像和新对话下拉菜单的字号与间距。保留 rdai 菜单名称、业务分区、品牌配色和权限。
- 接回已有新对话临时行和刷新入口，保留 owner、搜索、归档、coding、流续接及身份／编辑保护；临时行不写后端空记录。
- 修订现行受限预览和侧栏布局条款，统一「历史会话」命名，修复操作遮挡、重命名分页回退和语言刷新遗漏。归档超过 50 条的分页缺口不属于本次展示对齐，留作独立问题。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `session-history-workbench`：将基于 master 的完整历史列表内嵌到左侧菜单下，与既有搜索页和归档入口共存，替换旧预览限制并保留原业务行为。
- `workbench-sidebar-launch`：主导航历史菜单可展开完整列表，同时对齐菜单布局密度，复用统一新对话流程及既有布局开关。

## Impact

- 来源为 master 的 `templates/layout/session-panel.html`、`static/js/views/sessions.js`、`static/css/sessions.css` 和主导航模板；当前实际接入点为 `chat.html`、`console.js` 中的列表／导航调用及对应 CSS。新增面板展示放在 fork 文件，来源与差异登记沿用 `web-console-frontend-modules`，已有单体业务逻辑保留唯一实现。本次不承担通用资源覆盖框架、全历史模块拆迁或整站装载工具升级。
- 现有 API、ConversationStore、`project_store`、`session_prefs` 和 OpenCode 数据归属均不变，无后端协议或数据库迁移；独立 Desktop renderer 不在范围。
- 保留 `fix-session-history-refresh` 已完成的行为及其他在途工作，不把其他 change 归档或整体交付设为实施前置。规范合并时只检查重叠条款，避免旧预览表述覆盖本次决策。
- 实施与验收进度以 tasks.md 为准；本轮不包含归档。
