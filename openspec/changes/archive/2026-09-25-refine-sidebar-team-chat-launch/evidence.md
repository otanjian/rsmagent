# 实施证据（refine-sidebar-team-chat-launch）

本文件记录 tasks.md 各阶段要求的真实检查结果。阶段 1 的结果是进入阶段 2 的前置；
未取得证据的任务不得勾选。命令均在仓库根目录执行。

## 阶段 1 基线与依赖切片核验

### 1.1 实际加载链（在用侧栏 / 团队弹窗 / 历史入口）

检查：`channel/web/chat.html`、`channel/web/fork/handlers/pages.py::ChatHandler`、
`channel/web/core/template.py`。

结论：

- 页面由 `ChatHandler.GET` 经 `channel/web/core/template.py::render('chat.html')` 组装；
  `<!--#include path-->` 由该组装器在**服务端**展开，展开后再经 `_stamp_fork_fragments` 打版本号。
- `chat.html` 只 include 三个 fragment：`templates/views/tasks.html`、
  `templates/modals/task-edit.html`、`templates/modals/run-detail.html`。
- **在用侧栏**是 `chat.html` 内联的 `<aside id="sidebar">`（品牌 → `#sidebar-new-chat` →
  `#sidebar-nav`（工作台 `data-nav-shell="workbench"` + 管理 `data-nav-shell="admin"`）→
  `#sidebar-recent` → 底部 `#sidebar-account-footer`）。`templates/layout/sidebar.html`
  与 `static/js/chat/new-chat.js` **不在加载链中**（无任何 include/script 引用），不得作为修改目标。
- **在用团队弹窗**是 `chat.html` 内联的 `#team-chat-modal`（含 `#team-chat-list`、
  `#team-chat-status`）；`templates/modals/team-chat.html` 是未加载的旧副本。
- 在用脚本 `channel/web/static/js/console.js`（脚本清单第 2745 行，`defer`）；
  `views/sessions.js` 未被 `chat.html` 装载，故 `switchSession` 以 `console.js:10639` 为准。
- 历史入口：`#sidebar-recent-more`（查看全部）→ `navigateTo('history')`；`#sidebar-recent-archived`
  → `openArchivedSessionsModal()`；`#sidebar-recent-label` 双击 → `navigateTo('history')`。

### 1.2 `add-opencode-coding-agents` 的类型投影、普通路径拒绝与编码历史

证据（真实代码位置）：

- 类型投影：`channel/web/fork/common.py:319`（workbench 投影 `agent_type`）；
  `channel/web/fork/handlers/agents.py:309/374`（管理快照）；`channel/web/fork/runtime.py:2598`
  （会话列表 badge 的 `agent_type`，缺字段按 `AGENT_TYPE_NORMAL`）。
- 客户端读取：`console.js:2652`（workbench 投影保留 `agent_type`）、`console.js:10444`
  `agentTypeOf`（缺字段 = normal）、`console.js:10454` `isCodingAgent`。
- 普通路径拒绝：`channel/web/fork/common.py:250` `_reject_coding_agent`（`coding_web_only`）；
  团队名册写入路径已调用（`fork/handlers/sessions.py:465`）。
- 编码历史：`console.js` 的 coding 段（`channel/web/static/js/coding.js` + `agentTypeOf` 路由），
  会话列表通过 `agent_type` 决定打开方式。

可运行测试与结果：

```
.venv/bin/python -m pytest tests/test_coding_agent_type_boundary.py \
    tests/test_session_team_runtime.py tests/test_team_addressing.py -q -p no:randomly
→ 22 passed

node --test tests/test_coding_frontend.cjs tests/test_sidebar_session_archive_frontend.cjs
→ tests 35 / pass 35 / fail 0
```

**结论**：类型投影、编码普通路径拒绝与编码历史切片均已在真实代码中落地并有绿色测试支撑，
可作为本 change 的依赖；本 change 不修改其源文件、不替其归档。

