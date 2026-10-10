## Why

上游对齐把控制台地址路由（`ConsoleRoutes`）引入 `console.js` 后，`_applySidebarPermissions` 开始引用运行时全局 `window`（`channel/web/static/js/console.js:23364`）。但控制台导航的单元夹具——`tests/test_admin_area_group_gating.cjs` 用 `vm` 从一个沙箱里抽取并执行若干导航函数——只声明了 `document`/`location`/`sessionStorage` 等少量全局，**没有声明 `window`**。于是"未获准入的身份打开 `/admin` 仍应弹回 `/chat`"这条用例（`tests/test_admin_area_group_gating.cjs:209`）在 `!window.ConsoleRoutes` 处先抛 `ReferenceError: window is not defined`，根本执行不到断言。

此前基线实测确认这是**远端分支自身既有缺陷**，不是合并引入：在当时的 `gitea/rdai` 提交 `be88ae9d`，该文件 9 例中 8 过 1 败；在本地合并前提交 `3c16e461`，9 例全过。当前合并提交 `03516085` 也复现 8 过 1 败；仅在内存中补 `window: {}` 后，原 9 例全过。该文件既未列入 `scripts/check-production.sh` 的直接用例列表，也未登记进其调用的必要用例清单 `docs/design/upstream-sync/required-tests.json`，因此这条失败没有被现有门禁执行并阻断。

既有 `console-route-lifecycle` 已规定页面按登记决定区域、深路径授权及明确拒绝状态；`tests/test_console_parity_browser.cjs` 也已有 classic/split 下 `/settings/models` 被拒后保留地址、显示拒绝且不请求模型数据的用例。但现有浏览器场景只拒绝模型页，尚未覆盖「所有管理页面均不可读、管理区入口资格也被拒绝」与 `/admin` 落地页的区别；导航单元夹具也只支持 `/admin`、`/chat` 两种地址。

实际管理页面使用 `/agents`、`/settings/models` 等登记地址，**没有登记 `/admin/<page>` 页面**。当前实现会在入口资格被拒绝时先写入会话级拒绝标记，再判断是否重定向，因此合法深路径保留地址时也会置位该标记。本 change 按这些真实行为补齐覆盖，不新增路径或改变标记语义。

## What Changes

- 修正 `tests/test_admin_area_group_gating.cjs` 的 `vm` 夹具，使其声明被抽取导航函数实际引用的生产运行时全局（`window`）；不改动任何行为断言，仅补齐夹具依赖。
- 为未获准入身份补齐 `/admin` 落地页与已登记管理页面地址（如 `/agents`、`/settings/models`）的单元覆盖，支持显式 `pathname` 并接入真实区域解析；两类地址均保留现有拒绝标记写入，只有落地页重定向。
- 保留已有单页拒绝浏览器场景，扩展同一浏览器测试文件以覆盖「所有管理页面均不可读」：classic/split 下合法深路径显示拒绝、保留地址、不发起目标受限数据请求，并验证落地页回到对话入口。页面渲染由实际装配测试验收，单元测试只证明其所执行函数的行为。
- 将单元文件纳入本地统一门禁 `scripts/check-production.sh`，并在 `required-tests.json` 中登记必要单元用例及新增浏览器场景名称，使失败、跳过或未执行均暴露为门禁缺口。
- **不修改**生产脚本 `channel/web/static/js/console.js` 的任何行为；本 change 只恢复夹具一致性、补齐覆盖与门禁登记。

## Capabilities

### New Capabilities

（无。）

### Modified Capabilities

- `console-route-lifecycle`: 在既有登记路径及拒绝状态要求下，补充入口资格被拒绝时 `/admin` 与已登记管理页面的区别，并明确现有拒绝标记语义和路由装配适用范围。
- `fork-upstream-decoupling`: 明确以抽取生产函数进行单元验证的夹具必须声明被抽取代码引用的生产运行时全局（不仅是辅助函数），且受影响的控制台导航用例必须纳入本地门禁与必要用例清单。

## Impact

- 测试：`tests/test_admin_area_group_gating.cjs`（夹具声明、真实路径及区域解析、分支断言）；`tests/test_console_parity_browser.cjs`（复用现有浏览器框架新增入口资格被拒绝的场景）。
- 门禁与清单：`scripts/check-production.sh` 的 `node --test` 用例列表；`docs/design/upstream-sync/required-tests.json` 的单元用例及浏览器场景登记，浏览器执行复用现有 `check-upstream-sync.py` 入口。
- 生产代码：**无**。`channel/web/static/js/console.js`、`chat.html` 与后端路由均不改动。
- 数据唯一归属：不涉及身份/租户/业务数据；导航拒绝标记仍为会话级客户端状态，不扩大任何授权。
- 跨 change 依赖：与进行中的 `restore-upstream-web-parity-and-sync-gates` 共用路由契约与"必要用例实际执行并约束交付入口"要求（见 `fork-upstream-decoupling`）；本 change 不重复其对 `console.js` 的功能改动，只补夹具与覆盖，避免与其 delta 争用同一 requirement 名。
- 既有缺陷边界：既有红灯仅修 `tests/test_admin_area_group_gating.cjs`；浏览器文件仅补本 change 的行为覆盖，不顺手修复其他既有失败。
