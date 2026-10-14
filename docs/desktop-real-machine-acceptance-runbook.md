# 真机验收执行手册（harden-windows-desktop-reliability）

本手册把本 change 中**本机无法完成**的任务逐项映射到可直接执行的步骤与证据采集要求。任务勾选仍以 `openspec/changes/harden-windows-desktop-reliability/tasks.md` 为准；已完成的本机证据见同目录 `evidence/INDEX.md`。

## 0. 前置条件

| 项 | 要求 |
|---|---|
| 机器 | Windows x64（普通用户，125% DPI 用于 7.1），可选第二台作为"设备 A/B" |
| 构建 | Node 20+、`desktop/node_modules` 已安装（`npm ci`）；`electron-builder` 用于打包 |
| Python | 3.8–3.13（本机执行面用）；**远程-only 场景刻意不装 Python**（6.4 要证明这一点） |
| 服务器 | 一台可部署 Web + 网关的 Linux/Windows 主机；有效证书链（公网或企业 CA）；按 `docs/desktop-remote-deployment.md` 配置代理 |
| 账号 | 两个租户、每租户两名成员（6.5 用）；一名平台管理员 |
| 符号链接 | 对 3.4 的 symlink 切片：开启开发者模式或以管理员运行（否则 `os.symlink` 抛 `WinError 1314`） |
| 巡检 | 一个带远程调试端口的浏览器（`msedge --remote-debugging-port=9222`）用于 `scripts/inspect-console-cdp.py` |

**先做一次非破坏性预检**（不写任何文件，会打印将要创建的环境）：

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build\setup-windows.ps1 -Check -Python <解释器路径>
```

## 1. 任务 2.4：Windows 准备与开发启动（剩余：真建环境 + 启动）

```powershell
# 1) 创建/复用 .venv-win 并安装 desktop\build\requirements-desktop.txt
powershell -ExecutionPolicy Bypass -File desktop\build\setup-windows.ps1 -Python <解释器路径>
# 2) 启动 Electron（脚本会清除 ELECTRON_RUN_AS_NODE，并把 .venv-win 仅对本进程前置 PATH）
powershell -ExecutionPolicy Bypass -File desktop\build\dev-windows.ps1
```

采集：`config.json`、`identity.db`、记忆目录在**前后**的存在性/大小/mtime 快照（应完全一致）；`desktop-main.log` 与 `run.log` 均生成；后端就绪端口与 PID。
判据：窗口正常出现、后端 ready、两个日志文件都有内容、原环境未被覆盖。

## 2. 任务 3.4：归属与防替换切片（符号链接部分）

开启开发者模式/提权后：

```powershell
python -m unittest tests.test_junction_refusal -v          # junction 切片（已通过，可复跑）
python -m pytest tests/test_safe_fs.py tests/test_memory_console_scope.py `
                 tests/test_personal_memory_scope_protocol.py tests/test_memory_index_rel_path.py -q
```

判据：上面四个套件 **0 失败**（当前失败项全部是 `WinError 1314` 造成的）；重点看中间目录替换、操作期间替换、跨用户/租户越界是否都被拒绝。

## 3. 任务 3.5：记忆保存/清空/索引/账号切换（真机 + 安装包）

1. 用真实账号在页面执行：保存 → 清空 → 重试索引 → 切换账号再切回；
2. 观察：清空后**不得复活**旧内容（含过期批次提交）、切换账号后**不得**看到他人记忆、索引恢复后正文一致；
3. 远程场景：本机**不装 Python**，确认记忆读写只发生在服务器侧且只针对当前登录用户；
4. 复跑 `tests/test_account_memory_management.py`、`test_personal_memory_console.py`、`test_memory_path_marks_dirty.py`、`test_personal_memory_tool_execution.py`、`test_memory_storage_tenant_scope.py`。

## 4. 任务 6.4：真实安装包 × HTTPS 服务器

```powershell
cd desktop
npm run dist:win            # 产物：desktop\release\容大AI-Setup-2.2.0-x64.exe（productName-version-arch，NSIS、非 oneClick、perMachine=false）
```

1. 在**无 Python** 的 Windows x64 上安装；
2. 填 HTTPS origin → 完成浏览器确认与 Web 配对；
3. 依次验证：探测（`GET /api/desktop/meta` → 200 且 `Cache-Control: no-store`）、聊天、流式回复、**合成附件上传 + 回读**；
4. 记录：安装包版本、`git rev-parse HEAD`、代理配置校验和、客户端与服务器 PID；`Get-Process python*` 前后对比证明**没有本机 Python**被启动。

## 5. 任务 6.5：统一验证矩阵（逐项检查 探测 / 页面 / WSS）