### 1.3 现有合同（默认解析 / 使用范围 / settings 授权 / 归档重命名 / 离页保护）

- 统一默认解析：`channel/web/fork/common.py:343` `_resolve_default_agent`（返回
  `{agent_id, source}`，source ∈ user/tenant/shared/any/None）；
  `_workbench_chat_readiness`（`common.py:492`）是「能否开聊」的权威投影，
  与发送路径 `_authorize_chat_session`（`fork/handlers/chat.py:60`）同口径。
- 使用范围：`_require_chat_use`（`fork/handlers/chat.py:20`，功能权限 `chat.use`）、
  `_require_agent_action(ctx, id, "use", "agent.use")`（`fork/authorization.py:285`，
  私有 owner 优先于管理员旁路）、`_require_tenant_agent_binding`（`fork/authorization.py:794`，
  跨租户 404）、`_require_private_owner`（`authorization.py:825`）。
- 会话 settings 授权与持久化：`fork/handlers/sessions.py:369` `SessionSettingsHandler`
  （`GET`/`POST /api/sessions/{sid}/settings`，路由登记 `route_registry.py:307`，
  由 `web_channel.py:249/508` 导入 fork 实现 —— **fork 实现是在用实现**）。
  `POST` 在 `_db_scope()` 内做模型 `_require_model_use` 与 members 的 `_reject_coding_agent`，
  名册集合变化时调用 `_drop_team_runtimes`。
- 持久化：`agent/workspace/session_prefs.py::set_prefs` / `_save`。
  **当前 `_save` 吞掉落盘异常只记 warning（76-79 行），无法向调用者传播** —— 这是任务 2.4 的改造点。
- 归档 / 重命名：`console.js:9551 archiveSidebarSession`、`console.js:9586 renameSidebarSession`、
  `console.js:9769 openArchivedSessionsModal`；后端 `SessionDetailHandler`（PUT/DELETE）。
- 离页保护：`wsGuardUnsaved(proceed)`（`workspace.js`，coding 侧经 `window.wsGuardUnsaved`，
  `coding.js:123`）。

共享文件边界（与在途 change）：

- `add-multimodal-image-input`：进行中任务仅涉及附件路径标记，未改 `console.js`/`chat.html`
  （tasks 4.3 明确「前端未改动」）。本 change 不触碰附件草稿与上传标记。
- `port-upstream-tasks-page`：已改写 `chat.html`（任务页三处 include、脚本清单）与
  `console.css`（`.cfg-dropdown-disabled`）。本 change 只改侧栏/弹窗/最近会话相关区块，
  保留其 include 行、脚本顺序与 `#view-tasks` / `#task-edit-modal-overlay` 挂载点。

### 1.4 验收矩阵（本 change 的 acceptance matrix）

| # | 场景 | 入口 | 证据类型 |
| --- | --- | --- | --- |
| A1 | 混合 normal/coding 候选 | 侧栏团队按钮、历史页团队入口 | 前端单测 `test_team_chat_launch_frontend.cjs` |
| A2 | 仅 1 位 normal / 全 coding | 弹窗空态与禁用 | 同上 |
| A3 | 手工提交 coding 为 owner 或 member | `POST .../settings` | `test_coding_agent_type_boundary.py` + 新增路由测试 |
| A4 | 撤权 / 停用 / 跨租户 / 他人私有 | 同上 | 路由 + 作用域测试 |
| A5 | 落盘失败 | `session_prefs` 故障注入 | `test_session_team_runtime.py` 扩展 |
| A6 | 双击 / 响应丢失重试 | 弹窗 | 前端单测 |
| A7 | 编码会话中发起团队 / 取消 | 侧栏按钮 | 前端单测 |
| A8 | 侧栏 5 条预览 + 查看全部 | 侧栏 | `test_sidebar_session_archive_frontend.cjs` + 新增 |
| A9 | 团队/编码标识与成员恢复 | 侧栏、历史页 | 前端单测 |
| A10 | 三语 + 主题 + 360px | 页面 | 人工/视觉验收（阶段 5 记录） |

