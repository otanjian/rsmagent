## Context

编码类型的提示由 `add-opencode-coding-agents` 引入，落地在三个渲染点，用的是同一份 `.coding-agent-badge` 规则：

| 位置 | 渲染函数 | 当前标记 |
| --- | --- | --- |
| 工作台卡片右上角 | `console.js::agentWorkbenchCardHTML` | `<span class="coding-agent-badge">编码（Opencode）</span>`，与「默认」徽标同处 `.agent-wb-card-badges` 列 |
| 新对话选单的智能体行 | `console.js::paintNewChatMenu` | 同上，位于名称 `<span>` 之后 |
| 智能体管理详情身份区 | `console.js::renderAgentDetail` | 同一样式，用 `hidden` 类按类型开关 |

样式只在 `channel/web/static/css/coding.css` 定义一次，`chat.html` 通过 `<link href="assets/css/coding.css">` 加载。

`add-opencode-coding-agents` 的 spec 没有把「提示是文字徽标」写成 requirement，只在 evidence 里记录了当时的验收结果。因此本次既不是回退既有 requirement，也不能只改代码——那样会让后来者依据那份 evidence 把它改回文字。

## Goals / Non-Goals

**目标**

- 三处类型提示只呈现一个终端图标，没有可见文字。
- 三处由同一段实现产出，避免图形与样式各自演化。
- 类型仍可被读出：可访问名称与悬停说明沿用既有 `agents_type_coding`。
- `normal` 与缺少类型的旧数据保持不显示，兜底语义不变。

**非目标**

- 不删除或改写 `agents_type_coding` 这个 i18n key：创建表单的类型选项、详情表单的「类型」字段值和本图标的可访问名称都仍在使用它。
- 不改创建表单的类型选项与详情表单的字段值——那是待选项与配置数据，不是标签。
- 不改服务端投影、路由、权限、数据或发布开关。

## Decisions

### 图标而非文字

**选择**：图标用既有 Font Awesome 的 `fa-terminal`（`chat.html` 中编码类型选项已用同一个图标），不再渲染文字。

**理由**：`fa-terminal` 已经是本产品表示编码类型的那枚图形，复用不会引入第二个「编码」符号；而文字标签在卡片上与名称并排时读作第二个名字，正是本次要消除的问题。

**取舍**：图标比文字徽标更依赖用户已经认得这枚图形。因此可访问名称与悬停说明必须保留完整文案——这是把「信息量」从可见文字移到无障碍层，不是把信息删掉。

### 一份实现，三处调用

**选择**：新增 `codingAgentTypeHint(extraClass)`，返回整段标记（含 `class`、`role="img"`、`title`、`aria-label`、图标）。三处只传一个可选的附加类（详情身份区传 `hidden` 开关）。

**理由**：三处目前是三段字面量。若这次只把三段字面量各自改成图标，下次改图形或文案仍要改三处，且没有任何测试会失败。抽成函数后，`title` / `aria-label` / 图标名只有一个来源。

**取舍**：三个前端测试桩按 `section()` 切片加载 `console.js`，其中两个只加载 `renderAgentDetail` 所在切片、一个加载选单切片。函数定义放在 `agentWorkbenchCardHTML` 之前，卡片桩的切片本就覆盖它；其余两个桩各加一行切片加载即可。代价是多改两处测试桩，换来的是桩里跑的是真实实现而不是替身。

### 属性转义

**选择**：写入 `title` / `aria-label` 时在 `escapeHtml` 之后再 `.replace(/"/g, '&quot;')`。

**理由**：`escapeHtml` 基于 `textNode.innerHTML`，只处理 `&`、`<`、`>`，不处理引号；把翻译文案插进双引号属性里需要补引号转义。仓内既有属性插值（如附件 `src`）就是这么做的。

### 样式

**选择**：`.coding-agent-badge` 由文字胶囊改为 20×20 的图标胶囊（`display: inline-flex; align-items: center; justify-content: center; width/height: 20px`），配色、边框、圆角与明暗主题规则不变。

**理由**：卡片右上角的徽标列是固定宽度的一列，图标胶囊比文字胶囊窄，名称获得的宽度只增不减，不需要额外的布局补偿。保留 `display: inline-flex` 也保住了详情身份区 `hidden` 类的开关行为（该处改造前后一致）。

## Risks / Trade-offs

- **类型提示更不显眼**：看惯文字的用户可能一时认不出这枚图标。缓解手段是悬停说明与可访问名称，以及创建表单与详情字段仍以文字写明类型。若后续认为图标仍不够清楚，正确的下一步是换更明确的图形或补充图例，而不是回到文字标签。
- **`title` 原生提示的延迟**：原生 `title` 悬停约 1 秒才出现，且不覆盖触摸设备。触摸场景下类型信息的另一处出口是管理详情的「类型」字段，故未为此引入自定义浮层。

## Migration / Rollback

无数据、接口或路由变更。回退即把三处调用还原为文字徽标并恢复 `.coding-agent-badge` 的排版规则；图标胶囊比文字胶囊窄，回退不会遗留布局依赖。

## Open Questions

无。
