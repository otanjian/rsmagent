# 无原生桥的隔离预览、限制性 CSP 与短期受保护资源（任务 9.5，验收 A25）

对应主规范 `execution-isolation` 的「隔离预览面」——原文：
*生成的 HTML 或其它本地产出在桌面端显示时 SHALL 运行在无原生桥、受限 CSP 的隔离面内，
不得取得原生执行桥、凭据或额外文件能力；获权本机文件的预览 SHALL 通过短期、单文件的受
保护资源交付，且页面无法把该资源读回脚本。*任务原文：*实现无原生桥的隔离预览、限制性
CSP 与短期受保护资源。*

A25 行的"系统打开 / 打开所在文件夹 / 复制路径 / 另存为"四项由 9.4 交付
（`project-native-actions.md`），"上传产生独立获权服务器副本"由 9.7 交付；本文只记录
**预览**这一段。

本文记录**实际改了什么、怎么验证的、哪里没做**。

## 一、交付内容

本机文件没有 URL，而两条最省事的路都是错的：

- 开一条 `/preview/<name>` 服务器路由 —— 那会渲染**服务器的**同名文件，用户点的是自己
  机器上的报告，看到的是别人的（或根本没有的）东西；
- 把 `file://` 路径交给页面 —— 那等于给用户的磁盘发布一个**永久、可猜、且落在唯一持有
  原生桥的那个文档里**的地址。

所以字节走了一条绕路，而且**从不进入页面**：

```
   1. 调用方按系统动作的同一套规则重新判定  planLocalPreview → planNativeAction
   2. 字节在  主进程   里读，有上限                        readLocalPreviewContent
   3. 存进票据：单文件、无路径、在钟上到期                    createPreviewTickets
   4. 页面只拿到 cow-preview://preview/<token>/<name>，且
      每一次回答都盖上该类型的限制性 CSP                      answerPreviewRequest
```

