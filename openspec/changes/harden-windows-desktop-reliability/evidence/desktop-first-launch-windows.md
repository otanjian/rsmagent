# 真机运行记录：Windows 首次启动桌面应用（任务 2.4）

日期：2026-10-14 00:05（本机 Windows x64，交互式会话 4，非管理员）。

任务原文：*补Windows准备及开发启动脚本，创建/复用.venv-win或显式环境并清除ELECTRON_RUN_AS_NODE；按文档启动验证原环境、配置、身份和记忆不被覆盖。*

## 1. 执行的命令（真实运行，非模拟）

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build\setup-windows.ps1 -Python D:\tools\micromamba\envs\py312\python.exe
powershell -ExecutionPolicy Bypass -File desktop\build\dev-windows.ps1
```

## 2. 结果

| 判据 | 实测 |
|---|---|
| 创建/复用 `.venv-win` | **创建成功**：14,690 个文件 / **439 MB**；解释器 `D:\AIPJ\rsmagent\.venv-win\Scripts\python.exe`；`pip install -r desktop/build/requirements-desktop.txt` **全部装成**（playwright 1.52.0、dashscope、google-generativeai、aiohttp 3.14.5、openpyxl… 共 97 个包）；自检 `import openpyxl,dashscope,aiohttp → ok` |
| 清除 `ELECTRON_RUN_AS_NODE` | `dev-windows.ps1` 在启动 Electron 的那个进程里清除，并在仍存在时**直接失败**；实测应用以 Electron 正常起来（有窗口），证明未被当作 Node 运行 |
| 按文档启动 | Electron 起来：主进程 pid **23584**，窗口标题 **`RongAI`**；共 5 个 Electron 进程 |
| 本机后端就绪 | 进程链 `electron 23584 → .venv-win python 2524 → python 25328`，后端日志 **`[LOG] [backend] ready on port 9899`**；`GET http://127.0.0.1:9899/` → **200**（320,167 B 控制台页面）；9899 取自本机 `config.json` 的 `web_port` |
| 日志 | 仓库根新增 `desktop-main.log`（宿主日志）并在增长；`run.log` 由 393,058 B 增至 400,603 B |

## 3. 原环境/配置/身份/记忆**未被覆盖**（逐项前后对照）

| 对象 | 之前 | 之后 |
|---|---|---|
| `config.json` | 2,371 B，mtime 2026-10-09 13:45:32 | **完全相同**（大小与 mtime 均未变） |
| `identity.db`（身份库） | 61,456,384 B，mtime 2026-10-13 23:26:22 | **完全相同** |
| `.cow.pid` | 5 B，mtime 2026-10-09 09:12:42 | **完全相同** |
| macOS 的 `.venv` | 13,943 个文件 | **13,943 个文件**（未被读写作废） |
| `~/.cow/tenant-roots`（记忆目录） | 10 个文件 | 10 个文件，**无一晚于启动时刻**（最新为 2026-10-08 11:05） |
| `run.log` / `desktop-main.log` | 393,058 B / 不存在 | 400,603 B / 新建 —— 这是日志本身，按设计写入 |

## 4. 观察到的两点（都不是本次启动的故障）

1. **渠道凭据解密失败（既有数据条件）**：启动时 `[Identity] channel credential 'cred_…' decrypt failed` ×2 → `[ChannelInstances] tenant instance 'chan_…' has unusable credentials, skipping`。这是身份库中存有**用当前密钥无法解密的旧渠道凭据**（属于既有数据），应用**按设计跳过**该渠道实例并继续启动，控制台与后端均正常。
2. **`.venv-win` 的解释器是"重定向器"**：`.venv-win\Scripts\python.exe`（pid 2524）会再拉起基解释器 `D:\tools\micromamba\envs\py312\python.exe`（pid 25328），后者才是监听 9899 的进程。这与"第二次 spawn"缺陷（任务 5.1）**无关**——那是父进程重复启动后端；这里是 venv 启动器→基解释器的正常委派，且依赖解析仍走 venv（自检通过）。

## 5. 判定

**2.4 勾选**：两个脚本真实执行、`.venv-win` 真实创建、`ELECTRON_RUN_AS_NODE` 被清除、应用按文档启动且后端就绪，并且**原环境、配置、身份与记忆逐项证明未被覆盖**（上表）。

**未验证/未做**：真实安装包上的同流程（属 6.4/7.1）；`npm run dist:win` 尚未运行。
