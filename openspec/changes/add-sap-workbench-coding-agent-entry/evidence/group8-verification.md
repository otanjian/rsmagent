# 证据：第 8 组（验证与交付）

日期：2026-10-05。本页把三件事分开写，避免把“测试通过”当成“现场验收”：
8.1 新增测试清单、8.2 既有测试核对、8.3 回归结果（已做）；
8.4/8.5 现场实测（交互段由操作员在真实浏览器执行，本代理只做服务端核对）。

## 8.1 新增测试（按验收点索引）

| 验收点 | 位置 |
|---|---|
| 经编码入口创建与打开会话 | `tests/test_coding_session_routes.py`；`tests/test_sap_workbench_frontend.cjs`（`openCodingSession` 预留、`mountPlatformCode` 挂载 `iframe_url`）；`tests/test_sap_workbench_native_embed.py`（投影只给 `coding_session_id`/`agent_id`，不给地址与令牌） |
| 历史同步与 attach | `tests/test_coding_session_sync.py`；`tests/test_sap_workbench_frontend.cjs`（`resuming a history entry reopens its own coding session instead of creating one`、`a conversation started inside the embed is registered once, for this mount only`） |
| 按会话标识解析归属 | `tests/test_sap_workbench_plugin_bridge.py`（27 项，含凭据、直连、歧义命中拒绝） |
| 未归属拒绝 | 同上（`session_not_bound` 403 / `session_not_running` 409 / 未归属与伪造一律拒绝） |
| 工具被收窄 | `tests/test_coding_session_routes.py`（命名档案下发会话级 `allow`、未知档案拒绝、无档案即无 `allow`）；`tests/test_sap_workbench_project_toolkit.py`（13 项，项目默认 `deny`、幂等、不覆盖用户选择、JSONC 拒绝）；`tests/test_sap_workbench_project_plugin.cjs`（7 项，含 `execute asks the permission layer`） |
| 回滚开关（第 7 组） | `tests/test_sap_workbench_legacy_gate.py`（14 项） |
| 就绪等待单飞（1.4） | `tests/test_sap_workbench_frontend.cjs::two retry clicks that overlap mount one frame and arm one readiness wait`（去掉 `codeMount` 判断即复现第二个 iframe，已实测确认断言非空转） |

## 8.2 既有测试核对

- 删除项核对：全仓 `grep '同账号|same-account|starts an engine'` 只剩描述性文案（`tests/test_sap_workbench.py`
  的 docstring 与注释），没有把“新建必须关闭同账号旧绑定”“创建即启动引擎”当作断言的用例残留。
- 现行替换语义由 `test_sap_workbench_native_embed.py::test_new_session_closes_all_owner_bindings_before_capacity_check`
  锁定：替换范围是 **owner 级**（同租户同用户），其他租户/用户的活动会话不受影响。
- 引擎字段：`test_native_projection_names_the_coding_session_and_withholds_every_grant` 断言投影
  不含 `iframe_url` / `bootstrap_token` / `token` / `port` / `base_url` / `origin`。
- 第 5 组改动 `project/plugins/rsm-sap-workbench-navigation.js` 后，`native-host-guidance.test.ts`
  里钉住的插件摘要失配；已把新摘要加入 `native-host.ts:upgradeableGuidance`（保留旧摘要，已完成升级的
  部署仍能升级）并更新测试字面量。原失败已修复。

## 8.3 回归结果（2026-10-05，Windows）

| 范围 | 命令 | 结果 |
|---|---|---|
| Python（SAP 主题） | `python -m pytest tests/ -k sap_workbench`（排除两个仅 POSIX 可收集的部署用例） | **1314 passed / 4 skipped / 10 failed**，10019.90s |
| Node 前端契约 | `node --test tests/test_sap_workbench_frontend.cjs tests/test_sap_workbench_field_validation.cjs tests/test_sap_workbench_dom.cjs tests/test_sap_workbench_observations.cjs tests/test_sap_workbench_project_plugin.cjs` | **157 passed** |
| Bun 适配器 | `bun test Scene/sap_workbench/opencode_adapter` | **10 passed / 7 skipped / 1 failed** |

10 项 Python 失败**均在本次改动前已复现**，与本变更无关：

- `test_sap_workbench.py` 2 项：本机 screen gateway 起不来（`screen gateway startup failed`）。
- `test_sap_workbench_shutdown.py` 8 项：用例在 Windows 上对管道调用 `select.select`（`WinError 10038`）。

Bun 的 1 项失败是 `credentials.test.ts` 断言文件权限 `0600`，Windows 不保留 POSIX 权限位；实现与
测试文件本次都未改动。7 项 skip 是既有的暂停/条件用例（真实上游、双 host 部署等）。

另核对一次旧任务里出现的 `test_the_scene_catalog_warms_the_engine_and_still_allocates_no_session`：
该名字在当前测试文件中**已不存在**（现名 `test_the_scene_catalog_allocates_no_session_and_starts_no_engine`），
那次运行读的是重命名前的文件。对现行代码重跑：catalog 用例通过，回滚开关 14 项全通过。

## 8.4 / 8.5 现场实测

交互段落（页面登录、SAP 登录、目视确认 iframe 与对话）属交互式凭据，由操作员在真实浏览器执行；
本代理不读取、不重置、不绕过平台或 SAP 登录，也不复制 cookie/token，只做**服务端状态核对**。

### 入口地址：用 `127.0.0.1` 而不是 `localhost`

现场出现一次 `ERR_CONNECTION_REFUSED`（截图为 `localhost:9899`）。核对后确认服务当时在运行：

| 项 | 值 |
|---|---|
| `9899` 监听 | `0.0.0.0:9899`（nginx，**仅 IPv4**） |
| `9900` 监听 | `127.0.0.1:9900`（控制台 `app.py`） |
| `localhost` 解析 | 先 `::1`（IPv6），后 `127.0.0.1` |
| `GET http://127.0.0.1:9899/chat`、`/`、`:9900/` | 均 **200** |

