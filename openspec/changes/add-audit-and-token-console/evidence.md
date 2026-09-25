# add-audit-and-token-console 验证记录

本文件记录 change `add-audit-and-token-console` 的规范校验、红/绿、真实 Wire 的范围隔离、前端契约与回归。所有结论只来自实际执行过的输出；未执行的项在第 6 节明说，不含推断。

## 1. 规范校验

本机没有 Windows 版 `openspec` CLI（`openspec validate --strict` 不可用），因此用仓库自带的 `scripts/check_change_deltas.py`——它做的正是 `--strict` 的核心检查：ADDED 不能重述基线已有要求、MODIFIED 的目标必须存在于基线、每条要求的 Scenario 必须落在正确的 delta 块里：

```
$ python scripts/check_change_deltas.py add-audit-and-token-console
FAIL (proposed): 3 problem(s)
  - <the conversation-store seam module>: marked seam:conversation-store but this change never names it
  - <the scheduler integration module>: marked seam:scheduler but this change never names it
  - <the scheduler web-update test>: marked seam:scheduler but this change never names it
```

三条**全部**来自 `check_conflict_coverage`（上游冲突 seam 的覆盖检查），**没有一条 delta 问题**：本 change 的 ADDED/MODIFIED 对基线是干净的（没有「ADDED 重述已有要求」「MODIFIED 目标不存在」「Scenario 未落在 delta 块内」）。

三条与本次改动无关，且是仓库级约定而非本 change 的缺陷。对任一未归档 change 逐字相同：

```
$ python scripts/check_change_deltas.py auto-bind-channel-sender
FAIL (proposed): 3 problem(s)      # 同样三条
$ python scripts/check_change_deltas.py add-agent-capability-search
FAIL (proposed): 3 problem(s)      # 同样三条
$ python scripts/check_change_deltas.py enable-personal-wecom-bot-runtime
FAIL (proposed): 3 problem(s)      # 同样三条
```

上面的路径刻意写成占位而非原样：该检查的判定方式是「这条 seam 行的路径字符串是否出现在本 change 的 `*.md` 里」，把失败输出原样粘进来会让它**误报通过**——一条因为被引用而「被覆盖」的 seam 行，正是这个闸门想要防止的假阳性。（本仓库已有一个 change 正处于这种状态：它的 `evidence.md` 原样引用了同一段输出，于是 `check_change_deltas.py <它>` 报 `OK`。本次不复制这个做法。）

seam 覆盖由归档期的 `fork-decoupling-and-tenant-hardening` 承担：

```
$ python scripts/check_change_deltas.py
OK (applied): fork-decoupling-and-tenant-hardening — deltas consistent with the baseline, every conflicted file covered
```

## 2. MODIFIED 目标的基线比对

`audit-log` 的 MODIFIED 头「审计查询有边界」与基线 `openspec/specs/audit-log/spec.md` 逐字一致，且基线原有的两个 Scenario（「普通成员读取审计被拒」「租户管理员查询本租户审计」）在 delta 中原样保留（checker 的 `drops baseline scenarios` 检查未报）。新增的三条 Scenario 是本次要落成页面后才会出现的事实：参数越权被忽略、失败过滤在新增非成功结果后仍成立、改名账号的历史事件仍可过滤。

`token-usage-console` 是本 change 新增的 capability（`## ADDED Requirements`），ADDED 的三条要求在基线与其它 capability 中都不存在（checker 的「重述已有要求 / 重述其它 capability 的要求」两项均未报）。

## 3. 红/绿

命令与结果（`python -m pytest ... -q`）：

```
$ python -m pytest tests/test_audit_query.py tests/test_token_usage.py tests/test_token_usage_instrument.py -q
52 passed in 122.69s
```

```
$ python -m pytest tests/test_admin_audit_console_http.py -q
28 passed in 0.54s
```

```
$ python -m pytest tests/test_audit_token_console_wire.py -q
21 passed in 8.94s
```

