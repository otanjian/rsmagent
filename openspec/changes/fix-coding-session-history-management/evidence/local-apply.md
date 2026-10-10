# fix-coding-session-history-management 本机实施与验证记录

日期：2026-10-10。范围：仓库内可实施代码、测试与文档；**不包含**目标部署（`rd.rsmxm.com.cn`）的归档／恢复／删除线上抓包、真实授权账号登录与配套 OpenCode 端到端验收（见文末“未完成”）。

## 代码改动

### 前端 `channel/web/static/js/console.js`

- 新增共享的**按操作／目标 Agent／会话**在途去重集合 `_sessionActionsInFlight` 与 `_sessionActionKey`，以及可视忙碌标记 `_sessionMarkBusy`（同时作用于普通列表行与归档弹窗行）。三者放在归档段之前，供归档、恢复、删除共用。
- `archiveSidebarSession`、`restoreArchivedSession`：请求前登记在途键并标记忙碌行；应答（含 HTTP／正文失败）后解除。正文解析失败以 `{}` 兜底，不再让 `r.json()` 的拒绝吞掉业务结果。恢复额外记住发起时的归档弹窗 body 与身份上下文，迟到的应答不写进已关闭／重开的弹窗。
- `deleteSession` 拆为“确认 + 未保存保护”与 `submitSessionDelete`：
  - 未保存内容保护在**提交请求前**运行，取消则不发送任何删除（`wsGuardUnsaved(() => submitSessionDelete(...))`）。
  - 成功后重新校验身份上下文与当前选中会话：用户已切换会话／身份时只刷新列表，不强制跳转、不关闭后来打开的内容。
  - 有相邻会话走既有 `switchSession`，离开编码视图用 `leave({skipConfirm:true})`（确认已在请求前完成，不再重复询问）。
  - 无相邻会话且目标是编码智能体时进入 `enterCodingEmptyState`；普通会话仍走原来的 `newChat(false)` 未持久化新对话。
- `enterCodingEmptyState` / `renderCodingEmptyState`：关闭编码视图、清除已删除会话引用、隐藏普通输入区、渲染 `#coding-empty-state`（说明 + 唯一的显式「新建编码会话」按钮，经既有 `newChat → seam.open` 创建），保留编码智能体选中状态。
- `guardCodingMessage`：选中编码智能体但没有挂载编码视图时**直接拒绝**普通发送（含恢复的文本／语音回调），不再代用户 `openCodingSession`；草稿保留并提示 `coding_send_in_pane`。空态下唯一创建远端会话的入口是显式新建。
- `leaveCodingSession(options)`：转发 `options` 给 `coding.js` 的 `leave`。

### 前端样式与文案

- `channel/web/static/css/coding.css`：新增 `.coding-empty-state/-title/-hint/-new`（含深色）。
- `channel/web/static/css/console.css`：新增 `.session-item.busy, .archived-session-row.busy`（降透明度 + `pointer-events:none`）。
- `channel/web/static/js/i18n/coding.js`：新增 `coding_empty_title` / `coding_empty_hint` / `coding_empty_new`（zh／en／zh-Hant）。

### 后端删除分流

- `agent/coding/__init__.py`：新增 `CODING_LINK_UNAVAILABLE`，与 `CODING_NOT_LINKED` 区分。
- `agent/coding/sessions.py`：新增 `link_for_delete`，让读取异常向上传播（`link_for` 的吞异常语义保持不变，仅用于必须不失败的管理探测）。
- `agent/memory/conversation_store.py`：新增 `session_exists`（只回答“该 Agent 的 `sessions` 是否仍有该行”）。
- `channel/web/fork/handlers/sessions.py`：新增 `_agent_is_coding` 与 `_coding_link_for_delete`，并在 `SessionDetailHandler.DELETE` 用后者替代 `_coding_link`：
  - 关联读取异常 → `500 coding_link_unavailable`，保留记录，不降级为普通清理；
  - 编码智能体且记录仍存在但无关联 → `409 coding_not_linked`，保留记录；
  - 有效关联 / 普通会话 / 已确认不存在的重复删除 → 沿用原契约（含幂等与项目文件保留）。

## 本机回归结果