`localhost` 先解析到 `::1`，而入口只监听 IPv4，浏览器在回退前即可能报连接被拒。
**现场走查使用 `http://127.0.0.1:9899/chat`。**（入口暴露面的既有行为，不是本变更引入。）

### 重载后可达性

| 观测 | 值 |
|---|---|
| 端口 `4096 / 3100 / 8110 / 8200 / 9899 / 9900` | 全部可连接 |
| `GET http://127.0.0.1:9899/scene-assets/sap_workbench/frontend/workbench.js` | 200，且包含 `permission_profile`、`page_readwrite_unavailable`、`mountLegacyCode`、`codeMount`（重载后的前端改动已在服务上生效） |
| `GET /api/scenes/sap-workbench/config`（匿名） | 400（拒绝，未登录） |

### 现场实测记录（2026-10-05 22:07）

操作员登录后打开工作台卡片，报告「测试 ok」。服务端核对如下。

**新会话已在重载后建立（8.4 的可核对部分）**

| 字段 | 值 |
|---|---|
| `id` | `sap_062ecbe5ebec8c53c618c06870350817` |
| 主体 | `tnt_EA3qM-lHPLD8ZPwW` / `usr_9ZxVPz7M2FuOro1q` |
| `agent_id` | `sap-workbench-test15` |
| `display_mode` | **`iframe`** |
| `state` / `allocation_stage` | **`ready` / `ready`**（`allocation_error` 为空） |
| `coding_session_id` | `oc_489e4b238387a2b9b618ea0746a45383` |
| `remote_session_id` | `ses_rsm_489e4b238387a2b9b618ea0746a45383` |
| `created_at` / `updated_at` | **同为 22:07:29** |

`ses_rsm_*` 前缀是平台自身的会话标识，`oc_*` 是平台编码会话关联标识 —— 两者同时出现，
说明这条会话**是经平台编码智能体入口创建并打开的**，而不是自管引擎的产物。
`created_at == updated_at` 且 `allocation_stage` 直接落在 `ready`，说明
**从请求到就绪发生在同一秒内，没有引擎启动等待**（旧路径的 45s/70s 预算在此路径上不存在）。

**自管引擎路径未被走到**

| 观测 | 值 |
|---|---|
| `run.log` 22:07:29 的 SapWorkbench 行 | 只有 `screen gateway on 127.0.0.1:55455` 与 `public entry proxy on 127.0.0.1:9911`（来自 `browser_service/runner.py`，即**浏览器节点/导航通道**，不是 OpenCode 引擎） |
| `legacy self-managed engine path active` 警告 | **未出现** |
| 新绑定目录 `scenes/sap_workbench_runtime/sap_062ecbe5ebec8c53c6` | **不存在**（`iframe` 分支在 `runtime.py` 早于 `runtime_root.mkdir` 返回，不建目录、不写 `host.log`） |
| 新增 `bun.exe` 引擎进程 | **无**（现存两个 bun 进程是共享实例 `packages/opencode serve` 与 vite，早于本次会话） |
| 同账号旧会话 `sap_8656439cefeb743224237caff1ce2aec` | 于同一时刻 22:07:29 转 `closed`（owner 级替换，`display_mode` 仍为 `iframe`） |

**未能核对的部分（如实标注）**

场景动作账本 `cj-sap_workbench-actions` 中，**22:00 之后没有任何记录**（`count = 0`），
新会话 `sap_062ecbe…` 名下也是 `count = 0`。`run.log` 中同样没有该会话的导航/工具调用痕迹。

操作员确认：本轮实测只覆盖**打开工作台、对话嵌入、恢复与刷新**，**未**在对话中让助手调用 SAP 工具。
因此这不是异常，而是**该段本就没有执行** —— 此前并列的两种可能已由操作员确认收敛为这一种。

替代证据链也排查过并排除：平台侧 `~/cow/memory/long-term/index.db` 的 `opencode_session_links`
表存在但 **0 行**，不是本次工具调用的证据来源；`scenes/sap_workbench.sqlite3` 之外
没有其他记录 SAP 动作的库。**没有可选的替代证据**，因此 8.5 不勾选。

### 8.5 中本代理可直接实测的一段：未授权调用被拒（导航通道）

`POST /api/scenes/sap-workbench/bridge` 是左侧画面的导航通道，由服务凭据 + 仅限直连回环
两道校验保护（见 `http.py` 的 `_require_coding_service` 与 `SapWorkbenchBridgeHandler`）。
用伪造 session 标识、分别对直连端口与经 nginx 入口发起调用，实测结果：

| 目标 | 凭据 | 结果 |
|---|---|---|
| `http://127.0.0.1:9900/.../bridge`（直连控制台） | 无 | **401** |
| 同上 | 伪造 Basic | **401** |
| `http://127.0.0.1:9899/.../bridge`（经 nginx） | 无 | **401** |
| 同上 | 伪造 Basic | **401** |

四种组合全部 401，**没有任何一次落到 SAP 操作**。这实测了 8.5 里的「未授权会话被拒」一段。
（注意：经 nginx 的请求会因 `X-Forwarded-*` 头被拒，这是设计使然 —— 只有本机直连的编码服务可调用；
上表两组经 nginx 的 401 无法区分是凭据还是转发头导致，故只记录“被拒”这一事实。）

「导航送达」与「页面读写不可用」两段需要**已授权**的编码服务凭据与一个活动绑定，
属交互式凭据范围，本代理不读取、不复用该凭据，留待操作员在真实对话中执行。

### 8.5 真正的现场失败原因：项目里装的是旧版插件、skill 完全缺失（2026-10-05 23:20 定位）

操作员报告「打开事务码」不成功。先前把原因归到控制台崩溃，**那个归因是错的**。
实际原因是**交付产物根本没落到项目里**，而且失败方式是静默的。

**证据链**

