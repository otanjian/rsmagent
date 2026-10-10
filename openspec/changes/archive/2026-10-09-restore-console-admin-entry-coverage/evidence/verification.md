# 验证留痕：restore-console-admin-entry-coverage

产物完成不等于实现完成；以下为本次候选的实际执行结果。

## 基线与候选标识

- 分支：`rdai`
- 远端/合并基线提交：`03516085`（Merge remote-tracking branch 'gitea/rdai' into rdai）
- 失败复现基线：`node --test tests/test_admin_area_group_gating.cjs` → 8 过 1 败，`ReferenceError: window is not defined`
- 上游同步门禁候选摘要：`candidate_digest = 9507aa69c3fe1a6f6a4e70400590d32dd63e30bad57dcffedc157a7290bbb602`，`source_sha = 48c0d79c36146950667f8d1884ab823deae62305`
- 环境：macOS；Node v24.14.1；`.venv/bin/python`；Playwright 经 `NODE_PATH=$(npm root -g)` 解析（仓库 `node_modules` 未安装 playwright）

## 行为证据

1. 单元夹具（`tests/test_admin_area_group_gating.cjs`）
   - `node --test tests/test_admin_area_group_gating.cjs` → **14 tests / 14 pass / 0 fail**（原 9 例 + 新增 5 例）。
   - 原「无权限身份打开 `/admin` 弹回 `/chat`」两条断言未删改。
2. 浏览器拒绝页面（`tests/test_console_parity_browser.cjs`，经 `tests/test_knowledge_console_browser.cjs` 装配壳执行）
   - `COW_PARITY_BROWSER=1 COW_BROWSER_SCENARIO='denied admin'` → **4 scenarios / 0 failed**：
     - `parity: denied admin entry keeps registered deep routes (classic|split)`
     - `parity: denied admin landing returns to the conversation (classic|split)`
   - 断言：`/settings/models` 实际显示「无权访问」、地址保留、控制台入口隐藏、`/api/models` 计数为 0；刷新及从获准工作台后退后复核；`/admin` 最终进入对话页（`/` 或 `/chat`）。
3. 必要用例执行（`scripts/check-upstream-sync.py --output ...`）→ `passed: true`，各 layer 为空；新增单元用例与 4 个浏览器场景逐名执行并通过。
4. 失败闭环探针（4.3）：隔离候选中临时删除夹具 `window` 声明后，同一门禁入口 **exit 1**，`required-cases` 列出 6 条已登记 Node 用例失败及文件非零退出；恢复后同一入口 `passed: true`。
5. 跳过闭环探针（4.4）：以门禁自带 `browser_errors()` 复核，完整运行 errors 为空；从结果中移除 4 个新增场景（模拟 skip/未执行）后逐名报错。
6. 本地统一门禁：`bash scripts/check-production.sh --local` → **EXIT=0**，Python 回归 / Route coverage / Upstream sync / Web module seams / Desktop build / Native Web / Web and Desktop behavior 全部 PASS（本机未跑镜像与远端，脚本已声明 NOT RUN）。
7. `openspec validate restore-console-admin-entry-coverage --strict` → valid。

## 文件差异范围

仅测试、门禁脚本、必要用例清单与本 change 文档：

- `tests/test_admin_area_group_gating.cjs`
- `tests/test_console_parity_browser.cjs`
- `scripts/check-production.sh`
- `docs/design/upstream-sync/required-tests.json`
- `openspec/changes/restore-console-admin-entry-coverage/**`

`channel/web/static/js/console.js`、页面外壳、前后端路由及其他生产代码未改动。

## 环境适配与未完成项

- 本机 Playwright 为全局安装，运行门禁需 `NODE_PATH=$(npm root -g)`；`scripts/check-production.sh` 无执行位，本机以 `bash` 调用。二者为环境适配，非本 change 缺陷，也未修改脚本内容以外的东西。
- 未运行：Linux 镜像验收、远端服务 / 已安装 Desktop 包 / 目标容量（脚本明确 NOT RUN）；不影响本 change 的测试与门禁范围。
