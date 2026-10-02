## Why

当前 Web 工作台已经包含场景应用、企业权限、审计、外部系统接入和知识资料等功能，而 Electron 使用独立 React 业务界面并默认启动本地 Python 后端，形成两套功能覆盖不一致的前端。需要在服务器集中部署的前提下，使桌面端复用同一 Web 工作台，并按用户授权读取客户端文件，形成可分阶段发布、程序员可以直接实施的交付方案。

## What Changes

- **阶段一：完整承载 Web。** 增加远程服务器配置与能力协商；使用隔离的 WebContentsView 加载同一服务器 Web 入口；保留本地模式；交付原生 PKCE 登录到内嵌 Web Cookie 会话的受控交接、退出联动、租户切换、上传/下载/预览及完整页面验收。
- **阶段二：本地文件接入。** 增加只读目录授权、设备/工作区绑定、WSS 控制通道、HTTPS 分块传输、按需物化到本人服务器任务目录及用户主动另存为。首次交付不包含覆盖原文件、双向同步或任意本地命令。
- **阶段三：桌面体验与发布。** 增加通知与会话定位、托盘后台驻留、休眠恢复、版本协商、签名更新与诊断；交付默认关闭的固定 CSV/XLSX 本地解析器，启用取决于真实跨平台验收。
- 增加 Web 环境适配模块与窄化原生桥；普通浏览器继续使用原有实现，不复制场景、权限、审计等业务页面到 React。
- 明确修改既有 `desktop-tenant-context` 中“所有 Desktop 业务均经主进程 broker”的要求：本地 React 模式及原生服务沿用 broker；远程 Web 内容使用关联的独立 HttpOnly Cookie 会话。原生 Bearer 仍只存在于主进程内存。
- 更新 Web 前端规范中“Desktop 始终不装载 Web”的描述，分别登记本地 React 与远程 Web 两种消费者。不删除既有 React 页面、不迁移或覆盖用户本地业务数据。
- 所有新路由、工具、网关及能力投影登记到明确的权限与装配接缝；页面承载成功不等于业务执行、文件访问或本地计算已经开放。

## Capabilities

### New Capabilities

- `desktop-remote-web-workbench`：服务器配置、隔离 Web 容器、功能同源复用、窄化适配器及完整 Web 功能验收。
- `desktop-local-file-access`：设备身份、本人目录授权、会话绑定、逐次文件访问授权、跨平台路径约束和持久请求协议。
- `desktop-file-transfer`：有界分块上传、配额预留、版本校验、原子发布、按需物化和用户主动另存为。
- `desktop-runtime-lifecycle`：连接/后台/休眠/退出状态、系统通知、兼容协商、签名更新、诊断及恢复。
- `desktop-local-processing`：默认关闭的固定格式本地解析器、输入/输出边界、版本与资源限制、执行结果溯源。

### Modified Capabilities

- `desktop-tenant-context`：双传输边界、关联 Web 会话引导及双向撤销、上下文绑定；保留既有 PKCE、逐请求授权与不持久化原生凭据要求。
- `web-console-frontend-modules`：显式区分本地独立 React 和远程复用 Web 的消费方式，保留上游增量与路由覆盖门禁。

## Impact

- **客户端**：`desktop/src/main/{index,preload,auth-broker,asset-proxy,python-manager,tray,updater}.ts`、本地连接设置页面、构建与签名配置。远程模式引入独立 preload、WebContentsView 管理器、文件服务和网关客户端；主进程增加锁定版本的 WebSocket 客户端依赖。
- **Web**：`channel/web/chat.html` 的受控模块装载接缝与 `channel/web/static/js/fork/` 中的环境适配、目录入口、连接状态；沿用服务器当前发布的功能与授权，不另建桌面菜单真值。
- **服务器**：`auth/{desktop_auth,session,store,service,capability_matrix}.py`、`channel/web/route_registry.py`、新增 fork handlers、`integrations/desktop/` 服务与 aiohttp 网关、Agent 文件获取工具与已有执行记录接缝。
- **数据唯一归属**：账号/角色/租户/业务会话/执行记录仍归服务器；本地授权根的绝对路径只归客户端；服务器只保存工作区标识、相对路径、版本和任务副本。AuthSession 不新增 current_tenant；设备标识不冒充机器主体或 Membership。
- **依赖切片**：沿用 `identity-session`、`desktop-tenant-context`、`tenant-resource-isolation`、`agent-user-file-directories`、`scoped-project-browser`、`resource-execution-authorization`、`audit-log`、`credential-management`、`resource-quota`、`action-approval` 和 `execution-isolation`。按阶段验证实际消费到的切片，既有未验收能力保持原状态，不因本 change 解锁。
- **活跃 change**：知识资料转换、scheduler 通知等以实施时实际发布能力为准，尤其 `add-traceable-knowledge-ingestion`、`add-scheduler-self-delivery` 和 `rename-console-entry-system-access`。记录基线、门禁与缺口，不复制尚未完成的实现，也不替其他 change 声称验收通过。
- **兼容范围**：远程模式首批覆盖 Windows 10/11 x64、macOS arm64/x64；现有旧 Windows/Electron 发行线保留本地模式，未通过能力检查时不开放远程容器。第一版只支持 HTTPS origin 根路径部署，路径前缀部署明确返回不支持。
- **交付边界**：本次仅生成设计产物。三个阶段的实现、测试和真实打包验收均保持未完成；设计见 `design.md`，协议见 `contracts.md`，验收见 `acceptance.md`，实施入口见 `tasks.md`。
