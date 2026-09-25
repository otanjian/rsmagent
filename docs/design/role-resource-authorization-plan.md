# 角色分配菜单、技能、工具、模型和智能体的实现方案

## 2026-09-15 方案变更（部分已实施，2026-09-16 复核）

正式控制台对获权普通用户开放，成员与管理员共用页面、接口和业务流程；数据范围由可信租户/owner 过滤。本人私有对象可由所有权派生维护资格，依赖模型/工具/技能仍逐资源验证；公共定义及公共凭据写入另须对应管理资格。旧个人菜单授权映射至正式页面，不重置自定义角色或主动撤权。

最新方案：[统一控制台与数据范围方案](unified-console-access-plan.md)；实施契约：[unify-console-by-data-scope](../../openspec/changes/unify-console-by-data-scope/proposal.md)。

**实际状态**：本文的授权底座（两张 grant 表、`roles.model_defaults_json`、平台 all）**已实现**；本 change 在其上补了
「按 owner/scope 判定对象范围」（`auth/object_scope.py`）与「不用功能 grant 代替管理资格」（`ObjectScope.allows_public_configuration`）。
本文第 3、5 节里的资源目录表、模型策略表、集中授权服务与七个角色页签仍属**规划**（§9 逐项核对）。
判定口径见 [`evidence/8-5-doc-closure.md`](../../openspec/changes/unify-console-by-data-scope/evidence/8-5-doc-closure.md) §2.4。

---


日期：2026-09-09。基线：CowAgent 当前工作区（HEAD `27d291a1`，包含未提交内容）和本地 `../oneagent` 源码。本文是代码核对后的方案，不代表部署验收或已完成实现。承接用户要求：每个启用成员必须分配有效租户；平台管理员默认 all 权限。

## 1. 当前结论

目前具备账号、成员、租户角色和功能权限的基础，但没有完成五类资源的角色分配闭环。角色能选择权限点，不等于能选择具体技能、工具、模型或智能体。

| 对象 | 当前已有 | 当前缺少 |
| --- | --- | --- |
| 技能 | 租户技能库、启用状态、内容编辑；Agent 可配置选用技能 | 角色到具体技能的授权，以及技能使用、查看、维护分别判断 |
| 工具 | 内置工具目录、工具配置、MCP 加载 | 角色到具体工具/具体 MCP 工具的执行与配置授权 |
| 菜单 | 固定页面能力映射、部分按权限展示 | 角色可编辑的菜单/页签授权及与页面、按钮、API 一致的规则 |
| 模型 | 平台管理员可维护厂商/模型；会话、Agent 和全局模型选择 | 角色可用模型集合、默认/固定模型策略、实际模型调用时的授权 |
| 智能体 | `agent.read`、Agent 租户绑定、私有 owner 与租户共享区分 | 指定角色只能查看、使用或维护某几个 Agent |
| 平台管理员 | 平台作用域内置角色 `platform_admin` 的绑定（`users.is_platform_admin` 为派生镜像）及明确的平台管理接口 | 对全部已登记权限和上述资源动态生效的统一 all 判断 |

依据：

- [auth/policy.py](/Users/jiantan/ai_assistant/cowagent/auth/policy.py:22) 仍只有 9 项权限，角色保存拒绝未知项和通配符；member 默认 7 项，tenant_admin 默认 9 项，平台身份不参与普通租户角色权限并集。
- [auth/store.py](/Users/jiantan/ai_assistant/cowagent/auth/store.py:128) 保存 `roles.permissions_json` 和成员角色关联；`agent_bindings` 管理资源归属，没有五类资源的角色授权表或模型策略表。
- [identity-admin.js](/Users/jiantan/ai_assistant/cowagent/channel/web/static/js/identity-admin.js:914) 的角色编辑仍是功能权限多选，没有具体资源选择器。
- [AgentsHandler](/Users/jiantan/ai_assistant/cowagent/channel/web/web_channel.py:7597) 依据 `agent.read` 与租户资源投影列出智能体，没有角色到 Agent 的选择集合。
- [AgentProfile](/Users/jiantan/ai_assistant/cowagent/agent/registry.py:37) 中的模型、技能配置属于 Agent 自身能力配置，不是成员授权。

