# land-shared-agent-panel-on-own-files 验证记录

本文件记录 change `land-shared-agent-panel-on-own-files` 的规范校验、红/绿、真实 Wire 的落位序列、前端落点用例、回归与部署侧验证。所有结论只来自实际执行过的输出。

## 1. 规范校验

本机没有 Windows 版 `openspec` CLI（`openspec validate --strict` 不可用），因此用仓库自带的 `scripts/check_change_deltas.py`——它做的正是 `--strict` 的核心检查：ADDED 不能重述基线已有要求、MODIFIED 的目标必须存在于基线、每条要求的 Scenario 必须落在正确的 delta 块里：

```
$ python scripts/check_change_deltas.py land-shared-agent-panel-on-own-files
FAIL (proposed): 3 problem(s)
  - agent/memory/conversation_store.py: marked seam:conversation-store but this change never names it
  - agent/tools/scheduler/integration.py: marked seam:scheduler but this change never names it
  - tests/test_scheduler_web_update.py: marked seam:scheduler but this change never names it
```

三条**全部**来自 `check_conflict_coverage`（上游冲突 seam 的覆盖检查），**没有一条 delta 问题**——本次的 ADDED/MODIFIED 对基线是干净的（没有「ADDED 重述已有要求」「MODIFIED 目标不存在」「Scenario 未落在 delta 块内」）。

这三条与本次改动无关，且是仓库级约定而非本 change 的缺陷：对任何一个未归档 change 都会报同样的三条，例如

```
$ python scripts/check_change_deltas.py auto-bind-channel-sender
FAIL (proposed): 3 problem(s)      # 同样三条，逐字相同
```

seam 覆盖由归档期的 `fork-decoupling-and-tenant-hardening` 承担（脚本默认目标）：

```
$ python scripts/check_change_deltas.py
OK (applied): fork-decoupling-and-tenant-hardening — deltas consistent with the baseline, every conflicted file covered
```

再补一段独立的结构校验（临时脚本，未入库），交叉确认 delta 名称确实命中基线：

```
baseline reqs: 3  delta reqs: 2
NEW (added): 本人用户目录按已验证身份物化
   scenarios: 6
MODIFIED/MATCHES baseline: 工作区面板默认锚定当前 Agent 自己的目录并在无权时回落到本人私有 Agent
   scenarios: 11
missing baseline target: ['平台级只读文件根仅平台管理员可浏览', '租户与 Agent 工作区文件服务按作用域隔离']
```

两类要求的名称与基线一致（`openspec/specs/platform-file-browsing/spec.md` 第 126 行即被修改的那条），每条要求至少一个 Scenario；末行两条基线要求本次**未改动**，delta 不重述。无新增 capability。

## 2. 红/绿

只把源码 stash 起来（保留本次新增的用例），用同一批用例在改动前跑一遍，确认它们确实在测新行为：

```
$ git stash push -m "red-check-panel-landing" -- channel/web/fork/handlers/agents.py \
    channel/web/fork/handlers/workspace.py channel/web/route_registry.py \
    channel/web/static/js/workspace.js channel/web/web_channel.py

$ node --test tests/test_console_workspace_frontend.cjs        # 红
ℹ tests 22  ℹ pass 18  ℹ fail 4

$ python -m pytest tests/test_workspace_user_dir.py tests/test_agent_user_file_http.py -q   # 红
ImportError while importing test module 'tests/test_workspace_user_dir.py'.
E   ImportError: cannot import name 'WorkspaceUserDirHandler' from
    'channel.web.fork.handlers.workspace'
ERROR tests/test_workspace_user_dir.py
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 1.34s

$ git stash pop
Dropped refs/stash@{0} (67291a96a0bce327fbf34ed4f0665c5b01f29da1)
```

失败的正是本次新增的四条前端落点用例；后端则是模块级缺符号（新入口与新的物化函数都不存在）。

真实 Wire 用例单独再测一遍（上面那次 pytest 因收集期报错整批中断，`test_agent_user_file_http.py` 没有真正执行到）：

