# 任务：审计日志与 Token 消耗

## 1. 事实核对（开工前）

- [x] 1.1 核对源实现（`C:\oneagent-multi-rc`）两个页面的结构：筛选栏、汇总卡片、页签与表格列，确认「照源实现」的具体交付物。
- [x] 1.2 核对数据源差异：源实现的审计在各租户库、用量在每个租户工作区的 `conversations.db`（`manager._resolve_stores` 读取时扇出），本仓库两者都在 `identity.db`；据此确定不照抄扇出（design D1）。
- [x] 1.3 核对计量点差异：源实现逐 provider 埋点（自报有十个 provider 一条都不上报），本仓库所有 provider 都经 `AgentLLMModel.call` / `call_stream`；据此确定埋在分发点（design D5）。
- [x] 1.4 确认「平台运维」分组（`nav_group_platform_ops`）的菜单边界是平台级（`sidebar-hidden-platform-scope`），据此划定菜单与接口两条边界（design D8）。**已于 7 节修订**：该组内六个条目的边界并不相同，分组级判定用最宽条目的边界约束了最窄的两个。
- [x] 1.5 盘点既有审计写入：`auth/audit.py`（追加式 + 脱敏 + `audit_events`）、`_audit_in_tx` 与 `record_audit` 两个写缝，以及已存在的动作名层级（`auth.login` 之外由本次补）。

## 2. 审计侧：读取缝与动作补齐

- [x] 2.1 `AuditStore.query_events`：显式命名范围（`tenant` / `platform` / `all`），`scope="tenant"` 缺 `tenant_id` 直接报错而不是退化成不过滤。
- [x] 2.2 `query_events` 增加 `actor_username`（转义 `LIKE`，支持片段）与 `exclude_result`（「失败」= 不等于成功），并单独 `COUNT(*)` 出 `total`。
- [x] 2.3 `AuditStore.distinct_actions`：动作名从表里派生并按同范围过滤，不另立清单。
- [x] 2.4 `IdentityService.query_audit_events` / `audit_action_names`：Web 层不直接开 `identity.db` 的读缝。
- [x] 2.5 登录（`IdentityService.login`）在创建会话的同一连接上写 `auth.login`（只记 restricted 标志，不记口令与 token）；登出（`DbAuthLogoutHandler`）写 `auth.logout`。
- [x] 2.6 智能体管理动作补齐 `agent.create` / `agent.update` / `agent.archive` / `agent.delete` / `agent.knowledge_mode.set` / `agent.bind_channel_instance`，并记录 `result="denied"`（403 拒绝）与 `result="error"`（抛异常）；与既有 `<动作>.denied` 写法并存。
- [x] 2.7 审计写入全部 best-effort：留痕失败 MUST NOT 改变原拒绝或原成功结果。

## 3. 计量侧：store / manager / recorder / instrument

- [x] 3.1 迁移 33：`token_usage`（日/租户/主体/供应商/模型唯一键 + 累加计数，`tenant_id` 用 `''` 而非 `NULL` 以免唯一键被 NULL 语义击穿）与 `llm_call_logs`（逐次调用）。
- [x] 3.2 `TokenUsageStore`：单条 `ON CONFLICT DO UPDATE` 累加；读取侧（`summary` / `query_details` / `by_user` / `query_call_logs` / `distinct_values`）带 `tenant_id=None` 表示不过滤、并支持 `actor_username` 片段匹配。
- [x] 3.3 `TokenUsageManager`：队列 + 后台 flush 线程（调用方不等 SQLite）；同键先在内存合并；读取前 `_flush()`；`_fill_usernames` 读时补齐展示名（含未命中缓存）且不回落到别人的名字。
- [x] 3.4 `record_token_usage` / `record_llm_call`：身份取自 `current_identity()` 而非参数（谁传 user_id 谁就能把用量记到别人账上），摘要限长为一行、凭据形态打码。
- [x] 3.5 `instrument.py`：`meter_llm_call` / `meter_response` 处理 dict、迭代器（含非流式返回生成器）、异常与被放弃迭代四种形态；计时从调用前开始；失败记 `status="error"` 后原样重抛。
- [x] 3.6 `bridge/agent_bridge.py` 两个分发点接入计量（覆盖全部 provider）。
- [x] 3.7 用例：`tests/test_token_usage.py`（schema/迁移、累加、租户隔离、展示口径）与 `tests/test_token_usage_instrument.py`（四种结算形态）。

