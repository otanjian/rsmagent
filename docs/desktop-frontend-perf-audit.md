# 桌面端前端性能审计报告

- **审计对象**：`desktop/src/renderer`（Vite 6 + Electron 33 + React 18.3 + Tailwind 3 + zustand 5；无 SSR、无 Next.js）
- **审计依据**：`vercel-react-best-practices` 技能（Vercel Engineering，80 条规则 / 8 类，按 impact 分级）
- **审计方式**：只读静态审计 + 真实构建实测。**未修改任何源码**
- **代码规模**：92 个 `.ts/.tsx`、29,401 行；`useState` 381、`useEffect` 166、`useMemo` 42、`useCallback` 86、**`React.memo` 0**
- **显式排除的规则组**（不适用，避免误报）：`server-*` 全部、`async-api-routes`、`rendering-hydration-*`、`bundle-barrel-imports` 中的 Next.js `optimizePackageImports` 方案

---

## 一、结论摘要：按收益排序

| # | 问题 | 位置 | 收益 | 规则 |
|---|---|---|---|---|
| 1 | `highlight.js` 默认入口把全部 384 种语言打进主 chunk | `Markdown.tsx:3`、`FilePreview.tsx:3` | **−1,017 KB（−56.8%）**，gzip −290 KB（实测） | `bundle-conditional` |
| 2 | 渲染路径里的**同步 IPC**：asset URL 无缓存 + `sendSync` | `api/context.ts:593-608` → `main/preload.ts:147-148` | 列表 N 行 = N 次主线程阻塞 → 0；含图消息每个 delta 都触发 | `js-cache-function-results` |
| 3 | `MessageBubble` 未 memo，流式每个 delta 重渲染整个消息列表 | `MessageBubble.tsx:51,295` | 每帧渲染量 O(N 条消息) → O(1)，**一行 `React.memo`** | `rerender-memo` |
| 4 | 全应用单 chunk，零路由级代码分割 | `App.tsx:31-40`、`vite.config.ts:29-32` | 首屏 1,792 KB → 拆页后 −150~200 KB | `bundle-dynamic-imports` |
| 5 | 流式内容无批处理：每个 SSE delta 直接写 store | `store/chatStore.ts:453`（无 rAF/timer 合并） | 渲染次数直接降 N 倍（与 #3 叠加） | `rerender-use-deferred-value` |
| 6 | 流式气泡每 delta 全文重解析 Markdown + 每个代码块重高亮 | `Markdown.tsx:230,21-34` | 高亮次数从 T×k 降到 ≈k | `js-cache-function-results` |
| 7 | 全项目 0 个 `React.memo` 边界 | 92 个文件 | 任意父级状态变化 → 整棵子树重渲染 | `rerender-memo` |
| 8 | `publicDir` 把 6.54 MB 遗留 Web 控制台资源整包复制进桌面端 | `vite.config.ts:28` | `dist/renderer` 8.25 MB → ~1.95 MB | —（打包体积） |
| 9 | 为 2 个图标引入整个 FontAwesome（阻塞 CSS + 字体） | `index.html:10`、`StatusScreen.tsx:116,124` | 关键路径 CSS −102 KB、字体 −150 KB | `bundle-conditional` |
| 10 | 6 处 zustand 整店订阅（无 selector，v5 无相等层） | `App.tsx:49`、`SessionList.tsx:163` 等 | 任意字段变化 → 组件整树重渲染 | `rerender-defer-reads` |
| 11 | prop → state 镜像 effect 冲掉用户未保存输入 | `AgentsPage.tsx:365-370` | 修数据丢失 bug + 每轮少 3 次 setState | `rerender-derived-state-no-effect` |
| 12 | 搜索输入同步驱动全量过滤 + 整树重渲染 | `KnowledgePage.tsx:601-612,996-1034` | 输入延迟从可感知降到无感 | `rerender-use-deferred-value` |
| 13 | 每 delta 一次同步强制 reflow（写 `scrollTop`） | `ChatPage.tsx:190-233,329` | 30 token/s 下主要掉帧来源 | `client-passive-event-listeners` |
| 14 | 执行历史分页无限累积 + 行 JSX 不可 memo | `TasksPage.tsx:470-483,557` | 30→200 行成本不再线性恶化 | `rendering-content-visibility` |
| 15 | 连接期 3 秒轮询无差别替换 4 个状态数组 | `ChannelsPage.tsx:98-138` | 消除连接期每 3 秒整列表重渲染 + 表单被重置 | `rerender-dependencies` |
| 16 | 页面级 `busy`/`statusMap` 使一次保存 4~5 轮整页渲染 | `settings/ModelsTab.tsx:92-131` | 整页 10 卡片树 ×5 轮 → 1 个卡片 | `rerender-memo` |
| 17 | 全项目 0 处 `content-visibility`，无虚拟化 | 全量 | 长列表首帧只布局视口内条目（规则口径 ~10×） | `rendering-content-visibility` |
| 18 | Markdown 渲染器与第二语言字典可延后加载 | `Markdown.tsx:2-3`、`i18n.ts:1-1854` | boot 路径再 −145 KB(markdown-it) −52 KB(hljs core) −42 KB(zh) | `bundle-conditional` |

**建议顺序**：#1 + #2（体积与主线程阻塞，改动都很小）→ #3 + #5 + #6（聊天热路径，用户直接可感）→ #4 + #8 + #9（打包交付层）→ #7 打底 → 再按页面处理 #10~#16 → 最后 #17 + #18。

**实施后的量化目标**：首屏 JS `1,792 KB → ~570–620 KB`；再叠加 #18 的按需加载，boot 路径可到 `~380–450 KB（−75%）`；CSS 侧独立再省 102 KB 关键路径、运行时少拉 150 KB 字体；安装包 renderer 资源 `8.25 MB → ~1.95 MB`。分包后的真实数值需实施后重跑一次构建对比。

---

## 二、P0：打包层

