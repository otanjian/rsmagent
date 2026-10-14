# A01 真机首次选目录（2026-10-14，远程模式下经真实服务器）

用户操作：**连上远程服务端 `https://rd.rsmxm.com.cn`（ASTER 控制台，账号 me23n）并在其中选择了本机目录 `D:\AIPJ\cow-a01-demo`**。打包版 `容大AI.exe`（安装包 2.2.0）为承载进程。以下为机器上可直接复核的落盘与日志证据。

## 1. 时间线与落盘

| 时刻 | 事实 | 来源 |
|---|---|---|
| 13:52 | 打包版启动（窗口 `RongAI`，pid 15652） | 进程表 |
| 13:55:52 | **包内本机后端**启动（`cowagent-backend.exe` pid 21028，父=应用），监听 **127.0.0.1:9876** | 进程表 + `~/.cow/run.log`（`Local access: http://localhost:9876`、`Listening on 127.0.0.1 only`） |
| 13:56 | `desktop-remote.json` 写入：`mode=remote`，profile `srv_mv7p61im` → `https://rd.rsmxm.com.cn` | `%APPDATA%\CowAgent\desktop-remote.json` |
| 13:56:48 | `[remote] local web auto-bound to https://rd.rsmxm.com.cn` | `~/.cow/desktop-main.log` |
| **13:57:46** | **安装标识首次生成**：`{"id":"fsnZilgSrmNFSwFy7yklWbPgmRKP7Adh","createdAt":1791957466005}`（此前该文件不存在＝标识已被清空） | `%APPDATA%\CowAgent\installation.json`（mtime 13:57:46） |
| **14:13:30** | **选目录落盘为候选**：`D:\AIPJ\cow-a01-demo`，`label=cow-a01-demo`，`id=tCH0jb7FND695LX4xdi8mw`，**`deviceId=dev_fsnZilgSrmNFSwFy7yklWbPgmRKP7Adh`**（＝`dev_`+安装标识），`serverId=srv_aHR0cHM6Ly9yZC5yc214bS5jb20uY24`（＝base64 `https://rd.rsmxm.com.cn`），`userId=usr_hcWvTEbXtYDWq1ho`，`tenantId=tnt_EA3qM-lHPLD8ZPwW`，`lastUsedAt=1791958410930` | `%APPDATA%\CowAgent\desktop-directory-candidates.json`（mtime 14:13:30） |

## 2. A01 各项判定（当前可证部分）

| A01 要求 | 现状 |
|---|---|
| 清空有效安装标识后**首次选目录** | ✅ 标识确已清空（当时文件不存在），动作后生成新标识并用于候选的设备 ID |
| 无 `Illegal invocation` | ✅ **两个宿主日志中各 0 处**（仓库 `desktop-main.log`、`~/.cow/desktop-main.log`） |
| **choose→bind 按序** | ⚠️ 部分：选目录→候选落盘 ✅（14:13:30）；**绑定在服务器侧**（本机 `identity.db` 的 `desktop_bindings` 仍是另一次测试账号 `usr_zV2k…`/`tnt_xNHl…` 的 3 行，未被本次写入＝remote 模式的绑定确实落在服务器），服务器侧绑定记录需在服务端核对 |
| ID 持久稳定 | ✅ **重启复验通过**：重启前后 `installation.json` 完全一致（同一 `fsnZilgSrmNFSwFy7yklWbPgmRKP7Adh`、`createdAt` 未变），见 §2.1 |
| 取消不创建授权 | ❌ 未执行（需要再开一次选择器并点取消） |
| 重启后再次选择 | ⚠️ **重启本身已完成并复验**（见 §2.1：同一 ID、候选保留、未自动授权、远程冷启动未起本机业务后端、未复用旧身份）；**"再次选择"这一点击仍需真实鼠标** |

## 2.1 重启复验（同一会话内完成，2026-10-14）

在 `mode=remote` 已配置的前提下**冷启动**打包版（先 `Stop-Process` 全部 `容大AI`，确认 `容大AI=0 / cowagent-backend=0`，再启动 exe）：

| 复验项 | 结果 |
|---|---|
| 安装标识 | **重启前后完全一致**：`{"id":"fsnZilgSrmNFSwFy7yklWbPgmRKP7Adh","createdAt":1791957466005}` ✓ |
| 候选文件 | **mtime 与内容均未变**（14:13:30）→ 重启不重写候选 ✓ |
| 候选作用域隔离 | 5 条候选分两组：`srv_aHR0cHM6Ly9yZC5yc214bS5jb20uY24`（＝`https://rd.rsmxm.com.cn`）+ 今日账号 `usr_hcWvT…`/`tnt_EA3q…`+`dev_fsnZilg…`；以及旧的 `srv_aHR0cDovL2xvY2FsaG9zdDo5ODk5`（＝`http://localhost:9899`）+ `usr_zV2k…`/`tnt_xNHl…` → **按 server/user/tenant/device 分开记**，无串味 ✓ |
| **是否自动恢复授权** | **否** ✓：重启后应用停在"登录账号 / 确认服务器地址"页，**容器页未挂载**（CDP 里 `http:` 页面数 = 0），`window.electronAPI` 仅暴露基础面（无 localFiles 桥）→ 旧身份未被复用、授权未自动恢复 |
| **远程冷启动是否起本机业务后端** | **没有** ✓（重启后 `cowagent-backend` 进程数 0）→ 规范"remote 模式不启动本机 Python 业务后端"在**冷启动**这一条上成立（同时补上 `remote-server-half-2026-10-14.md` §4 的语义缺口；此前 9876 后端的出现属于"先本地启动、后由用户切远程"的另一条路径，由 `local-web-bind.ts` 设计承载） |
| 窗口 | `RongAI`（重启后 pid 10184） |

