## Why

master 的提交已合入 rdai，但 Desktop 调用的八个接口尚未被 fork 服务，当前 Web 也未接通模型目录和多节点回退链编辑。需要在保留现有租户、用户、资源授权的条件下按功能交付，避免等待全部前端模块迁移才恢复这些能力。

## What Changes

- R1：在现有 `/auth/context` 增加按动作的功能可用性投影；Web/Desktop 不再请求未开放的能力。
- R1：接通上下文用量、手动压缩两接口，保留会话 owner 校验、实时 Agent 寻址和写请求保护。
- R1：在现有 Web 中增加模型目录与有序回退链编辑，复用已存在的 `/api/models` 平台配置接口。
- R2：接通调度实例、接收者、任务创建和运行记录列表/详情/删除六接口；创建调用统一任务授权服务。
- R2：为新增运行记录保存不可随当前绑定变化的归属快照，在查询、分页、详情和删除之前限定身份范围；归属不明的历史记录保持不可见。
- 新增开发任务书、接口契约、可复制的起始测试、文件和函数定位、分批开关、验收与回退步骤。
- 保留上游模块原样，通过 fork 模块接入；109 处前端裁定、单体退出、Web 一键更新与微信个人渠道开放不属于本 change 的实现范围，继续独立跟踪。

## Capabilities

### New Capabilities

- `session-context-controls`：本人业务会话的上下文用量查询、手动压缩、并发控制和两端入口。

### Modified Capabilities

- `database-runtime-consumers`：增加动作级能力声明与两端同步关闭、旧客户端/服务端兼容行为。
- `database-scheduler-console`：增加受控的跨渠道任务创建、运行台账可见范围与历史归属语义。
- `platform-config-console`：增加当前 Web 中模型目录、隐藏模型与有序回退链的编辑和无损回读。

## Impact

- 后端：`auth/capability_matrix.py`、`auth/service.py::context_for_tenant`、`channel/web/route_registry.py`、fork handlers、scheduler 授权及运行记录接入点；保留所有现有接口形状和 upstream `api/core` 模块。
- 前端：现有 Web shell 的三个独立功能模块，以及 Desktop 的 TasksPage、ContextUsagePopover、API 客户端、会话上下文和轮询逻辑。
- 数据唯一归属：任务仍在现有 TaskStore；运行结果仍在既有 `runs`；会话正文仍在 `messages`；模型配置沿用现有配置和 model catalog。新增同库 `fork_scheduler_run_scopes` 仅承载授权快照，不复制运行正文。
- 分批前置：已存在的身份、租户、任务授权、配额、审计及 Desktop 身份传输切片；开放前以本 change 的真实验收确认，不以历史 change 的归档状态代替证据。
- 与已归档 `adopt-upstream-web-split` / `adopt-upstream-web-frontend-split` 衔接：承接功能缺口，逐项更新延后清单及来源登记；不把整个前端迁移标记完成，也不要求重新合并 master。
- 不增加运行时第三方依赖。数据库扩展为可重复执行的新增表，旧版本可忽略；细节见 design.md。本次仅生成规划产物，所有实现任务保持未完成。
