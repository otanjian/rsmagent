# fork-upstream-decoupling Specification

## Purpose
规定长命 fork 与上游 `master` 的长期共存契约：以单一权威路由清单派生路由注册与授权策略、为 fork 定制逻辑定义稳定接缝、把定制代码迁出上游核心文件，并以可重复的同步基建与覆盖不变量保证上游新增路由、功能与签名不因合并而丢失或静默降级。

本规范区分两类内容：**行为契约**（路由派生、接缝回退语义、存储组合、身份收敛、合并保持性）留在本 capability；**同步工具的机械配置**（`rerere`、`.gitattributes`、脚本实现）属工程手段，落在 change 的 tasks 与 design，不在本规范声明为能力行为。
## Requirements
### Requirement: 路由注册与授权策略由单一权威清单派生

系统 SHALL 维护一份权威路由清单作为路由存在性与授权策略的唯一事实源，并据其同时派生 web.py 的 URL 表与授权策略表。MUST NOT 存在第二份手工维护、可与权威清单漂移的并行清单。权威清单 SHALL 支持按来源（上游核心/fork 扩展）登记条目，使 fork 新增路由的合并不要求编辑上游核心文件的路由字面量。

权威清单 SHALL 逐条登记现有全部路由，不得以「从 `_WEB_URLS` 自动转录后再自动生成策略」的方式批量填充，因为该方式会使每条路由的策略成为未经验证的默认值。已知的未登记缺口 SHALL 在迁移时逐条补齐并各自给出策略与权限，至少包括：`/admin`（聊天页别名）、`/api/identity/administered-tenants`、`/api/scenes`、`/api/scenes/activate`、`/api/scenes/workbench/import`。

#### Scenario: 权威清单与派生结果一致

- **WHEN** 以权威清单构建应用并读取其 URL 表与策略表
- **THEN** 两条派生结果包含且仅包含权威清单登记的路由，无任何仅存在于一方或字段不一致的条目

#### Scenario: fork 扩展路由无需改上游核心文件

- **WHEN** fork 通过扩展注册新增一条业务路由
- **THEN** 该路由出现在 URL 表与策略表中，且上游核心文件的路由字面量未发生编辑

#### Scenario: 已知未登记路由被逐条补齐

- **WHEN** 迁移既有的路由与策略清单
- **THEN** `/admin`、`/api/identity/administered-tenants` 与三条 scenes 路由均在权威清单中有明确策略与权限条目，且其策略值经人工确认而非默认值

### Requirement: 路由与策略的覆盖不变量以 handler 实现为准且可静态校验

系统 SHALL 提供可执行的不变量校验，且该校验 MUST 以**三个独立来源**交叉比对，任一不一致即失败：

1. 权威清单登记的每个方法 MUST 存在对应授权策略条目；
2. 策略表登记的每个路由 MUST 存在于权威清单或明确登记为外部来源；
3. 权威清单登记的 HTTP 方法集合 MUST 与对应 handler 类实际实现的方法集合（通过内省 `get`/`post`/`put`/`delete`）一致。

第 3 条为必需项：仅比对权威清单与派生策略表是恒真的（二者同源），无法发现「handler 实现了某方法而清单未登记」这一类缺口——该类缺口正是未登记路由与未授权可达的成因。校验 SHALL 可独立运行并在不一致时失败，以便在 CI 与合并上游后立即发现遗漏。

#### Scenario: 已实现方法缺策略条目

- **WHEN** 权威清单中的某路由实现了某 HTTP 方法但策略表缺少该方法条目
- **THEN** 不变量校验失败并指出该路由与方法，构建不得视为通过

#### Scenario: handler 实现了清单未登记的方法

- **WHEN** 某 handler 类内省出 `post`，但权威清单该路由只登记了 `get`
- **THEN** 不变量校验以「实现与登记不一致」失败，指出该路由与未登记方法

#### Scenario: 合并上游新增路由后触发校验

- **WHEN** 上游合并引入一条新路由而未同步登记授权策略
- **THEN** 不变量校验以「新增路由未登记策略」失败，阻止其以未授权方式可达

### Requirement: 定制逻辑位于稳定接缝而非上游核心文件内

