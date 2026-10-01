# 有界刷新、恢复与失效清理（任务 9.3）

对应主规范 `desktop-project-artifacts` 的「文件面板与会话引用使用同一项目」（其中
一句正是本条的核心：*页面刷新后的展示 SHALL 先重验设备及授权；授权无效时不可仅凭
缓存名称继续操作*），以及 A21 的**刷新一半**（另一半是 9.2 的来源适配）——
A21 要求"文件变动能刷新，失效订阅被清理"。任务原文：*为本机目录变更提供有界刷新/
订阅并清理失效作用域，刷新/恢复前重验授权，避免文件面板与执行目标不同步。*

本文记录**实际改了什么、怎么验证的、哪里没做**。

## 一、交付内容

一开面板就只能靠轮询（本机目录没有服务器端的通知通道），所以这一条真正的难点不是
"怎么发现变化"，而是**两个方向的不能说谎**：

1. **没看全不能说看全了**：一次扫描有间隔、目录数、条目数、深度四重上限，任何一重
   到顶都意味着"有些地方这次没看"。把它说成"看全了"，面板就会把**没走到的目录**
   当成**被删掉的目录**——用户看到的是自己的半个项目没了。
2. **授权没了不能说还在**：面板列表、执行目标、本机引用三者必须描述同一个项目。刷新
   时凭页面里记着的名字恢复，或者授权被撤后继续用旧路径列表，都会让三者分叉。

