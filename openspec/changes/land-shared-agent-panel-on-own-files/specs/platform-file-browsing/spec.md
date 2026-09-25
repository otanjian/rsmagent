## ADDED Requirements

### Requirement: 本人用户目录按已验证身份物化

平台 SHALL 提供 `POST /api/workspace/user-dir`，供工作区面板在打开共享 Agent 时确保调用者本人的用户目录 `agents/<agent id>/user/<当前登录用户ID>` 存在。该目录 SHALL 以空目录形式创建，请求 SHALL 幂等：目录已存在时视为成功且 MUST NOT 改动其内容。

`user_id` MUST 取自经校验的认证上下文（`RuntimeIdentity`），请求体 MUST NOT 被接受为身份来源；请求体只允许命名 `agent`。因此该接口 MUST NOT 能寻址他人子目录——即使客户端提交他人用户标识，也只会在调用者自己的目录上生效。部署没有端用户身份（单用户旧布局）时 SHALL 以 403 拒绝，MUST NOT 退化为在共享根下创建目录。

接口 SHALL 按与工作区写入口相同的口径校验被命名的 Agent（租户绑定、私有 Agent 归属、会话归属），MUST NOT 因该 Agent 恰好是面板当前环境 Agent 而放宽；被命名的 Agent 不具备可写绑定时 SHALL 以 403 拒绝。目录 SHALL 创建在该 Agent 真实的工作区路径下，MUST NOT 由客户端提供的路径推导。

当该 Agent 工作区中的 `user` 容器是符号链接或非目录时，接口 SHALL 以 403 拒绝（`unsafe_user_directory`），MUST NOT 静默降级为在公共目录或其他位置创建。

#### Scenario: 首次打开共享 Agent 时创建本人目录

- **WHEN** 登录成员对某共享 Agent 请求物化本人用户目录，且该目录此前从未被创建
- **THEN** 服务端在该 Agent 工作区下创建 `user/<当前登录用户ID>` 空目录并返回成功

#### Scenario: 重复请求不产生改动

- **WHEN** 调用者对同一 Agent 再次请求物化，且目录已存在
- **THEN** 服务端返回成功，且目录中的既有文件不被改动

#### Scenario: 请求体中的他人用户标识不被采纳

- **WHEN** 调用者提交的请求体包含他人的用户标识
- **THEN** 服务端只物化调用者本人的目录，不为他人创建任何路径

#### Scenario: 被命名的 Agent 无写权限时拒绝

- **WHEN** 调用者命名了一个他人私有的、未绑定当前租户的、或不属于其会话的 Agent
- **THEN** 服务端以 403 拒绝，且不在任何位置创建目录

#### Scenario: 无端用户身份的部署拒绝物化

- **WHEN** 部署没有端用户身份（单用户旧布局）时调用该接口
- **THEN** 服务端以 403 拒绝，MUST NOT 在工作区根或共享 Agent 根下创建目录

#### Scenario: `user` 容器不安全时拒绝

- **WHEN** 该 Agent 工作区中的 `user` 是符号链接或非目录
- **THEN** 服务端以 403 拒绝（`unsafe_user_directory`），且不在其他位置创建目录

## MODIFIED Requirements

### Requirement: 工作区面板默认锚定当前 Agent 自己的目录并在无权时回落到本人私有 Agent

控制台对话页右侧工作区面板的顶栏入口 SHALL 在打开时把文件列表锚定到当前会话对应 Agent 的**落点目录**，并 SHALL 位于「文件」页签且重新列举该目录，MUST NOT 因上次预览过文件而停在预览页签，MUST NOT 沿用上次浏览的子目录或上一次的回落作用域，MUST NOT 以残留条目冒充当前 Agent 的目录。

落点目录 SHALL 按当前 Agent 的**可见性**判定，可见性 MUST 由服务端给出：

- 当该 Agent 为**租户共享**（服务端报告其不属于任何单个用户）时，落点 SHALL 是**调用者本人**的用户目录——工作区根之下的 `agents/<agent id>/user/<当前登录用户ID>`，即该成员上传的文件与平台回传结果所在处。面板 MUST NOT 落在共享根 `agents/<agent id>`，也 MUST NOT 落在 `user/` 下他人的子目录。
- 当该 Agent 为**私有**（属于某个用户），或当前花名册未报告该 Agent、或部署没有端用户身份时，落点 SHALL 是该 Agent 自己的目录——工作区根之下的 `agents/<agent id>`，即该 Agent 的 `AGENT.md`、`knowledge/`、`memory/`、`outputs/`、`scheduler/`、`skills/` 等所在处。

共享 Agent 的本人用户目录 SHALL 由服务端按已验证身份物化（见「本人用户目录按已验证身份物化」），因为在首次上传之前它并不存在。面板 SHALL 在列举该落点失败且该失败**不是**权限判定拒绝时，请求服务端物化该目录并对该落点**再列举一次**；物化至多一次，MUST NOT 形成重试循环，MUST NOT 因该目录尚不存在而报错或退到共享根。

当工作区根之下没有该 Agent 的目录时（会话已打开项目目录、或单 Agent 旧布局下工作区根本身即 Agent 目录），面板 SHALL 落到工作区根本身，MUST NOT 报「不是目录」；用户自己浏览进入的路径不存在时 SHALL 照常报错。所以回到工作区根的导航（面包屑）SHALL 保持可用。

