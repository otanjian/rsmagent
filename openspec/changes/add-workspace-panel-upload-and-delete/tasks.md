## 1. 现状盘点与依赖确认

- [x] 1.1 盘点面板读路径的既有 seam 与可复用件：`_workspace_request_scope`、`_workspace_path_allowed`、`_db_path_visible`、`_authorize_db_file_path`、`require_management_write`；确认新端点的作用域规则完全由「租户与 Agent 工作区文件服务按作用域隔离」定义，本次不新增也不放宽任何授权。
- [x] 1.2 确认可写范围判定的复用面：`common/state_dir.classify_agent_user_path(real_path, workspace)` 的四态语义与它现有的消费点（`_db_path_visible`、`_authorize_db_file_path`、`migrate_agent_user_files.py`）；确认「私有还是共享」取自 `land-shared-agent-panel-on-own-files` 已经在工作台投影上补出的 `visibility`，本次不新查一次归属。
- [x] 1.3 确认 `agent/workspace/service.py` 的既有能力边界（现在只有 `write_text`，无二进制写、无建目录、无移动），确定新增 `ensure_dir` / `write_bytes` / `move_to_trash` / `restore_from_trash` / `list_trash` / `purge_trash` 的最小面，并确认它们走 `common/safe_fs.py` 的 `mkdir` / `write_bytes_atomic`（逐级 `O_NOFOLLOW`）。
- [x] 1.4 确认面板落到工作区根第一层时的候选条目名单（用于不可删集合取全集，而不是凭记忆列举）：从 `agent/prompt/workspace.py` 的布局段（`_LAYOUT_AGENT_EN` / `_LAYOUT_ROOT_EN`）与 `ensure_workspace` 反查实际会出现的条目，与 `agent/workspace/service.py` 的 `SEARCH_DEMOTE_DIRS` 交叉核对。
- [x] 1.5 确认前端冲突点：`console.js` 在 `#view-chat` 上的 drop 处理会接管所有 `Files` 拖拽；面板与 `#chat-main` 同属其子节点。据此确定面板落点必须 `preventDefault + stopPropagation`。
- [x] 1.6 确认样式载体与模块归属：`chat.html` 只链接 `assets/css/console.css`，`static/css/workspace.css` 无任何页面引用；`channel/web/api/workspace.py` 是上游实现（只被 `build_app()` 用），fork 的活体入口是 `build_web_app()` 与 `channel/web/fork/handlers/`。两处都容易改错且改错不报错。
- [x] 1.7 确认部署侧两项前置（仓库内无随代码下发的代理配置）：`client_max_body_size` 与上传超时、系统临时盘容量；并登记回收站占盘的容量规划输入（本阶段只登记，实施参数见阶段 7）。**实测（evidence §7）：本机 nginx（`C:\nginx\conf\nginx.conf`）`http` 块为 `100M`，低于单文件上限 200MB，属**未满足**的前置条件，修法是运维侧把第 27 行提到 ≥210M；临时盘与回收站占盘数值见 evidence §7.2/§7.3。**

## 2. 服务端：可写范围 seam 与上传端点

进入本阶段前复核阶段 1 的作用域与不可删集合结论。

