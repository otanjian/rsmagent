# 被延后的上游前端增量（逐条清单）

`adopt-upstream-web-split` 的 D5 备选③ 允许把前端迁移降级为独立 change，条件是
**显式 `keep-fork` 基线决策 + 逐条列出被丢弃的上游增量**。本文件是那份清单；本
change 关闭（任务 7.2）前必须逐条给出「已随本 change 生效」或「显式转交下一轮同
步」，不留未交代项。

生成方式（可重跑，勿手抄）：

    git log --format="%h %ad %s" --date=short e5e2a52d..origin/master \
      -- channel/web/static/js channel/web/static/css channel/web/chat.html \
         channel/web/templates

范围界定：上述路径下、fork 控制台**当前不装载**的文件。判断「是否已生效」的判据
不是「文件在不在工作树」（都在，合并已引入），而是「控制台是否服务它」——目前
`channel/web/chat.html` 仍是 fork 单体版本，装载 `js/console.js` + `css/console.css`，
故上游拆分树中的任何改动都不生效。

## A. 结构类（`adopt-upstream-web-split` 的 `evidence/17` 已列，此处保留编号）

| # | 增量 | 上游提交 | 现状 |
| --- | --- | --- | --- |
| A1 | 拆分 shell（`chat.html` 结构与脚本顺序） | `ce10077b` | 未生效；fork 服务自己的 `chat.html` |
| A2 | 地址栏路由词汇（`#/…`、路径即视图） | `5101c0c3`、`c2efb0bb`、`1fb6b74b`、`370d96bd`、`d68644e5`、`f090a287` | 未生效；fork 在客户端切换视图 |
| A3 | `assets/js\|css/**` 按文件 mtime 的 `?v=` 版本戳 | 拆分同批 | 未生效；fork 自有点名式 cache-bust |
| A4 | 一键更新侧栏菜单 | `c28fff5f`、`43edb84b` | 未生效，且其后端 `/api/update/*` 在 `adopt-upstream-web-split` 中也未路由（`evidence/21` §E） |
| A5 | 上游落在拆分模块内的功能与修复 | 见 §B | 未生效；上界即 109 处待裁定区域 |

A5 的上界是可界定的：每个 fork 模块 = 上游模块 + fork 的 hunk（`port_frontend.py`
口径），被替换掉的区域即 `frontend_adjudication.md` 的 98 处；逐处裁定完成即
「A5 已收口」。

## B. 功能与修复类（按上游提交）

### B1. 已只缺 UI（后端半边已随 `adopt-upstream-web-split` 移植或本就在位）

这些增量在 fork 侧**有能力、缺入口**。逐条裁定时应优先确认「接上 UI 即可」，不要
重复实现后端。

| 增量 | 上游提交 | 后端半边现状 |
| --- | --- | --- |
| 模型目录覆盖编辑器（控制台按提供方编辑模型目录） | `a153c2e2`、`6d370cb6`、`ddfbc663`、`9014f356` | **在位**：`ModelsHandler` 的 `_apply_catalog` / `_merged_catalog` / `_handle_save_catalog` 已移植（`evidence/20`），`_PRESET_MODEL_META` 与 `model_catalog.remove_catalog` 同批 |
| 有序回退链（取代单一备用模型） | `7f98a3db`、`571ffad1` | **在位**：`_chat_fallback_capability` / `_set_chat_fallback` 已按 chain 语义移植（`evidence/20`），`tests/test_chat_model_fallback.py` 覆盖 |
| 搜索提供方新增（Tavily / SearXNG / Keenable） | `2641d76a`、`18198c14`、`127fa286`、`eaf8410a`、`9f025cba`、`750ef721` | **在位，且控制台入口已补**：`_SEARCH_PROVIDERS` / `_SEARCH_PROVIDER_LABELS` / `_handle_set_search_credential` 已移植（`evidence/20`），凭据弹窗也已接上三个新提供方——SearXNG 走实例 URL 输入（预填 `url_masked`、不作掩码哨兵），anysearch/keenable 留空即匿名，文案补进**已装载**的 `js/i18n/models-config.js`（三语；原先只在未装载的 `core/i18n.js`），见 `adopt-upstream-web-split` `evidence/21` §B8 与 `tests/test_console_search_providers.cjs`。剩余待裁定项只有拆分前端里的编辑器形态（`eaf8410a` 的 AnySearch 同名能力一并核对） |
| ASR 模型取配置值 | `dc6393a6` | **在位**：`_set_asr` 已移植，LinkAI 的 `voice_to_text_model` 置空即走配置默认值 |
| 上下文预算与用量 | `a153c2e2`、`2e7899fc`、`60706038`、`258e800c` | **半在**：`agent_max_context_tokens` 的取值与默认在 `ConfigHandler.GET` 位；`/api/sessions/(.*)/context_usage` 与 `compact_context` **未路由**（`evidence/21` §E），属本 change 的接线与后端一起补 |
| 调度运行历史与运行详情 | `81898611`、`082e1902`、`50a0d89e` | **缺**：`/api/scheduler/runs*`、`create`、`recipients`、`instances` **未路由**（`evidence/21` §E）。fork 的调度控制台是五个既有动词；接上运行历史需同时补路由与授权判定，故裁定成本高于 B1 其余项 |
| MCP 工具检索诊断展示 | `4024eac5`、`359198d1` | 需在裁定中确认后端字段是否已随 `tool_retrieval` 合并到位（`desktop/src/renderer/src/types.ts` 的记录显示上游已加该字段组） |