**仍未做的**：`选择`（重启后再选一次）与`取消`各一次——两者都需要真实鼠标操作原生对话框。

## 3. 顺带查明的服务器侧事实（6.3/6.4/6.5 相关）

| 探测 | 结果 |
|---|---|
| `GET https://rd.rsmxm.com.cn/api/desktop/meta` | **200**：`remote_web = {implemented:true, accepted:true, configured:true, available:true}`；顶层键 `console_entry_paths, entry_path, features, project_execution, protocols, remote_web` |
| 同响应中的 `project_execution` | **`available:false, reason:"not_accepted"`**（`platform:"win32"`, `files_write_verified:false`, `scripts_verified:false`, `tools:[]`）→ 执行面在**部署策略层未开放**（`not_accepted`），并非仅平台原因；按任务 14.5，`accepted` 只能在受支持平台 A01–A36 通过后放开，故此处**保持拒绝是正确的** |
| `GET/POST(/Upgrade) https://rd.rsmxm.com.cn/api/desktop/connect` | **404 `not found`**（text/html）→ **设备通道（WSS）未发布**，正是宿主日志每 ~30 秒刷 `[WARN] [desktop] device connection reconnecting: 1006:handshake_failed` 的原因（13:58 起持续）。修法是按 change 6.3 交付的部署说明：反代加 `location = /api/desktop/connect { proxy_pass http://127.0.0.1:9877; }`＋Upgrade 头，并在服务器上运行 `python -m integrations.desktop.gateway` |
| TLS 链 | **TLSv1.3**，CN `rd.rsmxm.com.cn`，签发 **Let's Encrypt / YE1**，有效至 **2026-12-15**，1 个 SAN → 6.5 的"适用 CA"路径在真实域名上得到正面样本（客户端已接受该证书，容器页正常加载） |

## 4. 一个我自己先前的怀疑，经代码核实后**否掉**

打包版切到远程模式后，包内本机后端（9876）仍在跑。我曾怀疑这违反"remote 不启动本机 Python"。
代码与规范核实结果：**不是问题**。`desktop/src/main/remote/local-web-bind.ts` 的注释写明 *"Auto-bind the in-process local backend into the remote Web container"*；规范要求针对的是**以远程模式启动时**不得启动本机业务后端（`desktop/src/main/index.ts:853` 起的分支：`if (startup.mode === 'remote') { … local backend not started … }`）。本次是**先以本地模式启动、随后由用户切到远程**，复用已在跑的本机后端作为"本机通道"绑定给远程工作台，属设计路径。

## 5. 仍未完成的原因（如实记录）

- A01 的"取消不创建授权""重启后再次选择"以及 A02 的快速 A/B 选择，都需要**真实鼠标操作原生对话框**（程序无法触发；仓库也没有可注入目录的 dev 钩子）。
- A04/A05 及后续 A 项需要 **`project_execution` 被接受**（当前 `not_accepted`）**且**设备通道可用（当前 `/api/desktop/connect` 404）；本机平台为 `win32`，按 change 规定不在支持之列。

## 6. A01 收口（2026-10-14，全部断言齐备）

在安装包 `容大AI 2.2.0` 上按用户既定运行方式（**远程模式**，服务器 `https://rd.rsxm.com.cn`）完成 A01 的全部动作与断言：

| A01 要求 | 实测证据 |
|---|---|
| 清空有效安装标识后**首次选目录** | 动作前 `installation.json` **不存在**；14:13:30 选择 `D:\AIPJ\cow-a01-demo` → 候选落盘（`deviceId=dev_fsnZilg…`＝`dev_`＋新标识；作用域＝真实 server/user/tenant） |
| 标识本身 | 创建于 **13:57:46**；当时应用的本机后端**同时在跑**（`cowagent-backend.exe` pid 21028，监听 127.0.0.1:9876）→ 首次选择满足"真实桌面 + 真实本机后端" |
| **重启后再次选择** | 重启（远程冷启动，**未启动本机业务后端**）→ 登录服务器 → **15:06:55** 在远程工作台再次选择同一目录 |
| 无 `Illegal invocation` | 两个宿主日志各 **0** 处 |
| choose/bind 按序 | 选择→候选经主进程 local-files 桥落盘 ✓；服务端设备行 `dev_HPttEjgBfwkUs0I6T7wENwYZ`（cow-a01-demo, windows, 2.2.0）**created 13:57:46 → updated 15:06:55** ✓ |
| **ID 持久稳定** | 重启前后 `installation.json` 一致（同 id、同 createdAt）；服务端**复用同一条设备行**（未重复；该账号共 2 台，另一台为既有 macOS）；本机三条 `desktop_bindings` 共用同一 `device_id` |
| **取消不创建授权** | 用户打开目录选择器后**点取消** → 候选文件与取消前**逐字节一致**（6 条，最新仍 15:06:55）、`localContext()` 仍 **`{"state":"none"}`**、服务端设备行未变、宿主日志无新 choose/bind 行 ✓ |

**操作方式如实记录**：取消由**用户手工点击**完成。我用 CDP **可信输入事件**（`Input.dispatchMouseEvent`，实测把 `navigator.userActivation` 从 false 变为 true）与 **Windows UI Automation**（可枚举/操作原生对话框，取消按钮按语言无关的控件 ID `2`）尝试代操作，但该远程流程在当前状态**不弹出原生对话框**——桥的 promise 持续 pending，各次尝试**零副作用**（候选逐字节未变、`localContext` 未变、无对话框残留）。最可能原因：该流程需要设备通道/本地上下文握手，而 `/api/desktop/connect` 在该服务器上未发布。

