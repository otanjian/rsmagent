## Why

用户需要在现有智能体入口中使用 OpenCode 完成编码，并从平台统一查找、恢复和管理编码会话。现有智能体全部使用普通执行器，缺少类型分流和外部会话关联。

## What Changes

- 增加 `agent_type=normal|coding`，旧配置缺省为 `normal`；coding 智能体增加 OpenCode 项目目录，复用名称、头像、描述和现有分配权限。
- 配置一个由运维维护的共享 OpenCode 服务，使用现有 API 和 Web 页面；不增加实例管理、容器调度或通用执行引擎框架。
- 在现有对话区域嵌入指定 OpenCode 会话，保留平台会话入口；OpenCode 前端只增加嵌入模式和必要的会话导航通知。
- OpenCode 是编码会话正文、标题和执行状态的唯一权威来源。平台保存轻量关联及可重建的列表缓存，支持统一列表、新建、恢复、重命名、删除和状态同步。
- 页面可见时每 5 秒查询刷新，进入页面、恢复连接及管理操作后立即刷新；不建设常驻事件同步服务、消息镜像或消息协议转换。
- 仅实现 Web 编码入口。普通智能体、现有身份权限与会话归属继续使用现行规则；coding 不进入普通执行器、群聊、渠道和定时任务。
- 用户已明确本期不建设 OpenCode 租户/用户执行隔离。外部共享服务按显式启用的试用能力交付，准确说明共享边界；不改变普通执行器的隔离策略。

## Capabilities

### New Capabilities

- `opencode-coding-agents`: 类型与配置、共享服务、权限入口、普通运行路径防误用及关闭兼容。
- `opencode-embedded-chat`: 指定会话嵌入、极小前端桥接、导航恢复及真实部署验收。
- `opencode-session-sync`: 会话关联、列表缓存、直接管理远端会话、5 秒刷新和故障恢复。

### Modified Capabilities

- `agent-chat-launch`: 按类型选择对话实现，coding 复用入口授权、采用独立项目目录和 OpenCode 模型配置。
- `session-history-workbench`: 历史列表兼容外部编码会话及对应管理操作。
- `execution-isolation`: 明确本期外部共享 OpenCode 的适用边界，原生执行器隔离约束继续有效。

## Impact

- rsmagent：`agent/registry.py`、`agent/admin.py`、普通运行入口、Web fork 的智能体/会话处理器、路由登记、现有会话存储及聊天前端模块。
- OpenCode：配套修改 `/Users/jiantan/ai_assistant/rsmcode/opencode/packages/app` 的嵌入展示和会话导航；不修改其后端协议或数据库。
- 数据：在现有会话数据库中增加一张关联表；复用现有 `sessions` 行作为摘要缓存，不复制 OpenCode 消息或工具记录。智能体类型创建后不可变；复制只复制配置。
- 依赖：复用已落地的智能体授权、历史会话和 OpenCode HTTP/Web 能力，无其他未完成 change 前置。接入前验证本地 OpenCode 的 V2 读/创建接口与现行管理接口配合，避免假设所有方法都在同一版本 API 中。
- 本 change 只生成实施文档。实现完成必须包含真实 iframe、认证、消息流和终端验收；文档校验通过不代表功能已经实现。