命令固定为 `.venv/bin/python -m pytest <子集> -q -p no:randomly`（后端）与 `node --test`（前端）。

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_coding_session_routes.py tests/test_session_archive.py tests/test_session_resurrect.py tests/test_session_idor_closure.py tests/test_session_store_resolution.py tests/test_cancel_session_agent_scope.py tests/test_conversation_store_long_sessions.py tests/test_coding_page_assets.py` | 96 通过 |
| `pytest tests/test_web_console_assets.py tests/test_coding_page_assets.py tests/test_session_archive.py tests/test_session_resurrect.py` | 31 通过 |
| `node --test tests/test_sidebar_session_archive_frontend.cjs tests/test_session_delete_coding_frontend.cjs tests/test_session_panel_frontend.cjs tests/test_session_history_frontend.cjs tests/test_session_history_refresh_frontend.cjs tests/test_console_i18n_coverage.cjs tests/test_coding_frontend.cjs tests/test_channel_workbench_frontend.cjs` | 149 通过 |
| `node --check channel/web/static/js/console.js` | 通过 |
| `openspec validate fix-coding-session-history-management --strict` | valid |

### 既有失败（非本次引入）

以下失败在**改动前**同样存在（已用 `git stash` 回退 `console.js` / `chat.html` / `i18n/coding.js` / `coding.css` / `console.css` 后复测同名失败），按约定不顺手修复：

- `tests/test_agent_chat_launch_frontend.cjs` 2 项（`new-chat-caret` 显隐与 presentation switch）。
- `tests/test_console_i18n_parity.cjs` 3 项（合并命名空间快照、每命名空间各语言同键、命名空间加载顺序）。

## 新增／调整覆盖

后端（`tests/test_coding_session_routes.py`）：

- `test_a_link_read_failure_refuses_the_delete_and_keeps_the_record`：关联读取抛错 → 500 `coding_link_unavailable`，记录保留，未调用远端删除。
- `test_a_coding_record_without_its_link_refuses_the_delete`：删除关联行制造不一致 → 409 `coding_not_linked`，记录保留。

前端（`tests/test_session_delete_coding_frontend.cjs`，8 项）：

- 删除最后一条当前编码会话：关闭视图进入空态、不创建远端会话、保留智能体、隐藏普通输入区。
- 相邻会话接续切换；取消未保存保护不发送删除；失败保留并提示原因；迟到应答不关闭后来打开的会话；普通会话仍走未持久化新对话。
- 行在应答落地前标记 `busy` 且重复点击只发一次，应答后释放。
- 无挂载编码视图时普通发送被拒绝、不创建远端会话。

前端（`tests/test_sidebar_session_archive_frontend.cjs`，新增 3 项）：

- 归档／恢复双击只发一次请求，应答后行恢复可操作。
- 迟到恢复应答不写进关闭后重开的归档弹窗。

前端（`tests/test_coding_frontend.cjs`）：更新“陈旧发送控件”用例，断言改为**拒绝**（不再创建 `/api/coding/sessions`，不落到 `/message`，提示编码入口）。

## 静态资源交付核验（本机）

应用 `http://127.0.0.1:9899` 返回 200，以下资源经服务端映射取到**改动后内容**：

| 资源 | 状态 | 标记 |
| --- | --- | --- |
| `assets/js/console.js` | 200 | `coding-empty-state` / `_sessionActionsInFlight` / `enterCodingEmptyState` 等 14 处 |
| `assets/css/coding.css` | 200 | `.coding-empty-state` |
| `assets/css/console.css` | 200 | `.archived-session-row.busy` |
| `assets/js/i18n/coding.js` | 200 | `coding_empty_*` 9 处 |

复现：`curl -s http://127.0.0.1:9899/assets/<path> | grep -c <标记>`。第一方资源仍由 `channel/web/core/template.py` 逐文件 mtime 版本戳缓存，本次未改资源清单。

## 未完成（需目标环境，未勾选）

- 1.1：目标部署归档点击的**实际失败层**（请求是否发出、平台会话 ID／`agent_id`、响应状态、刷新结果、fork 装配与资源版本）尚未采集；本机无该部署访问。归档链路的本地修复见 `console.js` 在途去重与失败提示，具体线上根因仍待证据。
- 3.2：未用真实授权账号登录控制台，未用配套 OpenCode 做“归档 → 恢复 → 删除”端到端；本机 OpenCode Web（`127.0.0.1:4096`）当前不可达，无法验证 `opencode.enabled` 关闭／服务离线场景。登录页需凭据，本机未提供。
- 生产确认单独如实标记：**不**将本机测试与资源交付等同于目标部署问题已解决。
