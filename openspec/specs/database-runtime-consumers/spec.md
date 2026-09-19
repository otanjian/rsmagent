# database-runtime-consumers Specification

## Purpose
定义 `identity_mode=database` 下 Web 对话、文件/上传、调度、OpenAI 兼容 API、MCP 预热、Agent Bridge 与外部通道启动等运行消费者的开放边界：拆除既有整体关闭门，并要求每次执行按当前身份重新授权。
## Requirements
### Requirement: database 模式开放已适配运行消费者

当 `identity_mode=database` 时，系统 SHALL 开放已适配的运行消费者，包括 Web 消息/流/轮询/取消、文件上传与文件服务、语音 ASR（若部署启用）、调度管理与执行触发、OpenAI 兼容 API、MCP 预热以及 Agent Bridge 运行时初始化。上述入口 MUST NOT 再因「database 模式」本身返回 `503 database_unavailable` 或等价整体关闭码；未登录或无权请求 SHALL 分别返回 `401`/`403` 等身份与授权错误。

「开放」SHALL 对浏览器控制台真实可达：已登录且获权的用户在控制台发起的附件上传（含粘贴、文件选择与目录选择）与上传回读 SHALL 成功，MUST NOT 因浏览器链路缺少租户选择而返回 400，也 MUST NOT 被前端静默丢弃。对于浏览器结构上无法附加租户选择头的传输（如以 `<img>`/`<audio>` 读取上传响应的子资源请求），其租户 SHALL 改由被寻址资源派生（见 `console-route-lifecycle`），并仍 SHALL 校验调用者的成员资格与资源授权。上传响应中用于回读的地址 SHALL 与写入目标同源（同一被授权智能体），使回读不会落到另一个智能体而 404。

文件服务中的浏览器传输 SHALL 在已登录且获权的成员下可用，包括聊天附件上传与回读，以及 Agent 生成文件工件的下载与预览；其中浏览器原生发起、结构上无法携带租户头的请求（`<img>`/`<a download>` 等）SHALL 由被寻址资源派生租户，MUST NOT 以「未选择租户」400 拒绝，MUST NOT 静默失败。

控制台工作区面板的传输（`GET /api/workspace/tree|search|resolve|meta|read`、`POST /api/workspace/write`）SHALL 对已登录且获权的租户成员可用：浏览、预览与保存本租户工作区文件 MUST NOT 因 database 模式返回 `503`，MUST NOT 以「未选择租户」400 拒绝（控制台同源 `fetch` 会携带租户选择），也 MUST NOT 以成功状态静默吞掉拒绝。

#### Scenario: 已登录且获权用户可发起 Web 对话

- **WHEN** 有效租户成员持有 `chat.use` 与目标 Agent 的 `agent.use`，并对所选模型持有 `model.use`，向 Web 对话入口发送消息
- **THEN** 请求进入既有对话执行路径，不因 database 模式被 503 短路

#### Scenario: 匿名访问对话传输

- **WHEN** 匿名客户端请求 Web 对话消息或流式入口
- **THEN** 系统返回 `401`（或等价未认证），不返回 database 整体不可用 503，也不执行模型调用

#### Scenario: 控制台附件上传与回读

- **WHEN** 已登录且获权的用户在控制台粘贴或选择文件，其请求无法携带租户选择头经由控制台上传链路
- **THEN** 上传成功并返回与写入智能体同源的回读地址；以该地址读取上传内容成功，不返回 400 missing_tenant，也不被前端静默丢弃

#### Scenario: 上传回读不得落到其他智能体

- **WHEN** 用户在与非默认智能体的会话中上传附件并回读
- **THEN** 回读按上传时的被授权智能体解析，不落到租户默认智能体而返回 404

#### Scenario: 已登录且获权成员下载或预览本租户 Agent 工件

- **WHEN** 有效租户成员在控制台点击其本租户 Agent 生成文件的下载按钮，或在预览面板打开该文件
- **THEN** 下载导航与预览请求分别返回文件内容，不因租户头缺失返回 400 missing_tenant，也不显示 not found

#### Scenario: 已登录成员使用工作区面板

- **WHEN** 有效租户成员在控制台对话页打开右侧工作区面板，浏览文件树、预览文件或保存编辑
- **THEN** 请求分别返回成功，不返回 `503 database_unavailable`；未登录时返回 `401`，无有效租户成员资格时返回 `403`

### Requirement: 能力投影报告真实开放状态

`/auth/context` 及等价消费者报告 SHALL 将已开放的 chat/files/scheduler/openai_api/channels/mcp、记忆浏览、项目浏览和 Desktop 数据库业务能力按其实际验收范围标为可用，同时将页面和对象的读取、配置、执行分别按当前授权投影。支持范围、部署启用状态、外部连接就绪与当前用户许可 SHALL 分别报告；已开放功能不得继续以固定 `deferred`/`consumer_closed` 表示未实现。投影 MUST NOT 单独授权，每个入口 SHALL 独立重验。

#### Scenario: 能力摘要不再把聊天标为延期
- **WHEN** 有效成员请求 `/auth/context`
- **THEN** chat 及已经验收开放的其他消费者不以 `deferred`/`consumer_closed` 作为不可用原因；缺少权限以权限或资源原因说明

#### Scenario: 定时管理已开放但目标不可运行
- **WHEN** 本人可读取任务，但所选智能体已停用或执行资源未授权
- **THEN** 任务列表和适用的暂停/删除动作保持可用，运行动作说明目标或授权原因，不把整个调度能力标为未实现

#### Scenario: 桌面适配和提供方就绪不同
- **WHEN** Desktop 数据库业务已验收，但某一微信实例尚未连接
- **THEN** Desktop 能力保持真实开放状态，该实例单独报告连接状态，不用一个全局关闭值隐藏两者差异

