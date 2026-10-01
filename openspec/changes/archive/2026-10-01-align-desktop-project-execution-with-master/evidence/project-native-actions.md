# 系统打开、打开所在文件夹、复制路径、另存为（任务 9.4，验收 A25 前半）

对应主规范 `desktop-project-artifacts` 的「本机产出可安全预览和系统打开」——原文：
*桌面 SHALL 提供获权本机文件的预览、系统打开、打开所在文件夹及另存为入口……复制完整
本机路径 SHALL 由明确用户动作在本机完成，不作为服务器文件 URL*；以及 A25 行的前半
（"系统打开、打开所在文件夹、复制路径、另存为"四项与"文件变动或无关联应用真实提示"）。
任务原文：*经窄化原生桥实现系统打开、打开所在文件夹、复制路径和另存为；每次重验文件与
grant，缺失/无应用提示真实原因。*

A25 行的**后半**（"上传产生独立获权服务器副本；未操作时不上传"）由 9.7 交付，预览的
隔离与 CSP 由 9.5 交付；本文只记录这四项系统动作，以及它们为什么必须长成这样。

本文记录**实际改了什么、怎么验证的、哪里没做**。

## 一、交付内容

这四项和面板里其它按钮的根本差别是：**动作发生在用户的机器上，而且系统会照做**。
`open` 交给 OS 的关联应用、`reveal` 打开文件管理器、`copyPath` 写系统剪贴板、`saveAs`
在用户选的位置落一份副本——四条路都**不可撤销**，也都不经过服务器。所以难点不是
"怎么调这几个 API"，而是三个"不能说谎"：

1. **不能说"这个文件还在"**。面板五分钟前读到它，不等于现在还能动它：目录可能被删、
   项目可能被重选、授权可能被撤、路径中间某一层可能被换成了指向别处的链接。
2. **不能说"我打开的是你看见的那个文件"**。系统会跟随任何交给它的路径；只在拼接出
   的字符串上判包含，一个指向项目外的链接就能把动作引出去。
3. **不能把目录告诉页面**。要打开就得有绝对路径，而绝对路径正是页面上不该出现的东西
   （它泄漏用户的家目录结构）。路径必须在主进程里算出、在主进程里用掉，回复只带文件名。