fork 定制逻辑（租户/身份、待办、外观、品牌及 fork 专有启动守卫）SHALL 通过与上游解耦的接缝接入：路由经扩展注册、启动行为经扩展钩子登记、前端经独立模块并在稳定挂载点装载、会话存储经 schema/查询扩展点组合。定制逻辑 MUST NOT 要求在上游核心文件中原地改写其数据模型定义、路由字面量或**公共方法签名**才能生效。

对上游核心方法的签名，fork 侧所需参数 SHALL 经接缝注入（可选参数、上下文查找或授权目标解析），MUST NOT 以改写上游方法签名为代价；上游核心方法在独立上游发行形态且无 fork 模块时 SHALL 保持原有签名与语义。

接缝的落地结构 SHALL 与上游当前的模块布局对齐，MUST NOT 长期停留在上游已废弃的文件形态内。当上游把某上游核心文件拆分或迁移为多个模块时，fork 在该文件内的定制 SHALL 随之迁入与上游新布局并列的 fork 自有模块；MUST NOT 通过保留旧形态单体文件、重写上游拆分为单体、或把 fork 实现与上游实现合并回同一文件来维持接缝。文件形态退役的判定 SHALL 以上游当前布局为准并记录于冲突基线，不以「fork 侧仍有内容」为由阻止迁移。

当 fork 因语义需要而平行承载某上游实现的完整逻辑时（例如授权与数据作用域必须参与方法体中部），该平行实现 SHALL 明确满足：位于 fork 命名空间、与上游路径不重合、上游模块保持上游形态、并登记其上游来源以支持变更检测与人工移植。此类平行实现属合法接缝形态，MUST NOT 因此被判定为「原地改写上游文件」。

“独立上游” SHALL 由明确的独立构建/发布装配确定，且不得挂载 rdai 身份库或业务数据。可选外观或界面接缝缺失 SHALL 只回退展示，不得扩大后端授权。rdai 发行形态的强制授权接缝集合 SHALL 由独立于待加载扩展的启动装配声明并检查；缺失身份、租户、owner、工具、任务执行或请求传输的必要授权接缝时 SHALL 拒绝启动或关闭受影响能力，MUST NOT 回退到上游免鉴权行为。请求参数、插件停用或普通运行时开关 MUST NOT 将 rdai 降格为独立上游模式。

#### Scenario: 上游核心文件不含 fork 专有分支
- **WHEN** 审查上游核心文件的路由字面量、启动流程、公共方法签名与会话存储 schema 定义
- **THEN** 其中不包含 fork 专有路由、fork 专有列的原地定义或 fork 专有的签名改写，fork 行为由接缝模块提供

#### Scenario: 接缝可独立启用与回退
- **WHEN** fork 接缝模块被移除或停用
- **THEN** 独立上游发行形态保持上游语义且不接触 rdai 数据；rdai 仅允许可选展示接缝回退，强制授权接缝缺失则拒绝启动或关闭受影响能力，不产生未授权或部分写入

#### Scenario: 双方在同一文件各自新增符号
- **WHEN** fork 在渠道实例模块新增凭据最小必填集与租户运行时能力，同时上游在同一模块新增类型标签与默认名派生
- **THEN** 两组成员各自以独立扩展块登记，合并后同时存在且互不覆盖；上游新增不要求改写 fork 的登记块，反亦然

#### Scenario: 上游拆分 fork 定制的宿主文件
- **WHEN** 上游把 fork 定制所在的某上游核心文件拆分为多个模块，而 fork 在该文件内既有定制实现
- **THEN** fork 定制迁入与上游新布局并列的 fork 自有模块，入口文件收敛为上游形态，MUST NOT 以保留旧单体或重写上游拆分为单体维持接缝

#### Scenario: 迁移期间两侧行为同时保留
- **WHEN** 上游拆分完成且 fork 定制已迁入自有模块
- **THEN** 上游拆分后的全部上游行为与 fork 的身份/租户/授权定制同时可用，两侧各自的可执行回归均通过，不出现任一侧被整文件覆盖

### Requirement: 上游新增功能与签名在合并中不丢失

合并上游时，系统 SHALL 保留上游新增的功能块与其安全约束，MUST NOT 因 fork 在同一文件的方法签名改写而丢弃。至少包括桌面端「按本地路径导入文件」能力（其 loopback 与每启动令牌校验 SHALL 保持生效）以及上游对上传、消息投递等入口的签名与语义。

#### Scenario: 合并保留上游桌面导入能力

