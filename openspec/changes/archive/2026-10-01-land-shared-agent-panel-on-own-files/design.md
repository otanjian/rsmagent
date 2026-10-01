## Context

`refine-workspace-panel-agent-root` 结束了面板「记得上次浏览」的行为，统一落在当前 Agent 自己的目录 `agents/<agent id>`，并在被拒时回落到调用者本人的私有 Agent。`isolate-shared-agent-user-data` 之后，租户共享 Agent 的文件被分成两层：根下是所有人共用的 `AGENT.md`、`knowledge/`、`memory/`、`scheduler/`、`skills/`，本人上传与回传结果在 `user/<user id>/`，服务端按 `user/<user_id>` 判归属。

两条线各自成立，合起来就出现空档：前端仍按「Agent 自己的目录」落，共享 Agent 的用户因此停在共享根上，自己的文件还要手动点进 `user/<自己的用户ID>`。本次只补落点，不动归属规则。

## Goals / Non-Goals

**Goals**

- 共享 Agent 的面板默认打开登录用户自己的文件目录（`agents/<id>/user/<user_id>`）。
- 该目录从未被创建过时，也能打开并停在那里（服务端按已验证身份物化空目录）。
- 私有 Agent、旧单用户布局、既有回落链的行为不变；不放宽任何授权。

**Non-Goals**

- 不改 `user/<user_id>` 的归属判断、下载/预览/写入的作用域规则（`isolate-shared-agent-user-data` 的责任域）。
- 不新增文件操作、不新增权限点、不新增能力开关。
- 不改执行层（Python/Shell/技能脚本/coding）的任何边界。

## Decisions

### D1 用服务端的 `visibility` 判定「共享」，不在前端推断

面板要按可见性分两支落点，就必须知道当前 Agent 是共享还是私有。可选做法有三种：

1. 前端拿 `GET /api/agents?view=personal`（本人私有列表）做**排除法**：不在里面即视为共享。
2. 前端读管理花名册（`GET /api/agents`）里既有的 `visibility` 字段。
3. 由**使用范围**投影（`GET /api/agents?view=workbench`，即对话页实际聊天用的那份）直接给出 `visibility`。

选 3。理由：

- 排除法把「我没有它」当成「它共享」，是推断而非事实；一个尚未出现在私有列表里的对象会被误判。授权事实不能由客户端拼。
- 管理花名册的**范围**对普通成员只含其本人私有 Agent（见 `_tenant_agents_admin_projection` 与 ``user-private-agent-management``），正在聊的共享 Agent 根本不在其中，选 2 对最需要它的用户恰好失效。
- 使用范围正是「我能聊哪些 Agent」，共享 Agent 一定在其中；把 `visibility` 加进这份白名单投影，前端只是复述服务端的结论。

服务端把派生收进一个函数 `_agent_visibility(ctx, agent_id)`（`private_owner_user_id` 为空即共享，沿用既有口径），管理投影与工作台投影同读一处，`can_share`/`can_unshare` 继续用同一行 binding，不重复取。

前端保留「未知即不共享」：花名册没有该行、或该行没有该字段时返回 `''`，落点退回 Agent 自己的目录。旧后端（无 `visibility`）因此维持既有行为，而不是被当成共享。

### D2 本人目录由显式写入口物化，而不是让读取产生副作用

`user/<user_id>` 由首次写入创建（`agent_user_uploads_dir(..., ensure=True)` 等），从未上传过的成员没有这个目录。面板要「停在那里」，就得先让它存在。可选做法：

1. 在 `GET /api/workspace/tree` 里顺手创建（读请求产生副作用）。
2. 面板调用既有 `POST /api/workspace/write` 写一个占位文件。
3. 新增一个语义单一的写入口 `POST /api/workspace/user-dir`。

选 3。

- 选 1 让一个纯粹的读请求改文件系统：授权模型上「谁能读」与「谁会建目录」就绑在一起，且它会在任何调用方（含未来别的调用者）下悄悄生效。本仓库的读路径（`list_dir`、`search`、`resolve`）均无副作用，不应由本次首次引入。
- 选 2 会留下一个用户看得见的占位文件，属于用数据模型表达控制流。
- 选 3 与既有写入口同构：走 `_db_scope()`、统一 origin/CSRF 门（`require_management_write`）、同一套 `agent` 校验（`_workspace_request_scope`：租户绑定 + 私有归属 + 会话归属），并登记在 `route_registry.py`（路由覆盖率闸门覆盖）。

