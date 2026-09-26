# 阶段 3 证据：知识页面与智能体搜索

本文件记录 `add-traceable-knowledge-ingestion` 阶段 3（任务 3.1–3.4）的实现与验收证据。仅记录已实际执行的检查；未实现项不在此宣称完成。本阶段只做页面与界面闭环，上传与转换服务端能力仍由阶段 B 提供、转换执行仍由阶段 C 提供；生产转换保持关闭。

## 3.1 知识库智能体菜单的可选顶部搜索

实现位置：`channel/web/static/js/console.js`（`initDropdown` 的 `opts.searchable`、`_mountDropdownSearch`、`_dropdownOptionMatches`、`_dropdownSearchKeydown`；`renderKnowledgeAgentSelect` 以 `{ withAvatar: true, searchable: true }` opt-in）。

- **可选、非侵入**：只有 `searchable: true` 的调用点才挂搜索框；未 opt-in 的下拉保持原 DOM（`listEl === menuEl`，行仍是 `.cfg-dropdown-menu` 直接子节点）。
- **匹配范围**：只按已可见选项的完整名称／ID 做大小写无关的子串匹配（含 CJK 子串），不新增服务器候选，不隐藏任何未被过滤的行给服务端。
- **清空／无结果**：清空恢复全部行并隐藏无结果提示；被过滤掉的已选行仍显示在触发器上（过滤只收窄菜单，不改变当前选择）。
- **键盘与输入法**：`ArrowUp/ArrowDown` 移动高亮、`Enter` 选中、`Escape` 先清空查询再关闭；输入框节点在过滤时不重建，焦点与光标不丢；`isComposing` 组合期间不触发过滤。
- **筛选不换库**：`selectKnowledgeAgent` 是唯一换库入口；过滤本身不改 `knowledgeAgentId`、不重发知识读请求。

## 3.2 上传按钮、原始资料页签、上传面板、逐项进度与冲突选择

实现位置：`channel/web/chat.html`（`#knowledge-tab-sources`、`#knowledge-panel-sources`、`#knowledge-sources-*`、`#knowledge-upload-*`）、`channel/web/static/js/console.js`（`switchKnowledgeTab`、`loadKnowledgeSources`、`renderKnowledgeSources`、`openKnowledgeUploadPanel`、`addKnowledgeUploadFiles`、`submitKnowledgeUpload`、`resolveKnowledgeUploadConflict`）。

- **页签独立状态**：文档页与原始资料页各自维护空／加载／失败状态；「有原件但无正文」由资料行的 saved/searchable 区分与详情正文说明共同承载，不借用文档页文案。
- **上传按钮能力门控**：`#knowledge-upload-btn` 仅在 `capabilities.source_upload.available` 为真时显示，不可用时把原因写在控件 `title` 上，而不是给一个点了必然失败的按钮。
- **上传面板**：多选、拖放到对话框或空列表投放区；逐文件做本地配额／大小校验（`source_quota_exceeded`、`max_file_size`），XHR 上报逐项进度。
- **同名冲突**：默认不覆盖，按项给出「始终新建资料」与「始终追加版本（携带 `target_source_id` + `expected_version`）」两种显式选择，未选择不发请求。
- **保存后转换**：仅在部署具备转换能力时可勾选，对刚保存项提交 `/api/knowledge/sources/task`（`action="convert"`）。

## 3.3 资料列表与详情：版本、状态／错误、下载、转换、重试、取消、生命周期

实现位置：`channel/web/static/js/console.js`（`renderKnowledgeSources`、`_knowledgeSourceRowHtml`、`_knowledgeSourceDetailHtml`、`openKnowledgeSourceDetail`、`downloadKnowledgeSource`、`setKnowledgeSourceLifecycle`、`retryKnowledgeSourceTask`、`cancelKnowledgeSourceTask`、`requestKnowledgeSourceConversion`、`_maybeStartKnowledgeSourcePolling`）。

- **区分「已保存」与「可检索」**：行以 `data-status`（`searchable`／`saved`／`converting`／`failed`／`disabled`）与 `data-latest-version`／`data-active-version` 同时表达「最新原件」与「生效正文」，v2 已存而 v1 仍生效时两个数字独立。
- **详情**：元数据、已发布正文版本、逐版本直接下载、转换任务（失败重试、进行中取消）、停用／启用／删除生命周期，以及「上传新版本」。
- **删除确认诚实计数**：提示明确写出将移除的原件版本数与当前已发布的检索正文；没有已发布正文时改述另一种说法，不留占位符。
- **轮询只在当前页**：仅当资料页签可见且存在进行中任务时以 4s 轮询；离开页签或切换 Agent 立即停表。
- **迟到响应隔离**：`_knowledgeSourceEpoch` 递增守卫，换库／重载后到达的旧响应被丢弃，不重绘用户已离开的库。

