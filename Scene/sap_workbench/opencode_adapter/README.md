# SAP 场景 OpenCode 适配器

2026-10-04 当前左侧导航修复：`native-host.ts` 组合原生 HTTP/WebSocket 层和共享工具注册表，附加 `sap_transaction_open`；项目导航插件同时提供原生 V1 server hook 与 V2 setup，只在工作台进程启用。当前内嵌 Web 走 V1，保持原协议及 message/part 历史；不强制切换协议，不重写 opencode.json，不限制原生模型、智能体、工具、MCP 和技能。工具从框架上下文取得 service/session/message/call 绑定，返回左侧 URL 接收确认，不能证明登录或业务结果。Chrome 已通过 ME21N/ME23N 同页导航，见 [本次验证](../../../openspec/changes/fix-sap-workbench-iframe-navigation/evidence/verification.md)。

当前原生 SAP iframe 模式由 `server.ts` 通过 `native-host.ts` 组合相邻 OpenCode 引擎的原生 HTTP/WebSocket 层，全局/项目配置沿原生发现规则加载，保留全部模型、智能体、工具、MCP、技能和命令。仅覆盖会话 DB，原私有历史保留；`credentials.ts` 只读原生全局库继承 provider 登录，使模型目录可用，不复制会话，不回写来源库，私有连接修改保留；监听器使用随机 Basic 凭据，浏览器经场景所有者网关访问。网关转发配置/文件/终端/会话 API 与 WebSocket，原生工具按 OpenCode 权限运行。场景适配当前 Web/CLI 的默认模型/事件形状，附加上述导航工具，并隐藏紧凑页签与内部顶部空白工具栏样式，不改 OpenCode 核心。四项 OpenSpec 技能和 CLI 已通过真实 native skill/bash 的隔离执行验证。下面固定配置和只读工具说明仅适用于旧 screen 模式。见 [此前原生配置证据](../../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/native-config-fullscreen.md)。

2026-10-04 当前 MCP/DOM 收尾：场景后端兼容 MCP 1.29 FastMCP 字符串包装，保留私有 JSON 预算；worker 管道及 DOM/F4 归属回读已修复。完整 SAP Python **1182 pass / 2 skip**、相关 Node **170 pass**；本适配器 TypeScript 和 Bun 本轮未修改、未重跑，上一轮 4/81 结果保留。计划仍为 **42/57，剩余 15 项**，真实 SAP/OpenCode 操作检查继续暂缓，没有保存工具或提交适配器，见 [MCP 与 DOM 收尾](../../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/mcp-dom-closeout.md)。

2026-10-04 上一轮新增已有订单只读工具，计划仍为 **42/57，剩余 15 项**；完整 SAP Python **1123 pass / 2 skip**、相关 Node **165 pass**、Bun **4 pass / 81 assertions**。真实 SAP/OpenCode 操作检查继续暂缓，没有保存工具或提交适配器。边界与该轮实际结果见 [采购订单只读收尾](../../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/purchase-order-read-closeout.md)。

历史源码记录 `rsmcode/opencode@0442518883`、Bun 1.3.14；当前来源 `9acdb1d09f`，26 个 host 引用静态接缝未变，当前来源 canonical host 已有隔离运行证据，真实 SAP/SSO/远程兼容尚未重测，见 [兼容报告](../deployment/README.md)。实现全部位于 SAP 场景目录，OpenCode 核心和普通智能体执行器不变。

旧 screen 模式的 `server.ts` 由场景按会话启动，通过 stdin 接收私有参数。`host.ts` 组合 canonical Session V2 HTTP 路由、固定配置和 ApplicationTools，共用 Effect memo map。每个绑定使用独立数据库及 loopback host，前置网关校验绑定。Location 的 SAP agent 就绪后才接受 prompt。

`tools.ts` 注册页面读取、导航、填写、表格滚动和 MCP 只读工具。桥从框架 service/session/message/call 上下文解析唯一浏览器；模型不能指定用户、浏览器或凭据。取消通知桥停止排队动作，不承诺撤销已经派发的动作。没有保存、过账和删除工具。

导航参数为 `revision`、`transaction`，只接受 SPRO/ME21N，发送 Enter 前复核同一命令字段焦点和控制代次。自动输入共用场景效果登记，未知页面、页签、帮助、提交及原始调试操作均拒绝。表格滚动使用 `sap_page_scroll(revision, table, direction)`，direction 为 up/down/left/right，table 只能来自当前有界观察；不能指定任意坐标或滚轮量。读回未证实目标效果时停止，不自动重试，且不把可见变化当作 SAP 业务校验成功。

`sap_purchase_order_read({document_number})` 只接受非全零的十位单据号，调用本绑定所属 MCP 和场景 Client，不能指定 SQL、expected、连接 ID、凭据或身份。工具最多等待 110 秒，服务端固定三表两轮 count/read/count（18 次），读取最多 90 秒；超出 32,000 UTF-8 字节的完整模型投影拒绝而不截断。返回首批标准 NB 物料订单投影及两轮计数/摘要，不是事务快照；条件、税额、伙伴、GUI expected 和 TCURX 未核验，`business_validated`、`complete_business_document`、`submission_authority` 均为 `false`。未找到不能推断此前未保存，不能自动重存；私有 `.compare` 不接提交消费者。

页面工具观察 `aria-invalid`；编辑器或 grid cell 显式错误或未知状态时，字段值相同也拒绝接受，无标记保持 `unavailable`。该判定不证明业务成功。

`backend/native_events.py` 将该版本的 session.next.*、textID、timestamp 转成 Web reducer 接受的事件、ordinal、created，并补全输入提升和执行结束事件，修复界面一直“思考中”。只转换流投影，不改持久历史或原生 Web 构建。

两个固定 MCP 登记于专属 host 配置；按用户授权，测试项目 opencode.json 也登记端点。工作台实际调用通过 sap_mcp_read 和受信凭据桥，密码和 connection_id 不写入项目或工具正文。旧 CLI 的 MCP 注册接口不等于 canonical Session V2 工具注册。

```sh
SAP_OPENCODE_ROOT=/path/to/rsmcode/opencode bun test Scene/sap_workbench/opencode_adapter/host.test.ts Scene/sap_workbench/opencode_adapter/deployment.test.ts Scene/sap_workbench/opencode_adapter/purchase_order.test.ts
```

上一轮 4 项 / 81 个断言通过，测试真实 HTTP/Session V2/工具/取消链路及订单工具参数、结果和失败投影，并把真实事件交给场景适配器及未修改的 Web reducer，验证文字、工具结果和完成状态。本轮未重跑 Bun/TypeScript；供应商及 SAP 桥为测试替身，历史真实模型、SAP 和 MCP 证据见 change 的 evidence/live-workbench.md。

修复前全适配器验证命令（供应商替身和隔离项目，不执行 SAP）：

```sh
SAP_OPENCODE_ROOT=/path/to/rsmcode/opencode bun test Scene/sap_workbench/opencode_adapter
```

修复前 6 项、129 个断言通过，包含 native 全局/项目配置、多模型/多 agent、四项技能、命令及真实 skill/bash 执行；历史结果不替代本次导航验证。本次新增原生导航与指引测试共 3 项、30 个断言通过，完整命令和结果见上方本次验证记录。
