## 1. 落点判定的事实来源

- [x] 1.1 盘点面板落点的既有判定与复用点：`wsAgentLandingPath`、`showActiveAgentWorkspace`、`resetWorkspaceToAgentRoot`、`wsOnSessionSwitch`、`loadWorkspaceDir` 的候选链与回落；确认服务端已有 `_agent_visibility` 口径（`agent_bindings.private_owner_user_id` 为空即租户共享）与使用范围投影的现状。
- [x] 1.2 确认前端无法从既有投影得到「共享/私有」：使用范围投影不含可见性字段，管理花名册对普通成员只含其本人私有 Agent；据此确定把可见性加入使用范围投影，而不是在前端用排除法推断。

## 2. 服务端：可见性与本人目录物化

进入本阶段前复核阶段 1 的投影范围结论。

- [x] 2.1 抽取 `_agent_visibility`，使管理投影既有 `visibility`、`can_share`/`can_unshare` 与工作台投影同读一处派生，不重复取 binding。
- [x] 2.2 在 `_tenant_agents_projection` 每行补 `visibility`（`private`/`tenant`），字段进入工作台投影白名单且仍是白名单式投影。
- [x] 2.3 新增 `_ensure_own_user_dir(agent_id)`：`user_id` 取自 `current_identity()`，`agent` 经 registry 解析，目录建在该 Agent 真实工作区下；幂等，已存在不改内容。
- [x] 2.4 新增 `WorkspaceUserDirHandler`（`POST /api/workspace/user-dir`）：走 `_db_scope()` 与统一 origin/CSRF 门，复用 `_workspace_request_scope` 的 Agent 校验，无端用户身份与 `user` 容器不安全时以 403 拒绝（不降级为公共目录）。
- [x] 2.5 在 `route_registry.py` 登记路由（`POST`，tenant 范围）并在 `web_channel.py` 重导出 handler，使路由覆盖率闸门覆盖新入口。
- [x] 2.6 后端用例：物化身份只来自认证上下文（请求体他人用户标识不被采纳）、重复请求不改动、无端用户不建目录、按被命名 Agent 而非环境 Agent 建、`user` 容器不安全拒绝、路由级成功/无用户/缺 Agent。

## 3. 前端：共享 Agent 落在本人目录

进入本阶段前复核阶段 2 的物化语义与拒绝码。

- [x] 3.1 增加 `WS_USER_DIR`、`wsAgentVisibility`（未知即 `''`）、`wsOwnUserDirPath`、`wsOwnUserId`（一次 `/auth/me`，正负结果都缓存）与 `wsEnsureUserDir`。
- [x] 3.2 `wsAgentLandingPath` 按可见性分两支：共享 → 本人 `user/<uid>`；私有/未知/无端用户 → `agents/<id>`。
- [x] 3.3 三处作用域重置（入口打开、回到 Agent 根、切换会话）先取本人用户 ID 再算落点、再列举，并在期间切换时按既有 `wsScopeEpoch` 丢弃迟到结果。
- [x] 3.4 `loadWorkspaceDir` 候选链在共享 Agent 下变为 `[本人目录, agents/<id>, '']`；仅当失败**不是**权限判定拒绝时物化一次并重试一次，拒绝仍走既有回落且不触发任何创建，保留无落点时的本地化提示。
- [x] 3.5 前端用例：共享 Agent 落本人目录、私有与未报告 Agent 不落、缺失时物化一次并停留、物化失败回落该 Agent 目录、拒绝仍回落本人私有 Agent（以 `/api/workspace/` 请求序列断言，避免被 `/auth/me` 干扰）。
- [x] 3.6 真实 Wire 用例：在 `tests/test_agent_user_file_http.py` 的真人会话夹具上跑通面板的落位序列——使用范围投影报告 `visibility`、物化后列举本人空目录、请求体不能选他人、二次物化幂等、不可寻址的 Agent 被拒且不创建任何目录（单测只覆盖一半，路由策略/身份/租户头/会话 cookie/handler 作用域这一串只有真实应用能回答）。

## 4. 回归与交付

- [x] 4.1 运行 `tests/test_console_workspace_frontend.cjs`、`tests/test_workspace_user_dir.py`、`tests/test_agent_visibility_toggle.py`、`tests/test_agent_workbench.py`、`tests/test_route_registry.py` 并记录输出。
- [x] 4.2 确认工作台投影白名单用例已随新增字段更新（否则既有断言会以「多余字段」失败），且未放宽白名单以外字段。
- [x] 4.3 重启后端并做真实 HTTP 验证：新路由随代码上线（重启前 404 → 重启后 400 且先撞租户门）、面板加载的脚本与磁盘文件逐字节一致；另在真实 Wire（`WebAppHarness`：真会话/真租户/真路由/真策略）上验证共享 Agent 的落位序列（投影报告 `visibility`、物化后列举本人目录、请求体不能选他人、二次物化幂等、不可寻址的 Agent 被拒且不创建任何目录）。
- [ ] 4.4 浏览器端多人实测（A 与 B 在同一共享 Agent 下各自落在自己目录）：自动化浏览器与后端不在同一网络命名空间（`127.0.0.1:9900` 落到 `chrome-error://`），公网入口停在登录页且无可用账号口令，故此条**未执行**；等价断言由 4.3 的真实 Wire 用例承担（见 `evidence.md` 第 7 节）。
- [x] 4.5 执行 OpenSpec 校验：本机无 Windows 版 CLI，改用 `scripts/check_change_deltas.py land-shared-agent-panel-on-own-files`（无 delta 问题；报出的三条为仓库级 seam 覆盖约定，对任一未归档 change 同样报出）+ 独立结构校验（名称命中基线、每条要求均有 Scenario）；核对与 `isolate-shared-agent-user-data`、`refine-workspace-panel-agent-root` 的合并语义，确认未扩大范围（不改归属规则、不改执行层边界）。

## 验收记录

用例输出、真实 HTTP 与浏览器验证、与既有 change 的合并语义核对见 [`evidence.md`](./evidence.md)。

两条边界在此重申，避免把勾选读成超出证据的承诺：

- **归属规则不在本次范围**：`user/<user_id>` 的归属判断、预览/下载/写入的作用域与执行层边界仍由 `isolate-shared-agent-user-data` 定义，本次只改面板落点与新增一个幂等的本人目录物化入口。
- **可见性来源单一**：前端落点完全依赖服务端投影的 `visibility`；未知即按私有处理，MUST NOT 在前端推断共享与否。
