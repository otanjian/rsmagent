## Context

帮助站是与主控制台同进程、经 `/help/` 提供的 Python + web.py 站点（见 `webhelp/README.md`）。与本次相关的既有约束：

- **路由白名单**：`webhelp/site.py::PAGES` 决定哪些页面可达，`channel/web/help_site.py` 只放行 `PAGES` 与 `assets/` 下的静态资源；`config.json` / `content.json` / `lang/*.json` 等 JSON 不对外暴露。
- **内容与代码分离**：页面结构在 `templates/*.html`，结构数据在 `content.json`，双语在 `lang/zh.json` 与 `lang/en.json`；模板默认转义，`$:` 只用于视图已转义的组件输出。
- **站点自包含**：`openspec/specs/product-manual-site` 要求手册页不引用站外资源；`tests/test_help_site.py::test_all_pages_and_documents_in_both_languages` 进一步把「页面里每个 `href`/`src` 都以 `/help/` 开头」钉成了不变量。
- **来源快照已经存在**：`https://www.workbuddy.link/p/ZljbYQzFpALFLcPwKPlct1` 是发布产物，页内 `ONECLICK` 对象有 63 条场景（名称、场景说明、数据替换提示、任务提示词），页内 `SOLUTIONS` 数组给出对应卡片的标题、行业、岗位与痛点，两者按 `src` 的文件名 slug + 所属库节点配对；源站的一键体验是 `workbuddy://task?action=start&prompt=<提示词>`。

## Goals / Non-Goals

**Goals:**

- 把 63 条场景作为帮助站的一个可独立访问页面提供，导航入口落在「企业级管控」与「架构」之间。
- 场景清单、提示词、深链协议只有一个来源（数据文件），模板与脚本不重复登记。
- 站点首次引入站外链接后，链接边界仍是可校验、可审计的白名单，而不是「任意 URL 都行」。
- 保持站点既有形态：JSON 承载内容、双语键、无构建步骤、无站外请求。

**Non-Goals:**

- 不把场景抄进 `openspec/specs/` 之外的运行时（不新增后端接口、不写库、不接 `Scene/` 的场景目录）。
- 不复刻源站的行业/岗位筛选 chips 与搜索框（本次只做分组目录）。
- 不翻译场景正文，也不改写正文里的产品名——正文按快照原文呈现。
- 不改变既有五个帮助页、能力文档与手册校验口径。

## Decisions

### D1 场景数据放独立文件 `webhelp/scenarios.json`

结构：`source`（快照来源页）、`captured_at`（抓取日期）、`deep_link`（如 `workbuddy://task?action=start&prompt=`）、`groups`（`id` + 文案键名 + 顺序）、`items`（每条含 `slug`、`name`、`group`、`industry`、`roles`、`pain`、`desc`、`case_hint`、`prompt`、`detail_url`）。

理由：既有惯例是 JSON 承载内容；独立文件才能自带来源与抓取日期，并让离线校验只读一处。

**备选**：写进 `content.json`（把外部快照与产品自身结构数据混在一起，且无法在文件级声明来源）；写进模板（改一条场景要动模板，校验无从下手）。

### D2 提示词以页面内嵌 JSON 下发，点击时由前端组装深链

63 段提示词合计约 5.6 万字符；URL 编码后写进 63 个 `href` 会放大到数倍并引入转义面。改为在页面内放置一个 `application/json` 数据块（只含 `slug → prompt`），`assets/js/main.js` 在点击 `[data-scenario-try]` 时用 `encodeURIComponent` 组装地址并触发跳转——与源站做法一致（源站也是运行时拼 `workbuddy://` 并创建临时 `<a>` 点击）。

**备选**：`href` 直写深链（体积与转义）；独立 JSON 资源按需拉取（需要给静态资源白名单放行 `.json`，并引入一次站内请求，收益为 0）。

### D3 站外白名单落在 `config.json`

新增 `external_links`：`schemes`（如 `workbuddy`）与 `hosts`（如 `www.workbuddy.link`、`workbuddy-space-static.codebuddy.work`）。测试与 `check_scenarios.py` 读同一份配置。

**备选**：写在 `scenarios.json`（把站点策略与内容快照混为一谈）；在测试里硬编码（两份真相，改配置要改两处）。

### D4 分组沿用源站的场景库边界（21 / 15 / 12 / 15）

源站四条场景库分别对应制造与工业、人事、财务、销售与商务；按库分组比平铺 63 张卡片可读，也比复刻行业/岗位筛选更小。组名用站点文案键（`scenarios.groups.*`）承载双语。

### D5 卡片字段取舍

卡片给出：名称、领域与岗位标签、痛点（`pain`）。`desc` 与 `caseHint` 放进 `<details>` 展开区——两者在 63 条中高度重复（仅技能名不同），平铺会淹没有效信息；展开形态复用站点既有的 `details.faq-item` 模式。提示词不进正文，只进 D2 的数据块与「查看做法」外链。

### D6 双语边界：界面文案双语，场景正文保持原语言

场景正文来自外部快照，翻译会与来源脱节且无法校验。`lang/zh.json` 与 `lang/en.json` 补齐导航、标题、分组、动作与说明文案键；正文由数据文件原样输出。

### D7 站点链接不变量从「全部站内」改为「站内 + 白名单站外」

`tests/test_help_site.py` 的既有断言（每个 `href`/`src` 以 `/help/` 开头）显式放宽为「站内，或 `config.json` 白名单命中的站外地址」，并断言白名单之外的站外地址不被渲染。放宽而不是删除，保留「页面不出现计划外目标」的强度。

### D8 样式沿用站点 design token

一键体验角标沿用源站的语义色（绿色）与圆角形态，但用站点现有 CSS 变量实现；卡片复用 `.feature-card` 家族的间距与描边，保证与其它帮助页一致。

## Risks / Trade-offs

- **快照会过期**（来源页改版、场景增删）→ 数据文件登记 `captured_at` 与来源地址，README 记录重抓方式；离线校验只做结构核对，不联网比对。
- **深链依赖外部客户端**：未安装客户端时点击无反应 → 卡片同时提供「查看做法」站外链接作为兜底；文档说明一键体验需要客户端。
- **提示词含外部安装命令** → 页面不执行任何内容，仅在用户显式点击后把提示词交给外部客户端；站点自身不发起请求。
- **内嵌 JSON 让页面增大约 75KB** → 站点对该页已是 `private, no-cache`，体积可接受；若后续显著变大，再改为独立 JSON 资源（届时需放行 `.json` 静态类型）。
- **内嵌 JSON 的转义面** → 序列化时把 `<` 转义为 `\u003c`，离线校验断言提示词与说明不含裸 `</`。

## Migration Plan

无数据迁移。新增页面与数据文件随站点发布即可生效；桌面端需重新构建以带上新的 `scenarios.json`（沿用既有 webhelp 资源收集）。回滚 = 移除导航条目、模板、数据文件与校验脚本，旧地址自然 404。

## Open Questions

无。一键体验的目标行为（与源站一致的客户端深链 + 做法页链接）、页面视觉风格（沿用帮助站现有风格）与导航位置（「企业级管控」与「架构」之间）已在开工前与使用者确认。
