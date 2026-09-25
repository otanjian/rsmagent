# isolate-shared-agent-user-data 验收证据

日期：2026-09-24。记录本 change 的红/绿核验、真实 HTTP 与真实浏览器授权验证、迁移演练与规范校验输出。

## 1. 规范校验

```
$ openspec validate isolate-shared-agent-user-data --strict
Change 'isolate-shared-agent-user-data' is valid
```

相关 change 的合并语义（任务 5.5）：唯一与本次共用 capability 的在办 change 是 `refine-workspace-panel-agent-root`，它对 `platform-file-browsing` 是 **ADDED** 一条新 requirement（面板默认锚定当前 Agent 目录并在无权时回落本人私有 Agent），本次是对该 capability 内既有 requirement「租户与 Agent 工作区文件服务按作用域隔离」的 **MODIFIED**，两者不重名、不互相覆盖，任一顺序归档都不冲突：

```
$ openspec validate refine-workspace-panel-agent-root --strict
Change 'refine-workspace-panel-agent-root' is valid
```

两者在行为上也不打架：其回落只在**面板落点目录**被 403/404 时触发，而本次新增的 403 是用户点名进入他人 `user/<id>` 时才出现，不改变落点；其规格同时要求「用户自己浏览进入的路径不存在时照常报错」，与本次的「无权即拒绝」一致。

## 2. 红：新断言在改动前失败

在干净检出上运行（`git worktree add --detach /tmp/rsm-baseline-userfiles HEAD`，HEAD = `6a8ebccd`），把四个新用例文件与迁移脚本拷入后执行：

```
$ .venv/bin/python -m pytest tests/test_agent_user_file_access.py \
    tests/test_agent_user_file_directories.py \
    tests/test_agent_user_file_http.py \
    tests/test_agent_user_file_migration.py -q
ERROR tests/test_agent_user_file_directories.py
E   ImportError: cannot import name 'agent_user_outputs_dir' from 'common.state_dir'

$ .venv/bin/python -m pytest tests/test_agent_user_file_access.py \
    tests/test_agent_user_file_http.py tests/test_agent_user_file_migration.py -q
FAILED tests/test_agent_user_file_access.py::UserSubtreeVisibilityTests::test_container_is_visible_but_unowned_entries_are_not
FAILED tests/test_agent_user_file_access.py::UserSubtreeVisibilityTests::test_own_file_is_visible_and_another_users_is_not
FAILED tests/test_agent_user_file_access.py::UserSubtreeAuthorizationTests::test_owner_is_allowed_another_member_is_forbidden
FAILED tests/test_agent_user_file_access.py::UserSubtreeAuthorizationTests::test_platform_admin_is_refused_even_under_the_platform_root
FAILED tests/test_agent_user_file_access.py::UserSubtreeAuthorizationTests::test_tenant_admin_gets_no_shortcut
FAILED tests/test_agent_user_file_access.py::PreviewConsumerTests::test_owner_may_consume_the_preview_and_others_may_not
FAILED tests/test_agent_user_file_access.py::WorkspacePruningTests::test_list_dir_omits_other_users_before_the_entry_cap
FAILED tests/test_agent_user_file_access.py::WorkspacePruningTests::test_search_does_not_descend_into_another_users_subtree
FAILED tests/test_agent_user_file_access.py::UploadDirectoryTests::test_uploads_land_in_the_callers_user_subtree
FAILED tests/test_agent_user_file_access.py::SendToolPrivateFileTests::test_another_members_file_is_refused
FAILED tests/test_agent_user_file_access.py::SendToolPrivateFileTests::test_owner_gets_no_public_copy
FAILED tests/test_agent_user_file_access.py::SendToolPrivateFileTests::test_unverified_caller_is_refused
FAILED tests/test_agent_user_file_http.py::test_a_member_reads_their_own_file_and_not_a_colleagues
FAILED tests/test_agent_user_file_http.py::test_a_platform_admin_is_refused_a_members_file
FAILED tests/test_agent_user_file_http.py::test_the_uploads_subresource_serves_each_member_their_own
FAILED tests/test_agent_user_file_http.py::test_the_tree_lists_the_container_but_hides_the_colleague
FAILED tests/test_agent_user_file_http.py::test_search_never_returns_a_colleagues_file
FAILED tests/test_agent_user_file_http.py::test_resolve_refuses_a_colleagues_file
FAILED tests/test_agent_user_file_http.py::test_workspace_read_refuses_a_colleagues_file
FAILED tests/test_agent_user_file_http.py::test_workspace_write_refuses_a_colleagues_file_and_changes_nothing
FAILED tests/test_agent_user_file_http.py::test_preview_of_a_members_file_needs_that_member
FAILED tests/test_agent_user_file_migration.py::DisasterRecoveryDrillTests::test_the_quarantine_region_is_refused_by_the_file_surface
22 failed, 22 passed

$ node --test tests/test_console_workspace_frontend.cjs        # 改动前
✖ a listing that arrives after an Agent switch is dropped
✖ a search hit list that arrives after a session switch is dropped
✖ closing the panel invalidates a listing still in flight
ℹ tests 16  ℹ pass 13  ℹ fail 3
```

