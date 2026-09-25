## Context

`/help/scenarios` 已经把 63 个场景列出来了，卡片上的「查看做法」指向站外的 WorkBuddy 发布页。本变更要把那批内容搬到站内。

动手前对来源站做了全量抓取与结构统计（189 个真实页面，另有 63 个不存在的路径变体返回 404）：

| 页族 | 页数 | 不同 CSS 类 | 普遍复用（≥80% 页出现） | 每页独有（≤20%） | 平均正文 |
| --- | --- | --- | --- | --- | --- |
| 场景引入 | 63 | 106 | 28 | 71 | 5.2 KB |
| 推荐任务 | 63 | 20 | 20 | 0 | 4.3 KB |
| Live Demo | 63 | 236 | 25 | 188 | 14.2 KB |

其他关键事实：

- **零图片**，`<img>` 总数 0；只有两份共享样式表（`shared/style.css` 21 KB、`shared/discrete-extra.css` 73 KB）和一个平台注入脚本 `/page/page_comm/inject.js`（可丢弃）。
- **站外链接只出现在场景引入页**：170 条，落在 42 页的痛点「依据 ·」锚点上，涉及 60 个域名。推荐任务与 Live Demo 是 0 条。
- 那 170 条锚点的**可见文字里不含网址串**（最短 28 字、中位 73 字、最长 169 字），是描述性出处说明而非裸链接。
- Live Demo 的 188 个每页独有类集中在逐步演示正文（表格、KPI、判定结论、思考过程），是各页定制的数据展示。

现有接入点：

- `webhelp/site.py` 的 `HelpView`：`PAGES` frozenset 决定可访问页面；`doc_*` 系列方法（`doc_exists` / `doc_title` / `doc_url` / `doc_body` / `doc_toc` / `doc_grouped`）已经从 `webhelp/docs/<slug>.html` + `docs/manifest.json` 渲染出「正文 + 右侧分组导航」的文档页形态，可作为本次的骨架。
- `channel/web/help_site.py` 的 `HelpSiteHandler.GET`：先看 `assets/` 前缀，再要求 `page in PAGES`，否则 `web.notfound()`。
- `webhelp/tools/check_scenarios.py`：既有的离线校验形态（读快照 + 读 `config.json` 白名单 + 比对渲染结果），本次沿用这一形态。

## Goals / Non-Goals

**Goals**

- 189 个来源页的正文内容全部落到站内，可离线浏览。
- 用帮助站自己的组件与 design token 渲染，与站点其他页面风格一致。
- 内容零丢失且可验证：提供「文本不丢」不变量，抽取或渲染漏内容时校验失败。
- 内容唯一来源是一份受审阅的快照文件，模板与脚本不硬编码正文。

**Non-Goals**

- 不搬运来源站的样式表、标记语言或平台注入脚本。
- 不保留痛点「依据 ·」的超链接（按决策去链接、留文字）。
- 不引入任何站外链接与站外资源；不修改 `add-help-scenario-catalog` 建立的站外白名单。
- 不为场景正文提供英文翻译（与 `scenarios.json` 同策略：界面文案双语、正文保持原语言）。
- 不追求像素级复刻来源站的视觉，只保证内容完整与站点风格一致。

## Decisions

### D1 快照放单文件 `webhelp/scenario_docs.json`

与 `scenarios.json` 同形态：一个受审阅的开发产物，由抽取脚本生成、随站点发布。按 63 个 slug 分组，每个 slug 下含 `intro` / `tasks` / `demo` 三份文档，便于校验与按需读取。

取舍：备选是「每场景一个分片文件」（`webhelp/docs/scenarios/<slug>.json`），加载更省内存但校验与审阅要跨 63 个文件。单文件在可校验性上更好；实测体积在数 MB 量级，`site.py` 已有 `lru_cache` 读全量 JSON 的先例（`scenarios.json` 199 KB），可接受。若实测超过 5 MB，退回分片方案。

### D2 归约为内容原语，未知结构降级而不丢弃

把来源的三套标记语言归约为 12 个原语：

| 原语 | 承载来源 | 渲染 |
| --- | --- | --- |
| `prose` | `card` / `p` / `subline` | 段落 |
| `pains` | `pains` / `pain` / `i` / `t` / `src` | 编号痛点条目，出处作尾注 |
| `compare` | `cmp` / `old` / `new` / `list` | 双栏对照 |
| `cards` | `g2` / `g3` / `val4` / `disp4` / `grade5` / `cost4` / `cause4` / `verdict-grid` / `action-grid` / `lv-grid` / `lv3` / `entry3` | 带色调的标题卡网格，可带 `kicker` 与 `meta` |
| `steps` | `flow` / `step` / `n` | 步骤列表 |
| `demo_steps` | `dstep` / `dstep-n` / `dstep-h` / `dstep-b` / `sd` | 演示步骤卡，正文递归为上述原语 |
| `outputs` | `outs` / `out` / `on` / `ot` / `od` | 产物清单 |
| `files` | `src` / `fn` / `fd` | 材料清单 |
| `score` | `card ev` | 评分与说明 |
| `table` | `tbl` / `compare-t` / `audit-tbl` | 表格 |
| `chips` | `std-strip` / `items` / `tag` / `kpi` | 标签条 |
| `notes` | `callout` / `notice` / `warn` / `think` / `prompt` / `cmt` / `io` | 提示/警示块 |

