## Why

「新建对话」折叠菜单当前把「多智能体对话」排在全部单人智能体之后，而团队对话是菜单里唯一的**跨智能体**动作，也是企业用户最常需要的协作入口；它被一长串智能体推到菜单底部后，用户需要滚过整份花名册才能看到，容易被当作"没有这个功能"。把团队入口提到菜单首位，使协作入口在任意智能体数量下都保持同一可见位置。

## What Changes

- 「新建对话」折叠菜单的顺序改为：**「多智能体对话」固定为第一项**，其后是分隔线，再按既有候选与排序规则列出逐个智能体的普通对话。
- 该顺序由两个启动控件共用的同一绘制实现产出，因此工作台侧栏「新建对话」菜单与历史页「新对话」菜单同时生效，不各自维护顺序。
- 团队入口的可见性条件不变：仍只在可用智能体多于一位时随折叠箭头出现，候选规则、创建流程、权限门禁与失败处理不变。
- 分隔线位置随团队行上移：团队行与其下的单人列表之间保留分隔线，不改变行高、配色、主题适配与键盘可达性。
- 主按钮、移动端弹层、发布开关回退行为、coding 智能体提示均不变。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `workbench-sidebar-launch`: `新建对话控件与选择菜单语义明确` 明确折叠菜单的选项顺序——「多智能体对话」固定排在逐个智能体的普通对话之前，并保证该顺序在侧栏与历史页两个入口一致。

## Impact

- **Web 前端**：`channel/web/static/js/console.js` 的 `paintNewChatMenu`（两个入口共用的唯一绘制实现）调整行拼接顺序；`channel/web/static/css/console.css`、`channel/web/static/css/sessions.css` 中 `.new-chat-sep` / `.new-chat-item` 的既有样式复用，预计无需新增样式。
- **数据归属**：不新增字段、接口或配置，不改变团队候选集合、默认响应者、会话与工作空间归属。
- **兼容与回退**：不新增 feature flag。`workbench-sidebar-launch` 的发布开关语义不变——开关关闭时侧栏不显示折叠菜单，历史页入口仍提供同一顺序的团队入口。既有会话、历史与团队数据不受影响。
- **测试**：`tests/test_agent_chat_launch_frontend.cjs`（两个入口共用绘制的顺序断言）、`tests/test_sidebar_launch_browser.cjs`（真实浏览器行渲染）。
- **依赖**：无跨 change 前置；沿用已归档的 `refine-sidebar-team-chat-launch` 所建立的共享控件与团队创建流程。