| 关注点 | 实现位置 | 语义 |
|---|---|---|
| 一个决定，四类拒绝 | `desktop/src/main/project-browser/preview.ts::planLocalPreview` | 判定**不是重写的**：`planNativeAction` 已经问了实时 grant、helper 的答复、磁盘、真实路径（没了 / 链接 / 变了 / 逃出 / 撤销各自成码）。预览只加问三件事：必须是文件（`not_a_file`）、必须装得下（`limit_exceeded`）、必须是这个面能渲染的类型（`unsupported_type`）。目录、链接、项目外一层链接都被挡在这里 |
| 上限是"拒绝"不是"截断" | `readLocalPreviewContent` + `defaultPreviewReader` | 位置读，一次读 `limit + 1` 字节：**多出来的那一个字节就是**"没装下"的答案。记录里的大小和实际不符时同样拒绝 —— 半份报告按整份渲染是对文件的谎 |
| 上限 8 MiB / 120 秒 / 8 个 | `PREVIEW_MAX_BYTES` / `PREVIEW_TICKET_TTL_MS` / `PREVIEW_TICKET_MAX` | 不是契约值，是"看一眼"的边界：再大的报告应该用真正的应用打开；地址泄漏了也已经不值钱；超出上限时**最老的**票据先走（最老的预览最不可能还在加载） |
| 一个票据授权一个名字 | `previewResourceFor` | 令牌是授权，文件名是**装饰**（窗口标题、下载名）。`asked.name !== ticket.name` 即 404：票据换不到兄弟文件、换不到目录、换不到"没名字" |
| 未知/过期/撤销一律 404 且无字节 | `answerPreviewRequest` | **没有**"退回上一次的内容"这条分支。拒绝文案是通用的一句，所以探针也无法用状态码推断某个文件是否存在 |
| 策略按类型分两档 | `previewContentSecurityPolicy` | html/htm/svg：`default-src 'none'` + `img-src/media-src data: blob:` + `font-src data:` + `style-src/script-src 'unsafe-inline'` + `connect-src/form-action/frame-src/object-src 'none'` + `base-uri 'none'` + `sandbox allow-scripts allow-forms allow-modals`。图片/录音：只留 `sandbox`，**不给脚本**。`allow-same-origin` 与 `allow-popups` 在两档里都**不存在** —— 前者的缺席就是整条隔离（文档变成不透明源：无 cookie、无存储、无同源、读不到父文档） |
| 每份回答都不可缓存 | `previewHeaders` | `Cache-Control: no-store` 是隔离的一部分：能缓存就能让一份过期副本比授权它的票据活得更久。另加 `X-Content-Type-Options: nosniff`（防类型嗅探把数据当代码）与 `Referrer-Policy: no-referrer`（不把宿主地址交给被预览的文档） |
| scheme 特权是一次**测量过的值** | `PREVIEW_SCHEME_PRIVILEGES = { standard, secure, stream }` | 刻意**没有** `supportFetchAPI` / `corsEnabled`：有它们，容器文档就能 `fetch()` 一个预览 URL 把本机文件读进脚本 —— 正是本任务要防的那件事。`bypassCSP` 也刻意关着，回答里的策略才有约束力。`standard` 让 URL 有可解析的源、`secure` 让页面不被混合内容拦住、`stream` 让媒体能分片读 |
| 令牌为什么在路径里 | `PREVIEW_HOST = 'preview'` + `previewUrlFor` | 一次"每个票据一个主机名"的写法是个陷阱：不透明主机被 Node 逐字节比较、被 Chromium 大小写折叠，一个含大写字母的令牌会在 handler 里和在窗口里解析成两个东西。固定主机把这个问题取消掉，令牌放在两个解析器都同意的地方 |
| 路径只到日期之前的扩展名 | `PREVIEW_TYPES` / `previewKindOf` | 一张表同时给出 kind 与 Content-Type（两张表就是两次不一致的机会）。**SVG 是标记不是图片**：它能带脚本，所以按 html 档服务并被沙箱。**PDF 不在表里**——见 §三 |
| 桥只认方法名与形状 | `remote/host-bridge.ts`（`PHASE3_METHODS` + `projectPreviewFile` 分支）、`remote/remote-host-ipc.ts`（`projects.previewFile`） | `workspace_id` 必填、`path` 必过 `checkProjectPath`（绝对路径/盘符/`..`/NUL 在碰文件 API 之前就被挡）；能力随 `localFiles` 开关：关着时方法**不在** `methods` 里，调用也被拒（`feature_unavailable`）；宿主旧了没有这个方法同样按名字拒绝，**不是**静默成功 |
| 容器侧接线 | `remote/remote-container-ipc.ts` | `projectPreviewFile()` 每次重验（root 从**当下**的 grant 取）、有界读、签票据，回复只有 `{name, kind, size, url, expires_at}` —— 没有任何字段能藏一个目录。`protocol.registerSchemesAsPrivileged` 在 import 时（`app.ready` 之前）执行，handler 装在**页面所在分区**的会话上（否则页面里的 URL 没人回答），随分区一起卸载；票据按 attach 建、detach 时 `revokeAll()` |
| 页面适配 | `fork/desktop-host.js`（`PROJECT_PREVIEW_METHODS` / `canUseProjectPreview` / `projectPreview`）、`fork/project-source.js`（`CowProjectSource.preview` / `canPreview`） | 面板只知道路径，`workspace_id` 由适配器从**实时绑定**加上；宿主抛错被转成同形的拒绝，而不是把异常抛进页面；`canPreview()` 先问宿主，问不到就是假 |
| 面板：嵌入 URL，不碰字节 | `workspace.js::wsLocalPreviewTicket / wsRenderPreview / wsRenderLocalMedia` | HTML 走 `frame.src = ticket.url`（帧上另设一遍 `sandbox`，与回答里的策略是两个独立决定）；图片/视频/音频走 `media.src = ticket.url`；本机文件**永不**指向 `rawUrl`。地址按设计会到期，所以图片失败**只重签一次**，第二次如实报原因（视频/音频不重签：用户正在看，换源比让他刷新更怪） |
| 面板：五种拒绝五种话 | `workspace.js::wsLocalPreviewMessage / wsLocalPreviewUnsupported / wsSetLocalPreviewFallback` | `not_found` / `stale_context` / `device_offline` / `limit_exceeded` / `unsupported_type` 各说各的；`feature_unavailable`（宿主旧）是**唯一**退回旧行为的分支，真实拒绝绝不被一次陈旧的渲染盖掉；没有内联预览时给"用系统应用打开"（9.4 那个真能用的动作） |
| 文案 | `i18n/core.js` 的 `ws_local_preview_unsupported` / `ws_local_preview_too_large` / `ws_local_preview_failed`（zh / zh-Hant / en） | 三份字典各一条，漏一条就会把原始 key 展示给用户 |

