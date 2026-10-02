## 1. 规范与红测

- [x] 1.1 完成 `agent-capability-bindings` 的技能与工具目录模糊搜索、绑定完整性、交互与多语言 delta，并通过 `openspec validate add-agent-capability-search --strict`。
- [x] 1.2 新增 `tests/test_agent_capability_search_frontend.cjs`：断言搜索框位于两个区块之上、子序列/大小写/描述/中文匹配、同一关键词同时过滤技能与工具、两目录各自的空态、清空恢复、过滤下取消与勾选不丢隐藏已选项、使用全部与工具全选语义不变、切换智能体重置、同智能体重绘保留关键词；先在旧实现上跑红。

## 2. 实现

- [x] 2.1 `console.js`：新增 `skillMatchesQuery()` / `toolMatchesQuery()` 与每智能体搜索状态；搜索框渲染在两个目录区块之上，技能行与工具行分别进入 `#agent-skills-list` 与 `#agent-tools-list`。
- [x] 2.2 `console.js`：关键词变化只重绘两个目录的行，保持输入框焦点；各目录无匹配时显示自己的空态；清除入口恢复完整目录。
- [x] 2.3 `console.js`：技能勾选以完整已绑集合增量提交，保留被过滤隐藏与已不在目录中的已选项；工具选择状态改为每次绘制就地求值，「使用全部」与「选择所有工具」的语义与保存协议不变。
- [x] 2.4 同步 `tests/test_agent_tools_frontend.cjs` 与 `tests/test_agent_config_fields_hidden_frontend.cjs` 对工具目录容器的取用，保持既有语义断言。

## 3. 样式与文案

- [x] 3.1 `console.css`：顶部搜索框、清除按钮、无匹配提示的浅色/深色与焦点态样式，窄屏不横向溢出。
- [x] 3.2 `i18n/agents.js` 与 `tests/fixtures/console_i18n_snapshot.json`：补齐简体中文、繁体中文、英文的占位、清除与两个目录的无匹配文案。

## 4. 验收

- [x] 4.1 运行定向 Node 回归（新增用例、技能/工具、能力页字段、i18n 覆盖率与对齐）与 `node --check channel/web/static/js/console.js`。
- [x] 4.2 浏览器验证真实「能力」页签：搜索框位置、技能与工具同时过滤、两目录空态、清除、过滤下勾选保持、浅色/深色与窄屏，并记录证据。
- [x] 4.3 记录无需迁移与无新增 feature flag 的兼容/回滚说明，完成任务状态与 evidence.md。