需要优先修正的两处：

1. [SkillsHandler](/Users/jiantan/ai_assistant/cowagent/channel/web/web_channel.py:6925) 和 [SkillContentHandler](/Users/jiantan/ai_assistant/cowagent/channel/web/web_channel.py:6978) 的写操作也检查 `agent.read`。目前“能看智能体”同时允许技能开关/内容写入，维护权限没有独立。正式规范 `tenant-skills-tools-console` 也写了这一行为，因此实施时必须同时修改规范和代码。
2. `/config`、`/api/models` 已按平台域开放，技能/工具目录接口已按租户域开放；[页面能力投影](/Users/jiantan/ai_assistant/cowagent/auth/service.py:851) 仍把对应管理页统一标为 consumer_closed。应分别登记配置读取、资源目录和运行能力，不能用“工具执行尚未开放”推断“工具目录也不能查看”。

## 2. OneAgent 的参考范围

| OneAgent 源码 | 可借鉴部分 | CowAgent 应补齐的部分 |
| --- | --- | --- |
| [auth/rbac.py](/Users/jiantan/ai_assistant/oneagent/auth/rbac.py:9) | 集中权限目录、使用/管理分离、admin 全权限判断 | 基于服务端平台身份派生 all，不相信前端角色名；保留多租户 Membership |
| [UserSkillPermissionsHandler](/Users/jiantan/ai_assistant/oneagent/channel/web/web_channel.py:2103) | 为用户选择可用技能，配合技能列表和提示词过滤 | 主授权对象改为租户角色；后续成员例外绑定 Membership，不写全局 User |
| [SkillManager](/Users/jiantan/ai_assistant/oneagent/agent/skills/manager.py:201) | 按允许技能过滤目录和提示词 | 技能正文读取、装配、工具调用和后台任务全部检查，过滤提示词不能替代执行鉴权 |
| [ModelPolicyStore](/Users/jiantan/ai_assistant/oneagent/auth/model_policy_store.py:17) 与 [模型解析](/Users/jiantan/ai_assistant/oneagent/models/user_model_resolver.py:240) | 角色/用户匹配、优先级、多模型能力、策略更新后缓存失效 | 可用模型集合与默认路由分开；数据库事务；模型回退仍须在允许集合内 |
| [菜单过滤](/Users/jiantan/ai_assistant/oneagent/channel/web/static/js/console.js:19582) | 同一权限用于侧栏、配置页签和按钮，隐藏空分组 | 后端统一计算菜单能力，直达路由和业务 API 各自鉴权 |

OneAgent 的技能分配主要落在用户上，模型策略用于选择模型；本次未发现其统一的“角色—具体工具/菜单/Agent”授权关系。不能将这些实现描述成完整的五类资源 RBAC，也不能把模型默认选择当作模型访问白名单。

## 3. 目标交互

在“组织与权限 → 角色权限 → 编辑角色”中提供：基本信息、功能权限、菜单、技能、工具、模型、智能体。保留现有角色列表、复制、分页、版本冲突和成员分配。

| 页签 | 用户配置内容 |
| --- | --- |
| 功能权限 | 可查看、使用、执行、编辑、启停、分配哪些功能；按模块分组 |
| 菜单 | 工作台/管理区下的具体页面和独立页签；父分组根据子项自动显示 |
| 技能 | 指定技能的查看、使用、编辑、启停；展示来源、状态及依赖工具 |
| 工具 | 内置工具及按连接分组的 MCP 工具；分别选择查看、执行、配置 |
| 模型 | 允许使用的模型；按 chat/embedding/image 等实际能力选择默认或固定模型 |
| 智能体 | 指定 Agent 的查看、使用、编辑、启停；标明当前租户和共享范围 |