后来补的两条（目录拒绝、`..` 跳跃）在基线工作树单独复跑，确认同样在改动前失败，其中 `..` 那条的基线输出直接展示了泄漏：

```
$ git worktree add --detach /tmp/rsm-baseline-uf2 HEAD
$ cp tests/test_agent_user_file_http.py /tmp/rsm-baseline-uf2/tests/
$ cd /tmp/rsm-baseline-uf2 && .venv/bin/python -m pytest tests/test_agent_user_file_http.py -q -k "tree or dot_dot"
FAILED test_the_tree_lists_the_container_but_hides_the_colleague
FAILED test_the_tree_refuses_a_colleagues_directory_instead_of_a_blank_listing
FAILED test_the_tree_still_lists_the_container_and_the_agent_workspace
FAILED test_a_dot_dot_path_cannot_reach_a_colleagues_directory
4 failed, 9 deselected

E  AssertionError: {"status": "success", "path": "agents/shared-agent/user/usr_<bob>", ...
     "entries": [{"name": "uploads", "path": ".../user/usr_<bob>/uploads", ...}]}
E  assert '200' in ('403', '404')
```

即改动前，A 从自己的目录 `..` 一步就能列出 B 的目录内容与目录名。

改动前的具体泄漏（基线输出）：

```
AssertionError: '200' == '200' : b'must-not-leak'
tests/test_agent_user_file_migration.py:265
```

即基线里 `user/<他人>/uploads/x` 与隔离区 `user/_legacy/*` 都能被同租户其他成员直接 `GET /api/file` 读到 200。

基线里通过的 22 条是「不得改变」的既有语义（普通共享文件全租户可读、容器本身可列、无身份时上传回落 agent `tmp`、迁移清单的多数分支）；它们改动后仍全绿（见下）。

## 3. 绿：改动后

```
$ .venv/bin/python -m pytest tests/test_agent_user_file_http.py -q
13 passed in 5.31s

$ .venv/bin/python -m pytest tests/test_agent_user_file_access.py \
    tests/test_agent_user_file_http.py tests/test_agent_user_file_directories.py \
    tests/test_agent_user_file_migration.py -q
60 passed in 10.70s

$ node --test tests/test_console_workspace_frontend.cjs \
    tests/test_console_upload_frontend.cjs tests/test_agent_workbench_frontend.cjs
ℹ tests 58  ℹ pass 58  ℹ fail 0
（新增 3 条迟到响应丢弃用例由红转绿）
```

### 3.1 文案（任务 4.2）

本次没有新增面板文案：拒绝与不可用沿用既有 key，三种语言都已在位（`channel/web/static/js/i18n/core.js` 的 `ws_forbidden` = 没有权限访问该文件 / 沒有權限存取該檔案 / You do not have permission to access this file）。新增的「目标目录拒绝」复用同一条 403 → `ws_forbidden` 路径，浏览器里显示的正是这句。

```
$ node --test tests/test_console_i18n_coverage.cjs tests/test_console_i18n_parity.cjs
ℹ tests 9  ℹ pass 9  ℹ fail 0
```

## 4. 文件面回归（任务 5.3，含上传/执行面邻近用例）