### 2.1 `highlight.js` 全量语言包 = 主 chunk 的 60%

**证据链**

1. 本次真实构建（`npm run build:renderer`）：

```
✓ 2141 modules transformed.
dist/renderer/assets/index-BUrjvxBb.css      56.81 kB │ gzip:  11.86 kB
dist/renderer/assets/index-DAkZIQ3k.js    1,792.44 kB │ gzip: 562.93 kB
(!) Some chunks are larger than 500 kB after minification.
```

2. 产物中确实含全部语言 —— 直接 grep 冷门语言名：`brainfuck` 2 处、`zephir` 1 处、`mathematica` 3 处、`haskell` 2 处、`elixir` 2 处、`fortran` 3 处；`registerLanguage` 在产物中出现 194 次。

3. esbuild 实测（`--bundle --minify`，两次独立测量一致）：

| 入口 | minified | deflate |
|---|---|---|
| `highlight.js/lib/index.js`（默认入口，注册 384 种语言） | **1,054.7 KB**（1,080,488 B） | **304.5 KB** |
| `lib/core.js` + bash/go/java/javascript/python | **37.5 KB** | **13.9 KB** |
| `lib/core.js` + 8 种常用语言 | 51.6 KB | — |
| `markdown-it`（对照） | 144.8 KB | 51.1 KB |

4. 引用点仅 2 处，均为默认入口：`Markdown.tsx:3`、`FilePreview.tsx:3`。

**关键旁证：网页端本来就只注册 5 种语言。** `channel/web/static/vendor/highlightjs/languages/` 下只有 5 个文件：`bash`、`go`、`java`、`javascript`、`python`。桌面端按同样 5 种注册是**行为对齐**而非功能削减。

**修复**：新建 `renderer/src/lib/hljs.ts`，用 `highlight.js/lib/core` + 语言模块 `registerLanguage`，两处引用改为指向它。

**收益**：主 chunk `1,792 KB → 约 775 KB`（**−1,017 KB / −56.8%**），gzip `563 KB → 约 273 KB`。

**注意（行为变更点）**：`Markdown.tsx:30` 在未知语言时走 `hljs.highlightAuto(str)`；该 API 的设计前提就是"注册了全部语言"，注册集缩小后自动探测质量会下降。建议直接删掉该兜底改为纯文本转义（未标注语言的代码块高亮价值低、代价高，且每次调用都会扫描全部已注册语法）；若确实需要，另补 3~5 个高频语言（每种 2~6 KB）。

### 2.2 全应用单 chunk，零代码分割

- `App.tsx:31-40` 静态 import 全部 10 个页面；构建产物只有 **1 个 JS chunk**。
- 全项目仅 1 处动态 import（`store/agentStore.ts:209`，且是静态字符串路径，可分析），而 Vite 明确警告它无效：

```
sessionStore.ts is dynamically imported by store/agentStore.ts
but also statically imported by ... dynamic import will not move module into another chunk.
```

- 全项目 `React.lazy` / `Suspense` 命中 **0**；`product` 模块内同样 0。
- `vite.config.ts:29-32` 的 `build` 仅有 `outDir`/`emptyOutDir`：无 `rollupOptions.manualChunks`、无 `chunkSizeWarningLimit`、无 `assetsInlineLimit`。

**修复**：App 路由里对 9 个非默认页面改 `React.lazy(() => import('./pages/XPage'))`，一层 `<Suspense>` 包住 `<Routes>`（`file://` 下 fallback 只需极简骨架）；`product.routes`（`App.tsx:390-392`）由构建期产物提供，保持静态。再配 `manualChunks` 显式声明 `vendor-react` / `vendor-markdown` / `vendor-hljs`（Electron `file://` 下分包对缓存无收益，价值在于限定启动必须解析的字节数），并把 `chunkSizeWarningLimit` 设成真实预算（如 700 KB）让回归报警。

**收益**：单页实测 27.3–90.8 KB（含共享本地模块，属上界），去重后首屏 −150~200 KB，同时消除 500 kB 警告的根因。

### 2.3 `publicDir` 把 6.54 MB 遗留资源整包塞进桌面端

`vite.config.ts:28`：`publicDir: path.resolve(__dirname, '../channel/web/static')`

| 目录 | 文件数 | 体积 |
|---|---|---|
| `dist/renderer` 全量 | 154~155 | **8.25 MB** |
| `dist/renderer/assets`（`index.html` 真正加载的） | 2 | 1.76 MB |
| `channel/web/static`（被整包复制的源） | 152 | 6.54 MB |

被复制进桌面端但 React 应用与主进程**零引用**的部分（对 `desktop/src` 全量 grep 命中 0）：

```
js/          3,540.8 KB（96 文件，含 console.js 1,165,078 B）
login-bg.jpg 1,255,372 B（2752×1536）      css/console.css 232,012 B
vendor/tailwind 407,279 B                  vendor/d3 279,706 B
vendor/highlightjs 121,727 B               vendor/markdown-it 103,012 B
axios.min.js 26,580 B                      logos/ 35.4 KB   avatars/ 3 KB   fragments/ 5.2 KB
```

主进程只 `loadFile(rendererHtml)`（`main/index.ts:382,388`），远程模式只 `loadURL(origin)`（`container.ts:116`、`content-window.ts:94`）；旧控制台由 Python 自己从 `channel/web/static` 提供（`channel/web/core/template.py:37`、`channel/web/api/pages.py:60`）。

**修复**：新建 `desktop/src/renderer/public`，只放 `vendor/fonts/inter/`、`vendor/fontawesome/`（或仅所需 css+woff2）、`logo.jpg`、`rongda-ai-mark.svg`，`publicDir` 指向它。（不能直接设 `false`：`index.html:9-10` 依赖 `./vendor/*`；且 `package.json:59-73` 的 `extraResources` 只带 `resources/`。）

**收益**：renderer 资源 8.25 MB → ~1.95 MB（−6.3 MB），并省掉每次构建复制 152 个文件。**落地前建议冒烟**：远程页 / 知识库 / 日志页确认无 404。