| 关注点 | 实现位置 | 语义 |
|---|---|---|
| 四个动作一个决定 | `desktop/src/main/project-browser/native-actions.ts`（新，纯 Node、效果注入） | `planNativeAction()` 回答唯一的问题"这台机器可不可以对**这个**文件动手、它在哪"，`performNativeAction()` 只负责执行。四个动作共用一次判定，而不是四个 handler 各自再推一遍包含关系 |
| 每次重验 | 同上：`root` 由调用方**当下**取（容器里是 `remoteGrantRegistry.absolutePathFor(grantId)`）；`entry` 是 `ProjectBrowser.resolve` 刚答的；`fs.lstatSync` + `fs.realpathSync` 再看一遍磁盘 | 授权、文件、真实路径三者都必须是**这一次调用**的事实。`stale_context`（授权没了）/`not_found`（文件没了）/`changed`（类型变了）各自成码，原样交给用户 |
| 链接不跟随、包含按 realpath | 同上：`stat.isSymbolicLink()` 一律 `not_a_file`；`isInside(realRoot, fs.realpathSync(candidate))` | helper 本就把链接算作链接；这里再看一遍是因为**目录中间的**一层也可能是指向项目外的链接——只比字符串会放过它 |
| 路径不出主进程 | 同上 + `remote/remote-host-ipc.ts::projectNativeAction` | 剪贴板在**主进程**里写（`clipboard.writeText`），对话框在主进程里弹，回复只有 `{opened}`/`{copied}`/`{saved}` 加一个 `name`。**没有**任何字段携带目录 |
| 真实原因 | 同上 `describeOpenFailure()` | 平台自己的话术分四类：无关联应用（`no_application`）、无权限（`permission_denied`）、文件不在（`not_found`）、其余原样透传（`device_error`，不压平成"打不开"）。macOS 的 *"The application cannot be opened for an unexpected reason"* 与 *"There is no application set to open the document"* 都归第一类 |
| 改过的文件不默默复制 | 同上：`expectedMtime`（面板读到的整秒版本）与 helper 报的版本比对，不一致即 `changed`；只有 `acceptCurrent: true`（用户在被告知后确认）才继续 | 与编辑器保存同一条规则：复制用户**没看见**的版本比拒绝更糟。`saveAs` 的目标等于源文件本身时也拒绝——那是"截断"而不是"另存" |
| 桥只认方法名，不认形状 | `desktop/src/main/remote/host-bridge.ts`（`PHASE3_METHODS` 增加四个方法 + `checkBridgeCall` 的 `projectOpenFile/RevealFile/CopyPath/SaveFileAs` 分支） | 动作=方法名（页面无法自造一个动作）；`workspace_id` 必填、`path` 必须过 `checkProjectPath`（绝对路径/盘符/NUL/`..` 直接挡在文件 API 之前）；只有 `projectSaveFileAs` 收 `expected_mtime`（非负整数）与 `accept_current`（布尔）。能力随 `localFiles` 开：关着时四个方法**不在** `methods` 里，也不在 `allMethods` 之外 |
| 容器侧接线 | `desktop/src/main/remote/remote-host-ipc.ts`（`projects` 口新增 `nativeAction`，`remote-container-ipc.ts` 用 Electron `shell` / `clipboard` / `dialog` / `fs.promises.copyFile` 实现效果） | 效果全部集中在容器这一层，`native-actions.ts` 保持可在没有 Electron 的环境里跑（测试里用真文件 + 假效果，能造出"这台机器没有能打开它的应用"） |
| 页面适配 | `channel/web/static/js/fork/desktop-host.js`（`PROJECT_ACTION_METHODS` / `canUseProjectActions()` / `projectAction()`）、`fork/project-source.js`（`CowProjectSource.act()` / `canAct()`） | 面板只知道**路径**，`workspace_id` 由适配器从**实时绑定**加上；宿主旧了或本机能力关着时 `canAct()` 为假、`act()` 回 `no_host`/`feature_unavailable`，而不是让按钮去点一个不存在的接口 |
| 面板：按钮说它要做什么 | `channel/web/static/js/workspace.js::wsUpdateHeaderActions/wsRetitle/revealPreviewFile/copyPreviewPath/downloadPreviewFile/openPreviewExternally`、`channel/web/chat.html` + `templates/views/chat.html` 的 `ws-btn-reveal` | 本机文件时同一排按钮换语义（`ws_open_system`/`ws_save_as`/`ws_local_copy_path`），`title` 与 `data-i18n-title` 同时改（换语言后仍然对）；`ws-btn-reveal` 对服务器文件保持 `hidden`——服务器文件在这台机器上没有"所在文件夹" |
| 面板：四种失败四种话 | `workspace.js::wsLocalActionMessage/wsLocalAction/wsSaveLocalCopy/wsLocalConfirm` | `not_found`/`changed`/`stale_context`/`no_application`/`permission_denied`/`feature_unavailable` 各有各的说法，宿主自己的解释作为后缀附上；`cancelled`（用户自己关掉保存框）**不说话**；`changed` 先问用户（`window.confirm`），拒绝再复制 |
| 面板：卡片 | `workspace.js::renderFileCard` + 文档级 `local-*` 点击委派 | 本机卡片的按钮是 `local-open`/`local-reveal`/`local-copy-path`/`local-save-as`，**不是** `download`：本机文件没有 `raw_url`，给它一个下载按钮等于给一个必然 404 的地址 |
| 文案 | `i18n/core.js` + `core/i18n.js` 的 `ws_open_system` / `ws_reveal_file` / `ws_save_as` / `ws_local_copy_path` / `ws_local_opened` / `ws_local_revealed` / `ws_local_copied` / `ws_local_saved_as` / `ws_local_no_application` / `ws_local_save_as_changed` / `ws_local_action_unavailable` / `ws_local_action_failed`（zh / zh-Hant / en），并同步 `tests/fixtures/console_i18n_snapshot.json` | 两份词典共用，漏一条就会把 `ws_open_system` 这样的原始 key 展示给用户 |