## 二、验证

命令固定为 `node --test <套件>`（前端）、`.venv/bin/python -m pytest <子集> -q -p no:randomly`
与 `desktop/node_modules/.bin/electron desktop/e2e/preview-isolation.probe.cjs`（真 Electron）。

| 手段 | 结果 | 覆盖 |
|---|---|---|
| `tests/test_desktop_isolated_preview.cjs`（新增） | **27 项通过** | 决策层：真目录真磁盘上 HTML/PNG 可预览且带 kind/size/absolutePath（绝对路径只到主进程）、SVG 是标记、xlsx/txt 按名字拒绝且消息指向系统应用、超限按字节拒绝（恰好等于上限则放行）、目录/链接/项目外一层链接/helper 自身拒绝（原码原话）/无 grant/根目录消失/大小写扩展名。票据层：URL 无路径、令牌不可从文件名推出、两个预览的 URL 不同、到期即止、可提前撤销、可全撤、有上限且淘汰**最老的**、一个票据换不到另一个名字/别的主机/别的 scheme/一段路径。策略层：两档 CSP 的每一条指令、`allow-same-origin` 与 `allow-popups` 的缺席、`no-store`/`nosniff`/`no-referrer`、404 无字节、scheme 特权恰好是三项。桥与容器层：能力随开关、行参加实时 `workspace_id`、回复里没有 `ws_1`、畸形路径不落到宿主、拒绝码原样、口缺失/无口/宿主抛错分别是 `feature_unavailable`/`feature_unavailable`/`device_error`（且保住宿主原话）、preload 暴露、`PHASE3_METHODS` 收录、容器在 ready 前注册且在页面分区上装卸。页面层：只经 `CowProjectSource.preview`、帧与媒体都指向票据、拒绝五种说法、`feature_unavailable` 是唯一回退、三份字典齐 |
| `desktop/e2e/preview-isolation.probe.cjs`（新增，真 Electron） | **16 项检查，0 失败，PASS**（输出见 `evidence/isolated-preview-probe.log`） | 一个真 HTTP 页面（带真 cookie 与哨兵桥）+ 一个真 `protocol.handle`：`cow-preview:` 文档真的能加载并跑自己的内联脚本；普通内容照常渲染；帧报给页面的事件源是 `"null"`（不透明源），`localStorage` 被拒、`document.cookie` 抛异常而宿主页面读得到 `console_session=super-secret`；读不到父文档、不能导航顶层；`desktopHost`/`electronAPI`/`require`/`process`/`module`/`ipcRenderer` 六种名字**全部**不存在；帧内 `fetch` 本机服务器与 `fetch` 另一个预览 URL 都被拒，真 WebSocket 到本机端口被拒，服务器自己的请求日志**只有** `/console`；帧内 `<img>` 指向另一个**有效**兄弟票据也加载失败（策略而不是票据在拦）；宿主页面能把受保护 URL 嵌进 `<img>`（正常内容仍可展示）；宿主页面 `fetch()` 同一个 URL **读不到字节**；撤销后 URL 从 fetch 和 `<img>` 两头都失效 |
| `node --test tests/test_desktop_*.cjs` | **658 项通过，0 失败** | 全部桌面端 node 套件无回归 |
| `.venv/bin/python -m pytest tests/test_desktop_*.py tests/test_mutation_evidence_scripts.py -q -p no:randomly` | **1185 项通过，1 跳过（608 subtests）** | 全部桌面端 Python 套件 + 变异脚本结构自检 |

> 一次记录在案的抖动：首次跑上面那条 Python 命令时
> `tests/test_desktop_remote_dispatch.py::BackgroundHandleTests::test_a_command_withdrawn_before_any_device_touched_it_is_released`
> 失败 1 项（`1 failed, 1184 passed`）。它带 `threading.Event` + `wait_timeout=15`，与预览
> 的改动面（`preview.ts` / 桥 / 页面）没有交集；单独跑该文件 **92 项全通过**，同一条命令
> 重跑 **1185 项全通过、0 失败**。判断为负载相关的时序抖动，**不是**本次改动引入；已
> 记录而不顺手"修"它。

### 变异检查（证明用例真的能失败，而不是碰巧通过）