```
$ python -m pytest tests/test_agent_user_file_http.py -q -k \
    "the_use_range_roster_says or panels_landing_sequence or body_cannot_choose \
     or making_the_folder_twice or cannot_address_is_refused"
E       AssertionError: not found
4 failed, 1 passed, 13 deselected, 2 warnings in 27.32s
FAILED ...::test_the_body_cannot_choose_whose_folder_gets_made
FAILED ...::test_the_use_range_roster_says_which_kind_of_agent_it_is
FAILED ...::test_the_panels_landing_sequence_makes_then_lists_the_callers_folder
FAILED ...::test_making_the_folder_twice_leaves_the_members_files_alone
```

改动前 `POST /api/workspace/user-dir` 是 404，`visibility` 字段也不存在，所以这四条以 404 / 缺字段失败。剩下一条（`cannot_address_is_refused`）两边都通过——它是护栏用例，断言语义是「403 或 404 且什么都没创建」，改动前后都成立，保留它是为了锁住「拒绝路径不产生副作用」。

恢复后绿见第 3、4 节。

## 3. 真实 Wire 的落位序列

单测各自只覆盖一半；只有真实 WSGI 应用能回答「路由策略 + 身份 + 租户头 + 会话 cookie + handler 自己的作用域」这一串是否连得上。因此把面板实际执行的那串请求写进 `tests/test_agent_user_file_http.py`（沿用该文件既有的 `WebAppHarness`：真人会话、真租户、真路由、真策略）：

```
$ python -m pytest tests/test_agent_user_file_http.py -q
18 passed, 2 warnings in 98.42s (0:01:38)
```

新增的五条（其中四条在改动前失败，见第 2 节）：

| 用例 | 断言的实际行为 |
| --- | --- |
| `test_the_use_range_roster_says_which_kind_of_agent_it_is` | `GET /api/agents?view=workbench` 中该共享 Agent 行 `visibility == "tenant"`——面板分叉所依据的事实由**它实际聊天用的那份投影**给出 |
| `test_the_panels_landing_sequence_makes_then_lists_the_callers_folder` | 从未上传过的成员：`POST /api/workspace/user-dir` 后，`GET /api/workspace/tree?path=agents/<agent>/user/<usr_…>` 返回 200 且 `entries == []`（空目录，不是报错、不是共享根） |
| `test_the_body_cannot_choose_whose_folder_gets_made` | 请求体里塞 `user_id`/`user`/`path` 指向同事，仍只创建调用者自己的目录；同事目录不存在；`user` 列表仍只含本人 |
| `test_making_the_folder_twice_leaves_the_members_files_alone` | 连续两次物化后，本人已上传文件的字节不变（幂等） |
| `test_an_agent_the_caller_cannot_address_is_refused` | 命名一个不可寻址的 Agent 被 403/404 拒绝，且工作区里**连 `user` 容器都没有**被创建（护栏用例，红绿皆过） |

## 4. 前端落点用例

```
$ node --test tests/test_console_workspace_frontend.cjs
ℹ tests 22  ℹ pass 22  ℹ fail 0
```

新增（红/绿对照见第 2 节；前四条红→绿，后两条是护栏用例，改动前后都通过）：

| 用例 | 断言 |
| --- | --- |
| `a shared Agent opens on the caller own folder` | 共享 Agent 的落点是 `agents/<id>/user/<uid>`，不是共享根 |
| `the caller own folder is created once and the panel stays there` | 目录缺失时物化一次、对该落点再列举一次并停留 |
| `a caller folder that cannot be made falls back to the Agent own folder` | 物化失败 → 回落到 `agents/<id>`，不重复物化 |
| `a refused shared Agent folder still falls back to the caller own private Agent` | 权限拒绝仍走既有回落，且不触发任何创建 |
| `a private Agent keeps opening on the Agent own folder` | 私有 Agent 落点不变 |
| `an Agent the roster did not report is not treated as shared` | 花名册无该行 → 按私有处理，不猜测共享 |

## 5. 回归