- [x] 2.1 抽出可写范围的单一判定 seam（例如 `_workspace_writable_scope(ctx, agent_id)` 或返回判定闭包的等价形态）：私有 Agent 为工作区整棵树，租户共享 Agent 为 `user/<本人 user_id>` 子树；越界返回稳定错误码 `out_of_scope`。该 seam 同时被上传与删除消费，MUST NOT 在两处各写一份。
- [x] 2.2 `agent/workspace/service.py` 新增 `ensure_dir(dir_rel)` 与 `write_bytes(dir_rel, rel, data)`：目标解析复用既有 `resolve` 边界，写入走 `safe_fs.write_bytes_atomic`，逐级符号链接与越界一律拒绝。目录按文件路径的需要惰性创建（空目录不保留）。
- [x] 2.3 新增 `WorkspaceUploadHandler`（`POST /api/workspace/upload`）：走 `_db_scope()` 与统一管理写闸门；`session` / `agent` 复用 `_workspace_request_scope`；解析 `dir` 后必须先过归属判定与 2.1 的范围判定，两者缺一即拒（「列不出来就写不进去」）。
- [x] 2.4 每条 `relative_paths[i]` 做两道校验：清洗（拒绝 `..`、绝对路径、盘符、`:` 数据流与空段）、拼接于 `dir` 之下后按最终真实路径确认仍在目标目录内；任一条不合规 SHALL 以该项失败返回，MUST NOT 使整批静默部分成功。
- [x] 2.5 同名冲突按 `name (1).ext` 递增自动重命名（服务端裁决、竞态安全），响应回传 `renamed[]`；MUST NOT 覆盖既有文件，MUST NOT 静默跳过。
- [x] 2.6 响应逐项报告（`saved` / `renamed` / `skipped` / `errors` 带稳定 code），使客户端能只重传失败项；服务端 MUST NOT 实现批次幂等记录、TTL 或跨请求去重状态（重复提交按同名冲突处理）。
- [x] 2.7 写盘改为分块流式并校验写入字节数（不匹配判该项失败）；MUST NOT 沿用 `_read_uploaded_file_bytes()` 的一次性 `.read()` 路径处理大文件。
- [x] 2.8 服务端以**无状态**方式独立复核单批相关上限：单文件大小、单批条目数、单批字节；超限 SHALL 以稳定 code 与可读原因返回，MUST NOT 返回含糊的 500。MUST NOT 引入按拖拽累计的跨请求状态。
- [x] 2.9 在 `route_registry.py` 登记路由（fork 段、`tenant` 策略）并使 handler 可从 `web_channel.py` 取得；更新 `scripts/route-baseline.txt`（按序插入新行），使 `test_route_registry.py` 的冻结基线等值校验通过，且 `check_route_coverage` 的方法集合内省比对通过。
- [x] 2.10 后端用例 `tests/test_workspace_upload_delete.py`：归属与范围（跨租户、他人私有 Agent、他人 `user/<id>`、共享 Agent 的共享根、不可见 `dir` 一律拒绝且无文件副作用）、逃逸（`..`、绝对路径、盘符、符号链接目录、检查后替换）、冲突重命名、重复提交按同名冲突处理且不留半截文件、单文件上限（服务端权威）与单次拖拽上限（前端护栏，见 4.5）、流式写入的字节校验、文件名（中文/超长/特殊字符）。

## 3. 服务端：删除与回收站

进入本阶段前确认 2.1 的范围 seam 已抽出并被删除路径复用（MUST NOT 复制一份）。

