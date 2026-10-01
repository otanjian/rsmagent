## Why

桌面端“退出账号 → 重新登录 → 选择本地目录”后，网页显示已登录，本地目录绑定却返回 `not signed in`。现场记录显示，退出已撤销原生会话及其配对 Web 会话，随后容器里的密码表单只创建普通 Web 会话。缺口在账号入口没有接回已有原生登录流程。

## What Changes

- 桌面账号退出改为串行执行：确认当前 Web 会话退出，再调用宿主完成原生退出和容器清理，回到已有本地登录/连接界面。新增一个窄化宿主 `signOut` 动作。
- 本地壳收到退出开始/结束通知，更新已有登录 gate；重新进入时复用已有原生 PKCE 授权、Web 配对与容器挂载。
- 桌面容器需要重新登录时显示恢复入口，走同一退出和原生登录路径；禁止在容器里通过普通密码表单制造只有 Web 登录的状态。普通浏览器保留原流程。
- 目录选择前拦截已知原生会话缺失/退出阻断，绑定时遇到确定的会话失效则提供重新登录操作；沿用现有目录 generation 和清理机制防止旧选择回流。
- 保留跨账号隔离、退出失败重试和真实客户端文件读取验收，分别验证登录失效与 `client_files` 工具权限不足。
- 补齐复测发现的模型上下文缺口：每轮将已验证的本地目录选择同时提供给模型与 `client_files`，明确本地输入与服务器工作区的区别，验收自然语言请求实际产生客户端读取。
- 补通本地桌面后台的设备网关：随后台启动既有 gateway，由本机 metadata 发布实际回环端口，受信宿主连接该端口；远程模式保持同源网关，沿用全部身份和目录授权。

## Capabilities

### New Capabilities

无。补充既有能力的桌面账号衔接行为。

### Modified Capabilities

- `desktop-tenant-context`：补充容器退出后回到原生登录、目录身份失败的恢复行为；沿用原有身份、凭据、租户和权限规范。
- `account-menu-actions`：明确桌面账号菜单串行退出及恢复入口，保留普通浏览器行为。

## Impact

- 桌面：主要接入 `remote-host-ipc.ts`、`remote-container-ipc.ts`、两类 preload、本地 `DesktopContext` / `App.tsx` / `RemoteConnectPage.tsx`；复用 `auth-broker.ts` 和 `local-web-bind.ts`，只补必要的退出阻断、通知及错误传递。
- Web：在 `channel/web/static/js/fork/desktop-host.js` 及桌面适配模块中承载逻辑，以最小接缝连接 `console.js` 的退出、登录展示和目录错误处理；同步受影响的 `contracts/desktop/` 能力声明。
- 数据唯一归属：身份库继续拥有 AuthSession 和配对关系，broker 独占原生令牌，容器持有受保护 Cookie。不新增身份表、服务端接口或会话存储。
- 依赖：现行 `desktop-tenant-context`、`identity-session`、`account-menu-actions`、`web-console-frontend-modules`；复用已存在的原生授权、Web 配对、容器清理与本地文件切片。实施前确认受影响路径可用，不重新审计所有历史 change。
- 范围：本次不引入第二套会话状态机、通用会话协调服务或全局 epoch，不重构租户切换/绑定传输，不调整角色授权。已有 `client_files` 权限缺失仍是独立配置问题。
