## Why

编码类型的提示目前是一枚文字徽标「编码（Opencode）」，同时出现在工作台卡片右上角、新对话选单的智能体行和智能体管理详情的身份区。在卡片上它与智能体名称并排，读起来像第二个名字，而它真正要表达的唯一信息——这张卡片的动作会打开嵌入的编码面板，而不是平台输入框——被埋在一段品牌文案里。使用者要求隐藏这行文字，只留一个类型图标。

## What Changes

- 三处类型提示 SHALL 只渲染一个终端图标，MUST NOT 再渲染任何可见文字。
- 图标 SHALL 带可访问名称与悬停说明，文案沿用既有 `agents_type_coding`，使读屏与悬停用户仍能读出该智能体是编码类型；MUST NOT 只靠颜色或图形区分。
- `normal` 类型或缺少 `agent_type` 的智能体 MUST NOT 显示该图标，沿用既有「缺类型按 normal 处理」的兜底。
- 智能体创建表单的类型选项与详情表单的「类型」字段值保持文字不变：那是待选项与配置数据，不是类型标签。

## Capabilities

### New Capabilities

- `coding-agent-type-hint`: 规定编码类型提示的呈现方式（图标而非文字）、三处出现位置的一致性、可访问名称与 normal 兜底。

### Modified Capabilities

（无）

## Impact

- 前端接入点：`channel/web/static/js/console.js` 的三处徽标渲染（`agentWorkbenchCardHTML`、`paintNewChatMenu`、`renderAgentDetail`）与 `channel/web/static/css/coding.css` 的 `.coding-agent-badge`（由文字胶囊改为图标胶囊）。
- 不新增 i18n key：可访问名称沿用 `agents_type_coding`，三语快照与覆盖率清单不变。
- 不改动 `add-opencode-coding-agents` 的产物或其 evidence。该 change 的 evidence 记录「工作台卡片与新对话选单显示 `编码（Opencode）` 类型徽标」，那是当时的验收记录，本次以新 capability 改变其呈现方式，不回写历史。
- 不涉及服务端、数据、路由、权限与发布开关；无迁移、无回退动作。