```
$ python -m pytest tests/test_capability_matrix.py -q
20 passed in 9.20s
```

```
$ python -m pytest tests/test_menu_grant_enforcement.py -q
9 passed in 39.32s
```

```
$ python -m pytest tests/test_personal_console_pages.py -q
26 passed in 140.24s
```

```
$ python -m pytest tests/test_console_menu_mapping.py tests/test_personal_console_pages.py -q
1 failed, 58 passed in 282.69s
```

那条失败是本 change 被既有契约抓到的**真实缺陷**，值得记下来：

```
E       AssertionError: {'admin.token_usage', 'admin.audit'} is not false : lost pages: [...]
tests\test_console_menu_mapping.py:440: AssertionError
FAILED tests/test_console_menu_mapping.py::ZeroVisibilityRegressionTests::test_a_tenant_admin_keeps_its_management_pages
```

**成因**：可用性投影最初把两页对**租户管理员**也报成 `available/read_allowed = True`（理由是 handler 确实允许其读本租户）。但该分组内其它页面的声明 permission 是空串，兜底分支的 `read_ok` 实际是 `pid in permissions`，对空串恒为 `False`——所以那两页成了**唯一**「在有菜单授权之前可用、开启菜单授权之后被收走」的页面，正是这条用例存在的意义。

**修正**：投影按分组的平台边界作答（平台管理员以外一律 `no_permission`）。租户管理员的作用域读仍是接口契约（`AuditScopeTests` 证明它在真实 Wire 上成立），但它不冒充控制台页面可用性。该结论连同边界一起写进 `design.md` D8。

**该修正后来被推翻**（第 9 节）：把页面报成不可用是让闸门闭嘴，不是回答问题。「页面可读、却没有菜单授权」是一个**授权缺口**，正确的修法是补上授权（默认值 + 迁移 34），而不是回头否认页面可用。修订后本条用例依旧通过，理由换成了前者。

修正后：

```
$ python -m pytest "tests/test_console_menu_mapping.py::ZeroVisibilityRegressionTests::test_a_tenant_admin_keeps_its_management_pages" "tests/test_audit_token_console_wire.py::PageProjectionTests" -q
4 passed in 13.49s
```

## 4. 真实 Wire：范围隔离与页面投影

`tests/test_audit_token_console_wire.py` 在真实 `build_web_app()`（真库、真会话 cookie、真租户头、真路由策略）上运行，两个租户各有一条审计与一条用量。断言的是只有装配好的应用才能回答的问题：

- **跨租户隔离**：平台管理员 `scope=all`（含平台范围行），租户管理员 `scope=tenant` 且事件里只有自己的租户。
- **参数越权**：租户管理员带 `?tenant=<另一租户>` 时，返回的仍是**自己**租户（不是 403，也不是对方的数据）；平台管理员带同一参数时被收窄到该租户。同一对断言在用量侧的 `summary` 上重复一遍。
- **拒绝**：普通成员对两个接口都是 403；无会话是 401/403。
- **过滤语义**：`status=failure` 保留 `result="denied"` 的行并把它映射为 `status="failure"`；`status=success` 丢掉它；`actor=ali` 命中按记录名匹配的行，`actor=nobody` 返回空。
- **动作清单同范围**：租户管理员读不到另一租户独有的 `tenant.copy_agents`、也读不到平台范围的 `user.set_status`；平台管理员两个都能读到。
- **用量页签**：`summary` / `details` / `by-user` / `call_logs` 各返回自己的形态；`actors` 只给出可见范围内的名字（租户管理员只看到 `alice`，平台管理员看到 `alice` 与 `foreign`）；未知页签是 400。
- **投影**：平台管理员与租户管理员都看到两页 `available/read_allowed=True` 且 `reason` 为空（**9 节修订**；初版对租户管理员作答 `no_permission`）；普通成员一律 `(False, False, "no_permission")`——**不是** `consumer_closed` / `deferred`（那是关于部署的断言，而部署是服务这两页的）。同组的 `admin.tenants` 对租户管理员仍作答 `no_permission`，`admin.settings` / `admin.branding` / `admin.logs` 仍作答 `consumer_closed`。