| 关注点 | 实现位置 | 语义 |
|---|---|---|
| 只在有本机绑定时订阅 | `desktop/src/main/project-browser/watch.ts`（新） | `ProjectWatcher.start(workspaceId)` 起轮询，`stop(reason)` 收尾；**刻意不用 `fs.watch`**：面板的读走 `fs-guard`（授权 + realpath 包含检查），监视器若自己开目录，就是第二套文件系统栈、第二套"项目在哪"的判断。轮询同一个 `tree` 调用，进程里只有一套权威 |
| 四重上限 | 同上：`WATCH_MIN_INTERVAL_MS` 1000 / `WATCH_MAX_INTERVAL_MS` 60000 / `WATCH_DEFAULT_INTERVAL_MS` 2000 / `WATCH_MAX_DIRECTORIES` 64 / `WATCH_MAX_ENTRIES` 4000 / `WATCH_MAX_DEPTH` 4 / `WATCH_PAGE_MAX` 200 | 每页向设备要**它肯给的上限**（`device-ops.LIST_LIMIT_MAX` = 200，要更多只会被静默夹回去，于是"整页 + 游标 = 还有没读到的"这个信号就丢了）；一次只跑一个扫描（`inFlight`），失败按 1s→2s→4s→8s 退避到 60s，命中上限的项目按最慢档轮询 |
| 指纹 | 同上 `fingerprint()` | 条目数 + 总字节 + 最新 mtime + 名字集合的 FNV-1a。**不读内容**：逐 tick 做内容哈希不是有界 I/O。helper 的 mtime 是整秒，同一秒内改两次且总大小不变低于该分辨率——这是**已记录**的边界，不是"已经处理" |
| 首次扫描是基线 | 同上 `scanOnce()` | 第一次只记形状、不发事件：否则面板一打开就报"整个项目都变了"，这种假警报会训练用户忽略该功能 |
| 截断 = 不报删除 | 同上 `diff()` | `scan.truncated` 为真时 `removed` 恒为空。"没走到"是**未访问**，不是"已删除"；`truncated` 也随事件上报，面板能说明这是部分视图 |
| 授权搬走即停 | 同上 `readScope()` / `scopeKey()` | 监视是**针对某一次授权**的：`workspaceId`、`bindingId`、`selectionGeneration`、`grantVersion`、`connectionEpoch` 任一变化都算"授权搬走了"（重选目录、换绑定、重新挂载设备、新 grant 版本），监视器 `stop('stale_context')` 并发 `projectWatchEnded`；`stop()` 清空 `seen`，不留可被下一次监视误当基线的残留 |
| 刷新后恢复：问宿主，不问缓存 | `desktop/src/main/project-browser/restore.ts`（新，纯函数）+ `remote-container-ipc.ts::liveLocalContext` | 页面刷新会带走它自己的全部变量，但**确认本身**留在主进程。恢复的判据是**实时**注册表（`liveProjectBinding`），不是记录：记录会活得比它命名的授权更久，直接还回去等于把一个已撤权的目录当成还开着的项目。答案只有三个：`live`（带标识符，**不含目录**）/ `stale`（`grant_revoked`）/ `none`（这个会话本来就没有本机项目，**不是失败**） |
| 恢复只恢复**这一个**会话的 | 同上 `mine` 过滤 | 记录带着确认时的 `agentId` + `businessSessionId`；别的 Agent / 别的会话留下的记录一律答 `none`，绝不"顺手恢复这台机器上最后打开的东西" |
| 恢复后继续订阅 | `liveLocalContext` | `live` 时重新 `projectWatcher.start(workspaceId)`：没有文档显示它时监视可能已经停了，刚回来的页面必须重新被告知变化 |
| 桥与门控 | `remote/host-bridge.ts`（`localContext` 进 `PHASE2_METHODS`，随 `localFiles` 开）、`remote-preload.ts`、`remote-host-ipc.ts`、`channel/web/static/js/fork/desktop-host.js` | 参数逐项校验（`agent_id`/`business_session_id` 必须是 ≤200 字符串）；`suspendLocalContext` 对 `page-unload` **不**撤授权——否则"刷一下页面"等于重新选一次目录 |
| 页面：恢复与失效清理 | `channel/web/static/js/console.js::_desktopRestoreContext/_desktopLocalLost` | 恢复在 Agent + 会话确定后跑一次；迟到的答复按 `requestKey` 丢弃（用户已经切走了）。`stale` **要说话**（`ws_sel_local_lost`），`none` 不说话。失效时先丢引用、再重读选择器、再提示，且**幂等**（重复上报不重复打扰） |
| 页面：选择器不被服务器列表冲掉 | `console.js::refreshWorkspaceSelector` | 本机目录不是服务器项目，后端报不出它：只要本机引用对**这个** Agent+会话仍然有效，就保留 chip，其余情况一律以服务器为准 |
| 页面：面板侧反应 | `channel/web/static/js/workspace.js::wsOnProjectChanged` / `wsApi` | `projectWatchEnded(stale_context)` 与"本机读被拒为 `stale_context`"两条路都走 `_desktopLocalLost`；其余原因只是重新推导来源。拒绝**照常抛给调用方**，面板仍能说明发生了什么 | 
| 文案 | `i18n/core.js` + `core/i18n.js` 的 `ws_sel_local_lost`（zh / zh-Hant / en） | 浏览器与桌面控制台共用一套词典，漏一条就会把原始 key 展示给用户 |

## 二、验证

命令固定为 `node --test <套件>` 与 `.venv/bin/python -m pytest <子集> -q -p no:randomly`。