### 2.4 为 2 个图标引入整个 FontAwesome

`index.html:10` 引入 `vendor/fontawesome/css/all.min.css`（**102,025 B，render-blocking**），全源码只有 2 个使用点：

- `StatusScreen.tsx:116` `<i className="fas fa-rotate-right text-xs" />`
- `StatusScreen.tsx:124` `<i className="fas fa-folder-open text-xs" />`

（另 2 处 `fa-` 命中均为注释。）并因此拉取 `fa-solid-900.woff2` 150,124 B（`vendor/fontawesome/webfonts` 共 287.7 KB 随包发出）。

**修复**：换成项目已在 53 处使用的 `lucide-react` 的 `RotateCw` / `FolderOpen`（122 个图标合计仅 43.3 KB），删掉该 `<link>`。

**收益**：关键路径 CSS −102 KB（在 1.79 MB JS 之外的独立收益，且除掉首屏前一个阻塞样式表），运行时字体 −150 KB。`inter-latin.woff2` 48,256 B 是全站字体，保留。

### 2.5 i18n 双语同模块

`i18n.ts` 97.7 KB / 1888 行（`zh` 2-924、`en` 925-1855），被 **55 个文件**静态 import。实测：整体 minify 后 **105,784 B**，仅 zh 时 62,793 B —— **第二语言约 42 KB 常驻首屏**（≈2.4% chunk）。

关键细节：`i18n.ts:1869-1871` 的 `t()` 在缺失时回退 `translations['en']`，**所以 en 必须同步常驻，应拆走的是 zh**。

**修复**：en 留作同步基线，`zh` 拆成 `i18n.zh.ts`，在 `getLang()`/`setLang()` 时 `await import()` 并把字典放进一个可变引用；`setLang` 改 async，切换后触发一次刷新（`App.tsx:220` 的 `handleLangChange` 已是这个模式）。收益首屏 −42 KB，真正价值是为后续按 namespace 拆分铺路。

### 2.6 资源流水线

- **`assets/logo.png`（45,727 B，1024×1024）是死资源且扩展名错标**：其 SHA256 与 `channel/web/static/logo.jpg` 完全相同（后者实测也是 PNG、1024×1024、同字节数），renderer 源码无引用（仅 `AgentAvatar.tsx:74` 一行注释；产物 `dist/assets` 只有 1 js + 1 css，证明未被打包）。真正在用的是 `./logo.jpg`（`AgentAvatar.tsx:91`，走 publicDir 运行时 URL，不经 Vite），同一张 1024² 图被当 28×28（`MessageBubble.tsx:170`）与 64×64（`ChatPage.tsx:343`）头像用。**修复**：删重复文件、出头像 64/128@2x 变体（或改用已内联的 `rongda-ai-mark.svg`，产物内 `data:image/svg` 命中 1 次说明内联生效）、纠正扩展名。收益：仓库 −45.7 KB，头像请求 45.7 KB → ~4 KB，并省掉每个头像实例一次 1024² 解码。
- **无体积度量手段**：`package.json` 无 `rollup-plugin-visualizer` / `vite-bundle-visualizer` / `size-limit`。建议加 visualizer，让 1.03 MB 这类回归不再静默通过（当前只触发一条被忽略的 500 kB 警告）。

---

## 三、P0：渲染路径里的同步 IPC（最易被漏掉）

**链路（已逐环节读源码核实）**

```
JSX 里调用 url 助手
  → api/client.ts:333-342  getFileUrl / getServeFileUrl → this.assetUrl(...)
  → api/context.ts:593-608  assetUrl() → window.electronAPI?.desktopAssetUrlSync({path, kind})   // 同步
  → main/preload.ts:147-148 ipcRenderer.sendSync('desktop-asset-url-sync', req)                  // 阻塞渲染线程
  → main/auth-broker.ts:1295 ipcMain.on(...) → asset-proxy.ts:110-120 mint()
                           每次 randomBytes(24) + 遍历整个 assets Map 做过期清扫
```

**为什么严重**：`sendSync` 会**阻塞渲染线程直到主进程回包**，而调用点全在 JSX / 渲染路径里，且**零缓存**（全项目 25 处调用，其中 12 处在渲染路径）：

```
components/AgentAvatar.tsx:105   src={apiClient.agentAvatarUrl(agent.id, ...)}        // 每行头像
components/MessageBubble.tsx:98-99, 222
components/ChatInput.tsx:798     src={apiClient.getFileUrl(att.preview_url)}          // 每个附件
components/FilePreview.tsx:106
components/Markdown.tsx:135, 137, 157   token.attrSet('src', apiClient.getFileUrl(src))  // 在 md.render 内部，按图片数触发
```

最后一行是关键放大器：这些调用发生在 `md.render` 的 image 规则里，而 `md.render` 在流式期间每个 delta 都重跑 —— **一条含 N 张图的回复，流式期间会发起 N × delta 次同步 IPC**。

**修复**：在 `ApiClient`/`desktopContext` 内加 `Map<path|kind, url>` 缓存（tenant 切换 / logout 的 epoch 变化时清空），或在数据层预解析 URL 存进消息与列表项。

**收益**：一次列表渲染 N 行 = N 次主线程阻塞 → 0 次；会话列表、附件、含图消息的滚动与流式卡顿基本消失。

---

## 四、P0：聊天流式热路径

这是用户体感最强的路径。一次 SSE delta 的完整代价链：