- [x] 3.1 定义不可删除集合与判定（沿用 `agent/knowledge/service.py` 的 `PROTECTED_FILES` + `_ensure_not_protected` 形态），共四类：① 工作区根第一层的 Agent 运行内部——`AGENT.md`、`USER.md`、`RULE.md`、`BOOTSTRAP.md`、`MEMORY.md` 与 `memory/`、`scheduler/`、`tmp/`、`knowledge/`、`skills/`、`websites/`、`subagents/`、`system/`、`plans/`；② 工作区根的 `user/` 容器与调用者本人的 `user/<本人 user_id>` 目录本身（后者是回收站父目录，删它会把回收站一起带走）；③ 回收站目录本身；④ 可写范围根自身。①的判定 MUST NOT 递归——本人 `user/<uid>/` 内的同名条目不受保护。共享 Agent 的可写范围够不到工作区根，①对其不适用。保护 MUST NOT 施加到上传路径上（上传是追加、删除是抹除，见 design D13）。
- [x] 3.2 `common/safe_fs.py` **新增两个原语**（既有原语不够用：只有 `mkdir` / `write_bytes_atomic` / `write_text_atomic` / `unlink`，没有 move 也没有递归删除）：`rename(root, src_rel, dst_rel)`（源与**目标两侧**的父目录链都要走同一套逐级 `O_NOFOLLOW`；跨设备 `EXDEV` 显式抛出而不是自行降级）与 `remove_tree(root, rel)`（递归删除，MUST NOT 跟随符号链接）。二者是删除链路唯一允许的破坏性原语，MUST NOT 在 `service.py` 里另写一份路径校验（注意既有的 `_replace_atomically` 就是一处自己实现的写路径，不要照它再加一个自己实现的移动）。
- [x] 3.3 `service.py` 新增回收站原语：`move_to_trash(scope_root, rel_path)`（同文件系统原子重命名到 `.trash/<批次 id>/files/<原相对路径>`，走 3.2 的 `rename`）、`list_trash`、`restore_from_trash`、`purge_trash`（走 3.2 的 `remove_tree`）。批次目录含一份元信息记录 `created_at` 与整批条目的原相对路径、类型、字节数与**逐条状态**（在回收站 / 已恢复 / 已彻底删除）——逐条状态是必需的，否则「恢复某条后列表里不再保留该项」无法实现、同一条也可能被恢复两次；元信息写完后再判断是否整批已清空，清空则移除批次目录，避免留下空壳。
- [x] 3.4 新增 `WorkspaceDeleteHandler`（`POST /api/workspace/delete`）：走统一管理写闸门与 `_workspace_request_scope`；逐条判定「在本人可写范围内」且「非 3.1 的四类不可删集合」且「非回收站」，任一不成立即该项失败并带稳定 code（`out_of_scope` / `protected_path` / `trash_not_targetable` / `not_found`）；支持文件与目录（含递归），单请求条目数受 `max_items_per_delete` 限制。
- [x] 3.5 新增 `WorkspaceTrashHandler`（`GET /api/workspace/trash`）：只列本人回收站，返回每条的原相对路径、删除时间、类型与字节数；MUST NOT 列出他人回收站，MUST NOT 暴露工作区绝对路径。已恢复/已彻底删除的条目不出现。
- [x] 3.6 新增 `WorkspaceTrashRestoreHandler`（`POST /api/workspace/trash/restore`）：批次标识只能用于在**本人**回收站内选中条目，MUST NOT 参与路径拼接（越权在构造上不可达）；恢复前**重新校验**目的地仍在当时的可写范围内且未变为不可删集合中的条目（Agent 可见性可能在删除之后改变），不合规即拒绝该项并说明原因；原位置被占用时按 `name (1).ext` 递增改名落盘并在结果中报告实际路径，MUST NOT 覆盖占用者，MUST NOT 静默失败；成功后把该条状态写回元信息。
- [x] 3.7 新增 `WorkspaceTrashPurgeHandler`（`POST /api/workspace/trash/purge`）：只销毁**本人**回收站内被选中条目（含清空），MUST NOT 触及回收站之外的任何路径；成功后把状态写回元信息。
- [x] 3.8 回收站对读路径隐藏：在 `list_dir` 的 `allow_entry` 与 `search` 的 `allow_dir` 上**显式**排除回收站容器——MUST NOT 依赖 `.trash` 的点前缀，因为 `show_hidden` 是客户端可达的参数（`handlers/workspace.py:132,149` 透传 `params.show_hidden == '1'`），点前缀只在默认调用下生效。两个 seam 与既有的他人 `user/<id>` 过滤是同一处，不新增过滤层。`meta()` 不必处理（它只返回 root/exists/server_time，不枚举条目）。另外确认 `.trash` MUST NOT 可作为上传落点或删除目标（由 3.1 与 3.4 覆盖）。
- [x] 3.9 回收站保留期限与清理点：保留期限为**实现常量**（意向 30 天，具体数值见 design Open Question 3），清理只按批次目录的修改时间判定，MUST NOT 跟随 `.trash` 内可能存在的符号链接；挂到既有清理时机，不新增调度器依赖。
- [x] 3.10 五条路由补入 `route_registry.py` 与 `scripts/route-baseline.txt`（与 2.9 同一处，按序插入）。
- [x] 3.11 后端用例：`tests/test_workspace_upload_delete.py` 覆盖 HTTP 面（范围越界（共享根、他人 `user/<id>`、他人私有 Agent）、工作区根不可删集合逐项拒绝（含 `user/` 容器与本人 `user/<id>` 目录本身——确认回收站不被一起移走）、本人目录内同名条目不受保护、**回收站隐藏不依赖点前缀**（带 `show_hidden=1` 列举与搜索均不出现 `.trash`）、`.trash` 不可作为落点或删除目标、恢复目的地因可见性收窄而拒绝、占用时改名恢复并报告实际路径、彻底删除只作用于本人回收站（他人批次标识无效））；`tests/test_workspace_trash.py` 覆盖原语面（递归目录删除后原位置不存在且回收站内结构完整、原子移动不留半截状态、**逐条状态**（恢复一条后列表只少那一条、同一条不能恢复两次、整批清空后批次目录被移除）、保留期限清理、`EXDEV` 时该项失败且不产生复制残留）。

