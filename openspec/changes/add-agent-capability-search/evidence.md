# 验收记录：能力页技能与工具目录模糊搜索

## 变更范围

- `channel/web/static/js/console.js`：`renderAgentCapabilitiesPane` 内新增顶部关键词框与 `skillMatchesQuery` / `toolMatchesQuery`，技能行与工具行分别由 `paintSkillList` / `paintToolList` 就地重绘。
- `channel/web/static/css/console.css`：`.agent-cap-search*` 与 `.agent-skill-no-match` 样式。
- `channel/web/static/js/i18n/agents.js` + `tests/fixtures/console_i18n_snapshot.json`：新增/更名 4 个键（简中 / 繁中 / 英文）。
- `tests/test_agent_capability_search_frontend.cjs`（新增）、`tests/test_agent_tools_frontend.cjs`、`tests/test_agent_config_fields_hidden_frontend.cjs`（按新 DOM 取用工具目录容器，语义断言不变）。

无接口、无数据库、无 feature flag 变更；回滚只撤回前端增量。

## 定向回归（全部通过）

```
node --check channel/web/static/js/console.js                        → SYNTAX_OK
node --test tests/test_agent_capability_search_frontend.cjs \
           tests/test_agent_tools_frontend.cjs \
           tests/test_agent_config_fields_hidden_frontend.cjs \
           tests/test_agent_profile_frontend.cjs \
           tests/test_agent_visibility_frontend.cjs \
           tests/test_console_i18n_coverage.cjs \
           tests/test_console_i18n_parity.cjs \
           tests/test_agent_welcome_frontend.cjs \
           tests/test_console_workspace_frontend.cjs   → 74 tests, 74 pass, 0 fail
```

`node --test tests/*.cjs` 全量：965 tests / 955 pass / 10 fail；10 个失败在 `git worktree` 的 HEAD 基线上同样失败（`test_appearance_browser.cjs`、`test_personal_console_frontend.cjs` 缺少 `personal-console.js`，以及 `test_sidebar_account_frontend.cjs`、`test_scenes_frontend.cjs`、`test_scene_workbench_frontend.cjs`、`tests/_tmp_repro_modeldefaults.cjs` 的既有失败），与本 change 无关。

## 单元级断言要点

- 搜索框在「技能」标题之上、且位于两个目录容器之外；两个容器内均不含关键词框。
- `kw` → `knowledge-wiki`；`knowledgewiki` 忽略分隔符；`IMAGE` 忽略大小写；`image gen` 命中显示名；`text prompts` 与 `询价` 命中描述；关键词首尾空白被忽略。
- 同一关键词同时收窄工具：`rd` → `read`；`scheduled tasks` → `scheduler`；清空后两个目录都恢复。
- 无匹配时两个目录各自显示自己的提示（技能/工具文案不同），不误报为「未安装」。
- 过滤下取消勾选：已选 `knowledge-wiki`、`image-generation`、`rfq-quote`，搜 `image` 后取消 `image-generation`，提交集合为 `knowledge-wiki`、`rfq-quote`；清空关键词后勾选状态一致；已不在目录中的绑定名（`gone-skill`）同样保留。
- 「使用全部」（`null`）在过滤下不变，关闭后从空子集开始；工具「选择所有工具」始终反映完整目录，不随过滤变化。
- 切换智能体清空关键词；同一智能体重绘（工具勾选）保留关键词并回填到重建的输入框。

## 浏览器验收

`node openspec/changes/add-agent-capability-search/evidence/serve-fixture.cjs` 起本地 fixture（shipped `chat.html` + `console.js`/`console.css`，仅内存在线数据，不触达真实 Agent / 技能 / 会话），浏览器加载 `http://127.0.0.1:9908/chat` → 智能体 → 打开「智能办公助理」→「能力」。

实测（`Runtime.evaluate`）：

| 检查 | 结果 |
| --- | --- |
| 搜索框位置 | `boxBelowSections=false`：框底部在工作区顶部之上，位于「技能」与「工具目录」标题之前 |
| 占位文案 | 简中 `搜索技能或工具`；繁中 `搜尋技能或工具`（切换语言后重绘即生效） |
| 初始目录 | 12 个技能 / 7 个工具全部展示 |
| `rd` | 技能 0 行；工具 `read`、`requirements_delivery`；`document.activeElement` 仍是 `agent-cap-search` |
| `img gen` | 技能 `image-generation`；工具 0 行 |
| `scheduled tasks` | 工具 `scheduler`（描述子串命中）；技能 0 行 |
| `询价` | 技能 `rfq-quote`（中文描述子串命中） |
| `card` | 两个目录均为 0 行 |
| `zzz-nope` | 技能区显示「未找到匹配的技能」、工具区显示「未找到匹配的工具」，清除按钮可见 |
| 工具全选状态 | 勾选「选择所有工具」后输入 `rd`：主控框仍为选中，可见行只剩 `read`、`requirements_delivery`；清空后仍选中 |
| 过滤下取消技能 | 搜 `image` 只显示 `image-generation`，取消勾选后 `POST /api/agents` 的 `skills` 为 `["knowledge-wiki","rfq-quote","data-analysis"]`（被隐藏的三项全部保留）；清空后 12 行恢复且勾选状态一致 |
| 窄屏 420×800 | 框宽 380、左右未溢出 Pane，`document.documentElement.scrollWidth == innerWidth` |

截图：

- `light-both-catalogues-filtered.png`：浅色，关键词 `re` 同时收窄技能（`rfq-quote`、`excel-report`、`code-review`）与工具（6 项）。
- `no-match-both-catalogues.png`：`zzz-nope`，两个目录各自的空态与可见的清除按钮。
- `dark-zh-hant-both-filtered.png`：深色 + 繁体中文，`sh` 过滤出技能 `web-search`、`sql-helper` 与工具 `bash`、`web_search`、`scheduler`。
- `narrow-viewport.png`：420px 宽视口下的布局。

## 兼容与回滚

- 无数据库迁移：只读取既有 `AgentProfile.skills`、`tools_allowlist`/`tools_denylist` 与 `/api/skills`、`/api/tools`。
- 无新增 feature flag；保存协议（含 `revision` 乐观并发）与过滤前完全一致，`skills: null` 的「使用全部」语义未受影响。
- 回滚只需撤回本 change 的前端增量（`console.js` / `console.css` / `agents.js`），旧版本对 `skills` 数组与工具白/黑名单完全兼容。
- 静态资源带 `mtime` 版本戳，刷新页面即生效，无需重启服务。
