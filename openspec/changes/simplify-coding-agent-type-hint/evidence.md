# 实施证据（simplify-coding-agent-type-hint）

本文件记录 tasks.md 各项要求的真实结果。命令均在仓库根目录执行。

## 1 规范

- `openspec validate simplify-coding-agent-type-hint --strict`：通过（Change is valid）。

## 2 实现

### 2.1 共用实现

`channel/web/static/js/console.js` 在 `agentWorkbenchCardHTML` 之前新增 `codingAgentTypeHint(extraClass)`，返回值经真实页面读取为：

```html
<span class="coding-agent-badge" role="img" title="编码（Opencode）" aria-label="编码（Opencode）"><i class="fas fa-terminal" aria-hidden="true"></i></span>
```

函数内补了引号转义（`escapeHtml` 只处理 `&`/`<`/`>`，属性内再 `.replace(/"/g, '&quot;')`），与仓内既有属性插值做法一致。

### 2.2 三处调用

| 位置 | 改动前 | 改动后 |
| --- | --- | --- |
| 工作台卡片 | `<span class="coding-agent-badge">编码（Opencode）</span>` | `codingAgentTypeHint()` |
| 新对话选单行 | 同上（名称 `<span>` 之后） | `codingAgentTypeHint()` |
| 管理详情身份区 | `<span class="coding-agent-badge ${coding ? '' : 'hidden'}">…</span>` | `codingAgentTypeHint(coding ? '' : 'hidden')`（`hidden` 开关保留） |

全仓检索 `.coding-agent-badge` 的消费者仍为这三处；`agents_type_coding` 的其余消费者（创建表单类型选项 `chat.html:1101`、详情「类型」字段值 `console.js:agent-type-value`、本图标可访问名称）未被改动。

### 2.3 样式

`channel/web/static/css/coding.css` 的 `.coding-agent-badge` 由文字胶囊（`padding: 2px 8px; font-size: 12px; gap: 6px`）改为 20×20 图标胶囊（`width/height: 20px; font-size: 11px; line-height: 1; align-items: center; justify-content: center`），配色、边框与明暗主题规则不动。卡片右上角徽标列是固定宽度的一列，图标胶囊比文字胶囊窄，名称获得的宽度只增不减。

## 3 真实页面验收（本机运行中的控制台，database 模式，6 个智能体）

页面：`http://localhost:9899/chat`，视口用 CDP `Emulation.setDeviceMetricsOverride` 固定为 1024×800。读取全部经 `Runtime.evaluate`，未改页面逻辑。

### 3.1 工作台卡片（截图 `evidence/coding-type-icon-workbench-card.png`）

1024px 下 6 张卡片：

- 只有 `sap`（`agent_type=coding`）带类型提示，其余 5 张为 `null`。
- `sap` 的提示：`textContent === ''`、`getBoundingClientRect()` 为 `20 × 20`、图标 `display` 非 none、`role="img"`、`title` 与 `aria-label` 均为 `编码（Opencode）`、不处于 `hidden`。
- 每张卡片的名称都未截断（`scrollWidth === clientWidth`）；`document.documentElement.scrollWidth - innerWidth === 0`，无横向溢出。

改动前的对照：使用者截图（同一账号、同宽）中该卡片名称为 `S…`，即文字徽标把「SAP 智能助手」挤到截断。改动后该卡片名称框宽 233px（其余卡片 265px），完整显示。

### 3.2 新对话选单行（截图 `evidence/coding-type-icon-new-chat-picker.png`）

历史页展开 `#new-chat-menu`（7 行），`sap` 行：

- 行宽 196px，子元素依次为 头像 `800–822`、名称 `831–912.3`、类型图标 `921.3–941.3`，行间居左排列，9px 为既有 flex `gap`。
- 图标 `textContent === ''`，可见（`getClientRects().length > 0`），`title="编码（Opencode）"`，行高 36px 不变。

### 3.3 管理详情身份区