当该目录的列举以一个**权限判定拒绝**作答（服务端对该 Agent 的租户绑定、私有归属或会话归属拒绝，返回 403/404）时，前端 SHALL 回落到**调用者本人的私有 Agent 自己的目录**（工作区根之下的 `agents/<本人的私有智能体>`）：以调用者本人的私有 Agent 作为该面板的作用域，并以该 Agent 的目录继续列举，保持该作用域用于面板后续的预览、读取与写入，同时 SHALL 以既有提示机制告知已切换到本人智能体目录。回落后的重试 SHALL 按回落后的 Agent 重新取路径，MUST NOT 复用被拒 Agent 的目录。拒绝 MUST NOT 被当作「目录不存在」：回落路径 MUST NOT 触发任何目录物化或创建。

回落 MUST NOT 放宽任何授权：作用域仍由服务端按实际资源归属判定，客户端自报的 Agent 标识、自报的用户标识 MUST NOT 成为授权依据。回落 SHALL 至多发生一次（一次落点最多探询一次本人的私有 Agent），MUST NOT 形成重试循环。调用者没有本人的私有 Agent，或本人私有 Agent 的目录同样被拒时，面板 SHALL 给出本地化的「没有权限打开该智能体的目录」提示，MUST NOT 把服务端的原始拒绝串（如 `agent not found`）当作结果呈现，MUST NOT 回落到他人目录，MUST NOT 以成功状态伪装。

面板锚定的 Agent 因切换 Agent、切换会话或重新打开入口而改变时，SHALL 丢弃上一次的回落作用域，按当前 Agent 重新判定并重新落到该 Agent 的落点目录；改动期间到达的、属于先前 Agent 或先前会话的列举结果 SHALL 被丢弃，MUST NOT 覆盖当前 Agent 的面板。

#### Scenario: 打开入口即看到当前 Agent 自己的目录

- **WHEN** 用户在与某私有 Agent 的对话中点开顶栏工作区入口，且此前预览过另一个文件、浏览过某个子目录
- **THEN** 面板显示该 Agent 自己目录下的条目（其 `AGENT.md`、`knowledge/`、`memory/` 等），位于「文件」页签，且只对该目录发起一次列举请求

#### Scenario: 共享 Agent 的入口默认打开本人文件目录

- **WHEN** 用户在与其租户共享 Agent 的对话中点开顶栏工作区入口
- **THEN** 面板显示 `agents/<agent id>/user/<当前登录用户ID>` 下的条目（本人上传的文件等），MUST NOT 停在共享根 `agents/<agent id>`

#### Scenario: 本人目录尚未创建时先物化再落位

- **WHEN** 登录成员从未在该共享 Agent 下上传过文件，因而其本人用户目录尚不存在，随后点开工作区入口
- **THEN** 面板请求服务端物化该目录并对该落点再列举一次，随后显示该（此时为空的）目录，MUST NOT 报错或退到共享根

#### Scenario: 共享落点在当前工作区不可达时退到工作区根

- **WHEN** 当前会话已打开项目目录，因而工作区根之下不存在 `agents/<agent id>`
- **THEN** 面板显示工作区根的条目，不显示「不是目录」之类的错误

#### Scenario: 物化失败时回落到该 Agent 自己的目录

- **WHEN** 共享 Agent 的本人用户目录列举失败且服务端拒绝物化（例如容器不安全）
- **THEN** 面板改为列举该 Agent 自己的目录 `agents/<agent id>`（在物化之前即已存在的共享根），MUST NOT 停留在错误状态、MUST NOT 重复物化

#### Scenario: 工作区根之下没有该 Agent 的目录

- **WHEN** 当前会话已打开项目目录（或单 Agent 旧布局下工作区根即 Agent 目录），因此工作区根之下不存在 `agents/<agent id>`
- **THEN** 面板显示工作区根的条目，不显示「不是目录」之类的错误

#### Scenario: 用户浏览进入的路径不存在

- **WHEN** 用户点进一个已被删除的目录
- **THEN** 面板照常报告该路径不存在，MUST NOT 悄悄退回工作区根

#### Scenario: 无权浏览该 Agent 目录时回落到本人私有 Agent

- **WHEN** 当前会话对应 Agent 自己的目录被服务端以 403/404 拒绝（他人私有 Agent、跨租户、未绑定当前租户或非本人会话），且调用者拥有本人的私有 Agent
- **THEN** 面板改为列举调用者本人私有 Agent 自己的目录并保持该作用域，提示已切换到本人智能体目录，且不触发任何目录物化

#### Scenario: 没有可供回落的目录时给出本地化提示

- **WHEN** 当前会话对应 Agent 自己的目录被拒绝，而调用者没有任何私有 Agent，或本人私有 Agent 的目录同样被拒
- **THEN** 面板显示本地化的「没有权限打开该智能体的目录」，不回落、不重复重试、不呈现服务端原始拒绝串

#### Scenario: 回落作用域不跨 Agent 与会话存活

- **WHEN** 用户在回落状态下切换到另一个 Agent 或另一个会话，随后再次打开面板
- **THEN** 面板按新的当前 Agent 重新判定并列举该 Agent 的落点目录，不沿用上一次的回落 Agent；此前 Agent 或会话的迟到列举结果不覆盖当前面板

#### Scenario: 可见性未知时保持既有落点

- **WHEN** 服务端报告的花名册中没有当前 Agent 的行，或该行没有报告可见性
- **THEN** 面板按私有 Agent 处理，落到 `agents/<agent id>`，MUST NOT 猜测其为共享 Agent