## 阶段 2 团队候选与服务端边界

### 2.1–2.2 独立的团队候选投影与全部团队表面

证据（真实代码位置）：

- `console.js::teamCandidateAgents()` 是团队专用投影：从真实使用范围（`availableChatAgents()`）读入，
  再按 `agentIsCoding()` 排除 coding；`agent_type` 缺失按 normal（`agentTypeOf`），未知对象不列入。
- 团队表面全部改用该投影：创建弹窗列表与搜索（`renderTeamChatRows`/`teamChatMatches`）、预选与默认响应者
  （`initialTeamChatSelection`）、已选摘要（`renderTeamChatSelected`）、邀请（`renderComposerAgentMenu` 的
  invite 段）、团队 @ 候选与临时成员展示（`validTeamMemberRows`/`sessionRoster`）。
- 单人 coding 切换不受影响：`availableChatAgents()` 仍服务输入框底部的 Agent 切换（
  `renderComposerAgentMenu` 的 switch 段）与工作台目录/编码历史。

可运行测试与结果：

```
.venv/bin/python -m pytest tests/test_team_roster_boundary.py \
    tests/test_coding_agent_type_boundary.py tests/test_session_team_runtime.py \
    tests/test_team_addressing.py tests/test_workbench_sidebar_launch.py -q -p no:randomly
→ 48 passed

node --test tests/test_team_chat_launch_frontend.cjs tests/test_composer_agents_frontend.cjs \
    tests/test_workbench_sidebar_launch_frontend.cjs tests/test_session_history_frontend.cjs \
    tests/test_sidebar_session_archive_frontend.cjs tests/test_sidebar_session_rename_frontend.cjs \
    tests/test_workbench_menu_grant_frontend.cjs tests/test_nav_area_frontend.cjs \
    tests/test_console_i18n_frontend.cjs tests/test_coding_frontend.cjs \
    tests/test_agent_chat_launch_frontend.cjs
→ tests 152 / pass 152 / fail 0
```

### 2.3–2.5 服务端整次拒绝、持久化错误与历史无效名册

证据（真实代码位置）：

- `channel/web/fork/common.py::_validate_team_roster` 在 `_db_scope()` 内逐个校验 owner 与 members 的
  租户绑定、注册表存在性、启用状态、`agent.use` 与私有 owner 规则，再按 `agent_type` 拒绝 coding；
  规则是**整次拒绝，从不修补**，返回标准化名册（去重、owner 不出现在 members）。
- `channel/web/fork/handlers/sessions.py::SessionSettingsHandler.POST` 调用该函数；失败返回明确错误码，
  未持久化则返回 500（`session_prefs_write_failed`）且不退休运行时。
- `agent/workspace/session_prefs.py` 的 `_save` 默认严格化并抛 `SessionPrefsError`，`_save_best_effort`
  保留非关键写入的旧语义。

可运行测试与结果：见 2.1–2.2 的 pytest 组（含新增 `tests/test_team_roster_boundary.py` 15 项与
`tests/test_coding_agent_type_boundary.py` 的 3 项类型边界），覆盖 owner/member 两种 coding 注入、
`coding_web_only`、撤权 `forbidden`、越租户 404、他人私有 403、停用 `team_member_disabled`、
未知 `team_member_unknown`、落盘失败 500 且无副作用、旧无效名册显式清理与退回单人。

## 阶段 3 选择弹窗与可靠创建

证据（真实代码位置）：`console.js` 的 `_teamChatDraft` 草稿态 + `prepareTeamChatSession()` /
`commitTeamChatSession()` 的「先保存、后切换」事务；`setTeamMembers(ids, target)` 显式携带
`{sessionId, agentId}` 目标快照，不依赖全局当前会话；`commitPreparedSession()` 让普通新建与团队启动
共用同一提交路径；`newChatLeavesCodingPane()` 单独承载编码离页保护。

