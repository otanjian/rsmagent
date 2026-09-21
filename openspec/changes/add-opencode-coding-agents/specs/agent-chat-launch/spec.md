## ADDED Requirements

### Requirement: 按智能体类型选择聊天实现

Web 显式选择 coding 智能体时，系统 SHALL 在现有对话视图进入 OpenCode 嵌入会话，并复用 `chat.use` 和目标 `agent.use` 的启动授权；普通智能体 SHALL 保持现行启动、发送及 `model.use` 规则。coding 项目 SHALL 使用其配置的远端默认项目或历史关联项目；其模型与执行权限由 OpenCode 管理，不按普通人设/工作区初始化。coding 的具体执行边界以 `opencode-coding-agents` 和 `execution-isolation` 的外部共享服务规则为准。

#### Scenario: 启动编码智能体
- **WHEN** 用户具备 coding 智能体使用资格并点击开始对话
- **THEN** 创建该智能体的编码会话并嵌入展示，使用其远端项目，不要求平台选择普通模型，不自动发送消息

#### Scenario: 普通智能体兼容
- **WHEN** 用户选择 normal 或缺少类型字段的智能体
- **THEN** 沿用原普通聊天及模型授权，不访问 OpenCode

#### Scenario: 切换过程中取消或目标失效
- **WHEN** 用户取消既有离页确认，或启动校验发现 coding 目标已不可用
- **THEN** 保留原会话，不创建新远端会话、不回退普通默认
