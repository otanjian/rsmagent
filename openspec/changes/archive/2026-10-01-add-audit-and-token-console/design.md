# 设计：审计日志与 Token 消耗

## D1 数据源照本仓库的模型，不照源实现的模型

源实现的审计在每个租户库、用量在每个租户工作区的 `conversations.db`，读取时 `_resolve_stores` 扇出合并。本仓库两者都进 `identity.db`：

- 好处是「当前租户」变成一次带 `WHERE` 的查询，而不是一次扇出。扇出合并有一条源实现没有的失败路径——某个租户的工作区目录恰好不在（未挂载、被清理、单飞部署），该租户的数字会**静默缺失**，而页面看起来完全正常。不扇出就没有这条路径。
- 代价是跨租户读变成 `tenant_id IS NULL` 的一次无过滤查询，所以**范围必须显式命名**：`AuditStore.query_events` 用 `scope="all"` 表示跨租户，而不是让 `tenant_id=None` 兼任「不过滤」。`None` 意思是「不要过这一列」是本仓库所有用量读的既有语义（`store._usage_filters`），让它同时兼任「平台管理员」会让「忘了传 tenant」与「故意跨租户」写成同一行代码——而这个方向的错误是跨租户泄漏，不是报错。

## D2 读取范围由资格推导，`?tenant=` 只是收窄

`audit_read_scope(ctx)` 是这两个接口唯一的范围判定点，返回 `(scope, tenant_id)` 或抛 403：

| 调用方 | scope | 行为 |
| --- | --- | --- |
| 平台管理员 | `all` | 跨租户；`?tenant=` 收窄到该租户 |
| 选中租户的租户管理员 | `tenant` | 只读该租户；`?tenant=` **被忽略** |
| 其他 | — | 403 |

三条都是刻意的：

- **租户管理员的 `?tenant=` 是被忽略而不是被 403**。403 等于「这个租户 id 存在，但你不能读」，越权探测要的正是这一位信息；而该调用方自己的范围已经是最窄的答案，忽略它没有任何信息损失。
- **普通成员被拒绝而不是被静默限定**。给成员一个空列表会读成「没有事件」，而真相是「这不是你的页面」。
- **范围先算、查询后建**。所有基于请求参数的判定都发生在任何 SQL 之前，`scope='tenant' and tenant_id is None` 在 store 层直接报错而不是退化成不过滤。

路由策略登记为 `personal`（任何已认证调用方可达），设计上由 handler 兜底判定——把一个功能性权限写成路由门会假装「持有该权限」等于「有这个资格」，而后者才让读取安全。

## D3 「失败」= 不等于成功

审计的 `result` 列有 `success` / `denied` / `error`，未来还会有别的。页面的结果过滤是个二选一控件，所以：

- `status=success` → `result = 'success'`
- `status=failure` → `result <> 'success'`
- 其他字面值 → `result = <该值>`（运维仍可以精确要 `denied`）

把「失败」写成 `result IN ('denied','error')` 会在新增一个非成功结果的当天静默漏掉它，而这个漏掉的形态是「页面说没有失败」——比报错更难发现。Agent 管理动作的拒绝还额外以 `<动作>.denied` 形式留痕（`agent.create.denied`），排除式写法同样覆盖它。

## D4 操作者按记录的用户名匹配，不先解析成 user id

`audit_events` 与 `token_usage` 都同时存 `actor_user_id`（稳定键）与 `actor_username`（写入时的展示值）。页面上的过滤框是**按人找事**，输入通常是名字片段：

- 解析成 id 再查，在「账号改过名」这个最需要它的场景会返回空——该账号的历史行留着旧名，用新名字换来的 id 查不到它们。
- 精确匹配同样不行：输入片段是常态，精确匹配返回空而用户不知道差别在哪。