1. `chatStore.ts:453` — `case 'delta': updateMsg(sid, speaking(), (m) => ({ ...m, content: m.content + (data.content || '') }))`，**每个 delta 直接调一次**；`tailTimer`（343/411/677）只用于流结束延迟，`followTimers`（83/925）用于后续消息，**都不是渲染批处理**。
2. → `patchMessages`（330-334）新建 `sessions` 对象 + 目标 session 对象。
3. → `ChatPage.tsx:82` `useChatStore((s) => s.sessions[activeId])` 返回新引用 → **ChatPage 每 delta 重渲染**。
4. → `ChatPage.tsx:377` `messages.map(...)` → 每个 `MessageBubble` 全量重渲染（无 memo）。
5. → 流式气泡 `Markdown.tsx:230` 全文重解析，每个代码块重新 `hljs.highlight`，每张图再一次同步 IPC（见第三节）。
6. → `ChatPage.tsx:190-233` effect 每 delta 强制 reflow + 写 `scrollTop`。
7. → `ChatInput`（`forwardRef`，未 memo，内部 5 个未 memo 子组件）每 delta 重渲染。

**根因是第 1 步**：delta 频率即渲染频率。**修 #3（memo）+ #5（批处理）两条的收益远大于其它所有渲染优化之和。**

### 4.1 `MessageBubble` 加 memo —— 一行改动，最高 ROI

`MessageBubble.tsx:51` 是裸函数组件，`:295` 直接 `export default MessageBubble`；父级 `ChatPage.tsx:377` 每次渲染都重建全部子元素。

**一行就够，因为前置条件全部满足（已核实）**：

- `chatStore.ts:337` — `msgs.map((m) => (m.id === id ? fn(m) : m))` 只为目标消息建新对象，**其余消息引用不变**；
- `ChatPage.tsx:271-293` — `handleRegenerate` / `handleEdit` / `handleDelete` / `handleMediaLoad` 全部 `useCallback`，`activeId` 不变时身份稳定。

修复：`export default React.memo(MessageBubble)`。

**收益**：每 delta 渲染量从 O(N 条消息) 降到 O(1)。100 条消息的会话约减少 99% 的 delta 期渲染工作。

### 4.2 `Markdown.tsx` —— 高亮结果无缓存

`Markdown.tsx:230` `useMemo(() => md.render(content || '', { imageBaseDir }), [content, imageBaseDir])`：流式时 `content` 每 delta 变化 → 整篇重解析；`:21-24` 的 highlight 回调对**每个** fence 重新执行；`:30` 未知语言落到 `highlightAuto`。

**修复**：

- (a) 模块级 `Map` 以 `${lang}\u0000${code}` 为键缓存 highlight 结果 → 已完成的代码块在后续 delta 全部命中，只有正在增长的末块重算；
- (b) 去掉 `highlightAuto` 兜底（见 2.1）；
- (c) 流式期间 `useDeferredValue(content)` 把重解析降为低优先级。

**收益**：含 k 个代码块、T 个 delta 的回复，highlight 次数从 T×k 降到 ≈k。这是流式卡顿与首字延迟的主因之一。

### 4.3 `ChatPage.tsx:190-233, 329` —— 每 delta 一次同步强制 reflow

effect 依赖 `[messages, activeId, isStreaming, scrollToBottom]`（`:233`），体内读 `el.scrollHeight / scrollTop / clientHeight`（`:215`）并写 `el.scrollTop = el.scrollHeight`（`:171`）；滚动容器用内联 `onScroll={handleScroll}`（`:329`），处理函数每次读布局（`:300`）且无 rAF 合并。

对照 `ChatTimeline.tsx:109-116` 做了 rAF 合并、`:128` 用了 `{ passive: true }` —— **那才是本项目里的正确写法，可照抄**。

**修复**：原生 `addEventListener('scroll', fn, { passive: true })` + rAF 内只读一次布局；auto-follow 依赖"最后一条消息的 id/长度"而非整个 `messages` 数组；`ChatPage.tsx:94` 的兜底 `session?.messages ?? []` 提为模块常量（当前无 session 时每次渲染都是新数组，会白跑 effect）。

**收益**：大 DOM 下每 delta 省一次 1~5 ms+ 同步 reflow；30 token/s 时这是主要掉帧来源。

### 4.4 `ChatInput` 加 memo

`ChatInput.tsx:59` 是 `forwardRef<...>(function ChatInput(...))`，**forwardRef 不是 memo**。内部挂载 `ContextUsagePopover`（894-902，自身 6 个 `useState` + `useSyncExternalStore` + 两处 `LEGEND.map`）、`WorkspaceSelector`（917）、`ModelSelector`（923）、`AgentSelector`（928）及 4 个 `Tooltip`。

**做得对的地方**：输入文本是本地 state（`:66`），键击不上传父级；window drag 监听用 `dropHandlerRef`（600-601）保持单次绑定。**问题在反向传导**：ChatPage 每 delta 重渲染 → 整棵 composer 子树重建。

**修复**：`const ChatInput = memo(forwardRef<...>(function ChatInput(...)))`。props 中 `onSend`/`onNewChat`/`onStop`/`onClearContext`/`onAdjustContext` 均稳定，`isStreaming`/`sessionId` 是原始值，memo 可直接生效。

### 4.5 长消息列表无 `content-visibility`

`ChatPage.tsx:377-409` 的消息项只是 `<div key={msg.id} data-user-seq=...>`（393-396），无离屏跳过；全项目 `content-visibility` / `contain` / `will-change` 命中 **0**。

`ChatTimeline.tsx:95-100` 在滚动中对**全部**气泡逐个 `getBoundingClientRect()`（先 `querySelectorAll('[data-user-seq]')` 再线性扫描），`:298` 展开面板时一次性渲染全部提问行。

**修复**：在 `index.css` 加 `.cv-auto { content-visibility: auto; contain-intrinsic-size: auto 80px }`，挂到消息项、会话行、日志行、文件树行；`updateActive` 改为维护 seq→元素 Map 或只测视口附近气泡。**收益**：长会话/长日志首帧只布局视口内条目（规则口径 ~10×），滚动测量从 O(N) 降到 O(1)。

### 4.6 事件扫描与自愈循环

