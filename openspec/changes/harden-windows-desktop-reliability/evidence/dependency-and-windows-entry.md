# 阶段 2 证据：依赖诊断（2.2 完成）与 Windows 入口脚本（2.4 部分完成）

日期：2026-10-13。

## 1. 任务 2.2（**勾选**）

> 复用构建检查与启动 stderr，核心缺依赖停止就绪，可选工具首次使用前给出模块/运行时缺项（含 Playwright）；验证聊天正常，不建立全量扫描或依赖注册表。

| 要求 | 实现 / 证据 |
|---|---|
| 复用构建检查 | 真实执行 `python desktop/build/check-skill-dependencies.py` → `checking 4 skill(s) against desktop/build/requirements-desktop.txt` → **`ok: every declared skill dependency is shipped by this bundle`**，退出码 0 |
| 启动 stderr → 核心缺依赖阻止就绪 | 新增纯模块 `desktop/src/main/dependency-error.ts`（`missingModuleFrom(lines)`），`python-manager.ts` 的 `missingModule()` 改为委托它；分类出的 `dependency_missing` 属于渲染层 `UNRECOVERABLE_CODES`，**不会到达 ready，也不提供无用的"重试"** |
| 可选工具首次使用前给出缺项（含 Playwright） | `agent/tools/browser/browser_tool.py` 的引擎/依赖检查在 **`execute()` 内**（首次使用时）；`tests/test_browser_dependency_message.py` **4 passed**：文案点名 `playwright` 与 "module"，指向 `desktop/build/requirements-desktop.txt`，且不再引导浏览器 onboarding |
| 验证聊天正常 | 该检查不在模块作用域（导入工具不需要 playwright），因此启动与聊天不受其影响；新测试断言这一点 |
| 不建立全量扫描或依赖注册表 | 新测试断言启动路径（`python-manager.ts`、`python-probe.ts`）中不存在 `pip list` / `importlib.metadata` / `pkg_resources` / `-m pip` 之类扫描 |

新增 `tests/test_desktop_dependency_error.cjs`（**6/6**）：分类行为（`ModuleNotFoundError`/`ImportError` 两种形态、**只报第一个**（后续是级联）、普通崩溃不臆造模块名）；分类被经理层复用；`dependency_missing` 不可恢复；浏览器检查在首次使用；无全量扫描。
回归：`test_desktop_startup_contract.cjs` **7/0**、`tsc -p tsconfig.main.json --noEmit` **exit 0**。

**为什么把分类抽出来**：`missingModule()` 原本是私有方法，无法在没有 Electron 运行时的情况下针对真实 stderr 文本验证；抽成 `dependency-error.ts`（与 `backend-port.ts` 同一先例）后，分类可以被真实驱动，且没有被复制成第二处实现。

## 2. 任务 2.4（**未勾选**，剩一步）

> 补 Windows 准备及开发启动脚本，创建/复用 .venv-win 或显式环境并清除 ELECTRON_RUN_AS_NODE；按文档启动验证原环境、配置、身份和记忆不被覆盖。

已完成：

- 脚本 `desktop/build/setup-windows.ps1`、`desktop/build/dev-windows.ps1` 已交付，并在 `.gitignore` 里按仓库约定加了 `!` 例外（否则会被 `desktop/build/*` 规则静默丢弃）；`test_desktop_startup_contract.cjs` **7/0** 断言其约束（探测解释器而非信任、不覆盖可用环境、清除 `ELECTRON_RUN_AS_NODE` 并在仍存在时硬失败、`.venv-win` 仅对本进程前置 PATH）。
- **真实执行**（两个文档入口）：
  - `setup-windows.ps1 -Check` → 正确报出本机 PATH 的 `python`/`python3` 都是 **Microsoft Store 占位别名**并给出三种修法，退出码 1（具名失败）；
  - `setup-windows.ps1 -Check -Python D:\tools\micromamba\envs\py312\python.exe` → 校验出 `3.12.13`、打印将要创建的环境与依赖文件，并明说 **"nothing was created or installed"**，退出码 0（这就是任务里的"显式环境"路径）；
  - `dev-windows.ps1` → 在守卫处说明"没有 `.venv-win`"，随后执行文档中的 `npm run build`（我的输出截断曾把它中断，已重跑至完成：**exit 0，30s**，随后编译产物测试 6/6、34/0 全绿）。
- **原环境/配置/身份/记忆未被覆盖**：在两个入口执行前后对 `.venv-win`、`.venv`、`run.log`、`config.json`、`~/.cow\identity.db`、`~/.cow\config.json`、`~/.cow\run.log` 做（存在性 + 大小 + mtime）快照比对，**完全一致**。

**未做（因此不勾选）**：真正创建 `.venv-win`（会 pip 安装 `desktop/build/requirements-desktop.txt`）与启动 Electron 窗口。剩余步骤一条命令即可：`desktop\build\setup-windows.ps1 -Python <解释器路径>`，然后在有桌面会话时运行 `desktop\build\dev-windows.ps1`。按"未执行不得登记为通过"，本项保持未勾选。
