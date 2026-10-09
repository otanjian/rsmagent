## Context

- `tests/test_admin_area_group_gating.cjs` 通过 `fnSource()` 抽取 `_consolePageForView`、`_viewNavDenied`、`_sidebarRecentDenied`、`_qualifyAdminConsoleEntry`、`_isPlatformOnlyEntry`、`_applySidebarPermissions` 六个生产函数。沙箱声明了 `document`、`location`、`sessionStorage` 等依赖，却遗漏 `_applySidebarPermissions` 读取的 `window`；无权限身份打开 `/admin` 的用例在行为断言前抛出 `ReferenceError`。
- 此前基线记录：远端提交 `be88ae9d` 为 8 过 1 败，本地合并前提交 `3c16e461` 为 9 过 0 败。审核在当前合并提交 `03516085` 复现了前者；仅在内存中补 `window: {}` 后原 9 例全过。该文件未进入本地门禁的直接列表或必要用例清单。
- `channel/web/route_registry.py` 的 `CONSOLE_PAGES` 登记 `/agents`、`/settings/models` 等管理页面及页签；`/admin` 仅为管理首页，没有 `/admin/<page>` 登记。`channel/web/static/js/fork/router.js` 从服务输出的元数据解析路径，`_navAreaFromPath` 在路由可用时委托该解析；固定返回 `area` 的单元桩不能证明路径有效。
- `_applySidebarPermissions` 在入口资格被拒绝时先写 `cow_nav_admin_denied = '1'`，再判断是否跳转。因此 `/admin` 跳回 `/chat`，合法管理深路径在路由可用时不跳转，但同样写标记。该函数不渲染拒绝页；渲染与阻止目标加载由 `navigateTo → showUnavailableView` 等页面导航逻辑负责。
- 现有 `tests/test_console_parity_browser.cjs` 已覆盖 classic/split 下模型单页被拒时的地址、拒绝文案及模型请求计数；尚需补充所有管理页面均不可读、入口资格也被拒绝的身份场景。既有主规范已覆盖登记路径、权限拒绝及深路径恢复，本 change 只补入口资格这一边界。

## Goals / Non-Goals

**Goals:**

- 补齐真实运行时全局，保留既有行为断言，恢复导航单元测试。
- 基于真实登记地址验证管理首页与非首页管理页面的区别，保留生产拒绝标记语义。
- 以单元测试验证菜单、标记与重定向，以实际装配的浏览器测试验证拒绝页面及目标数据请求边界。
- 将新增覆盖纳入现有统一门禁及必要清单，并以失败候选验证门禁执行。

**Non-Goals:**

- 不修改 `console.js`、路由模块、后端路由、页面外壳等生产代码，不新增 `/admin/<page>` 地址，不改变拒绝标记的写入或消费时机。
- 不新增路由模块缺失时对规范管理地址的降级支持；不将模块缺失下未发生重定向解释为页面可用。
- 不处理归档与活跃 `restore-upstream-web-parity-and-sync-gates` 的重复，不顺带修复其他既有失败。

## Decisions

1. **保留原测试并补齐全局。** `boot()` 默认提供 `window: {}`，先恢复原 9 例；原无权限 `/admin` 用例的两条断言保持不变。再支持显式 `pathname` 与 `window` 成员注入，未传路径的旧 `area` 调用仍派生原地址。
2. **新路径场景执行真实区域解析。** 装载生产 `_navAreaFromPath`；路由可用场景装载生产 `fork/router.js`，使用权威 `CONSOLE_PAGES` 或实际页面输出的元数据，避免复制第二份路径清单。不得沿用固定返回 `admin` 的桩证明路径有效。测试先确认 `/agents`、`/settings/models` 可解析且属于管理区，并确认 `/admin/agents` 不被解析为已登记页面。
3. **按现状记录重定向及标记。** 无入口资格身份为已认证、权威投影已知、非平台/租户管理员且无任何获准管理入口的成员。单元矩阵如下；每个场景使用新的会话存储，以验证本次调用产生的标记。

   | 路由能力 | 地址 | 单元断言 |
   | --- | --- | --- |
   | 可用 | `/admin` | 标记为 `1`，`location.replace('/chat')` |
   | 可用 | `/agents`、`/settings/models` | 标记为 `1`，不重定向，管理入口及受限菜单隐藏 |
   | 不可用 | `/admin` | 保持原标记与重定向行为 |
   | 不可用 | `/settings/models` | 真实 `_navAreaFromPath` 回落为工作台，此函数不重定向、不写标记；只记录既有函数边界，不声明页面正常或已拒绝 |

   路由不可用时，生产后备判断只识别 `/admin` 和 `/admin/` 前缀，不能推导所有规范管理地址都会重定向。该装配故障不在本 change 中扩展为新的降级能力。获准成员另外覆盖路由可用时的 `/admin` 与对应管理页，确认无重定向且不新增标记。
4. **复用浏览器框架验证可见结果。** 保留已有仅模型页被拒的场景，在 `tests/test_console_parity_browser.cjs` 新增所有管理页被拒的身份变体：明确非平台管理员、非租户管理员、`authorization_mode: 'role'`，将权威投影中全部管理页设为不可用、不可读且菜单拒绝，保留获准工作台页面。classic/split 均验证 `/settings/models` 的可见拒绝状态、地址不变、管理入口隐藏及 `/api/models` 请求为零，并在刷新和从获准工作台后退后复核。另验证 `/admin` 最终回到对话页，允许旧 `/chat` 按既有规范规范化为 `/`。浏览器不以缺少 `location.replace` 调用代替渲染断言，也不假定会话标记在页面其他逻辑消费后仍存在。
5. **规范只补已有要求的入口边界。** `console-route-lifecycle` 用独立 ADDED requirement 描述入口资格被拒时的分流，沿用既有登记路径及拒绝状态规则。`fork-upstream-decoupling` 的独立 requirement 明确全局依赖与必要用例登记，不重复活跃 change 正在处理的同名门禁 requirement；同时要求渲染声明有实际装配证据。
6. **复用现有门禁。** 单元文件加入 `check-production.sh` 的直接列表及 `required-tests.json.node`，后者列出必要测试名称；新增浏览器场景加入已有 `browser.names`，由现有 `check-upstream-sync.py` 执行并核对实际通过结果。故意缺 `window` 的候选验证失败，恢复后使用同一入口验证；新增浏览器场景被跳过或未执行也必须被清单检查识别。

## Risks / Trade-offs

- 单元夹具仍可能随生产依赖漂移；补齐全局并登记门禁使故障暴露，真实路由模块与权威元数据减少手工桩造成的误判。
- 新增浏览器场景复用现有 runner、资源及认证响应框架，增加有限运行时间。缺浏览器依赖、用例被过滤或未执行均记为未通过，不以单元测试替代。
- 审核只确认了夹具故障、内存补桩结果和路由/标记分支，新增浏览器身份场景仍待实现并执行，不能提前声明已通过。
- 本 change 依赖 `restore-upstream-web-parity-and-sync-gates` 的路由模块、页面登记与浏览器门禁。开始测试实现前核对目标提交及接缝；若发现需修改生产代码才能满足场景，应记录具体差异并重新评估范围，不弱化断言或顺带改变生产行为。
- 改动限于测试、门禁清单及本 change 文档，无数据迁移或 feature flag。恢复时可回退这些文件；生产路由及客户端标记语义不受文档调整影响。
