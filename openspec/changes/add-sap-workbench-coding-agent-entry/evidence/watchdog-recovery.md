# 证据：标准服务看门狗与启动顺序修复

日期：2026-10-05。宿主：`rd.rsmxm.com.cn`（Windows，8GB）。

## 修复前现场

打开工作台时四个端口全部 DOWN：

```
port 4096 = DOWN   (OpenCode API)
port 3100 = DOWN   (OpenCode web UI)
port 8110 = DOWN   (sap-abap MCP)
port 8200 = DOWN   (sap-pyrfc MCP)
```

原因定位：

1. `RDAI-AppWatchdog` 只守护主控制台的 9900，**没有任何任务守护这四个端口**；
2. `RDAI-Autostart` 只在开机运行一次 `start-opencode-web.ps1`；该脚本 2026-10-05 18:35 的运行**卡在 MCP 网关阶段**超过 3.5 分钟，脚本自身已打印完最后状态行（`sap-pyrfc : DOWN :8200`），但调用方的 `& powershell.exe ... | ForEach-Object` 管道因后代进程持有写句柄而一直未返回，导致其后的 API 与 UI 启动步骤根本没有执行；
3. 服务当天 10:05 尚在（PID 4844），之后死亡并一直无人拉起。

## 改动

- `scripts/start-opencode-web.ps1`
  - 启动顺序改为 **API → UI → MCP**；MCP 是可选依赖，不得阻塞必需项；
  - MCP 启动改用 `Start-Process` 文件重定向 + `WaitForExit(60000)`，替换会挂住的管道写法；
  - 新增 `-SkipMcp`（看门狗使用）与 bootstrap 启动锁 `logs/opencode-start.lock`（陈旧 5 分钟后可接管），使脚本可被定时安全调用。
- `scripts/watchdog-opencode.ps1`（新）：每 20 秒探测 4096/3100（必需）与 8110/8200（可选）；健康时静默，异常退出后拉起；尊重启动锁，不与被拉起者竞争。
- `scripts/register-opencode-tasks.ps1`（新）：注册 `RDAI-OpenCodeWatchdog`（SYSTEM，3 个错开的 1 分钟触发器，与 `RDAI-DocReaderWatchdog` 同形）。

## 验证

### 1) 健康态静默

四个端口均 UP 时运行看门狗：退出码 0，未创建/未追加 `logs/watchdog-opencode.log`。

### 2) 崩溃恢复（实测）

杀死 4096 与 3100 的进程树后运行看门狗：

```
19:07:44  required service down (opencode-api(4096):4096) -> starting: start-opencode-web.ps1 -SkipMcp
19:07:44  === start-opencode-web begin (restart=False skipmcp=True) ===
19:07:45  starting opencode API server on 4096 (auth=on) ...
19:07:45  opencode API launched (PID 7340)
19:07:45  starting vite web UI on 3100 ...
19:07:45  vite web UI launched (PID 9420)
19:07:45  MCP gateways skipped (-SkipMcp)
19:07:54  API  :4096 = OK (PID 7340)
19:07:54  WEB  :3100 = OK (PID 12916)
19:07:54  === start-opencode-web end ===
```

恢复用时约 **10 秒**（暖缓存下的 API 冷启动；冷启动实测约 16.9 秒）。结束后锁文件已释放，四端口均 UP。

### 3) 启动锁与并发礼让

恢复过程中计划任务 `RDAI-OpenCodeWatchdog` 并发触发了一次，其日志为：

```
19:07:49  down: required=[4096] optional=[] but a launcher holds the lock -> waiting
```

即第二次调用正确礼让，未产生端口冲突的第二个实例。

## 未覆盖

- 任务 1.1（打开路径的就绪探测）与第 2 组（对话切换到平台编码入口）尚未实现；本证据仅覆盖存活与就绪的**部署侧**。
- MCP 网关自身的恢复只在端口探测层面验证，未验证 SAP 连接可用性。

## 补充（2026-10-05 23:40）：存活判据由 CONNECT 改为 HTTP 应答

