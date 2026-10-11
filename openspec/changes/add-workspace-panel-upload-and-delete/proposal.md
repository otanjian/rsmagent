## Why

控制台对话页右侧工作区面板（「文件」页签）目前只能**读取**：列举、搜索、预览、编辑。把文件带进工作区只有一条绕路——拖到中间的聊天区变成**附件**（`channel/web/static/js/console.js` 在 `#view-chat` 上的 drop 处理 → `chat/state.js` 的 `handleFileSelect`），落点是该 Agent 的 `uploads/`，不是用户正在浏览的目录，也不保留文件夹层级。用户于是只能在「我要引用它」和「我要归档它」之间二选一。

既有的附件通道还有两个特性，使它无法承担「把一批资料放进当前目录」：

1. 目录上传的落点是 `uploads/webdir_<id>/<根目录>/…`（`channel/web/fork/runtime.py:1421-1468`），与面板当前目录无关，文件名还带 `webdir_` 前缀；
2. 上传用 `fetch()` 提交（`chat/state.js:794-830`），而 `fetch` 没有上传进度事件；失败分支静默 `splice` 掉占位项，用户看到的是「没反应」。

删除侧则是**完全空白**：全仓库没有任何针对工作区文件的删除端点（`workspace.py` 里唯一的 `DELETE` 属于 `ProjectManageHandler`，删的是项目而非文件）。面板能增不能减，用户清理自己的文件只能绕道执行层。

本次补齐面板的**写入面**，并且明确划定它的边界：

1. **上传**——把文件或整个文件夹拖进面板，落点是**面板当前浏览目录**且必须是**调用者本人的可写范围**，越界即拒绝并给出可读原因；保留目录结构，带实时进度与超限提示。
2. **删除**——在**同一范围**内删除单个文件或目录（含递归），经**回收站**软删除，可浏览、可恢复到原位置、可彻底删除。

「本人可写范围」按 Agent 可见性分两支，与面板**现有的落点规则**天然对齐（`land-shared-agent-panel-on-own-files` 已经把共享 Agent 的面板锚在成员本人的 `user/<user_id>`）：私有 Agent 为其工作区整棵树，租户共享 Agent 为其工作区下的 `user/<调用者 user_id>` 子树。本次不改落点规则，只让面板在**它已经停住且确实属于本人的那个目录**上可写，并拒绝其余位置。

## What Changes

