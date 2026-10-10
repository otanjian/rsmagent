# agent-workspace-scaffold Specification

## Purpose
TBD - created by archiving change fix-persona-workspace-facts. Update Purpose after archive.
## Requirements
### Requirement: 工作空间目录结构段落描述本部署真实布局

系统 SHALL 在为新工作区创建 `RULE.md` 时，依据该工作区的实际路径生成「工作空间目录结构」段落，使其与本部署的路径解析一致：工作区形如 `<共享根>/agents/<id>` 时，共享层（`skills/`、`websites/`、知识库）与每位用户私有层（`users/<user_id>/`）位于该共享根下，工作区只是其中一份；工作区就是实例根时，该根同时是工作区与共享根，其它智能体位于其 `agents/<id>/` 下。

段落 SHALL 至少标注：本智能体工作区的核心文件（`AGENT.md`、`USER.md`、`RULE.md`、`MEMORY.md`）、`memory/`（含 `long-term/index.db`，会话与长期记忆索引）、`scheduler/`、`tmp/`，以及共享层与用户私有层的位置和可见范围。

新建普通 Agent 的段落 SHALL 同时描述工作区下 `user/<不可变用户ID>/{uploads,outputs,work}` 业务布局，说明无项目时的默认工作位置为本人目录，区别于共享根下跨 Agent 的 `users/<user_id>` 个人数据域。`tmp/` SHALL 标明为既有内部或兼容位置，不能描述为统一布局下新业务上传的默认目录；私有与共享普通 Agent 使用同一业务布局说明，具体可见性仍以授权为准。

系统 MUST NOT 在该段落中以其它部署的根路径充当本部署的工作区或共享根。

#### Scenario: 多智能体部署下的布局段落

- **WHEN** 在 `<根>/agents/<id>` 位置创建新工作区
- **THEN** `RULE.md` 的布局段落给出该工作区路径与其所属共享根路径、`agents/<id>/` 下的核心文件与 `memory/`、`scheduler/`、`tmp/`，并给出共享层与 `users/<user_id>/` 用户私有层的位置

#### Scenario: 实例根作为工作区时的布局段落

- **WHEN** 工作区就是实例根（缺省智能体所在位置）
- **THEN** 布局段落把该根同时标注为工作区与共享根，并给出其它智能体所在的 `agents/<id>/`

#### Scenario: 不写入其它部署的根路径

- **WHEN** 本部署解析出的工作区与共享根不是 `~/cow`
- **THEN** 生成的段落 MUST NOT 以 `~/cow/` 作为本部署的工作区或共享根

#### Scenario: 新建私有与共享普通工作区
- **WHEN** 系统分别为新建私有普通 Agent 和共享普通 Agent 生成工作空间说明
- **THEN** 两者都说明配置根与 user/<用户ID> 业务目录的区别，uploads/outputs/work 位于该业务目录，users/<用户ID> 的跨 Agent 数据位置不被混淆