- `chatStore.ts:401-408` `updateCard`（被 `tool_progress` 524-531、`tool_end` 533-552、`subagent_step` 559-587 调用）每个事件都 `msgs.map()` × `(m.steps || []).some(...)` 扫描全部消息 × 全部步骤。**修复**：模块级 `Map<tool_call_id, messageId>`（`tool_start` 写入、气泡收尾清理），O(消息数×步骤数) → O(1) 定位 + O(步骤数) 更新。
- `chatStore.ts:729-735` 掉线自愈每 2s 最多 30 次 `loadHistory(sid, 1)`，`924-931` 未结束回复每 3s 重读 page 1；而每次成功都在 `:909` `flatMap(historyToMessages)` 全量重建消息对象（全列表换新对象，**memo 也救不了**）。**修复**：与 4.2 一起做增量合并（按 `_seq` 复用已有消息对象）+ 指数退避 + 窗口隐藏时暂停。

---

## 五、P0/P1：页面级渲染与状态

### 5.1 零 `React.memo` 边界

92 个文件、29,401 行中 `React.memo` 出现 **0 次**；`useMemo` 仅 42 处（对 381 处 `useState`）。也未启用 React Compiler（`package.json` 无 `babel-plugin-react-compiler`，`vite.config.ts:25` 只有 `react()`）—— "不需要手动 memo"的豁免不成立。

`useState ≥ 8` 的文件全部没有 memo 边界：

| 文件 | useState | useEffect | useMemo | useCallback | memo |
|---|---|---|---|---|---|
| `pages/TasksPage.tsx` | 35 | 7 | 2 | 3 | 0 |
| `pages/AgentsPage.tsx` | 33 | 11 | 4 | 0 | 0 |
| `pages/settings/ModelsTab.tsx` | 32 | 5 | 4 | 2 | 0 |
| `pages/KnowledgePage.tsx` | 25 | 4 | 3 | 12 | 0 |
| `pages/settings/BasicSettings.tsx` | 24 | 5 | 0 | 0 | 0 |
| `pages/ChannelsPage.tsx` | 22 | 7 | 3 | 0 | 0 |
| `components/ChatInput.tsx` | 15 | 9 | 2 | 2 | 0 |

**含义**：这些页面里任意 `setState`（含输入框按键、轮询回调）都重渲染整棵子树。**顺序必须是：先抽子组件 + `React.memo` + 稳定回调，再谈其它微优化。**

### 5.2 zustand 整店订阅（6 处）

```
components/UpdateBanner.tsx:22    const state = useUpdateStore()
components/WorkspacePanel.tsx:46  const { open, tab, width, current, previewError, edit, editNotice } = useWorkspaceStore()
layout/NavRail.tsx:87             const { navCollapsed, toggleNav } = useUIStore()
layout/NavRail.tsx:109            const updateState = useUpdateStore()
layout/SessionList.tsx:163        } = useSessionStore()          // 12 个字段
App.tsx:49                        const { sessionsCollapsed, toggleSessions, navCollapsed } = useUIStore()
```

已在依赖源码中核实：`zustand/react.js:7-12`（v5.0.14）用 `useSyncExternalStore(api.subscribe, () => selector(api.getState()))`，**没有相等层**，默认 selector 是 identity —— 所以 `useXxxStore()` 等价于"订阅一切"。

最尖锐的是下载进度：`updateStore.ts:79` 每次 `percent` 变化都让 NavRail 与整壳重渲染，而 `NavRail.tsx:109-115` 只用到 `hasPendingUpdate` / `hasAvailableUpdate` / `status.state`；`App.tsx:49` 则让整壳在任何 UI 字段（哪怕与 App 无关）变化时重建。

**修复**：改逐字段 selector（`useUIStore((s) => s.navCollapsed)`）；多字段用 zustand 5 的 `useShallow`；action 用 `useStore.getState()` 在回调里取；NavRail 只订派生布尔。

**已验证的正面案例（不要误改）**：`App.tsx:50-53` 已用 `useWorkspaceStore((s) => s.togglePanel)` 正确形式；`SessionList.tsx:190-194` 的 `buildGroups` 已 `useMemo` 包裹，其内部 `groups.sort()`（`:112`）排的是本地构造数组，不污染 store；`sessionStore.ts:110` `sortSessions` 已 `[...list].sort` 拷贝，只在 action（261/270）中调用。

### 5.3 `KnowledgePage.tsx` —— 搜索输入同步驱动全量计算

`search` 由输入框直接驱动（`601-606`）后原样下传：`612` `search={search.trim().toLowerCase()}` → 根文件过滤（`996-1007`）→ 每个 `DirNode` 递归 `countMatches`（`1013-1018`）→ 又对同一批 files 再 `filter` 求 `visibleFiles`（`1029,1032-1034`）；`1015`/`1033` 每次比较都对 `title`/`name` 调 `.toLowerCase()`。全程无 `useDeferredValue` / `startTransition` / `useMemo` / `memo`（全项目这三者命中 0）。

**修复**：`useDeferredValue(search)`；一次性构建小写 haystack 索引 + `Set<matchedPath>` + `Map<dir, matchCount>`（`useMemo` 依赖 `[data, dq]`），让 `DirNode` 只做 O(1) 查表；两趟 filter 合并为一趟；`DirNode`/`FileLeaf` 加 `memo` 且只传原始值 props。另 `:750` 从 props 初始化 state 且无 key reset；`:267-285` 的 `refresh` 把 `activePath` 放进依赖导致身份频繁失效，连带 `createCategory`/`createDocument`/`importDocuments` 全部重建。

### 5.4 `TasksPage.tsx:470-483,557` —— 执行历史无限累积

```ts
const page = await apiClient.getSchedulerRunPage('', PAGE_SIZE, runs.length)   // 474 用 runs.length 当偏移量（陈旧闭包读法）
setRuns((prev) => [...prev, ...page.runs])                                     // 475
...
{runs.map((run) => {                                                           // 557 整张卡片 JSX 内联展开 565-630
```

