## Why

2026-10-13 的 Windows 开发模式巡检发现日志、启动诊断、记忆和平台提示问题；同时既有本机项目执行代码仍缺四份主规范基线及完整真实平台验收。将稳定性、远程部署和本机项目执行验收统一承载，避免能力声明、错误提示及验收任务在两个 change 中分散或重复。

## What Changes

- 修复主进程终端断管后的日志递归；分离桌面与后端日志并采用可靠轮转，复用现有日志入口。自建队列、异常聚合、导出界面和旧日志自动迁移不作为本次交付前置。
- 本机开发模式仅按显式路径、平台项目 venv、PATH 探测实际可执行的 Python；复用构建依赖检查，核心缺依赖明确失败，可选工具在首次使用时检查；错误贯通现有 IPC 与错误页。
- 先定位第二次后端启动来源，仅在证实内部竞态后补对应并发或旧回调保护，不预设生命周期重构；提供 Windows 环境准备与 Electron 启动脚本。
- 定位个人记忆 403 的底层异常，修复合法 Windows 路径访问并保留归属、防替换及内容版本守卫；按实际原因显示恢复提示。
- 复用现有 metadata/能力组合分别表达读取、写入和执行限制，补原因字段与统一错误文案，不新建第二套能力快照或缓存。能力缺失时关闭新增入口，已有绑定保留可解释状态，云端工作区继续可用。
- 对既有 remote 模式、HTTPS、PKCE、协议协商和重连做回归，只补缺失的超时、诊断、WSS 网关代理及部署说明，验证真实 Windows 到远程服务器的链路，不重新开发现有认证或连接框架。
- 对控制台/工作台命名窗口落地差异进行定向复现，按既有页面路由规则修正或明确交互；巡检截图增加有界超时。
- 并入原本机项目执行 change：承接四个新能力基线、项目浏览及执行隔离增量，复验文档 generation 的上游修复，覆盖本地/远程的六工具、技能包、命令幂等/取消、产物来源与安全预览。按文件面和脚本面分阶段验收，再开放相应声明与显式部署开关。
- 使用当前 `acceptance-local-project-execution.md` 的 A01–A36 矩阵；保留原 13 个文件的完整快照、3 份历史证据和原任务完成状态。原目录仅在迁移完整性及规划校验通过后删除，未将未验收项记为完成。
- 本次不实现 Windows 原生文件 helper 或命令隔离，不开启 Windows 本机项目执行，不新增 enterprise-local 模式或跨端同步。后续平台建设必须独立提案、取得文件访问和执行隔离的真实平台证据，再修改能力契约。
- 保持一个 change，按工作包 A（稳定性与普通远程连接）和 B（本机项目执行验收）分别交付；隔离门槛仅阻止执行能力开放，不阻止日志、文案、代际或选择缺陷修复。复用已有测试与证据，只对改动及证据不足项补测，避免重复建设。

## Capabilities

### New Capabilities

- `desktop-runtime-diagnostics`：桌面主进程日志、解释器与依赖探测、后端生命周期和开发启动诊断。既有相关代码存在；本次新增统一可验收的行为规范。
- `desktop-project-execution`：本地及远程模式的原地项目工具路由、运行作用域、兼容与生命周期；已有代码，补齐尚未应用的基线。
- `desktop-skill-runtime`：本机技能完整包、版本、依赖、资源根及凭据边界；已有代码，补齐基线。
- `desktop-execution-delivery`：业务运行关联、双端重验、真实终态、幂等、取消及进程归属；已有代码，补齐基线。
- `desktop-project-artifacts`：本机产物保存位置、历史来源、文件面板、预览和显式传输；已有代码，补齐基线。

### Modified Capabilities

- `desktop-tenant-context`：追加远程运行位置及连接诊断要求，不改变 PKCE、原生凭据和租户边界。
- `scoped-project-browser`：追加能力入口/错误要求，并承接原地打开与复制导入的区别、普通浏览边界和本机绑定重验，不放宽服务器项目根。
- `execution-isolation`：承接本机执行根及权限模式的约束，保留原场景，不以 cwd、环境变量或提示词充当隔离。
- `database-memory-console`：追加 Windows 合法访问及安全异常分类要求，不降低共享记忆服务的既有守卫。
- `production-deployment`：追加远程桌面同源 HTTPS/WSS、网关运维、持久化及客户端验收要求，不重定义原日志或构建规范。

## Impact

- 桌面：`desktop/src/main/index.ts`、`python-manager.ts`、`auth-broker.ts`、`remote/connection.ts`、`remote/device-connection.ts`、`remote/profiles.ts`、`project-browser/`、`local-files/`，renderer 的 `useBackend.ts`、`StatusScreen.tsx`、类型与翻译，`desktop/build/` 和开发启动脚本。
- 服务端/Web：`agent/memory/personal.py`、`common/safe_fs.py`、现有记忆页面、`channel/web/static/js/workspace.js`、桌面 metadata 和错误契约、`integrations/desktop/gateway.py`、`docker/nginx.conf.example` 及部署文档。具体修改由复现证据决定，不整文件覆盖上游。
- 数据唯一归属：远程服务继续持有身份、租户权限、业务会话、服务器记忆和上传文件；主进程独占原生 Bearer；桌面保存服务器非秘密配置、日志和本机授权根。日志不得成为身份库或记忆正文副本，本机绝对目录不得写入服务器元数据。无需业务数据库迁移，不修改用户现有配置或清空记忆。
- 原 `accept-desktop-local-project-execution` 的内容已并入本 change，不再作为外部依赖；本 change 负责其原有执行验收与基线补齐。与 `add-desktop-local-runtime-with-enterprise-services` 保持 remote 与 enterprise-local 区别；与 `optimize-console-login-startup` 协调代理模板，保留 gzip、缓存及流式行为。旧名称仅用于迁移追溯和历史快照。
- 复用 `identity-session`、`desktop-tenant-context`、`execution-isolation`、`resource-execution-authorization`、`audit-log`、`credential-management`、`action-approval`、`resource-quota` 的适用切片；不以相关 change 整体完成作为本次普通远程聊天的门槛。
- 巡检报告仅是问题证据，不是实现指令；记忆 403 根因、第二次 spawn 来源和窗口落地差异仍需定向复现。本次仅创建规划产物，未实施或验收产品能力。
