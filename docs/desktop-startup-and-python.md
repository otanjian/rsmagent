# 桌面启动：解释器选择、依赖缺项与恢复

适用范围：`desktop/`（Electron 桌面宿主）本机模式。来源：`harden-windows-desktop-reliability` 任务 2.1–2.5；现场报告 `docs/design/windows-desktop-verification-2026-10-13.md` §4.2、§4.6。

## 1. 解释器怎么选（只按三条来源，按顺序）

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | `COW_PYTHON_PATH` | 环境变量，最高优先；显式指定即指令 |
| 2 | `config.json` 的 `python_path` | 仅开发模式读取（安装包不使用系统 Python） |
| 3 | 项目 venv | Windows：`.venv-win/Scripts/python.exe` → `.venv/Scripts/python.exe` → `venv/Scripts/python.exe`；macOS/Linux：`.venv-win/bin/python` → `.venv/bin/python` → `venv/bin/python` |
| 4 | PATH | Windows `python`；其他平台 `python3` |

**候选必须"回答"才算数**：桌面会真的启动它并读取 `sys.version_info` 与 `sys.executable`（无 shell、异步、每个候选 3 秒上限、整轮 15 秒上限、超时进程会被结束、输出有上限）。因此：

- 解释器不存在 → `python_not_found`；
- 存在但不可用（应用商店占位别名、版本不在窗口内、回答无法解析、探测超时）→ `python_invalid`；
- 支持窗口为 **3.8 – 3.13**，与 `agent/skills/dependencies.py` 的 `MIN_PYTHON` / `MAX_PYTHON_EXCLUSIVE` 一致，并有跨语言测试防止漂移。

**显式指定失败时不会回退**：如果 `COW_PYTHON_PATH` 或 `python_path` 指向的解释器不可用，桌面直接报错，不会"悄悄换一个"——否则会出现"在开发机上能跑、换台机器就不行"。

**刻意不做**：不搜索 conda / micromamba / uv 的所有环境，不查 py Launcher，不扫描全盘。使用这些工具的用户用上面 1 或 2 显式指路即可。

## 2. 失败后用户看到什么、该做什么

| 错误码 | 含义 | 恢复动作 |
|---|---|---|
| `python_not_found` | 所有候选都不可用 | 安装 Python 3.8–3.13 并准备环境（Windows：`desktop\build\setup-windows.ps1`），或用 `COW_PYTHON_PATH` 指定 |
| `python_invalid` | 找到了但不是能用的解释器（含应用商店占位） | 安装真实 Python，或把完整路径写入 `COW_PYTHON_PATH` / `config.json` 的 `python_path` |
| `dependency_missing` | 后端启动后因缺少 Python 包停止，报错里会点名模块 | 开发模式按模块名安装（或 `pip install -r desktop/build/requirements-desktop.txt`）；安装版重新安装客户端 |
| `backend_removed` / `backend_missing` / `backend_blocked` | 随包后端丢失/被拦截 | 见启动页提示：杀毒信任区恢复或重装 |

以上四项都属于"等待不会变好"的失败，因此**不走 40 秒重试倒计时**，直接显示原因与动作。

## 3. 可选工具缺依赖（含 Playwright）

浏览器工具在**首次使用时**检查运行依赖（复用既有 `agent/tools/browser/browser_env.py` 的能力探测，不新建依赖清单、不扫描全部工具）：

- 缺 **playwright 模块** → 明确说"缺少 Python 模块 playwright（不是缺少浏览器）"，并指向上面的依赖修复路径；
- 模块在、但**没有可用浏览器引擎** → 保留原有引导（对话内 `/install-browser`，已有 Chrome 时自动跳过）。

## 4. 远程模式不需要 Python

`remote` 模式**不探测、不启动任何本机 Python**：桌面启动分支在 remote 下根本不进入本机后端（`desktop/src/main/index.ts`），解释器探测只属于本机后端启动路径。因此"这台机器没有可用的 Python"不影响远程聊天、流式回复与工作台；设备通道故障与服务器业务故障分别提示。

安装版只使用随包后端：构建期由 `desktop/build/check-skill-dependencies.py`（技能声明 vs `requirements-desktop.txt` vs 解释器窗口）拦住缺依赖的出包，运行期若后端缺失只提示"修复安装"，不会去搜索系统 Python 或创建开发环境。

## 5. Windows 快速开始

```powershell
# 1) 准备环境（创建/复用 .venv-win，安装 desktop/build/requirements-desktop.txt）
powershell -ExecutionPolicy Bypass -File desktop\build\setup-windows.ps1
powershell -ExecutionPolicy Bypass -File desktop\build\setup-windows.ps1 -Check   # 只看计划，不创建任何东西
powershell -ExecutionPolicy Bypass -File desktop\build\setup-windows.ps1 -Python C:\Python312\python.exe

# 2) 启动开发模式（清除 ELECTRON_RUN_AS_NODE，把 .venv-win 放到 PATH 最前）
powershell -ExecutionPolicy Bypass -File desktop\build\dev-windows.ps1
```