```
$ .venv/bin/python -m pytest tests/test_platform_file_browsing.py \
    tests/test_platform_file_and_channels_phase1.py tests/test_console_workspace_transport.py \
    tests/test_console_file_transport.py tests/test_console_upload_transport.py \
    tests/test_workspace_edit.py tests/test_private_agent_file_scope.py \
    tests/test_upload_agent_scope.py tests/test_search_files_tool.py \
    tests/test_history_agent_workspace.py tests/test_agent_user_file_access.py \
    tests/test_agent_user_file_directories.py tests/test_agent_user_file_http.py \
    tests/test_agent_user_file_migration.py tests/test_object_scope.py -q
236 passed, 2 warnings, 3 subtests passed in 28.65s
```

Python/Shell/技能/coding 的运行方式未改动（本 change 不触碰 `agent/permission/`、`bridge/agent_initializer.py`、沙箱与执行器），因此不新增沙箱依赖，也不提供任何关闭用户目录校验的开关。

## 5. 阶段 4 的真实浏览器验证（任务 4.3）

在一个**隔离实例**上进行：用测试夹具的同一套真实身份与租户共享 Agent 布局（同租户成员 `alice` / `bob`，一个共享 Agent，工作空间位于租户共享根下），由产品自己的 web.py/cheroot 服务（与 `channel/web/fork/runtime.py` 相同的静态与日志中间件）在 `http://localhost:9898` 提供真实控制台，开发者本机 9899 实例与数据未被触碰。会话为真实 Cookie 会话，浏览器是 Cursor 内置浏览器。

| 步骤 | 观察结果 |
| --- | --- |
| alice 登录后打开文件面板（落点 Agent 工作空间） | `memory  user  shared-notes.txt`（公共资料照旧） |
| alice 展开 `user` | 只列出 `usr_<alice>`，同事目录不出现 |
| alice 进入本人 `user/<alice>/uploads` | `alice-report.txt 12B` |
| alice 直接请求同事目录 `user/<bob>` | 面板显示「没有权限访问该文件」（403），不再是空列表 |
| alice 搜索 `alice`（本人资料） | 命中 `alice-report.txt` |
| alice 请求同事文件 `/api/file` | `403 forbidden` |
| alice 请求公共文件 `shared-notes.txt` | `200 shared notes anyone may read` |
| alice 解析本人文件并打开 `preview_url` | `200`，`Cache-Control: private, no-store`，正文 `alice report` |
| **切换账号为 bob** 后同一 `preview_url` | `404 not found`，响应不含正文 |
| bob 展开 `user` | 只列出 `usr_<bob>` |
| bob 搜索 `alice` | `results: []` |
| bob 搜索本人 `bob-secret` | 命中 `bob-secret-plan.md` |
| bob 请求本人文件 `/api/file` | `200` 正文 `bob secret` |
| bob 请求 alice 的文件 `/api/file` | `403 forbidden` |

搜索与文件读取的观测值：

```
alice: {"container":"usr_<alice>","colleague":"没有权限访问该文件","own":"alice-report.txt 12B"}
bob:   {"container":"usr_<bob>","aliceDir":"没有权限访问该文件","own":"bob-secret.txt 10B"}
bob:   search(q=alice) -> {"results": []}
       search(q=bob-secret) -> {"results": [{"name":"bob-secret-plan.md", ...}]}
       /api/file(alice 的文件) -> 403 forbidden
       /api/file(shared-notes.txt) -> 200
alice: preview_url 本人 -> 200 "private, no-store"；切到 bob 后同一 URL -> 404 not found
```

**未在本轮浏览器验证**：Agent 可见性转换（共享/私有切换）本身属于 change `show-and-toggle-agent-visibility` 的功能；本 change 对该项的要求是「不放宽既有边界」，由第 4 节的私有 Agent、跨租户、平台管理员用例逐条覆盖，未在浏览器里做一次真实转换。

## 6. 归属判断的顺序（任务 2.2/3.1）

`channel/web/fork/handlers/files.py::_db_path_visible` 的用户子树分支在**任何**共享根或平台根分支之前返回，`_authorize_db_file_path` 同样先判定 `user_subtree_state` 再判断平台根与管理员放行；因此「命中 user/<user_id> 时先校验本人」不是靠调用顺序偶然成立。

用变异测试确认该顺序是承重的：把 `_authorize_db_file_path` 里这段用户子树判断整体移到平台根分支**之后**再运行：

