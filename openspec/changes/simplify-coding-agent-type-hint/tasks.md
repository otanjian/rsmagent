## 1. 规范

- [x] 1.1 新建 capability `coding-agent-type-hint`，把「提示是文字徽标」改写为「图标提示 + 可访问名称」的可验收要求；`openspec validate simplify-coding-agent-type-hint --strict` 通过。（证据 1）

## 2. 实现

- [x] 2.1 在 `console.js` 的 `agentWorkbenchCardHTML` 之前新增 `codingAgentTypeHint(extraClass)`：返回整段图标标记，含 `role="img"`、`title`、`aria-label`（文案取 `agents_type_coding`，属性内补引号转义）与 `fa-terminal` 图标。（证据 2.1）
- [x] 2.2 三处调用替换为 `codingAgentTypeHint()`：`agentWorkbenchCardHTML`（卡片右上角徽标列）、`paintNewChatMenu`（选单行名称之后）、`renderAgentDetail`（身份区，传 `hidden` 开关）。（证据 2.2、3.1–3.3）
- [x] 2.3 `coding.css` 的 `.coding-agent-badge` 改为图标胶囊（20×20、居中、`display: inline-flex` 保持），配色与明暗主题规则不动。（证据 2.3）
- [x] 2.4 全仓检索确认 `.coding-agent-badge` 的三处消费者与 `agents_type_coding` 的其余消费者（创建表单选项、详情字段值、可访问名称）未被误改。（证据 2.2）

## 3. 测试

- [x] 3.1 `test_agent_workbench_frontend.cjs`：卡片徽标只含图标、不含可见文字；可访问名称与悬停说明仍为 `agents_type_coding`；`normal` 与缺类型旧档案不出现图标。（证据 4.1）
- [x] 3.2 `test_team_chat_launch_frontend.cjs` 与 `test_agent_chat_launch_frontend.cjs`：补载 `codingAgentTypeHint` 切片，断言选单行的编码智能体只出图标，且与卡片产出的标记一致（锁住「一份实现」）。（证据 4.1）
- [x] 3.3 `test_tenant_default_agent_frontend.cjs` 补载同一切片并断言详情身份区只出图标；`test_user_default_agent_frontend.cjs` 的切片本就覆盖该函数，未改。（证据 4.1）
- [x] 3.4 新增断言：三处渲染产物中徽标内不出现可见文字节点，且这些断言在改动前的文字标记上为红（红/绿核验）。（证据 4.1、4.2）

## 4. 文档与证据

- [x] 4.1 确认 `docs/` 无引用该文字徽标的位置（检索无命中），无需同步三语。（证据 5）
- [x] 4.2 写入 `evidence.md`：三处渲染产物、可访问名称、normal 兜底、测试与校验的真实输出；并记录 `add-opencode-coding-agents` 的 evidence 仅作历史记录、不回写。（证据 6）

## 5. 验收

- [x] 5.1 跑相关前端用例与全量前端回归，失败集与改动前基线一致。（证据 4.3）
- [x] 5.2 `openspec validate simplify-coding-agent-type-hint --strict` 通过。（证据 1）
