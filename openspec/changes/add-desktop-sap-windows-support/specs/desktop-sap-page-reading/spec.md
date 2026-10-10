## MODIFIED Requirements

### Requirement: macOS 当前页面按需读取

系统 SHALL 在 macOS 与 Windows 桌面工作台提供 `sap_page_read`，通过现有 OpenCode 会话和场景工具通道读取左侧 SAP 页面。工具 SHALL 使用受信执行上下文解析场景目标，不接受模型指定 URL、脚本、选择器、身份或窗口。普通成员具有既有使用权限和会话所有权时 SHALL 可调用，不要求管理员身份、独立登录或额外读取授权步骤。打开工作台和页面变化 MUST NOT 自动触发模型调用。

两端宿主 SHALL 共用同一请求/回执协议、同一受信执行上下文解析和同一固定 DOM 提取逻辑；Windows 支持 MUST NOT 引入第二套读取通道、额外授权步骤或不同的提取语义。

#### Scenario: 普通成员询问当前采购订单
- **WHEN** 获授权普通成员在已关联的 macOS 或 Windows 工作台中询问当前页面，助手调用读取工具
- **THEN** 返回该工作台 SAP 页面内容到同一 OpenCode 会话，不创建另一套助手、模型配置或浏览器

#### Scenario: 模型试图改变目标
- **WHEN** 工具收到非空业务参数或尝试传入脚本、URL、选择器及其他会话标识
- **THEN** 拒绝不受支持的参数，不改变实际读取目标

### Requirement: macOS 启用与兼容回退

本能力 SHALL 在已验证的 macOS / Windows 宿主方法可用且读取开关开启时报告可用。普通 Web、Linux 宿主和旧客户端 SHALL 返回不支持，MUST NOT 因此阻断原有 SAP 人工操作、OpenCode 对话或独立后台查询。截图/OCR 及其他后续能力 MUST NOT 成为本次交付的前置。

启用 SHALL 基于真实桌面宿主桥、实际项目插件及既有边界测试；配置存在或模拟回执不能代替该证明。关闭功能 SHALL 拒绝新请求、清理 pending 并停止交付在途结果，不迁移或复制历史数据。

#### Scenario: 当前 macOS 构建通过验收
- **WHEN** 真实 macOS 读取路径及必要边界已验证，Windows 宿主已在同一门禁下实现并通过平台参数自动化覆盖
- **THEN** 两端可一并交付并开启读取；真实 Windows + SAP 现场验收尚未补齐时，MUST NOT 宣称 Windows 已通过现场验证

#### Scenario: 普通 Web 或旧桌面客户端
- **WHEN** 同一平台会话在不支持读取的入口调用页面工具
- **THEN** 明确说明该入口不可读，不选择其他桌面窗口代读，其他已有功能保持可用

#### Scenario: 运行中关闭开关
- **WHEN** 管理员关闭读取开关或当前工作台失效
- **THEN** 新请求被拒绝，在途结果不交付；重新开启后只处理新调用，不重放旧内容

#### Scenario: Linux 宿主仍被拒绝
- **WHEN** 宿主平台不是 `darwin` 或 `win32`，调用读取
- **THEN** 以 `page_read_unsupported` 拒绝，且不因新增 Windows 支持而放宽其他平台门禁
