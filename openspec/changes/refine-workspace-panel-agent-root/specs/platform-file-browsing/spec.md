## ADDED Requirements

### Requirement: 工作区面板默认锚定当前 Agent 自己的目录并在无权时回落到本人私有 Agent

控制台对话页右侧工作区面板的顶栏入口 SHALL 在打开时把文件列表锚定到**当前会话对应 Agent 自己的目录**——工作区根之下的 `agents/<agent id>`，即该 Agent 的 `AGENT.md`、`knowledge/`、`memory/`、`outputs/`、`scheduler/`、`skills/` 等所在处。入口打开后 SHALL 位于「文件」页签并重新列举该目录，MUST NOT 因上次预览过文件而停在预览页签，MUST NOT 沿用上次浏览的子目录或上一次的回落作用域，MUST NOT 以残留条目冒充当前 Agent 的目录。

当工作区根之下没有该 Agent 的目录时（会话已打开项目目录、或单 Agent 旧布局下工作区根本身即 Agent 目录），面板 SHALL 落到工作区根本身，MUST NOT 报「不是目录」；用户自己浏览进入的路径不存在时 SHALL 照常报错。所以回到工作区根的导航（面包屑）SHALL 保持可用。

当该目录的列举以一个**权限判定拒绝**作答（服务端对该 Agent 的租户绑定、私有归属或会话归属拒绝，返回 403/404）时，前端 SHALL 回落到**调用者本人的私有 Agent 自己的目录**（工作区根之下的 `agents/<本人的私有智能体>`）：以调用者本人的私有 Agent 作为该面板的作用域，并以该 Agent 的目录继续列举，保持该作用域用于面板后续的预览、读取与写入，同时 SHALL 以既有提示机制告知已切换到本人智能体目录。回落后的重试 SHALL 按回落后的 Agent 重新取路径，MUST NOT 复用被拒 Agent 的目录。

回落 MUST NOT 放宽任何授权：作用域仍由服务端按实际资源归属判定，客户端自报的 Agent 标识 MUST NOT 成为授权依据。回落 SHALL 至多发生一次（一次落点最多探询一次本人的私有 Agent），MUST NOT 形成重试循环。调用者没有本人的私有 Agent，或本人私有 Agent 的目录同样被拒时，面板 SHALL 给出本地化的「没有权限打开该智能体的目录」提示，MUST NOT 把服务端的原始拒绝串（如 `agent not found`）当作结果呈现，MUST NOT 回落到他人目录，MUST NOT 以成功状态伪装。

面板锚定的 Agent 因切换 Agent、切换会话或重新打开入口而改变时，SHALL 丢弃上一次的回落作用域，按当前 Agent 重新判定并重新落到该 Agent 自己的目录。

#### Scenario: 打开入口即看到当前 Agent 自己的目录

- **WHEN** 用户在与某 Agent 的对话中点开顶栏工作区入口，且此前预览过另一个文件、浏览过某个子目录
- **THEN** 面板显示该 Agent 自己目录下的条目（其 `AGENT.md`、`knowledge/`、`memory/` 等），位于「文件」页签，且只对该目录发起一次列举请求

#### Scenario: 工作区根之下没有该 Agent 的目录

- **WHEN** 当前会话已打开项目目录（或单 Agent 旧布局下工作区根即 Agent 目录），因此工作区根之下不存在 `agents/<agent id>`
- **THEN** 面板显示工作区根的条目，不显示「不是目录」之类的错误

#### Scenario: 用户浏览进入的路径不存在

- **WHEN** 用户点进一个已被删除的目录
- **THEN** 面板照常报告该路径不存在，MUST NOT 悄悄退回工作区根

#### Scenario: 无权浏览该 Agent 目录时回落到本人私有 Agent

- **WHEN** 当前会话对应 Agent 自己的目录被服务端以 403/404 拒绝（他人私有 Agent、跨租户、未绑定当前租户或非本人会话），且调用者拥有本人的私有 Agent
- **THEN** 面板改为列举调用者本人私有 Agent 自己的目录并保持该作用域，且提示已切换到本人智能体目录

#### Scenario: 没有可供回落的目录时给出本地化提示

- **WHEN** 当前会话对应 Agent 自己的目录被拒绝，而调用者没有任何私有 Agent，或本人私有 Agent 的目录同样被拒
- **THEN** 面板显示本地化的「没有权限打开该智能体的目录」，不回落、不重复重试、不呈现服务端原始拒绝串

#### Scenario: 回落作用域不跨 Agent 与会话存活

- **WHEN** 用户在回落状态下切换到另一个 Agent 或另一个会话，随后再次打开面板
- **THEN** 面板按新的当前 Agent 重新判定并列举该 Agent 自己的目录，不沿用上一次的回落 Agent