所有资源选择器支持搜索、分页、已选项查看和当前租户筛选。普通角色的“全选当前结果”保存具体 ID 快照，新资源不会自动加入；平台管理员显示“全部权限（all，含后续新增）”，使用系统内置、只读的授权展示，不要求手工勾满所有分页。

增加“有效权限预览”：选一个本租户成员后，展示来自哪些角色、可用资源和不能访问的原因。分配某个 Agent 时展示其模型、技能和工具依赖；缺项提示补配，不默默附赠依赖权限。

示例：采购角色拥有对话、历史、知识和待办菜单；只能使用采购 Agent、采购查询技能、指定采购查询工具及一个获准模型。财务 Agent、发送邮件工具、模型配置页不会因此开放。

## 4. 授权规则

### 4.1 平台管理员默认 all

沿用平台作用域内置角色 `platform_admin` 的绑定作为唯一平台资格来源，由服务端派生 `authorization_mode=all`。账号上的 `is_platform_admin` 字段是同一写事务内维护的派生镜像：绑定是唯一事实来源，镜像仅供兼容读取，不单独作为授权凭证，也不参与授权判定（不一致时以绑定为准）。不把平台管理员伪装成每个租户都存在的一条租户角色记录——平台角色属于独立作用域，不进租户 `roles` 表、不进权限目录、不进 `member`/`tenant_admin` 的角色权限并集，也不允许角色保存接口写入 `*`、`platform_admin` 或平台身份标识。

- all 覆盖菜单、已登记功能动作以及技能、工具、模型、智能体资源授权，含未来新增的合法目录项；无需生成逐资源 grant。
- 平台管理员可以管理所有租户的角色与资源分配。跨租户管理使用明确目标租户的专用平台入口，校验目标和记录审计，不要求为此伪造目标租户 Membership 或授予 tenant_admin。这是对旧“只有目标租户 tenant_admin 才可做成员/角色写入”条款的明确调整。
- 成员在工作台执行租户业务时仍使用真实有效租户上下文。平台管理员同样至少归属一个有效租户；平台管理切换目标不等于切换为别人的业务身份。私有历史、待办、文件的 owner 不因 all 被改写或自动共享。
- all 跳过角色功能/资源选择限制，但仍检查账号/租户有效、强制改密、资源存在与启用、消费者已接通、凭据有效和执行约束。不存在或尚未实现的能力返回明确状态，不能因 admin 直接调用任意名称。
- 服务端每次关键操作重新确认平台资格；取消平台管理员身份后下一次操作不再持有 all。前端的 all 只是展示结果，不是凭证。

租户管理员继续管理本租户成员和角色，可分配范围受平台给本租户的资源上限约束；不自动获得平台凭据管理、跨租户管理或平台 all。普通角色按明确授权生效。

### 4.2 功能、资源、数据范围分别计算

原资源 grant 路径的计算方式如下；2026-09-15 新方案补充：本人私有对象维护由真实所有权派生，公共资源维护另须对应管理资格。两条路径都保持租户、状态和依赖资源边界，且使用同一正式业务服务。

> 2026-09-24 补充：正式控制台已提供「私有 ↔ 租户共享」自助转换入口（change `show-and-toggle-agent-visibility`）。归属人可把自己私有的智能体转为租户共享，租户管理员可指定归属人恢复为私有；转换只读写 `agent_bindings.private_owner_user_id`，不引入新的可见性列，也不修改上述两条路径的任何判定。`openspec/changes/archive/2026-09-13-add-user-personal-agent-provisioning` 中「不做个人助理的『共享给租户』入口」的说法已由该 change 覆盖，归档记录按规则保持原样。

```text
有效身份与租户
∩ 当前角色功能权限的并集
∩ 当前角色对该资源/动作授权的并集
∩ 平台允许当前租户使用的资源范围
∩ 资源原有租户、owner、共享与Agent能力范围
∩ 资源启用及消费者可用状态
```

