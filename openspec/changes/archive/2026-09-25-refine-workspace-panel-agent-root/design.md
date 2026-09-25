## Context

工作区面板的前端状态与请求作用域今天是这样协作的（改动前的实际代码）：

- `channel/web/static/js/workspace.js`
  - `toggleWorkspacePanel()` → `openWorkspacePanel(wsCurrentFile ? 'preview' : 'files')`：预览过文件就落在「预览」页签。
  - `switchWorkspaceTab('files')` → 仅当 `#ws-file-list` 为空时才 `loadWorkspaceDir(wsCurrentDir)`：非空则沿用上一次的行与目录。
  - `wsApi()` 给 `/api/workspace/*` 追加 `session=<sessionId>&agent=<activeAgentId>`，`loadWorkspaceDir('')` 请求的是**工作区根**。
  - `loadWorkspaceDir()` 失败时只渲染 `wsErrorMessage(e)`。
- 服务端：`channel/web/fork/runtime.py::_get_workspace_root()` 先看会话已打开的项目目录，再看 `channel/web/tenant_workspace.py::resolve_tenant_workspace_root()`；在 database 身份模式下后者直接返回**调用者租户的共享根**，因此面板的文件根与 Agent 无关。每个 Agent 自己的目录是 `<共享根>/agents/<agent id>`（`channel/web/fork/handlers/agents.py::_tenant_agent_workspace`、`agent/private_agent.py::_plan` 都按这个布局解析）。
- `channel/web/fork/handlers/workspace.py` 的 `_workspace_request_scope(ctx, session, agent)` 对 Agent 做 `_require_tenant_agent_binding` + `_require_private_owner` + `_require_owned_session`，拒绝时抛 `web.HTTPError` 403/404；`_workspace_service(session, agent)` 的根为「会话已打开的项目目录」优先，否则租户共享根。

因此「默认打开对应智能体所有的文件夹」＝把落点从工作区根挪到 `agents/<agent id>`；「没有权限则打开自己的私有智能体文件夹所在目录」＝把作用域换成 `agents/<我的私有智能体>`。

## Goals / Non-Goals

- Goals：入口打开必然显示当前 Agent 自己的目录；工作区根没有该目录时落到根而不是报错；当前 Agent 无权读取时显示本人私有 Agent 的目录并说明；回落不形成循环、不放宽授权。
- Non-Goals：不改 `/api/workspace/*` 的授权与契约（不加 `scope=agent` 之类的参数）；不改 `_get_workspace_root()` 的会话工作目录语义（`@` 引用、bash cwd、预览仍按它工作）；不为回落新增后端路由；不做「递归展开整棵树」（面板仍是逐级浏览）。

## Decisions

### D1：落点由前端表达为「工作区根下的相对路径」，而非改服务端根解析

`_get_workspace_root()` 的「项目目录优先、否则 Agent workspace」是会话级工作目录的既定语义，改它会把已打开项目的会话从项目根挪走，也会改变 bash/相对路径的落点。面板要的是「Agent 自己的目录」，而它在工作区根之下、是可以直接寻址的相对路径，所以在 `toggleWorkspacePanel()` 里做：

```
function toggleWorkspacePanel() {
    if (wsPanelOpen) { closeWorkspacePanel(true); return; }
    wsAutoOpenSuppressed = false;
    openWorkspacePanel();
    showActiveAgentWorkspace();
}
```

- `wsAgentDirPath(id)` = `agents/<id>`；`wsAgentLandingPath()` 读当前作用域 Agent（`wsAgentOverride || activeAgentId`）。
- `showActiveAgentWorkspace()` 先清 `wsAgentOverride`（否则会把上一个回落的 Agent 当成当前 Agent），再把 `wsCurrentDir` 置为落点路径，并在列表非空时清空列表——`switchWorkspaceTab('files')` 的「列表为空才列举」判据因此必然成立，一次请求、一个目录。
- `resetWorkspaceToAgentRoot()`、`wsOnSessionSwitch()` 同样把 `wsCurrentDir` 置为落点路径并先清回落作用域。
- 取舍：没有把 `switchWorkspaceTab()` 的「空才列举」判据改成「每次都列举」，因为 `openInPreview()` 对目录会 `openWorkspacePanel('files')` + `switchWorkspaceTab('files')` + `loadWorkspaceDir(meta.path)`，改成每次列举会凭空多一次请求；保留判据、由落点入口负责归零成本更低。

### D2：落点目录可能不存在——只有落点路径享有「回落工作区根」