- 新增 `POST /api/workspace/upload`（multipart）：`session` / `agent` 与其它工作区路由同构，`dir` 为目标相对目录，`relative_path` 为该文件在 `dir` 之下的相对路径，一个 `file` 部分。**一次请求只搬一个文件**——这是「一个 200MB 文件」与「5000 个小文件」共用一条判定、重试与进度路径的前提，也让服务端的内存占用与单文件大小解耦。服务端 SHALL 按调用者的有效租户与归属规则解析 `dir` 与相对路径，MUST NOT 因请求体自报的 agent 标识或路径扩大范围。
- 上传落点 SHALL 被**双重判定**：既有的归属规则（不可见即不可写）与「本人可写范围」都放行才成立。越界 SHALL 以稳定错误码拒绝，MUST NOT 落盘、MUST NOT 静默回落到任何「就近可写」目录。`user_id` MUST 取自已验证身份。
- 落盘 SHALL 逐级拒绝符号链接、`..`、绝对路径、盘符与 NTFS 数据流；同名冲突 SHALL 按 `name (1).ext` 自动重命名，MUST NOT 覆盖既有文件；写入 SHALL 原子且失败不留下半截文件。
- 重试 SHALL 只重传未成功项（响应逐项报告结果，客户端据此重传失败项）；服务端 MUST NOT 为跨请求去重保留任何状态，也不承诺重复提交的幂等。
- 面板「文件」页签 SHALL 成为拖拽落点：拖入文件或文件夹即上传到**当前浏览目录**，所含文件的相对路径 SHALL 保真（空目录不保留）；落点不可写时 SHALL 在拖入前就可见地表明；拖入面板 MUST NOT 触发聊天附件上传。
- 目录遍历 SHALL 用 `DataTransferItem.webkitGetAsEntry()` 递归，并循环 `readEntries()` 直到返回空（该 API 每批至多 100 条）；浏览器不支持目录拖入时 SHALL 提示改用文件夹选择。
- 上传 SHALL 显示**准确实时进度**（已传字节 / 总字节、项数、百分比）。进度 SHALL 用 `XMLHttpRequest.upload.onprogress`（`fetch` 没有上传进度事件）；显示字节 SHALL 按该文件字节与 `event.total` 的比例换算，MUST NOT 直接用含 multipart 开销的请求体长度；总字节 SHALL 在遍历完成后才确定；存在失败时 MUST NOT 显示 100%。并发数与分块粒度属实现细节。
- 容量上限：单文件 200MB（服务端权威复核）、单次拖拽 ≤5000 个条目且 ≤5GB（客户端在清单确定后、发送前判定）。超限项 SHALL 被跳过并在汇总中逐项提示（路径 + 大小 + 原因），其余文件 SHALL 继续上传；单次拖拽级上限是体验护栏而非安全边界，MUST NOT 被表述为安全保证。
- 新增 `POST /api/workspace/delete`：在本人可写范围内软删除**文件或目录（含递归）**，一次请求可含多个条目，逐项报告结果。删除范围 SHALL 与上传范围同源但更严——私有 Agent 工作区根第一层的 Agent 运行内部（`AGENT.md`、`knowledge/`、`memory/`、`scheduler/`、`skills/`、`subagents/`、`system/`、`tmp/`、`websites/`、`plans/`）、`user/` 容器、调用者本人的 `user/<本人 user_id>` 目录本身、回收站本身与可写范围根自身 MUST NOT 可删；运行内部的判定 SHALL 只作用于工作区根第一层，MUST NOT 递归到本人目录内的同名条目。该保护 SHALL 只约束删除、不约束上传（上传是追加，删除是抹除）。
- 删除 SHALL 是**软删除**：条目 SHALL 以同文件系统内的原子重命名移入 `<workspace>/user/<本人 user_id>/.trash/`，MUST NOT 在原地直接销毁；无法原子移动的情形 SHALL 以该项失败报告，MUST NOT 退化为「先复制再删除」。回收站 SHALL 不出现在列举与搜索中（**不依赖 `.trash` 的点前缀**——`show_hidden` 是客户端可达的参数，必须由显式准入规则排除），且 MUST NOT 作为上传落点或删除目标。删除 SHALL 有二次确认，确认文案 SHALL 包含条目数与体积。
- 新增 `GET /api/workspace/trash`、`POST /api/workspace/trash/restore`、`POST /api/workspace/trash/purge`：浏览本人回收站、恢复到原位置、彻底删除。三者 SHALL 均只作用于**调用者本人**的回收站，批次标识 MUST NOT 参与路径拼接。恢复 SHALL 重新校验目的地仍在当时的可写范围内且未变为不可删集合中的条目（Agent 可见性可能在删除之后改变），不合规时拒绝并说明原因；原位置被占用时 SHALL 以可用新名落盘并如实报告实际路径，MUST NOT 覆盖、MUST NOT 静默失败。回收站 SHALL 有保留期限清理点（实现常量），MUST NOT 无限积累。
- 服务端写盘 SHALL 为分块流式并校验实际写入字节数（新端点自带这条路径，不沿用 `_read_uploaded_file_bytes()` 的 `.read()` 一次性读取，`channel/web/fork/runtime.py:607`），使 100–200MB 的单文件在并发上传下不放大内存占用。既有 `/upload` 附件通道的读法本次不改。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `platform-file-browsing`:
  - ADDED「工作区面板支持把文件与文件夹拖拽上传到本人目录」——落点必须是本人可写范围（私有 Agent 整根 / 共享 Agent 的 `user/<本人 user_id>`）、越界拒绝且拖入前可见、层级保真、与聊天附件落点互不干扰、目录遍历与不支持时的兜底。
  - ADDED「工作区上传的落盘边界、归属与冲突处置」——归属与范围双重判定、每条相对路径的逃逸判定、同名自动重命名、原子写入，以及「重试只重传失败项」的契约（服务端无状态）。
  - ADDED「工作区上传的容量上限、超限提示与实时进度」——单文件上限由服务端无状态复核、单次拖拽两项由客户端判定，逐项超限提示且其余继续、三层进度的准确性契约与失败汇总。
  - ADDED「工作区面板支持在本人可写范围内删除条目，且可经回收站恢复」——软删除与回收站位置、不可删集合（工作区根第一层的运行内部 + `user/` 容器与本人目录本身 + 回收站 + 范围根）、运行内部判定不递归、恢复的目的地重校验、回收站三条路径的本人作用域与保留期限清理点。

既有「租户与 Agent 工作区文件服务按作用域隔离」的正文本次不改：新端点的作用域规则正是由该要求定义，本次只引用不复述（避免复制另一处责任域的规范）。

## Impact