## 5. 前端契约与仓库级闸门

```
$ node --test tests/test_console_view_registry.cjs tests/test_console_i18n_coverage.cjs tests/test_console_i18n_parity.cjs
ℹ tests 16
ℹ pass 16
ℹ fail 0
```

```
$ node --check channel/web/static/js/audit-console.js        -> OK
$ node --check channel/web/static/js/i18n/audit-console.js   -> OK
$ node --check channel/web/static/js/console.js              -> OK
```

```
$ python scripts/check-web-module-seams.py
OK: 22 upstream module(s), 293 fork-only symbol(s), 0 findings
```

```
$ python scripts/check-route-coverage.py
route-coverage: 196 routes (68 upstream, 128 fork), 241 method entries
OK
```

i18n 快照：先用一次性 delta 核对脚本逐键打印增/删/改（`zh: +0 -0 ~0` 之外的三语差异均来自本次新增的 `audit-console` 命名空间与 `menu_audit`/`menu_token_usage` 两键），核对确认无意外改动后再重写 `tests/fixtures/console_i18n_snapshot.json`，复跑 delta 为

```
== zh: +0 -0 ~0
== zh-Hant: +0 -0 ~0
== en: +0 -0 ~0
no key overlaps
```

CJS 全量并与 HEAD 基线逐条对比（基线用 `git worktree add <path> HEAD` 取干净树跑同一命令）：

- 基线：`ℹ tests 937 / pass 867 / fail 70`
- 本次：`ℹ tests 976 / pass 916 / fail 60`
- 归一化后的测试级失败集合差异：**新增 0 条**，另**修复 8 条**（基线失败而本次通过，属既有的并发/顺序型抖动，与本 change 无关）。

## 6. 未执行项（明说边界）

- **浏览器端实测两个页面**：**已补做**。平台管理员 `admin` 在 `/admin` 的侧边栏完整可见「平台运维」组，末两项即「审计日志 / Token 消耗」（`#sidebar-nav` innerText 尾串 `… 运行日志 审计日志 Token 消耗`；两项 `offsetParent` 非空、渲染尺寸 205×44）。原先记为未执行，是因为自动化浏览器与服务的网络命名空间不一致（`127.0.0.1:9900` 落到 `chrome-error://`）；改走公网入口 `https://rd.rsmxm.com.cn/admin` 后可达。
- **重启后端并做真实 HTTP 验证**（路由随代码上线、页面加载的脚本与磁盘文件逐字节一致）：**已补做**。后端由 pid 10668（旧代码）重启为 1160，5 项健康探活全 200；新路由匿名访问得 401（受门禁而非 404，排除「路由没上线」）；迁移 33 已落到线上 `identity.db`（`token_usage` / `llm_call_logs` 与索引就位）；平台管理员的九个 panel 全 200，`?tenant=` 收窄返回 `scope=tenant`，伪造租户 id 得 403；审计读回了本轮真实登录写下的 4 条 `auth.login` 事件。

## 7. 与既有约定的交叉核对