| 场景 | 操作 | 判据 |
|---|---|---|
| 断网恢复 | 断网 30–60s 后恢复 | 有界退避重连；身份重验；**不重发未确认写** |
| 网关重启 | 只重启网关进程 | 设备通道恢复；Web 服务状态不被当成网关就绪 |
| Web 重启 | 只重启 Web 进程 | 页面/聊天恢复；已提交数据与上传文件保留 |
| 撤销 | 撤销设备绑定、再撤销会话 | 设备通道拒绝；会话失效按原生授权恢复 |
| 两租户两成员 | 4 个账号两两交叉 | 互不可见；切换租户后旧响应不落地 |
| 模式/来源切换 | 本机↔远程、换 origin | 旧身份/连接/迟到响应失效；**不自动重放旧写请求** |
| 证书拒绝 | 用不受信证书 | 显示证书原因并停止；**不降级 HTTP**、不引导重登循环 |
| 企业 CA / VPN | 经 VPN 用企业 CA 域名 | 分别验证探测/页面/WSS 的信任与可达；不以"浏览器能开"作为全部通过 |
| 授权根 | 选择本机目录后检查服务器库与日志 | **不出现本机绝对路径**（只应有标识） |

## 6. 任务 7.1：发布冒烟与升级回退（125% DPI，普通用户）

1. 安装后逐项：启动、托盘、单实例（再启一次 → 仍只有一个主进程）、开机自启（`--hidden`）、通知、主题、登录、更新检查；
2. 固定版本升级：装 v2 覆盖 v1 → 验证配置/身份/文件保留；再回退装 v1 → 同样验证；
3. 记录每次的安装包版本、代理配置校验和与 `tasks.md` 勾选变化。

## 7. 任务 11.2–14.6：A01–A36 索引

以 `acceptance-local-project-execution.md` 为权威矩阵（tasks 与矩阵均 36/36 覆盖）。各任务已在本机完成的**非真机**部分与对应证据：

| 任务 | A 项 | 本机已有证据 | 真机还需 |
|---|---|---|---|
| 11.2 / 11.3 | A01 / A02 | `readonly-input-vs-project-mode.md`（语义）、`selection-without-execution.md`（choose→bind/取消/迟到） | 真实桌面 + 真实本机后端上的首次/重启选择、快速 A/B |
| 11.4 | A03 | `local-refusal-mapping.md`、`switch-and-version-compat.md` | 从普通 Web 提交绝对路径、伪造 handle、复用他人选择记录 |
| 11.5、12.1–12.5、13.1–13.3、14.1–14.4、14.6 | A04–A33 | `capability-declaration-and-gates.md`（Windows 无工具、门禁）、`dependency-registration-and-baseline.md`（隔离证据不足只阻止执行面） | 真实模型链路、真实设备、脚本隔离、幂等/撤权/取消、安装包 |
| 14.5 | 门禁 | — | 受支持平台 A01–A36 全通过后才可放开 `accepted` |

## 8. 证据采集与回填

建议目录：`openspec/changes/harden-windows-desktop-reliability/evidence/real-machine/<YYYY-MM-DD>/`

一次采集到位（只写该目录、从不删除、拒绝盘根/仓库根/家目录，均已实测）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\collect-real-machine-evidence.ps1 `
    -Out openspec\changes\harden-windows-desktop-reliability\evidence\real-machine\<日期> `
    -Origin https://<正式域名> [-Cdp http://127.0.0.1:9222]
```

```text
versions.txt        # 安装包版本、git rev-parse HEAD、代理配置校验和、客户端/服务器 PID
probe.txt           # curl -sS -D - https://<origin>/api/desktop/meta
inspection.json     # node scripts/inspect-console-cdp.py --endpoint http://127.0.0.1:9222 --out <dir>
screenshots/*.png
logs/               # desktop-main.log、run.log、网关日志
processes.txt       # Get-Process python*, Get-NetTCPConnection -State Listen
```

- 巡检脚本会**自带期限与失败记录**：超时记 `inspection_limitation`，**不得**当作产品缺陷；页面文本不符预期记 `needs_confirmation`。
- 回填：把结论写进对应 `tasks.md` 的复选框，并在 `acceptance-local-project-execution.md` 的 A 项旁标注证据目录；**未执行不得勾选**。

## 9. 常见坑（都已在本次会话踩过）

1. `ELECTRON_RUN_AS_NODE` 从 Electron 宿主终端继承 → 应用静默退出；用 `dev-windows.ps1` 或先 `Remove-Item Env:\ELECTRON_RUN_AS_NODE`。
2. PATH 上的 `python` 是 Microsoft Store 占位别名 → 用 `-Python <路径>` 或 `COW_PYTHON_PATH`。
3. `core.autocrlf` 必须保持 **false**：本仓库按 LF 存储，设为 true 会把 CRLF 写进工作区并让若干测试失败（见 `windows-unsupported-and-generated-consistency.md` 第 3 节）。
4. 端口探测：Windows 上 bind 成功≠端口空闲（`SO_REUSEADDR`），且**不得**杀非自己的进程（见 `second-spawn-investigation.md`）。
5. 无 `npx`（只有 `.ps1`）的会话里，测试的"产物过期即编译"会 `ENOENT`；先 `desktop\node_modules\.bin\tsc.cmd -p tsconfig.main.json` 让产物比源码新。
6. 符号链接权限：`EPERM`/`WinError 1314` 是权限而非逻辑问题。
7. 面板"控制台/工作台"是**同窗口**切换，不再有命名窗口；若看到旧窗口请关闭（见 `docs/console-navigation-and-windows.md`）。