| 证据 | 内容 |
|---|---|
| 项目内插件 | `C:\rdai\rsmCode\sapwork\.opencode\plugins\rsm-sap-workbench-navigation.js` 摘要 `0412f4ef…`，与**退役宿主**的 `Scene/sap_workbench/opencode_adapter/navigation-guidance.js` **字节级相同** |
| 两者的门控变量 | 旧版读 `RSM_SAP_WORKBENCH_NAVIGATION` + `RSM_SAP_NAVIGATION_HOST`；新版读 `RSM_SAP_WORKBENCH_BRIDGE_URL` |
| 谁设置这些变量 | `RSM_SAP_WORKBENCH_NAVIGATION=1` 只由**已退役**的每会话宿主设置（`opencode_adapter/native-host.ts:100`）；`RSM_SAP_WORKBENCH_BRIDGE_URL` 由 `scripts/start-opencode-web.ps1:81` 为**共享服务**设置 |
| 后果 | 共享服务里 `RSM_SAP_WORKBENCH_NAVIGATION` 恒为空 → 插件 `server()` 立即 `return {}` → **不注册 `sap_transaction_open`** |
| skill | `.opencode/skills/sap-workbench/` 三个文件**全部不存在**（安装器报告 3 项 `installed`） |

**根因不是"忘了安装"，而是安装的触发点不够**：`project_toolkit.install()` 只在
**保存场景配置**时被调用（`backend/http.py:_install_project_toolkit` 仅出现在配置保存路径）。
本项目的配置早在新插件写出来之前就存好了，此后没有人再保存过配置 ——
于是**任何"已配置"的部署都停留在旧修订上，永远不会被迁移**。这正是本次交付在真机上
"看起来已启用、实际没有工具"的形态。

**已修复**

1. 重新安装：skill 三项 `installed`、插件 `upgraded`、`conflicts` 为空；项目内插件摘要现为
   `beb9eae8…`，与发布源一致。
2. 实测装进项目的**那一份**：带 `RSM_SAP_WORKBENCH_BRIDGE_URL` → 注册 `["sap_transaction_open"]`；
   不带 → 注册 `[]`（保持惰性，不注册必然失败的工具）。
3. **会话打开时也执行一次幂等安装**（`http.py` 的 open 分支）：冲突或错误只记 warning、不致命，
   让交付自愈而不是烂在磁盘上。
4. 新增契约测试 `test_the_shipped_plugin_gates_on_the_variable_the_launcher_exports`：把
   "插件读的门控变量"与"启动脚本导出的变量"钉在一起，并禁止退役宿主的变量名残留在插件里。
   这类"两半必须一致"的契约没有任何单侧测试能覆盖，而它恰恰是本次静默失效的形态。

**仍未完成的一段**：真实对话里的「导航送达」与「页面读写不可用」。阻断点已不是本变更，
而是下面 8.6 的基础设施故障。

### 8.6 独立缺陷：泄漏的监听套接字让所有存活检查失效（2026-10-05 23:19 起发现）

为让共享服务加载新插件，按既定计划执行了 `start-opencode-web.ps1 -Restart`。此后
**`/code-api/` 全程挂起**，而且没有任何守护报告异常。

**现象与证据**

| 证据 | 内容 |
|---|---|
| `netstat -ano` | `127.0.0.1:4096 LISTENING 7340`，另有多条 `CLOSE_WAIT 7340` |
| `Get-Process -Id 7340` | `HasExited=True`、`Threads=0`、无 `Path`/`Name` —— **僵尸对象**；`taskkill` 报"找不到该进程" |
| 绑定测试 | 自行绑定 `127.0.0.1:4096` **失败**（"通常每个套接字地址只允许使用一次"）；而 `0.0.0.0:4096`、`[::1]:4096` 均成功 → 幽灵套接字真实占住该**特定地址** |
| API 行为 | `packages/opencode/src/server/server.ts:117-121`：端口为 `0` 时"先试 4096，失败则退到任意空闲端口" → 新起的 API **静默绑到 51097**，而 nginx 固定代理 4096 |
| 对外症状 | `https://rd.rsmxm.com.cn/code-api/config` **超时挂起**（不是快速失败） |
| 守护日志 | `watchdog-opencode.log` 自 **22:27:51** 起再无任何记录 —— 整个故障期间守护保持沉默 |

**根因有两层**

1. **泄漏本身**：`taskkill /f /t` 杀掉 API 进程后，监听套接字仍被一个已终止进程的对象引用，
   Windows 一直保留该地址。这类泄漏在 Windows 上只能靠重启清除（无进程可杀、无句柄可关）。
2. **更严重的一层 —— 存活判据本身是错的**：`watchdog-opencode.ps1` 的 `Test-PortListening`
   与 `start-opencode-web.ps1` 的 `Get-Listener` 都只问"端口在不在监听"。泄漏的套接字
   **照旧完成三次握手**（实测 `ConnectAsync` 19ms 即成功），于是守护判"健康"保持沉默、
   启动脚本判"API already listening → skip"。**两者都不会、也无法发现问题。**

**已修复（8.6）**

- 两个脚本改用 loopback HTTP 探针 `Test-ServiceAnswering`：**任何**状态码都算应答。实测
  API `401`、UI `302`、`sap-abap` `404`/`400`、`sap-pyrfc` `404`/`406` 全部正确判活，
  而泄漏的 4096 判死。
- 探针超时取 5s：`sap-pyrfc` 空闲后**首个**请求实测 ~2.9s，取 3s 会抖动成每分钟误重启。
- `AllowAutoRedirect=$false`（重定向也是应答）、`Proxy=$null`（绝不让整机代理插在中间）。
- 判"在监听但不应答"时写明确切原因与处置（需重启机器），**不再声称已恢复**。

**实机验证**

| 时间 | 谁 | 日志 |
|---|---|---|
| 23:36:07 | 守护（新探针） | `required service down (opencode-api(4096):4096) -> starting` |
| 23:36:21 | 守护 | `launcher exited; api=False web=True` |
| 23:36:39 | 启动脚本（新探针） | `API :4096 is LISTENING BUT NOT ANSWERING (socket leaked by a dead process; restarting this machine is what clears it) -> not starting` |
| 23:37:07 / 23:37:47 | 守护 | 继续如实报告，并按 bootstrap 锁重试 |