- **零可见性回归**：见第 3 节。这是本次唯一一处由既有用例抓出并修正的行为面变化。
- **追加式审计不被削弱**：本次只新增写入与读取；`auth/audit.py` 的 `query_tenant` / `query_platform` 原样保留，新增的 `query_events` 是分页过滤的读缝，且在 store 层强制 `scope` 显式命名（`scope="tenant"` 缺 `tenant_id` 直接抛 `AuditError`，不会退化成不过滤）。
- **计量不改变调用契约**：`instrument.py` 只观察——dict 原样返回、迭代器包一层转发后原样交出、异常先记 `status="error"` 再原样重抛；所有写入异常都被 catch 到 `logger.debug`/`warning`，不会把一次成功的 LLM 调用变成失败。
- **`tenant_id` 的 `''` 与 `NULL`**：`token_usage` 的 `tenant_id` 用 `''` 而非 `NULL`，因为唯一键里的 `NULL` 不参与相等比较（多行 `NULL` 会各自成立），会把「同日同人同模型」的累加变成多行。审计侧的 `NULL` 语义（平台范围）保持原样不动。
- **身份取自环境而非参数**：`recorder.py` 只从 `current_identity()` 取主体与租户，参数覆盖仅留给「明知更准」的调用方（例如为特定租户重放的计划任务），因为「谁传 user_id 谁就决定这笔账记在谁头上」。

## 8. 交付后发现的缺陷（已修复）

**i18n 命名空间加载顺序**（2026-09-25 浏览器实测发现）。

`chat.html` 把 `assets/js/i18n/audit-console.js` 放在 `assets/js/console.js` **之后**。而 `console.js` 在自身执行时用一个 IIFE 把 `window.__cowI18N__` **快照**进查找表（`mergeI18nNamespaces`，`console.js:928`），此后再注册的命名空间不会被并入。于是 `audit_title` / `token_usage_title` / `audit_filter_*` / `token_usage_*` 全部回落到键名本身，页面直接渲染出 `audit_title` 这样的裸键。

`chat.html` 在按域 i18n 块上方本就写明了该约定（「They must run before console.js merges `window.__cowI18N__` into the lookup table」），其余 18 个命名空间文件都遵守，唯独本次新增的这个违反了。

两点必须写清：

- 该缺陷对**所有账号**可见，包括平台管理员；它与本文的按范围可见性无关，只是恰好同屏出现，容易与「租户管理员看不到菜单」混为一谈。
- 它是**静默**的：命名空间确实注册了（`window.__cowI18N__['audit-console'].zh.audit_title === "审计日志"`），快照夹具与既有 i18n 用例全部通过。既有用例查的是「键是否齐全」，而这里是「加载顺序」，所以一条都没抓到。

修复：把该 `<script>` 从视图模块旁移入按域 i18n 块（`appearance.js` 之后）。视图模块 `audit-console.js` 仍留在 `console.js` 之后——它依赖 `registerConsoleView`，方向相反，两处不可互换。

回归防护：`tests/test_console_i18n_parity.cjs` 新增用例，断言 `chat.html` 里每个 `assets/js/i18n/*` 都出现在 `assets/js/console.js` 之前。该用例对修复前的顺序**确实失败**（在内存中重放旧顺序验证，输出 `guard violations: ["assets/js/i18n/audit-console.js"]`），因此不是空断言。

验证：修复后页面文本为 `审计日志` / `平台管理员查看全部，租户管理员仅当前租户` / `用户` / `Token 消耗` / `按模型与用户统计 LLM Token 使用情况`；`test_console_i18n_parity` 6、`test_console_i18n_coverage` 4、`test_console_view_registry` 7、`test_console_workspace_frontend` 22、`test_channel_scope_nav_frontend` 10、`test_admin_area_group_gating` 6、`test_workbench_menu_grant_frontend` 9 全通过（共 64 条，0 失败）。

## 9. 交付后发现的缺陷（已修复）：租户管理员看不到「平台运维」

2026-09-25 现场实测截图：一个 `tenant_admin` 账号在 `/admin` 的侧边栏里**完全没有**「平台运维」分组，因此看不到新增的两页。这不是 i18n 缺陷（第 8 节那个是独立的，且对平台管理员同样可见），而是菜单边界判在了分组上。

