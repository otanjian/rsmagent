# Tasks: 该页显示名改为「系统接入」

> 依据：`openspec/specs/console-information-architecture` 的「系统接入位于模型与接入分组」（本 change 由「外部系统接入位于模型与接入分组」重命名而来），其要求「导航、页标题、面包屑及场景跳转 SHALL 一致」。delta 见 `specs/console-information-architecture/spec.md`。

## 1. 回归测试（RED）

- [x] 1.1 `tests/test_external_connections_frontend.cjs`：新增用例断言三种语言下 `menu_external_connections` 与 `ec_title` 相等且为「系统接入」/「系統接入」/「System Access」——改前实测 `'外部系统接入' !== '系统接入'` 失败
- [x] 1.2 同文件：断言 `chat.html` 菜单项的静态回退文本等于简体中文翻译值，且不出现任何一种语言的旧名
- [x] 1.3 同文件：断言用户可见文案中不再出现旧名（三种语言各自的旧译名）
- [x] 1.4 `tests/test_external_connections_menu.py`：断言该页在角色分配的菜单资源投影中的 `name` 为新名——改前实测 `'外部系统接入' != '系统接入'` 失败

> 1.2 的断言按实际约束收敛：`chat.html` 每个菜单项只有**一个**静态回退文本（简体默认，`applyI18n()` 随后按语言覆盖），因此它只能等于简体值，不可能同时等于三种翻译。原先「等于同一语言下的翻译值」的写法对 zh-Hant / en 恒不成立，实测暴露后已连同 spec 正文一并改正。

## 2. 实现（GREEN）

- [x] 2.1 `channel/web/static/js/i18n/external-connections.js`：三种语言的 `menu_external_connections` 与 `ec_title` 改为系统接入 / 系統接入 / System Access
- [x] 2.2 `channel/web/chat.html`：菜单项 `data-i18n="menu_external_connections"` 的静态回退文本同步
- [x] 2.3 `auth/service.py`：`admin.external_connections` 的 `label` 同步
- [x] 2.4 `scripts/seed_external_connections.py`：该脚本建立的访问角色名同步
- [x] 2.5 提及该页名的注释与英文对照对齐（`console.js`、`external-connections.js`、`console.css`、`auth/{policy,store,capability_matrix}.py`、`chat.html` 的 VIEW 注释）
- [x] 2.6 `tests/fixtures/console_i18n_snapshot.json`：对应键的值同步（键集合不变；改动为 6 行，未重排文件）

## 3. 验证

- [x] 3.1 新增用例与既有前端用例全通过：`test_external_connections_frontend.cjs`（含本 change 的 2 条）全绿、`test_external_connections_browser.cjs`、`test_desktop_external_broker.cjs` 6/6；`test_console_i18n_parity.cjs` 在本 change 应用后仍全绿
- [x] 3.1.1 与并行 change 的交叉观测：同一工作树内 `add-external-connection-agent-assignment` 正在改动同一批文件，向 i18n 追加了 43 个 `ec_assign_*` / `ec_action_agents` / `ec_toast_agents_*` 键但尚未更新 `tests/fixtures/console_i18n_snapshot.json`，因此 `test_console_i18n_parity.cjs` 当前有 2 条失败。逐键比对确认这 43 个键**无一属于本 change**（本 change 的 6 个值在 actual 与 expected 中完全相同，且无键被删除）；该失败是对方 change 的收口项，不在本 change 范围内
- [x] 3.2 后端子集全通过：`test_external_connections_menu.py`、`test_console_menu_mapping.py`、`test_console_migration_drill.py`、`test_tenant_admin_skills_menu.py`、`test_external_connections_browser.py` = 67 passed / 1 skipped
- [x] 3.3 `openspec validate rename-console-entry-system-access --strict` 通过；`--deltas-only` 确认 RENAMED 与 MODIFIED 两个 delta 都被解析（`deltaCount: 2`）
- [x] 3.4 变异测试确认三处各自被守住：单独回退 i18n 值 / 单独回退 `chat.html` 回退文本 / 单独回退服务端 `label`，用例分别转红；恢复后全绿
- [x] 3.5 真实运行面：侧边栏与页标题在 zh / zh-Hant / en 下均为新名，DOM 内无旧名；服务端 `label` 经进程重启后复验（监听 9899 的旧进程 PID 2014 启动于改动之前，内存中持有旧 `_SIGNED_CONSOLE_PAGES`，`GET /api/tenant/authorization/catalog?kind=menu` 实测已由「外部系统接入」变为「系统接入」，资源 id `nav:admin.external_connections` 不变）。证据见 `evidence/live-console/results.json` 与 `evidence/live-console/console-system-access.png`

## 4. 收口

- [x] 4.1 能力标识、视图 id、路由与 `data-view` 未变：`external-system-access-console` / `admin.external_connections` / `external_connections` / `#view-external_connections` 原样保留
- [x] 4.2 记录真实运行面观测值作为证据：`evidence/live-console/results.json` + 截图
- [x] 4.3 有意不改的引用登记：`external-system-access-console` Purpose、`console-settings-organization`、`personal-email-integration`、`erp-connection-integration` 使用能力概念词汇（理由见 proposal）；`desktop/dist/**` 为 gitignore 的构建产物，待重新构建自动带上

## 5. 待办（本 change 之外）

- [ ] 5.1 归档时由权威 spec 承载：RENAMED 与 MODIFIED 会写入 `openspec/specs/console-information-architecture/spec.md`，届时「外部系统接入」只余能力概念词汇引用。若随后决定改能力标识（本次已决定不改），需单独起 change 并同步 `openspec/config.yaml`。
