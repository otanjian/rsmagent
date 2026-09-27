## ADDED Requirements

### Requirement: MCP 配置表单不提供逐工具声明

MCP 新增/编辑表单 SHALL 只暴露连接级配置字段，MUST NOT 提供任何「哪些远端工具可调用」的逐工具声明输入。提交的 `config` SHALL 只包含 `mcp-connection-integration` 接受的非秘密键，任一会被服务端拒绝的字段 SHALL 在提交前被表单拦下并提示。查看模式 SHALL 只呈现已保存的连接级配置。

#### Scenario: 表单不含逐工具声明
- **WHEN** 操作者打开 MCP 新增或编辑表单
- **THEN** 表单不含逐工具声明输入，远端工具名不作为可编辑配置项出现

#### Scenario: 提交的配置只含连接级键
- **WHEN** 操作者保存 MCP 连接
- **THEN** 请求体中的 `config` 只含连接级键（传输、URL、认证、请求头、命令、参数、环境变量键、OAuth 提供方、工具名前缀），不含任何逐工具名单

#### Scenario: 窄屏与多语言
- **WHEN** 在窄屏或非默认语言下打开 MCP 配置
- **THEN** 既有字段与提示可用、文案完整且遵循既有表单体验