## 二、验证

命令固定为 `node --test <套件>`（前端）与 `.venv/bin/python -m pytest <子集> -q -p no:randomly`。

| 套件 | 结果 | 覆盖 |
|---|---|---|
| `tests/test_desktop_project_native_actions.cjs`（新增） | **24 项通过** | 决策层：真目录真磁盘上打开/定位/复制路径/另存各一次，回复里**不含**项目目录；OS 失败三种（无应用、无权限、抛异常）各自成码且保留原话；另存确实在目标落下一个内容相同的副本、对话框只拿到文件名；版本冲突（拒绝 → 用户确认 → 接受）与"版本一致不算冲突"；对话框被关掉是 `cancelled` 且不写盘；复制到自身被拒且原文件不被截断；文件已删/链接/目录另存/中间层链接逃出/类型变化/helper 自身拒绝/无 grant/根目录消失/畸形路径（含 `C:\`、`..`、NUL、空串）与未知动作名。桥层：四个方法随 `localFiles` 开关出现在 `methods` 中、逐个到达 `nativeAction` 口、畸形参数在桥上被挡（不落到宿主）、拒绝码原样返回、口缺失或没有口时是 `feature_unavailable`。适配层：动作带实时 `workspace_id`、`expectedMtime`/`acceptCurrent` 转成 `expected_mtime`/`accept_current`、无绑定回 `no_host`、无路径回 `invalid_request`、桥抛错被转成同形答复。页面层：四种拒绝四种说法、改动后先问用户（同意才重试、拒绝只问一次）、头部按钮语义切换、卡片动作 |
| `tests/test_desktop_web_pages.py::PageAuditTests::test_w25_the_local_file_actions_are_reachable_and_speak_the_hosts_language`（新增） | **27 项通过**（本文件） | 面向**装配后的页面**：四个按钮在 `chat.html` 里都真实存在、`onclick` 指向真实函数、`ws-btn-reveal` 默认 `hidden`；面板只经 `CowProjectSource.act` 发动作；两份词典 × 三种语言都齐（缺一条就会把 key 当文案显示） |
| `node --test tests/test_desktop_*.cjs` | **631 项通过，0 失败** | 全部桌面端 node 套件（含桥、设备客户端、v2 契约、执行、日志等）在本次改动后无回归 |
| `.venv/bin/python -m pytest tests/test_desktop_*.py tests/test_mutation_evidence_scripts.py` | **1185 项通过，1 跳过（608 subtests）** | 全部桌面端 Python 套件 + 变异脚本自检；跳过的是环境相关用例 |

### 变异检查（证明用例真的能失败，而不是碰巧通过）

脚本 `evidence/scripts/mutate_project_native_actions.py`（可重跑）逐条把实现改成一条
"看起来更省事"的写法，跑同一批用例（`tests/test_desktop_project_native_actions.cjs` +
`tests/test_desktop_web_pages.py`），出现预期失败后立刻还原源文件并复查内容一致。
输出见 `evidence/project-native-actions-mutations.log`，退出码 0：**22 类走样全部被对应用例
判为失败**。

| 变异 | 汇总 | 抓到的用例 |
|---|---|---|
| M1 用 `statSync` 代替 `lstatSync`（跟随链接，把链接当普通文件） | 2 failed, 38 passed | `…a link…is refused`、`…resolves outside the project…` |
| M2 只在拼接的字符串上判包含（目录里的链接把动作引到项目外） | 1 failed, 39 passed | `…resolves outside the project…` |
| M3 绝对路径也收下（页面能指名项目外的文件） | 1 failed, 39 passed | `a path that could never be project-relative is refused before any check` |
| M4 条目类型和 helper 说的不一致也不管（打开用户没看见的东西） | 1 failed, 39 passed | `an entry whose kind changed on disk is refused rather than acted on` |
| M5 系统打不开也回成功（用户以为开了） | 1 failed, 39 passed | `an operating system failure is reported as the system described it` |
| M6 把"没有关联应用"压成"打不开"（用户不知道该装什么） | 1 failed, 39 passed | 同上 |
| M7 复制路径时顺手把真实路径交给页面（目录结构漏出去） | 1 failed, 39 passed | `reveal and copy-path act on the machine and keep the path out of the reply` |
| M8 版本比较写反（拿相同当冲突，改过的文件照样复制） | 1 failed, 39 passed | `a copy is refused while the file has moved on, unless the user says so` |
| M9 允许复制到文件自身（把原件截断成一份副本） | 1 failed, 39 passed | `a copy onto the file itself is refused rather than truncated` |
| M10 路径形状不查就递给宿主（畸形路径直达本机） | 1 failed, 39 passed | `a malformed action never reaches the port` |
| M11 版本号只查类型不查符号（负版本放行到宿主） | 1 failed, 39 passed | 同上 |
| M12 本机文件能力还没开就报四个动作可用（页面点了才失败） | 1 failed, 39 passed | `the four actions are published with local files, and not without them` |
| M13 动作不带当前工作区（宿主不知道该问哪个项目） | 4 failed, 36 passed | `an action carries the live workspace, and a refusal comes back as an answer` |
| M14 只看宿主不看绑定（没有本机项目也照样问） | 1 failed, 39 passed | `an action without a project, or without a path, is refused by name` |
| M15 桥抛错就抛给页面（拒绝变成异常） | 1 failed, 39 passed | 同上 |
| M16 本机文件的按钮不换语义（点了去开一个不存在的服务器地址） | 1 failed, 39 passed | `a local file's buttons say what they will do…` |
| M17 本机文件的卡片给一个服务器下载地址（点了必然 404） | 1 failed, 39 passed | `a local file card carries the system actions instead of a server download` |
| M18 改过的文件不问用户就复制（默默复制用户没看过的版本） | 1 failed, 39 passed | `saving a copy asks before it copies a version the user has not seen` |
| M19 四种失败共用一个说法（"文件没了"和"没装应用"分不开） | 2 failed, 38 passed | `the four reasons a system action did not happen are told apart` |
| M20 定位按钮默认就显示（服务器文件也被提供一个做不到的动作） | 1 failed, 26 passed | `test_w25_the_local_file_actions_are_reachable_…` |
| M21 宿主只认预载方法名（面板的四个动作全被按名字拒绝） | 2 failed, 38 passed | `every action the panel names reaches the preload method the host declares` |
| M22 动作表当字典用（`constructor` 也算一个动作名） | 1 failed, 39 passed | `an action name that is not one of the four is refused, table hooks included` |

