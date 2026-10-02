## 1. 核对接入基线

- [x] 1.1 核对 `chat.html` 实际加载的 i18n 命名空间、`VIEW_META.skills` 名称映射和侧栏/页面模板，确认既有导航与授权机制保持；无新增跨 change 前置后进入文案实施。

## 2. 统一能力中心名称

- [x] 2.1 将 `channel/web/static/js/i18n/navigation.js` 的 `menu_skills` 与 `channel/web/static/js/i18n/core.js` 的 `skills_title` 更新为简繁中文「能力中心」、英文「Capability Center」。
- [x] 2.2 同步 `channel/web/chat.html`、`channel/web/templates/layout/sidebar.html`、`channel/web/templates/views/skills.html` 中目标菜单和主标题的默认文案；保留智能体详情「能力」页签及其他独立文案。
- [x] 2.3 定向更新 `tests/fixtures/console_i18n_snapshot.json` 对应语言的两个翻译值，审查差异未改变视图、地址、权限、技能工具功能或原生 Desktop 文件。

## 3. 验证与交付记录

- [x] 3.1 在第 2 阶段文案与快照一致后，运行 `node --test tests/test_console_i18n_coverage.cjs tests/test_console_i18n_parity.cjs`，记录真实结果并处理本次引入的问题。
- [ ] 3.2 检查实际控制台的菜单、顶部标题、面包屑及主标题，覆盖简繁中文/英文、classic/split、刷新及旧地址、窄屏和侧栏收起；确认技能工具内容与智能体「能力」页签仍按原流程显示。
- [x] 3.3 在本 change 中记录验收结果与发布/恢复说明：无数据迁移、无新增 feature flag，既有导航开关两侧使用新名称，回退仅恢复文案与快照；无法验证的项目明确记录，不将模拟结果当作真实页面验收。
- [x] 3.4 运行 `openspec validate rename-capabilities-menu-to-center --strict`，确认文档与最终实现范围一致，仅在对应实施和验收完成后勾选任务。
