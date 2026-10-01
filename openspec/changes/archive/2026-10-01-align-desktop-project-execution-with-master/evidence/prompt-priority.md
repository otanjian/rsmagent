# 8.7 产出归属、维护权限与客户端平台

任务原文：

> - [ ] 8.7 调整项目与共享维护提示的优先级，确保业务产出写项目、技能/记忆维护遵循原权限，工具描述取客户端实际平台。

对应的验收条目：

- **A06**「报告真实位于所选项目」→ 业务产出写项目；
- **A07**「技能/记忆目录未搬进项目」→ 技能/记忆维护遵循原权限；
- **A12**「显示真实平台和缺项；正确使用客户端 Shell 语义；不能悄悄回退服务端执行」→ 工具描述取客户端实际平台。

契约依据（`desktop-project-execution`）：

> 相对输入和业务产出路径 SHALL 以该目录为基准。

> 记忆、知识检索、远程业务 API 和 MCP SHALL 保持既有授权与服务归属，不因存在项目而传入无法解析的客户端路径。

> 脚本能力 SHALL 按执行隔离与平台验收结果启用；缺失能力不得隐式降级为服务器执行。

三句话各自对应一处**容易被"顺手改错"的实现**，因此三处都单独设了用例与变异验证。

---

## 1. 业务产出写项目

`_local_execution_notes` 现在把产出归属作为一条**规则**说明，而不只是让相对路径碰巧落对地方：

> 用户要的成果要落进**这个项目**：输出、报告与生成的文件都写在项目目录下（相对路径本来就是这个意思）。不要把成果留在临时目录里，也不要让用户自己去找。

这条是 A06 的「真实位于所选项目」在提示层的表述。只依赖相对路径是不够的：模型完全可能把成果写进临时目录、再把路径报给用户 —— 产出真实存在，但不在项目里。

同一段还保留原有的两条更正：工作目录是**用户本机**的目录，且"能否修改"由每次工具调用决定、本段不作判定。

---

## 2. 技能/记忆维护遵循原权限

这是三处里最容易丢的一处，因为布局把两个目录**并排**放着，只有路径不同：

> 该项目授予的是**这个目录**上的权限，不延伸到技能与记忆维护：二者仍遵循原有权限，仍在上面那个系统目录中。不要经由项目去写它们；也不要把客户端路径交给在服务端解析路径的工具（记忆、知识、MCP、远程业务 API）——工具无法接受该路径时，应如实说明，而不是自己编一个。

后半句是契约里"不因存在项目而传入无法解析的客户端路径"的直接落实：本机项目里的路径对服务端工具毫无意义，模型替它"补"一个服务端路径是错误行为，而不是帮忙。

现有 `_build_project_workspace_section` 已经说了"记忆和技能仍在系统目录、不要写入项目"，本任务补的是**权限**口径：不只是位置问题，而是项目授权不覆盖它们。

## 3. 工具描述取客户端实际平台

### 3.1 平台由客户端提供，不由服务器猜

脚本工具的语义描述本来就来自 launcher（`describeScriptCapabilities` 输出 `Platform` / `Shell` / `Python` / `Isolation` / `Reads` / `Writable`），也就是 A12 要的"显示真实平台和缺项"。本任务补两处缺口：

**（a）提示词里说明平台归属。** 已知客户端平台时点名；未知时**明说未知**：

> 这里的命令运行在**客户端**平台（`win32`）上，不是服务器平台。……

> 这里的命令运行在**客户端**平台上，而本轮未能确定该平台；不要假设它就是服务器的平台。……

未知时**不得**退回服务器平台——这是本任务的核心不变量，用例 `test_an_unknown_platform_is_never_replaced_by_the_servers_own` 专门盯它，变异 M3 把 `client_platform` 换成 `sys.platform` 会被判为失败。理由不是洁癖：本机命令不在本进程所在机器上执行，`sys.platform` 对它**不是证据**，拿它填空是"自信地答错"，比不说更糟。

**（b）平台来源单一且分层。** `Agent.client_platform()`：

| 会话 | 来源 |
| --- | --- |
| 非桌面目标（服务端会话） | `""`——没有客户端，且**不**去探测 launcher |
| 本机桌面（launcher 在本进程侧） | `script_platform(script_scope(...))`，即 launcher 自报 |
| 远程桌面（设备在用户机器上） | 设备 hello 里声明的 `platform` |

远程与本机取平台的位置不同，因为它们本来就是不同的事实；`remote_mode_for()` 用"本进程能否解析出目录"来区分，而不是靠猜。

`script_platform()` 在 launcher 取不到答案时返回 `""`，**不返回** `"posix"`：把未知当成 POSIX 正是 A12 要防的隐式降级（变异 M7）。

### 3.2 服务器平台的说明不许跟着工具跑到客户端

`bash` 的 `PLATFORM:` 段落断言的是"命令在哪台机器上跑"。桌面视图复用这个工具时，"那台机器"是客户端，于是这段文字变成了**对错误机器的断言**。

修法是结构性的，不是文本处理：