每行都调 `findAgent(run.agent_id)`（`agentStore.ts:246` 是 `getState().agents.find(...)` 线性扫描），并重算 `formatDuration`/`formatRunTime`；行 JSX 内联在 `map` 内无法 memo。

**修复**：抽 `RunRow` 并 `memo`；`useMemo` 建 `Map<agentId, Agent>`；行容器加 `content-visibility: auto; contain-intrinsic-size: auto 96px`；偏移量改 ref/cursor。另 `:974` 是恒等 `useMemo(() => recipients, [recipients])`（零收益纯开销），`:978-986` 用 effect 校准 `recipientId` 属派生状态，`:245-249` 切 Tab 会卸载 `RecordsView` 导致已加载的多页历史全丢并重取第 0 页。

### 5.5 `AgentsPage.tsx:365-370` —— prop → state 镜像 effect（会丢用户输入）

```ts
useEffect(() => setName(agent.name || ''), [agent.name])                     // 365
useEffect(() => setDescription(agent.description || ''), [agent.description]) // 366
useEffect(() => setModelKey(agent.model ? `${agent.bot_type || ''}|${agent.model}` : ''), [agent.model, agent.bot_type])  // 367-370
```

`agentStore.mutate` 成功后必 `await get().refresh()`（`store/agentStore.ts:201`），而 `refresh` 用全新数组 + 全新对象 `set({ agents })`（137-140、172-179）。于是同页任何 mutation（切知识库模式、勾技能）→ refresh → 三个 effect 重跑 → **用户正在输入但未保存的姓名/描述/模型被服务端值覆盖**。既是性能问题，也是数据丢失 bug。

**修复**：只在 `agent.id` 变化时初始化（`useRef` 记已初始化 id + 本地 draft），保存成功后由 `save()` 回写；或对详情面板用 `key={agent.id}` 做 keyed reset。

### 5.6 `ChannelsPage.tsx:98-138` —— 轮询无差别替换状态

**核实修正**：轮询是**有条件的** —— 仅当有渠道处于 settling 时每 3 秒一次（`settling` 判定 130-132，`useEffect` 133-138），不是常驻轮询。但每次 tick 都用 4 个全新数组覆盖 state（98-117），即使 payload 一致也会让 `connected` 的 `useMemo`（140-162，含 filter+slice+sort）重算、`available` 换身份、每张 `ChannelCard` 重渲染，并连带 `ChannelBinding` 的 `initial`（792-795）与 `useEffect(() => setValue(initial), [initial])`（799-801）重跑，**把用户正在填的表单重置**。

**修复**：tick 先做签名比较（`name + active + login_status + instance_id`）再 `set`；reseed 依赖改原始值（`channel.agent_id`、`(channel.members || []).join(',')`）。另 `loadChannels`（`:98`）每次渲染重建并作为 `onChanged` 下发（263/296/725/757），应 `useCallback`；`121-125`/`133-138`/`161-162` 三处 `eslint-disable exhaustive-deps` 把 `loadChannels`/`isMultiInstanceType` 摘出依赖，`125` 还把函数调用 `getLang()` 写进依赖数组。

### 5.7 `ModelsTab.tsx:92-131` —— 一次保存 4~5 轮整页渲染

`busy`/`statusMap` 挂在页面根（92-113），`flash`/`run` 每次渲染重建，向下传给 6 个 `CapabilityCard`（单个 227 行、无 memo）+ `TtsCard` + `VendorSection`。附带 `:94` 的 `setTimeout` 未清理，切走设置页后仍会 `setState`。

**修复**：`busy`/`status` 下沉到卡片；`run`/`flash` 用 `useCallback` + 函数式 `setState` 固定身份；卡片加 `memo`，`onSave` 收敛为稳定的 `(capKey, provider, model)` 签名。

### 5.8 其它 P1

| 位置 | 问题 | 规则 |
|---|---|---|
| `settings/ModelsTab.tsx:756-766`（+748-752、787、797） | `TtsCard` 每次渲染重建 3 个选项数组 + 非惰性 `useState` 初始化式（内部 `providers.find` 每渲染执行）；`mode === 'off'` 未挂载时也照算 | `rerender-lazy-state-init`、`js-index-maps` |
| `pages/AgentsPage.tsx:100-108, 743, 776-803` | `agents` 每次 refresh 都是新数组 + 无 memo → roster 全部卡片与技能库整列重渲染；`picked` 的 `useMemo` 依赖 `agent.skills` 新数组，每次刷新重建 Set | `rerender-memo` |
| `store/workspaceStore.ts:177-181` | 拖动分栏时每次 `mousemove` 同步 `localStorage.setItem` + 全店 set（调用点 `WorkspacePanel.tsx:69` 无节流） | `rerender-use-ref-transient-values`、`js-cache-storage` |
| `api/context.ts:380-381` | 启动门两个独立 IPC 串行 await（`desktopAuthProbe` 后接 `desktopAuthStatus`） | `async-parallel` |
| `components/FilePreview.tsx:68-70` | `hljs.getLanguage` + `hljs.highlight` 直接写在渲染路径，无 `useMemo` | `rerender-memo` |
| `components/ChatInput.tsx:128,340,450,503` 等 | 大量内部闭包以 props 形式下传，是 memo 化前必须先收敛的依赖 | `rerender-memo` |

---

## 六、P2 汇总

### 6.1 渲染路径重复计算