### Requirement: 调度在触发前重验授权

调度任务 SHALL 在创建时记录触发所需的 `user_id`、`tenant_id` 与目标资源标识；每次触发执行前 SHALL 重新解析成员资格、功能权限与资源 grant。重验失败 MUST NOT 调用模型或产生新的工具副作用，并 SHALL 记录可诊断的失败原因。

#### Scenario: 撤权后到点触发
- **WHEN** 任务创建时用户有权，触发前其 `agent.use` 或 `chat.use` 已被撤销
- **THEN** 本次触发被跳过或标记失败，不执行 Agent 运行

### Requirement: 通道按显式实例启动且不限为仅 web

系统 SHALL 按配置与显式渠道实例记录解析并启动已启用的外部 IM 通道，MUST NOT 在启动阶段将通道列表强制过滤为仅 `web`。单通道启动失败 SHALL 记录错误且不得阻止 Web 控制台启动。通道实例 MUST 为显式登记记录，MUST NOT 由旧 `channel_type` 或单租户配置隐式合成。

#### Scenario: 配置含飞书实例时启动
- **WHEN** 部署的配置或实例记录包含已启用的飞书（或其他 IM）通道
- **THEN** 进程尝试启动该通道；失败时记录错误，Web 控制台仍可用

#### Scenario: 未显式登记实例
- **WHEN** 配置只有旧 `channel_type` 字段而没有显式渠道实例记录
- **THEN** 系统不隐式合成实例，不启动该通道

### Requirement: Desktop 按 database 身份适配

Desktop SHALL 通过数据库登录取得独立 AuthSession，并以同一会话值作为 `Authorization: Bearer` 访问业务接口，MUST NOT 继续使用共享密码或旧 `cow_auth_token`。系统 MUST NOT 对 Desktop 开放匿名或免登录入口；旧认证客户端 SHALL 被明确拒绝并提示需要重新登录，MUST NOT 回退 legacy 赋权。

#### Scenario: Desktop 使用数据库账号登录
- **WHEN** Desktop 用户以有效账号通过登录接口认证并携带会话 Bearer 请求业务接口
- **THEN** 系统按该会话解析身份与租户并正常提供服务

#### Scenario: Desktop 旧认证连接
- **WHEN** Desktop 使用共享密码或旧 token 连接服务
- **THEN** 系统拒绝或明确提示需要重新登录，不回退 legacy 赋权

### Requirement: 缺口消费者真实路由完成逐项恢复

database 缺口恢复 SHALL 包含 `GET /api/scheduler`、`POST /api/scheduler/run|toggle|update|delete`、`GET /api/memory`、`GET /api/memory/content`、`GET|POST /api/weixin/qrlogin` 和 `GET /api/projects/browse` 共 10 个方法。适用依赖通过后，合法获权请求 SHALL 进入受保护业务服务，不得仅因 database 身份模式返回整体关闭503。开放必须包含真实路由、授权、对象范围与客户端传输，不能仅改变注册策略或模拟 handler 成功。

#### Scenario: 获权用户经真实应用访问恢复入口
- **WHEN** 已完成相应身份、资源及部署条件的用户通过实际 Web 或 Desktop 入口使用上述功能
- **THEN** 请求进入正确作用域的业务路径，结果与对象动作投影一致，未授权调用仍被拒绝

#### Scenario: 只有组件测试通过
- **WHEN** 直接调用某个处理器成功，但实际应用路由仍关闭或客户端缺少必要上下文
- **THEN** 该恢复项验收不通过，不计为功能已覆盖

### Requirement: 按动作投影跨版本功能可用性

当前租户的能力响应 SHALL 向客户端提供逐动作的 available 和 reason，覆盖上下文用量、压缩、任务创建、投递目标选择和运行记录操作。该投影 SHALL 与服务端实际路由开放状态一致，MUST NOT 授予用户权限或替代每次请求的对象授权；同一功能 MUST NOT 因独立维护两份开关而产生相反状态。

#### Scenario: 分批发布
- **WHEN** 上下文能力已验收开放而调度新功能尚未开放
- **THEN** 投影分别报告真实状态，客户端仅发开放功能的请求，直接请求关闭功能也被服务端拒绝

#### Scenario: 旧服务器没有新字段
- **WHEN** 新客户端连接没有动作级声明的旧服务器
- **THEN** 新能力默认为未开放，不影响原有聊天、历史和任务管理接口

### Requirement: 关闭和切换使客户端停止无效请求

Web SHALL 在能力关闭、登出或租户切换时停止相应轮询并失效缓存；迟到响应 MUST NOT 恢复旧身份内容。客户端 SHALL 区分成功空数据、功能未开放、无权限和请求失败，MUST NOT 把所有错误转换成空列表。

#### Scenario: 轮询过程中切换租户
- **WHEN** 任务通知轮询未完成时切换租户
- **THEN** 原轮询结果不显示，后续请求使用新租户声明，旧轮询不继续运行

#### Scenario: 请求被拒绝
- **WHEN** 已开放功能的请求返回 403 或服务端故障
- **THEN** 页面分别显示权限或故障状态，不显示“暂无记录”，也不扩大请求范围重试

### Requirement: 分批开放依赖真实验收

每批能力 SHALL 在业务正向、授权拒绝及真实入口验收通过后开放；仅有接口占位、模拟返回或文档完成 MUST NOT 作为开放依据。回退 SHALL 首先关闭相应入口与服务能力，保留原有业务数据和其他已交付能力。

#### Scenario: 真实客户端尚未验收
- **WHEN** API 模拟用例通过但真实入口尚未完成该能力验收
- **THEN** 该批能力保持未通过，不将实现状态标成完整交付