可运行测试与结果：`tests/test_team_chat_launch_frontend.cjs`（18 项）覆盖候选人排除 coding、从编码会话
打开弹窗且保留原会话、搜索不清空选择、移除默认响应者后需显式指定、两位成员+响应者的前置条件、
只写 prepared 会话、被拒写入不留痕、响应丢失/取消不提交、双击防重入、保存中切租户丢弃迟到提交、
成员失效在写入前拒绝、保存后 composer 按真实名册渲染、保存期间的编辑先获得发言权、拒绝编码离页保护，
以及新增的 Escape 关闭与焦点恢复（桌面回到启动按钮、手机回到可见的抽屉开关）。

## 阶段 4 侧栏与最近会话布局

证据（真实代码位置）：

- `console.js::applySidebarLaunchV2()` 只切换 `#sidebar.sidebar-launch-v2`、第二个启动按钮的可见性与
  导航顺序（`SIDEBAR_V2_VIEW_ORDER`），不触碰任何团队规则。
- `sidebarRecentLimitCount()` 在开关开启时给 5 条、关闭时保持 10 条；`_sidebarRecentViewAllAlways()`
  让标题区「查看全部」在两个状态下都可达。
- 最近会话条目统一经 `sessionTypeMarker()` 派生标识（团队/编码/置顶），`setSidebarRowType()` 与
  `setSidebarRowTitle()` 分离写入，保证原地重命名后标识仍在。

可运行测试与结果：

```
node --test tests/test_workbench_sidebar_launch_frontend.cjs
→ tests 18 / pass 18 / fail 0
.venv/bin/python -m pytest tests/test_workbench_sidebar_launch.py -q -p no:randomly
→ 通过（与阶段 2 的 48 项合并运行）
```

4.6 回归：`tests/test_session_history_frontend.cjs`、`tests/test_sidebar_session_rename_frontend.cjs`、
`tests/test_sidebar_session_archive_frontend.cjs`、`tests/test_workbench_menu_grant_frontend.cjs`、
`tests/test_nav_area_frontend.cjs`、`tests/test_console_i18n_frontend.cjs` 全部包含在上面的 152 项绿色结果中。

## 阶段 5 集成验收与发布回退

### 5.1 接口集成（真实 handler + 真实持久化）

按验收矩阵逐项执行 A3–A5，全部命中真实 WSGI 应用与 `session_prefs`，无 mock 取代服务端门槛：

| 场景 | 用例 | 结果 |
| --- | --- | --- |
| 混合 normal/coding 名册 | `test_a_coding_agent_is_refused_as_a_member_and_not_stored` | 400 `coding_web_only`，未落盘 |
| 全部 coding | `test_a_roster_that_is_coding_all_the_way_through_is_refused` | 400 `coding_web_only`，未落盘 |
| 强行提交 coding 为 owner | `test_a_coding_agent_is_refused_as_the_owner_and_not_stored` | 400 `coding_web_only`，未落盘 |
| 仅 1 位 normal | `test_one_normal_member_is_stored` / `test_the_last_member_can_be_removed_back_to_a_solo_conversation` | 200，两位下限只属创建流程 |
| 旧档案缺类型 | `test_an_omitted_type_still_creates_a_normal_agent` | 缺字段按 normal |
| 撤权 | `test_revoking_the_use_grant_refuses_the_whole_write` | 403 `forbidden`，未落盘 |
| 越租户 | `test_a_member_from_another_tenant_is_refused_and_not_stored` | 404，未落盘 |
| 他人私有 | `test_another_members_private_agent_is_refused_and_not_stored` | 403 `forbidden`，未落盘 |
| 落盘失败 | `test_a_failed_persist_answers_an_error_and_changes_nothing` | 500，值不变、运行时不退休 |

### 5.2 真实页面：团队选择界面中没有 coding

