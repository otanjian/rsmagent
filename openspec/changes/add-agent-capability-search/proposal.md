## Why

智能体管理「能力」页签的技能区块只有「使用全部已安装技能」开关与完整目录，工具区块同样只有「选择所有工具」与完整目录。默认智能体可挂载数十项技能与工具，管理动作本质是「找到某一项再勾选或取消」，目前只能逐行肉眼查找，条数一多就无从下手。

## What Changes

- 在「能力」页签的技能区块与工具区块之上增加**单个**关键词搜索框（「技能」标题之上），同一个关键词同时检索技能目录与工具目录。
- 匹配范围：技能名称（`name`/`id`）、显示名（`display_name`）、描述（`description`），以及工具名与工具描述；不区分英文大小写，忽略关键词首尾空白。
- 匹配规则：名称类短标识同时接受连续子串与按顺序的非连续字符子序列，并忽略 `-`/`_`/空白分隔（`kw` 命中 `knowledge-wiki`，`rd` 命中 `read`）；描述只做子串匹配，避免长文本的子序列把任意关键词都判为命中。
- 搜索只改变可见行，不改变已绑能力：子集模式下切换某个可见技能行时，以「完整选中集合（含被关键词隐藏的已选项）」为准提交，过滤不得清除隐藏的已选技能；「使用全部」的 `null` 语义、工具「选择所有工具」对完整目录的判断与保存协议保持不变。
- 每个目录各自显示无匹配提示与共用的清除条件入口，不误报为「未安装」或读取失败。
- 输入过程中只重绘两个目录的行，不重建输入框，保持焦点与中文输入法组合状态；切换智能体清空关键词并回到完整目录。
- 补充简体中文、繁体中文与英文文案，并同步 i18n 快照。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `agent-capability-bindings`：能力页技能与工具目录的模糊搜索、过滤下的绑定完整性、空态与多语文案。

## Impact

- 数据唯一归属仍是既有 `AgentProfile.skills`、`tools_allowlist`/`tools_denylist` 与已安装目录（`/api/skills`、`/api/tools`）；无数据库迁移、无新增接口、无新增 feature flag，回滚只撤回本 change 的前端增量。
- 修改 `channel/web/static/js/console.js`（`renderAgentCapabilitiesPane`）、`channel/web/static/css/console.css`、`channel/web/static/js/i18n/agents.js` 与 `tests/fixtures/console_i18n_snapshot.json`；新增 `tests/test_agent_capability_search_frontend.cjs`，并同步 `test_agent_tools_frontend.cjs`、`test_agent_config_fields_hidden_frontend.cjs` 中对工具目录容器的取用。
- 依赖既有能力绑定渲染与技能/工具目录接口，以技能/工具前端回归与 i18n 回归为接入门槛；无新增跨 change 前置。
- 工作区存在其他进行中的改动，本 change 只增量修改上述区域，不触碰无关流程。