## 4. 接口与权限 key

- [x] 4.1 `admin_audit_handlers.py`：`GET /api/admin/audit/events` 与 `GET /api/admin/audit/actions`，`audit_read_scope()` 单点判定（平台管理员 `all` 可收窄 / 租户管理员 `tenant` 忽略 `?tenant=` / 其余 403）。
- [x] 4.2 `admin_token_usage_handlers.py`：`GET /api/admin/token-usage` 单地址服务 `summary` / `details` / `by-user` / `call_logs` / `actors` / `models` / `providers`，范围判定复用 4.1 的同一点。
- [x] 4.3 行投影同时给出源实现的字段名（`timestamp`/`actor`/`status`/`resource_type`）与本仓库自有字段（`changes`/`target`/`tenant_id`），`result="denied"` 映射为 `status="failure"` 但保留可区分的 `result`。
- [x] 4.4 `_SIGNED_CONSOLE_PAGES` 登记 `admin.audit`（审计日志）与 `admin.token_usage`（Token 消耗），`scope="platform"`。
- [x] 4.5 `/auth/context` 投影对两页作答 `mode == "all" or is_platform_admin or is_admin`（**7 节修订**，原为「仅平台管理员」），并明确区分 `no_permission`（资格不够）与 `consumer_closed`（部署断言）；同组 `admin.tenants` 仍作答 `no_permission`、其余三项仍作答 `consumer_closed`。
- [x] 4.6 `route_registry.py` 登记三条路由（`personal` 策略，由 handler 兜底判定），`web_channel.py` 重导出 handler；`scripts/check-route-coverage.py` 通过（196 路由）。
- [x] 4.7 读故障返回可区分的失败码，MUST NOT 以空列表冒充「无记录」。

## 5. 前端：两个视图与 i18n

- [x] 5.1 `chat.html`：侧边栏「平台运维」新增 `Token 消耗` 项，并补 `#view-audit` / `#view-token_usage` 两个视图的筛选栏、汇总卡片、页签与表格。
- [x] 5.2 `console.js`：`VIEW_META` 增加 `audit` / `token_usage` 两行（`nav_group_platform_ops` + 各自 page id）。
- [x] 5.3 `audit-console.js`：通过 `registerConsoleView` 注册两视图（审计与用量分别 `load`/`repaint`），日期初始化、查询、渲染、分页与页签切换；读失败与空结果分开呈现。
- [x] 5.4 `identity-admin.js`：让出旧 `loadAuditView` 注册（原先借用 `admin.settings` 页面 id）。
- [x] 5.5 i18n：新增 `static/js/i18n/audit-console.js` 命名空间（`zh` / `zh-Hant` / `en`），`navigation.js` 更新 `menu_audit` 并新增 `menu_token_usage`；`menu_token_usage` 不在两个命名空间重复声明。
- [x] 5.6 `scripts/check-web-module-seams.py` 通过（22 上游模块 / 293 fork 专有符号 / 0 findings）。

## 6. 回归与交付