即：修复前守护对整个故障无感；修复后一分钟内就说出 `api=False` 与确切原因。

**同类判据已全量清扫（8.7）**：其余启动脚本与全部四个看门狗原本各自仍有 connect 判据，同类故障
会让它们同样失明；清扫中还牵出 `start-sap-mcp.ps1` 与一处纯 listener 上报。共触及 10 个脚本，
详见下节。

### 8.7 存活判据统一：探针定稿为原始套接字读（2026-10-05 23:55）

**清扫范围**：同一类缺陷共触及 **10 个脚本**（8.6 的 2 个 + 本节 8 个），端口覆盖
4096 / 3100 / 8110 / 8200 / 9900 / 9898 / 8100 / 50051。

| 文件 | 端口 | 原判据 |
| --- | --- | --- |
| `start-opencode-web.ps1` | 4096 / 3100 | connect（8.6） |
| `watchdog-opencode.ps1` | 4096/3100/8110/8200 | connect（8.6） |
| `start-services.ps1` | 9900 / 9898 闸门 + 7 项启动后验证 | connect |
| `start-weknora.ps1` | 8100 | connect |
| `start-docreader.ps1` | 50051 | connect |
| `watchdog-app.ps1` | 9900 | connect |
| `watchdog-oneagent.ps1` | 9898 | connect |
| `watchdog-weknora.ps1` | 8100 | connect |
| `watchdog-docreader.ps1` | 50051 | connect |
| `start-sap-mcp.ps1` | 8110 / 8200 闸门 + 末尾 UP/DOWN 上报 | connect |

另在 `start-opencode-web.ps1` 中修掉"MCP :$p = OK"两行纯 listener 上报（清扫中发现）。
`start-sap-mcp.ps1` 的危害是**叠加**的：泄漏套接字让它打印"already listening"并跳过启动，
而 `watchdog-opencode.ps1` 已按应答判定、会持续报告这两个端口 DOWN —— 于是**每次恢复尝试都恰好
跳过**，网关再也回不来。

**看门狗那 4 处后果最重**：健康判据就是 `if (Test-AppListening) { exit 0 }`（"能连上就什么都不做"），
泄漏套接字让该条件永远成立，看门狗便**永远跳过它存在意义所在的那次拉起**。这正是 4096 故障的形态，
只是换了个端口。

**有意保留的 listener 判据**（不是遗漏，是语义正确）：`start-sap-mcp.ps1` 的 `Stop-SapGateway`
等待循环问的是"套接字是否真的消失了"；各启动脚本在失败路径上用 `Test-PortListening` 取得
`OwningProcess` 以打印处置说明。这些是"在监听吗"的正当用法，健康的路径一律按应答判定。

**探针定稿**：连接 → 发一个 HTTP/1.1 请求 → 要求对端**有反应**（读到字节，或读到 0 即干净关闭）。
泄漏套接字两者皆无，读超时。

**8.6 的 `HttpWebRequest` 版本为什么不够**：docreader 是纯 gRPC（h2c）。实测它对 HTTP/1.1 请求
回的是 **HTTP/2 帧**（46 字节，~1ms），`HttpWebRequest` 解析不了、只报一个笼统的 `UnknownError`
—— 用它会把**活着的** docreader 判成死。改为读原始字节后，同一个函数同时覆盖 HTTP 与 gRPC。

**全端口标定**（同一探针）：

| 端口 | 结果 | 耗时 |
|---|---|---|
| 80 | `HTTP/1.1 301` | 42ms |
| 443 | `HTTP/1.1 400`（明文 HTTP 打 TLS 口，nginx 仍以状态行应答） | 1ms |
| 3100 | `HTTP/1.1 302` | 1ms |
| 8100 | `HTTP/1.1 200` | 1ms |
| 8110 / 8200 | `HTTP/1.1 404` | 13ms / 4ms |
| 9898 | `HTTP/1.1 303` | 9ms |
| 9899 | `HTTP/1.1 303` | 2ms |
| 9900 | `HTTP/1.1 303` | 2ms |
| 50051 | 46 字节二进制（HTTP/2 帧） | 1ms |
| **4096** | **零字节，读超时** | **2522ms** |

即：只有 4096 这一个"没有反应"；**状态码不是必需的信号，有无反应才是**。

**实机验证**

| 检查 | 结果 |
|---|---|
| 调用**发布版**函数（从 `watchdog-app.ps1` 解析出 `Test-AppListening`，非临时副本） | 9900 / 9898 / 8100 / **50051** 全部 `True`；**4096 `False`（2522ms）** |
| 四个看门狗对活服务各跑一遍 | 退出码 0、各 ~300ms；`watchdog.log`(44301B/500行)、`watchdog-oneagent.log`(1878B/23行)、`weknora-watchdog.log`(1149B/16行)、`docreader-watchdog.log`(223B/3行) **逐字节不变** → 静默健康路径成立、无假阳性 |
| `start-opencode-web.ps1 -SkipMcp` | `API :4096 is LISTENING BUT NOT ANSWERING (PID 7340; restarting this machine is what clears it) -> not starting` + `WEB :3100 = OK (PID 424)`（不再静默绑到随机端口） |
| `watchdog-opencode.ps1` | `required service down (opencode-api(4096):4096) -> starting` → `launcher exited; api=False web=True` |
| `start-sap-mcp.ps1`（健康路径，两网关在线） | `[sap-abap]  already answering on 8110` / `[sap-pyrfc] already answering on 8200` → `sap-abap : UP :8110`、`sap-pyrfc : UP :8200`，退出码 0（无假阳性） |
| 语法 | 10 个改动脚本 `[Parser]::ParseFile` 全部通过 |

**有意保留的差异**：`watchdog-opencode.ps1` 的同名函数保留 `HttpWebRequest` 实现 —— 它的 5s 超时
编码了"sap-pyrfc 网关空闲后首个请求实测 ~2.9s"这一余量（该文件守护的四个端口全是 HTTP），
且本来就是按应答判定。语义与其余各处一致（"有没有应答"），只是客户端库不同。