`tests/test_sidebar_launch_browser.cjs` 用生产 `chat.html`、生产 CSS/JS（只把后端响应替换为 fixture，
并按 `channel/web/core/template.py` 的方式展开 include、按 handler 的方式替换呈现开关）驱动真实 Chrome：

- 弹窗打开后焦点落在搜索框；列表恰为 3 位普通 Agent；按名称 `编码`、职责 `工程`、id `coder` 搜索均无结果，
  弹窗整段可见文本不含编码 Agent；Escape 关闭后焦点回到启动按钮。
- 输入框的邀请段不含编码 Agent，而同一次读取的切换段仍含它——单人 coding 对话没有被误伤。
- 侧栏预览里团队行读出成员名（`通用助手、研究员`）与头像，编码行读出编码图标。

结果：5 个场景、41 项断言、8 张截图、0 个页面错误。截图见
[evidence/](evidence/)（`sidebar-launch-v2-1440px.png`、`-1024px`、`-360px`、三种配色深色各一张）。

### 5.3 运行闭环（本机可验证的部分）

| 环节 | 证据 | 结果 |
| --- | --- | --- |
| 名册持久化（创建事务的结果） | `tests/test_team_roster_boundary.py`、`tests/test_team_chat_launch_frontend.cjs` | 保存在 prepared 会话上，刷新/历史按同一 owner+members 恢复 |
| 首轮运行时按完整名册重建 | `tests/test_session_team_runtime.py`（4 项，真实 `AgentBridge` 缓存） | 名册变化后下一次取用时重建，`agent_delegate` 才会进入工具集 |
| `@` 指定成员定址 | `tests/test_team_addressing.py`（8 项） | 按名称/ID 命中、重叠名最长匹配、非开头与未知名不命中 |
| composer 按保存后名册渲染 | `tests/test_composer_agents_frontend.cjs`、`tests/test_team_chat_launch_frontend.cjs` | 成员数、默认响应者、成员移除后回到单人语义 |
| 真实模型的一次多 Agent 往返 | 需要可用模型凭据 | **未验证**（见末节，不以模拟替代） |

### 5.4 桌面/窄桌面/手机与主题

同一浏览器验收在 1440、1024、360px 下断言：`#sidebar-nav` 是唯一声明的滚动容器、文档无横向溢出、
启动控件位于导航之上、底部账号区不出视口；手机抽屉打开后有关闭遮罩，关闭后遮罩消失、
侧栏回到屏外。三配色（business/slate/classic）×明暗共 6 种组合下启动控件都在侧栏内、主题按预期生效。

（阶段 5 验收时快速发起区还是两个并排按钮，故当轮的断言是「两个启动入口等宽相邻」；阶段 6 收敛为
唯一启动控件后，上述几何断言已按单一控件重写并在同一批用例中重跑通过，见 6.5。）

### 5.5 升级与回退

- 开关关闭：第二个启动入口整块不可见（`appearance.css` 曾为此增加
  `#sidebar .sidebar-new-chat.hidden { display: none }`——配色规则给了该按钮自己的 `display`，其 id 权重压过
  裸 `.hidden`，这是本次真实页面验收发现的回退缺陷并已修复；阶段 6 收敛为唯一控件后，回退只需箭头与菜单
  不出现，见 6.5）、预览回到 10 条、`#sidebar` 不带 `sidebar-launch-v2`；同一状态下 `teamCandidateAgents()`
  仍排除 coding。
- 开关开启：预览 5 条、唯一启动控件位于导航之上（同一浏览器用例对照断言）。
- 服务端拒绝与开关无关：阶段 2/5.1 的全部边界用例都在开关缺省（关闭）下运行并全绿。
- 未新增路由、未改动授权登记（`git diff --name-only` 不含 `route_registry.py` / `authorization.py` /
  `auth_handlers.py`），因此 5.6 的「对应路由覆盖检查」无新增对象。
