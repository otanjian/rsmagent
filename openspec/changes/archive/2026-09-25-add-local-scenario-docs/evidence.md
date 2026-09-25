# 实施证据（add-local-scenario-docs）

本文件记录 tasks.md 各项要求的真实结果。命令均在仓库根目录执行，Python 一律用 `.venv/bin/python`。

## 1 规范

- `openspec validate add-local-scenario-docs --strict` → `Change 'add-local-scenario-docs' is valid`。

## 2 快照抽取（tasks 1.x）

抽取脚本：`openspec/changes/add-local-scenario-docs/evidence/extract_scenario_docs.py`。重抓命令：

```bash
python3 openspec/changes/add-local-scenario-docs/evidence/extract_scenario_docs.py \
    webhelp/scenarios.json webhelp/scenario_docs.json
```

产出 `webhelp/scenario_docs.json`（3.31 MB）的实测口径：

| 指标 | 值 |
| --- | --- |
| slug 数 | 63（与 `scenarios.json` 一致） |
| 文档数 | 189（63 × `intro` / `tasks` / `demo`），缺失 0 |
| 块数 | 1711 |
| 文本基准 | 27686 条片段 / 585189 字 |
| 顶层元数据 | `source`、`catalog`、`extracted_at`（2026-09-22）、`extractor`、`kinds`、`primitives`、`items` |

原语分布（登记 16 个，本次快照实际用到 13 个）：

| 原语 | 数量 | 占比 |
| --- | --- | --- |
| `heading` | 515 | 30.1% |
| `prose` | 256 | 15.0% |
| `cta` | 189 | 11.0% |
| `note` | 188 | 11.0% |
| `cards` | 139 | 8.1% |
| `demo_steps` | 106 | 6.2% |
| `pains` / `compare` / `steps` / `outputs` / `tasks` | 各 63 | 各 3.7% |
| `table` | 2 | 0.1% |
| `chips` | 1 | 0.1% |

`files`、`score`、`neutral` 三个原语在本次快照里 **0 次命中**：来源页的已知结构全部被映射成了具体原语，没有容器降级到中性块。这三条路径目前只有渲染实现与校验规则，没有真实样本（见 §7）。

## 3 路由与渲染（tasks 2.x / 3.x）

`channel/web/help_site.py` 放行 `scenario/<slug>[/tasks|/demo]` 后，对运行中的服务（`http://127.0.0.1:9899`）实测：

| 路径 | 状态 |
| --- | --- |
| `/help/scenario/ecn`、`/help/scenario/ecn/tasks`、`/help/scenario/ecn/demo` | 200 |
| `/help/scenario/ecn?lang=en` | 200 |
| `/help/scenario/nope`、`/help/scenario/ecn/unknown`、`/help/scenario/ECN` | 404 |
| `/help/scenario_doc`、`/help/scenario/` | 404 |
| `/help/scenarios.json`、`/help/assets/scenario_docs.json`、`/help/assets/../scenario_docs.json`、`/help/tools/check_scenario_docs.py` | 404 |

最后四行是 tasks 2.3 的边界：快照 JSON、`templates/`、`tools/` 都不通过 `assets/` 暴露，路径穿越同样被拦。

正文与外壳的实测（`curl` 抓页面后逐项核）：

- `?lang=zh` 页面 `<html lang="zh-CN">`、标题 `工程变更管理ECN · 场景引入 · 容大AI`；`?lang=en` 页面 `<html lang="en">`、标题 `工程变更管理ECN · Overview · 容大AI`。
- 外壳文案随语言切换（`场景引入/推荐任务/Live Demo/本页目录/返回场景列表` ↔ `Overview/Suggested tasks/Live Demo/On this page/Back to all scenarios`）。
- 两种语言下正文都保留来源页原语言：`变更分级凭感觉` 在英文页面里同样存在。

**修正**：`scenario_doc.aside_title`（`全部场景` / `All scenarios`）原本在模板与视图里都没有被消费，是一个没人用的语言键。这次把它接到 `scenario_doc_nav()` 的侧栏标题上（复用既有 `.doc-aside-title` 样式），并补了断言锁住它。

## 4 校验与测试（tasks 4.x / 5.1）

三个离线校验器全部通过：

```
$ .venv/bin/python webhelp/tools/check_manual.py
OK 手册结构、双语文案、截图与引用一致
$ .venv/bin/python webhelp/tools/check_scenarios.py
OK 场景快照、双语文案、白名单与页面渲染一致
$ .venv/bin/python webhelp/tools/check_scenario_docs.py
OK 场景详情快照完整、无外部依赖、渲染后不丢文本
```

帮助站测试：

```
$ .venv/bin/python -m pytest tests/test_help_site.py tests/test_help_site_url.py tests/test_route_registry.py -q
59 passed, 276 subtests passed in 8.67s
```