身份来源是这条路由的关键：`user_id` 取自 `current_identity()`，**请求体只能命名 Agent**。因此它无法寻址他人子目录——即使客户端伪造也只会建自己的目录。Agent 的工作区由 registry 解析（与 `_get_upload_dir` 同一写法），而不是从会话的 panel root 推导：会话打开了项目目录时，panel root 是那个项目，若据它推导就会把目录建到项目里去。

`user` 容器为符号链接或非目录时按 `StateDirError` 拒绝（403, `unsafe_user_directory`），与既有上传路径一致——不静默降级为公共目录，否则一次坏身份会变成跨用户写入。

### D3 落点解析要等本人用户 ID，但不阻塞面板

`wsAgentLandingPath()` 是同步函数，被落点判定（`loadWorkspaceDir` 里的 `landing` 比较）与三处作用域重置复用。本人用户 ID 需要一次 `/auth/me`（与账号菜单同一投影，含 `usr_*`）。

做法：保留同步的 `wsAgentLandingPath()`，其输入是**已缓存**的 `wsOwnUserIdCache`；由 `wsOwnUserId()` 负责取一次并缓存（正向、负向都缓存——'' 是「本部署没有端用户身份」这个事实，不是失败）。三处作用域重置（`showActiveAgentWorkspace`、`resetWorkspaceToAgentRoot`、`wsOnSessionSwitch`）先 `await` 这一次读取，再算落点、再列举；期间若作用域又被切换，按既有 `wsScopeEpoch` 丢弃这次到达（落到别人的面板上才是真问题）。

取舍：面板打开比原来晚一个 microtask（缓存命中时）到一次网络往返（首次）。换来的是落点在所有路径（入口、切 Agent、切会话）一致，不会出现「先落在共享根、用户 ID 到了之后再跳一次」。

### D4 回落链向下延伸一层，且区分「缺失」与「拒绝」

原候选链：`[落点, '']`（落点不存在 → 工作区根）。共享 Agent 的落点变成 `agents/<id>/user/<uid>` 后，链变为 `[本人目录, agents/<id>, '']`。

关键区分：只有**缺失**（非 403/404）才尝试物化并重试；**拒绝**仍走既有回落（本人私有 Agent），且 MUST NOT 触发任何创建。混为一谈会导致：一个被拒的 Agent 让面板去建目录，或在别人看不到的路径上留下痕迹。

物化只对落点本身重试一次；候选链后面的元素是**另一个答案**，不是同一问题的重试。

### D5 共享 Agent 的面板落点也受「是不是当前可见」的兜底保护

共享 Agent 的落点只有在工作区根确实含有 `agents/<agent id>` 时才可达。会话打开了项目目录、或旧布局下工作区根即 Agent 目录时，落点列不出来。此时按 D4 的链回落到 `agents/<id>`（同样列不出来）再回落到工作区根——与私有 Agent 的既有行为一致，不新增特例。

副作用权衡：若会话打开了项目目录且项目里没有该 Agent，面板仍会向服务端请求物化（服务端建在**该 Agent 真实工作区**的本人目录里，不在项目里）。结果是一个空目录被建好、面板回落到工作区根。这是幂等且只影响调用者本人的，接受；MUST NOT 改成「先探测 `agents/<id>` 再决定是否物化」——那会把一次落点变成三次请求，并让物化依赖一个与物化目标不同的路径。

## Risks / Trade-offs

- **共享 Agent 首次打开多一次请求**：仅在该目录不存在时发生一次；目录建好之后与私有 Agent 相同（一次列举）。物化失败则回落（每面板打开仍只试一次）。
- **旧后端（无 `visibility`）**：前端返回 `''` → 维持既有落点，行为不劣化；新后端 + 旧前端同样不受影响（多出的字段被忽略）。
- **可见性与落点的一致性依赖单一派生**：`_agent_visibility` 同时供管理投影与工作台投影使用，避免「徽标说是共享、落点按私有走」这类漂移。
- **`/auth/me` 不可用（legacy 无登录模式）**：缓存 '' → 落点回到 Agent 自己的目录，正是该模式的既有行为。

## Migration Plan

无数据迁移。落点是读侧行为；本人目录由服务端在首次打开时创建（空目录、幂等），无需回填。回滚只需撤下本次前端落点分支与新增路由，已创建的空目录无副作用。

## Open Questions

- 面板是否需要在共享 Agent 下显式提示「这是你的文件目录」？（本次判定不需要：面包屑已显示 `agents/<id>/user/<usr_…>`，再加提示属于冗余。若后续产品要求，按 i18n 三语键补齐。）