- 未新增会话/项目/编码/账号/外观存储：本次改动不写任何新的本地存储键，也不迁移数据。

### 5.6 共享代码回归

- 前端全量：`node --test tests/*.cjs` → 888 项，878 通过，10 失败；与改动前基线失败集完全一致
  （`test_appearance_browser.cjs`、`test_personal_console_frontend.cjs` 等，已在本机 HEAD worktree 复现）。
- Python 全量：`pytest tests -q -p no:randomly` → 6320 项，32 失败。将 32 个失败节点在本机 HEAD worktree
  与改动树（`COW_DATA_DIR` 指向空目录）分别重跑，两边均为 25 failed / 7 passed 且失败节点集合相同；
  其中 `tests/test_channel_startup_open.py` 3 项在本机是**环境数据**所致（未设 `COW_DATA_DIR` 时
  `config.get_data_dir()` 回落到仓库根，读到了真实渠道实例），指向空目录后 5 项全绿。无一项由本 change 引入。

### 5.7 文档与校验

- 用户文档三语同步更新「加入会话与群组」：`docs/zh/multi-agent/team.mdx`、`docs/multi-agent/team.mdx`、
  `docs/ja/multi-agent/team.mdx` 增补侧栏启动入口、5 条预览与「查看全部/已归档」不再受限，以及
  「编码类 Agent 只做单聊、不进入任何团队选择界面（含服务端拒绝）」的规则说明；侧栏入口的最终形态
  （唯一「新建对话」控件 + 与历史页同源的折叠菜单）见阶段 6.6。
- 发布说明：仓库按版本创建 `docs/releases/vX.Y.Z.mdx`（最新 `v2.1.9`），没有未发布文件，故本次不新建版本号；
  下个版本的发文文案（含启用/回退步骤）记录于此，发布时原样搬入对应版本文件。
- 开关启用/回退：`config.json` 顶层 `workbench_sidebar_launch_v2`（`true`/`"1"`/`"yes"`/`"on"` 为启用，
  其它值一律关闭）。启用只影响侧栏布局与预览条数；回退改回 `false` 即可，团队候选过滤、服务端拒绝与
  已保存名册都不回退。
- `openspec validate refine-sidebar-team-chat-launch --strict`：通过（见下）。

### 5.8 启用结论

阶段 5 的真实页面验收与接口集成均通过，代码中的开关**仍保持默认关闭**：本次交付的是「可安全启用」的
实现与证据，目标环境的启用动作由发布流程执行。未使用占位接口或模拟数据宣称上线。

## 阶段 6 单一启动控件与会话历史页共用

### 6.1 规范修订

- `specs/workbench-sidebar-launch/spec.md`：快速发起区由「新建对话 + 多智能体对话」两个入口改为
  唯一一个「新建对话」控件（主按钮 + 右侧折叠箭头 + 选择菜单），并规定菜单选项与创建流程与会话
  历史页 `#new-chat-menu` 完全一致。
- `specs/agent-team-conversation/spec.md`：「团队启动独立于当前编码会话」改为以「可用智能体多于一位」
  为唯一条件，明确侧栏折叠菜单与历史入口都不得因当前处在编码会话而隐藏。
- `specs/session-history-workbench/spec.md`：新增「从历史页打开与新建对话」，要求两处折叠菜单同源。
- 校验：`openspec validate refine-sidebar-team-chat-launch --strict` → 通过（1 change / 3 specs / 20 requirements）。

### 6.2 共用实现（`channel/web/static/js/console.js`）

- 新增 `NEW_CHAT_SURFACES = { panel, sidebar }`：每个入口声明 `control`（主按钮 id）、`caret`、`menu`、
  `start`（主按钮行为）、`available`（该入口是否可用）。`panel.available` 恒为真，`sidebar.available` 即
  `sidebarLaunchV2()`。