**成因**：`chat.html` 在 `data-group="platform-ops"` 上带 `sidebar-hidden-platform-scope`。整组对该类身份隐藏，而组内六个条目的边界并不相同——`admin.tenants` 跨租户必须平台级、`admin.settings` / `admin.branding` / `admin.logs` 的 consumer 本就关闭，只有 `admin.audit` / `admin.token_usage` 是已被 handler 收窄到本租户的只读视图。用最宽条目的边界约束最窄的两个，结果是接口已经为租户管理员作答、菜单却让他看不见入口。

**修复**（详见第 7 节任务）：边界下移到逐项——分组去掉平台级隐藏，`tenant` / `platform` / `branding` / `logs` 四项各自携带 `platform-scope-only`；`console.js` 以 `_isPlatformOnlyEntry` 逐项门禁，且空分组判定不计平台专属项（普通成员不会看到一个空表头）；`BUILTIN_MENU_DEFAULTS["tenant_admin"]` 补两页，迁移 34 回填既有内置角色。

**线上核对**（`identity.db`，本次重启后）：

```
schema_migrations: version 34 applied_at 1790333874
role_88GUL-gwh1sA50oP (默认租户/tenant_admin): nav:admin.audit, nav:admin.token_usage  created_at 1790333874
role_VCw3X4aelR8D9nv- (AI启航团/tenant_admin): nav:admin.audit, nav:admin.token_usage  created_at 1790332792
```

**投影逐账号核对**（`IdentityService._console_pages_projection` 跑在线上库上）：

```
test15  @ AI启航团   roles=[member, tenant_admin] platform_admin=False mode=role
    admin.audit          available=True  read_allowed=True  scope=platform reason=''
    admin.token_usage    available=True  read_allowed=True  scope=platform reason=''
    admin.tenants        available=False read_allowed=False scope=platform reason='no_permission'
    admin.settings       available=False read_allowed=False scope=platform reason='consumer_closed'
    admin.branding       available=False read_allowed=False scope=platform reason='consumer_closed'
    admin.logs           available=False read_allowed=False scope=platform reason='consumer_closed'
RC001…RC005 / test15-2 @ AI启航团  roles=[member]
    两页均 available=False read_allowed=False reason='no_permission'
paul    @ 默认租户    roles=[member, tenant_admin] platform_admin=False
    两页均 available=True  read_allowed=True
admin   @ 默认租户    platform_admin=True mode=all
    两页 + admin.tenants 均 available=True read_allowed=True
```

即：**看到入口**放宽到租户管理员，**读到哪些行**没有放宽——后者由第 4 节真实 Wire 的 `?tenant=` 越权被忽略与跨租户隔离断言继续钉住。

**线上字节核对**（`127.0.0.1:9900` 与公网入口 `https://rd.rsmxm.com.cn` 返回同一份 HTML，`len=270852`）：

```
platform-ops group present: True
group carries sidebar-hidden-platform-scope: False
platform-scope-only item count: 4
  data-view=tenant       platform-scope-only=True
  data-view=platform     platform-scope-only=True
  data-view=branding     platform-scope-only=True
  data-view=logs         platform-scope-only=True
  data-view=audit        platform-scope-only=False
  data-view=token_usage  platform-scope-only=False
all i18n scripts before console.js: True
```

**复跑结果**：

```
tests/test_audit_token_console_wire.py                  22 passed
tests/test_console_menu_mapping.py                      33 passed   (零可见性闸门)
tests/test_menu_grant_enforcement.py + test_admin_audit_console_http.py  37 passed
node tests/test_admin_area_group_gating.cjs              9/9 pass
node tests/test_channel_scope_nav_frontend.cjs          11/11 pass
node tests/test_console_i18n_parity.cjs                  6/6  pass
```

## 10. 修订：分组与条目的显示名（2026-09-25）

现场看到两处名字与它们实际容纳/打开的内容不符，按用户要求一并改名（design D10）。

**改了什么**：