```
$ .venv/bin/python -m pytest tests/test_agent_user_file_access.py tests/test_agent_user_file_http.py -q -k platform_admin
FAILED tests/test_agent_user_file_access.py::UserSubtreeAuthorizationTests::test_platform_admin_is_refused_even_under_the_platform_root
E   AssertionError: Tuples differ: (True, 'platform') != (False, 'forbidden')
E     - (True, 'platform')
E     + (False, 'forbidden')
1 failed, 1 passed
```

平台根资格分支先命中的话，非 owner 的平台管理员就拿回了他人 `user/<id>` 文件。改回原顺序后四条用例全绿（60 passed）。同一次变异里 `test_a_platform_admin_is_refused_a_members_file`（真实 HTTP）仍然通过——该夹具里成员文件不在平台根之下，所以**钉住这个顺序的是单元用例**，HTTP 用例钉住的是没有平台根放行时的拒绝。

## 7. 迁移与灾难演练（任务 5.1/5.2）

`scripts/migrate_agent_user_files.py` 按可信会话记录归属，产物进入 `user/<owner>/`，无法唯一归属的进入 `user/_legacy/` 隔离区；隔离区对文件接口不可读（第 2 节的基线红里正是它泄漏 200，改动后由 `test_the_quarantine_region_is_refused_by_the_file_surface` 固定为拒绝）。演练覆盖：中断后续跑不重复搬运、内容校验（SHA 比对）、源区域清空、二次运行无事可做、已存在的同名文件不覆盖。

### 7.1 在真实实例上先跑 dry-run（发现并修掉一个真实缺陷）

任务 5.1 要求清单本身可信，所以清单先在真实租户 `test15` 上做过 dry-run（只读，不搬任何文件）：

```
workspace: .../agents/bug-butler-test15                      quarantine  1
workspace: .../agents/business-analysis-test15               quarantine  4
workspace: .../agents/customer-requirements-advisor-test15   quarantine  4
workspace: .../agents/my-assistant-admin-test15-RC001        quarantine  3
workspace: .../agents/my-assistant-admin-test15              quarantine 31
workspace: .../agents/tax-health-check-test15                quarantine 32
dry run: nothing was moved (pass --apply to perform the moves)
```

**dry-run 当场暴露的缺陷**：真实工作区里存在解包目录（`tmp/_docx/word/document.xml` 与 `tmp/_docx2/word/document.xml`、两套 `.rels` / `[Content_Types].xml` / `app.xml` …），而隔离区目的地只取 `basename`，于是 **75 个文件里有 12 个落在同一个目的地**。`claimed` 去重只覆盖"可归属"那一支，隔离支没查重，`--apply` 时第二个文件会被静默顶掉。这正是"迁移清单必须先看"的原因——单元测试当时全绿，是因为没人造过嵌套解包目录。

修法（先红后绿）：隔离区改为保留**相对 legacy 根**的路径（`tmp/_docx/word/document.xml` → `user/_legacy/tmp/_docx/word/document.xml`），目的地按构造唯一；`claimed` 之外，`_quarantine` 再对已计划目的地做一次重复即报错的保护；`apply_plan` 里"运行中目的地被别人占走"的回退路径也换成同一函数。新增 3 条用例（`test_two_sources_never_share_one_destination`、`test_a_quarantined_nested_file_keeps_its_relative_shape`、`test_a_flat_file_keeps_its_name_under_its_legacy_root`）在改前红、改后绿。修后同一批真实工作区复跑：**75 个文件，0 碰撞**。

**归属为何全是隔离**：这不是迁移脚本没读到记录，而是记录真的不存在。全盘只有一个带 `messages` 表的会话库（`/Users/jiantan/cow/memory/long-term/index.db`，101 条，`tenant_id` 只有默认租户 `tnt_xNHlQIA2XP-z6nQG`）；`test15` 各 Agent 自己的 `memory/long-term/index.db` 里没有 `messages` 表。也就是说这批历史文件没有任何可信归属可查——按规范"未知归属受保护保留"，全部进 `user/_legacy/` 是 fail-closed 的正确结果，而不是猜测归属后发布。