## 3.4 验收证据

### 界面单元测试（真实 DOM + 真实三语词典）

命令：`node --test tests/test_knowledge_sources_frontend.cjs tests/test_knowledge_console_frontend.cjs tests/test_knowledge_agent_search_frontend.cjs tests/test_console_i18n_coverage.cjs tests/test_console_i18n_parity.cjs`

结果：`58 passed / 0 failed`（25 + 10 + 13 + i18n 10）。关键覆盖：

- 搜索与文档搜索互不干扰、无隐藏候选泄漏（服务器候选不被改写）、其他下拉保持原样、清空／无结果／键盘／IME。
- 上传能力门控、权限投影（无写权不出现入口、`knowledge.write` 授权不强制放行、租户管理员不越权写）。
- 列表／详情／下载／生命周期／重试／取消、逐项进度、同名冲突按项选择、本地过滤、轮询范围与迟到响应丢弃。

### 真实浏览器界面证据（保存搜索位置与资料闭环）

命令：`COW_KNOWLEDGE_BROWSER_OUTPUT=openspec/changes/add-traceable-knowledge-ingestion/evidence-3-ui NODE_PATH=$(npm root -g) node tests/test_knowledge_console_browser.cjs`

结果：`4 scenarios, 0 failed`；产物 `evidence-3-ui/results.json` 与两张截图 `sources-upload-panel.png`、`sources-narrow.png`。四个场景：

1. 桌面（浅色）：Agent 菜单搜索只收窄自身行、清空复原，且文档搜索框不受影响、不触发知识读请求；
2. 桌面（深色）：CJK 无匹配时给出空状态提示而不是空菜单；
3. 桌面：原始资料页签为完整闭环——列表 → 详情 → 上传面板，saved 与 searchable 不互相借用状态；
4. 窄屏（390×844）：资料闭环仍可用。

该脚本按生产方式装配 `chat.html`（展开 `<!--#include-->`、注入 `{{COW_*}}`），运行真实 `console.js`／CSS／i18n；运行期 `pageErrors=0`、`unexpectedRoutes=0`。脚本有 pytest 包装 `tests/test_knowledge_console_browser.py`：`NODE_PATH=$(npm root -g) .venv/bin/python -m pytest tests/test_knowledge_console_browser.py -q` → `1 passed`（无 Playwright 时按既有约定跳过）。

### 服务端与回归

- 命令：`.venv/bin/python -m pytest tests/ -k "knowledge or memory or console or tenant_read or agent_admin or agent_registry or web_console or knowledge_console" -q` → `838 passed, 2 failed`；其中 2 项失败为既有环境依赖（`test_personal_console_frontend.py`、`test_weixin_qr_flow.py`），在 clean HEAD 基线上同样失败（基线同两文件 `23 failed`），与本次改动无关。
- 知识专项：`.venv/bin/python -m pytest tests/ -k "knowledge" -q` → `152 passed`。
- `python scripts/check-route-coverage.py` → `OK`（202 routes, 247 method entries）。
- `python scripts/check-web-module-seams.py` → `OK`（0 findings）。
- `python scripts/check_change_deltas.py` → `OK`。
- `openspec validate add-traceable-knowledge-ingestion --strict` → `Change 'add-traceable-knowledge-ingestion' is valid`。

## 已知边界（不宣称完成）

- **真实上传/转换闭环**：页面已可发起上传与转换请求，但端到端「上传 → 转换 → 可检索 → 回答点击原件」依赖阶段 C（任务 4、5）完成；生产转换保持关闭。
- **既有失败**：`node --test tests/*.cjs` 全量运行有 10 项失败、clean HEAD 基线为 20 项，均为登录／外观／个人台等既有问题（含同一批用例），与本阶段无关；本阶段相关文件级命令全部 `0 failed`。
- **主题／语言证据形式**：三语由 i18n 覆盖与一致性测试证明；深浅色与窄屏由真实浏览器场景与截图证明（深色场景覆盖菜单空状态，浅色场景覆盖资料闭环）。