上面「本证据仅覆盖存活」一段的前提已经不成立 —— 存活判据本身被发现是错的。

**故障**：为让共享服务加载新插件，执行 `start-opencode-web.ps1 -Restart`；随后 `/code-api/` 全程挂起。
根因是 `taskkill /f /t` 杀掉的 API 进程把监听套接字泄漏在 `127.0.0.1:4096` 上（`netstat` 显示
`LISTENING 7340`，而 `Get-Process 7340` 是 `HasExited=True`、`Threads=0` 的僵尸对象；
自行绑定 `127.0.0.1:4096` 失败，`0.0.0.0:4096` 与 `[::1]:4096` 成功）。泄漏的套接字**照旧完成
三次握手**（实测 19ms 成功），而 `packages/opencode/src/server/server.ts:117-121` 在端口 `0`
时"先试 4096，失败则退到任意空闲端口"，于是新 API 静默绑到 51097，nginx 固定代理的 4096 无人应答。

**为什么守护没有报告**：`Test-PortListening` 只问"端口在不在监听"。守护因此在整个故障期间
保持沉默（`watchdog-opencode.log` 停在 22:27:51），启动脚本也判"API already listening → skip"。

**修复与实测**：两个脚本改用 `Test-ServiceAnswering`（loopback HTTP GET，**任何**状态码都算应答，
`AllowAutoRedirect=$false`、`Proxy=$null`、5s 超时 —— 3s 会被 `sap-pyrfc` 空闲后 ~2.9s 的首个请求抖成误报）。
实测探针标定：API `401` / UI `302` / `sap-abap` `404` / `sap-pyrfc` `406` 全部判活，泄漏的 4096 判死。
修复后守护于 23:36:07 立即报出 `required service down (opencode-api(4096):4096)` 与
`launcher exited; api=False web=True`；启动脚本打印
`API :4096 is LISTENING BUT NOT ANSWERING (socket leaked by a dead process; restarting this machine is what clears it) -> not starting`。

**这类泄漏无法在运行中清除**（无进程可杀、无句柄可关），只能重启机器。因此启动脚本不再声称
"已恢复"，而是写明处置方式。

## 补充（2026-10-05 23:55）：同类判据全量清扫，探针定稿为原始套接字读

上一段结尾提到的"同类未修"已全部修完。清扫后，**同一类缺陷共 9 处**（8.6 的 2 处 + 本段 7 处）：

| 文件 | 端口 | 原判据 |
| --- | --- | --- |
| `start-opencode-web.ps1` | 4096 / 3100 | connect（8.6 已改） |
| `watchdog-opencode.ps1` | 4096/3100/8110/8200 | connect（8.6 已改） |
| `start-services.ps1` | 9900 / 9898 闸门 + 7 项启动后验证 | connect |
| `start-weknora.ps1` | 8100 | connect |
| `start-docreader.ps1` | 50051 | connect |
| `watchdog-app.ps1` | 9900 | connect |
| `watchdog-oneagent.ps1` | 9898 | connect |
| `watchdog-weknora.ps1` | 8100 | connect |
| `watchdog-docreader.ps1` | 50051 | connect |

**看门狗那 4 处后果最重**：健康判据是 `if (Test-AppListening) { exit 0 }`，即"能连上就什么都不做"。
泄漏套接字让这个条件永远成立，看门狗便**永远跳过它存在意义所在的那次拉起** —— 服务死了却没人报，
正是 4096 这次故障的形态，只是换了个端口。

### 探针定稿

连接 → 发一个 HTTP/1.1 请求 → **要求对端有反应**：读到字节（应答），或读到 0（干净关闭）。
泄漏套接字两者皆无，读超时。

**为什么不是 `HttpWebRequest`（8.6 的初版实现）**：docreader 是纯 gRPC（h2c）。实测它对 HTTP/1.1
请求回的是 **HTTP/2 帧**（46 字节，~1ms），`HttpWebRequest` 解析不了、只报一个笼统的 `UnknownError`
—— 用它会把**活着的** docreader 判成死。改成读原始字节后，同一个函数同时覆盖 HTTP 与 gRPC。

