# Tasks

## 1. 落点：当前 Agent 自己的目录（`agents/<agent id>`）

- [x] 1.1 `channel/web/static/js/workspace.js`：新增 `WS_AGENT_DIR`、`wsScopedAgentId()`、`wsAgentDirPath(id)`、`wsAgentLandingPath()`，并让 `wsApi()` 改用 `wsScopedAgentId()`
- [x] 1.2 新增 `showActiveAgentWorkspace()`——先清 `wsAgentOverride` 再取落点路径（否则会把上一个回落的 Agent 当成当前 Agent），置 `wsCurrentDir` 为落点路径，并在文件列表非空时清空列表（使 `switchWorkspaceTab` 的「列表为空才列举」判据必然成立），随后切到「文件」页签
- [x] 1.3 `toggleWorkspacePanel()` 改为 `openWorkspacePanel(); showActiveAgentWorkspace();`，不再因 `wsCurrentFile` 落在预览页签
- [x] 1.4 `resetWorkspaceToAgentRoot()`、`wsOnSessionSwitch()` 同样先清回落作用域、再置落点路径并重列

## 2. 落点目录缺失时回落工作区根

- [x] 2.1 `loadWorkspaceDir()`：候选路径 = 落点路径 + 工作区根，且只在本次调用就是落点时提供第二条（`relPath === wsAgentLandingPath()`）
- [x] 2.2 「不存在」判据为**非 403/404**（`list_dir` 的 `FileNotFoundError` 由 handler 以 HTTP 200 + `status:error` 返回）；用户点进去的路径不在候选内，照常报错

## 3. 无权时回落到本人私有 Agent 的目录

- [x] 3.1 `wsApi()`：`wsAgentOverride` 非空时优先作为 `/api/workspace/*` 的 `agent` 参数
- [x] 3.2 新增 `wsIsRefusal(e)`（403/404）与 `wsShouldFallBackToOwnAgent(e)`（未回落且为 403/404）
- [x] 3.3 新增 `wsOwnPrivateAgentId()`：读 `GET /api/agents?view=personal`，优先该成员默认 Agent（在私有列表内时），否则取首个；只记肯定答案
- [x] 3.4 新增 `wsFallBackToOwnAgent()`：设置回落作用域并提示 `ws_fallback_own_agent`
- [x] 3.5 `loadWorkspaceDir()`：拒绝时回落一次并**按回落后的 Agent 重新取路径**（本人私有 Agent 的目录是另一个）；局部 `fallbackTried` 保证一次落点最多探询一次
- [x] 3.6 落点被拒且无可回落时渲染 `ws_agent_forbidden`，非落点失败仍走既有 `wsErrorMessage()`

## 4. 文案与快照

- [x] 4.1 `channel/web/static/js/i18n/core.js`：新增 `ws_fallback_own_agent`、`ws_agent_forbidden`（zh / zh-Hant / en 三语）
- [x] 4.2 `tests/fixtures/console_i18n_snapshot.json`：同步三语取值

## 5. 测试

- [x] 5.1 `tests/test_console_workspace_frontend.cjs`：`makeCtx` 支持按 URL 子串分派响应，元素桩让 `childElementCount` 与 `innerHTML` 同步，新增 `peek`/`poke`/`flush`/`withAgent` 辅助
- [x] 5.2 新增「入口打开落在当前 Agent 自己的目录（`agents/<id>`）、只发一次列举、丢弃上次子目录与预览」
- [x] 5.3 新增「工作区根没有该 Agent 目录时落到根」「用户点进去的路径不存在时照常报错」
- [x] 5.4 新增「拒绝→探询本人私有 Agent→按本人 Agent 的目录重列并保持作用域」
- [x] 5.5 新增「回落只发生一次（只探询一次）」
- [x] 5.6 新增「无私有 Agent 时给出本地化无权提示且不呈现原始拒绝串」
- [x] 5.7 新增「切换 Agent / 切换会话丢弃回落并重新落到新 Agent 目录」
- [x] 5.8 `tests/test_console_i18n_parity.cjs`、`tests/test_console_i18n_coverage.cjs` 通过

## 6. 验收

- [x] 6.1 `node --test tests/test_console_workspace_frontend.cjs tests/test_console_i18n_parity.cjs tests/test_console_i18n_coverage.cjs` 全绿
- [x] 6.2 真实实例（`localhost:9899`，租户 `test15`，Agent `my-assistant-admin-test15`）：点入口只发一次 `GET /api/workspace/tree?path=agents%2Fmy-assistant-admin-test15&session=…&agent=my-assistant-admin-test15`，面包屑为 `agents/my-assistant-admin-test15`，「文件」页签显示 `knowledge`、`memory`、`scheduler`、`tmp`、`MEMORY.md`、`RULE.md`、`AGENT.md`、`USER.md`
- [x] 6.3 真实实例：把当前 Agent 换成一个本租户不绑定的标识（`no-such-agent-xyz`）后点入口，请求序列为「落点目录 → `/api/agents?view=personal`（该管理员无私有 Agent）→ 工作区根」，面板显示本地化 `ws_agent_forbidden` 而非服务端原始串
- [ ] 6.4 真实实例：以**拥有本人私有 Agent** 的成员身份，对他人私有 Agent 的会话点入口，确认回落到 `agents/<我的私有智能体>` 并出现切换提示（需成员账号，未实测；本机 `GET /api/agents?view=personal` 对租户管理员返回空列表，见 `evidence/verification.md` §3）