- **WHEN** 合并上游新增的按本地路径导入功能所在的文件
- **THEN** 该能力及其 loopback/令牌校验完整保留，fork 的授权目标解析经接缝叠加而非替换该功能

#### Scenario: 上游签名不得被 fork 覆盖

- **WHEN** 上游修改了某公共方法的签名或默认行为
- **THEN** 合并结果采用上游签名，fork 所需参数来自接缝，不存在仅为 fork 而存在的方法签名分叉

### Requirement: 删除/修改类冲突有显式决策并记录

对「一方删除而另一方修改」的文件，系统 SHALL 在冲突基线清单中记录每一文件的处置决策（保留删除 / 恢复文件 / 采用上游版本 / 迁移后删除）及其理由，MUST NOT 在每次合并时重复进行人工判断。该决策 SHALL 对上游后续同文件的改动持续生效。

该要求 SHALL 覆盖两个方向：fork 删除而上游修改，以及上游删除而 fork 修改。后者的处置 SHALL 明确 fork 定制内容的去向（迁入替代模块、恢复文件或放弃该定制），MUST NOT 以「fork 侧仍在修改」为由保留已被上游废弃的文件形态。

#### Scenario: fork 已删除而上游继续修改

- **WHEN** 合并发现某文件在 fork 侧被删除、在上游侧被修改
- **THEN** 合并按基线清单中登记该文件的处置决策自动取舍，不产生需要逐次人工裁决的冲突

#### Scenario: 上游删除而 fork 仍在修改

- **WHEN** 合并发现某文件在上游侧被删除、在 fork 侧被修改
- **THEN** 合并按基线清单中登记的处置决策处理，且该决策已说明 fork 定制内容的去向，不静默丢弃 fork 定制，也不为保留定制而复活上游已废弃的文件

#### Scenario: 定制迁移完成后的删除留痕

- **WHEN** fork 定制已迁入替代模块，原文件被上游删除
- **THEN** 基线将该文件登记为迁移后删除并指向替代模块，后续合并不再就该文件要求人工裁决

### Requirement: 双方任务身份模型收敛为单一解析接缝

当 fork 与上游在同一执行路径上分别实现任务身份解析（例如调度任务触发时的身份来源）时，系统 SHALL 收敛为单一身份解析接缝，由该接缝产出执行所需身份，各侧的特有行为作为接缝的输入或策略接入，MUST NOT 保留两套并列的身份解析实现。收敛 SHALL 保持既有对外行为：由租户成员创建、带创建者快照的任务 SHALL 仍以该成员身份执行；仅具 Agent 维度的历史任务 SHALL 仍按 Agent 范围执行；身份不可解析时 SHALL 按执行授权的 fail-closed 规则拒绝。

收敛 SHALL 同时明确两项此前未决的设计：其一，身份上下文的唯一入口模块；其二，调度服务的拓扑（每 Agent 一个服务 vs 全局单一服务）与执行回调的签名契约。二者 SHALL 在实现前确定并记录，MUST NOT 以「保留两侧实现」回避。

#### Scenario: 调度任务触发时的身份

- **WHEN** 一个由租户成员创建、携带创建者快照的调度任务触发
- **THEN** 执行以该成员身份进行，其工作区、会话与记忆按该成员解析，不落到裸 Agent 或全局默认

#### Scenario: 历史任务仅具 Agent 维度

- **WHEN** 一个历史任务没有创建者快照，仅有 Agent 维度
- **THEN** 执行保持按 Agent 范围解析，不因缺少创建者而失败或借用其他身份

#### Scenario: 身份不可解析

- **WHEN** 任务触发时其身份无法解析（快照缺失且 Agent 绑定不可用）
- **THEN** 执行被拒绝并记录可诊断原因，不静默以空身份继续

#### Scenario: 服务拓扑与回调契约已确定

- **WHEN** 实施身份收敛
- **THEN** 调度服务的拓扑与执行回调签名已有单一记录的决定，且不存在两套并列的回调契约

### Requirement: 会话存储 schema 组合接缝支持 fork 与上游列并存

会话与消息存储的 schema 与查询构造 SHALL 提供扩展接缝，使 fork 新增列（如所有者维度）与上游新增列（如多智能体维度）可组合而非互相覆盖。接缝 SHALL 覆盖**列定义、主键与唯一约束、索引定义、INSERT 列/占位/参数构造、公共 WHERE 片段、迁移步骤**六类扩展点，并 SHALL 定义各扩展点的唯一职责与冲突处理规则。任何人一侧新增列或重建约束不要求重写另一侧的 SQL 字面量。