## 4. 前端：落点、遍历与分批上传

进入本阶段前复核阶段 2 的响应契约（`saved` / `renamed` / `skipped` / `errors` 与稳定 code）。

- [x] 4.1 在 `workspace.js` 新增面板落点：绑在 `#ws-body-files`，`dragenter`/`dragover` 高亮并显示目标目录名，`drop` 时 `preventDefault + stopPropagation` 后进入上传流程；`dataTransfer.types` 不含 `Files` 时不做任何事（保持与「面板拖出到输入框」互不干扰）。
- [x] 4.2 落点可写态：依据当前浏览目录是否在本人可写范围内切换视觉态与提示语，`dragover` 阶段即表明会被拒，MUST NOT 等到 `drop` 后才告知。越界返回的 `out_of_scope` 要映射为可读文案，且与「目录不存在」区分开。
- [x] 4.3 目录遍历：在 `drop` 回调里**同步**把 `dataTransfer.items` 全部转成 `webkitGetAsEntry()` 句柄后再异步遍历；`readEntries()` 循环读到空数组；遍历并发限 8–16；深度（32）作为实现护栏常量，不进入用户可见契约。
- [x] 4.4 不支持目录拖入时（无 `webkitGetAsEntry`）给出本地化提示并引导使用既有文件夹选择；MUST NOT 静默只上传顶层文件。
- [x] 4.5 上限的前端体验层：遍历完成后逐项标记超限（单文件 200MB、单次条目数 5000、单次总量 5GB）并汇总提示（路径 + 大小 + 原因），其余文件继续上传；单次拖拽级超限在发送前拦截并说明上限与超出量（该判定是体验护栏，不得表述为安全保证）；服务端单批复核失败时如实呈现而非重试到死。
- [x] 4.6 单文件一请求、并发 3，用 `XMLHttpRequest` 以取得 `upload.onprogress`；显示字节按 `scale = 本文件字节 / event.total` 换算，累计显示已传字节/总字节、项数与百分比。并发数取 3（峰值 ≈ 并发 × 单文件上限，实现时在注释里写明该公式并保持可调）。断言单个 200MB 文件与 5000 个小文件走同一条路径（不出现「单文件独占一批」这类特例分支），且单个文件失败可被单独重传。
- [x] 4.7 进度语义：遍历阶段只显示「已发现 N 项」不做百分比；总字节在遍历完成后确定；存在失败时 MUST NOT 显示 100%，改为「部分完成」。
- [x] 4.8 结果汇总与刷新：完成后刷新当前目录并把结果（成功 / 重命名 / 跳过 / 失败）列在面板上，失败与跳过项可展开、可单独重试——重试 SHALL 只重传未成功项，MUST NOT 重传已报告成功的条目，也不依赖任何服务端幂等。上传期间允许切换目录，但落点在开始时已固定并在 UI 上可理解。

## 5. 前端：删除入口、二次确认与回收站视图

