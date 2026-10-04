## Why

SAP智能工作台的助手使用通用 Playwright 打开交易时，操作的是独立浏览器，左侧 SAP iframe 没有变化。需要让“打开 ME21N/ME23N”等明确导航请求送到当前工作台的可视 SAP 页面。

## What Changes

- 增加场景专用交易导航工具，绑定当前工作台与 OpenCode 执行上下文。
- 前端在现有 SAP iframe 内应用导航，并返回导航接收确认；不新开窗口、不重建对话。
- 超时、旧会话和迟到请求明确失败；导航确认不冒充登录成功、页面业务状态或单据提交成功。
- 保留 OpenCode 原生模型、智能体、MCP、技能和其他工具配置。
- 兼容原生 V1/V2 工具入口；保持当前内嵌 Web 的协议及会话历史，并修复本机 loopback 网关接收工具请求时的连接兼容问题。

## Capabilities

### New Capabilities

- `sap-workbench-iframe-navigation`: 对当前左侧 SAP iframe 的交易 URL 导航、绑定与确认。

### Modified Capabilities

无。本次增加有限的交易导航能力，不把此前归档中尚未通过的通用 DOM 自动控制和业务提交范围标为完成。

## Impact

仅修改 `Scene/sap_workbench/` 导航运行时、场景宿主和前端，刷新场景源清单，并新增相应测试及本 change。在 SAP 项目安装一个仅对工作台进程启用的原生导航插件，不重写 opencode.json。SAP 使用标准 Web GUI `~transaction` 参数；不修改普通智能体或 rsmcode/OpenCode 源码。数据与生命周期继续归属场景会话。