- `Bash._WIN_PLATFORM_NOTE` 把这段说明从 f-string 里**拆成独立常量**，`platform_note` 是它的对外形态；Windows 分支的渲染结果与改动前**逐字节一致**（POSIX 分支 `platform_note == ""`，因为没什么要更正的）。
- `IsolatedScriptTool` 通过 `_without_platform_note(delegate)` 拿到**去掉该说明**的描述。撤下时带走的换行与它当初被包进去的一致，因此结果**逐字节等于**同一工具在 POSIX 上的渲染（用例 `test_stripping_reproduces_the_posix_description_exactly`，变异 M12 用"顺手重排"的方式改坏它会被判失败）。
- 说明被撤下后若脚本可用，launcher 自己的文案顶上；不可用时，跟上的是拒绝原因 —— 这才是模型真正需要的信息（变异 M10 让本地工具照搬服务器描述会被判失败）。

`Bash` 依旧**不**提供 `platform_hint()`：它只知道**自己**的平台，对客户端一无所知。`_client_platform(tools)` 只认那些真正知道客户端平台的对象，所以服务器会话不会被这条兜底路径塞进一个平台（用例 `test_a_server_session_never_calls_the_launcher`）。

---

## 4. 验证

### 4.1 用例

```
.venv/bin/python -m pytest \
  tests/test_desktop_local_prompt_priority.py \
  tests/test_desktop_execution_target_wire.py \
  tests/test_shared_agent_personal_workspace.py \
  tests/test_shared_agent_personal_workspace_wire.py \
  tests/test_workspace_layout_template.py \
  tests/test_desktop_local_script_tool.py \
  tests/test_desktop_run_context.py tests/test_desktop_run_scope_cancel.py \
  tests/test_desktop_local_worker.py tests/test_desktop_run_authorization.py \
  tests/test_desktop_resource_landing.py tests/test_desktop_run_inputs.py \
  tests/test_desktop_tool_disposition.py tests/test_desktop_local_input_staging.py \
  tests/test_desktop_resource_staging.py tests/test_desktop_resource_refs.py \
  tests/test_mutation_evidence_scripts.py -q -p no:randomly
```

`437 passed, 237 subtests passed`。新增用例 `tests/test_desktop_local_prompt_priority.py` 51 项。

`bash` 改动后的回归（该文件被重构，单独跑一遍）：

```
.venv/bin/python -m pytest tests/test_bash_background.py tests/test_bash_config_propagation.py \
  tests/test_bash_exit_codes.py tests/test_bash_output_encoding.py tests/test_bash_streaming.py \
  tests/test_bash_windows_guidance.py tests/test_invariant_bash.py \
  tests/test_desktop_local_script_tool.py tests/test_desktop_local_prompt_priority.py \
  tests/test_desktop_local_worker.py -q -p no:randomly
```

`184 passed, 3 skipped`。

### 4.2 变异检查（12 类走样，全部被对应用例判为失败）

```
.venv/bin/python \
  openspec/changes/align-desktop-project-execution-with-master/evidence/scripts/mutate_prompt_priority.py
```

```
12 类实现走样都被对应用例判为失败，且还原后源文件与原文一致。
```

12 项覆盖：产出不再要求落进项目、暗示项目授权覆盖技能与记忆维护、平台取不到就退回服务器平台、客户端平台未知时不再说明未知、运行时的脚本工具提示干脆不读、服务器会话也回答一个客户端平台、取不到平台就报 `posix`、平台提示报错带崩提示词构建、服务器端项目也声称有客户端平台、本地脚本工具照搬服务器平台说明、平台常量不拆出来、撤下平台说明时顺手重排文本。

用例本身也被元测试盯着（`tests/test_mutation_evidence_scripts.py`）：每项变异必须写出预期失败的用例名、锚点必须仍在源码里、且只能改声明的源文件。

### 4.3 已确认的既有失败（非本次引入）

`tests/test_shared_asset_prompt_guidance.py::test_guidance_reaches_the_llm_request` 失败，原因是该用例用 `Agent.__new__(Agent)` 构造对象却**没有设置 `workspace_scope`**，于是 `get_full_system_prompt()` 抛 `AttributeError` 后回退到缓存提示词，断言随之落空（同文件其余 18 项通过）。

已用 `git stash` 在**改动前**的源码上复跑同一用例确认：失败现象、报错位置与文案完全相同（同样 `'Agent' object has no attribute 'workspace_scope'`）。属既有缺陷，未顺手修改。

---

## 5. 本任务范围

**已完成**：本地项目提示中的产出归属规则；项目授权不覆盖技能/记忆维护（含"不要把客户端路径交给服务端工具"）的口径；`Agent.client_platform()` 的三条来源分支与"未知即未知"不变量；`script_platform()` 的 fail-closed 语义；`Bash` 平台说明的常量抽出与 `IsolatedScriptTool` 的撤下；上述用例与 12 项变异证据。

**不在本任务**：A06/A07/A11—A13/A27 的**两种模式实跑**（真实技能、真实产出内容、依赖版本记录）属 8.8；本节只保证提示层与工具描述层不会把这三件事说错。