面板打开的落点是**会话相关**的：会话已打开项目目录时工作区根是那个项目，它下面不会有 `agents/<id>`；单 Agent 旧布局下工作区根本身就是 Agent 目录，同理。这不是错误，而不是目录才是。

- `loadWorkspaceDir(relPath)` 把候选路径表达为「落点路径 + 工作区根」两条，只在 `relPath === wsAgentLandingPath()`（即本次是落点）时提供第二条；用户自己点进去的路径不在候选里，因此照常报错。
- 「不存在」的判据是**非 403/404**：`WorkspaceService.list_dir()` 对不存在的路径抛 `FileNotFoundError`，handler 以 HTTP 200 + `status:error` 返回，`wsApi` 记下的 `err.status` 是 200。
- 取舍：没有先列举工作区根再判断 `agents/<id>` 是否存在（那会让常见情形恒为两次请求），而是直接请求落点目录、失败即换根。

### D3：回落作用域放在前端，用既有 `/api/agents?view=personal` 解析「我的私有智能体」

服务端要**静默替换**根目录，就得在只读接口里改写请求语义（并在 search/resolve/read/write 上保持一致），这与 `platform-file-browsing` 既有的「MUST NOT 因客户端自报标识扩大范围、归属必须按实际资源归属判定」口径容易冲突。前端方案把回落表达为**换一个合法作用域**（本人私有 Agent），服务端仍按同一套归属规则判定：

- `wsOwnPrivateAgentId()` 读 `GET /api/agents?view=personal`（服务端已按 `private_agent_ids(tenant, user)` 过滤），优先取该成员默认 Agent（在其私有列表内时），否则取第一个；
- `wsFallBackToOwnAgent()` 把它记进 `wsAgentOverride`，`wsApi()` 优先用该值作为 `agent` 参数——面板后续的预览/读取/写入随之落在同一作用域；
- 回落后的重试按 `wsAgentLandingPath()` 重新取路径（本人私有 Agent 的目录是另一个），而不是复用被拒的那个；
- 判定只看 **HTTP 403/404**（`_workspace_request_scope` 的拒绝）。`/api/workspace/*` 的「路径不存在」等业务失败由 handler 以 HTTP 200 + `status:error` 返回，`wsApi` 记下的 `err.status` 为 200，因此不会误触发回落。

- 取舍：`/api/agents?view=personal` 需要 `agent.read`（内置 `member` 默认持有）。没有该权限的自定义角色会回落失败，行为回退为本地化无权提示——可接受，因为这不是新增的拒绝路径。

### D4：回落只发生一次，且不跨 Agent / 会话存活

- `wsShouldFallBackToOwnAgent(e)` 在 `wsAgentOverride` 已设置时返回 false，因此第二次拒绝只会渲染提示，不会循环。
- 一次落点（两个候选路径）最多探询一次：`loadWorkspaceDir()` 用局部 `fallbackTried` 记账，第二个候选遇到同一拒绝时不再重问 `/api/agents?view=personal`。
- 「本人私有 Agent」是**调用者属性**（不随 Agent 切换变化），所以 `wsOwnAgentId` 记住肯定答案、不记住空答案（成员可能稍后创建自己的私有 Agent）。而「回落作用域」是**会话/Agent 属性**，在 `showActiveAgentWorkspace()`、`resetWorkspaceToAgentRoot()`、`wsOnSessionSwitch()` 三处清空。

### D5：提示而非静默，且不把服务端原始拒绝串当作结果

- 目录换成另一个 Agent 后，面包屑是唯一的线索，容易读成「面板坏了」。回落成功时复用既有 `_wsToast` 提示一次（`ws_fallback_own_agent` 三语），不新增组件。
- 落点被拒又无可回落时，渲染 `ws_agent_forbidden`（「没有权限打开该智能体的目录」三语）：服务端对同一情形回的是 `agent not found`（404）这类面向接口 roster 的串，读起来既不是本地化文案、也不指向「目录」这件事。非落点的失败仍走既有 `wsErrorMessage()`，因此文件级 404/403 的既有文案不变。

## Migration / Rollback

无数据迁移。回滚只需还原 `workspace.js` 的落点与回落逻辑、`i18n/core.js` 的新键（以及同步的快照 fixture）；不涉及服务端，因此回滚即回到「记得上次预览 + 工作区根」的旧行为。

## Open Questions

- 无本人私有 Agent 的租户管理员是否应改由服务端派生一个「本人目录」作为回落目标？当前不做：`platform-file-browsing` 未定义此类目录，先以本地化提示收口。
