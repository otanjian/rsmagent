## MODIFIED Requirements

### Requirement: 权限目录有限且具有稳定元数据

系统 SHALL 保留原九项权限ID并增加本次明确登记的 skill.read/use/edit/enable、tool.read/execute/configure、model.read/use、agent.use/edit/enable、chat.use 十三项功能动作，并登记 `history.read_all`（查看指定智能体全部会话），提供稳定id、group、label、description、scope和assignable元数据。知识库写入 MUST NOT 作为可分配的目录项存在：目录中不再包含 `knowledge.write`，知识库写入改由数据根归属与智能体归属判定（见 `tenant-knowledge-console`），MUST NOT 被解释为读取能力的替代或包含关系——读取仍由 `knowledge.read` 独立判定。自定义角色 SHALL 仅能保存允许分配的目录项，未知ID、客户端通配符、平台标识或身份管理员资格不得保存。平台all由权威系统资格派生，不写普通角色；不按前缀推导权限，不预登记SSO或未适配客户端权限。

#### Scenario: 展示并选择登记权限

- **WHEN** 租户管理员为自定义角色查看和选择可分配权限
- **THEN** 服务端返回分组、用途及作用域明确的目录，其中「知识」分组只提供「查看知识」的 `knowledge.read`，不返回任何知识写入权限项，保存后仅授予所选择且允许分配的稳定权限 ID

#### Scenario: 授予知识写入权限

- **WHEN** 租户管理员尝试为自定义角色选择知识写入权限，或角色写请求显式包含 `knowledge.write`
- **THEN** 目录中不存在该可分配项，系统按尚未登记 ID 拒绝整次变更，不保存部分权限；知识库写入能力 MUST NOT 因角色配置而获得

#### Scenario: 提交未知或不可分配权限

- **WHEN** 角色写请求包含尚未登记 ID、通配符、平台管理员标识或身份审计权限
- **THEN** 系统拒绝整次变更，不保存部分权限或将输入解释为隐式管理员资格

### Requirement: 内置角色采用显式默认权限且升级不自动扩权

系统 SHALL 以显式登记的目录项定义 `member` 与 `tenant_admin` 的内置权限集合，并 SHALL 将其调整为：`member` 为「使用型 + 可创建自有资源」集合（`tenant.info.read`、`agent.read`、`agent.use`、`agent.edit`、`history.read`、`knowledge.read`、`memory.read`、`todo.read`、`todo.write`、`skill.read`、`skill.use`、`skill.edit`、`tool.read`、`tool.execute`、`tool.configure`、`model.read`、`model.use`、`chat.use`）；`tenant_admin` 为本规范明确纳入其默认集合的已登记目录项（本次 `history.read_all` 不加入默认集合），且 SHALL 是 `member` 集合的超集。集合 MUST 以显式 id 枚举，MUST NOT 动态展开整个目录：新增目录项 MUST NOT 自动进入任一内置集合，必须显式修改集合才纳入。平台内置角色 SHALL 属于独立作用域，不进入 `member`/`tenant_admin` 的默认或显式权限并集。

内置角色的有效权限 SHALL 以其持久化权限集合为准；该角色无持久化集合时，SHALL 回落到本规范的显式默认集合。持久化集合为空 SHALL 被解释为「无业务权限」，MUST NOT 回落为默认集合。既有租户的内置角色 MUST 经版本化迁移回填为本规范的显式默认集合，迁移 MUST NOT 跨租户、MUST NOT 改动自定义角色，MUST NOT 使 `tenant_admin` 失去其集合内的任何权限。

目录移除既有权限项时，SHALL 以版本化迁移把该 id 从**所有**角色的持久化权限集合中剥离（内置与自定义一视同仁），MUST NOT 改动同一集合中的其它权限，MUST NOT 使任何角色因此获得新的权限；被剥离的 id 在剥离后 MUST NOT 再作为授权依据。

#### Scenario: 升级保留既有角色权限

- **WHEN** 已有 tenant_admin、member 和自定义角色用户在本次迁移后再次请求有效权限
- **THEN** 内置角色分别取得本规范明确纳入的默认集合，自定义角色保持原选择并遵守原资源范围，迁移不对自定义角色新增任何执行权

#### Scenario: 管理员不能短路未登记业务权限

- **WHEN** tenant_admin 尝试请求未登记或未开放的业务执行能力
- **THEN** 请求按实际路由开放状态及权限检查被拒绝，不因管理员名称、内置集合或资源可读而放行

#### Scenario: 平台管理员自动包括新目录项

- **WHEN** 平台身份有效且目录新增合法动作
- **THEN** 平台all包括该动作，普通member/tenant_admin不会因扩目录自动获得，执行仍须满足运行条件

#### Scenario: 目录扩充不扩平台内置角色默认集合

- **WHEN** 功能目录新增合法业务动作
- **THEN** 平台内置角色的显式权限集合不因此自动写入新权限，平台范围能力仍由服务端资格派生，普通角色同样不自动获得

