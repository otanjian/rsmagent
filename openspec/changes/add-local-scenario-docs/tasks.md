## 1. 快照抽取（依赖 `add-help-scenario-catalog` 已合入）

- [x] 1.1 新增 `openspec/changes/add-local-scenario-docs/evidence/extract_scenario_docs.py`：遍历 `webhelp/scenarios.json` 的 63 个 slug，抓取场景引入（`<slug>.html`）、推荐任务（`<slug>-tasks.html`）与 Live Demo（先试 `live-demos/<slug>/index.html`，缺失再试 `live-demos/<slug>.html`），抓取失败 MUST 以非零状态退出并列出 slug
- [x] 1.2 在抽取脚本中实现脏标记剥离（注释、`data-page-node-id`、`data-pnid-children`、`<script>`）与「已知结构 → 原语」映射，未映射容器保留内部文字与内联强调并标记为中性块
- [x] 1.3 在抽取脚本中实现内联标记白名单（保留 `b` / `strong` / `br`）与锚点拆文字（`a` → 纯文字）
- [x] 1.4 输出 `webhelp/scenario_docs.json`：顶层登记 `source`、`extracted_at`（抓取日期）、`extractor`，另带 `catalog`（slug → 名称与分组）、`kinds` 与 `primitives`；按 slug 分组，每个 slug 含 `intro` / `tasks` / `demo` 三份文档与逐页 `text` 基准（供文本不丢校验使用）
- [x] 1.5 抽取脚本可就地打印统计：每页族页数、原语使用计数、中性块占比、快照体积；实测快照超过 5 MB 时改为按场景分片并同步更新 D1

## 2. 路由放行

- [x] 2.1 先写失败测试：`tests/test_help_site.py` 断言 `/help/scenario/<已登记 slug>` 返回 200、`/help/scenario/nope` 与非法的 `/help/scenario/<slug>/unknown` 返回 404
- [x] 2.2 `channel/web/help_site.py` 的 `HelpSiteHandler.GET` 放行 `scenario/<slug>[/tasks|/demo]`：slug 用 `[a-z0-9-]+` 严格校验，把 `page` 归一为 `scenario_doc` 并解析出 `slug` 与 `kind`；其余形态维持既有 404 行为
- [x] 2.3 确认静态资源白名单边界不变：`.json`、`templates`、`tools` 仍不对外暴露，`webhelp/scenario_docs.json` 不可通过 `assets/` 直接下载

## 3. 渲染与原语

- [x] 3.1 `webhelp/site.py`：`PAGES` 增加 `scenario_doc`；新增 `scenario_doc` 读取、`scenario_doc_title` / `scenario_doc_url` / `scenario_doc_neighbors` / `scenario_doc_grouped` 与 12 个原语的渲染方法，保持 `$:` 只输出视图已转义内容
- [x] 3.2 新增 `webhelp/templates/scenario_doc.html`：面包屑 + 页头 + 正文 + 右侧 63 场景分组导航 + 动作条（「一键体验」复用既有内嵌提示词数据块与深链机制、「返回场景列表」），来源页原三个子导航入口改为本地三页互跳
- [x] 3.3 页面 OK 状态与未命中 slug 的 404 呈现对齐既有 `doc.html` 的写法
- [x] 3.4 `webhelp/assets/css/style.css`：为 12 个原语补样式，取值只用站点既有 CSS 变量；中性块保持朴素排版，不引入第二套设计语言
- [x] 3.5 `webhelp/lang/zh.json` 与 `en.json`：补三份文档页面的面包屑、标题、标签、动作与导航文案键，两语言键集合一致
- [x] 3.6 侧栏「全部场景」标题接入 `scenario_doc.aside_title`（此前该键两语言下都无人消费），并在 `test_scenario_doc_pages_are_indexed_and_navigable` 里锁住中英文标题

## 4. 校验与测试

- [x] 4.1 新增 `webhelp/tools/check_scenario_docs.py`：核对快照元数据、三份文档齐全、原语类型合法、内联标签落在白名单内、slug 与 `scenarios.json` 一致、无站外链接与站外资源
- [x] 4.2 在同一脚本中实现**文本不丢不变量**：逐场景逐文档比对来源文本基准与渲染结果，缺失即非零退出并指出 slug、文档类型与缺失片段；归一规则集中在一处函数
- [x] 4.3 先写失败测试：`tests/test_help_site.py` 覆盖「人为删掉一段正文 → 校验非零退出并将归一规则钉住会失败」、「快照缺一份文档 → 校验非零」、「slug 集合不一致 → 校验非零」
- [x] 4.4 `tests/test_help_site.py` 补渲染断言：三份页面在两种语言下均 200、面包屑与右侧导航存在、出处引用为纯文字不含锚点、页面无站外链接与站外资源、正文不含内联标签白名单外的标签
- [x] 4.5 运行 `pytest tests/test_help_site.py tests/test_help_site_url.py tests/test_route_registry.py -q` 与 `python webhelp/tools/check_manual.py`、`check_scenarios.py`、`check_scenario_docs.py`，全部通过
- [x] 4.6 `/help/scenarios` 卡片的「查看做法」由站外地址改为本地 `/help/scenario/<slug>`，并同步更新 `check_scenarios.py` 中「做法页地址命中站外白名单」的既有断言与对应测试

## 5. 打包与文档

- [x] 5.1 `desktop/build/cowagent-backend.spec` 的 webhelp 资源收集补 `scenario_docs.json`，并保留既有「打包资源齐全」断言
- [x] 5.2 更新 `webhelp/README.md`：新增三个页面地址、目录表补 `scenario_docs.json` 与 `tools/check_scenario_docs.py`、记录快照来源与重抓方式、说明正文归约与文本不丢校验口径
- [x] 5.3 把 `webhelp/config.json` 的静态资源 `version` 由 `2.x` 提到 `2.1`：新 CSS 因 `?v=` 未变而命中 `max-age` 缓存，浏览器里 0 条 `sdoc` 规则；提版本后实测 97 条。口径写进 README 的「静态资源缓存」

## 6. 验收

- [x] 6.1 浏览器人工核对：三个页面（含中英文、深/浅主题、窄屏）的面包屑、右侧导航、原语渲染观感、出处引用为纯文字、一键体验组装出的地址
- [x] 6.2 断网下核对：63 个场景的三份页面全部完整渲染，无站外请求
- [x] 6.3 `openspec validate add-local-scenario-docs --strict` 通过

## 7. 遗留（写入 evidence.md §7）

- [ ] 7.1 `files`、`score`、`neutral` 三个原语在当前快照里 0 样本，渲染分支未被真实数据检验；来源页改版出现新容器时，第一个降级块会走 `neutral`
- [ ] 7.2 63 个场景只人工目视了 `ecn`；其余靠文本不丢不变量与 189 页 200 断言兜底，逐页排版观感未逐个查看