`tests/test_help_site.py` 覆盖：三页在中英文下均 200、未知 slug / 非法 kind / 大写 slug / 裸 `scenario_doc` 均 404、`/help/scenarios` 的 63 个「查看做法」全部指向本地地址且可见内容里不再出现来源站主机、右侧导航 63 条且当前项高亮、正文不含白名单外标签与站外链接、校验器对「缺文档 / 活跃内容 / 未知原语与 tone / 丢文本 / 缺外壳文案」五种坏快照都非零退出、HTML 净化器只留白名单标签。

**红/绿核验**：把侧栏标题断言喂给改动前的 `scenario_doc_nav()` 实现（临时替换该方法后重渲染真实页面），中英文标记均为 `False`（红）；当前实现下均为 `True`（绿），且 63 条侧栏链接数量不变——断言确实能区分这次改动，不是恒真。

## 5 打包与文档（tasks 5.x）

- `desktop/build/cowagent-backend.spec:166` 新增 `(rp('webhelp', 'scenario_docs.json'), 'webhelp')`；`tests/test_help_site.py` 的「打包资源齐全」断言同步纳入该文件。
- `webhelp/README.md`：访问清单补场景详情地址；目录表补 `scenario_docs.json` 与 `tools/check_scenario_docs.py`；「应用场景页」补「查看做法已本地化」与 `detail_url` 只作抓取记录；新增「场景详情页（本地快照）」与「静态资源缓存」两节；校验清单补第三个校验器；打包说明点名两个 JSON。

### 5.1 顺带修掉的缓存问题

`webhelp/config.json` 的 `version` 是静态资源的 `?v=` 戳，但它在 CSS 改动后没有变，响应又带 `max-age`，于是浏览器一直用旧 `style.css`：CDP 实测 `document.styleSheets` 里含 `sdoc` 的规则数是 **0**，页面上的新组件全是裸样式（`border-radius: 0px`、`background: rgba(0, 0, 0, 0)`），而 `curl` 直接取服务端文件能数到 106 处 `sdoc` 规则——服务端是对的，是缓存。把 `version` 由 `2.x` 改为 `2.1` 后重载，同一处实测变成 **97** 条 `sdoc` 规则，`.sdoc-heading` 的 `display: flex` / `gap: 10px`、`.sdoc-task` 的 `border-radius: 10px` / 卡片底色才真正生效。该口径已写进 README 的「静态资源缓存」一节：改 CSS/JS 必须同步改 `version`。

## 6 浏览器验收（tasks 6.1 / 6.2）

在运行中的服务上用 CDP 读取真实页面（未改页面逻辑）。截图见 `evidence/`：

| 文件 | 内容 |
| --- | --- |
| `scenario-intro-dark-full.png` | 场景引入页整页（深色） |
| `scenario-tasks-dark-full.png` | 推荐任务页整页（深色） |
| `scenario-demo-dark-full.png` | Live Demo 页整页（深色） |
| `scenario-intro-light.png` | 场景引入页（浅色） |
| `scenario-intro-mobile-390.png` | 场景引入页（390×844，移动端） |
| `scenarios-list-local-detail.png` | `/help/scenarios` 卡片区（查看做法已指向本地） |

- **自包含（6.2）**：三页各只加载 4 个同源资源（两张 CSS 与两段脚本），`performance.getEntriesByType('resource')` 里站外条目为 `0`；页面内 4 张图片 `currentSrc` 全部同源；`/help/scenarios` 可见内容里唯一的站外锚点是页面顶部的来源页链接（白名单内），正文引用已全部去掉锚点。
- **原语渲染**：引入页出 5 个二级标题与 1 张表（三级变更分级、FMEA 联动规则），任务页出 10 张任务卡与 10 个「复制」按钮（`data-sdoc-copy`），Demo 页出 9 个文档标题与 6 张表（验证计划 16 项、审批链、文件更新追踪等）。
- **窄屏**：390px 下 `document.documentElement.scrollWidth - innerWidth === 0`，无横向溢出，也没有任何元素右边界越出视口；最宽的表格（348px）容在正文（350px）内；导航收成汉堡菜单、侧栏 63 条收起，页面内子导航三页互跳仍在。

### 6.1 未能覆盖

- **英文页面的视觉核对只做了标记级**（`lang="en"`、`Overview`/`Suggested tasks` 等外壳文案存在、正文保持中文），没有逐页截图。
- **63 个场景只人工看了 `ecn` 一个**；其余 62 个靠 `check_scenario_docs.py` 的文本不丢不变量与 pytest 的 189 页 200 断言兜底，观感（某个原语在某页的排版）没有逐个目视。
- **`/tasks` 与 `/demo` 的浅色主题**没有单独截图（浅色只截了引入页）。

## 7 未覆盖与未决

- `files`、`score`、`neutral` 三个原语在本次快照里没有样本。渲染实现与校验规则都在，但**没有任何真实页面走过这三条分支**；将来来源页改版出现新容器时，第一个降级到 `neutral` 的块会走这条从未被真实数据检验过的路径。
- 来源页是人工复核的产物：脚本按既有结构映射，来源改版后需要重抓并重跑校验，而不是「抓一次就永久有效」。这一前提已写进 README。
- 本站正文按快照冻结，来源页更新不会自动反映到站内。
