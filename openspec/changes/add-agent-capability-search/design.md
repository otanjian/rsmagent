## Context

见 proposal.md。真实接入点是 `channel/web/static/js/console.js` 的 `renderAgentCapabilitiesPane()`：它一次性把「技能」与「工具目录」两个 `.agent-cap-section` 铺进 `#agent-detail-skills`，两个目录的行都由模板字符串渲染，勾选事件按 DOM 元素标识约定读取。没有组件层，也没有服务端检索接口；`installedSkills`（`/api/skills`）与 `installedTools`（`/api/tools`）就是「全量」目录。

保存路径分两类：技能走 `saveAgentCapabilities(agent, { skills })`（`null` = 使用全部已安装技能，数组 = 显式子集），工具走 `tools_allowlist` / `tools_denylist`，两者都进 `/api/agents` 的 `action: 'update'` 并参与 `revision` 乐观并发。

## Goals / Non-Goals

**Goals:** 一个搜索框同时收窄技能与工具目录；过滤不影响已绑技能与工具选择；输入焦点与中文输入法不被重绘打断；切换智能体回到完整目录；浅色/深色可用并有简中/繁中/英文文案。

**Non-Goals:** 服务端全文检索、跨智能体全局搜索、技能安装/卸载、改变 `skills`/工具白黑名单语义、数据库迁移、修改 `/api/skills` 与 `/api/tools` 契约。

## Decisions

1. **匹配放在前端，且按字段区分强度。** 两个目录都已全量在客户端，再引服务端查询只会带来新接口、权限与竞态。名称类短标识（技能名、技能显示名、工具名）接受「忽略 `-`/`_`/空白的连续子串」与「按顺序的非连续子序列」，让 `kw` 命中 `knowledge-wiki`、`rd` 命中 `read`；描述是长文本，只做子串匹配——对长文本做子序列会让几乎任意关键词都命中。查询串先 `trim` + `toLowerCase`，空查询返回全部。

2. **一个框、两个独立重绘的列表容器。** 搜索框渲染在两个 `.agent-cap-section` 之外（`技能` 标题之上），技能行进 `#agent-skills-list`、工具行进 `#agent-tools-list`，两者由同一关键词各自重绘。若沿用整块 `innerHTML` 重绘，输入框会被替换，焦点、光标位置与输入法组合状态在每次按键后丢失。

3. **选中集合以「完整已绑集合」为准，而不是过滤后的可见行。** 原实现从 `.agent-skill-item:checked` 收集名称，一旦有行被关键词隐藏，切换任意可见行就会把隐藏的已选项一起丢掉。技能改用闭包内的 `Set`：初始值取 `agent.skills` 全量，行勾选时增量增删，提交时按目录顺序归一化，并保留「仍绑定但已不在目录中」的名称。工具本来就从 `tools_allowlist`/`tools_denylist` 增量计算，不受可见行影响，但行渲染移入 painter 后仍保持这一性质。

4. **工具选择状态改为每次绘制就地求值。** `tools`、`toolSelected`、`requiresExplicitBinding` 从 `render()` 内部提到 `toolState()`，因为行 painter 也会被关键词处理器调用（此时可能已有一次保存改变了白/黑名单），不能再依赖某次整页签重绘的闭包快照。`render()` 只额外计算「选择所有工具」的选中/半选状态；该状态始终基于完整目录，不随过滤变化。

5. **关键词是渲染器上的每智能体状态。** 状态形如 `{ agentId, query }`，放在 `renderAgentCapabilitiesPane` 与 `renderAgentTasksPane` 之间的模块级 `let`（现有前端测试按函数边界切片加载代码，放在该区间内可被所有切片包含）。渲染时若 `agentId` 与当前智能体不同则重置为 `''`，从而「切换智能体清空、同智能体重绘保留」。

6. **空态是各目录自己的独立提示。** 无匹配且关键词非空时，该目录渲染自己的提示；关键词为空时目录为空则渲染空内容（保持原「目录本身为空」的表现），不伪造「未安装」或复用加载失败文案。清除入口共用搜索框内的按钮。

## Risks / Trade-offs

- 子序列匹配对 1–2 个字符的关键词过于宽松（`a` 会命中大量名称） → 模糊搜索的既有代价；描述不参与子序列匹配，已把误命中面收到最短标识上。
- 工具行渲染从模板移入 painter，`test_agent_tools_frontend.cjs` 与 `test_agent_config_fields_hidden_frontend.cjs` 需要按新 DOM 取用工具目录 → 只改测试替身/断言取值位置，语义断言保持不变。
- `console.js` 有未提交改动且被多个 change 共享 → 只增量编辑 `renderAgentCapabilitiesPane` 区间与相邻 CSS/i18n，并跑定向回归确认既有技能/工具断言不回归。
- 静态资源可能先于服务重启更新 → 本 change 不改 Python 与接口，无需重启即可见效。

## Migration Plan

先写红（前端测试断言模糊匹配、两目录过滤、过滤下绑定完整性、空态、切换重置），再实现匹配与两个 painter，最后补 CSS 与三语文案及 i18n 快照，跑定向回归与 `openspec validate --strict`。无需数据迁移、无需 feature flag；回滚只撤回本 change 的前端增量，`skills` 数组、`null` 语义与工具白/黑名单对旧版本完全兼容。验收结果记录在 evidence.md。