接缝 MUST 表达约束级变更而非仅列级变更：上游对会话/消息表的**主键与唯一约束重定义**（由单键改为含智能体维度的复合键）SHALL 由接缝登记并生成。收敛结果 SHALL 显式决定会话/消息行的最终主键与唯一约束，MUST NOT 由 fork 侧原有主键静默覆盖上游的复合键——否则会静默丢失上游的多智能体唯一性保证。

规范 SHALL 区分两个不同维度：所有者维度（逐用户，既有）与租户维度（逐租户，新增）。租户维度为更粗粒度的纵深防御，SHALL 作为独立扩展项登记，MUST NOT 与所有者维度混为同一列。

#### Scenario: 两侧各新增一列

- **WHEN** fork 侧新增一个租户维度列且上游侧新增一个智能体维度列
- **THEN** 合并后的 schema 同时包含两列，且插入、查询、分页路径均使用组合后的列表与 WHERE 条件

#### Scenario: 上游重定义主键与唯一约束

- **WHEN** 上游把会话/消息表的主键由单列改为含智能体维度的复合键，并相应调整唯一约束与索引
- **THEN** 合并后的 schema 采用经显式决定的复合主键与唯一约束，索引一并调整，不因 fork 既有主键而丢失复合键

#### Scenario: 所有者维度与租户维度分别登记

- **WHEN** 审查会话/消息的 schema 与查询
- **THEN** 所有者维度与租户维度各自作为独立扩展项存在，两者的过滤条件可并用且语义不重叠

#### Scenario: 扩展点缺省时不改变上游语义

- **WHEN** 未注册任何 fork 扩展点
- **THEN** 会话与消息的读写沿用上游定义的表结构与查询语义，不附加额外过滤

### Requirement: 既有行的租户维度按行所有者回填

迁移既有行以补齐租户维度时，系统 SHALL 以该行**所有者**（既有所有者维度）对应用户的租户成员关系作为权威归属来源。MUST NOT 仅按 Agent 的当前归属回填：Agent 与租户的绑定可被重绑且可能为多对多，按 Agent 回填会产生错误的隔离归属。无法确定归属的行 MUST 保持不可跨租户读出，且 MUST NOT 因回填失败丢失或改写原始内容。

#### Scenario: 按行所有者回填

- **WHEN** 迁移一条所有者已知的既有会话行
- **THEN** 其租户维度按该所有者用户的成员关系回填，与 Agent 当前绑定无关

#### Scenario: 归属不可确定

- **WHEN** 某行的所有者缺失、对应用户无有效成员关系或存在多义
- **THEN** 该行不被赋予推测的租户维度，且保持不可跨租户读出

### Requirement: 合并上游后既有功能与授权边界可回归验证

合并上游后，系统 SHALL 通过可执行回归验证确认：上游新增路由未被静默丢弃、上游新增功能与安全约束（含桌面端本地导入）仍生效、fork 新增路由仍可达、授权策略对被合并的每条路由仍然生效、删除/修改类冲突按基线决策处置。回归 SHALL 以权威清单、三腿不变量校验与冲突基线清单为核心，覆盖至少一次「上游新增 → 登记策略 → 门禁生效」的完整验证。

#### Scenario: 合并上游后回归

- **WHEN** 合并一次上游更新后执行回归验证
- **THEN** 报告上游新增路由均出现在权威清单与策略表中、上游新增功能与安全校验仍生效、fork 路由仍可达、删除/修改类冲突无未决项，任一缺失即失败

### Requirement: legacy 面退役的删除决策入冲突基线

删除上游原生的 `legacy` 认证面（`web_password`、`cow_auth_token`、`AuthLoginHandler`/`AuthLogoutHandler`/`AuthCheckHandler`、`_get_web_password`/`_create_auth_token`/`_verify_auth_token`/`_check_auth`/`_require_auth`，以及 `route_registry.py` 中登记为 `upstream` 的三条 `/auth/*` 路由）SHALL 作为**长期删除决策**写入 `scripts/conflict-baseline.txt`。因本退役是**文件内删除/改写**（冲突形态为 `UU`）而非整文件删除（`DU`），逐文件处置 SHALL 使用 `keep-fork` 或 `seam:<tasks>`，文档双侧编辑使用 `merge-docs`；MUST NOT 对本 change 的文件内删除使用 `keep-deletion`，MUST NOT 因此改动 `scripts/sync_report.py` 的 `DELIBERATE_REMOVALS`。处置决策 SHALL 对上游后续同文件的改动持续生效。系统 MUST NOT 在每次合并时重新人工裁决该删除，MUST NOT 因合并静默复活共享密码或旧 token 认证路径。