| key（未变） | 旧显示名 | 新显示名 |
| --- | --- | --- |
| `nav_group_platform_ops` | 平台运维 / 平台維運 / Platform Ops | 平台管理 / 平台管理 / Platform Management |
| `menu_platform` | 系统设置 / 系統設定 / System Settings | 平台用户管理 / 平台用戶管理 / Platform User Management |
| `platform_title`（该页标题） | 平台账号 / 平台帳號 / Platform Accounts | 平台用户管理 / 平台用戶管理 / Platform User Management |
| `platform_search_placeholder` | 搜索平台账号 … | 搜索平台用户 … |

理由：

- **分组**：组内同时容纳**管理**（租户管理、平台用户管理、品牌设置）与**运维观测**（运行日志、审计日志、Token 消耗），「运维」只覆盖后者，让「租户管理」格格不入。
- **条目**：原「系统设置」与它打开的页面（`#view-platform`，页面标题本是「平台账号」）矛盾；且「系统设置」是 `nav_system` / `admin.settings` 这个部署级配置页的名称，同一个词被两处使用，其中一处是错的。条目名、页标题与面包屑 SHALL 一致，所以页面标题与搜索占位一起对齐。

**故意没改**：菜单键 `nav_group_platform_ops` / `menu_platform` 保留原名，因此 `console.js` 的 `VIEW_META`、`nav:` 菜单授权行与覆盖用例都不需要动——只有三语取值与快照夹具的 12 个值变化。这是「改名」不是「重建」。

**规范 delta**：`openspec/specs/console-information-architecture/spec.md` 的 `工作台与管理控制台具有独立导航` 是分组名的权威出处（「控制台按…平台运维分组」「平台运维保持平台边界」），因此本 change 新增 `specs/console-information-architecture/spec.md` 的 MODIFIED delta，改名并把边界改为与实现一致的**逐项**表述，另加两个把逐项行为钉住的 Scenario；基线 5 个 Scenario 逐字保留：

```
$ python scripts/check_change_deltas.py add-audit-and-token-console
FAIL (proposed): 3 problem(s)
  - <the conversation-store seam module>: marked seam:conversation-store but this change never names it
  - <the scheduler integration module>: marked seam:scheduler but this change never names it
  - <the scheduler web-update test>: marked seam:scheduler but this change never names it
```

仍是同样三条仓库级 seam 覆盖（与任何未归档 change 逐字相同，见第 1 节），**没有新增 delta 问题**——MODIFIED 的目标存在于基线、基线 Scenario 未被丢弃。（同上，路径写成占位：这三行的判定方式是「该 seam 行的路径字符串是否出现在本 change 的 `*.md` 里」，把失败输出原样粘进来会让它误报通过。）

`openspec/specs/unified-console-access/spec.md` 里的「平台运维」**未改**：那里指的是**职责领域**（「组织与权限、公共配置维护及平台运维仍 SHALL 校验各自管理资格」），约束的是资格校验而不是侧栏标签，行为与措辞都仍然成立；改它属纯改名，收益为零。

**验证**：见第 11 节。

## 11. 改名后的复跑

```
$ node tests/test_console_i18n_parity.cjs
ℹ tests 6 / pass 6 / fail 0
$ node tests/test_console_i18n_coverage.cjs
ℹ tests 4 / pass 4 / fail 0
```

快照契约（`test_console_i18n_parity.cjs` 的 `the merged namespace table deep-equals the pre-split snapshot`）在 12 个值同步后逐字节相等——这条用例正是「改名有没有漏改某一语」的探测器。

其余受影响的用例（改动点：注释/用例名里的旧名 + 页面标题对齐，无行为变化）：

```
node  test_admin_area_group_gating.cjs          9/9   pass
node  test_channel_scope_nav_frontend.cjs      11/11  pass
node  test_console_view_registry.cjs            7/7   pass
node  test_nav_area_frontend.cjs                6/6   pass
node  test_workbench_menu_grant_frontend.cjs    9/9   pass
python test_branding.py + test_audit_token_console_wire.py + test_admin_audit_console_http.py
      98 passed, 2 subtests passed
python test_console_menu_mapping.py (零可见性闸门)   33 passed
```