首期采用 allow 集合，不引入角色 deny、角色继承或通用 IAM 委派；没有授权就是拒绝。多角色相同授权取并集，取消一个角色不应删除另一角色仍授予的能力。成员单独例外可在后续增加 `membership_resource_grants`，不作为本次角色分配的前置。

保留原 9 个权限 ID；新增有限目录示例：`skill.read/use/edit/enable`、`tool.read/execute/configure`、`model.read/use/policy.manage`、`agent.use/edit/enable`、`chat.use`。菜单用稳定资源 ID 与 view 动作管理，不为每个菜单另造一套角色系统。业务身份管理资格的赋予仍属于既有管理员边界，普通功能角色不能提升平台资格。

“管理”不自动包含“执行”；依赖关系以明确元数据登记。技能授权不自动授予它会调用的工具，Agent 使用权也不自动授予其模型或所有下属技能。LLM 的技能文本、工具参数和选择结果均不能充当授权依据。

### 4.3 菜单与业务权限的关系

菜单 grant 控制页面和页签可访问性；页面读能力、实现状态与菜单 grant 同时满足才显示。父菜单按获准子项生成，直接 hash 请求同样判断。

业务 API 按对应功能动作与资源判断，不要求“出现某个管理菜单”才允许聊天使用模型。例如没有“模型服务”管理菜单的人，可以在对话中使用被分配模型，但不能查看供应商凭据。菜单勾选也不会给缺少功能权限的人补出写操作。

继续使用现有导航规划的稳定 ID 和 `console_pages` 投影，不维护另一棵有独立名称/路径的数据表。页面和不同范围页签分别计算，修正目录已开放而摘要仍关闭的问题。

## 5. 数据与服务设计

保留 `identity.db`、User → Membership → Role、`roles.permissions_json`、Registry 和配置服务。资源内容与凭据不搬进授权表。

下表逐项标注**存在性**（2026-09-16 全仓检索核对）：

| 数据结构 | 主要内容与约束 | 实际状态 |
| --- | --- | --- |
| `resource_catalog` | 稳定 resource_id、kind、source_ref、scope、owner_tenant_id、状态、revision。只存资源引用与非敏感元数据，不存技能正文或模型密钥；菜单由导航登记提供虚拟目录即可 | ⬜ **规划**：全仓 0 命中 |
| `tenant_resource_grants` | 平台可分配给某租户的全局模型、工具/MCP 等资源及动作上限。租户自有资源从原归属系统派生；不重复维护 Agent 的租户 owner | ✅ **已实现**：`auth/store.py`（`_migration_11`）、读写 `auth/service.py:2039`、`:2443` |
| `role_resource_grants` | tenant_id、role_id、resource_kind、resource_id、action；同租户角色外键与组合唯一约束，角色整体版本控制 | ✅ **已实现**：`auth/store.py:681` 起（含 menu 类资源的 view 动作） |
| `model_policies` | tenant_id、role_id、capability、mode(default/fixed)、model_resource_id、priority、version；模型引用必须在可分配范围 | ⬜ **规划**：0 命中；已实现的是 `roles.model_defaults_json`（每角色每能力最多一个默认，`auth/service.py:6460`、`:6499`） |
| `tenants.authorization_revision` | 授权相关写操作同事务递增，供能力投影、目录、运行缓存失效；不能替代每次请求的当前身份检查 | ⬜ **规划**：0 命中；`tenants` 只有 `version` |

新增集中服务 `AuthorizationService`（`check_action`/`filter_resources`/`grantable_resources`/`effective_navigation`/`explain`）
**尚未存在**：当前判定由 `auth/object_scope.py`（范围）与 `auth/service.py` 的角色 grant 读取（`:2020`、`:2039`）共同承担。

资源标识规则：Agent 复用不可变 agent_id；技能区别内置来源和租户覆盖，不能只用显示名称；工具区分 builtin 和 MCP 连接 ID/工具名；模型使用服务端稳定资源 ID 关联厂商配置 ID、模型代码、能力和实例。自定义厂商已有 ID 可复用，传统厂商缺稳定 ID 时通过目录迁移建立映射。重命名不丢授权，删除后不把旧 ID 复用于另一资源。

