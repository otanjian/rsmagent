## Why

控制台「智能体开发」分组中的「能力」菜单需要按用户指定名称调整为「能力中心」，更明确地表达内置工具、MCP 工具和技能的集中管理入口。既有 `console-information-architecture` 规范要求菜单、页面标题与面包屑名称一致，因此此次命名调整同步覆盖该页面的标题。

## What Changes

- 将截图所示 Web 控制台菜单名称由「能力」改为「能力中心」，同步顶部标题、面包屑当前页名称与页面主标题。
- 简体中文和繁体中文均使用「能力中心」，英文使用「Capability Center」；同步实际加载的翻译字典、HTML 默认文案及对应翻译快照。
- 保留原 `skills` 视图、`admin.skills` 导航标识、既有地址、图标、排序、页面内容和权限规则。
- 将现行规范中技能入口的历史目标名称「工具与技能」更新为「能力中心」。智能体详情中的「能力」页签、模型能力字段及其他业务描述不纳入本次更名。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `console-information-architecture`：更新技能入口的正式展示名称，规定该页面菜单与标题的多语言一致性及旧入口兼容性。

## Impact

- 主要代码接入点为 `channel/web/static/js/i18n/navigation.js` 的 `menu_skills`、`channel/web/static/js/i18n/core.js` 的 `skills_title`，以及 `channel/web/chat.html` 和对应侧栏、技能页面模板的默认文案。
- 顶部标题与面包屑通过 `channel/web/static/js/console.js` 中既有 `VIEW_META.skills` 消费 `menu_skills`，继续复用此映射。
- 更新 `tests/fixtures/console_i18n_snapshot.json` 对应翻译值，使用已有 i18n 回归及页面检查验收。
- 数据唯一归属仍为原技能、工具与身份服务；此次仅修改前端展示文案，无 API、数据库、业务资源或权限模型变化，无数据迁移和新增 feature flag。
- 依赖现有控制台导航与 i18n 加载机制，无新增跨 change 依赖。原生 Desktop 独立界面不在此次截图对应的 Web 控制台范围内。
