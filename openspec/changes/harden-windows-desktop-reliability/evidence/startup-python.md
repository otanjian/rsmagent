# 阶段 2 证据：P0 本机启动诊断与开发入口（任务 2.1–2.5）

实施日期：2026-10-13（Windows x64，`D:\AIPJ\rsmagent`）。
对应规范：`specs/desktop-runtime-diagnostics/spec.md`（本机开发模式选择经过实际验证的解释器、启动失败与依赖缺失提供准确恢复动作、Windows 开发入口不继承错误 Electron 运行形态）与 `specs/desktop-tenant-context/spec.md`（远程不启动本机 Python）。

## 1. 改动

| 文件 | 改动 |
|---|---|
| `desktop/src/main/python-probe.ts`（新） | 候选顺序（`COW_PYTHON_PATH` → 开发配置 `python_path` → 平台 venv → PATH）、无 shell 异步探测（版本 + `sys.executable`）、每候选 3 s / 整轮 15 s、超时清理、输出上限、Store 别名识别、显式失败不回退；纯 Node，无 Electron 依赖 |
| `desktop/src/main/python-manager.ts` | 启动前探测解释器（替代"存在即用"的 `findPython()`）；新增失败码 `python_not_found` / `python_invalid` / `dependency_missing`；从启动输出中提取 `ModuleNotFoundError` 的模块名并单独归类 |
| `desktop/src/renderer/src/{types.ts,hooks/useBackend.ts,components/StatusScreen.tsx,i18n.ts}` | 三个新失败码贯通类型、重试倒计时例外、失败页文案与中英双语恢复动作 |
| `agent/tools/browser/browser_tool.py` | 首次使用检查区分"缺 playwright 模块"与"缺浏览器引擎"，各给对应修复动作（复用既有 `has_playwright` 字段，不新建依赖注册表） |
| `desktop/build/setup-windows.ps1`（新） | 解析并探测解释器（含 Store 别名识别与版本窗口）、创建/复用 `.venv-win`、安装 `requirements-desktop.txt`；`-Check` 只报告不创建；不触碰 `.venv`（macOS）、`config.json`、`identity.db`，不删除任何文件 |
| `desktop/build/dev-windows.ps1`（新） | 启动 Electron 前清除 `ELECTRON_RUN_AS_NODE`、把 `.venv-win\Scripts` 放到 PATH 最前、校验其可运行 |
| `docs/desktop-startup-and-python.md`（新） | 解释器选择规则、失败码与恢复动作、可选工具缺依赖、remote 不需要 Python、Windows 快速开始 |

## 2. 真实验证（本机 Windows）

### 2.1 解释器探测（任务 2.1）

| 场景 | 实际结果 |
|---|---|
| PATH 上的 `python3` / `python`（本机**确实是**应用商店占位别名） | 分类为 `store_alias`，原文摘录：`Python was not found; run without arguments to install from the Microsoft Store...` |
| 真实解释器 `D:\tools\micromamba\envs\py312\python.exe` | 探测通过：`{"v": [3, 12, 13], "e": "D:\\tools\\micromamba\\envs\\py312\\python.exe"}`，版本在窗口内 |
| `node --test tests/test_desktop_python_probe.cjs` | 18 项：**17 通过 / 0 失败 / 1 跳过**（跳过项是"本机 PATH 上有可用解释器"的环境相关用例，本机 PATH 只有 Store 别名——这本身就是 §4.2 的现场事实）。带 `COW_TEST_PYTHON` 指向真实解释器复跑：该项**通过**（`3.12.13`、`sys.executable` 正确） |

### 2.2 Windows 准备与启动脚本（任务 2.4）