资源目录由各资源所有者同步并在请求时验证引用是否仍有效，不成为可脱离真实资源的第二事实源。同步缺失按不可用处理，不能因 grant 存在就访问不存在/被移出的资源。

新增集中服务 `AuthorizationService`：提供 `check_action`、`filter_resources`、`grantable_resources`、`effective_navigation`、`explain`。授权写事务依次验证操作者、目标租户/角色、可分配上限、资源状态、版本，再提交 grants/策略/审计和 revision；全部成功或全部回滚。租户管理员不能仅凭知道另一个模型/工具 ID 将它授给自己。

建议接口（拟新增，非当前已有）：

- `GET /api/tenant/authorization/catalog`：当前操作者在当前租户可分配的功能和资源，分页过滤后计算总数。
- `GET/PUT /api/tenant/roles/{id}/authorization`：统一读取/保存功能、菜单和资源选择及模型策略，携带 expected_version；不拆成五个会产生部分成功的独立保存按钮。
- `GET /api/tenant/members/{id}/authorization`：获准管理员查看有效授权及来源；普通成员只查看本人。
- `GET /api/me/resources?kind=...&action=...`：返回本人业务可用资源的最小投影，不能借此获得管理配置或凭据。
- 平台端对应 `GET/PUT /api/platform/tenants/{tenant_id}/roles/{id}/authorization` 和租户资源上限入口，全部复用同一服务；平台资格由原身份服务确认。
- `/auth/context` 增补 `authorization_mode`、`authorization_revision`、有效功能及页面/动作摘要；资源大列表另按需分页，不把全部资源塞入登录响应或 token。

## 6. 五类资源实际生效的位置

| 对象 | 必须接入的检查点 |
| --- | --- |
| 技能 | 列表、正文读取、开关、编辑、提示词装配、按名称动态加载及其后续工具调用 |
| 工具 | 用户目录、传给模型的 tool schema、真实 execute、MCP 重连/动态发现后的调用、后台/委派调用 |
| 模型 | 用户模型列表、会话选择保存、Agent 初始化、每次模型调用、失败回退、摘要/子智能体及已接通的其他能力调用 |
| 智能体 | 列表、详情、编辑、发起会话、历史恢复、委派目标、定时任务执行 |
| 菜单 | 侧栏、页签、hash 深链接、首页卡片、页内创建入口及能力刷新 |

优先改 `channel/web/web_channel.py` 的实际 handler 与 `auth/service.py` 摘要，再接 `agent/skills/manager.py`、`bridge/agent_initializer.py`、`agent/tools/tool_manager.py` 和真实工具执行入口；只改 `BaseTool` 不保证覆盖 override、MCP 和委派路径，需要逐调用路径盘点。当前模型选择主要在 `bridge/agent_bridge.py`，会话/Agent/global/fallback 都须收口到授权后的解析。

特别注意现有 `SkillManager._normalize_skill_filter` 会将空数组归一为 None，None 表示全部。新授权结果中的空集合必须保持“一个也不允许”，不能直接塞进这个接口后扩大为全量。授权集合与 Agent 配置中的 None/空列表分别处理。

运行携带已验证 `RuntimeIdentity`，线程沿用 submit/wrap，委派继承调用者，不提升为目标 Agent 创建者或平台管理员。缓存至少隔离 user、tenant、agent、session 与 authorization_revision；角色撤销后下一次模型或工具调用重验。已发出的外部请求不能被宣称撤回，尚未开始的后续动作必须被拒绝。

模型解析规则：先计算授权集合，再选模型。固定策略优先；否则按显式会话选择 → 角色默认 → Agent 默认 → 租户默认 → 平台默认依次找合法候选。每一步都受允许集合约束，空集合返回无可用模型。显式选择未授权模型或违反固定策略时明确拒绝；后台回退也仅可使用允许且配置有效的候选，不能回退到未授权全局模型。