全量 CJS 与本次改动前逐条对比（改名只碰 i18n 取值与注释，若有测试钉住旧字面值就会在此暴露）：

```
$ node --test "tests/*.cjs"
ℹ tests 981 / pass 921 / fail 60
```

失败数与本 change 改动前记录的 60 条**完全相同**（§5：当时为 976 测试 / 916 通过 / 60 失败；本次多出的 5 条测试与 5 条通过来自 7.6 新增的分组门禁用例）。失败集中在 `tests/test_appearance_browser.cjs` 与 `tests/test_personal_console_frontend.cjs` 两个既有的浏览器/账号面板用例，与分组门禁、i18n、导航范围、品牌均无关：**改名未引入任何新增失败**。

### 改名后的线上核对（重启后 PID 1236）

四个入口都返回同一份字节（`len=270858`），旧名一处不剩：

```
http://127.0.0.1:9900/admin     new_group=True new_item=True new_title=True old_group=False old_item=False
http://127.0.0.1:9900/chat      new_group=True new_item=True new_title=True old_group=False old_item=False
http://127.0.0.1:9899/admin     new_group=True new_item=True new_title=True old_group=False old_item=False
https://rd.rsmxm.com.cn/admin   new_group=True new_item=True new_title=True old_group=False old_item=False
```

（`new_group` = 服务端 HTML 里 `data-i18n="nav_group_platform_ops">平台管理<`，即 i18n 未解析时的静态兜底文本；`old_*` 是改名前的同名兜底。）

服务端 i18n 文件逐键核对：

```
/assets/js/i18n/navigation.js
    nav_group_platform_ops = ['平台管理', '平台管理', 'Platform Management']
    menu_platform          = ['平台用户管理', '平台用戶管理', 'Platform User Management']
/assets/js/i18n/identity-admin.js
    platform_title                = ['平台用户管理', '平台用戶管理', 'Platform User Management']
    platform_search_placeholder   = ['搜索平台用户', '搜尋平台用戶', 'Search platform users']
```

两个容易误判为「漏改」的残留，实为**不同的键**，故意保留：

- `navigation.js` 仍有「系统设置 / 系統設定 / System Settings」——那是 `nav_system`，部署级配置页的名称，与本次改的条目不是同一个键。
- `identity-admin.js` 仍有「平台账号」——那是 `platform_user_empty`（暂无平台账号）与 `platform_user_edit_title`（编辑平台账号）：它们命名的是页面内的**对象**（账号），不是菜单/页面标签。与本仓库既有的 `成员管理`（菜单）↔ `用户管理`（页标题）同属「菜单词与对象词可以不同」的既有形态，未一并改动。

## 12. 交付后发现的缺陷（已修复）：Token 消耗 的三个页签没有接线（2026-09-25）

现场实测：Token 消耗 页签组里「明细」有数据，但点「按用户汇总」与「调用日志」**没有任何反应**——面板不切、样式不动、不发请求。

**成因**：这一组页签由标记驱动。`audit-console.js` 的头注释写明「Globals are attached (window.loadAuditLog / window.loadTokenUsage / …) because the ported markup calls them from inline `onclick`, exactly as the source does」，并导出 `window.switchTokenUsageTab`；`chat.html` 里每一组其它页签也都用内联 `onclick`（`agent-detail-tab` / `config-tab` / `memory-tab` / `knowledge-tab`）。但移植时**标记那一半没跟过来**：