准备脚本不触碰 macOS 的 `.venv`、`config.json`、`identity.db`、`run.log` 与记忆目录，也不删除任何文件；`-Check` 只做解释器校验并打印计划。

## 6. 相关代码与测试

| 位置 | 作用 |
|---|---|
| `desktop/src/main/python-probe.ts` | 候选顺序、探测、窗口判定、整轮预算（纯 Node，可单测） |
| `desktop/src/main/python-manager.ts` | 启动前选解释器；`dependency_missing` 归类；失败码与诊断摘要 |
| `desktop/src/renderer/src/components/StatusScreen.tsx`、`i18n.ts` | 三类新失败的中英文案与恢复动作 |
| `desktop/build/setup-windows.ps1`、`dev-windows.ps1` | Windows 环境准备与开发启动 |
| `tests/test_desktop_python_probe.cjs` | 18 项：顺序、平台 venv、Store 别名、版本窗口、超时清理、显式不回退、整轮预算 |
| `tests/test_desktop_startup_contract.cjs` | 7 项：失败码/文案/双语一致性、重试倒计时例外、Windows 脚本约束、remote 不探针 |
| `tests/test_desktop_python_window.py` | 版本窗口与 `agent/skills/dependencies.py`、PowerShell 脚本三处一致 |
| `tests/test_browser_dependency_message.py` | 缺 playwright 模块与缺浏览器引擎的文案区分 |
| `tests/test_desktop_backend_port_guard.cjs` | 6 项：真实套接字证明"bind 不等于空闲"、监听 PID/镜像可查、只清理属于自己的监听者 |

## 7. 排查：第二个后端进程 / 端口被占用

**现象**：`run.log` 里出现

```
[ERROR][...][runtime.py:168] - [WebChannel] 端口 9899 已被占用，可执行 `cow restart` 清理残留进程……
[ERROR][...][app.py:331] - [ChannelManager] Channel 'web' startup error: No socket could be created -- (('0.0.0.0', 9899): [WinError 10048]……)
```

而稍后又看到 `[WebChannel] Slow history list` 这类**正常服务**的日志——说明当时有两个后端：一个在服务，另一个刚被启动并立刻失败。

**先分清两种来源**（阶段 5.1 查到底的结论）：

1. **本实例内部的重复 spawn：不存在。** `PythonBackend.start()` 开头有 `status === 'ready' || 'starting'` 守卫；恢复重启走 `restart()`，由 `recovering` 单飞。因此日志里若出现两次启动，那是**两个不同的进程**（应用重启、另一个应用实例，或命令行自己起的），不是管理器重入。
2. **与"用户自己在命令行起的后端"撞端口**（这台机器上的实际情况）：命令行跑着 `python app.py`（用配置里的 `web_port`，此处 9899），桌面端随后启动自己的后端，`pickPort()` 从 `config.json` 读到同一个 9899。日志里**失败的那个实例带 `[desktop] plugin ... disabled (not needed in desktop client)`**，据此可判断"这一行是桌面端起的"；正在服务的那个没有这一行。

**为什么旧代码没能避开（已修）**：`isPortFree()` 只做了一次 `bind 127.0.0.1:9899`。Windows 上普通 winsock bind 默认带 `SO_REUSEADDR`，所以**别人监听 `0.0.0.0:9899` 时这个 bind 仍然成功**，探测据此回答"空闲"→ `pickPort()` 把 9899 交给后端 → 后端（cheroot，不共用地址）绑定失败 `WinError 10048`。同一句谎话还让 `freePort()` 的清理**从未执行**——这正是手动启动的那个后端能存活下来的唯一原因。现场复现（不需要启动应用）：

```powershell
node scripts/repro/probe-port-free-windows.cjs
# 占用 0.0.0.0:9899 时：旧探测说 free；真实连接说 occupied；修复后的 isPortFree 说 busy
```

**现在的行为**（`desktop/src/main/backend-port.ts`）：

- 端口空闲 = 能 bind **且** 连不上（连接被应答即视为被占用）；
- 清理只针对**能证明属于自己的**监听者：本实例 spawn 的子进程 PID，或随包分发的 `cowagent-backend.exe`；`python.exe` 这类一律**不动**，改为尝试下一个候选端口（19876/29876/39876/49876/55876，全不可用则请操作系统分配）。

**排查命令**：

```powershell
# 谁在监听（默认端口 9876；配置里的 web_port 可能是 9899）
Get-NetTCPConnection -LocalPort 9899 -State Listen | Select-Object OwningProcess
netstat -ano -p tcp | Select-String ':9899\s'
# 看这个 PID 是什么进程
Get-Process -Id <PID> | Select-Object ProcessName, Path
# 端口探测诊断
node scripts/repro/probe-port-free-windows.cjs
```

**不要**用 `taskkill /IM python.exe` 这类批量清理：会杀掉与本次问题无关的进程（包括正在服务的那个）。

