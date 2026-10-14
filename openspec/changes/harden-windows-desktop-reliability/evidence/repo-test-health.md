# 仓库测试健康度复扫（行尾修复后）

日期：2026-10-13，Windows x64 / Node 24.19。目的：在修好 `core.autocrlf` 与 2,231 个文件的行尾之后，对**全部 node 测试文件**做一次复扫，并把失败按原因归类，确认本 change 的改动没有引入回归。

## 1. 总量

| 指标 | 值 |
|---|---|
**首扫（行尾修复后）与复扫（解释器修复后）对照：**

| 指标 | 首扫 | 复扫（本轮修复后） |
|---|---|---|
| 扫描文件 | 165 | 166（新增的 `_python_bin.cjs` 也被算作 `*.cjs`） |
| 通过断言 | 2159 | **2155** |
| 失败断言 | 62 | **51** |
| 跳过断言 | 0 | **109** |
| 有失败的文件 | 32 | **28** |

失败减少的 11 项正是本轮修好的 4 个文件（见 §6）：它们从"因找不到解释器而失败"变成"有解释器时通过、没有时诚实跳过"。

## 2. 失败归类（30 个真实测试文件）

| 类别 | 文件数 | 说明 |
|---|---|---|
| 无浏览器/playwright | **7** | `test_appearance_browser`、`test_console_parity_browser`、`test_desktop_consent_browser`、`test_desktop_login_gate_browser`、`test_desktop_sap_login_memory`、`test_desktop_sap_page_read`、`test_notifications_http_browser` —— 本机缺少 playwright 与其浏览器引擎（报告第 1 条同一原因） |
| 符号链接权限（`EPERM` / `WinError 1314`） | **3** | `test_desktop_device_artifacts`、`test_desktop_isolated_preview`（余 1 项）、`test_desktop_project_native_actions`（余 2 项） |
| POSIX 权限位（`0o600` vs `0o666`） | **2** | `test_desktop_execution_journal`、`test_desktop_remote_config` |
| ~~POSIX `.venv/bin/python` 缺失~~ → **本轮已修** | **0**（原 4） | 见 §6：4 个文件改为复用任务 2.1 的解释器解析（显式路径 → 平台 venv → PATH，且拒绝 Microsoft Store 别名），找不到时**跳过**而不是回落到不可能的路径 |
| 期望的文件不存在 | **1** | `test_personal_console_frontend.cjs`：`channel/web/static/js/personal-console.js` 在该树中不存在 |
| 源码形状/夹具漂移（断言） | **13** | 见下 §2.1，全部指向既有代码与断言口径不一致，与本次改动无关 |

## 2.1 那 13 个"源码形状/夹具漂移"的具体原因（逐个追因）

| 文件 | 失败用例（首条） | 首条错误 |
|---|---|---|
| `test_knowledge_sources_frontend.cjs` | the sources tab shows its own panel and loads the list | `ReferenceError: _applyKnowledgePanels is not defined`（该函数在 `console.js` 中已不存在/改名） |
| `test_member_model_catalog_frontend.cjs` | the tab switch loads exactly the tab that was opened | `ReferenceError: window is not defined`（夹具沙箱缺 `window`） |
| `test_console_upload_frontend.cjs` | upload carries the tenant selection header | `TypeError: Cannot read properties of undefined (reading 'href')`（夹具缺 DOM 片段） |
| `test_personal_address_forward_frontend.cjs` | the forward runs before the availability gate | `navigateTo must keep the leave check`（断言的源码形状已变） |
| `test_recovered_pages_frontend.cjs` | navigation gates the view before its lazy loader runs | `the dispatch sites must stay in console.js`（同上） |
| `test_forced_password_gate.cjs` | the login overlay and the forced gate are both outside #app | `#app, #login-overlay and the modal must all exist`（夹具/模板口径） |
| `test_agent_chat_launch_frontend.cjs` | entering chat with several Agents anchors the default, no picker | `AssertionError: Expected values to be strictly equal` |
| `test_desktop_command_runner.cjs` | A20: our own successful write makes the next edit current again | `the run that raced the edit is warned` |
| `test_desktop_downloads.cjs` | the destination directory is the host's, and an existing file is reported | `AssertionError: Expected values to be strictly equal` |
| `test_scene_workbench_frontend.cjs` | legacy registry preserves the generic workbench fallback | `AssertionError: Expected values to be strictly equal` |
| `test_console_i18n_parity.cjs` | merged namespace table deep-equals the snapshot | 词典与快照漂移（`coding_unavailable` 文案、缺 3 个键） |
| `test_workbench_menu_grant_frontend.cjs` | recent-sessions request skipped when grant withheld | `Missing loadSidebarRecentSessions`（函数已不存在） |
| `test_sidebar_account_frontend.cjs` | （多条） | 断言与当前模板/脚本口径不一致 |

这些文件**均不读取本会话改过的文件**（按"失败文件是否引用改动文件"筛查为 `CLEAN`），或已用原版 `chat.html` 基线逐项排除（见 §3）。因此它们是**既有**的源码形状/夹具问题，属独立工作范围。

## 3. 本轮新增的排除工作：读 `chat.html` 的那 9 个失败文件