脚本 `evidence/scripts/mutate_isolated_preview.py`（可重跑）逐条把实现改成一条"看起来
更省事"的写法，跑 `tests/test_desktop_isolated_preview.cjs`，出现预期失败后立刻还原源文件
并复查内容一致。输出见 `evidence/isolated-preview-mutations.log`，退出码 0：**35 类走样
全部被对应用例判为失败**。

| 变异 | 汇总 | 抓到的用例 |
|---|---|---|
| M1 目录也当文档预览 | 1 failed, 26 passed | `a directory is not a document…` |
| M2 超过上限也照样预览 | 1 failed, 26 passed | `a file too large … is refused, not truncated` |
| M3 不认识的扩展名当图片 | 2 failed, 25 passed | `the kind follows the file…`、`a kind with no isolated preview is refused by name…` |
| M4 扩展名不小写 | 1 failed, 26 passed | `the kind follows the file…` |
| M5 SVG 当纯图片 | 1 failed, 26 passed | 同上 |
| M6 读取时截断而不是拒绝 | 1 failed, 26 passed | `a preview reads at most its bound…` |
| M7 读回来超界不拦 | 1 failed, 26 passed | 同上 |
| M8 不把上限交给读取器 | 1 failed, 26 passed | 同上 |
| M9 令牌就是文件名 | 1 failed, 26 passed | `the token is not derivable from the name…` |
| M10 过期不生效 | 2 failed, 25 passed | `a ticket stops answering when it expires…`、`an unknown, expired or revoked ticket is a 404…` |
| M11 没有上限 | 1 failed, 26 passed | `the store is bounded…` |
| M12 一个票据能换成另一个文件名 | 1 failed, 26 passed | `one ticket cannot be spent on another name…` |
| M13 别的主机也认 | 1 failed, 26 passed | 同上 |
| M14 未知票据不再 404 | 2 failed, 25 passed | `an unknown, expired or revoked ticket is a 404…`、`one ticket cannot be spent on another name…` |
| M15 把 `allow-same-origin` 加回去 | 1 failed, 26 passed | `every preview policy is restrictive…` |
| M16 图片和录音也放脚本 | 1 failed, 26 passed | 同上 |
| M17 生成页可以连网 | 1 failed, 26 passed | 同上 |
| M18 允许缓存 | 1 failed, 26 passed | `a live ticket answers with exactly its own file…` |
| M19 引用来源漏出去 | 1 failed, 26 passed | 同上 |
| M20 URL 里把文件名放在令牌前面 | 5 failed, 22 passed | `a ticket names one file…` 等 5 项 |
| M21 路径形状不查就递给宿主 | 1 failed, 26 passed | `a malformed preview never reaches the port…` |
| M22 预览不提 `workspace_id` 也收 | 1 failed, 26 passed | 同上 |
| M23 宿主抛错被压成"不支持预览" | 1 failed, 26 passed | `a host that cannot preview says so by name…` |
| M24 本机文件关着也把预览报成可用 | 1 failed, 26 passed | `the preview is published with local files…` |
| M25 给 scheme 加上 `supportFetchAPI` | 1 failed, 26 passed | `the scheme privileges are exactly the three the probe measured` |
| M26 装到别的会话上 | 1 failed, 26 passed | `the scheme is registered before ready and installed on the container session` |
| M27 卸载时不收票据 | 1 failed, 26 passed | 同上 |
| M28 本机 HTML 指向服务器地址 | 1 failed, 26 passed | `a local preview asks the host and embeds the answer…` |
| M29 本机图片指向服务器地址 | 1 failed, 26 passed | 同上 |
| M30 面板不再问宿主 | 1 failed, 26 passed | 同上 |
| M31 帧不再带沙箱标记 | 1 failed, 26 passed | `the shell never frames a local file without a sandbox…` |
| M32 问宿主能不能预览之前就报可用 | 1 failed, 26 passed | `a refusal the page can act on is shown with the action that works` |
| M33 任何拒绝都当成"这台机器不支持" | 1 failed, 26 passed | 同上 |
| M34 太大和类型不支持共用一个说法 | 1 failed, 26 passed | 同上 |
| M35 一种语言漏掉一句话 | 1 failed, 26 passed | 同上 |

三处**必须写下来**的坑（都改写过变异，而不是改弱用例）：

