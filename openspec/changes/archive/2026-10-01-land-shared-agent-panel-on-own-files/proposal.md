## Why

工作区面板在 change `refine-workspace-panel-agent-root` 之后，对所有 Agent 统一落在 `agents/<agent id>`。对**私有** Agent 这是对的：整个根都是属主自己的东西。对**租户共享** Agent 则不是——共享根下放的是所有人共用的 `AGENT.md`、`knowledge/`、`memory/`、`scheduler/`、`skills/`，而本人上传和平台回传的文件在 `user/<user id>/`（change `isolate-shared-agent-user-data`）。

于是共享 Agent 的用户点开面板看到的是共享目录，自己的文件还藏在 `user/<自己的用户ID>` 下面，要手动点两层才找到「我的文件在哪」。文件面板的口径还多了一层不一致：服务端已经按 `user/<user_id>` 判归属（别人看不到、自己看得到），前端却把面板停在共享根上。

本次按用户确认的范围补齐这一步：**共享智能体的面板默认打开登录用户自己的文件目录**。

## What Changes

- 工作区面板在打开、切换 Agent、切换会话时，若当前 Agent 为**租户共享**（`agent_bindings.private_owner_user_id` 为空），SHALL 落在**调用者本人的** `agents/<agent id>/user/<当前登录用户ID>`，而不是 `agents/<agent id>`；本人用户目录只在本人之下，MUST NOT 落在他人子目录。
- 该目录由首次写入创建，从未上传过的成员没有它。面板 SHALL 在列举前请求服务端**物化本人目录**（空目录、幂等），然后停在该目录继续列举；MUST NOT 因为目录尚不存在而报错或退到共享根。
- 新增 `POST /api/workspace/user-dir`：`user_id` MUST 取自**已验证身份**，请求体只允许命名 Agent；服务端 SHALL 按既有租户绑定、私有归属、会话归属校验 `agent`，并在该 Agent 的工作区下创建 `user/<user_id>`。`user` 容器为符号链接或非目录时 SHALL 拒绝（403），MUST NOT 静默降级为公共目录。
- 工作区「使用范围」投影（`GET /api/agents?view=workbench`）SHALL 每行补充 `visibility`（`private`/`tenant`），与既有管理投影同一处派生；共享与否 MUST 由服务端给出，前端 MUST NOT 依「哪份花名册恰好列了这一行」自行推断。
- 私有 Agent、花名册未报告的 Agent、无端用户身份的部署（单用户旧布局）SHALL 保持落在 `agents/<agent id>`，行为不变。
- 既有回落链 SHALL 保留并向下延伸一层：本人目录缺失且物化失败 → 该 Agent 自己的目录 → 工作区根；列举被**权限判定拒绝**（403/404）时仍回落到调用者本人的私有 Agent，MUST NOT 把拒绝当作「目录不存在」而去创建任何东西。
- 物化至多一次、回落至多一次，MUST NOT 形成重试循环；MUST NOT 放宽任何授权：客户端自报的 Agent 标识与用户标识都不是授权依据。

## Capabilities

### New Capabilities

<!-- 无新增 capability：面板落点、可见性投影与本人目录物化同属 platform-file-browsing 的责任域 -->

### Modified Capabilities

- `platform-file-browsing`:
  - MODIFIED「工作区面板默认锚定当前 Agent 自己的目录并在无权时回落到本人私有 Agent」——把落点规则按**可见性**分成两支：共享 Agent 落在本人 `user/<user id>` 子目录，私有 Agent 保持 Agent 自己的目录；并补充「本人目录缺失时先物化」与延伸后的回落链。
  - ADDED「本人用户目录按已验证身份物化」——`POST /api/workspace/user-dir` 的身份来源、Agent 校验、容器安全拒绝与幂等语义。

## Impact

- **行为受影响**：租户共享 Agent 的工作区面板打开、切换 Agent、切换会话后落在本人文件目录；共享 Agent 的首次打开多一次物化请求（仅在缺少该目录时）。私有 Agent 与旧布局行为不变。
- **不受影响**：`GET /api/workspace/tree|search|resolve|meta|read`、`POST /api/workspace/write` 的授权与响应契约；`user/<user_id>` 归属判断本身（本次不改服务端归属规则）；预览/下载/写入的作用域机制；对话页其余入口；桌面端 `WorkspacePanel`。
- **代码面**：`channel/web/static/js/workspace.js`（`WS_USER_DIR`、`wsAgentVisibility`、`wsOwnUserDirPath`、`wsAgentLandingPath`、`wsOwnUserId`、`wsEnsureUserDir`、`showActiveAgentWorkspace`/`resetWorkspaceToAgentRoot`/`wsOnSessionSwitch` 的落点解析、`loadWorkspaceDir` 的候选链与物化重试）；`channel/web/fork/handlers/workspace.py`（新 `WorkspaceUserDirHandler`、`_ensure_own_user_dir`）；`channel/web/fork/handlers/agents.py`（`_agent_visibility` 抽取，工作台投影新增 `visibility`）；`channel/web/route_registry.py` 与 `channel/web/web_channel.py` 的路由登记。无新增配置项、无新增前端依赖、无数据库变更。
- **测试面**：`tests/test_console_workspace_frontend.cjs`（共享 Agent 落本人目录、私有/未报告不落、缺失时物化一次并停留、物化失败回落 Agent 目录、拒绝仍回落本人私有 Agent）；`tests/test_workspace_user_dir.py`（新增：物化身份来源、幂等、无端用户不建、按被命名 Agent 而非环境 Agent、容器不安全拒绝、路由级成功/无用户/缺 Agent）；`tests/test_agent_visibility_toggle.py`（使用范围投影的两态与转换后跟随）；`tests/test_agent_workbench.py`（工作台投影白名单加入 `visibility`）。
- **i18n 面**：无新增文案。落点变化由面包屑（`agents / <id> / user / <usr_…>`）自明，不引入需要翻译的提示。