- [x] 6.1 `tests/test_console_view_registry.cjs`：`audit` 从 `identity-admin.js` 移出，改为断言 `audit-console.js` 注册 `audit` 与 `token_usage`；`FORK_LOADERS` 去掉 `loadAuditView`。
- [x] 6.2 `tests/fixtures/console_i18n_snapshot.json`：按 delta 复核后更新（`zh` / `zh-Hant` / `en`），并复跑确认增量为 0。
- [x] 6.3 `node --test "tests/*.cjs"`：与 HEAD 基线逐条对比，未引入任何新增失败（基线 67 条测试级失败，本次 59 条，均为既有失败，另修复 8 条）。
- [x] 6.4 新增 `tests/test_admin_audit_console_http.py`：范围判定、过滤语义（失败=非成功、字面值、时间边界含当天）、投影、400/500 区分、动作清单同范围。
- [x] 6.5 新增 `tests/test_audit_token_console_wire.py`：在真实 WSGI 应用上验证跨租户隔离（含租户管理员借 `?tenant=` 越权被忽略）、各页签形态、主体下拉范围、以及 `/auth/context` 的三类身份答案。
- [x] 6.6 用 `scripts/check_change_deltas.py` 校验本 change 的 delta：无 delta 问题（报出的三条为仓库级 seam 覆盖约定，已用另外三个未归档 change 复现同样三条）。evidence.md 中的路径写成占位而非原样，避免该检查因「路径被引用」而误报通过（本仓库已有一个 change 处于该状态）。
- [x] 6.7 浏览器端实测两个页面。平台管理员 `admin` 在 `/admin` 侧边栏完整可见「平台运维」六项；租户管理员的作用域读与页面可用性由 6.5 的真实 Wire 用例与 6.4 的投影用例承担。
- [x] 6.8 重启后端并做真实 HTTP 验证：路由随代码上线、`/admin` 与 `/chat` 均 200、页面加载的脚本与磁盘文件逐字节一致（公网入口与 `127.0.0.1:9900` 返回同长度同内容）。

## 7. 修订：菜单边界由分组级下移到逐项级（2026-09-25）

交付后实测发现：租户管理员在 `/admin` 侧边栏完全看不到「平台运维」，因而也看不到这两页。成因是 1.4 的判定——整组带 `sidebar-hidden-platform-scope`，而组内 `admin.settings` / `admin.branding` / `admin.logs` 的 consumer 本就关闭、`admin.tenants` 必须跨租户，用它们的最宽边界统一约束只读的两页，代价是这两页对租户管理员彻底不可见。

- [x] 7.1 `chat.html`：「平台运维」分组去掉 `sidebar-hidden-platform-scope`；`tenant` / `platform` / `branding` / `logs` 四项各自加 `platform-scope-only`（`platform` 的 `data-perm="platform"` 保留）。分组本身不再携带任何平台级隐藏。
- [x] 7.2 `console.js`：新增 `_isPlatformOnlyEntry(item)`（识别 `platform-scope-only` 或 `data-view="platform"`）；`_applySidebarPermissions` 对非平台管理员**逐项** `hidden: true`；空分组判定改为不计平台专属项，普通成员因此不会看到一个空的「平台运维」表头。
- [x] 7.3 `auth/policy.py`：`BUILTIN_MENU_DEFAULTS["tenant_admin"]` 增加 `admin.audit` / `admin.token_usage`（新租户即取即用）。
- [x] 7.4 `auth/store.py`：迁移 34 为**既有**内置 `tenant_admin` 回填 `nav:admin.audit` / `nav:admin.token_usage`；只动已持有 `menu` 授权的内置行（否则会把功能权限型角色的门禁**打开**而隐藏其余页面），`member` 一行不动（它对这两个接口本就 403，授权等于广告一个答 403 的面）。
- [x] 7.5 `auth/service.py`：`_console_pages_projection` 对两页按 4.5 修订口径作答。
- [x] 7.6 用例：`tests/test_admin_area_group_gating.cjs` 扩到 9 条（平台管理员六项全可见 / 租户管理员恰好看这两项 / 普通成员连表头都没有）；`tests/test_channel_scope_nav_frontend.cjs` 断言分组无平台类且逐项带标记；`tests/test_audit_token_console_wire.py` 的 `PageProjectionTests` 更新为「租户管理员被提供两页」并新增「同组平台专属项不被提供」。
- [x] 7.7 复跑零可见性闸门 `tests/test_console_menu_mapping.py`（33 passed）：该用例断言「开菜单授权不得收走一个原本可读的页面」，修订后它通过的理由是**这两页进了默认授权**，而不是把页面报成不可用——即闸门仍指向正确的原因。

## 8. 修订：分组与条目的显示名（2026-09-25）

现场实测发现两处名字与实际内容不符，一并纠正（design D10）：

