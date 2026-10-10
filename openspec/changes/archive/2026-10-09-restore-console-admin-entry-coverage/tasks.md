## 1. 核对依赖并恢复夹具（依据 `fork-upstream-decoupling`「控制台导航单元夹具声明生产运行时全局并纳入门禁」）

- [x] 1.1 固定目标提交与远端基线，核对生产 `CONSOLE_PAGES`、`fork/router.js`、`_navAreaFromPath`、`_applySidebarPermissions` 及现有浏览器执行入口；记录本 change 依赖切片可用的证据，再开始后续测试实现。
- [x] 1.2 在 `tests/test_admin_area_group_gating.cjs` 的 `boot()` 中先仅补 `window: {}`，复现失败并确认原 9 例通过；保留原 `/admin` 用例的两条断言，不删除、不弱化。
- [x] 1.3 支持显式 `pathname` 与 `window` 成员注入；未传路径的旧 `area` 调用保持原行为，默认 `window` 为空对象。新路径场景执行真实 `_navAreaFromPath`，路由可用时装载生产 `fork/router.js` 并复用权威页面元数据，不使用固定返回 `admin` 的区域桩。

## 2. 覆盖真实地址的入口分支（依据 `console-route-lifecycle`「未获准入的管理区入口区分落地页与深路径」）

- [x] 2.1 验证 `/agents`、`/settings/models` 是已登记管理地址，`/admin/agents` 不能解析为已登记页面；不新增生产路由，也不把未登记路径当作合法深路径。
- [x] 2.2 对已知投影、非平台/租户管理员且无任何获准管理页的身份，覆盖路由可用时的 `/agents`、`/settings/models`：保留路径、`location.replaced === null`、拒绝标记为 `1`，管理入口及受限菜单隐藏；这些单元断言不宣称拒绝页面已渲染。
- [x] 2.3 覆盖同一无权限身份在路由可用和不可用时打开 `/admin`：均写拒绝标记并跳 `/chat`；另覆盖获准成员在路由可用时进入 `/admin` 及其获准管理页面，不重定向且不新增拒绝标记。
- [x] 2.4 用真实 `_navAreaFromPath` 记录路由模块缺失时 `/settings/models` 回落到工作台、该函数不跳转也不写标记的既有边界；不把此结果作为页面可用或拒绝渲染证据，不承诺所有管理地址均有降级重定向。

## 3. 验证实际拒绝页面（依据 `console-route-lifecycle` 的入口分流及既有拒绝状态要求）

- [x] 3.1 保留 `tests/test_console_parity_browser.cjs` 已有仅模型页被拒的 classic/split 场景，新增「全部管理页均不可读」身份变体；确认认证响应与投影均为普通成员、无管理员旁路，所有管理页及菜单拒绝，保留获准工作台页面。
- [x] 3.2 classic/split 分别直接打开 `/settings/models`，断言拒绝视图实际可见、文案为「无权访问」、地址保留、管理入口隐藏、`/api/models` 请求为零；刷新后及从获准工作台后退后重复核对，不能仅断言无重定向。
- [x] 3.3 同一身份在 classic/split 打开 `/admin`，验证最终进入对话页（允许 `/chat` 规范化为 `/`），不发起被拒管理目标的数据请求；标记写入由单元测试证明，不要求浏览器其他逻辑消费标记后仍保留它。

## 4. 纳入门禁与必要用例清单（依据 `fork-upstream-decoupling` 的夹具与必要执行要求）

- [x] 4.1 将 `tests/test_admin_area_group_gating.cjs` 加入 `scripts/check-production.sh` 的 `node --test` 列表，并登记至 `required-tests.json.node`，列明必要测试名称，含落地页、真实深路径、获准身份及路由缺失边界。
- [x] 4.2 将新增 classic/split 浏览器场景逐名登记进 `required-tests.json.browser.names`；保留既有场景登记，复用当前 `check-upstream-sync.py` 浏览器执行入口，不另建 runner。
- [x] 4.3 在隔离候选中临时移除夹具 `window` 声明，使用实际门禁入口验证失败；恢复后以同一入口验证并记录对应结果，证明文件确实执行且退出状态约束门禁。
- [x] 4.4 验证新增浏览器场景未执行或被跳过时必要用例检查失败；恢复完整执行后逐名核对通过结果，不能以测试文件退出码或旧报告代替。

## 5. 验证与留痕

- [x] 5.1 执行 `node --test tests/test_admin_area_group_gating.cjs`，记录新增后的用例数与全部通过结果。
- [x] 5.2 执行包含新增场景的浏览器回归，留存 classic/split 拒绝视图、地址及目标请求计数的证据，记录刷新、后退及落地页结果。
- [x] 5.3 运行现有统一门禁及必要用例执行检查，记录同一候选的结果；缺依赖、skip、未执行或无关失败均如实列出，不以相关子集通过冒充整体通过，也不顺带修复其他红灯。
- [x] 5.4 核对本 change 的文件差异仅涉及两份测试、门禁脚本、必要用例清单及本 change 文档；确认 `console.js`、页面外壳、前后端路由和其他生产代码未改动。
- [x] 5.5 保存固定远端基线与候选提交/内容标识、行为证据及未完成项；运行 `openspec validate restore-console-admin-entry-coverage --strict`，文档完成不计为实现或验收完成。