全端口实测（同一探针，`Get-NetTCPConnection` 对照 `OwningProcess` 见第 4 列）：

| 端口 | 结果 | 耗时 | 说明 |
| --- | --- | --- | --- |
| 80 | `HTTP/1.1 301` | 42ms | nginx |
| 443 | `HTTP/1.1 400` | 1ms | 明文 HTTP 打 TLS 口，nginx 仍以状态行应答 |
| 3100 | `HTTP/1.1 302` | 1ms | Vite |
| 8100 | `HTTP/1.1 200` | 1ms | weknora |
| 8110 / 8200 | `HTTP/1.1 404` | 13ms / 4ms | MCP 网关 |
| 9898 | `HTTP/1.1 303` | 9ms | OneAgent 控制台 |
| 9899 | `HTTP/1.1 303` | 2ms | nginx |
| 9900 | `HTTP/1.1 303` | 2ms | RSM 控制台 |
| 50051 | 46 字节二进制（HTTP/2 帧） | 1ms | docreader（gRPC） |
| **4096** | **零字节，超时** | **2522ms** | **泄漏套接字** |

即：只有 4096 这一个"没有反应"；**状态码不是必需的信号，有无反应才是**。

### 实测

1. **发布版函数，非临时副本**：从 `watchdog-app.ps1` 解析出 `Test-AppListening` 后调用 ——
   9900 / 9898 / 8100 / 50051 全部 `True`，**4096 `False`（2522ms）**。这正是旧 connect 判据永远为
   `True` 的那个端口。
2. **无假阳性**：四个看门狗对活服务各跑一遍，退出码 0、耗时各 ~300ms，且
   `watchdog.log`(44301B/500 行)、`watchdog-oneagent.log`(1878B/23 行)、`weknora-watchdog.log`(1149B/16 行)、
   `docreader-watchdog.log`(223B/3 行) **逐字节不变** —— 静默健康路径成立。
3. **启动链**：`start-opencode-web.ps1 -SkipMcp` 打印
   `API :4096 is LISTENING BUT NOT ANSWERING (PID 7340; restarting this machine is what clears it) -> not starting`
   与 `WEB :3100 = OK (PID 424)`。不再把 API 静默启动到随机端口 —— 那会把"服务死了"变成"服务莫名其妙"。
4. **守护链**：`watchdog-opencode.ps1` 打印
   `required service down (opencode-api(4096):4096) -> starting: start-opencode-web.ps1 -SkipMcp`
   → `launcher exited; api=False web=True`。故障期间不再沉默。

### 有意保留的差异

`watchdog-opencode.ps1` 的同名函数**保留 `HttpWebRequest` 实现**，不并入上述原始套接字版本：
它的 5s 超时编码了"`sap-pyrfc` 网关空闲后首个请求实测 ~2.9s"这一余量（该文件所守护的四个端口全是
HTTP），且它本来就是按应答判定（8.6 已验证其 1 分钟内报出 `api=False`）。语义与其余各处一致
（"有没有应答"），只是客户端库不同。

### 语法

8 个改动脚本经 `[Parser]::ParseFile` 检查，全部无错误。

### 仍需重启

`127.0.0.1:4096` 上的泄漏套接字**在运行中无法清除**，必须重启机器。看门狗现已在每次尝试前
如实报出该状态（不再假装恢复）。重启后应核对 4096/3100/8110/8200/9899/9900/9898/8100/50051 各端口
均"应答"而非仅"在监听"。

## 重启结果（2026-10-06 00:03:14 重启，00:06 起全部就绪）

**泄漏套接字已清除。** 重启前基线（`logs/probe-baseline.txt`）与重启后实测对比：