### 8.8 就绪等待与"启动中/已泄漏"判别（2026-10-06 00:15，重启后现场发现）

重启清除了泄漏套接字（见 `watchdog-recovery.md` 的「重启结果」），同时暴露了 8.6/8.7 探针的两个
盲区。二者都不是判据"错了"，而是判据"不够"。

**① 固定 8s 的验证窗口太短 → 造出重复进程**

```
00:04:44  opencode API launched (PID 6736)         <- 正确启动
00:05:07  API  :4096 = DOWN                        <- 仅 23s，冷启动还没答完
00:05:24  required service down (opencode-api(4096):4096) -> starting   <- 看门狗相信了这个判决
00:05:28  opencode API launched (PID 7336)         <- 第二个 API
00:05:48  opencode API launched (PID 5176)         <- 第三个
```

冷启动 API 实测需 **~25s**，而验证原为固定 `Start-Sleep -Seconds 8`。看门狗把 `DOWN` 当事实、
再起一个；第二个 API 绑不上 4096（地被占），于是走 `server.ts` 的端口回退**落到随机端口 50046**
（实测），机器上短暂跑了两个 API 服务，nginx 只代理到正确的那个（5176）纯属运气 ——
正是 8.6 讨论的"随机端口"危害，这次由**重复启动**而非**泄漏套接字**触发。

修为一处**有界共享就绪等待**（`Wait-ServicesAnswering`，`$ReadyTimeoutSeconds` 默认 90s，`param` 可调）：
健康时一有应答立即返回（实测 **1.5s**），只在慢启动时才等，且**一个共享截止时间**（不是每端口一份，
否则总预算会被"永不就绪的服务"乘起来，超出看门狗的 180s 上界）。

**② 应答探针无法区分"正在启动"与"已泄漏"**

两者都是"在监听但不应答"，但正确处置相反。仅凭应答判定时，启动器会对**正在启动**的服务误报
"泄漏套接字、需重启机器"，并因此拒绝启动 —— 现场确实存在该状态（新起的 API 先绑套接字、
后开始应答；8.6 的原始误判是相反的极端）。

新增 `Get-ListenerOwnerState`，用套接字属主判别：

| 状态 | 含义 | 处置 |
| --- | --- | --- |
| `alive` | 活进程持有 | **启动中** —— 等待，绝不重复启动 |
| `orphan` | 无主（属主已终止/僵尸对象 `HasExited=true` 或 `Threads=0`） | **泄漏套接字** —— 如实报告，需重启机器 |
| `none` | 无人绑定 | 正常启动 |

三个判据各司其职，缺任一都会产生错误动作：

- 只有 `Test-ServiceAnswering`（判"能不能用"）→ 误判启动中为泄漏，拒绝启动。
- 只有 listener 判定 → 就是 8.6 的原始故障（对泄漏套接字无感）。
- 只有固定等待 → 窗口短了重复启动、长了拖延故障报告。
- 加上 `Get-ListenerOwnerState`（判"为什么不能用"）与 `Wait-ServicesAnswering`（决定"等多久"）才闭合。

**实测**

| 检查 | 结果 |
|---|---|
| 健康路径 `start-opencode-web.ps1 -SkipMcp` | `API already answering on 4096 -> skip`、`web UI already answering on 3100 -> skip`、`API :4096 = OK (PID 5176)`、`WEB :3100 = OK (PID 3188)`，**1.5s** 返回、无重复进程 |
| 清理重复 API | 终止退到随机端口的 PID 7336@50046；4096 仍由 5176 持有（清理前后均核对） |
| 语法 | `Parser::ParseFile` 通过 |

### 阻断状态：已解除（2026-10-06 00:03:14 重启）

`127.0.0.1:4096` 上的幽灵套接字已在重启中清除，`/code-api/` 恢复（`401`/51ms）。十一个端口全部
`LISTEN=True` 且 `ANSWER=True`；`RDAI-Autostart`/`RDAI-WeKnora`/`RDAI-DocReader` 三个 Boot 任务
按既有顺序拉起全部服务。细节见 `watchdog-recovery.md` 的「重启结果」与本节 8.8。

重启时暴露出探针的两个盲区（固定 8s 等待造出重复 API、无法区分"正在启动"与"已泄漏"），
已一并修复并实机验证 —— 见 8.8。

**当前唯一未完成项**：8.5 的现场段（真实对话中的「导航送达」与「页面读写不可用」）：

1. `http://127.0.0.1:9899/chat` 打开 SAP 工作台卡片，确认对话 iframe 起来；
2. 在对话里让助手打开一个事务码（例如 `打开ME21N`），确认它调用 `sap_transaction_open`
   并只回报"已送达"；
3. 核对 `scenes/sap_workbench.sqlite3` 的 `cj-sap_workbench-actions` 出现 `transaction_open` 行；
4. 让助手尝试读左侧页面 / 提交单据，确认它拒绝，并核对无越权写入。

### 现场失败原因定位（2026-10-05 22:18–22:28）：控制台原生崩溃并重启

> **更正（23:20）**：本节结论**不完整**。控制台确实崩溃并重启了，但「打开事务码」不成功的
> 直接原因是**项目里装的是退役宿主那版插件、skill 完全缺失**（见上节 8.5「真正的现场失败原因」）。
> 本节保留为一次真实发生的、与本变更无关的独立事件记录。

操作员报告工具面一步「没有成功」。核对日志后确认：**不是工具链的问题，而是控制台在测试进行中崩溃并自动重启**。

**证据链**

| 证据 | 内容 |
|---|---|
| `app-lifecycle.log` | `Windows fatal exception: code 0x80010108`（`RPC_E_DISCONNECTED`）+ 一次 `access violation`，**无 Python 帧**（`<no Python frame>`），即原生崩溃 |
| 启动记录 | `22:18:57`、`22:19:01`、`22:27:36` 三次 `process started`（均由看门狗 `RDAI-AppWatchdog` 拉起，`argv=app.py` 相对路径） |
| `logs/watchdog.log` | `22:18:58 app is down (nothing listening on 9900...) -> starting`、`22:27:35` 同一句 |
| 会话后果 | 工作台会话 `sap_062ecbe…` 于 **22:28:49**（新进程起来后）转 `state=closed` / `allocation_stage=paused` |