### 验收期补上的一条缝：四个动作在真机上点不动（已修，M21/M22 就是它的变异）

9.8 的验收要求"卡片/系统打开"要有**真的走一遍**的证据，而在此之前这条链路的每一层
都是各自单独测的：`makeBridge` 那批用例驱动**真的 IPC handler**，但另一侧是测试自己
造的桩；`loadAdapter` 那批驱动**真的适配器**，另一侧也是桩。于是两半各自都对，
**中间那一层没人比过**：

- `CowProjectSource.act()`（`fork/project-source.js`）把面板说的动作名
  （`open` / `reveal` / `copyPath` / `saveAs`，也就是文件卡片按钮上那四个 `data-action`）
  原样交给 `CowDesktopHost.projectAction()`；
- `CowDesktopHost.projectAction()`（`fork/desktop-host.js`）当时拿
  `PROJECT_ACTION_METHODS`（**预载方法名**：`projectOpenFile`…）去比这个名字，
  比不中就回 `invalid_request: unknown project action`。

结果：桥两端都"好"，`canAct()` 也是 `true`、四个按钮照常画出来，但**每一次点击**都在
还没有碰到预载之前就被拒；页面把 `invalid_request` 归到兜底文案（`ws_local_action_failed`），
用户看到的是"操作失败"，没有任何线索指向"这台机器从来不会收到这个请求"。
`tests/test_desktop_project_native_actions.cjs` 里 M13/M15 之外全是绿，正是这个盲区的形状。

