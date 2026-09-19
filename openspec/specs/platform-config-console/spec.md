# platform-config-console Specification

## Purpose
在 database 多租户模式下，向平台管理员开放对全局系统配置（`/config`）与模型配置（`/api/models`）的读取与写入控制台，同时保持租户管理员与普通成员无权访问的边界；legacy 模式访问控制保持不变。该能力用于将这两类 `closed` 消费者恢复为平台管理员的受控配置入口。
## Requirements
### Requirement: 系统配置与模型配置控制台归类为平台域

系统 SHALL 将 `/config` 与 `/api/models` 的 GET/POST 在路由方法策略中归类为 `platform`（平台管理员），MUST NOT 再作为 `closed` deferred 消费者在 database 模式返回 `503 database_unavailable`。未登录或非平台管理员访问时，SHALL 根据请求身份返回 `401`（无有效会话）或 `403`（已登录但无平台管理员资格），MUST NOT 进入下游 handler 产生配置写入或 Bridge 重置副作用。

#### Scenario: platform 策略可匹配
- **WHEN** 对 `/config` 或 `/api/models` 发起 GET/POST 请求
- **THEN** 路由策略匹配到 `platform`，不再作为 `closed` 消费者被短路成 503

#### Scenario: 未登录访问
- **WHEN** 匿名请求访问 `/config` 或 `/api/models`
- **THEN** 系统返回 `401 unauthorized`，不产生任何配置读取或写入副作用

#### Scenario: 非平台管理员访问
- **WHEN** 已登录但 `is_platform_admin=false` 的用户请求 `/config` 或 `/api/models`
- **THEN** 系统返回 `403 forbidden`，不会获取全局配置数据或执行写入

### Requirement: 平台管理员可读取全局配置与模型配置

已认证且具备平台管理员资格的请求 SHALL 能够读取全局系统配置与模型配置。GET `/config` SHALL 返回系统配置投影（含标题、模型、`bot_type`、api_base、掩码后的 api_keys、providers 等现契约字段，MUST NOT 再暴露 `web_password`）；GET `/api/models` SHALL 返回 `{status, providers, capabilities}`。返回内容 MUST NOT 泄漏未掩码的 api_key 明文或敏感完整凭据。

#### Scenario: 平台管理员读取系统配置
- **WHEN** 平台管理员携带有效会话 GET `/config`
- **THEN** 系统返回 `200` 与系统配置投影，api_key 以掩码形式呈现，不返回可复用的明文密钥

#### Scenario: 平台管理员读取模型配置
- **WHEN** 平台管理员携带有效会话 GET `/api/models`
- **THEN** 系统返回 `200` 与 `{status:"success", providers:[...], capabilities:{...}}`，包含厂商凭据状态与能力当前选择

### Requirement: 平台管理员可写入全局配置与模型配置

已认证且具备平台管理员资格的请求 SHALL 能够通过 POST 写入全局配置与模型配置。POST `/config` SHALL 接受 `{updates}` 并保存到全局配置；POST `/api/models` SHALL 支持既有 action（`set_provider`、`delete_provider`、`set_custom_provider`、`delete_custom_provider`、`set_active_custom_provider`、`set_capability`、`set_voice_reply_mode`、`set_search_credential`）。写入后 SHALL 使相关 bot 路由按需重建，并保持 `config.json` 现有字段结构不变。

#### Scenario: 平台管理员保存系统配置
- **WHEN** 平台管理员 POST `/config` 提交 `{updates: {...}}`
- **THEN** 系统将更新写入全局配置并返回成功；无有效更新时按现契约返回 `no updates provided`

#### Scenario: 平台管理员设置模型能力
- **WHEN** 平台管理员 POST `/api/models` 提交 `{action:"set_capability", capability, provider_id, model}`
- **THEN** 系统持久化对应 provider/model 到全局配置并返回 `{status:"success"}`，相关语音/模型路由被重置

#### Scenario: 未知 action 被拒绝
- **WHEN** 平台管理员 POST `/api/models` 提交无法识别的 action
- **THEN** 系统返回 `{status:"error", message:"unknown action"}`，不产生配置写入

### Requirement: 租户级身份无法借助管理员资格越权访问全局配置

租户本身（`tenant`/`personal` 域）的身份解析与权限 SHALL 不因导航、前端可见性或任何残留租户选择而获得全局配置访问权。全局系统配置与模型配置的访问权 SHALL 独立判定，MUST NOT 由 `tenant_admin` 或普通成员资格短路；`must_change_password` 受限会话亦 SHALL 被拒绝访问该类控制台（复用受限会话的拒绝规则）。

#### Scenario: 租户管理员不获得全局配置权限
- **WHEN** 具备 `tenant_admin` 资格但非 `is_platform_admin=` 的用户访问 `/config` 或 `/api/models`
- **THEN** 系统返回 `403 forbidden`，不会因租户管理员资格豁免平台域检查

#### Scenario: 受限会话被拒绝
- **WHEN** 处于强制改密（`must_change_password=true`）受限状态的会话请求 `/config` 或 `/api/models`
- **THEN** 系统按受限会话规则拒绝访问，不返回全局配置或执行写入

### Requirement: 模型目录编辑保留覆盖和隐藏语义

当前 Web SHALL 向平台管理员提供按提供方编辑模型目录、隐藏/恢复模型和恢复预设的入口。保存 SHALL 保留覆盖配置与隐藏条目的区别，不把未编辑预设全部固化；失败时 SHALL 保留用户草稿并显示错误。非平台管理员 MUST NOT 通过该入口或接口修改全局模型目录。

#### Scenario: 隐藏预设后刷新
- **WHEN** 平台管理员隐藏一个预设模型并保存后刷新
- **THEN** 该模型保持隐藏，其他未编辑预设继续跟随服务端预设更新

#### Scenario: 恢复预设和保存失败
- **WHEN** 管理员恢复提供方预设，或保存过程中请求失败
- **THEN** 成功恢复时仅清除该提供方覆盖与隐藏项；失败时不显示已保存并保留当前草稿

### Requirement: 有序回退链支持多节点无损编辑

当前 Web SHALL 支持回退链添加、删除、排序和启停，并保持服务端既有 chain 配置契约。读取、保存和重入 SHALL 保留所有有效节点及其顺序，自定义提供方 SHALL 可被选择。启用空链或不完整节点 SHALL 在页面提交前明确提示，MUST NOT 静默截断已有链为一个节点。

#### Scenario: 调整多节点链
- **WHEN** 管理员将三节点回退链重新排序、保存并重新打开
- **THEN** 三个节点及新顺序保持一致，运行时按该顺序读取配置

#### Scenario: 普通成员尝试写配置
- **WHEN** 普通成员或非平台管理员的租户管理员提交目录或回退链修改
- **THEN** 请求被拒绝，不产生配置写入或模型运行时重置