```
$ grep -rn "data-token-usage-tab\|token-usage-tab-btn\|switchTokenUsageTab" rsmagent
channel/web/static/js/audit-console.js:281:    function switchTokenUsageTab(tabId) {
channel/web/static/js/audit-console.js:287:        var tabs = document.querySelectorAll('#token-usage-tabs .token-usage-tab-btn');
channel/web/static/js/audit-console.js:519:    window.switchTokenUsageTab = switchTokenUsageTab;
channel/web/chat.html:2448:   <button type="button" data-token-usage-tab="details" class="token-usage-tab-btn …">
channel/web/chat.html:2451:   <button type="button" data-token-usage-tab="by-user" class="token-usage-tab-btn …">
channel/web/chat.html:2454:   <button type="button" data-token-usage-tab="call-logs" class="token-usage-tab-btn …">
```

三个按钮既没有内联 `onclick`，`wireTokenUsage()` 也没给它们挂监听（它只处理四个筛选控件、模型框的回车和刷新按钮）。上面这份输出就是缺陷本身：函数定义在第 281 行、导出在第 519 行、按钮在标记里，三者之间**没有任何一条线把它们连起来**。

**为什么全部用例都没抓到**：函数存在、三个面板存在、三个加载器存在、三个 `type=` 形态后端都答（§4 的 `test_each_tab_answers_with_its_own_shape`）、视图注册正确（`test_console_view_registry.cjs`）。于是页面正常渲染、汇总卡有数、`明细` 有数据——**一个全绿的套件分不出「页签能用」和「页签是死的」**，因为没有任何一条断言问过「按下它是否到达那个切换函数」。这是本 change 第二次栽在同一形态上：第 8 节那个是「文件加载顺序」，这次是「标记与函数之间的那一行」——两次都发生在 **HTML 与 JS 的接缝**上，而两次的用例都只测了接缝两侧各自完好。

**修复**：给三个按钮各补 `onclick="switchTokenUsageTab('<id>')"`，与本文件其它各组页签一致，调用模块已导出的那个全局函数。

**回归防护**：新增 `tests/test_token_usage_tabs_frontend.cjs`（6 条），标记侧与行为侧各测一层：

- **可达性**（这条正是原本缺失的）：标记里每个页签按钮都在内联处理器里点名自己的 id，且模块确实导出了 `switchTokenUsageTab`——只测一半仍会漏掉本次的缺陷（函数在、按钮在、连线不在）。另断言标记的页签集合与模块切换的面板集合一致，挡住「加了页签没加面板」的反向情况。
- **行为**：以最小 DOM/fetch 桩加载模块，调用切换后断言可见面板**恰好**移动一个、选中样式随之移动、并以 `type=by-user` / `type=call_logs` 重新拉取该页签的数据。

**反向验证（该用例不是空断言）**：把三个 `onclick` 从 `chat.html` 摘掉后重跑，恰好 1 条失败：

```
✖ every Token 消耗 tab button calls the switch the module exports (3.0045ms)
ℹ tests 6 / pass 5 / fail 1
```

随后从备份恢复，文件与原稿逐字节相同（SHA-256 `13518A1F…4FB1` 前后一致）。

**线上核对**（重启后 PID 3032）：四个入口返回同一份 HTML（`len=270983`），三个页签全部接线。

```
http://127.0.0.1:9900/admin     len=270983  tabs=[details:wired, by-user:wired, call-logs:wired]
http://127.0.0.1:9900/chat      len=270983  tabs=[details:wired, by-user:wired, call-logs:wired]
http://127.0.0.1:9899/admin     len=270983  tabs=[details:wired, by-user:wired, call-logs:wired]
https://rd.rsmxm.com.cn/admin   len=270983  tabs=[details:wired, by-user:wired, call-logs:wired]
```

**全量 CJS**：987 测试 / 927 通过 / 60 失败——失败数与本次修改前逐条相同（仍为 `test_appearance_browser.cjs` 与 `test_personal_console_frontend.cjs` 两个既有失败组），**净增 6 条测试、6 条通过、0 条新增失败**。

```
$ python -m pytest tests/test_audit_token_console_wire.py tests/test_admin_audit_console_http.py tests/test_branding.py -q
98 passed, 2 subtests passed
```

