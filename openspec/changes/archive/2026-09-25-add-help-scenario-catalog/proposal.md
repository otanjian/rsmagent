## Why

帮助站目前只有「首页 / 核心能力 / 企业级管控 / 架构 / 关于」五个入口，讲的都是产品能做什么，缺少一层「用在哪、做完长什么样」的落地示例：访客读完能力列表仍然要自己去想「这套东西在我的岗位上对应哪件事」。

已发布的 `https://www.workbuddy.link/p/ZljbYQzFpALFLcPwKPlct1`（WorkBuddy 实践案例与客户指南）已经沉淀了一批**演示数据与技术准备都做好、点一下就能跑完**的场景（页内标识为「一键体验」，共 63 个），每个场景带任务提示词与技能包。把这批场景作为帮助站的「应用场景」页收录进来，访客可以直接对着自己的岗位挑一个跑一遍，而不只是读能力清单。

## What Changes

- **新增帮助站页面 `/help/scenarios`（应用场景）**：按领域分组列出全部支持一键体验的场景，每张卡片给出场景名称、所属领域、岗位、痛点说明、场景说明与数据替换提示，并提供两个动作：
  - 「一键体验」：按源站口径用 `workbuddy://task?action=start&prompt=<场景提示词>` 唤起外部客户端并自动带入该场景的完整任务提示词；
  - 「查看做法」：打开源站对应的方案详情页（站外链接）。
- **新增导航入口**：顶部导航在「企业级管控」与「架构」之间插入「应用场景」，中英双语。
- **新增场景数据文件 `webhelp/scenarios.json`**：随站点发布，记录快照来源、抓取日期、深链协议模板、分组与 63 条场景（名称、领域、岗位、痛点、场景说明、数据替换提示、提示词、做法页地址）。页面不再硬编码场景清单。
- **内嵌提示词**：场景提示词以页面内 JSON 数据块随页面下发，点击时由前端组装深链——避免把 URL 编码后的提示词写进 63 个 `href`（体积与转义风险）。
- **站外链接白名单**：站点此前是「页面内所有链接都指向 `/help/`」。本变更首次引入站外链接，改为按 `config.json` 声明的「协议 + 主机」白名单校验，未登记的站外地址在校验与测试中失败。
- **新增离线校验 `webhelp/tools/check_scenarios.py`**：核对场景数据字段完整性、分组覆盖、深链模板与做法页地址是否落在白名单内、页面渲染条目数与数据文件一致。

**BREAKING**：无。后端路由、鉴权与数据不变；仅新增一个公开只读页面与一个静态资源。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `product-manual-site`: 该能力覆盖帮助站本身。新增「应用场景」页面与导航入口、场景目录数据契约、一键体验的深链行为、站外链接白名单，以及场景页的双语与离线渲染要求。

## Impact

- **站点代码**：`webhelp/site.py`（`PAGES` 增加 `scenarios`、新增场景渲染辅助方法）、`webhelp/templates/scenarios.html`（新增）、`webhelp/config.json`（`nav` 增加条目、新增站外链接白名单）、`webhelp/scenarios.json`（新增数据）、`webhelp/lang/zh.json` 与 `webhelp/lang/en.json`（导航、标题与页面文案键）。
- **前端资源**：`webhelp/assets/css/style.css`（场景卡片、角标、动作按钮）、`webhelp/assets/js/main.js`（一键体验点击时组装深链）。
- **校验与测试**：新增 `webhelp/tools/check_scenarios.py`；`tests/test_help_site.py` 的「所有链接都指向 `/help/`」不变量改为「站内 + 白名单站外」，并覆盖新页面在两语言下的渲染。
- **打包**：`webhelp/scenarios.json` 需随桌面安装包一同发布（沿用 `desktop/build/cowagent-backend.spec` 既有的 webhelp 资源收集，缺项则补 spec）。
- **不改**：既有五个帮助页与全部能力文档正文、`channel/web/help_site.py` 的路由与静态资源白名单（`.json` 仍不对外暴露）、`webhelp/tools/check_manual.py` 的手册校验口径。