- `onNewChatButton(event, surfaceName)` / `openNewChatMenu(menuId, surfaceName)` / `paintNewChatMenu` /
  `closeNewChatMenus(keep)` / `syncNewChatControls()` 全部按 surface 泛化；`paintNewChatMenu` 用同一份
  `chatAgentCatalog()` 与 `teamCandidateAgents()` 渲染，两个入口不会各自演化出不同的选项集。
- 删除 `sidebar-new-team-chat` 按钮与其点击函数 `startSidebarTeamChat`；侧栏团队的入口收敛为菜单项
  `菜单项 'multi'`，与历史页一致。
- 新增 `_newChatSurfaceOfMenu(node)`：从菜单节点反查所属 surface，供关闭菜单后把焦点还给该入口的主按钮。

### 6.3 布局与定位

- `chat.html`：`#sidebar-new-chat` 包进 `.sidebar-new-chat-wrap`（含 `#sidebar-new-chat-caret` 与
  `#sidebar-new-chat-menu`），历史页按钮补 `id="history-new-chat-btn"` 供共用逻辑与焦点恢复引用。
  该 id 特意不与输入框的加号按钮（`#new-chat-btn`）撞名：两者都仍是唯一的，否则焦点恢复会落在
  输入框而不在表头启动控件上（见 6.4）。
- `appearance.css` / `console.css`：`.sidebar-new-chat-wrap { position: relative; display: flex;
  flex-direction: column; }`。列向 flex 是必需的——首轮实现漏掉它，按钮在真实页面验收中不再撑满
  侧栏宽度（`the launch button fills the block its picker is anchored to` 失败），补上后通过。
- 折叠箭头绝对定位，不挤动居中标签；展开态菜单锚定按钮下沿；侧栏折叠（`#sidebar.collapsed`）时菜单
  从按钮右侧浮层展开；`app[data-nav-area="admin"]` 隐藏整个 `.sidebar-new-chat-wrap`，不只隐藏按钮。

### 6.4 交互细节

- 打开任一新对话菜单会先 `closeNewChatMenus(keep)` 关掉另一个，两个菜单不会同时可见。
- 侧栏在手机抽屉内打开团队选择器时先 `closeSidebar()`，避免全屏弹窗叠在抽屉上。
- 关闭菜单/选择器后焦点回到该入口的主按钮：`_teamChatTrigger` 在点击当刻取 `document.activeElement`，
  并沿途用 `_newChatSurfaceOfMenu` 找到主按钮，避免把焦点还给随菜单一起隐藏的菜单行。
- 面板与侧栏各自的启动按钮 id 互不重复（`history-new-chat-btn` / `sidebar-new-chat`），否则面板的焦点
  恢复会被重复 id 抢到输入框的加号按钮上（见 6.7）。

### 6.5 测试

- `tests/test_agent_chat_launch_frontend.cjs`：新增「侧栏启动控件是另一个位置的同一控件」「侧栏箭头跟随
  呈现开关、历史页箭头不跟随」「只剩一个可用 Agent 时两个入口都不带箭头」。
- `tests/test_team_chat_launch_frontend.cjs`：新增「从手机抽屉打开的成员选择器会收起抽屉」「焦点回到
  启动控件而不是关闭的菜单行」「会话面板把焦点还给自己的按钮而不是输入框」；原有侧栏团队用例改为走
  共用菜单。
- `tests/test_workbench_sidebar_launch_frontend.cjs`：改为断言唯一启动控件与定位钩子（开关关闭时无箭头、
  开关打开时控件在导航之上、不存在第二个团队按钮、按钮起单人对话而箭头打开共用菜单、只有一位可用
  Agent 时不带箭头、三区顺序与单滚动区、启动块与菜单各自的样式钩子、页面 id 唯一）。
- `tests/test_sidebar_launch_browser.cjs`（Playwright 真实页面）：开关打开后侧栏只有一处启动控件，箭头打开
  的菜单与历史页逐项一致且团队选项从不包含 coding Agent；按钮撑满锚定块；桌面折叠侧栏时菜单从右侧
  浮层展开且不撑宽侧栏；手机从抽屉打开选择器会收起抽屉；三配色×明暗与 360px/1024px/1440px 下无溢出、
  单滚动区；开关关闭后箭头与菜单消失但历史页团队入口仍在。