```
$ python -m pytest tests/test_workspace_user_dir.py tests/test_agent_user_file_http.py \
    tests/test_agent_visibility_toggle.py tests/test_agent_workbench.py \
    tests/test_route_registry.py -q
98 passed, 2 warnings in 156.06s (0:02:36)
```

文件/工作区面回归：

```
python -m pytest tests/test_platform_file_browsing.py tests/test_console_workspace_transport.py \
  tests/test_console_file_transport.py tests/test_console_upload_transport.py tests/test_workspace_edit.py \
  tests/test_upload_agent_scope.py tests/test_object_scope.py tests/test_agent_user_file_access.py \
  tests/test_agent_user_file_directories.py -q
118 passed, 4 warnings, 1 error, 3 subtests passed in 164.47s (0:02:44)
```

唯一那条 ERROR **不是行为失败**：`test_console_workspace_transport.py::test_read_refuses_an_escaping_relative_path` 的用例体通过，报错发生在临时目录清理阶段（`shutil.rmtree` 撞上仍被占用的 `...\cow\memory\long-term\index.db`，`PermissionError: [WinError 32]`）。隔离重跑该用例：

```
$ python -m pytest \
    "tests/test_console_workspace_transport.py::ConsoleWorkspaceTransportTests::\
test_read_refuses_an_escaping_relative_path" -q
1 passed, 2 warnings in 3.40s
```

即 Windows 上长批次运行时的文件占用抖动，与本次改动的代码路径无关（本次不触碰 `memory/long-term`）。

工作台投影白名单用例（`tests/test_agent_workbench.py::test_view_workbench_returns_whitelisted_fields_only`）随新增字段更新，其余字段未被放宽。

## 6. 部署侧：路由与静态资源

按最新代码重启本机后端（`.\stop-all.ps1 -Service backend` → `.\start-all.ps1 -Service backend`，pid 10236，自检 5 项 HTTP 200）。重启前后对同一入口探测，证明新路由确实随代码上线：

```
# 重启前（旧进程）
POST /api/workspace/user-dir  ->  404

# 重启后
$ curl -s -X POST http://127.0.0.1:9900/api/workspace/user-dir \
    -H 'Content-Type: application/json' -d '{"user_id":"usr_forged","agent":"x"}'
{"status": "error", "message": "tenant selection required", "code": "missing_tenant"}   HTTP:400
```

404 → 400 说明路由已登记、且先撞上租户门（本机控制台直连不带租户头），不是回落到静态/前端路径；同时也验证了请求体里的 `user_id` 不被当作身份来源。

面板实际加载的脚本与磁盘文件逐字节一致，且含本次逻辑：

```
$ curl -s http://127.0.0.1:9900/assets/js/workspace.js -o live_ws.js
served == disk : True
107:  function wsAgentVisibility(agentId) {
132:  function wsOwnUserDirPath(agentId) {
135:      if (wsAgentVisibility(agentId) !== 'tenant') return '';
1135: async function wsEnsureUserDir() {
1140:      const res = await fetch('/api/workspace/user-dir', {
1251:      created = await wsEnsureUserDir();
```

## 7. 未覆盖 / 边界说明

- **未做浏览器端多人实测**：本机的浏览器自动化与后端不在同一网络命名空间（自动化浏览器访问 `127.0.0.1:9900` 落到 `chrome-error://`），只能走公网域名，而公网入口停在登录页；我没有账号口令，不猜测、不试错登录。因此「A/B 两名成员在同一共享 Agent 下各自落在自己目录」这一条**由第 3 节的真实 Wire 用例覆盖**（同一 `WebAppHarness` 内 alice/bob 共用同一 Agent、各自目录、互相不可见），不是由浏览器实测覆盖。
- **不在本次范围**：`user/<user_id>` 的归属判断与预览/下载/写入作用域仍属 change `isolate-shared-agent-user-data`；本次不改服务端归属规则，也不动执行层（Python/Shell/技能/coding）边界。
- **既知与本 change 无关的失败**：`tests/test_private_agent_file_scope.py::test_file_serve_still_serves_shared_agent_files` 在本次改动前的干净基线上即失败（该文件未列入第 5 节回归集，避免把既有失败计入本次结论）。
