## Why

共享智能体的「默认空间」提示目前指向租户根，文件面板已有进入本人目录的逻辑，但未选择项目时工具仍在智能体根执行。需要补齐默认目录与实际 cwd，使用户在共享智能体下生成的业务文件默认进入自己的 `user/<用户ID>/`。

## What Changes

- 未选择项目时，共享普通 Agent 使用 `<agent workspace>/user/<不可变用户ID>/`；已选有效项目仍优先，本人私有 Agent 保持原行为。
- 复用现有用户目录函数与会话 cwd 切换机制，让 `default_workspace` 提示、正常的文件面板落点和工具实际工作位置一致。保留「默认空间」名称及现有 API 字段。
- 运行前按可信身份确保本人目录存在；创建或解析失败时明确失败，不能悄悄在共享根执行。目录变更只作用于当前会话，不改共享 Profile、进程 cwd 或智能体配置根。
- 新回复的相对图片/文件引用按本轮实际 cwd 解析，生成的明确定位沿用现有工件/消息字段保存，避免生成成功却预览失败或切换项目后链接漂移。
- **BREAKING**：共享 Agent 无项目会话的新一轮相对业务路径改以本人目录为基准。共享规则、技能、知识与个人记忆仍使用既有来源；既有文件、历史消息和已保存链接不迁移、不重写。

## Capabilities

### New Capabilities

- `shared-agent-personal-workspace`：共享普通 Agent 的个人默认目录、会话 cwd、目录提示和新产物引用的一致性。

### Modified Capabilities

无。复用 `platform-file-browsing` 的文件授权与前序 change 已实现的本人目录落点；不改项目浏览、项目失效策略、文件交付命名或执行隔离规则。

## Impact

- 主要接入点：`common/state_dir.py` / `agent/workspace/` 的小型目录解析辅助函数、`bridge/agent_bridge.py` 的会话目录应用、Web fork 的默认空间投影及相对媒体/工件引用；提示与前端仅做必要适配。
- API 保留 `current`、`default_workspace`、`projects_root`、`recents`。有效目录由现有 `current.path` 或 `default_workspace` 表达，不新增协议版本、状态机、升级拦截或 feature flag。Desktop 复用原字段做兼容验证，不改本机目录及远程连接机制。
- 唯一归属保持：业务文件归 `(tenant, agent, user)`，Agent 配置归 Agent，个人记忆归 `(tenant, user)`；`uploads/outputs/work` 沿用现有结构。
- 前置只核对 `land-shared-agent-panel-on-own-files` 已有的本人落点、目录物化和归属检查；不重写该 change 的浏览回落策略。复用 `guard-shared-knowledge-skill-writes` 的现有提示，不依赖其他 change 整体完成。
- 不新增共享资料入口、历史迁移/映射系统、运行快照、产物防覆盖机制，不重构团队、渠道或 scheduler。共用原生会话入口已有的可信用户及宿主上下文继续沿用。
- 现行 `agent-user-file-directories` 和 `execution-isolation` 的授权与保证不变：个人 cwd 不等于脚本直接读盘的用户隔离。本 change 仍处于规划阶段。