| 位置 | 问题 | 规则 |
|---|---|---|
| `TasksPage.tsx:974` | 恒等 `useMemo`，零收益纯开销 | `rerender-simple-expression-in-memo` |
| `TasksPage.tsx:978-986` | effect 校准 `recipientId`（派生状态）；`:983` 每次全量 `scoped.map` | `rerender-derived-state-no-effect` |
| `TasksPage.tsx:1032-1036, 1057-1061` | `instanceOptions`/`recipientOptions` 每次渲染 `.map` 新建数组 props | `rerender-memo` |
| `AgentsPage.tsx:49-54`、`ChannelsPage.tsx:165-167` | effect 维护"选中项失效则重选/清空"（派生状态） | `rerender-derived-state-no-effect` |
| `ChatInput.tsx:322-325` | 草稿写透用 effect，每次击键调度一次 effect | `rerender-move-effect-to-event` |
| `ChatInput.tsx:624-626` | `slashIndex` 用 state+effect 夹紧 | `rerender-derived-state-no-effect` |
| `ChatInput.tsx:268-291` | 每渲染重建 20 条命令数组 + 20 次 `t()` + filter（菜单关闭时也执行） | `rerender-memo` |
| `ChatPage.tsx:92`（+55-75） | `useSuggestionCards()` 每渲染重建 6 个对象 + 12 次 `t()`，每 delta 都跑 | `js-cache-function-results` |
| `ModelsTab.tsx:893-924` | 每次调用新建 6 元素数组做 `includes`；`configured`/`missing` 每渲染重新 filter | `js-set-map-lookups` |
| `ChannelsPage.tsx:183-194`（调用点 294） | 渲染期对 `fields` 深拷贝 + spread 整个 channel | `rerender-memo` |
| `AgentsPage.tsx:230-234, 511-517, 652-655` | 每渲染重建 `tabs` 数组与行内 options 字面量（对照 `:562` `CORE_FILES` 已提到模块级） | `rendering-hoist-jsx` |
| `AgentsPage.tsx:858-863` | `slug` 定义在组件内，每次调用重建 2 个正则字面量 | `js-hoist-regexp` |
| `chatStore.ts:103-113, 146` | 每条历史消息至少重建 2 次 `/_\(Cancelled…\)/` 正则；发送路径为每个 label `new RegExp` | `js-hoist-regexp` |
| `ModelsTab.tsx:268-272` | `VendorSection` 接收 `statusMap`/`flash` 但函数体不解构，属死 props | `rerender-memo` |
| `hooks/useSchedulerNotifyPoll.ts:141` | 轮询回调里 `runs.slice().sort(...)` | `js-min-max-loop` |
| `SessionList.tsx:346-352` | 侧栏内联 `onScroll`，每次事件读 3 个布局属性，无 rAF 合并 | `client-passive-event-listeners` |
| `SessionList.tsx:275-280` | effect 以 `sessions` 数组为依赖，无关刷新也会 `scrollIntoView` 打断浏览位置 | `rerender-dependencies` |
| `store/agentStore.ts:249-252` | selector 返回无 memo 的派生数组 `s.agents.filter(...)`（当前无组件使用，属未爆的地雷） | `rerender-derived-state` |

### 6.2 异步与瀑布

| 位置 | 问题 | 规则 |
|---|---|---|
| `ModelsTab.tsx:477-479` | 保存厂商时 3 个请求串行，前两个互相独立 | `async-parallel` |
| `AgentsPage.tsx:873-884` | `mutate` → `uploadAgentAvatar` → `refresh()` 串行，末次 refresh 只为刷新头像却阻塞弹窗关闭（`onCreated` 在 `:890`） | `async-defer-await` |
| `api/client.ts:566-578` | multipart 组装逐文件串行 `await value.arrayBuffer()`（5 文件约可省 60% 准备时间） | `async-parallel` |
| `hooks/useSchedulerNotifyPoll.ts:21`、`hooks/usePushPoll.ts:11-12` | 两个常驻轮询固定间隔且**无 visibility 退避**（后者注释明说 "Keep polling while hidden"）；状态通知已逐字段去重，问题只在节奏 | `js-request-idle-callback` |

### 6.3 key / 清理 / 存储 / CSS

- **`key={index}` 家族 21 处**，其中 **`settings/ModelCatalogEditor.tsx:204` 是真缺陷**：该行内部是受控 `TextInput` + `removeRow(idx)`（206-221），删除中间行时 React 会复用错位的 DOM/焦点。`ChatInput.tsx:793-794` 的附件列表同样支持中间删除（`removeAttachment(i)`），改用 `att.file_path + att.file_name`。其余（`LogsPage.tsx:129`、`MessageSteps.tsx:249-254`、`FileTree.tsx:126`、`OnboardingWizard.tsx:145`、`SessionList.tsx:590` 等）多为只读或只追加列表，影响低。
- **定时器未清理**：`TasksPage.tsx:996`、`KnowledgePage.tsx:226-232`（`statusTimer` 只在下次 `showStatus` 时清理，卸载无 `clearTimeout`）、`ModelsTab.tsx:94`、`ChannelsPage.tsx:619/623`、`ChatInput.tsx:93`（`mentionTimerRef` 的 160 ms 搜索定时器不在卸载 effect 清理范围内，卸载后可能仍发一次 workspace 搜索请求）。
- **`localStorage` 35 处**均无内存缓存（`js-cache-storage`）；`i18n.ts:1866`、`sessionStore.ts:95` 的读取在模块加载期。另 `sessionOwners.ts:37-44, 67-78` 每次分页加载都整表 `JSON.stringify` 最多 400 条（写放大 O(400) → O(变化数) 可优化）。
- **`index.css:224-230` 的 `body * { transition-property: background-color, border-color, color, fill, stroke; … }`**：全树每个元素常驻过渡，主题/壁纸切换时上千节点同时过渡（样式重算 + 重绘抖动）；`prefers-reduced-motion` 仅在 232-240 兜底。建议收敛到主题根节点或少数面板容器，或切换瞬间挂 `.theme-transition` 类 250 ms 后移除。另 3 处 `backdrop-filter`（`index.css:193,210,220`）无合成层提示。
- **`useState` 非惰性初始化**：`McpEditorModal.tsx:85`（`toForm(null)`）、`ModelsTab.tsx:748` 应改 `useState(() => ...)`；`OnboardingWizard.tsx:30` 的 `getLang()` 是纯内存读，可忽略。

---

## 七、明确"不是问题"的项（避免误改）

