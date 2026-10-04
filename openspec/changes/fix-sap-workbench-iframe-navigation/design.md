## Context

问题见 proposal.md。左侧是跨域 SAP iframe；原生 OpenCode 中的通用 Playwright 管理另一浏览器。原生页面没有同源 DOM 控制桥。现有场景工具桥携带受信 service/session/call 上下文，场景心跳已有平台认证。

## Goals / Non-Goals

**Goals:** 为明确的交易打开请求提供同一左侧 iframe 导航；保留原生配置和会话历史。

**Non-Goals:** 此次不实现跨域字段读取/填写、同一 SAP 内部会话保证、业务提交或登录身份证明。

## Decisions

1. 场景宿主使用原生 HttpApiApp 路由及 Node HTTP/WebSocket 层，共用同一 Effect memo map，附加注册 `sap_transaction_open`。不修改 OpenCode 源码，不限制其原有工具和模型。
2. 工具桥只接受交易代码，从保存的 SAP URL 生成 `~transaction`；不得接受模型指定的任意 URL/窗口/账号。服务端排队，场景心跳获取导航，父页面修改现有 iframe 的 src 并确认。队列仅在当前场景生命周期内有效。
3. 原生导航结果仅证明当前左侧接收了 URL 导航。用浏览器实测确认 ME21N/ME23N 标题；不得据 load 事件宣称登录、单据或业务结果成功。
4. 项目 `.opencode/plugins/rsm-sap-workbench-navigation.js` 安装场景提供的原生 V1/V2 插件。只在工作台进程的专用环境标记启用时追加导航指引；V1 通过原生 server hook 注册工具并保留原生权限询问，V2 使用共享 ApplicationTools 注册表及可见智能体指令追加。原项目指令、模型、权限和工具保留，普通 OpenCode 进程不改变行为。已有同名但内容不同的文件拒绝覆盖，仅允许升级本 change 安装的精确初版内容。V2 Promise 插件 setup 后显式 reload，确保指引提交。
5. 当前内嵌 Web 实际选择 V1 协议，历史保存在原生 message/part 表。保留该协议及历史，不为接入工具强制切换 V2。测试同时覆盖 V2 原生 runner、V1 hook，并在 Chrome 验证真实 V1 对话链路。
6. 场景 loopback 网关关闭内核 TCP keepalive：现场 macOS 接受 Bun 连接时设置 SO_KEEPALIVE 报 EINVAL，导致请求尚未进入工具桥即超时。HTTP 连接复用、认证、请求期限和工作台心跳保留；不修改其他服务。

## Risks / Trade-offs

- [URL 导航重载 SAP，可能遇到登录过期或未保存草稿] → 工具描述说明边界，仅响应用户明确的打开交易请求；不附加保存操作。
- [迟到心跳/关闭会话/重复轮询] → 绑定与前端对象一致性检查、一次性命令 ID、超时清理、关闭取消及接收确认。
- [原生宿主组合与引擎版本相关] → 使用本项目已固定的原生路由、作用域和工具注册接口，以原生监听及实际工具执行测试验证共享注册表。

## Migration Plan

重载本项目源后端，恢复或创建场景会话以加载新增工具。无数据库迁移，也不重写项目 opencode.json。回滚还原场景导航代码并移除本 change 安装且内容未被用户修改的导航插件，现有历史与原生配置不变。