- `openAgentDetail('sap')`：身份区文本为 `S SAP 智能助手 sap`，不含类型文字；图标 20×20、可见、`title` 为 `编码（Opencode）`；同页「类型」字段值仍为 `编码（Opencode）`（图标是标签，不是字段）。
- `openAgentDetail('bug-butler-test15')`（normal）：图标 `hidden` 且不可见，「类型」字段值为 `普通`。

### 3.4 未能覆盖

- 触摸设备的悬停说明、以及超长名称在极窄视口下与图标的相互挤压，未逐项截图；前者依赖 `title` 的原生行为（已在设计取舍中说明），后者由卡片名称 `truncate` 与图标 `flex-shrink: 0` 承担。

## 4 测试

### 4.1 新增/更新的断言

- `tests/test_agent_workbench_frontend.cjs`：卡片提示只含图标、不含可见文字、带 `role="img"` / `title` / `aria-label`，且与 `codingAgentTypeHint()` 输出相同；`normal` 与缺类型旧档案不出现；新增用例断言 `coding.css` 的提示规则是 20×20 方形胶囊且不含 `padding`。
- `tests/test_agent_chat_launch_frontend.cjs`：选单行的编码智能体只出图标，且与 `codingAgentTypeHint()` 输出逐字相同（锁住「一份实现」）。
- `tests/test_tenant_default_agent_frontend.cjs`：详情身份区只出图标、与共用实现相同；表单「类型」字段仍保留文字；`normal` 仍带 `hidden`。
- `tests/test_team_chat_launch_frontend.cjs`、`tests/test_tenant_default_agent_frontend.cjs`、`tests/test_agent_chat_launch_frontend.cjs`：测试桩补载 `codingAgentTypeHint` 切片，使这三处跑的是真实实现而非替身。`test_agent_workbench_frontend.cjs` 与 `test_user_default_agent_frontend.cjs` 的切片本就覆盖该函数，未改。

### 4.2 断言确实能区分改动（红/绿核验）

把改动前的文字标记 `<span class="coding-agent-badge">…</span>` 与 `<span class="coding-agent-badge hidden">…</span>` 直接喂给新增断言的五条判据（图标存在、无可见文字、`role="img"`、`title` 文案、`aria-label` 文案），五条在两种旧标记上**全部为红**；在当前标记上全部为绿。

### 4.3 命令与结果

- `node --test tests/test_agent_workbench_frontend.cjs tests/test_agent_chat_launch_frontend.cjs tests/test_team_chat_launch_frontend.cjs tests/test_tenant_default_agent_frontend.cjs tests/test_user_default_agent_frontend.cjs tests/test_sidebar_launch_browser.cjs` → 86 项，86 通过，0 失败。
- 前端全量 `node --test tests/*.cjs` → 901 项，891 通过，10 失败；失败集与改动前基线完全一致，只有 `tests/test_appearance_browser.cjs` 与 `tests/test_personal_console_frontend.cjs` 两个文件（与本 change 无关，已在本机 HEAD worktree 复现），无一项由本 change 引入。
- 未跑 Python 用例：本 change 不触及服务端、路由、数据与权限，`git diff --name-only` 仅含 `console.js`、`coding.css` 与测试。

## 5 文档

`docs/` 全量检索无「编码（Opencode）」类型徽标的描述（只有 `docs/opencode-coding-agents.md` 讲 OpenCode 自己外壳的渠道徽标，与本次无关），因此没有需要同步的三语文案。

## 6 与既有 change 的关系

`openspec/changes/add-opencode-coding-agents/evidence.md` 记录「工作台卡片与新对话选单显示 `编码（Opencode）` 类型徽标」，那是当时的验收记录。本次**不回写**该文件：呈现方式的变更由新 capability `coding-agent-type-hint` 承载，`tasks.md` 4.2 已把这一点写清，避免后来者据旧 evidence 改回文字标签。

## 7 未决

- 图标是否足够被认出（`fa-terminal` 在创建表单的类型选项里已在使用）。若复审认为仍不清楚，正确的下一步是换更明确的图形或在选单加图例，而不是恢复文字标签——该取舍已写入 design.md。