- [x] 8.1 `i18n/navigation.js`：`nav_group_platform_ops` 三语由「平台运维 / 平台維運 / Platform Ops」改为「平台管理 / 平台管理 / Platform Management」。
- [x] 8.2 `i18n/navigation.js`：`menu_platform` 三语由「系统设置 / 系統設定 / System Settings」改为「平台用户管理 / 平台用戶管理 / Platform User Management」。
- [x] 8.3 `i18n/identity-admin.js`：该条目的页面标题与搜索占位对齐为同一名称（`platform_title` / `platform_search_placeholder` 三语），消除「条目叫系统设置、页面叫平台账号」的矛盾。`chat.html` 的静态兜底文本同步。
- [x] 8.4 只改显示名，**不改菜单键**：`nav_group_platform_ops` / `menu_platform` 作为 key 保留，`console.js` 的 `VIEW_META`、`nav:` 菜单授权与覆盖用例均不受影响。
- [x] 8.5 `tests/fixtures/console_i18n_snapshot.json`：上述 12 个取值同步（三语 × 4 键）；`test_console_i18n_parity.cjs` 6 与 `test_console_i18n_coverage.cjs` 4 通过。
- [x] 8.6 引用旧名的注释与用例名同步（`console.js`、`console.css`、`audit-console.js`、`identity-admin.js`、`auth/service.py`、`auth/policy.py`、`auth/store.py`、三个 `.cjs` 用例与 `test_audit_token_console_wire.py` 的 docstring）。归档 change 与历史设计文档不改写。
- [x] 8.7 规范：新增 `specs/console-information-architecture/spec.md` 的 MODIFIED delta——改名并把边界改为与实现一致的逐项表述，新增「租户管理员只多出组内被授权的条目」「普通成员看不到平台管理分组」两个 Scenario；基线 5 个 Scenario 逐字保留。`scripts/check_change_deltas.py` 无新增问题（仍为那三条仓库级 seam 覆盖，与任何未归档 change 逐字相同）。

## 9. 修订：补上 Token 消耗 三个页签的接线（2026-09-25）

现场实测发现三个页签点不动。函数、面板、加载器与接口都在，缺的是标记里那一行 `onclick`（见 9.2）。

- [x] 9.1 `chat.html`：`#token-usage-tabs` 的三个按钮各补 `onclick="switchTokenUsageTab('<id>')"`（`details` / `by-user` / `call-logs`），与本文件其它各组页签（`agent-detail-tab` / `config-tab` / `memory-tab` / `knowledge-tab`）保持一致，并调用模块已导出的那个全局函数。
- [x] 9.2 根因：`audit-console.js` 的 `window.switchTokenUsageTab` 只为内联处理器而导出（模块头注释写明「the ported markup calls them from inline `onclick`」），但标记没有调用它；`wireTokenUsage()` 也没有为页签按钮挂监听（它只处理筛选控件、模型框回车与刷新按钮）。移植时漏了标记那一半。
- [x] 9.3 新增 `tests/test_token_usage_tabs_frontend.cjs`（6 条），分两层：
  - **可达性**：标记里每个页签按钮都在内联处理器里点名自己的 id，且模块确实导出了 `switchTokenUsageTab`；另断言标记的页签集合与模块切换的面板集合一致。
  - **行为**：以最小 DOM/fetch 桩加载模块，调用切换后断言可见面板恰好移动一个、选中样式随之移动、并以 `type=by-user` / `type=call_logs` 重新拉取该页签的数据。
- [x] 9.4 反向验证该用例不是空断言：把三个 `onclick` 从 `chat.html` 摘掉后重跑，恰好 1 条失败（`every Token 消耗 tab button calls the switch the module exports`），恢复后文件与原文逐字节相同（SHA-256 一致）。
- [x] 9.5 全量 CJS：987 测试 / 927 通过 / 60 失败——失败数与改动前相同（既有的浏览器与账号面板用例），本次净增 6 条测试、6 条通过、0 条新增失败。

## 验收记录

用例输出、与既有约定的交叉核对、以及未执行项的边界说明见 [`evidence.md`](./evidence.md)。