**该崩溃与本次变更无关**

- `app-lifecycle.log` 全文共 **10 份同样的崩溃转储**，时间跨度为 **2026-09-21 起**（09-21、09-23、09-25、09-26、10-04、10-05），远早于本变更（`.openspec.yaml` 的 `created: 2026-10-05`）。
- 转储中**没有** `sap_workbench` / `browser_service` / `screen` 帧；崩溃线程 `Thread 0x00000000 <no Python frame>`，栈上出现的是 `cheroot`、`websocket`、`mcp_client`、`wecom_bot_channel` 等既有组件。
- 全仓 `rg 'comtypes|win32com|pythoncom|winsdk|uiautomation|win32gui|win32api|pywintypes'` 在 Python 代码中**无命中**（唯一命中是 `tests/test_excel_tool.py` 断言 Excel 工具**不用** `win32com`）。`RPC_E_DISCONNECTED` 来自某个第三方原生库，不在本变更改动范围内。

**当前状态（22:33）**：控制台已恢复，PID 3216 持有 `127.0.0.1:9900`，`GET /` → **200**；看门狗日志最后两条（22:27:49、22:28:21）均为「进程存活 -> 等待」，说明重启后已稳定。原会话 `sap_062ecbe…` 已 `closed`，需重新打开一张卡片。

另注：仍有一个 2026-10-04 22:28 启动的 `app.py`（PID 12088）残留未退出，它没有持有 9900，是重复实例；未擅自终止，仅记录。

### 附带发现并修复：启动脚本的 MCP 步骤仍可无限阻塞（2026-10-05 22:40）

核对一个挂起 3.9 小时的旧后台任务（「Start service and time cold start」）时，发现第 1 组
的 MCP 可选化修复**尚不完整**。

**现象**：该任务在 18:35:11 调用不带 `-SkipMcp` 的 `start-opencode-web.ps1`，脚本于 18:35:16
打印完网关状态后**再没有任何输出**，直到 22:27 控制台重启才被终止 —— 耗时 13960s，且
`opencode-web.log` 中该次运行**始终没有 `=== start-opencode-web end ===`**。

**根因**：`$mcp.WaitForExit($timeout)` 在输出被重定向时，除了等进程退出，还会等两个重定向流
到达 EOF。`start-sap-mcp.ps1` 拉起的 `sap-abap` / `sap-pyrfc` 是**继承这些句柄的游离子进程**，
即使启动器 powershell 早已退出，句柄仍被持有，于是这次调用越过自己的 60s 上界一路等了 3.9 小时。
（这正是脚本注释里记录的那类「残留句柄」问题，只是换到了 `WaitForExit` 上。）

**修复**：改为对进程句柄做有界轮询 —— `while (-not $mcp.HasExited -and (Get-Date) -lt $deadline)`。
`HasExited` 只回答「进程是否结束」，不关心谁持有它的 stdout。`ExitCode` 读取加保护（重定向下
可能未被缓存，不能因此把已完成的启动器变成一次异常）。

**复测**（同一路径，带 240s 硬上界）：

| 运行 | 结果 |
|---|---|
| `-SkipMcp` | 14.9s 返回，写出 `end` |
| 不带 `-SkipMcp`（即曾挂起 3.9h 的路径） | **14.3s / 30.5s / 18s 三次均完成**，写出 `end`，`8110`/`8200` 均为 OK |
| 启动锁 | 运行结束后已释放（`opencode-start.lock` 被移除） |

### 结论

| 任务 | 状态 | 依据 |
|---|---|---|
| 8.4 打开 / 对话嵌入 / 恢复 / 刷新 | **已核对**：机制段有服务端证据（经平台入口同秒就绪、`iframe`、无引擎启动）；页面段为操作员实测确认 | 上表 |
| 8.5 工具面 | **已定位并修复真正的阻断原因**：项目内是退役宿主的旧插件 + skill 缺失 → 工具根本没注册；已重装、改为打开时幂等自愈、并加跨产物契约测试。未授权调用被拒已实机验证（4/4 → 401，无 SAP 操作） | 8.5 节 |
| 8.5 现场段（导航送达 / 页面读写不可用） | **已完成并实机验证**：真根因是**会话级 allow 从未落到 OpenCode**（`POST /api/session` 接受 create 体的 `permission` 但不落库）→ 已改为 create 后补 `set_permission`（PATCH 是唯一落库路由）+ 打开已 `ready` 链接时自愈。修复后代码路径**自动**产生的会话 `ses_rsm_46864cb6…` 自带规则集；工具实测可调用（日志 `permission=sap_transaction_open pattern=ME21N`）、导航**送达**（`transaction_open / succeeded`）、页面读写被明确拒绝、全表 `action_kind` 只有 `transaction_open`；113 passed；9900 控制台已重载 | 8.9 节 |
| 8.6 服务存活判据 | **已修复并实机验证**：守护与启动脚本改为应答探针，泄漏套接字不再被判为健康 | 8.6 节 |
| 8.7 存活判据全量清扫 | **已修复并实机验证**：同类 10 个脚本全部统一为原始套接字应答探针（覆盖 HTTP 与 gRPC）；发布版函数对 4096 判死、对 50051 等判活；四个看门狗无假阳性 | 8.7 节 |
| 8.8 就绪等待与启动中/已泄漏判别 | **已修复并实机验证**：重启暴露的两个盲区 —— 固定 8s 等待导致重复 API 落到随机端口（实测 50046），探针无法区分"正在启动"与"已泄漏"；改为有界共享就绪等待 + `Get-ListenerOwnerState` 属主判别。健康路径 1.5s 返回、无重复进程 | 8.8 节 |
| 清除 4096 泄漏套接字 | **已完成（2026-10-06 00:03:14 重启）**：十一个端口全部 `LISTEN=True/ANSWER=True`；`/code-api/config` 经 nginx 401/51ms（故障期间为超时挂起） | 重启结果节 |
| 8.10 看门狗「永不重启」 | **已修复并实机验证**：`Test-AppStarting` 只凭 PID 文件、信任无上界 → 残留孤儿（`1168`/`6768`）让看门狗认定"仍在启动"而无限等待（实测 00:44:44→00:48:04）。改为要求进程年龄 < 启动窗口（180s/300s）；四脚本同修。中毒条件复现 → 22s 内恢复；健康路径四脚本全静默、日志 0 增量、无新进程 | 8.10 节 |

