# 本地模式按 master 行为接通（change 任务 5.1—5.4）

范围：把"打开本机项目"这一轮的**原 Agent 工具入口**拨到已验证的本机根上，脚本交给
第 4 组的平台启动器，文件面板／`@`／工具参数共用同一个来源，并用真实生成文件核对
六工具、个人根回归与源输入保护。运行日志：`evidence/local-e2e-run.log`。

## 1. 接通了什么（5.1 / 5.2）

| 接缝 | 实现 | 证据 |
| --- | --- | --- |
| 原工具入口 + 已验证根 | `agent/desktop_local/run_context.py::tool_view_for_run` 按轮浅拷贝工具并钉住本轮冻结目录；根只能来自可信注册表（`resolve_target_root` 每次调用复核授权） | `tests/test_desktop_local_e2e.py::SixToolsInPlaceTests` |
| 脚本只走平台启动器 | `AgentStreamExecutor._run_tool` → `capabilities.local_call_refusal` → `script_executor.run_script` → 桌面主进程 `executor-endpoint.ts`；无启动器即拒绝，**不**在本进程执行、也**不**转发服务器 | `ScriptIsolationTests`（含真实 seatbelt worker）、`tests/test_desktop_local_executor_endpoint.cjs` |
| 工具身份/schema 不变 | `script_tool.IsolatedScriptTool` 沿用 master `bash` 的 `name`/`params`；描述取启动器实测平台文案 | `tests/test_desktop_local_script_tool.py` |
| 记忆/知识/远端 API/MCP 不受牵连 | `needs_local_directory` 改为**闭集**判定（见第 4 节）；参数不经本机路径解析 | `MasterBehaviourPreservedTests`、`tests/test_desktop_source_resolver.py` |
| 无项目回归本人默认工作区 | 无 desktop 目标 → 原共享实例、原参数、原 cwd；关闭项目后 `_local_tools` 保持 `None` | `MasterBehaviourPreservedTests::test_a_session_without_a_project_keeps_the_server_behaviour`、`::test_closing_the_project_restores_the_default_workspace` |

项目业务产出优先项目 cwd 的落点（`_maybe_emit_artifact` 的锚点、`_permission_denial`
的 cwd 均取 `_run_cwd(agent.effective_cwd())`）由第 3 组套件
`tests/test_desktop_run_context.py` 覆盖，本次未改动。

## 2. 端到端是怎么真的跑起来的（不是 mock）

两层，都是真进程、真文件：

1. **Python 层，真工具真目录。** `tests/test_desktop_local_e2e.py` 用 master 的
   `Read/Write/Edit/Bash/Ls/SearchFiles` 原类实例（`config['cwd']` 指向"服务器
   工作区"），再取本轮的 `tool_view_for_run` 发给项目目录，路径带**中文＋空格**
   （`…/我的 项目`，内含 `输出 目录`）。断言包括 read 的行号输出、edit 的原地替换、
   ls 的中文条目、search_files 的中文命中，以及服务器目录**一个字节都没变**。
2. **跨语言层，真沙箱命令。** `tests/_desktop_executor_host.cjs` 起**编译后的
   生产**执行端点（`desktop/dist/main/local-execution/executor-endpoint.js`）：
   真 loopback 监听、真启动令牌、真 `sandbox-exec` worker。Python 侧把
   `COW_DESKTOP_EXECUTOR_URL/TOKEN` 指过去，于是 `IsolatedScriptTool` 的一次调用
   走完 backend → HTTP → node → seatbelt → worker → 真 `bash`，产出的
   `脚本产出.txt` 真的出现在项目里。

手工复核（同一路径，脱离 pytest）：

```
$ .venv/bin/python /tmp/manual_e2e.py        # host 起真端点后再调 run_script
available: (True, None)
refusal: None
status: success
result: /private/var/.../local-e2e-manual-ymz6uhkw/我的 项目
Darwin
exit_code: 0
file: 沙箱产出
```

（该临时脚本仅用于本次记录；等价断言已在 `ScriptIsolationTests::
test_a_real_sandboxed_command_runs_inside_the_project` 内固化，非 macOS/无 node 时跳过。）

## 3. 文件面板 / `@` / 工具参数是同一来源（5.3）

`OneSourceTests` 对同一个引用同时问三个面：

* 面板/会话来源：`source_for_session` 与 `source_for_identity` 给出**同一个根**
  （`root == …/我的 项目`），`describe()` 只回 kind/available/refusal，不含目录；
* `@` 引用：`resolve_reference("报告 2026.txt")` 的绝对路径＝项目内真实路径，
  渲染成 `[本机项目文件: 报告 2026.txt]`；绝对路径/`..`/不存在的引用都不生效并
  按名说明，**没有**回落到服务器目录、也没有上传；
* 工具参数：项目内的绝对路径被改写成相对 id（同一个文件，不是副本），相对 id 原样
  透传，项目外的服务器路径整条调用按名拒绝（`REFUSAL_NO_LANDING`）。

