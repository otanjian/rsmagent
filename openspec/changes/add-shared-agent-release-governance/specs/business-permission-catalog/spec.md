## MODIFIED Requirements

### Requirement: 权限目录有限且具有稳定元数据

系统 SHALL 保留原九项权限ID并增加本次明确登记的 skill.read/use/edit/enable、tool.read/execute/configure、model.read/use、agent.use/edit/enable、chat.use 十三项功能动作，并登记共享普通智能体发布和回滚共用的 agent.publish，提供稳定id、group、label、description、scope和assignable元数据。知识库写入 MUST NOT 作为可分配的目录项存在：目录中不再包含 `knowledge.write`，知识库写入改由数据根归属与智能体归属判定（见 `tenant-knowledge-console`），MUST NOT 被解释为读取能力的替代或包含关系——读取仍由 `knowledge.read` 独立判定。自定义角色 SHALL 仅能保存允许分配的目录项，未知ID、客户端通配符、平台标识或身份管理员资格不得保存。平台all由权威系统资格派生，不写普通角色；不按前缀推导权限，不预登记SSO或未适配客户端权限。

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

系统 SHALL 以显式登记的目录项定义 `member` 与 `tenant_admin` 的内置权限集合，并 SHALL 将其调整为：`member` 为「使用型 + 可创建自有资源」集合（`tenant.info.read`、`agent.read`、`agent.use`、`agent.edit`、`history.read`、`knowledge.read`、`memory.read`、`todo.read`、`todo.write`、`skill.read`、`skill.use`、`skill.edit`、`tool.read`、`tool.execute`、`tool.configure`、`model.read`、`model.use`、`chat.use`）；`tenant_admin` 的默认集合显式增加 agent.publish，为已登记的全部目录项，且 SHALL 是 `member` 集合的超集。集合 MUST 以显式 id 枚举，MUST NOT 动态展开整个目录：新增目录项 MUST NOT 自动进入任一内置集合，必须显式修改集合才纳入。平台内置角色 SHALL 属于独立作用域，不进入 `member`/`tenant_admin` 的默认或显式权限并集。

内置角色的有效权限 SHALL 以其持久化权限集合为准；该角色无持久化集合时，SHALL 回落到本规范的显式默认集合。持久化集合为空 SHALL 被解释为「无业务权限」，MUST NOT 回落为默认集合。既有租户 SHALL 使用版本化迁移管理默认集合。本次发布治理仅向仍等于上一版完整显式默认集合的 tenant_admin 追加 agent.publish；已收紧或空持久化集合、member 及自定义角色保持不变，并报告需要另行显式授权的管理角色。无法确认上一版默认集合时 MUST 保留原集合，不猜测回填。迁移不得跨租户、重置现有选择或动态展开目录。

目录移除既有权限项时，SHALL 以版本化迁移把该 id 从**所有**角色的持久化权限集合中剥离（内置与自定义一视同仁），MUST NOT 改动同一集合中的其它权限，MUST NOT 使任何角色因此获得新的权限；被剥离的 id 在剥离后 MUST NOT 再作为授权依据。

#### Scenario: 升级保留既有角色权限
- **WHEN** 已有 tenant_admin、member 和自定义角色在发布治理迁移后请求有效权限
- **THEN** 仍采用上一版完整默认集合的 tenant_admin 仅增加 agent.publish；其他持久化集合不变，私有 owner 可按既有维护资格发布而不增加 member 权限

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
- **WHEN** 有效成员在已迁移租户中请求授权上下文或发起对话
- **THEN** 其默认集合保留 chat.use、agent.use、model.use、skill.use、tool.execute 及 agent.edit/skill.edit/tool.configure；目标启用、已有有效发布和依赖允许时可正式对话，member 不自动获得共享发布权

#### Scenario: 管理员取得全目录且显示与生效一致
- **WHEN** 有效租户管理员请求角色列表与授权上下文
- **THEN** 采用默认集合者获得显式登记集合，已收紧者按实际持久化集合返回，页面与接口判定一致

#### Scenario: 回填不跨租户且保留自定义角色
- **WHEN** 版本化发布治理迁移在多租户库执行
- **THEN** 只对目标范围内符合上一版默认集合条件的 tenant_admin 追加 agent.publish；member、自定义及收紧/空集合、资源 grant 和默认模型不变；重复迁移不再次赋权

#### Scenario: 空持久化集合不回落

- **WHEN** 某租户管理员显式将某内置角色的功能权限清空并保存
- **THEN** 该角色成员的有效权限为空集合，系统不回落为默认集合，除非该成员另有其他角色提供权限

#### Scenario: 移除目录项后存量角色不再持有

- **WHEN** 权限目录移除某项后，版本化迁移在含该 id 的内置与自定义角色上执行
- **THEN** 每个角色的持久化集合中该 id 被剥离、版本号递增，同一集合中的其它权限保持不变，且该 id 在后续授权判定中不再产生任何效果

#### Scenario: 迁移保留空或收紧集合
- **WHEN** tenant_admin 已显式清空或收紧业务权限，发布治理迁移执行
- **THEN** 原集合保持，迁移报告需要显式授权，不恢复默认或增加 agent.publish

## ADDED Requirements

### Requirement: 发布授权采用最小动作集合

草稿规则、运行绑定配置和有限预览 SHALL 复用现有维护资格；预览另需实际 chat/model/tool 依赖，不要求正式 agent.use。共享发布与回滚 SHALL 共用 agent.publish 功能权限及对象 publish grant 或合法租户管理资格；publish 允许审阅目标发布定义和差异，不附赠 edit。私有发布与回滚 SHALL 使用当前 owner + agent.edit 的既有本人维护资格，无需逐对象 publish grant；member 默认集合不因此扩充。启停继续按原规则。功能权限为空或撤销不能由 owner 补回，不因作者或旧归属推导共享权限。本期不登记独立预览、回滚或策略编辑权限，也不提供仅预览测试者角色。

#### Scenario: 共享编辑者没有发布权
- **WHEN** 用户有共享 edit 和实际依赖资格但没有 publish
- **THEN** 可以保存合法定义/绑定和有限预览，发布与回滚均拒绝，工具配置仍不得超出资源授权上限

#### Scenario: 私有发布不扩大共享资格
- **WHEN** member 是私有 A 的 owner 且有 agent.edit，但没有共享 B 的 publish 资格
- **THEN** 可按依赖/部署条件发布和回滚 A，不能发布或回滚 B；A 仍为私有

#### Scenario: 权限被清空
- **WHEN** 私有 owner 的 agent.edit 或共享发布者的 agent.publish 失效且无其他合法权限来源
- **THEN** 后续相应发布/回滚拒绝，不因旧会话、身份标签或历史 grant 补回功能权限