| 命令 | 实际结果 |
|---|---|
| `setup-windows.ps1 -Check -Python <真实解释器>` | `interpreter D:\tools\micromamba\envs\py312\python.exe (3.12.13)`，打印计划，**exit 0**；确认**未创建** `.venv-win` |
| `setup-windows.ps1 -Check -Python python`（Store 别名） | `python -> Microsoft Store placeholder, not an interpreter`，**exit 1**，未创建任何文件 |
| 两个脚本语法解析 | 均 `parses clean`（`[Parser]::ParseFile`） |
| `node --test tests/test_desktop_startup_contract.cjs` | 7 项全部通过，含"准备脚本不得触碰 macOS `.venv`/`config.json`/`identity.db` 且不删除任何文件"、"启动脚本必须在启动 Electron 前清除 `ELECTRON_RUN_AS_NODE`" |

`dev-windows.ps1` 的完整启动（真正拉起 Electron 窗口）**未执行**：本机无可用 `.venv-win` 且会打开 GUI；其契约由上面的静态用例锁定，真实启动归阶段 7 的安装包/开发冒烟。

### 2.3 后端可启动性（任务 2.2 的"核心依赖"部分）

在**独立临时数据目录**内实跑本机后端（`COW_DATA_DIR=<临时目录>`，端口 19899，解释器为窗口内的 3.12.13）：

- `GET /api/health` → `{"status": "ok"}`，启动日志完整（`Starting channels: ['web']`、`Scheduler loop started`）；
- 临时目录内生成 `identity.db`、`run.log`、`tenants/` 等，**仓库根的 `config.json`、`identity.db` 未被读写**；临时目录用后已删除。

**副作用如实登记**：这次实跑中后端把 4 个内置技能同步到了本机 Agent 工作区 `C:\Users\admin\cow\skills`——该目录的**修改时间被更新**（16:50:39），但逐一核对后**没有任何新增、修改或删除的文件**（内容均为 9/5–10/8 的既有文件），`~/.cow/run.log` 也未被写入。业务数据（身份库、配置、记忆）未被触碰。为避免重复此类副作用，本次不再重复该实跑。

### 2.4 远程模式与依赖分类（任务 2.2 / 2.5）

| 检查 | 证据 |
|---|---|
| remote 不探测/不启动本机 Python | `tests/test_desktop_startup_contract.cjs`：remote 分支内不含 `startBackend()`，且 `await startBackend()` 全文件仅一处（非 remote 分支） |
| 缺依赖单独归类 | 启动输出中的 `ModuleNotFoundError: No module named 'X'` → `dependency_missing` 并点名 `X`（`python-manager.ts` 的 `missingModule()`；文案与重试倒计时由启动契约用例锁定） |
| 可选工具（Playwright）首次使用 | `tests/test_browser_dependency_message.py`：**4 项全部通过**——缺模块时报"缺 Python 模块 playwright（不是缺浏览器）"并指向依赖修复；模块在而引擎缺时仍给 `/install-browser` 引导 |
| 打包依赖检查复用 | `desktop/build/check-skill-dependencies.py`（构建期：技能声明 vs `requirements-desktop.txt` vs 解释器窗口）未改动，继续复用 |

### 2.5 跨语言版本窗口一致性

`tests/test_desktop_python_window.py`（`python -m unittest`，本机 3.12.13）：**3 项全部通过**——`python-probe.ts` 的 `MIN_PYTHON`(3,8) / `MAX_PYTHON_EXCLUSIVE`(3,14)、`setup-windows.ps1` 的 `$minMinor=8` / `$maxMinorExclusive=14` 与 `agent/skills/dependencies.py` 完全一致。

## 3. 未验证 / 不计入完成

- **任务 2.2 的"验证聊天正常"未执行**：需要真实 `config.json` 与真实身份库，本次刻意只用独立临时数据目录启动后端（无模型密钥），因此"通过 UI 正常聊天"这一条**不勾选**，留待阶段 7 的真实应用冒烟或用户在本机确认。
- **任务 2.5 的安装版现场**（随包后端被移除 → 仅提示修复安装）未在真实安装包上复跑：代码路径与文案为既有实现 + 契约用例覆盖，真实验收归阶段 7/14。
- `.venv-win` 未真正创建（准备脚本只验证了 `-Check` 路径）：创建与安装依赖需要网络安装 `requirements-desktop.txt`，归本机开发准备，不影响已交付行为。