多个角色默认策略按 priority 降序、稳定 policy_id 升序决定，预览展示命中来源，不能按角色返回顺序随机选择。策略默认值不产生资源授权；固定/默认引用失效时显示具体原因，不通过异常处理开放全局模型。

## 7. 实施顺序与验收

| 阶段 | 交付内容 | 完成条件 |
| --- | --- | --- |
| A：统一授权基础 | 集中授权服务、服务端 all、有限权限增量、资源稳定 ID、授权表及事务 | 平台管理员动态全权限；普通角色拒绝伪造 all；同租户、版本、审计和最后管理员约束通过 |
| B：角色分配与菜单 | 七个角色配置页签、资源上限/角色选择、预览、菜单投影、技能读写拆分 | 保存后真实 API 与界面一致；仅 agent.read 不再具有技能维护权；目录开放状态准确 |
| C：技能/工具/Agent | 目录与装配过滤、真实执行守卫、Agent使用/维护、委派上下文 | 指定资源可用，未授权资源直调/间接调用均被拒；空集合不变全量 |
| D：模型与业务闭环 | 模型允许集合、默认/固定策略、所有模型解析与fallback；开放所需运行消费者 | 模型实际请求命中分配且不越界；撤权生效；一条完整聊天—技能—工具—模型链路通过 |

当前 database 的聊天、工具执行、调度等仍有明确关闭入口。A/B 的配置完成不能当作运行可用；C/D 必须连同所涉消费者的认证、资源归属和真实调用完成后按切片开放。Desktop、外部通道、OpenAI API 等尚未接通入口保持原状态，后续接通时复用授权服务，不能用 legacy 回退绕过。

验收至少覆盖：

1. 平台管理员无需 grants 即拥有五类授权；新增合法资源自动包括在 all；撤销平台资格下一请求生效。
2. 租户管理员只能分配本租户范围，伪造其他租户角色/模型/工具 ID 被拒；平台跨租户管理记录明确目标租户。
3. 两个普通角色的授权取并集；撤销最后来源后下一请求与下一实际调用拒绝，旧 token/页面摘要不能继续授权。
4. 菜单可见不意味着可写；隐藏管理菜单不妨碍已授权聊天使用模型；直达管理 URL 仍受控。
5. 技能 read 与 edit/enable 分离；技能没有工具权限时不可绕过；空授权集合不加载任何技能或工具。
6. 模型固定、默认、多角色优先级、会话显式选择、备用模型和策略服务故障行为一致；不返回供应商明文密钥。
7. Agent 有 use 权限但缺其模型/工具时准确提示依赖缺失；委派不能继承目标作者权限。
8. 管理员保存与另一管理员撤销并发时，版本冲突/资格/审计保证事务完整；授权更新使相关缓存失效。
9. 同一账号切换租户后目录、默认模型、页签和后台结果均隔离；保留每个启用成员至少一个有效租户的约束。
10. 被关闭消费者仍不发生执行；已接通消费者须以隔离身份和真实 handler/执行入口验证，不能仅用模拟菜单验收。

## 8. 迁移与规范衔接

当前 `openspec list` 无活动 change，导航和身份相关规范已归档。适合另起资源授权 change，不能回写归档任务为“已实现资源分配”。本文先交付方案，不自动创建或执行 change。

已有 9 项权限保留 ID；先只读盘点角色、可见资源和现有默认模型，形成迁移预览。已开放读取可按明确资源清单迁成 grants，不能把当前关闭的执行能力当成已授权。平台管理员由资格派生 all；普通角色新增执行权默认无。技能维护从 agent.read 拆出属于有意收紧，普通成员不自动获得编辑/启停权限，管理员通过新角色配置显式分配。

资源授权按模块在数据迁移和实际守卫同一兼容版本中启用。启用后回退只能使用仍执行授权的兼容构建，不能把 grant 表读取失败或缺记录解释为全部允许。资源内容、历史 owner、凭据归属和已有会话不因授权迁移搬家。