| 套件 | 结果 | 覆盖 |
|---|---|---|
| `tests/test_desktop_project_watch.cjs`（新增） | **28 项通过** | 真项目 + 真 `fs-guard` + 真 `ProjectBrowser`：首次扫描是基线、根/子目录/嵌套新增、无变化不发事件、删除文件是变化而删除目录是移除、**新建目录挤掉预算时不把没走到的目录说成删除**、重选/新 grant 版本/重挂设备/撤权四种授权变化都停表并清 `seen`、`verifyScope` 主动重验、主动停止的收尾、条目上限与**超过一页的目录**都算部分视图、深度到顶也是部分视图、截断项目走最慢档、健康项目按配置间隔、低于下限被夹住、失败的读退避且不编造变化、同一时刻只跑一个扫描、真定时器下真扫描，以及适配器侧（事件形状、跨绑定/工作区丢弃、结束原因、未订阅面板被忽略、无绑定即无订阅） |
| `tests/test_desktop_project_refresh.cjs`（新增） | **22 项通过** | 控制台侧：刷新后按宿主答案恢复且**只**问 `(agent_id, business_session_id)`、敌意答案里的路径一个字都不留、`stale` 不恢复并提示、`none` 静默、迟到答复丢弃、宿主缺失/抛异常/拒绝都不炸页面、失效清引用 + 重读选择器 + 提示且**幂等**、服务器项目列表不冲掉本机 chip、无本机项目时选择器照旧；面板侧：`stale_context` 结束交给控制台、其他原因只重推来源、本机读被拒 `stale_context` 也断开引用、其他拒绝保留项目；适配器侧：转发参数、老宿主/无宿主答 `none` 且**绝不抛**；纯函数侧（`dist/main/project-browser/restore.js`）：实时重读（记录在、授权没了 → `stale`）、重选后按**当下**版本恢复、别的 Agent/会话的记录不恢复、同一会话最新一次确认生效、答案只含标识符不含目录；文案侧：三种语言都有该串 |
| `tests/test_desktop_project_watch.cjs tests/test_desktop_project_refresh.cjs tests/test_desktop_project_source.cjs tests/test_desktop_remote_host.cjs tests/test_desktop_host_frontend.cjs tests/test_desktop_context_frontend.cjs tests/test_desktop_workspace_menu_frontend.cjs tests/test_desktop_local_selection.cjs` | **186 项通过** | 与 9.2 的来源适配、桥能力清单/参数校验/门控、控制台上下文、目录选择一起跑：新增的 `localContext` 没有破坏任何既有面 |
| `tests/test_desktop_shell_covering.cjs` | **4 项通过** | `remote-container-ipc` / `remote-preload` 覆盖检查 |
| `tests/test_desktop_web_pages.py tests/test_mutation_evidence_scripts.py` | **36 项通过（含 356 subtests）** | 页面桥方法清单包含 `localContext`；变异脚本的结构守卫（锚点存在、必须声明预期失败、只能改声明过的源文件） |

### 变异检查（证明用例真的能失败，而不是碰巧通过）

脚本 `evidence/scripts/mutate_project_watching.py`（可重跑）逐条把实现改成一条
「看起来更省事」的写法，跑同一批用例，出现预期失败后立刻还原源文件并复查内容一致。
16 类走样 **16/16 命中**，输出见 `evidence/project-watching-mutations.log`，退出码 0。

| 变异 | 汇总 | 抓到的用例 |
|---|---|---|
| M1 首次扫描不是基线（一开面板就说整个项目都变了） | 4 failed, 24 passed | `the first scan is a baseline`、`an unchanged project reports nothing at all`、`a new file in the root is reported for the root`、`a revoked project stops the watch…` |
| M2 截断的扫描照样报删除 | 1 failed, 27 passed | `a directory the truncated scan did not reach is not reported as removed` |
| M3 授权搬走了还接着看（只看工作区 id） | 3 failed, 25 passed | `a re-picked project stops the watch…`、`a new grant version is a different authorization…`、`a re-attached device epoch stops the watch` |
| M4 授权被撤了也不停（拿旧授权当现在还在） | 2 failed, 26 passed | `a revoked project stops the watch and forgets what it had seen`、`verifyScope stops a watch whose authorization moved` |
| M5 一次要点得比设备肯给的页还大 | 1 failed, 27 passed | `a directory bigger than one page is a partial view, not the whole directory` |
| M6 队列里还有没看的目录却不说这是部分扫描 | 1 failed, 27 passed | `a directory the truncated scan did not reach is not reported as removed` |
| M7 深度上限不算截断（下面还有目录却说看全了） | 2 failed, 26 passed | `a project too deep to finish is honestly partial`、`a scan that hits its entry bound…` |
| M8 凭记录就恢复（不复验授权是否还在） | 1 failed, 21 passed | `a record whose grant is gone is stale, not an open project` |
| M9 别的会话的记录也当成自己的 | 1 failed, 21 passed | `another chat's confirmation is not resumed` |
| M10 授权没了说成"本来就没有" | 1 failed, 21 passed | `a record whose grant is gone is stale, not an open project` |
| M11 服务器项目列表覆盖本机 chip | 1 failed, 21 passed | `the server project list does not wipe the local chip while it is in effect` |
| M12 失效清理变成空操作 | 1 failed, 21 passed | `losing the local project drops the reference and re-reads the selector` |
| M13 恢复时 `stale` 不告诉用户 | 1 failed, 21 passed | `an expired local project is not resumed, and the user is told why` |
| M14 迟到的答复照样采纳 | 1 failed, 21 passed | `an answer that arrives after the user moved on is dropped` |
| M15 本机读被拒为 `stale_context` 却不断开引用 | 1 failed, 21 passed | `a local read refused as stale_context drops the reference too` |
| M16 宿主拒绝就抛给页面（拒绝变成异常） | 1 failed, 21 passed | `a browser or an older host answers "nothing to resume", never a throw` |