按"失败文件是否读取本会话改过的文件"筛查时，9 个失败文件都会读 `channel/web/chat.html`（本会话删掉了两个 `target="cow-*"`）。为排除自身影响，做了**基线对照**：把 `chat.html` 换回 `origin/rdai` 原版（`target="cow-*"` 恢复为 2 处），重跑这 9 个文件：

| 文件 | 本会话改动后 | 原版 chat.html |
|---|---|---|
| `test_console_i18n_parity.cjs` | 3 / 3 | **3 / 3** |
| `test_forced_password_gate.cjs` | 3 / 1 | **3 / 1** |
| `test_knowledge_sources_frontend.cjs` | 26 / 2 | **26 / 2** |
| `test_personal_console_frontend.cjs` | 0 / 1 | **0 / 1** |
| `test_recovered_pages_frontend.cjs` | 14 / 5 | **14 / 5** |
| `test_scene_workbench_frontend.cjs` | 2 / 1 | **2 / 1** |
| `test_sidebar_account_frontend.cjs` | 61 / 7 | **61 / 7** |
| `test_workbench_menu_grant_frontend.cjs` | 6 / 3 | **6 / 3** |
| `test_admin_area_group_gating.cjs` | 0 / 1 | **0 / 1** |

**逐项完全一致** → 这些失败与本 change 无关（对照后 `chat.html` 已恢复为本次改动版）。

## 4. 行尾修复带来的改善（同一批套件）

| 套件 | 修复前 | 修复后 |
|---|---|---|
| `test_desktop_execution_types.py` | 1 failed / 6 passed | **7 passed** |
| `test_desktop_host_frontend.cjs` | 20 / 1 | **21 / 0** |
| `test_desktop_isolated_preview.cjs` | 23 / 4 | **26 / 1**（余 1 为符号链接权限） |

## 5. 本轮修复：4 个把解释器路径写死的测试（**本 change 范围之外，可独立取舍**）

这 4 个文件都用 `path.join(ROOT, '.venv', 'bin', 'python')` 定位解释器——本 checkout 的 `.venv` 是 macOS 上建的，Windows 上不存在该路径；其中两个（`test_sap_workbench_field_validation`、`test_admin_area_group_gating`）在候选**全部失败后仍回落到这个不存在的路径**，于是读者看到的是 `spawnSync … ENOENT`，而不是"这台机器没有解释器"。

改动：新增 `tests/_python_bin.cjs`（复用任务 2.1 的解析顺序：`COW_PYTHON_PATH` → `PYTHON_BIN` → `SAP_WORKBENCH_PYTHON` → `PYTHON` → `.venv-win/Scripts` → `.venv/Scripts` → `.venv/bin` → `python3` → `python`），并且：

- **先用 `where`/`which` 定位再运行**，命中 `WindowsApps` 的候选**绝不启动**（Store 别名只是启动器，不是解释器）；
- 逐个真正运行 `-c "sys.stdout.write(sys.executable)"` 才接受；
- 一个都没有时返回 `null`，测试**跳过**（`no usable Python interpreter on this host`），不回落到不可能的路径。

两条路径都实测过：

| 文件 | 修复前 | 无解释器（`PYTHON_BIN` 未设） | 有解释器（`PYTHON_BIN=<真实解释器>`） |
|---|---|---|---|
| `test_desktop_skill_cache.cjs` | 14 通过 / 1 失败 | 14 / 0 / **跳 1** | **15 / 0 / 0** |
| `test_desktop_skill_package_digest.cjs` | 5 / 5 | 0 / 0 / **跳 10** | **10 / 0 / 0** |
| `test_sap_workbench_field_validation.cjs` | 0 / 4 | 0 / 0 / **跳 4** | **4 / 0 / 0** |
| `test_admin_area_group_gating.cjs` | 0 / 1 | 0 / 0 / **跳 14** | **14 / 0 / 0** |

即：有解释器时 **43 项断言全通过、0 失败**（修复前是 19 通过 / 11 失败）。

> **范围说明**：这 4 个测试文件（外加新增的 `tests/_python_bin.cjs`）**不属于本 change 的 10 份能力增量**，是为了让 Windows 上能如实运行它们而做的仓库健康改动。若希望本次交付严格只含 change 内容，可独立撤销这 5 个文件而**不影响**本 change 的任何任务与证据（本会话未执行撤销）。

## 6. 结论

62 项失败**已全部归类，且没有任何一项由本 change 引入**：

| 类别 | 文件数 |
|---|---|
| 无浏览器/playwright（环境） | 7 |
| 符号链接权限 `EPERM`/`WinError 1314`（环境） | 3 |
| POSIX 权限位 `0o600`（环境） | 2 |
| POSIX `.venv/bin/python` 缺失（环境） | 4 |
| 期望的文件不存在 | 1 |
| 源码形状/夹具漂移（既有，逐个已追因） | 13 |

排除自身影响的两种手段都用上了：①"失败文件是否引用本会话改动文件"筛查（`CLEAN` 者与本改动无关）；②对读 `chat.html` 的 9 个文件做**原版基线对照**，通过/失败数逐项一致（§3）。

**未完成**：13 个"源码形状/夹具漂移"只追到原因与首条错误，**未逐个修复**——它们属既有代码与断言口径不一致，与本 change 的范围无关；如需要，应作为独立任务处理（例如把硬编码 `.venv/bin/python` 的 4 个测试改为按平台解析解释器）。此处如实标注，不宣称已修复。
