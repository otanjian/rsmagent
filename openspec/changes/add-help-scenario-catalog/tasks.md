## 1. 场景快照与离线校验

- [x] 1.1 生成 `webhelp/scenarios.json`：登记 `source`、`captured_at`、`deep_link`（`workbuddy://task?action=start&prompt=`）与 `groups`（制造与工业 21 / 人事 15 / 财务 12 / 销售与商务 15），逐条写入 63 个场景的 `slug`、`name`、`group`、`industry`、`roles`、`pain`、`desc`、`case_hint`、`prompt`、`detail_url`，来源页的 `desc`/`caseHint`/`prompt` 逐字保留
- [x] 1.2 新增 `webhelp/tools/check_scenarios.py`：核对必填字段、`group` 引用有效、分组条目数与 `groups` 声明一致、`deep_link` 模板非空、`detail_url` 与 `source` 命中站外白名单、提示词与说明不含裸 `</`
- [x] 1.3 在同一个校验脚本中比对「页面渲染出的场景条目数」与「数据文件条目数」，不一致时以非零状态报告两个数量
- [x] 1.4 先写失败测试：`tests/test_help_site.py` 增加「数据缺字段 / 未登记站外地址 / 条目数不一致」三种情形下 `check_scenarios` 非零退出

## 2. 页面与导航接线

- [x] 2.1 `webhelp/site.py`：`PAGES` 增加 `scenarios`；新增场景渲染辅助（分组读取、卡片渲染、内嵌提示词数据块、动作文案），保持 `$:` 只输出视图已转义内容
- [x] 2.2 新增 `webhelp/templates/scenarios.html`：页头、说明、分组与卡片网格、页脚行动区；复用既有 `section` / `features-grid` / `details.faq-item` 形态
- [x] 2.3 `webhelp/config.json`：`nav` 在 `enterprise` 与 `architecture` 之间插入 `scenarios`；新增 `external_links.schemes` 与 `external_links.hosts` 白名单
- [x] 2.4 `webhelp/lang/zh.json` 与 `en.json`：补齐 `meta.title_scenarios`、`scenarios.lead`、`scenarios.intro.*`、`scenarios.groups.*` 与卡片动作/字段文案（`scenarios.try`、`scenarios.detail`、`scenarios.pain_label`、`scenarios.explain_label`、`scenarios.hint_label`、`scenarios.roles_label`）
- [x] 2.5 `webhelp/site.py` 的 `page_titles` 与 `lead_keys` 接入新页面，使标题与 SEO 描述不落回首页文案

## 3. 前端资源

- [x] 3.1 `webhelp/assets/css/style.css`：场景卡片、一键体验角标、领域与岗位标签、卡片动作区，取值使用站点既有 CSS 变量
- [x] 3.2 `webhelp/assets/js/main.js`：点击 `[data-scenario-try]` 时从内嵌数据块取提示词、按 `deep_link` 模板组装地址并唤起客户端；缺失条目时不动作也不报错
- [x] 3.3 内嵌 JSON 序列化时把 `<` 转义为 `\u003c`，并断言页面不出现裸 `</script`

## 4. 测试与校验

- [x] 4.1 改写 `tests/test_help_site.py::test_all_pages_and_documents_in_both_languages` 的链接不变量：站内链接保持原有「必须可达」断言，站外链接改为「必须命中 `config.json` 白名单」
- [x] 4.2 新增断言：`/help/scenarios` 两语言均 200、导航入口位于 `enterprise` 与 `architecture` 之间、卡片字段齐全、页面含内嵌提示词数据块且条数为 63、可见文案不含提示词正文
- [x] 4.3 运行 `pytest tests/test_help_site.py tests/test_help_site_url.py tests/test_route_registry.py -q` 与 `python webhelp/tools/check_manual.py`、`python webhelp/tools/check_scenarios.py`，全部通过

## 5. 打包与文档

- [x] 5.1 核对 `desktop/build/cowagent-backend.spec` 的 webhelp 资源收集包含新增的 `scenarios.json`（缺项则补 spec，并加断言钉住）
- [x] 5.2 更新 `webhelp/README.md`：新增页面地址、目录表中补 `scenarios.json` 与 `tools/check_scenarios.py`、记录快照来源与重抓方式、说明一键体验依赖外部客户端

## 6. 验收

- [x] 6.1 浏览器人工核对：导航位置、四个分组、卡片字段、一键体验组装出的地址、查看做法外链、英文站点文案、页面无站外资源请求（深色 / 浅色 / 窄屏三态）
- [x] 6.2 `openspec validate add-help-scenario-catalog --strict` 通过