- [x] 5.1 删除入口：**面板没有右键菜单可以沿用**（`workspace.js` 里唯一的 `menu` 是聊天输入的 `mention-menu` 自动补全，与文件行无关）。**修订（见 evidence §8.4）**：最初按「工具栏一个删除按钮 + 行内勾选多选」实现，后改为**删除按钮就在该行上、位于文件名右侧**——勾选框是批量模型，而面板其余部分并不使用；且不可删的行只能「提供后拒绝」，与「MUST NOT 让用户点了才知道被拒」相冲突。现在：可删的行才渲染 `data-ws-act="delete"` 按钮，不可删的行不渲染任何动作、只带锁（原因在锁的 `title` 上），一次点击只作用于该行（请求体仍支持多条目，spec 未限定 UI 形态）。回收站入口仍放在工具栏 `.workspace-files-toolbar`。行内动作在行的自身点击**之前**处理，以免按删除的同时又打开了该目录。
- [x] 5.2 二次确认：确认文案 SHALL 包含**条目数与总体积**（不是笼统的「确定删除吗」），并说明会进入回收站、可恢复；目录删除的文案要显式说明「含其全部内容」。
- [x] 5.3 删除结果：逐项报告（成功 / 被拒 / 不存在），失败项不中止其余；完成后刷新当前目录，并在面板上给出「已移入回收站 N 项 · 打开回收站」的入口。
- [x] 5.4 回收站视图：列出条目（原相对路径、删除时间、类型、体积），每行提供**恢复到原位置**与**彻底删除**按钮（外加工具栏的「清空回收站」）；彻底删除的二次确认要说明不可恢复。恢复结果如实显示**实际落盘路径**（原位置被占用时会是新名）。**修订（见 evidence §8.4）**：与 5.1 同理，由勾选改为行内按钮，仍按 `batch_id` + `index` 寻址单个条目。
- [x] 5.5 样式落在 `console.css`：高亮、进度条/进度文案、可写态提示、删除确认、回收站视图；明暗主题各验证一次。

## 6. i18n 与文案

- [x] 6.1 `static/js/i18n/core.js` 补齐三语（zh / zh-Hant / en）文案：落点提示、落点不可写、遍历中、超限（单文件/条目数/总量）、进度（字节/项数/部分完成）、批次失败与重试、不支持目录拖入、删除入口与二次确认（含条数与体积）、删除失败原因（越界/属于不可删集合/不存在）、回收站标题与空态、恢复与彻底删除、恢复改名提示、保留期限与「可见性变化后无法恢复」的说明。
- [x] 6.2 同步 `tests/fixtures/console_i18n_snapshot.json`：只增不改，逐键复核增量后再重写；复跑 `tests/test_console_i18n_parity.cjs` 与 `tests/test_console_i18n_coverage.cjs`。

## 7. 配置与部署验收

- [x] 7.1 定档上限项：**全部取固定常量，不新增配置项**——服务端 `WS_UPLOAD_MAX_BYTES`（200MB）、`WS_UPLOAD_CHUNK_BYTES`（1MB，流式粒度，非用户契约）、`WS_DELETE_MAX_TARGETS`（2000，一个请求体能要求多少工作的上界；面板一次只能删除已渲染的行，而列举以 `MAX_ENTRIES=500` 封顶）；客户端单次条目数 5000、单次总字节 5GB、深度护栏 32；回收站保留期限 `TRASH_RETENTION_SECONDS` = 30 天。确认未引入任何服务端持久状态（回收站是文件系统数据，不是数据库行）。
- [x] 7.2 部署验收：代理 `client_max_body_size` ≥ 单文件上限 + 余量、上传超时放宽、系统临时盘余量（**单请求临时占用 = 一个文件的大小，与文件数无关**）；记录实测数值（以真实 HTTP 用例产出证据，而非仅声明）。**实测（evidence §7）：本机 nginx `http` 块为 `100M`，**低于** 200MB 单文件上限 —— 该前置条件在仓库内无法满足，属运维动作（把 `C:\nginx\conf\nginx.conf` 第 27 行提到 ≥210M），已在 evidence §7/§10 显式登记为未满足项。临时盘数值见 §7.2。**
- [x] 7.3 容量规划：把回收站占盘计入部署输入，说明「保留期限内累计删除量」的上界估算方式，以及容量紧张时缩短保留期限的操作路径。
- [x] 7.4 真实 Wire 用例：在真实 WSGI 应用上跑通——真租户/真会话下上传到本人目录、跨租户与他人 `user/<id>` 与共享根被拒且无副作用、代理阈值内的大文件成功、删除与恢复到原位置、彻底删除只作用于回收站、错误码可诊断。
- [x] 7.5 明确并记录失败模式：超过代理阈值（413）与超过服务端单文件上限、以及越界（`outside_own_directory`）三者的差异，前端提示能区分。