#### Scenario: 上游继续修改被删认证文件
- **WHEN** 合并发现 `channel/web/web_channel.py` 或 `channel/web/static/js/console.js` 在 fork 侧删除 legacy 认证块、在上游侧被修改
- **THEN** 合并按基线登记的 `keep-fork` 或 `seam:<tasks>` 处置取舍，不复活 `web_password`、`cow_auth_token` 或 `/auth/*` 共享密码路径

#### Scenario: 上游新增路由被登记而非静默挂载
- **WHEN** 合并引入新的 `/auth/*` 或认证相关路由
- **THEN** 该路由必须出现在权威清单与授权策略表中方可可达，不得以未登记方式挂载

#### Scenario: 不误用整文件删除词表
- **WHEN** 本 change 产生的冲突全部为文件内改写（`UU`）
- **THEN** 基线登记为 `keep-fork`/`seam:`/`merge-docs`，`DELIBERATE_REMOVALS` 与既有 `keep-deletion` 行保持不变且仍镜像一致

### Requirement: 退役后缺失断言式回归防止旧认证复活

对于上游可能重新引入的 legacy 认证符号（共享密码字段、旧 token cookie、旧认证 handler、`_require_auth`、免登录分支），系统 SHALL 提供可执行的「不复活」断言式回归，明确断言这些符号**不存在**于实现中；MUST NOT 仅在文档中声明已删除。该回归 SHALL 在合并上游后立即运行，使旧认证路径的任何回归以测试失败而非静默放行暴露。

#### Scenario: 合并复活共享密码字段
- **WHEN** 一次上游合并把 `web_password` 或 `cow_auth_token` 重新写回实现
- **THEN** 不复活回归失败并指出被复活的符号，构建不得视为通过

#### Scenario: 合并复活旧登录 handler
- **WHEN** 一次上游合并重新引入 legacy `/auth/login` handler、`_verify_auth_token` 或 `_require_auth`
- **THEN** 不复活回归与路由覆盖不变量同时失败，指出该 handler 与未登记路由

### Requirement: 退役批次完成后立即同步排练并登记基线

删除上游原生认证面的工作 SHALL 收拢为紧凑批次（含 handler 级 `_require_auth`/`ctx is None` 收敛），并在批次完成后 SHALL 立即以 `scripts/sync-from-master.sh` 对当时 `master` 做一次同步排练，用结果重新生成 `scripts/conflict-baseline.txt` 并按 `keep-fork`/`seam:`/`merge-docs` 登记新增的修改类冲突处置；MUST NOT 在未排练、未登记基线的状态下把该批次视为完成。

#### Scenario: 批次后同步排练
- **WHEN** 删除批次落地，且工作树干净
- **THEN** 同步排练产出冲突报告，新增冲突文件逐条登记处置（文件内改写为 `keep-fork`/`seam:`，文档为 `merge-docs`），报告无未决项

#### Scenario: 未登记基线不得收尾
- **WHEN** 批次声称完成但 `conflict-baseline.txt` 未包含新产生的修改类冲突
- **THEN** 验收失败，`scripts/check_change_deltas.py` 报告存在未被本 change 点名的冲突文件

### Requirement: 上游资产与接缝在退役中保持不丢失

本 change 的安全删除 SHALL NOT 触碰或退化既有上游资产与接缝：会话存储组合 schema（`agent/memory/conversation_store.py`）、调度身份收敛接缝（`agent/tools/scheduler/integration.py`）、`chat.html` 的 `data-fork-fragment` 挂载点、`tests/test_scheduler_web_update.py` 的上游行为断言，以及上游 `_import_local_file` 的 loopback 与每启动令牌校验。这些接缝的既有测试 SHALL 保持通过，MUST NOT 因删除 legacy 而被移除或放宽。