## 4. 本轮发现并修掉的一处真实缺陷：`needs_local_directory` 的谓词

`BaseTool` 声明了类属性 `cwd`，所以 `hasattr(tool, "cwd")` 对**每个**工具都为真。
原实现沿用了 master `apply_project_dir` 的谓词
（`name in CWD_TOOLS or hasattr(tool, "cwd")`）来判断"这次调用是否必须在这个目录里
干活"，于是真正的记忆/知识类工具也会被算作"需要该目录"：项目一旦撤权，这些与项目
无关的调用会一起被拒绝 —— 正是 3.5／5.2 要求中"记忆等服务器侧工具不受牵连"要避免
的连带伤害（此前用例用的是不带 `cwd` 的假工具，所以没暴露）。

修法：`needs_local_directory` 改成闭集判定（`CWD_TOOLS`），并把
"项目**移动**哪些工具"（`tool_view_for_run`，保持 master 一致）与"项目**能拒绝**
哪些调用"（本次收窄）两件事在代码里写清楚。副作用只有一处：带 `cwd` 但不在集合内
的工具不再因项目失效被拒；重定向行为未变。

变异验证（每次改后清 `__pycache__` 再跑，避免过期字节码伪造结论）：

| 变异 | 结果 |
| --- | --- |
| 谓词改回 `… or hasattr(tool, "cwd")` | `test_a_server_side_tool_survives_the_project_being_revoked` 失败（1 failed / 46 passed） |
| 谓词改成 `return True`（一律拒绝） | 3 项失败：上述用例 + `test_desktop_run_context.py::…test_a_server_side_tool_still_runs_when_the_project_is_gone` + `test_desktop_target_resolution_acceptance.py::A10…` |

## 5. 其他真实性校验：脚本侧的"未启动器不执行"

`ScriptIsolationTests::test_without_a_launcher_the_command_does_not_run_anywhere`
在没有 `COW_DESKTOP_EXECUTOR_*` 的环境里调用 `bash`：断言 `local_call_refusal`
返回"没有可用启动器"的原因为止的拒绝、`IsolatedScriptTool.execute` 返回 error、
并且命令会创建的标记文件**不存在**、服务器目录也没有变化。派发接缝
（`_run_tool`）同样在任何进程被启动前返回 `capability` 拒绝。

## 6. A 编号覆盖（5.4）

| A | 本次证据 | 说明 |
| --- | --- | --- |
| A04 | `SixToolsInPlaceTests`（六工具同一真实项目、中文空格路径、服务端哨兵字节不变、复用原 Python 工具类）＋ `ScriptIsolationTests::test_a_real_sandboxed_command_runs_inside_the_project`（真实 Bash） | 通过 |
| A08（本地侧） | `ConcurrentRunTargetTests`：一个共享工具表、两轮各钉各自目录（写各自的真实文件、互不覆盖、共享实例 cwd 不变）；重选目录后旧运行被拒且新目录不被写入 | 通过；跨会话/cached Agent 的服务端侧分支见 `tests/test_desktop_target_resolution_acceptance.py::A08…` |
| A03（本地侧） | `OneSourceTests::test_a_message_reference_never_becomes_a_server_path`（伪造/绝对路径/越界一律不解析）＋ 服务器侧个人根校验未改动的既有断言（`tests/test_desktop_target_resolution_acceptance.py`、`tests/test_desktop_local_root.py`） | 通过 |
| A30 | `MasterBehaviourPreservedTests`（无项目/关闭项目回归本人工作区）、`GrantModeTests`（只读输入：读得到、写与脚本被拒）＋ 既有普通 Web/Channel 回归 | 通过 |

## 7. 回归

```
$ .venv/bin/python -m pytest tests/test_desktop_*.py -q -p no:randomly
575 passed, 6 warnings, 19 subtests passed in 69.62s

$ .venv/bin/python -m pytest tests/test_desktop_local_e2e.py -p no:randomly -v
16 passed

$ node --test tests/test_desktop_local_execution.cjs tests/test_desktop_local_executor_endpoint.cjs
tests 69 / pass 69 / fail 0
```

另跑过 `npx tsc -p tsconfig.main.json --noEmit`（desktop 主进程）通过。

## 8. 边界与未完成（不勾选的部分不靠开关结项）

* **Windows 与本机安装包内**的启动器未接通：`resolveScriptSupport` 在 win32 直接
  返回 `unsupported_platform`，脚本能力不声明可用（A26/A27 的安装包内复核仍待
  第 10 组）。
* **技能部署与产出 UI**（面板本机 source adapter、卡片/系统打开）属第 8/9 组，
  本次只保证"来源一致"和"工具真在项目里动手"，未做 UI 侧新面。
* 本次 E2E 的沙箱断言依赖 macOS `sandbox-exec` 与 `node`，其他平台跳过而非降级；
  Windows/Linux 的同形证据在各自平台实现后补齐。
* 资源预算（内存/CPU 硬上限）的缺口沿用 `evidence/isolation-acceptance.md` 的记录，
  本组未改变。
