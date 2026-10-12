## MODIFIED Requirements

### Requirement: MCP 按传输类型配置和探测

MCP SHALL 支持 stdio、SSE 和 Streamable HTTP。远程连接 SHALL 配置服务 URL、认证方式与多个请求头；stdio SHALL 配置命令、独立参数列表及环境变量，请求头和环境变量的值 MUST 采用凭据机制。传输相关字段 SHALL 条件校验，测试 SHALL 完成真实握手和工具列表发现并返回工具摘要，不调用业务工具。未通过本地进程隔离验收的部署 MUST 禁止 stdio 启动及测试，不以管理员身份豁免。配置与探测 SHALL 接受 master 编辑器支持的连接字段，并按本规范及连接管理规范校验和生效，不能只在界面保存未被运行时使用的字段。

#### Scenario: 切换到 stdio
- **WHEN** 用户选择 stdio 并提交命令及参数
- **THEN** 系统不要求远程 URL，以独立参数执行受控进程；隔离条件不满足时拒绝启动并说明原因

#### Scenario: 远程握手成功但发现失败
- **WHEN** MCP 服务握手成功而工具列表请求失败
- **THEN** 显示发现失败，不把旧工具数量作为本次发现结果

#### Scenario: 远程服务需要多个请求头
- **WHEN** 用户配置两个合法请求头并测试或使用连接
- **THEN** 受控客户端携带这两个请求头，普通配置及响应不包含其秘密值，仍执行目标地址和传输保留头校验

## ADDED Requirements

### Requirement: master MCP 配置映射到唯一连接记录

系统 SHALL 接受并一致处理 `name`、`type`、`command`、`args`、`env`、`url`、`headers`、`scope`、`tool_name_prefix`、`timeout` 和 `disabled`，将其转换为同一平台或租户连接及其凭据；master 配置中的 `scope` SHALL 始终表示 OAuth scope，不能决定连接归属。传输别名及缺省推断 SHALL 与固定 master 基线一致。既有连接 SHALL 按稳定 ID 更新，读取、编辑、测试和实际调用 SHALL 使用同一权威数据；MUST NOT 通过 JSON 导入重新启用全局文件配置写入或建立第二份运行时配置。未知或不适用字段 SHALL 明确拒绝。

#### Scenario: 传输别名和地址别名
- **WHEN** 导入使用 `streamable-http`、`streamable_http`、`streamablehttp` 或 `http` 以及 `serverUrl` 的远程配置
- **THEN** 规范化为同一 Streamable HTTP 连接语义，回读为约定的 master 格式，地址保持一致

#### Scenario: 未指定传输类型
- **WHEN** 单条配置未提供 `type`
- **THEN** 按 master 基线有 URL 时推断为 SSE，否则为 stdio，并校验对应必填字段

#### Scenario: OAuth scope 不能选择租户
- **WHEN** JSON 中包含 `scope` 或试图附加 tenant/owner 字段
- **THEN** `scope` 仅作为 OAuth 配置解析，租户/owner 字段被拒绝，连接归属仍由当前授权操作决定

#### Scenario: 编辑既有连接
- **WHEN** 用户修改配置并保存
- **THEN** 保持原稳定 ID、继承关系、智能体分配和已有授权关联，并使会话与发现缓存按既有规则失效

### Requirement: MCP 高级字段在受控运行时生效

`tool_name_prefix` SHALL 保持既有工具身份兼容规则。`timeout` SHALL 为正整数秒数，未配置时沿用现有默认值；实际测试或执行期限 SHALL 取用户配置、部署限制和本次剩余期限中的最严格值，并向用户说明有效限制。OAuth `scope` SHALL 作为独立字段传入现有授权流程，不能与 OAuth 提供方标识混用；改变授权相关配置 SHALL 按既有版本绑定规则要求重新授权。需要授权 SHALL 明确呈现，草稿测试 MUST NOT 静默启动交互授权或借用其他连接令牌。

#### Scenario: 用户配置短超时
- **WHEN** 用户设置 5 秒超时，部署允许更长等待
- **THEN** 测试和使用遵守更短的配置期限，超时显示真实失败，不仍固定等待默认 30 秒

#### Scenario: 配置超过部署期限
- **WHEN** 用户配置的超时高于部署上限或本次剩余期限
- **THEN** 实际尝试受较短期限约束，界面显示有效限制，配置不能绕过运行时预算

#### Scenario: 前缀和 scope 保存后回读
- **WHEN** 用户保存工具名前缀和显式 OAuth scope 并重新打开连接
- **THEN** 字段回读一致，工具身份遵循现有映射，授权请求采用对应 scope，提供方标识保持独立

#### Scenario: 尚未完成 OAuth 授权
- **WHEN** 测试返回需要授权
- **THEN** 显示需要授权并允许保存合法配置，后续通过绑定当前连接和主体的现有授权入口完成授权，不将该测试标为握手成功

### Requirement: 存量 MCP 配置兼容且不丢失凭据语义

系统 SHALL 兼容读取存量单请求头、环境变量及 OAuth 配置，以保持原有效行为的映射呈现给新编辑器；首次普通字段编辑 MUST NOT 删除未修改的秘密或重新分配连接 ID。旧单请求头和新多请求头的有效表示 SHALL 有明确优先级，显式清除请求头组后 MUST NOT 回退使用残留旧值。存量 OAuth 提供方值曾被作为 scope 使用时，升级 SHALL 明确区分并保留旧有效 scope，不能无提示改变授权请求。存量不符合新建名称约束或有重名的记录 SHALL 仍能按稳定 ID 读取和编辑，不因新建校验而强制改名或合并。

#### Scenario: 旧单请求头连接仅修改前缀
- **WHEN** 用户打开旧版创建的连接并仅修改工具名前缀
- **THEN** 原请求头值和其他配置继续生效，不要求读取或重新输入明文，不复制连接

#### Scenario: 旧 OAuth 字段兼容
- **WHEN** 存量 OAuth 记录没有独立 scope，但旧运行时曾使用提供方字段作为 scope
- **THEN** 兼容转换保留原有效 scope，后续显式修改采用独立 scope，并遵守重新授权规则

#### Scenario: 存量名称不符合 master 新建约束
- **WHEN** 原连接名称含中文或与另一存量连接重名
- **THEN** 用户仍可按稳定 ID 编辑该连接，其他记录不受影响，新建继续执行新建名称校验
