## Why

容大 AI 已有 MCP 运行能力和部分 ERP 场景接口，但缺少统一、可授权的连接管理入口，OA 与个人邮箱也尚未形成配置到执行的完整链路。需要将已确认的卡片式 HTML Demo 落为生产规范，对齐 OneAgent 的实际接入能力，并补齐本项目的多租户、凭据、审计和执行边界。

## What Changes

- 在控制台「模型与接入」分组、「消息渠道」之后增加「外部系统接入」，以连接卡片管理 MCP、ERP、OA、邮箱；新增连接先选择类型，再打开对应配置抽屉。提供搜索、筛选、编辑、启停、连接测试、删除、错误恢复和未保存保护。
- 建立统一连接元数据、版本和状态服务。平台 MCP 模板及租户覆盖、租户 ERP 多连接与唯一默认、租户 OA 单连接、当前租户下本人邮箱单连接具有明确归属；控制台、场景与运行时共用同一数据源。
- MCP 支持 stdio、SSE、Streamable HTTP、认证及已有 OAuth 链路、工具发现、全局继承/租户覆盖/恢复继承。配置权限与工具执行授权分离，stdio 以执行隔离切片验收为开放前提。
- ERP 交付 SAP RFC/BAPI、ADT SQL 对应配置、真实连通性检查及现有场景调用；U9、金蝶现有实现仅为模拟，生产连接类型不得将其标成可用。真实 U9/金蝶适配器不纳入本次交付。
- OA 交付站点、账号及 OpenAPI 参数配置、登录检查、待办/流程查询与受控审批动作；邮箱交付个人 IMAP/SMTP、TLS、发件人与目录限制配置、分协议测试，以及获权的邮件查询、附件处理和发送等动作。
- 所有秘密采用服务端加密与掩码投影；复用现有凭据、授权、内部审计、单动作审批和硬配额基础设施。为平台共享 MCP 单独定义严格绑定连接的平台服务凭据，禁止将租户或个人凭据变为全局共享。
- 数据迁移覆盖本项目既有 MCP 配置及 ERP JSON，保留连接引用和授权映射，提供预检、幂等执行、切换及恢复方案。OneAgent 仅参考代码与交互，不导入其配置、凭据或客户数据。
- **BREAKING**：ERP 配置读取不再回传原始密码；旧整表保存改为版本条件写入并拒绝无版本覆盖。现有场景调用接口和连接标识通过兼容层保留，随附调用方迁移与发布门槛。

## Capabilities

### New Capabilities

- `external-system-access-console`：卡片目录、类型选择、差异化表单、范围与状态、交互完整性及客户端验收。
- `external-connection-management`：连接唯一归属、统一 API、并发与幂等、秘密更新、测试、生命周期、迁移与启用条件。
- `mcp-connection-integration`：三种传输、工具发现、认证、平台继承和租户覆盖、运行时刷新及执行边界。
- `erp-connection-integration`：SAP 配置与调用、默认连接、现有场景兼容、真实与模拟适配器区分。
- `oa-connection-integration`：租户 OA 配置、认证、查询与受控写操作。
- `personal-email-integration`：本人邮箱配置、IMAP/SMTP 测试、邮件与附件操作及个人归属。

### Modified Capabilities

- `console-information-architecture`：新增外部系统接入的正式菜单位置及共用页面要求。
- `console-settings-organization`：明确模型凭据与外部连接配置的唯一编辑位置，场景与工具页面只链接至主入口。
- `credential-management`：在保持租户与个人凭据隔离的前提下，增加只服务于获准平台共享 MCP 的平台服务凭据用途。

## Impact

- 前端：`channel/web/chat.html`、`channel/web/static/js/console.js` 的页面注册/导航/路由，新增按需加载的连接管理模块；复用既有主题、国际化、身份上下文、能力投影和离页机制。设计参考为 `docs/design/demos/external-system-access.html`，其中模拟身份、模拟测试和示例数据不进入生产。
- 服务端：`channel/web/route_registry.py`、`auth/store.py`、`auth/service.py`、`auth/capability_matrix.py` 等；连接及秘密元数据归属身份控制库，不归属场景业务库。新增独立连接服务和类型适配器，避免继续扩大 `web_channel.py` 的混合职责。
- 运行时：`agent/tools/tool_manager.py`、`agent/tools/mcp/`、`Scene/_shared/` 及 SAP/采购场景；新增 OA、邮箱受控工具入口，复用现有 ExecutionRun 和授权链，不把所有秘密注入通用 Bash。
- 依赖：`audit-log`、`credential-management`、`resource-execution-authorization`、`action-approval`、`resource-quota`、`execution-isolation` 按消费切片核验；Desktop 另需 `desktop-tenant-context` 与原生 broker 路由验收。Web 开放不能代替 Desktop、Channel 或定时任务验收。
- 本 change 产物定义生产实现与验收范围；生成产物不表示代码已实现、真实外部系统已连通或依赖切片已通过。