- **行为受影响**：面板「文件」页签新增拖拽上传与删除（含回收站）；上传期间面板显示进度；落盘/删除后刷新当前目录并把新条目排在可见处；面板在本人范围之外的目录上不再静默可写（例如共享 Agent 的共享根会明确拒绝并提示）。
- **不受影响**：聊天区拖拽仍是附件上传（`console.js` 的 `#view-chat` drop 处理与 `handleFileSelect` 不改，本次只在面板落点上阻断冒泡）；`/upload` 的附件与目录上传契约；既有 `GET /api/workspace/tree|search|resolve|meta|read`、`POST /api/workspace/write` 与 `POST /api/workspace/user-dir` 的契约；`user/<user_id>` 归属规则本身；执行层（Python/Shell/技能脚本/coding）边界；桌面端 `WorkspacePanel`。
- **代码面**：`channel/web/fork/handlers/workspace.py`（新增 `WorkspaceUploadHandler`、`WorkspaceDeleteHandler`、`WorkspaceTrashHandler`、`WorkspaceTrashRestoreHandler`、`WorkspaceTrashPurgeHandler`，以及范围判定 seam `_write_scope` / `_resolve_write_target`、不可删集合的准入共用面与「逐条可删」注解 `_annotate_deletable`）；`common/safe_fs.py`（**新增两个破坏性原语** `rename` 与 `remove_tree`——既有原语只有 `mkdir`/`write_bytes_atomic`/`write_text_atomic`/`unlink`，没有 move 也没有递归删除，两者都需同样的逐级 `O_NOFOLLOW` 加固）；`agent/workspace/service.py`（新增 `ensure_dir` / `write_bytes` / `available_name` / `entry_size` / `trash_root_rel` / `move_to_trash` / `list_trash` / `restore_from_trash` / `purge_trash` / `cleanup_trash` 与保护集 `undeletable_reason` / `unuploadable_reason`，消费上述 `safe_fs` 原语，MUST NOT 另写路径校验；`list_dir` / `search` 的准入面显式排除回收站）；`channel/web/web_channel.py`（导入五个新 handler）；`channel/web/route_registry.py` 与 `scripts/route-baseline.txt`（五条路由入册、按序插入并更新冻结基线——`check_route_coverage` 会内省比对注册方法集合与 handler 实际方法）；`channel/web/static/js/workspace.js`（落点与可写态提示、遍历、逐文件发送与 XHR 进度、结果汇总与刷新、删除入口与二次确认、回收站视图、恢复与彻底删除）；`channel/web/templates/views/chat.html`（工具栏按钮、进度卡片、拖拽提示）；`channel/web/static/css/console.css`（高亮、进度、回收站与确认样式——注意 `static/css/workspace.css` 并未被 `chat.html` 引用，样式必须落在 `console.css`）；`channel/web/static/js/i18n/core.js` 三语文案并同步 `tests/fixtures/console_i18n_snapshot.json`。注意 **不要**改到 `channel/web/api/workspace.py`：那是上游实现，只被 `build_app()` 使用，fork 的活体应用是 `build_web_app()`。
- **配置面**：**本次不新增任何配置项**。服务端上限（`WS_UPLOAD_MAX_BYTES=200MB`、`WS_DELETE_MAX_TARGETS=2000`、`WS_UPLOAD_CHUNK_BYTES=1MB`）与前端常量（5000 个条目、5GB、深度 32）以及回收站保留期限（30 天）全部取固定常量，不登记 `config-template.json`——没有「不同部署需要不同上限」的证据，做成配置只会多一处需要登记与验证的读入口。无数据库变更、无新增服务端持久状态（回收站是文件系统数据，不是数据库行）、无前端依赖新增。
- **部署面**：反向代理的 `client_max_body_size` 须不小于**单文件上限** + 余量（建议 ≥210m），上传超时相应放宽；系统临时盘须能容纳**单个**上传请求体（web.py 的 multipart 会把上传部分先落到临时文件，因此临时占用与文件数无关）；回收站会占用磁盘，容量规划须把「可恢复窗口内的删除量」计入。仓库内没有随代码下发的代理配置（已确认），前二项列为部署验收项。
- **测试面**：新增 `tests/test_workspace_upload_delete.py`（归属、范围越界、逃逸、冲突重命名、单文件上限、流式写入的字节校验、重复提交按同名冲突处理且不留半截文件；删除与回收站：工作区根不可删集合逐项、本人目录内同名条目不受保护、递归目录删除、原子移动与 `EXDEV` 失败不留复制残留、**回收站隐藏不依赖点前缀**（`show_hidden=1` 时也不出现）、`.trash` 不可作为落点或删除目标、逐条状态与批次清空、恢复目的地重校验、占用时改名恢复、彻底删除只作用于本人回收站）与 `tests/test_workspace_trash.py`（回收站原语与保留期限清理）；新增 `tests/test_console_workspace_upload_frontend.cjs`（落点、可写态提示、层级保真、逐文件请求与进度换算、超限提示、重试只含失败项、不支持目录 API 的兜底、切换范围后旧落点结果不落进新目录）与 `tests/test_console_workspace_delete_frontend.cjs`（删除入口、锁定行排除、二次确认文案含条数与体积、回收站视图、按批次恢复与彻底删除、清空）。前端用例为既有 VM 上下文补 `XMLHttpRequest` 与 `FormData` 替身。回归 `tests/test_safe_fs.py`（新增的 `rename` / `remove_tree` 两个原语）、`tests/test_console_workspace_frontend.cjs`、`tests/test_agent_user_file_access.py`、`tests/test_agent_user_file_http.py`、`tests/test_private_agent_file_scope.py`、`tests/test_object_scope.py`、`tests/test_route_registry.py`、`tests/test_console_i18n_parity.cjs`、`tests/test_console_i18n_coverage.cjs`。
