## Why

外部系统接入卡片缺少直接分配多个智能体的入口。需要用“默认展示已分配、搜索添加、逐项移除”的弹窗完成管理，同时在大量智能体和部分可见场景下保持权限正确、保存不误覆盖。

## What Changes

- 首期覆盖租户 MCP／ERP／OA，以及当前租户获准使用的平台 MCP；卡片增加「分配智能体」入口和当前可见分配摘要。
- 搜索输入（目录搜索与分配弹窗搜索）支持中文输入法：组合期间的拼音不作为查询，正在书写的输入框不在提交前被重绘。
- 弹窗默认分页展示已分配项，按名称或 ID 搜索添加，逐项移除，统一保存；使用服务端普通分页、权限过滤和防抖搜索。
- 保存增删差量，复用现有权限、事务和审计机制，以独立分配版本处理并发；网络结果不确定时重新读取核对，不建立响应重放协议。
- 在身份数据库维护分配关系和连接级配置状态，不复制凭据，不修改智能体工具白黑名单。工具展示和实际调用均检查分配。
- **BREAKING（仅对首次保存分配的存量连接生效）**：首次保存后，该连接仅供已分配且满足原有授权的智能体使用；清空分配表示禁止所有智能体使用。未配置的存量连接明确显示「沿用原权限」并保持原行为，新连接初始为已配置且分配为空。
- 合法删除连接时，同事务清理附属分配关系并留审计；分配关系本身不新增删除阻塞，已有运行任务、默认连接等删除保护保持。
- 本期不包含个人邮箱分配、智能体详情反向展示、全租户策略切换、映射导入工具、复杂游标或独立幂等重放设施。

## Capabilities

### New Capabilities

- `external-connection-agent-assignment`: 定义分配关系、弹窗、权限内搜索分页、差量保存、连接级首次配置及兼容行为。

### Modified Capabilities

- `external-connection-management`: 增加卡片入口与状态摘要，明确合法删除连接时清理附属分配。
- `agent-runtime-capability-enforcement`: 对已配置分配的连接增加工具枚举与派发限制，保留原有授权交集及未配置存量连接的兼容行为。

## Impact

- 前端：`channel/web/static/js/external-connections.js` 及对应样式、i18n；复用已有卡片、弹窗和未保存退出处理。
- 接口与存储：`channel/web/external_connection_handlers.py`、`channel/web/route_registry.py`、`integrations/external/service.py`、`auth/store.py`；新增租户范围下的已分配列表、搜索、差量保存，复用 `auth/object_scope.py` 与身份查询。
- 数据唯一来源为 `identity.db` 中的关系和连接级分配状态；沿用现有审计与备份流程，将新增数据纳入即可。
- 运行时：`integrations/external/tools.py` 与 `integrations/external/runtime.py` 等共同入口，保持现有调用者身份、权限、配额及执行开放条件。
- 依赖既有连接管理、MCP 继承、资源执行授权和私有智能体规范；与 `apply-test-result-in-place` 及智能体目录优化共享文件，实施时保留其改动，不重新建设这些能力。
- 当前 MCP `tools.call` 仍受已有执行开放条件限制，本 change 不负责放开第三方搜索工具。规划完成不代表实现或第三方业务执行验收完成。