## 8. 回归、证据与交付

- [x] 8.1 运行并记录 `tests/test_workspace_upload_delete.py`、`tests/test_workspace_trash.py`、`tests/test_safe_fs.py`、`tests/test_console_workspace_upload_frontend.cjs`、`tests/test_console_workspace_delete_frontend.cjs`、`tests/test_console_workspace_frontend.cjs`、`tests/test_agent_user_file_access.py`、`tests/test_agent_user_file_http.py`、`tests/test_private_agent_file_scope.py`、`tests/test_object_scope.py`、`tests/test_route_registry.py`、`tests/test_console_i18n_parity.cjs`、`tests/test_console_i18n_coverage.cjs` 的输出。
- [x] 8.2 规模实测并记录：5000 个小文件 + 多个文件夹（结构正确、进度单调递增至 100%、文件数准确、请求数与文件数同阶）、单个 200MB 成功、201MB 被跳过且其余继续；进度显示字节与落盘总字节误差 < 1%。**实测（evidence §2/§3/§4）：5000 文件 / 12 文件夹 / 并发 3 / 227.9s，0 失败、0 结构错配、进度单调、末值误差 0.0；单个 200MB SHA-256 一致（167.1 MB/s）；200MB+1KB 以 `too_large` 被拒且无残留、同批其余文件照常落盘。**
- [x] 8.3 断网/中断重试验证：重试请求体只含未成功项，已成功项不重传（无需服务端状态）；并实测记录「服务端已写盘但响应丢失」这一残余情形下的实际表现（`(n)` 副本是否出现、出现概率），据此判断是否需要另立变更引入服务端幂等。**实测（evidence §9）：第二轮只带首轮失败的两项；重复落盘确实产生可见的 `ok-0 (1).txt` 并 `renamed: true`。副本的**出现概率**无法用进程内 WSGI 制造（需真实丢响应），已在 evidence §11 显式登记为未测量项；结论仍是无需服务端幂等——残余退化为「用户多拖了一次」，对用户可见且可自行清理。**
- [x] 8.4 删除相关的破坏性验证（在真实数据上做，不用替身）：误删目录后经回收站恢复，内容与结构逐项一致；确认「原位置被占用」时改名恢复不会覆盖占用者；确认范围根、不可删集合中的条目与回收站本身都删不掉。
- [x] 8.5 确认未波及面：聊天区拖拽仍是附件上传（`tests/test_console_upload_transport.py` / `tests/test_console_upload_frontend.cjs` 通过）、`/upload` 契约不变、`GET /api/workspace/*` 与 `write`/`user-dir` 契约不变、执行层（Python/Shell/技能脚本）路径不受影响。
- [x] 8.6 执行 OpenSpec 校验：`scripts/check_change_deltas.py add-workspace-panel-upload-and-delete`（ADDED 不重复既有或跨能力要求），并核对与 `platform-file-browsing` 既有要求、`land-shared-agent-panel-on-own-files` 的落点规则、`isolate-shared-agent-user-data` 的归属规则的合并语义，确认未扩大范围。**实测（evidence §6.1）：delta 半边无 finding（无 ADDED 重复既有/跨能力要求、无 MODIFIED 标题失配），本变更的 delta 是纯 `ADDED`（向 `platform-file-browsing` 追加 4 条，既有 3 条一字未动）；默认基线下的 3 条 finding 全部属于上游同步基线的 `seam:` 行（`fork-decoupling-and-tenant-hardening` / `adopt-upstream-web-split` 分别以该基线通过 `OK (applied)`），不属本变更范围，故不为其改名或改写文档以「刷绿」检查。**
- [x] 8.7 产出 `evidence.md`：用例输出、真实 HTTP 与规模实测数据、删除/恢复的破坏性验证记录、部署侧实测数值，以及本次未做（分片/断点续传/GB 级单文件、移动与重命名、版本历史）的显式边界说明。