| 端口 | 重启前 | 重启后 | 服务 |
| --- | --- | --- | --- |
| 80 / 443 / 9899 | LISTEN=T ANSWER=T | LISTEN=T ANSWER=T | nginx |
| 3100 | T / T | T / T | Vite web UI |
| **4096** | **LISTEN=T / ANSWER=F, OWNER=7340（僵尸）** | **T / T, OWNER=5176** | OpenCode API |
| 50051 | T / T | T / T | docreader (gRPC) |
| 8100 | T / T | T / T | weknora |
| 8110 / 8200 | T / T | T / T | SAP MCP 网关 |
| 9898 | T / T | T / T | OneAgent 控制台 |
| 9900 | T / T | T / T | RSM 控制台 |

**十一项全部 `LISTEN=True` 且 `ANSWER=True`。** 对外路径恢复：

```
/code-api/config -> HTTP 401 in 0.051885s     (故障期间：超时挂起)
/code/           -> HTTP 200 in 0.078025s
```

看门狗日志在 00:06:17 写下 `launcher exited; api=True web=True` 之后**再无任何记录** ——
静默健康路径成立，故障期间"每分钟如实报告"的行为已回到正常。

## 补充（2026-10-06 00:15）：重启暴露的两个连带缺陷，修复为 8.8

重启把 8.6/8.7 探针的两个盲区照了出来。两者都不是判据"错了"，而是判据"不够"。

**① 固定 8s 的验证窗口太短（会造出重复进程）**

```
00:04:40  === start-opencode-web begin (skipmcp=False) ===
00:04:44  opencode API launched (PID 6736)         <- 正确启动
00:05:07  API  :4096 = DOWN                        <- 仅 23s，冷启动还没答完
00:05:24  required service down (opencode-api(4096):4096) -> starting   <- 看门狗相信了这个判决
00:05:28  opencode API launched (PID 7336)         <- 第二个 API
00:05:48  opencode API launched (PID 5176)         <- 第三个
```

冷启动 API 实测需 **~25s**，而验证是固定 `Start-Sleep -Seconds 8`。看门狗把 `DOWN` 当事实、再起一个；
第二个 API 绑不上 4096（地被占），于是走 `server.ts` 的端口回退，**落到随机端口 50046**（实测），
机器上短暂跑了两个 API 服务，nginx 只代理到正确的那个（5176）纯属运气。这正是 8.6 花整节
讨论的"随机端口"危害，只是这次由**重复启动**而非**泄漏套接字**触发。

修为一处**有界共享就绪等待**（`Wait-ServicesAnswering`，`$ReadyTimeoutSeconds` 默认 90s）：
健康时一有应答立即返回（实测 **1.5s**），只在慢启动时才等，永不猜测。就绪判定不再需要"猜多久"。

**② 应答探针无法区分"正在启动"与"已泄漏"**

两者都是"在监听但不应答"，但正确的处置相反：等待 vs 如实报告泄漏、需重启机器。仅凭应答判定时，
启动器会对**正在启动**的服务误报"泄漏套接字、需重启机器"，并因此拒绝启动 —— 现场确实存在这种状态
（新起的 API 先绑套接字、后开始应答）。

新增 `Get-ListenerOwnerState`，用**套接字属主**区分：

| 状态 | 含义 | 处置 |
| --- | --- | --- |
| `alive` | 活进程持有 | **启动中** —— 等待，绝不重复启动 |
| `orphan` | 无主（属主进程已终止/僵尸对象） | **泄漏套接字** —— 如实报告，需重启机器 |
| `none` | 无人绑定 | 正常启动 |

三个判据各司其职：`Test-ServiceAnswering` 判"能不能用"、`Get-ListenerOwnerState` 判"为什么不能用"、
`Wait-ServicesAnswering` 决定"等多久"。缺任一个都会产生错误动作：只有应答判定 → 误判启动中为泄漏；
只有 listener 判定 → 就是 8.6 的原始故障；只有固定等待 → 重复启动。

**实测**：健康路径 `API already answering on 4096 -> skip` + `web UI already answering on 3100 -> skip`
→ `API :4096 = OK (PID 5176)` / `WEB :3100 = OK (PID 3188)`，**1.5s** 返回、无重复进程。
已终止那个退到随机端口的重复 API（PID 7336 @ 50046），4096 仍由 5176 持有（清理前后均核对）。
`Parser::ParseFile` 通过。