修法：把"动作名 → 预载方法"做成**一张表**（`PROJECT_ACTIONS`），
`PROJECT_ACTION_METHODS` 由它派生（`canUseProjectActions()` 认的能力清单与
`projectAction()` 认的动作名从此不可能各说各话）；查表用 `hasOwnProperty`，
所以 `constructor` / `toString` 这类原型键不是动作名。

新增用例（同一个文件，`node --test tests/test_desktop_project_native_actions.cjs`，
40 项全绿）把两个真模块**按页面加载的顺序**放进同一个沙箱、下面垫一个按容器行为作答的
预载桩，四个动作逐一断言"以预载方法名的形态到达预载"，外加预览同一条缝的接线，
以及"这台宿主没有的动作按名字拒绝、预载根本不被调用"。M21 就是修之前的那一版写法，
它现在必然被这条用例抓住。

### 两处必须写下来的坑

两处**必须写下来**的坑（都被脚本的"变异后根本编不过"判定挡住，也就都被改写过）：

- 想把链接判定写成 `stat.isSymbolicLink() && entryKind === 'link'`，因为 helper 的取值被
  收窄成 `'dir' | 'file'`，TS 报 `TS2367`——**变异后编译失败**会被 `node --test` 报成
  "24 项全红"，看起来像抓到了，其实一句断言都没跑。现在的判定按 `Command failed:`
  识别这种"根本没起来"，并把它算作**未抓到**（这条盲点第一次跑时正是这样漏掉了 M1/M8）。
- 变异锚点必须**唯一**：同一个 `checkProjectPath(params.path, false)` 在 resolve/read/write
  三个 case 里也有，只写一行会改到别的分支上，于是套件全绿、脚本却报告"变异没被发现"。
  `tests/test_mutation_evidence_scripts.py::test_every_anchor_is_the_only_place_it_could_mean`
  现在对**每一个**变异脚本做这个结构检查（顺带修掉了 9.1/9.3/8.5 三个脚本里同样的歧义锚点）。

> 恢复说明：`desktop/src/main/remote/host-bridge.ts` 的改动在本地曾被一次误还原抹掉。
> 该文件已被重建，并用**还原前的编译产物逐字节比对**验证（重建后的 `.ts` 编译结果与该
> 产物完全一致），随后重跑了上述全部桌面套件；这不是"看起来补回来了"，是比对过的。

## 三、边界与未做

- **预览的隔离与 CSP 不在本文**：A25 行里"生成的 HTML 不得取得原生执行桥/凭据/额外文件
  能力"由任务 9.5 交付（`execution-isolation` 的隔离预览面）。本文的 `preview` 按钮只把
  **本机已获权**的文件交给系统应用或本机对话框，不把内容渲染进页面。
- **上传/分享并建立服务器副本不在本文**：A25 行的后半与"显式交付服务器"场景由任务 9.7
  交付。本文四条动作**从不上传**任何内容——`copyPath` 写的是本机剪贴板，`saveAs` 落在
  用户选的本地位置，两者都不经过服务器。
- **端到端**：A25 的本机/远程 E2E 卡片与系统打开证据在任务 9.8 汇总。
- **已记录的平台边界**（代码里有注释，这里不当作已解决）：
  - 版本比较用 helper 的**整秒** mtime（与 9.3 同一个已知分辨率边界）：同一秒内改两次且
    大小不变，不会触发"文件已改"的询问；
  - 链接一律拒绝、不解析（项目内链接也是）：这是**故意**的，链接是逃逸的唯一天然通道；
  - 目录不能另存为单个文件（`not_a_file`）；
  - 另存为不提供"覆盖目标"语义：目标等于源文件时直接拒绝，其它同名情况交给系统对话框；
  - `open`/`reveal` 的效果由 Electron 提供，测试用假效果覆盖（真平台行为靠 9.8 的 E2E）。