| 正式规范 | 需要明确修改的旧约定 |
| --- | --- |
| `business-permission-catalog`、`rbac-authorization` | 原只允许9项/不做资源grants/不做模型策略改为本次新增范围；普通角色仍禁止通配符，平台 all 是独立系统资格 |
| `console-navigation-availability`、`console-information-architecture` | 从固定页面能力补为菜单/页签授权与真实功能状态交集；平台全权限和关闭功能分开 |
| `tenant-skills-tools-console` | agent.read 可写技能改为独立 skill.edit/enable 加具体资源授权；目录读取与工具执行独立 |
| `platform-config-console` | 保留平台凭据管理；新增普通成员可用模型投影和策略，不把全局配置接口开放给普通成员 |
| `identity-management-workbench`、`user-membership`、`business-permission-catalog` | 保留 tenant_admin 本租户管理，同时新增平台管理员以明确目标租户执行授权管理的路径，不伪造 Membership |
| `tenant-resource-isolation`、`member-tenant-assignment` | 保留资源原归属、有效租户和数据隔离；平台 all 不改写 owner、不自动授予不存在的成员关系 |

2026-09-08 的 [原对比报告](/Users/jiantan/ai_assistant/cowagent/docs/design/user-role-permission-gap-and-plan.md) 中“首批不做 all”及“平台管理员仅有限平台权限”的建议，针对本轮明确新增目标由本文替代；原身份安全、数据归属和非平台角色不得提权的约束继续保留。

## 9. 按实际结果的分类（2026-09-16）

判定口径与完整清单见
[`evidence/8-5-doc-closure.md`](../../openspec/changes/unify-console-by-data-scope/evidence/8-5-doc-closure.md) §2。

| 本文范围 | 实际状态 | 依据 / 说明 |
| --- | --- | --- |
| 平台管理员派生 all（§4.1） | ✅ 已实现并有用例 | `tests/test_identity_resource_authorization.py`；`authorization_mode=all` |
| `role_resource_grants` / `tenant_resource_grants` / `roles.model_defaults_json`（§5） | ✅ 已实现 | `auth/store.py:681`、`auth/service.py:2020`、`:2443`、`:6460` |
| 功能权限、菜单、技能、工具、模型、智能体六个页签 + 有效权限预览（§3） | ⬜ 规划 | `identity-admin.js` 无资源选择页签；保存路径只覆盖 `resource_grants` / `model_defaults` |
| `resource_catalog`、`model_policies`、`tenants.authorization_revision`、`AuthorizationService`、`membership_resource_grants`（§5） | ⬜ 规划 | 全仓 0 命中（见 §5 表） |
| 「管理不自动包含执行」「空集合不归一为全量」等规则（§4.2、§6） | 🟡 部分已实现 | 技能维护与 `agent.read` 已拆分（`evidence/5-4-public-surface-authority.md`）；`SkillManager._normalize_skill_filter` 的空数组语义仍是既有风险点，未在本轮改动 |
| 五类资源的**对象范围**判定（本文原以 grant 表达） | ✅ 已由本 change 补齐 | `auth/object_scope.py`：owner 优先于管理员例外；公共配置需管理资格本身 |
| 成员模型目录（§6「模型」一行） | 🟡 已实现（未验收） | 快照内 `admin.models` 页 scope 已改为 `tenant`（`auth/service.py:103`），新增 `model_catalog_open()`（`:3393`），写面仍单独报 `actions.manage`（平台资格）；用例 `tests/test_member_model_catalog.py` 等 29 项 + 前端 7 项通过（本周期重跑）。属主证据 `evidence/5-4-public-surface-authority.md` §3 尚未回填，故不记为已验收 |
| 运行消费者开放、真实链路验收（§7 阶段 C/D 的执行部分） | ⬜ 未覆盖 | 阻塞于真实凭据与真实运行进程（`evidence/7-1-runtime-preflight.md`）；不得用组件集成测试代替 |
