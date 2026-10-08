## ADDED Requirements

### Requirement: macOS 工作台按需读取当前 SAP 页面

SAP 工作台 SHALL 在现有原生 SAP iframe 与嵌入 OpenCode 会话上增加 macOS 的按需 DOM 读取。工具可用性 SHALL 由读取开关、当前 macOS 宿主方法及合法场景关联共同决定；未支持或未关联时不能声称能读取当前页面。普通 Web、Windows 和旧客户端 SHALL 保留现有展示与对话行为，并说明页面读取不支持。

读取 SHALL 保持原生 SAP 页面和同一 OpenCode 服务/会话，不截图推流、不新建浏览器或独立存储，不自动调用模型。关闭或失败 SHALL 不妨碍 SAP 人工操作及已授权后台业务查询。本能力不新增填写、点击、滚动、提交或证书信任例外。

#### Scenario: macOS 中询问当前页面
- **WHEN** 当前 macOS 工作台已具备读取能力，用户在同一 OpenCode 会话询问页面内容
- **THEN** 助手可调用 sap_page_read 获取实际 DOM 观察，SAP 页面及未保存输入保持原状

#### Scenario: 不支持的入口
- **WHEN** 用户在 Windows、普通 Web 或旧桌面客户端询问当前 SAP 页面
- **THEN** 明确说明该入口不支持页面读取，不以后台查询或其他窗口结果冒充当前画面

#### Scenario: 读取失败或功能关闭
- **WHEN** 读取超时、页面替换或功能开关关闭
- **THEN** 展示可辨识原因，SAP 人工操作、已有 OpenCode 对话历史和独立业务查询继续可用