取舍：Live Demo 有 188 个每页独有类（占 236 的 80%），逐页枚举不现实。因此抽取器按「已知结构 → 原语」映射，**映射不到的容器保留其内部文本与内联强调，渲染为中性块**。这条规则保证任何未预料的结构都只是「样式朴素」，不会变成「内容消失」。

### D3 内联标记白名单 + 转义

正文里存在内联强调（`<b>`）与出处锚点。快照只允许保留 `b` / `strong` / `br` 三个标签；`a` 标签在抽取阶段就被拆成纯文字（见 D4）。渲染时对白名单外的标签一律去标签留文字，其余字符按站点既有方式转义。序列化内嵌数据时把 `<` 转义为 `\u003c`，与 `add-help-scenario-catalog` 的既有做法一致。

### D4 痛点出处去链接、只留文字

170 条「依据 ·」出处按决策渲染为纯文字。依据：来源锚点的可见文字本身不含网址串（已核对 170 条），去掉链接不损失可见信息，同时让新内容不含任何站外链接——站外白名单无需扩容，页面也不会因外网点不开而出现「点了没反应」的死链。

### D5 三个本地页面共用一套外壳

`/help/scenario/<slug>`、`/help/scenario/<slug>/tasks`、`/help/scenario/<slug>/demo` 共用「面包屑 + 页头 + 正文 + 右侧 63 场景分组导航 + 动作条」外壳，形态对齐既有 `doc.html`。动作条提供「一键体验」（复用 `add-help-scenario-catalog` 的内嵌提示词 + 深链机制）与「返回场景列表」。来源页原有的三个子导航入口改为本地三页互跳。

取舍：备选是复用 `doc` 页面（`/help/doc?p=<slug>`）承载。不采用的原因：`doc` 的内容来自 `docs/manifest.json` 的手册目录，混入 63 个场景会让手册导航与校验口径变复杂；且场景文档需要按 slug 区分三个子页，路径形态比查询参数更合适。

### D6 路由放行 `scenario/<slug>[/tasks|/demo]`

`HelpSiteHandler.GET` 在 `page not in PAGES` 之前增加一条：路径匹配 `scenario/<slug>` 或 `scenario/<slug>/(tasks|demo)` 时，把 `page` 归一到 `scenario_doc` 并解析出 `slug` 与 `kind`。slug 用 `[a-z0-9-]+` 严格校验，任何不匹配的形态继续 404。静态资源白名单与「`.json` 不对外暴露」的边界不变。

### D7 站点外壳文案双语，正文单语

`/help/scenarios` 已确立这一边界。本变更同样只补外壳键（面包屑、三页标题与标签、动作按钮、导航标题），正文保持快照原语言。

### D8 文本不丢是硬校验，不是人工保证

`check_scenario_docs.py` 对每个 slug 的每份文档比对两组文本：来源页可见文本（抽取自抓取到的 HTML）与渲染后可见文本。比对前做受控归一（折叠空白、剥离出处括号、统一标点）。任何一处来源文本在渲染结果中找不到，校验以非零状态退出并指出 slug、文档类型与缺失片段。

这条不变量是本变更敢对 236 个类的页族做归约的前提：它把「归约是否丢内容」从人工审阅变成可重复的机械判定。

## Risks / Trade-offs

- **Live Demo 归约失败导致内容缺失**（最高风险，188/236 类为每页独有）。缓解：D2 的「未知结构降级保留」+ D8 的文本不丢校验；两者缺一不可。
- **快照体积膨胀**。189 页原始 HTML 约 11 MB，抽取后预计 1.5–3 MB。缓解：D1 已预留分片退回路径；实测超 5 MB 即改分片。
- **来源站改版导致重抓口径失效**。缓解：沿用 `capture_scenarios.py` 的立场——重抓是一次人工审阅的步骤，抽取脚本随变更一起入库，快照带 `captured_at`。
- **文本不丢校验的归一规则过松**会漏掉真丢内容，**过严**会把合法改写判成失败。取舍：归一规则集中在抽取脚本的一处函数里，并在测试中用「人为删一段」的反例钉住它确实会失败。
- **中性块与站点风格不一致**。缓解：中性块只用站点已有的排版 token（边框、圆角、次级文字色），不做额外装饰，观感上是「朴素段落」而非「另一个设计系统」。

## Migration Plan

1. 合入前置变更 `add-help-scenario-catalog`（提供 `scenarios.json` 与站外白名单）。
2. 入库抽取脚本与 `scenario_docs.json`。
3. 路由放行 + 渲染 + 样式 + 文案。
4. 校验与测试全绿后，把 `/help/scenarios` 卡片的「查看做法」由站外地址改为本地 `/help/scenario/<slug>`。
5. 桌面打包补 `scenario_docs.json`。

回滚：本变更不触碰既有页面与数据结构，回滚只需还原上述改动；`/help/scenarios` 的「查看做法」在回滚后重新指向站外即可。

## Open Questions

- 快照是否需要随来源站定期重抓并在站点显示 `captured_at`？当前决定：与 `scenarios.json` 一致，只在数据文件里记录，不在页面展示。
- 中性块是否需要进一步细分为更具体的原语（例如把 Live Demo 的判定结论单独建模）？当前决定：先按 12 个原语落地，待文本不丢校验通过后用实际观感决定是否细化。