**本轮变异暴露并修掉的三处问题**（都是"用例太弱/实现了谎"，不是脚本问题）：

1. **`if (false)` 不是一条合法的 TS 变异**：TS 对不可达块不做控制流收窄，
   `scan.seen` 直接变成类型错误，整个套件构建失败 —— 27 failed / 0 passed
   *看起来*像"抓到了"，其实一句断言都没跑。脚本因此加了 `broken_build()`：
   输出里出现 `error TS…`/`SyntaxError`/`Cannot find module` 时，该条判为
   **未被发现**并提示改写。M1 也改成一条会真的放过首次扫描的条件。
2. **M2/M6 一开始没抓到**：原用例的截断场景里 `seen` 只有根目录，`removed` 本来就
   是空的，那两行守卫等于没被测。补了真数据用例——用**目录预算**制造"基线走到 A、
   这一轮先走到新目录 B 就没预算了"，于是"没走到"与"被删除"在数据上第一次真正分叉。
3. **`truncated = true` 曾经写了两遍**（循环内的边界分支 + 循环后的"队列非空"）。
   两处等价，任一处被改掉都不可观测——这正是"两处必须一致的重复"。删掉循环内那
   一处，只留循环后那一句（"还有队列没走完"是唯一的真相来源），M6 随之可测。

## 三、明确边界（不勾选的部分）

- **不做 `fs.watch` / 原生目录通知**：本机目录变更靠有界轮询发现，间隔下限 1s。
  大项目（命中上限）按 60s 最慢档轮询。这是刻意的取舍，不打算"以后改成 inotify"。
- **指纹分辨率**：整秒 mtime + 总字节数；同一秒内等大小改写可能不上报。已记录。
- **系统打开/另存为、隔离预览与 CSP**：9.4 / 9.5，未做。
- **A21 的完整验收与安装件真机**：属 9.8 / 10.x；本文只有开发机（macOS）证据，
  Windows 未验证。
- **远端模式下"服务器→设备"的目录变更通道**：本机项目在**远程**模式下的变更通知
  仍走同一条设备命令通道（第 6 组已通），但没有额外的推送式订阅；轮询在同一台设备
  进程内完成。

## 四、未验收/未运行的项

- 未跑全量（本仓库全量约 6789 用例 / 28 分钟）：本次改动只落在桌面主进程、控制台
  与面板前端，按规则只跑相关子集（上表）。
- 真机 Electron 的"刷新页面后本机项目还在"未做端到端点击验证：恢复逻辑的**决定**
  由纯函数用例覆盖，**宿主侧**（`remote-container-ipc` 的记录表 + 实时注册表）由
  变异 M8/M9/M10 与 `test_desktop_shell_covering.cjs` 覆盖；端到端点击属 9.8。