#### Scenario: 成员取得使用型与创建型权限

- **WHEN** 有效成员在已迁移租户中请求 `/auth/context` 或发起对话
- **THEN** 其有效权限包含 `chat.use`、`agent.use`、`model.use`、`skill.use`、`tool.execute` 及 `agent.edit`/`skill.edit`/`tool.configure`，可启动与租户共享默认智能体的对话，不再因缺少 `chat.use` 被拒

#### Scenario: 管理员取得全目录且显示与生效一致

- **WHEN** 有效租户管理员请求 `/api/tenant/roles` 与 `/auth/context`
- **THEN** 其内置角色持久化集合与有效权限都等于本规范明确纳入的默认集合，新登记但未默认授予的 `history.read_all` 不因管理员身份出现，控制台显示的权限集合与接口判定一致

#### Scenario: 回填不跨租户且保留自定义角色

- **WHEN** 版本化迁移在含多个租户的库中执行
- **THEN** 仅各租户的内置 `member`/`tenant_admin` 行被回填为显式默认集合，自定义角色的权限、资源授权与默认模型保持不变

#### Scenario: 空持久化集合不回落

- **WHEN** 某租户管理员显式将某内置角色的功能权限清空并保存
- **THEN** 该角色成员的有效权限为空集合，系统不回落为默认集合，除非该成员另有其他角色提供权限

#### Scenario: 移除目录项后存量角色不再持有

- **WHEN** 权限目录移除某项后，版本化迁移在含该 id 的内置与自定义角色上执行
- **THEN** 每个角色的持久化集合中该 id 被剥离、版本号递增，同一集合中的其它权限保持不变，且该 id 在后续授权判定中不再产生任何效果

#### Scenario: 新增全部会话读取不自动提权
- **WHEN** 部署升级登记 `history.read_all`
- **THEN** member、tenant_admin 和自定义角色的现有权限、grants 及显式空集合不变；管理员须按原角色授权流程明确授予新功能和目标智能体动作


### Requirement: 多角色权限实时合并且受资源和执行约束

有效成员的功能权限 SHALL 取当前租户有效角色的显式并集；资源动作grant亦取允许并集，再与租户资源上限、原归属/owner、Agent限制和消费者状态共同校验。平台all对既有动作仅跳过功能和资源选择条件；本次跨用户历史例外仍要求目标 Agent 的显式 grant。每次请求及实际调用 SHALL 验证最新授权，撤销最后来源后下一次拒绝，仍有来源则保留；部门变化不隐式授权。模型默认值不得产生资源grant，授权空集合必须拒绝，普通资源授权不得扩大个人历史/待办等数据范围；只有本规范明确登记的 `history.read_all` 与目标 Agent 的 `history_read_all` grant 可在当前租户内扩大该共享普通 Agent 的历史只读范围。

具体资源grant门槛 SHALL 仅适用于本次菜单、技能、工具、模型、Agent，其他数据继续按原功能和owner判断，本人 history/todo 不增加 grant 要求；跨用户历史例外使用目标 Agent 的专用动作，不建立逐会话 grant。

#### Scenario: 撤销一个重复权限来源
- **WHEN** 成员两个角色均授予同一权限，其中一个角色被撤销
- **THEN** 当前权限仍可由剩余角色提供；撤销最后有效来源后下一请求被拒绝，旧能力上下文不能延长授权

#### Scenario: 有功能权限但缺资源资格
- **WHEN** 成员具有 history.read 或 todo.read，但目标记录属于其无权访问的其他用户
- **THEN** 系统拒绝操作，功能权限不能扩大私有历史、待办或其他资源的可见范围

## ADDED Requirements

### Requirement: 指定智能体全部会话权限独立登记

`history.read_all` SHALL 归入“会话”分组，scope 为 tenant、assignable 为 true，描述明确限定“当前租户中被明确授权的共享普通智能体全部已保存会话，只读”。该动作 SHALL 与既有 `history.read` 及目标 `agent:<id>` 的 `history_read_all` grant 共同校验，缺少任一条件不得跨 owner 读取；默认本人历史仍按原规则。

新权限 MUST NOT 隐含原会话修改、生成报告、文件读取或智能体使用权，不从 `agent.read/use/edit/enable`、管理员名称或平台 all 单独派生。生成报告需另外满足既有运行/模型依赖；功能目录和页面能力投影不代表尚未验收的读取已开放。

#### Scenario: 功能和目标不完整
- **WHEN** 角色只获得新功能权限但未选择目标智能体，或仅选择资源动作却缺少必要功能权限
- **THEN** 无法读取他人历史，本人已有历史访问不受影响

#### Scenario: 只读角色尝试生成报告
- **WHEN** 角色能查看目标智能体全部会话但缺少现有模型/运行权限
- **THEN** 可查看问答，不能通过生成按钮或直接接口触发模型