**因此 `--apply` 的后果需要产品侧确认**：这 75 个文件（WeCom 收文 `wecom_*.docx`、语音 `voice_input_*`、网页截图 `web_*`/`screenshot_*` 等）在 `_legacy/` 里会被文件接口拒绝访问，即上传者本人也读不回来。规范如此规定（未知归属不得发布），但它是不可逆的用户可见后果，故本 change 停在 dry-run，未执行 `--apply`。

## 8. 面板迟到的响应（任务 4.1）

`channel/web/static/js/workspace.js` 以 `wsScopeEpoch` 标记作用域：切换 Agent/会话/账号、重新打开面板、关闭面板都会递增世代，异步回来的列表/搜索结果/预览在绘制前比对世代并丢弃过期结果。三条新用例在改动前红、改动后绿（第 2、3 节）。

## 9. 任务清单逐条对照

| 任务里的断言 | 证据 |
| --- | --- |
| 目录与策略：伪造 owner / 混入畸形目录名 | `test_malformed_owner_inside_container_is_unowned`、`test_unsafe_user_id_is_refused` |
| 软链接（别名不得成为另一种拼写） | `test_symlink_alias_resolves_to_the_real_owner`、`test_user_container_must_not_be_a_symlink_or_file`、`test_user_subdirectory_must_not_be_a_symlink`、`test_a_symlinked_entry_is_never_followed` |
| 绝对路径与父级路径 | `test_path_outside_the_workspace_is_not_matched`、`test_a_dot_dot_path_cannot_reach_a_colleagues_directory` |
| 管理员（租户 / 平台）不因资格放行 | `test_tenant_admin_gets_no_shortcut`、`test_platform_admin_is_refused_even_under_the_platform_root`、`test_a_platform_admin_is_refused_a_members_file` |
| 跨租户 / 缺租户上下文 | 既有租户隔离用例（`test_private_agent_file_scope.py`、身份与租户套件）在本 change 改动后全绿；用户子树规则不参与该判定，也没有放宽它 |
| 账号改名不迁移文件 | 路径只由 `user_id` 决定（`agent_user_root(workspace, user_id)`），显示名不参与；`test_root_is_under_workspace_user_container` 固定该形状 |
| 预留目录冲突（`_legacy` 等） | 隔离区 `test_the_quarantine_region_is_refused_by_the_file_surface`；容器内畸形名 `test_malformed_owner_inside_container_is_unowned` |
| 检查后替换（容器被换成软链/文件） | `_assert_user_container_safe` 在每次解析时复核容器类型，由上述三条软链用例固定 |
| 父目录递归不泄漏 | `test_list_dir_omits_other_users_before_the_entry_cap`、`test_search_does_not_descend_into_another_users_subtree`、`test_search_never_returns_a_colleagues_file` |
| 子资源 / 静态入口 | `test_the_uploads_subresource_serves_each_member_their_own`、`test_preview_of_a_members_file_needs_that_member`、`test_public_workspace_file_needs_no_identity`（公开预览仍可读） |
| 现有修改接口 | `test_workspace_write_refuses_a_colleagues_file_and_changes_nothing`（拒绝且文件字节不变） |
| 迁移可重跑 / 中断 / 冲突 | 第 7 节；真实工作区 dry-run 见 7.1（结论：0 碰撞，`--apply` 待产品侧确认） |

## 10. 未执行

- 生产/开发实例（`localhost:9899`）上的多人手工联调：需要开发者本人的账号与租户数据，本轮改用隔离实例完成等价验证。真实实例上只做了只读的迁移 dry-run（第 7.1 节），未在真实数据上执行 `--apply`。
- 真实实例上的多人文件归属复验：`localhost:9899` 重启后浏览器会话失效，缺少 `test15` 账号口令，未能在真实租户上跑通鉴权请求。
- 真实模型驱动的「上传附件 → Agent 读取 → 生成结果链接」端到端对话：需要可用的模型额度与对话链路，本 change 的验收只覆盖平台文件面的读写与归属，不覆盖模型行为。
- 执行层隔离（沙箱、配置根与运行根分离、索引/记忆/后台任务的 owner 贯彻）：**本 change 不交付**，共享 Agent 内 Python/Shell/技能/coding 直接读盘的用户隔离不在验收保证内（已写入 `docs/design/shared-agent-user-data-isolation-plan.md` 的交付状态与 `docs/zh/channels/web.mdx`）。