1. **"变异没被发现"有时是变异根本不可观测**。最初写的 M23 是把
   `remote-host-ipc.ts` 里预览那道的 `!isRemoteLocalFilesEnabled()` 闸门删掉。它跑出
   `0 failed, 27 passed` —— 因为能力关着时**桥层**已经凭 `methods` 拦下了调用，那个分支
   在公开面上不可达。把它硬写成"变异"只会是自欺。现在 M23 换成同一段 `catch` 里的真实
   走样（把宿主抛错压成 `feature_unavailable`），并为此补了一条区分"这台机器不支持"与
   "这台机器刚才坏了"的用例；闸门本身在代码里保留为纵深防御，并在注释里写明它不可观测。
2. **懒匹配的断言等于没断言**。`/function wsLocalPreviewUnsupported\(refusal\) \{[\s\S]*?code === 'feature_unavailable'…/`
   会一路 lazy 到文件**后面**的 `wsLocalPreviewMessage`，那里面有字面相同的一行 ——
   于是 M33（把函数写成 `return true`）照样"通过"。断言现在锚定整个函数体。
3. **锚点必须唯一，而且会被别的任务弄得不唯一**。9.5 往 `workspace.js` 和
   `project-source.js` 里加了两处与 9.4 字面相同的行（`if (code === 'not_found') …`、
   `if (!ctx || !bridge)`），把 9.4 变异脚本的 M14/M19 变成了歧义锚点 ——
   `tests/test_mutation_evidence_scripts.py` 当场抓住。两个锚点已延长到各自独有的邻居行，
   9.4 的脚本重跑仍是 20/20 全中。

## 三、边界与未做

- **PDF 不做内联预览，这是测量结果不是遗漏**：这个 shell 以 `plugins: false` 运行，
  Chromium 没有 PDF 查看器，`application/pdf` 的帧渲染成空白。面板因此显示"本机文件没有
  内联预览"并给"用系统应用打开"（9.4 的真动作）。`PREVIEW_TYPES` 里没有 pdf 是一条有意
  的决定，不是忘加。
- **被预览的文档拿不到外部资源**：票据是为**一个**文件签的，一个想要自己兄弟文件的页面
  不是这个面能展示的页面。这条边界写在代码注释里，也是探针"连另一个有效兄弟票据都加载
  不出来"那一条检查的由来 —— 它是**故意**的，不是"策略写紧了"。
- **预览不改动、不上传任何内容**：票据里的字节是只读的副本，`readLocalPreviewContent`
  只读不写；没有任何路径把内容送去服务器（上传/分享是 9.7 的显式动作）。
- **`stream: true` 的语义**是 Chromium 的分片读能力，用于 `<video>`/`<audio>` 的范围请求；
  同一次预览重复读**同一个**票据是允许的（票据不是一次性的：窗口可能先加载文档再重载）。
  让它失效的是钟、`revoke`、`revokeAll`，不是使用次数。
- **已记录的平台边界**：
  - `location.origin` 在不透明源里仍报 URL 的元组（`cow-preview://preview`），**有效**源
    是 `null`（探针同时测了两个：帧在 `postMessage` 上暴露给页面的是 `"null"`）。这不是
    矛盾，是"URL 的元组"与"安全源"两件事；隔离由后者决定（存储与 cookie 都不可用）。
  - 沙箱里的 `document.cookie` 在 Chromium 里**抛异常**而不是返回空串；探针把"抛异常"
    接受为"读不到"，同时验证宿主页面读得到那个 cookie。
  - 跨设备（远程）预览沿用同一个 scheme：`cow-preview:` 只在**页面所在分区**的会话上
    有 handler，远程模式下那个分区由容器创建，所以本机与远程走的是同一条代码路径。
- **真平台行为靠本次探针**（真 Electron、真 Chromium、真 protocol handler），不是靠假
  效果：单测证明决策、票据与策略**作为值**是对的，探针证明 Chromium **拿它们做了什么**。
- **端到端**：A21–A25/A34/A35 的完整验收与本地/远程 E2E 卡片在任务 9.8 汇总，见
  `evidence/p3-artifacts-and-actions.md`（§三 是同一批脚本在真 Chromium 里点卡片的探针，
  并记录了那次整体走通时暴露的"动作名 / 预载方法名不是一套词"缺陷）。
