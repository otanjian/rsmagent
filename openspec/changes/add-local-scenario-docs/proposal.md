## Why

`add-help-scenario-catalog` 把 63 个场景收进了帮助站，但每个场景卡片上的「查看做法」仍指向站外的 WorkBuddy 发布页。这带来两个问题：

- **离线不可用**：桌面安装包与内网部署都会随站点发布 webhelp，但来源站不可达时，访客点开「查看做法」只能看到浏览器错误页。站点其余部分一直是自包含的。
- **内容未沉淀**：真正有价值的是「查看做法」背后的方案正文（现状、痛点与出处、做法对比、场景闭环、会产出什么、不承诺什么），以及配套的推荐任务与 Live Demo 逐步演示。这些内容目前完全不在站内。

来源站上这三类内容共 **189 个页面**（63 场景引入 + 63 推荐任务 + 63 Live Demo），零图片、零外部脚本，只有两份共享样式表。本变更把这批内容本地化，并用帮助站自己的组件渲染，使场景文档与站点其他页面风格一致、可离线、可校验。

## What Changes

- **新增场景文档数据 `webhelp/scenario_docs.json`**：把 189 个来源页解析成结构化文档快照。快照是唯一内容来源，模板与前端脚本 MUST NOT 硬编码正文。
- **归约标记语言**：来源页共出现 106（场景引入）+ 20（推荐任务）+ 236（Live Demo）个 CSS 类。本变更把它们归约为少量内容原语（段落、痛点、做法对比、色调卡片网格、步骤、演示步骤、产物、材料清单、评分、表格、标签条、提示/警示），未知结构降级为中性容器而**不丢弃**。
- **新增三个本地页面**：
  - `/help/scenario/<slug>` —— 场景引入（原「查看做法」正文）
  - `/help/scenario/<slug>/tasks` —— 推荐任务
  - `/help/scenario/<slug>/demo` —— Live Demo 逐步演示
- **路由放行**：`channel/web/help_site.py` 目前要求路径整体命中 `PAGES`，`scenario/<slug>` 会被判 404。本变更放行该路径形态并保持既有白名单边界（`.json`、`templates`、`tools` 仍不对外暴露）。
- **站外引用去链接**：痛点里的 170 条「依据 ·」出处保留完整文字，MUST NOT 渲染超链接（正文本身不含网址串，去掉链接不丢可见信息）。新内容因此**不含任何站外链接与站外资源**，站外白名单保持 `add-help-scenario-catalog` 的现状不变。
- **子导航本地化**：来源页的「场景引入 / Live Demo / 推荐任务」三个站外入口改为本地三页互跳，并补「一键体验」与「返回场景列表」两个动作。
- **新增离线校验 `webhelp/tools/check_scenario_docs.py`**：结构完整性、slug 与 `scenarios.json` 对齐、原语类型合法、内联标签白名单、无站外链接与资源，以及**文本不丢不变量**（逐页比对来源文本与渲染文本，缺一即失败）。

**BREAKING**：无。仍是公开只读页面；后端路由、鉴权与数据不变。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `product-manual-site`: 该能力覆盖帮助站本身。新增「场景文档本地化」的页面路由、内容快照契约、原语渲染、站外引用去链接、子导航本地化与文本不丢校验要求。

## Impact

- **站点代码**：`webhelp/site.py`（`PAGES` 增加 `scenario_doc`、新增原语渲染与文档读取方法）、`webhelp/templates/scenario_doc.html`（新增）、`webhelp/scenario_docs.json`（新增数据）、`webhelp/lang/zh.json` 与 `webhelp/lang/en.json`（外壳文案键）。
- **路由**：`channel/web/help_site.py`（放行 `scenario/<slug>[/tasks|/demo]`），`channel/web/route_registry.py` 的 `/help/(.*)` 路由条目无需改动。
- **前端资源**：`webhelp/assets/css/style.css`（12 个内容原语的样式）。
- **校验与测试**：新增 `webhelp/tools/check_scenario_docs.py`、`openspec/changes/add-local-scenario-docs/evidence/extract_scenario_docs.py`（快照抽取脚本）；`tests/test_help_site.py` 覆盖三页渲染、路径边界与文本不丢。
- **打包**：`webhelp/scenario_docs.json` 需随桌面安装包发布（沿用 `desktop/build/cowagent-backend.spec` 既有的 webhelp 资源收集，缺项则补 spec）。
- **依赖**：本变更依赖 `add-help-scenario-catalog` 已落地的 `webhelp/scenarios.json`（slug 与分组对齐）与站外白名单机制；两者须先于本变更合入。
- **不改**：既有五个帮助页与能力文档正文、`/help/scenarios` 页面与卡片、`webhelp/tools/check_scenarios.py` 的既有校验口径、`webhelp/tools/check_manual.py`。
