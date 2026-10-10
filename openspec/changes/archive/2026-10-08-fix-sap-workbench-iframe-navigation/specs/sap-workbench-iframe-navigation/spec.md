## Purpose

提供 SAP智能工作台的有限交易导航能力，使助手响应打开交易请求时改变用户当前看到的左侧 SAP iframe，而非另开不可见或独立浏览器，并区分导航接收与实际 SAP 页面业务结果。

## ADDED Requirements

### Requirement: 当前左侧交易导航

工作台 SHALL 提供专用工具处理明确的 SAP 交易打开请求，在当前左侧 iframe 导航，MUST NOT 新开浏览器窗口、标签页或重建右侧对话。原有 OpenCode 配置、模型、智能体、技能、MCP 和其他工具 SHALL 保留。

场景 SHALL 兼容内嵌 Web 实际选择的原生 V1/V2 工具入口，MUST NOT 为工具接入而强制切换协议或隐藏既有对话历史。导航指引 SHALL 仅在工作台进程启用。

#### Scenario: 对话打开采购交易
- **WHEN** 用户在已打开的工作台要求打开 ME21N 或 ME23N，助手调用工作台交易工具
- **THEN** 当前左侧 iframe 接收对应交易的 URL 导航，右侧会话保持不变，未新开浏览器页面

### Requirement: 绑定和失效拒绝

导航 SHALL 由受信执行上下文确定场景与会话，使用已保存 SAP 地址，仅接受交易代码参数。旧绑定、跨用户、任意 URL、已关闭页面及迟到响应 MUST NOT 操作当前工作台。未收到前端确认的命令 SHALL 有限超时并清理，不能静默重放。

#### Scenario: 无页面接收
- **WHEN** 导航请求没有得到当前工作台页面确认
- **THEN** 工具返回超时或页面不可用，不宣称打开成功；后续心跳不得重放已失效命令

#### Scenario: 切换后旧响应到达
- **WHEN** 工作台关闭或已替换，旧心跳响应才返回
- **THEN** 旧响应不能改变新的 SAP iframe

### Requirement: 导航确认含义明确

工具 SHALL 区分 iframe 导航已接收与 SAP 交易页面的实际观察；MUST NOT 从导航接收或 iframe load 推断 SAP 登录、单据读取、创建或提交成功。URL 导航可能重载页面及要求 SAP 重新登录，SHALL 在工具说明中保留该边界。

#### Scenario: SAP 返回登录或错误页
- **WHEN** 父页面已应用交易 URL，但 SAP 返回登录页或权限错误
- **THEN** 工具结果仅表明导航已应用，不报告交易页面和业务结果已确认