### 8.9 现场段最终根因：`POST /api/session` 静默丢弃 `permission`（2026-10-06 00:30）

**现场形态**（截图 `image-d1902af1…`）：用户在工作台对话里要求"打开ME21N事务代码"，助手回：
`sap_transaction_open` **当前对我不可用**（项目配置里它是 `deny`，且未作为可调用工具注入本会话），
并拒绝用 shell 直连桥接端点绕过（服务日志可见它探测了 `$env:RSM_SAP_WORKBENCH_BRIDGE_URL` 与
`Invoke-RestMethod … $env:RSM_SAP_WORKBENCH_BRIDGE_URL`）。

**链路核对**：

| 环节 | 实机值 |
|---|---|
| 项目级 deny | `C:\rdai\rsmCode\sapwork\.opencode\opencode.json` = `{"permission":{"sap_transaction_open":"deny"}}` |
| 插件是否注册工具 | **是** —— `plugins/rsm-sap-workbench-navigation.js:113` `sap_transaction_open: transactionOpenTool`（门控 `RSM_SAP_WORKBENCH_BRIDGE_URL`，服务进程已导出） |
| skill 产物 | 存在（`SKILL.md` + `references/boundaries.md`、`references/transactions.md`） |
| 会话 | `ses_rsm_c9238813…` / `oc_c9238813…` / 绑定 `sap_e678ffba…`，`state=ready` |

即工具**已注册**，是被**项目级 deny** 从模型工具表摘掉的 —— 正是**会话级 `allow`** 该反转的
（`resolveTools` 用 `Permission.merge(agent.permission, session.permission ?? [])`，会话规则在后、
末次匹配生效）。真实阻断点是**该 allow 从未到达**。

**判别实验**（`PATCH /session/{id}` 带空体 `{}` 会回显**已存储**的会话；`session/session.ts:744`
在无 `permission` 键时保留现值，故可当探针）：

| # | 操作 | 结果 |
|---|---|---|
| A | `POST /api/session` create 体带 `permission:[… allow …]` → 再 `PATCH {}` 回显 | **无 `permission`**（create 丢弃） |
| B | 对活会话 `PATCH {"permission":[{"permission":"sap_transaction_open","pattern":"*","action":"allow"}]}` | **200**，响应体即含该数组 |
| C | 紧接 `PATCH {}` 回显同一会话 | **有 `permission`**（方法有效性对照） |

A 与 B/C 同为 200、结局相反 → **create 路径静默丢弃规则集，只有 PATCH 落库**。
注：`GET /api/session/{id}` 的返回**不含** `permission` 字段，不能用它判断；最初据此得出的
"无规则集"结论无效，故改用 PATCH 回显。

**代码侧**：`agent/coding/sessions.py:_create_upstream` 仅在 `create_session(permission=…)` 下发，
且 docstring 断言"session 会自己存下、重开读回"—— 与实测相反；`reserve()` 对已 `ready` 链接直接
返回，故**没有任何补救路径**，每个经 `reserve()` 创建的工作台会话都拿不到规则集。

**修复与验证**：

- `_create_upstream`：create 之后补 `client.set_permission(...)`（唯一落库路由），失败在标记
  `ready` **之前**抛出，重试沿用幂等语义。
- `reserve()`：已 `ready` 且本次点名了 profile 时**再补一次** → 旧会话下次打开自愈。
- `opencode.py`：更正两处与实测相反的 docstring。
- 测试：假件原**在 create 里就记下 permission**（用例因此在假件上通过、真实工具却不可见 ——
  正是本次失效形态）→ 改为**如实建模**；新增 `test_reopening_a_ready_session_reapplies_the_profile`。
  `test_coding_session_routes.py` + `lifecycle` + `sync` + `test_opencode_client.py` = **113 passed**。
- 同域隐患核查：`prompt.ts:1060` 若 prompt 带 `tools` 会**整体替换** `session.permission`；嵌入端
  **不发** `tools`（全仓仅测试夹具出现 `capabilities.tools`），故本轮修复不会被一次对话覆盖。
- 控制台重载：`python -u app.py`（PID 6580，10s 内应答 200）。**更正**：本条当时把"停掉 9900 后 190s 未被拉起"归因于 `RDAI-AppWatchdog` 的 Repetition `Duration` 为空、`StopAtDurationEnd=True` 即"不按分钟重复"，**该归因是错的** —— 看门狗确实每分钟运行（`NextRun` 正常推进），真因是 `Test-AppStarting` 的信任无上界，见下节 8.10。

### 8.10 看门狗「永不重启」：PID 文件的信任必须有上界（2026-10-06 01:11）

**现象**（8.9 重载时撞见）：停掉 9900 后，看门狗**每分钟都在跑**，却每 20 秒记一次
`port 9900 not listening yet, recorded app process alive -> waiting`，从 `00:44:44` 记到 `00:48:04`，
9900 始终没起来。它会一直这样等下去，而这正是这个脚本唯一要做的事。

**根因**：`Test-AppStarting` 只凭 PID 文件判定"正在启动"，而 `Get-CimInstance` 里任何 `app.py`
都算命中。本机有**两个**控制台跑 `python.exe -u app.py`（`C:\rdai\rsmagent` 与
`C:\oneagent-multi-rc`），Windows 无法询问进程的 cwd —— 脚本注释本身就承认了这点。开机时又发生
了**同款生成风暴**（与 8.8 的重复 API 同族），实测残留：

