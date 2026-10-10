## Why

macOS 桌面 SAP智能工作台已经展示用户正在操作的 SAP 页面，但嵌入的 OpenCode 缺少读取该页面的工具。补齐一次按需读取，就能回答当前订单、字段、未保存输入和表格内容，无需新增助手、浏览器或存储。

## What Changes

- 本 change 只交付 macOS 的结构化页面读取。Windows 暂不具备验证环境，不纳入本次实现和验收门槛。
- 在现有项目插件中增加无业务参数的 `sap_page_read`，沿用 OpenCode 会话归属解析及场景 `dispatch` 的鉴权、调用去重、配额和审计。
- 复用工作台 heartbeat 请求/回执通道，在现有桌面宿主桥增加一个固定读取方法；从当前工作台的 SAP frame 提取 DOM 文本、字段当前值、表格及状态。
- 返回采集时间、标题、字段、表格、页签/消息以及读取范围和截断说明。以当前已渲染 DOM 为边界，不承诺整个订单或截图中每个像素的完整还原。
- 普通成员沿用现有场景使用权限；保留正确会话/frame 核验、密码过滤、超时和过期结果丢弃。
- 读取正文只进入当前 OpenCode 工具调用；不新增读取租约系统、独立对话/快照数据库、模型配置或审批流程。

截图、OCR、单独的可访问性树、Windows 支持及自动点击/填写/翻页/提交均留待后续需要时另开 change。现有 SAP 登录和证书信任策略保持不变。

## Capabilities

### New Capabilities

- `desktop-sap-page-reading`: macOS 当前 SAP 页面的一次结构化读取、有界结果和失败处理。

### Modified Capabilities

- `sap-workbench-scene`: 增加 macOS 的按需读取入口及普通 Web/其他桌面平台的不可用说明，保持原生 SAP 展示和同一 OpenCode 会话。
- `sap-workbench-session-binding`: 为已有绑定补充当前视图、读取请求及过期回执约束，不建立另一套授权或租约。

## Impact

- 桌面：`desktop/src/main/remote/host-bridge.ts`、`remote-host-ipc.ts`、`remote-container-ipc.ts`、`container.ts` 及 `desktop/src/main/remote-preload.ts`；复用发送方、来源和文档代次检查。
- 场景：`Scene/sap_workbench/frontend/workbench.js`、`backend/http.py`、`runtime.py`；读取请求采用 `navigation.py` 已有的单个 pending 请求模式，回执内容单独校验。
- 工具：`backend/project_toolkit.py`、`project/plugins/rsm-sap-workbench-navigation.js` 和现有场景 skill。旧受管浏览器的页面读取路径保持兼容。
- 提取：参考 `browser_service/dom.py` 的纯提取逻辑，去掉 DOM 观察标记及动作能力，针对当前 SAP Web GUI 页面实现。
- 数据归属：平台继续拥有身份/绑定和无正文的动作元数据；现有 SAP 页面拥有登录态及未保存输入；OpenCode 唯一保存对话和工具正文。新增 pending 结果仅驻内存，无数据库迁移。
- 基线与衔接：遵循主规范 `sap-workbench-scene`、`sap-workbench-session-binding` 及现有审计/配额要求；复用 `add-sap-workbench-coding-agent-entry`、`fix-sap-workbench-iframe-navigation` 已有接入，实施时核实真实调用路径，不把它们的任务完成状态当作本工具验收。
