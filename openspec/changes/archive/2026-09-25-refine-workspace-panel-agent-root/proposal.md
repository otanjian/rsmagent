## Why

对话页顶栏的工作区入口（`#workspace-toggle-btn` → `toggleWorkspacePanel()`）今天打开的是**上一次留下的状态**，而不是当前 Agent 的目录：

- `toggleWorkspacePanel()` 走的是 `openWorkspacePanel(wsCurrentFile ? 'preview' : 'files')`——只要此前预览过任何文件，点入口落到的是「预览」页签，用户看不到任何文件夹；
- 即使落到「文件」页签，`switchWorkspaceTab('files')` 只在文件列表为空时才重新列举，因此**上一次浏览的子目录**会继续显示，看起来像是当前 Agent 的目录；
- 面板的文件根是**工作区根**（`channel/web/fork/runtime.py::_get_workspace_root` → `channel/web/tenant_workspace.py::resolve_tenant_workspace_root` 返回租户共享根），而每个 Agent 自己的东西（`AGENT.md`、`knowledge/`、`memory/`、`outputs/`、`scheduler/`、`skills/`）在 `<共享根>/agents/<agent id>` 下。于是点开入口看到的是 `agents/`、`users/`、`websites/` 这些**租户共享层**，要找到「我在跟谁干活、它的文件在哪」还得手动点进 `agents/<id>`；
- 面板以 `agent=<activeAgentId>` 请求目录（`channel/web/api/workspace.py` + `channel/web/fork/handlers/workspace.py` 的 `_workspace_request_scope`），当该 Agent 属于他人私有、未绑定本租户或是别的会话时，服务端返回 403/404，前端只渲染一条原始错误串（如 `agent not found`），入口变成死路。

工作区入口是「我当前在跟谁干活、它的文件在哪」的唯一入口。它落在租户共享根、显示别人的目录、或者干脆什么都不显示，都会让人误判自己看到的是谁的文件夹。

## What Changes

- 顶栏工作区入口 SHALL 在打开时锚定**当前会话对应 Agent 自己的目录**（工作区根下的 `agents/<agent id>`，即该 Agent 的 `AGENT.md`、`knowledge/`、`memory/`、`outputs/`、`scheduler/`、`skills/`）：丢弃上次浏览的子目录与上次的回落作用域，置为「文件」页签并重新列举该目录，MUST NOT 因上次预览过文件而停在预览页签。
- 该目录不存在时（会话已打开项目目录、或单 Agent 旧布局下工作区根就是 Agent 目录）SHALL 落到工作区根本身，MUST NOT 报「不是目录」；用户自己点进去的路径不存在时 SHALL 照常报错。
- 该目录的列举被**权限判定拒绝**（403/404：他人私有 Agent、跨租户 Agent、未绑定当前租户、非本人会话）时，面板 SHALL 回落到**调用者本人的私有 Agent 自己的目录**（`agents/<我的私有智能体>`），并以该作用域继续面板的预览/读取/写入，同时给出「已切换到我的智能体目录」的提示。
- 回落 SHALL 至多一次，MUST NOT 形成重试循环；没有本人私有 Agent、或本人私有 Agent 的目录同样被拒时，SHALL 给出本地化的「没有权限打开该智能体的目录」，MUST NOT 把服务端的原始拒绝串当作结果，MUST NOT 回落到他人目录。
- 回落 MUST NOT 放宽授权：作用域仍由服务端按实际资源归属判定，客户端自报的 Agent 标识不构成授权依据。
- 切换 Agent、切换会话、重新打开入口时 SHALL 丢弃上一次的回落作用域，按当前 Agent 重新判定并重新落到它自己的目录。
- 影响范围为 fork 侧前端模块 `channel/web/static/js/workspace.js` 与三语文案 `channel/web/static/js/i18n/core.js`；MUST NOT 改动 `/api/workspace/*` 的授权策略、路由表或响应契约。

## Capabilities

### New Capabilities
<!-- 无新增 capability：面板的落点与回落同属 platform-file-browsing 的责任域 -->

### Modified Capabilities
- `platform-file-browsing`: 新增一条 requirement，规定控制台工作区面板打开时落在当前 Agent 自己的目录、以及权限拒绝时回落到本人私有 Agent 目录的行为与边界（目录缺失回落工作区根、回落至多一次、不放宽授权、不跨 Agent/会话存活、无私有 Agent 时给出本地化无权提示）。

## Impact

- **行为受影响**：工作区入口每次打开都落到当前 Agent 自己的目录（「文件」页签），并可通过面包屑回到工作区根；此前「记得上次预览」的隐式行为被移除；无权访问当前 Agent 目录时不再停在原始错误串，而是显示本人私有 Agent 的目录并提示已切换。
- **不受影响**：`GET /api/workspace/tree|search|resolve|meta|read`、`POST /api/workspace/write` 的授权与响应契约、`channel/web/fork/handlers/workspace.py`、项目（工作空间）选择与 `openInPreview` 的自动预览、对话页其余入口、桌面端 `WorkspacePanel`。
- **代码面**：`channel/web/static/js/workspace.js`（`WS_AGENT_DIR` / `wsScopedAgentId` / `wsAgentDirPath` / `wsAgentLandingPath`；`toggleWorkspacePanel` 与新增 `showActiveAgentWorkspace`；`loadWorkspaceDir` 的落点与回落；`wsShouldFallBackToOwnAgent` / `wsIsRefusal` / `wsOwnPrivateAgentId` / `wsFallBackToOwnAgent`；`resetWorkspaceToAgentRoot` / `wsOnSessionSwitch`）；`channel/web/static/js/i18n/core.js`（新增键 `ws_fallback_own_agent`、`ws_agent_forbidden` 三语）。
- **测试面**：`tests/test_console_workspace_frontend.cjs` 钉住「入口打开落在当前 Agent 自己的目录且只发一次列举」「工作区根没有该 Agent 目录时落到根」「用户点进去的路径不存在时照常报错」「拒绝→探询本人私有 Agent→按本人 Agent 的目录重列」「回落只发生一次」「无私有 Agent 给出本地化无权提示」「切换 Agent / 切换会话丢弃回落并重新落到新 Agent 目录」；`tests/test_console_i18n_parity.cjs` 与 `tests/test_console_i18n_coverage.cjs` 覆盖新键，`tests/fixtures/console_i18n_snapshot.json` 同步。
- **不新增**：无新接口、新路由、新配置项、新能力开关；不新增前端依赖。