#### Scenario: 退役后上游接缝测试仍通过
- **WHEN** 删除批次落地后运行既有接缝测试
- **THEN** 组合 schema、调度身份收敛、fork fragment 挂载与本地导入保全校验全部通过，未出现被删除或放宽的断言

#### Scenario: 本地导入保全未被静默丢弃
- **WHEN** 合并把上游 `_import_local_file` 带入本树
- **THEN** 其 loopback 与每启动令牌校验完整保留，除非另有显式登记的决策将其移除

### Requirement: 能力恢复接缝同时保留上游功能和数据库强制边界

数据库能力恢复 SHALL 通过独立路由、启动钩子、工具/任务授权服务、前端模块及 Desktop 主进程 broker 接入，保留上游公开方法签名、数据格式和非安全业务行为。路由接管 SHALL 明确匹配原处理器、来源及 HTTP 方法，MUST NOT 靠重复 URL 顺序、宽泛覆盖或修改上游公开签名实现。

rdai SHALL 根据独立声明的强制接缝集合在启动及能力开放前检查身份、tenant/owner、对象授权、执行重验及各类传输；缺失任一必要接缝时受影响能力 SHALL 不可用或启动失败，MUST NOT 因扩展缺失而注册旧免鉴权入口。新增工具或传输 MUST NOT 仅因复用旧公开接口就默认视为已授权。

#### Scenario: 上游新增方法或传输
- **WHEN** master 新增工具动作、HTTP 方法、上传路径或流传输
- **THEN** rdai 将其纳入接缝覆盖清单并验证身份与对象授权；未覆盖入口保持关闭，不自动承接旧免鉴权行为

#### Scenario: 强制接缝缺失
- **WHEN** rdai 缺少路由授权、SchedulerTool 授权、后台执行重验或 Desktop 凭据 broker 等必要接缝
- **THEN** 启动装配发现缺失并拒绝启动或关闭相关能力，不退回上游实现、不隐藏故障为成功

### Requirement: master 到 rdai 的每次更新验证无冲突语义漂移

每次 master 更新合入 rdai SHALL 记录明确的上游基线与候选版本，并检查即使无文本冲突仍可能变化的路由、工具动作、任务字段、通知元数据、凭据响应和请求传输。最终合并候选 SHALL 同时通过上游行为回归与 rdai 权限隔离回归，MUST NOT 以合并无冲突或单侧测试通过替代。

检查范围 SHALL 包含 scheduler tool/store/integration、Desktop main/preload/client 与原生认证扩展，以及原有 `_import_local_file`、渠道实例扩展、启动钩子和会话 schema 接缝。测试 SHALL 分别覆盖独立上游、完整 rdai、rdai 强制授权接缝缺失以及仅可选 UI 接缝缺失，不得通过删掉安全断言或上游行为断言放行。

#### Scenario: 无冲突合并改变任务字段
- **WHEN** master 无文本冲突地新增任务字段或改变工具、调度读写行为
- **THEN** 合并候选验证字段保留、通知语义、原子并发与 HTTP/工具/后台一致授权，不因旧测试仍通过而遗漏新路径

#### Scenario: 上游本地文件导入更新
- **WHEN** master 修改 `_import_local_file` 或 Desktop 上传、预览、下载及本地导入调用
- **THEN** 合并候选同时保留上游行为与 rdai 受限路径、主进程令牌边界、tenant/owner 授权，未覆盖传输不得开放

### Requirement: 恢复能力的冲突决策与双侧回归可追溯

master 到 rdai 的更新 SHALL 记录冲突文件、双方意图、采用的稳定接缝及保留或调整的行为断言，MUST NOT 对包含双方业务逻辑的文件整文件选择 ours/theirs 以跳过分析。冲突排练 SHALL 在隔离环境进行且不直接改变正式分支；排练证据不替代最终合并候选的双侧回归。

#### Scenario: 双方修改同一业务文件
- **WHEN** master 与 rdai 同时修改任务、渠道实例、认证、Desktop 或其他业务文件
- **THEN** 逐项保留双方独立意图，记录接缝决策并运行对应上游行为与 rdai 安全回归，不整文件覆盖另一方逻辑

#### Scenario: 只完成冲突排练
- **WHEN** 隔离环境已完成冲突排练，但尚未对最终合并候选执行双侧回归
- **THEN** 合并验收保持未完成，不把排练成功等同于正式候选可交付