| PID | 起于 | 由谁拉起 | 持有的监听端口 |
|---|---|---|---|
| 1168 | 00:04:19 | **oneagent** 看门狗 | 无（9898 由 4988 持有） |
| 6768 | 00:04:24 | （rsmagent 生命周期日志记录在案） | 无（9900 当时由 7072 持有） |

谁最后写 PID 文件谁说了算，于是 `.cow.pid` 落在 `6768` 上：它活着、命令行命中 `app.py`，
看门狗便认定"仍在启动"，**永不复位**。

**修复**：给这一信任加上界。`Test-AppStarting` 在命令行命中之外，再要求该进程
`CreationDate` 距现在小于 `$StartGraceSeconds`（app/oneagent=180s，docreader/weknora=300s，
均远高于实测 60–90s 的启动窗口）。**已存在超过启动窗口的进程不是"正在启动"**，无论命令行是什么。
四个看门狗同一处缺陷、同一处修复。

**确定性复现与验证**：把 `.cow.pid` 写成那个 **4016 秒前的活孤儿 `6768`**（即当初的中毒条件），
再停掉真正的 app：

| 观察 | 结果 |
|---|---|
| 看门狗日志 | `app is down (nothing listening on 9900, no live recorded process) -> starting` |
| 拉起 | `app.py launched (PID 4364)` |
| 9900 | **22s 后应答 200** |
| `.cow.pid` | 改写为 `4364`（与 9900 的属主一致） |

修复前同一条件只会一直记 `waiting`（上表 00:44:44–00:48:04 即为真实发生过的记录）。

**回归（健康路径不得误报）**：四个看门狗各手动运行一次 →

| 脚本 | 退出码 | 输出 | 日志增量 |
|---|---|---|---|
| watchdog-app / oneagent / docreader / weknora | 全 0 | 全为空 | **全 0 字节** |

进程集合运行前后**完全一致**（无重复启动）；`9898/9900/50051/8100` 仍 `LISTEN/ANSWER`。

**清理**：确认 `1168`/`6768` 均不持有任何监听端口后终止（服务分别由 `4988`@9898、`4364`@9900
正常承载）。清理后 `9898/9900/50051/8100/4096/3100` 全部 `LISTEN=True/ANSWER=True`；四个脚本
`Parser::ParseFile` 全通过。

**顺带更正**：8.9 节曾把"停掉 9900 未被拉起"归因于看门狗不按分钟重复 —— 那是错的，见上。

**现场目视段：已完成（2026-10-06 00:57）**

控制台以修复版重载后，工作台于 **00:55:47** 新建了绑定 `sap_461a8880639132d8c5d3700a8df64afa`
（会话 `ses_rsm_46864cb6…`）。这是**未经任何人工干预**、纯粹由修复后代码路径产生的证据：该会话
**自带**规则集（`PATCH {}` 回显 `"permission":[{"permission":"sap_transaction_open","pattern":"*","action":"allow"}]`），
而修复前经 `reserve()` 创建的会话一律没有（本文件 8.9 上半段即该缺陷的判别实验）。

| 验收项 | 实测 |
|---|---|
| 插件工具**可调用** | 向 `ses_rsm_46864cb6…` 发"请打开 ME21N 事务代码"，OpenCode 日志出现 `evaluated permission=sap_transaction_open pattern=ME21N action.permission=sap_transaction_open action.pattern=* action.action=allow` —— 即**模型工具表里确已含该工具**（修复前该行根本不可能出现，因为工具被项目 deny 摘掉） |
| 导航**送达** | `cj-sap_workbench-actions` 中该绑定出现 `action_kind=transaction_open, state=succeeded, tool_call_id=msg_10cff9664001q5Hp07w0c0Phx7:ME21N`（另有用户先前的 `…:ME23N`，同样 `succeeded`）。`succeeded` 表示左侧画面已确认接收 |
| 未授权会话**被拒** | 见 8.5：桥接端点无凭据/伪造凭据、直连与经入口 **4/4 → 401** |
| 页面读写**仍不可用** | 发"读取当前页面所有字段的值，并把这张采购订单直接提交保存"，助手明确拒绝并给出能力边界（左侧为可见 iframe、无任何工具可读其 DOM/字段/表格；工作台无提交通道，不得创建/修改/删除任何 SAP 数据），并请用户在正常页面手动录入；`cj-sap_workbench-actions` 全表 `action_kind` **只有 `transaction_open`**（当时 4 行，均 `succeeded`），读页/提交类动作在数据模型里根本不存在 |

**操作员目视确认（2026-10-06 约 06:30，截图 `image-dc420509…`）**：左侧 WebGUI 显示事务码 **ME23N**、标准采购订单 `4500000127`（由 Franz Musterman 创建）；右侧对话先发"打开 ME23N"，助手报告导航已送达，再问"当前界面的信息是?"时明确拒绝读取：
"无法读取左侧 SAP 画面的内容。该画面是跨域 iframe，没有任何工具能读取其字段、表格、标签或消息；`sap_transaction_open` 只确认导航指令已送达，不代表我已看过页面。"
并建议改用 MCP 查 `EKKO`/`EKPO`。对应台账：`cj-sap_workbench-actions` 于 **2026-10-06 06:30:04** 写入
`session=sap_6e40413a…` / `action_kind=transaction_open` / `state=succeeded` /
`tool_call_id=msg_10e302e54001rupQ0edhiZfuAF:ME23N` —— 与截图一致。

附带（纪律面）：修复前那次失败会话 `ses_rsm_c9238813…` 在重载后复跑，日志确认工具**已可见**
（同样出现 `permission=sap_transaction_open pattern=ME21N`），但该绑定在控制台重载时已被置为
`closed`，故桥接返回 `session_not_bound`(403)。助手如实报告"未送达、这不是登录/权限问题"，
并**拒绝**用 shell 直连桥接端点绕过 —— 与 8.5 的纪律要求一致。