| 检查项 | 结论 |
|---|---|
| 事件监听泄漏 | **无泄漏**：19 个文件的 `addEventListener` 与 `removeEventListener` 数量全部 1:1 配对（含 `ChatInput` 的 5 个 window drag 事件、`App.tsx` 的 2 个）；`hooks/` 目录 5 个文件的监听器注册全部有清理 |
| `passive` 用法 | `ChatTimeline.tsx:128` 的 scroll 已 `{ passive: true }`；`KnowledgeGraph.tsx:283` 因需 `preventDefault` 显式 `passive: false`（正确）；`ChatInput` 的 drag 监听需 `preventDefault`，不应加 passive；其余监听目标为 `keydown`/`mousedown`/`click`，不需要 |
| barrel / `import *` | 全项目 `import * as` 命中 **0**，无 `from '../components'` 形式；动态 import 仅 1 处且为静态字符串（满足 `bundle-analyzable-paths`） |
| `markdown-it` / `lucide-react` | `markdown-it` 仅 `Markdown.tsx:2` 引用，`:230` 已 `useMemo(md.render)` 包裹（模块级单例 + CJK/greedy-link/fence 补丁都在模块作用域，写法正确）；`lucide-react` 53 条具名导入 / 122 图标 / 43.3 KB，tree-shaking 有效（全库 `import *` 会是 731.5 KB），**不要**改深层导入路径 |
| `index.html` 脚本 | `<script type="module">` 即 defer（满足 `rendering-script-defer-async`）；`:11-25` 内联主题脚本是防闪必需（保留） |
| 组件内定义组件 | 全项目 `.tsx` **大小写敏感**扫描命中 **0**（`IconBtn`、`HoverAction`、`CORE_FILES` 等均已提到模块级） |
| 消息列表 key | 用的是稳定 id：`ChatPage.tsx:379,394 key={msg.id}`、`ChatTimeline.tsx:300 key={it.seq}`、`SessionList.tsx:474 key={s.session_id}` |
| Context Provider | 项目未用 React Context（`createContext` 命中 0），全部走 zustand + `useSyncExternalStore` |
| 虚拟化 | 未安装也未使用 windowing 库（`package.json` 无 `react-window`/`react-virtualized`/`react-virtuoso`），所以"未虚拟化"是设计现状而非回归 |
| store 引用稳定性 | `chatStore.ts:337` 只为目标消息建新对象，其余引用不变 —— **这是 memo 能生效的前提，加 memo 前不需要改 store** |
| 轮询状态粒度 | `useSchedulerNotifyPoll.ts:50-65` 的 `patchNotifyState` 逐字段比较后去重、`getSchedulerNotifyState` 返回稳定引用，App 侧 `useSyncExternalStore` 不会额外抖动（该文件的问题只在节奏，见 6.2） |
| 其它已核对项 | `useTheme` 已用 `Promise.all`；capabilities 有 in-flight 去重；`sessionOwners` 有内存缓存；`SessionList.tsx:223-230` 的 `expandSpace` 在无变化时返回原引用 |

---

## 八、附：审计方法与依赖体积清单

### 8.1 实测命令

```powershell
# 1) 真实构建，取 chunk 体积
cd desktop; npm run build:renderer
#   → assets/index-*.js  1,792.44 kB │ gzip: 562.93 kB   (2141 modules)

# 2) 确认全量语言包进了产物
Select-String -Path dist/renderer/assets/index-*.js -Pattern brainfuck,zephir,mathematica

# 3) 单依赖体积实测（esbuild bundle + minify + deflate）
esbuild node_modules/highlight.js/lib/index.js --bundle --minify --format=esm
#   → 1054.7 KB min / 304.5 KB deflate
esbuild <core + bash/go/java/js/python>.mjs --bundle --minify --format=esm
#   → 37.5 KB min / 13.9 KB deflate
```

### 8.2 依赖体积清单（esbuild 单模块实测，minified）

| 模块 | 体积 | 说明 |
|---|---|---|
| `highlight.js` 主入口（全语言） | **1,080,488 B** | 占主 chunk 60% |
| `hljs core + 8 常用语言` | 52,866 B | 修复方案对照 |
| `markdown-it`（含 linkify-it/mdurl） | 148,229 B | 仅首条 markdown 消息才需要 |
| `react + react-dom + react-router-dom` | 160,223 B | 不可压缩基线 |
| `i18n.ts` 全量（zh+en） | 105,784 B | zh-only 62,793 B → 第二语言 ≈42 KB |
| `lucide-react` 实际使用的 122 图标 | 44,380 B | tree-shaking 有效 |
| `zustand` | 8,013 B | |
| **合计** | **≈1,547 KB** | 与 1,792 KB 主 chunk 自洽（余 ~245 KB 为业务代码） |

### 8.3 方法与局限

- **静态扫描**：`Select-String` 全量遍历 `desktop/src/renderer/src`（92 个 `.ts/.tsx`）与 `desktop/src/main`，统计 hook 密度、集合链式调用、事件监听配对、`key={index}`、`React.memo`/`content-visibility`/`useDeferredValue` 命中数。
- **深读**：`ChatPage`、`ChatInput`、`MessageBubble`、`Markdown`、`ChatTimeline`、`SessionList`、`store/` 全部 13 个文件、`api/client.ts`、`api/context.ts`、`hooks/` 全部、`TasksPage`、`KnowledgePage`、`AgentsPage`、`ChannelsPage`、`ModelsTab` 等 30+ 个文件逐行阅读，每条结论附真实行号与原文。
- **交叉核实**：关键结论均回读源码二次确认，并修正了 1 处夸大（ChannelsPage 轮询是 settling 期有条件轮询，非常驻）。
- **局限**：未运行应用、未做 CPU profile / React DevTools Profiler。标注"实测"的是构建与打包工具测得的确定值，其余为规则给出的量级估算。若要精确排序，下一步应在 Electron 内对"流式回复"与"知识库搜索"两个场景各录一次 Profiler。