### B2. 纯前端修复（无后端半边）

这些是真正的单向丢失，逐处裁定必须检查是否存在同类修复：

| 增量 | 上游提交 |
| --- | --- |
| 知识库空状态：面板须藏在空状态之后 | `d081f65d`（PR `cbe14fd1`） |
| 微信/企微渠道卡片对齐（扫码/手动两个页签） | `24c15337` |
| 历史重载时去重自进化气泡 | `72368469` |
| 创建 Agent 弹窗在矮屏下页脚可见 | `1f191022` |
| 多 Agent 选择器：主 Agent 行 + 更紧凑的单人视图 | `8b3d5f53` |
| 控制台移动端布局 | `0771b752` |
| 流式输出时自动滚动不再与用户上滑相争 | `b05ec4ac` |
| 长 URL 在气泡内换行 | `b4c435e7` |
| `@某个 Agent` 输入提示仅在团队会话显示 | `d7bb9c68` |
| 技能卡片编辑图标 + frontmatter 渲染 | `8b513aa3` |
| 渠道断开确认文案 | `ef52cccf` |
| 渠道断开真正生效 + 微信渠道排前 | `483f486c` |
| 登录前不再轮询 401（含 `config.json` 容忍 BOM） | `a9dfc54c` |
| Agent 名册先于读它的对话状态加载 | `b2249347` |
| `navigateTo` 只定义一次，不在装载时包装 | `d43aa0c6` |

> `483f486c` 的**后端**半边（disconnect 真正生效）已由 `adopt-upstream-web-split`
> 移植（实例重命名、旧卡片路由、旧类型剪枝，见该 change 的 `evidence/21` §B1–B3）；
> 这里列的是其**前端**半边。

## C. 与「109 处裁定」的关系

§A5 与 §B 不是两套清单：§B 的每一条落点都在 §A5 界定的区域内，逐一裁定到它时，若
该处上游改动携带功能或安全修复，就按 `merged` 或 `upstream` 处置并在裁定记录里指
回本表条目。故本表的收口判据是：

1. 109 处裁定全部完成（任务 2.1）；且
2. 本表每一条被标注为「已随本 change 生效」或「显式转交下一轮同步」（任务 7.2）。

## D. 不在本表范围

- 后端 handler 增量：由 `adopt-upstream-web-split` 的 `evidence/18` / `20` / `21` 收口；
- Desktop 渲染进程：`desktop/src/renderer/**` 是独立的 Electron 应用，不装载 Web 控制台的前端，故上游对该目录的改动不因 `console.js` 保留而丢失——**正因如此它已在消费 `evidence/21` §E 里未路由的接口，见 D1**；
- 一键更新 API 的路由：`evidence/21` §E（该 change 未路由，理由是它属拆分后前端）。

### D1. 桌面端消费的未路由接口（本 change 任务 0.6 收口）

上游桌面端随 `adopt-upstream-web-split` 按 `merge` 处置进入（不是 `keep-fork`），而 `desktop/dist` 是 gitignore 产物、由 `desktop/src` 经 vite 构建，故**下一次构建即生效**。它调用下列 fork 后端**未注册**的接口：

| 接口 | 桌面端用途 | fork 端状态 |
| --- | --- | --- |
| `/api/scheduler/runs`、`/runs/detail`、`/runs/delete` | 任务页运行历史 / 详情 / 删除 | 未路由（`evidence/21` §E） |
| `/api/scheduler/create`、`/recipients`、`/instances` | 任务创建、收件人与实例选择 | 同上 |
| `/api/sessions/<id>/context_usage`、`/compact_context` | 上下文用量环与压缩 | 同上 |

fork 的桌面端在本轮同步前对这 8 条**0 处调用**（`git grep` 于 `b5c5090f` 的 `desktop/src/renderer`），故这是**同步引入的前后端断口**，不是 fork 缺失的能力。表现以降级为主：多数调用点 `.catch(() => [])`，界面呈空态；「删除运行记录」会暴露错误。逐条处置（接上路由与授权判定，或降级/隐藏入口）见任务 0.6，验收见任务 6.5。

运行期形状已由 §6.4 验收实测固定（`adopt-upstream-web-split` `evidence/23` §3 的 `KnownGapAcceptance`）：6 条 scheduler 与 3 条 `/api/update/*` 回答 **404**（确实未注册），而 `/api/sessions/<id>/{context_usage,compact_context}` 被 fork 既有的 `/api/sessions/(.*)` 捕获后回答 **405**——两条路径**不是** 404，收口时不能按"未注册"处理，需在会话详情 handler 上显式拒绝或补服务。
