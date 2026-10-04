# 运行依赖评估（未应用到核心）

> 2026-10-03 18:45 实施更新：本文以下章节保留早期调查过程，不能作为当前运行结论。场景外置 host、模型中继、凭据桥、Chrome 画面/输入及页面工具已在线接通；真实 ME21N 日期填写/回读/恢复已通过，详见 [当前验收记录](live-workbench.md)。无需修改 OpenCode 核心。统一登录、完整生产隔离/审批、桌面双栏及提交仍未验收。

初次 apply 仅实现场景配置阶段。后续已补场景自有的 OpenCode 启动/工具适配组件和 MCP 登录消费者，尚未挂入在线工作台。以下文件级观察说明为何不能把组件测试等同于自动操作上线；不改变原场景、普通执行器或 OpenCode 核心。

## 1. 当前 OpenCode 工具路径

基线 `../rsmcode/opencode@0442518883`：

- `packages/server/src/handlers/session.ts` 的 `/api/session` 使用 `SessionV2.Service`；已用本项目 `OpenCodeClient` 验证真实创建/读取/重试。
- `packages/core/src/tool/registry.ts` 从 canonical `ApplicationTools` 和 Location 注册表取得工具；`packages/core/src/tool/builtins.ts` 不加载项目 MCP/旧插件。
- `packages/core/src/tool/AGENTS.md` 明确记录插件 boot 尚未重设计、MCP/Session 注册仍需设计。
- `packages/opencode/src/tool/registry.ts` 的 `.opencode/tools`/Plugin 加载属于另一套旧工具路径。旧 `/experimental/tool/ids` 能列出工具也不能证明 `/api/session` 的模型循环可调用它。
- `packages/sdk-next/src/opencode.ts` 有真实公开 `opencode.tools.register`，但它构造独立 embedded 服务，未暴露可向当前 CLI 进程注册工具的远端接口。尚未证明外置 bootstrap 能与现有 Web/CLI 共享同一个 canonical 注册服务及取消上下文。

因此 G0 工具加载/无副作用真实调用/取消传播保持未完成，不能以模拟工具或 SDK 单测替代。

可选路径：继续在场景目录实验一个外置 SDK host，需验证完整 Web 依赖、同实例注册、身份与计量；或者对 OpenCode 做一个窄配套 change，给专用服务启动路径提供可配置的 canonical 工具适配加载钩子，工具实现仍放场景目录。后者候选接缝为 `packages/opencode/src/server/server.ts` 及其 `routes/instance/httpapi/server.ts` 的进程服务组合，优先复用现有 `ApplicationTools`；不重设计整个 MCP 框架。必须先验证该接缝共享 memoMap/服务生命周期，再固定最终文件清单。

后续验证：`Scene/sap_workbench/opencode_adapter/host.ts` 直接组合现有 CLI 路由和 `ApplicationTools`，共享 Effect memo map，无需修改 OpenCode 源码。真实 HTTP `/api/session` → V2 runner → canonical 工具 → HTTP 桥的契约测试通过，框架会话 ID、消息/调用 ID、服务标识及取消通知均验证。模型供应商和最终页面桥是测试替身，尚不是 SAP 端到端证据。发现该构建异步初始化 Location 配置：首次 prompt 必须等待专用 agent/权限就绪，否则首轮可能暴露默认工具。在线接入须解决此就绪门槛及逐会话网关、模型计量，不能直接暴露此 host。

## 2. SAP 会话凭据

`auth/service.py` 的 `create_credential` 只允许控制者创建租户凭据；`save_personal_resource_config` 只支持已授权 tool/skill 个人配置，不支持当前场景的每用户、每会话、每登录代次凭据资源。现有接口不能直接满足“普通用户一次输入，仅该 SAP 会话使用、注销即失效”的公开消费者契约。

不能以管理员身份代存所有用户密码、写私有凭据 SQL、把会话伪装成一个可执行工具或把密码放项目配置来凑通。候选配套 change：在现有凭据责任域增加按真实当前主体创建/解析/撤销的短生命周期会话凭据接口；资源授权由受信消费者证明，禁止任意模型工具解析引用。复用现有加密、owner 检查、版本和审计，不另造密码库。候选修改为 `auth/service.py` 及必要的资源策略声明与专项测试；没有依据先改身份公共表。

## 3. 上游隔离与模型配额

`packages/server/src/middleware/authorization.ts` 及 `auth.ts` 是服务级 Basic 认证，不是平台 tenant/user/session 授权。当前启动的回环开发实例未设密码，仅可用于临时兼容探测。SAP 场景须经逐会话网关或独立部署单元隔离，禁止其他用户直达上游向绑定会话发消息；不能把隐藏 OpenCode 导航当成授权。

场景可外置部署独立网关及限制可达上游，避免改变普通 coding 的共享服务约定。模型消耗须接入现有硬配额消费：只有会话并发限制、工具计数或事后 token 汇总仍不足以通过。尚未具备可验证的模型准入/取消/结算钩子；是否需核心接缝随工具启动接缝一起核实。

## 4. 已确认可以保持在场景目录内的工作

配置/UI/路由消费者已实现；后续浏览器运行单元、noVNC 画面桥、动作串行化、场景会话关联和 SAP 页面适配可放本目录。Docker daemon 当前不可用，浏览器执行节点及证书信任尚未部署。开发者已信任的 Chrome 可做页面兼容观察，但它不取代按用户隔离的执行节点。

推荐先审阅上述两个窄配套范围（OpenCode 启动/工具接缝、会话凭据公共接口），确认后另立 change 实施；当前 change 保持运行能力关闭。原普通 Agent 执行循环和其他场景无需为这些依赖重构。

后续更新：OpenCode 启动/工具接缝已有场景外置实现，不再以必须改 OpenCode 核心作为前提。`backend/mcp_login.py` 为凭据域的消费者组件，接收一次登录密码并分发给两个已登记网关，不保存秘密；尚未替代缺少的凭据生产/撤销入口或可视浏览器身份核验。实际后台 SAP HTTPS 探测仍报 `SSLCertVerificationError`（verify_code 20，unable to get local issuer certificate），已请求受信 CA 路径。Chrome 的人工证书例外不等于后台 CA 信任；本轮未关闭 TLS 校验、未发送 SAP 密码。


## 用户最新部署选择（2026-10-03）

用户明确要求固定两个本机 MCP 地址，随后确认只对测试 SAP `https://sap.goodsap.cn:44300` 跳过 MCP 后台证书校验，并将 MCP 账号密码先维护在场景配置。这替代此前等待 CA 和统一登录凭据的本阶段条件。

场景已实现固定端点、配置凭据的 AES-256-GCM 存储和版本绑定、只写密码/布尔回读、定向 TLS 例外；Google Chrome 保存回读与两个真实网关的认证/只读探测均通过。其他 SAP 来源仍严格验证。配套核心没有改动。OpenCode 会话授权、页面桥和在线工具绑定依然是待完成依赖，不能据连接成功开放自动操作。详见 integration.md 最新一节。