- 命令与结果：`node --test tests/test_agent_chat_launch_frontend.cjs tests/test_team_chat_launch_frontend.cjs
  tests/test_workbench_sidebar_launch_frontend.cjs tests/test_session_history_frontend.cjs
  tests/test_composer_agents_frontend.cjs tests/test_sidebar_session_archive_frontend.cjs
  tests/test_workbench_menu_grant_frontend.cjs` → 112 项全绿；
  `NODE_PATH=$(npm root -g) node tests/test_sidebar_launch_browser.cjs` → 6 个场景 / 61 项检查 /
  10 张截图 / 0 页面错误（真实页面）；`.venv/bin/python -m pytest tests/test_workbench_sidebar_launch.py
  tests/test_team_roster_boundary.py tests/test_coding_agent_type_boundary.py
  tests/test_session_team_runtime.py -q -p no:randomly` → 40 项全绿。
- 前端全量回归：`node --test tests/*.cjs` → 897 项，887 通过，10 失败；失败集与改动前基线完全一致
  （`tests/test_appearance_browser.cjs`、`tests/test_personal_console_frontend.cjs`，与 5.6 记录的
  HEAD worktree 复现结果相同），无一项由本 change 引入。
- 证据截图：`evidence/sidebar-launch-v2-1440px.png`、`-1024px.png`、`-360px.png`、
  `-classic-dark.png`、`-business-dark.png`、`-slate-dark.png`、`-picker-excludes-coding.png`、
  `-preview-five-rows.png`，浏览用例数据 `evidence/sidebar-launch-browser.json`。

### 6.6 文档与 change 产物

- 用户文档三语同步改写「加入会话与群组」的侧栏入口描述（`docs/zh/multi-agent/team.mdx`、
  `docs/multi-agent/team.mdx`、`docs/ja/multi-agent/team.mdx`）：侧栏顶部是**一个**「新建对话」控件，
  点按钮 = 单聊，点右侧箭头 = 展开与历史页相同的选择菜单（逐个 Agent 或「多智能体对话」）。
- change 产物同步：`proposal.md` 的 What Changes 与 New Capabilities、`design.md` 的目标/非目标、
  决策与示意图、`tasks.md` 的 5.4 措辞与新增阶段 6。

### 6.7 焦点归属修正（收尾发现）

- 问题：首轮实现给历史页「新对话」按钮也加了 `id="new-chat-btn"`，而该 id 属于输入框的加号按钮，
  页面出现重复 id；`_newChatSurface('panel').control` 取 `getElementById` 会命中加号按钮，从面板菜单
  打开团队票后按 Escape，焦点会落到输入框而不是表头启动控件。
- 处理：历史页按钮改用唯一 id `history-new-chat-btn`，`NEW_CHAT_SURFACES.panel.control` 指向它；
  `test_team_chat_launch_frontend.cjs` 新增「会话面板把焦点还给自己的按钮而不是输入框」，
  `test_workbench_sidebar_launch_frontend.cjs` 新增「页面 id 唯一」断言（防止重复 id 回归）。

## 尚未由本机环境验证的部分（不当作通过）

- 真实模型轮次：5.3 的「首条消息发给完整团队、`@` 指定成员、委派、旧消息保留」中，名册持久化、
  运行时按名册重建（`tests/test_session_team_runtime.py`）、`@` 定址解析（`tests/test_team_addressing.py`）
  与 composer 按保存后名册渲染均已验证；真实供应商模型的一次多 Agent 往返需要可用模型凭据，
  本机环境未提供，故不以模拟结果代替。
- 视觉人工复核：已由 8 张真实页面截图与几何/对比断言替代，未做人工目视签收。