所以按 `actor_username LIKE ? ESCAPE '\'` 匹配（`%` / `_` / `\` 转义，避免输入里的通配符变成通配符），`actor_user_id` 仅在内部调用方明确知道 id 时使用。用量侧同口径。

## D5 计量点选在分发点，不在 provider 内

`AgentLLMModel.call` / `call_stream` 是本仓库所有 provider 的唯一出口。埋在这里有三个必须处理的形状：

1. **返回 dict**：调用已经完成，直接读 `usage` 后结算。
2. **返回迭代器**：注意这**不只是流式路径**——若干 provider（deepseek 在内）非流式路径也返回生成器，所以「从返回值上读 usage」这个做法从一开始就是错的。必须包一层转发迭代，在读完后、调用方提前停止迭代后、以及迭代抛异常时都结算。`try/finally` 里的结算才能让一个被取消的回合仍然计数。
3. **抛异常**：先记 `status="error"` 再原样重抛。调用方的错误处理不是埋点的事，但控制台要能解释那次没有回答的调用。

计时从**调用前**开始，因为返回生成器的 provider 此时还没发 HTTP 请求——从首个 chunk 计时会把模型自身的延迟（其中的大头）排掉。

不写返回值、不吞异常：埋点是观察者。

## D6 用量写入不阻塞 LLM 调用

LLM 调用路径 MUST NOT 等 SQLite：事件进队列，后台线程按间隔 flush，同（日期/租户/主体/供应商/模型）键的事件**先在内存合并**再一次 upsert。合并键与 `idx_token_usage_key` 唯一索引一致，写入是单条 `ON CONFLICT DO UPDATE`；源实现是「读—判断—写」，两个 flush worker 同时工作时会丢计数。

`actor_username` 在写入时留空、在读取时补齐（`_fill_usernames`，带缓存含未命中缓存），这样 flush 线程不做任何查询，而控制台只渲染有界的一页。补不上的 id 留空并回落到显示 id，而不是丢掉该行。

## D7 展示口径不臆造未测得的用量

三个口径沿用源实现，因为要匹配的交付物就是那个页面，且它们都必须**可被读者识别为口径**而不是事实：

- 输入摘要相同的行合并、token 相加、`call_count` 自增——这是展示层归并，`call_count` 说清了「几行并成一行」。
- 全 0 的行按摘要粗略估算并打 `estimated`，页面以 `*` 标记。让「供应商没上报」显示成一行 0 会被读成「没花钱」。
- 输入/输出摘要按密钥形态打码后才返回。

## D8 页面可用性投影按同一资格作答

`_SIGNED_CONSOLE_PAGES` 里两页的 `permission` 为空、`scope` 为 `platform`，而「谁真的能读」由 handler 判定。若投影不额外作答，两页会落到兜底分支，在页面**已经可服务**时报告 `consumer_closed`——那是关于部署的断言，会对平台管理员撒谎。

所以投影对这两页问同一组资格（`mode == "all"` 或平台管理员或租户管理员）。用一个功能性权限（假想的 `audit.read`）代替它会把页面报给一个持有该权限、却仍会被 handler 403 的自定义角色——即「投影说开着、接口答 403」。

**边界从分组级下移到逐项级（D8 修订，2026-09-25）**：「平台运维」分组（后更名为「平台管理」，见 D10）原先整体带 `sidebar-hidden-platform-scope`，菜单语义是平台级（`console-information-architecture`: 「平台运维保持平台边界」）。但该组内六个条目的真实边界并不相同：

- `admin.tenants` 跨租户，必须保持平台级；
- `admin.settings` / `admin.branding` / `admin.logs` 的 consumer 本就关闭（`consumer_closed`），对任何人都不可用；
- `admin.audit` / `admin.token_usage` 是**只读操作者视图**，handler 已把租户管理员收窄到本租户。

把整组按平台级隐藏，等于用最宽条目的边界去约束最窄的两个——租户管理员因此在菜单里完全看不到它们，尽管接口已经为他们作答。所以边界改为逐项声明：

- **标记**：分组上的 `sidebar-hidden-platform-scope` 移除；`tenant` / `platform` / `branding` / `logs` 四项各自携带 `platform-scope-only`（`platform` 原有的 `data-perm="platform"` 保留）。
- **门禁**：`console.js` 的 `_isPlatformOnlyEntry(item)` 同时识别 `platform-scope-only` 与 `data-view="platform"`，非平台管理员逐项 `hidden: true`；空分组判定**不计**平台专属项，普通成员因此不会看到一个空的「平台管理」表头。
- **投影**：`_console_pages_projection` 对两页作答 `mode == "all" or is_platform_admin or is_admin`，同组另外四项继续按各自边界作答（`admin.tenants` → `no_permission`，其余 → `consumer_closed`）。
- **菜单授权**：`BUILTIN_MENU_DEFAULTS["tenant_admin"]` 增加 `admin.audit` / `admin.token_usage`（新租户建角色时即取即用），迁移 34 为**既有**的内置 `tenant_admin` 角色回填 `nav:` 授权——存量租户不必重建角色。迁移只动已持有 `menu` 授权的内置行：给一个纯功能权限型角色补这条授权会把它的门禁**打开**，反而隐藏其余页面。

隔离性没有因此放松，且由用例钉住：租户管理员的 `?tenant=` 被忽略（不 403），作用域仍是自己的租户；普通成员拿到的是 `no_permission`，且在 `tests/test_admin_area_group_gating.cjs` 里连分组表头都拿不到。这与「投影说可用、接口答 403」是相反方向的两件事——放宽的是**谁能看见入口**，收紧的仍然是**谁能读到哪些行**。

## D9 前端拆成独立视图模块

`audit-console.js` 通过 `window.registerConsoleView({ id, label, load, repaint })` 注册 `audit` 与 `token_usage`，不往 `console.js` 的导航分发里加分支（`fork-upstream-decoupling` 的分割约定）。原先 `audit` 由 `identity-admin.js` 注册并借用 `admin.settings` 页面 id（当时该分组里唯一可借的运维页 id）；现在两页各有自己的 page id，菜单授权可以只给审计而不连带声明系统设置。

i18n 同样按命名空间拆分（`static/js/i18n/audit-console.js`），由既有的合并机制并入 `window.__cowI18N__`。

**页签由标记驱动，所以「接线」本身是需要被测的契约**。该模块把头文件导出的 `window.switchTokenUsageTab` 交给标记里的内联 `onclick` 调用——这正是 `chat.html` 里每一组页签（`agent-detail-tab` / `config-tab` / `memory-tab` / `knowledge-tab`）的既有写法，也是本模块头注释写明的约定。代价是这份契约有一半在 HTML 里，而 HTML 那一半不会因为函数存在就被验证：函数、面板、加载器、接口全都到位，缺的只是按钮上那个 `onclick`，页面照常渲染、汇总卡照常有数、接口照常作答——**一个全绿的测试套件分不出「页签能用」和「页签是死的」**，因为没有一条断言问过「按下它是否到达那个切换函数」。

因此 D9 附带的测试义务是：页签的可达性（标记里每个按钮都点名自己的 id，且模块导出了那个函数）与行为（切换会移动可见面板、移动选中样式、重新拉取该页签的数据）各测一层。见 `tests/test_token_usage_tabs_frontend.cjs`。

## D10 分组名与条目名按它们实际容纳/打开的页面命名

边界下移到逐项之后，两个名字与实际内容不再一致，本次一并纠正（2026-09-25）：

- **分组**：`nav_group_platform_ops` 显示名由「平台运维」改为「平台管理」（en `Platform Management`）。该组同时容纳**管理**（租户管理、平台用户管理、品牌设置）与**运维观测**（运行日志、审计日志、Token 消耗），「运维」只覆盖后者，让「租户管理」显得格格不入。
- **条目**：`menu_platform` 显示名由「系统设置」改为「平台用户管理」（en `Platform User Management`），并把该页标题（`platform_title`）与搜索占位（`platform_search_placeholder`）从「平台账号」对齐为同一名称。条目名原本与它打开的页面（`#view-platform`，注释仍写着 `System Settings - Platform view`）矛盾，且「系统设置」是 `nav_system` / `admin.settings` 这个部署级配置页的名称——同一个词被两处使用，其中一处是错的。

两点是刻意的：

- **只改显示名，不改菜单键**。`nav_group_platform_ops` / `menu_platform` 作为 key 保留，因此 `console.js` 的 `VIEW_META`、菜单授权（`nav:` 授权行）、i18n 覆盖用例都无需改动——只有三语取值与快照夹具的 12 个值变化。
- **规范上要跟着改一次**。分组名写在已验收的 `console-information-architecture` 里（「控制台按…平台运维分组」「平台运维保持平台边界」），因此本 change 为它补了一条 MODIFIED delta：既改名为「平台管理」，也把边界从「分组保持平台边界」改回它与实现一致的**逐项**表述，并新增两个把逐项行为钉住的 Scenario。`unified-console-access` 里的「平台运维」不在此列——那里指的是**职责领域**而非侧栏标签（它约束的是资格校验，措辞与行为都仍然成立），改它属于纯改名，收益为零。
