# 8.8 代表技能在两种模式的执行验收（A06 / A07 / A11–A13 / A27）

任务原文：

> - [ ] 8.8 执行 A06/A07/A11—A13/A27，在两种模式验证未改写的代表技能资源及输出内容；记录依赖版本与产出证据。

验收条目原文（`acceptance.md`）：

- **A06** 本地与远程各运行固定 Excel 技能 → 汇总单元格为 350；报告真实位于所选项目；可用系统应用打开；输入摘要不变
- **A07** 本地与远程各运行带模板和辅助资源的文档技能 → 模板资源齐全、输出字段正确；技能/记忆目录未搬进项目；项目以外缓存未被写入
- **A11** 技能包传输中断、摘要错误、路径穿越、symlink、超预算；并发执行时升级包
- **A12** 缺 Python/处理库、技能硬编码服务器路径，或 Linux 服务器驱动 Windows 设备
- **A13** 技能消费服务器附件、知识/MCP 结果或 web_fetch 下载文件
- **A27** 脚本尝试读身份库、主进程配置、其他用户缓存或写技能目录；请求未授权网络/凭据

结论先写：

| 验收 | 本地模式 | 远程模式 |
| --- | --- | --- |
| A06 | ✅ 真实执行，汇总 = 350 | ❌ **未接线**，见 §3 |
| A07 | ✅ 真实执行，模板与辅助资源齐全 | ❌ **未接线**，见 §3 |
| A11 | ✅ 越界技能目录被启动器拒绝 | ⛔ 依赖远程接线 |
| A12 | ✅ 缺依赖/平台不兼容指名报错、未授权技能不部署 | ⛔ 依赖远程接线 |
| A13 | ✅ 服务器路径被拒绝、落盘输入按项目引用读得到 | ⛔ 依赖远程接线 |
| A27 | ✅ 沙箱内读不到服务器工作区、写不进技能目录 | ⛔ 依赖远程接线 |

**这次验收本身发现并修掉了一个真实缺口，同时暴露了另一个仍未接线的缺口。** 两者都记在下面。

用例：

```
.venv/bin/python -m pytest tests/test_desktop_skill_acceptance.py \
    tests/test_desktop_remote_skill_acceptance.py -q -p no:randomly
# 17 passed（原先记录 8.9 缺口的那条 skip 已随机制落地改成正向断言）
```

---

## 0. 代表技能：未改写

两个夹具技能写在现有技能格式里（`SKILL.md` + `scripts/` + 资源），依赖在 frontmatter 声明，**按原样运行**：

`tests/fixtures/skills/summary-workbook`（A06）

- 输入：三条合成记录 100 / 200 / 50，由 `scripts/make_input.py` 生成到**项目里**；
- 产出：`scripts/summarize.py` 在 `--output` 下写 `汇总报告.xlsx`（含 `汇总` 单元格 = 350）与 `汇总结果.json`；
- 读 xlsx 用标准库（zip + 少量 XML），写 xlsx 用 `xlsxwriter` —— 这正是技能声明的依赖。

`tests/fixtures/skills/report-document`（A07）

- `assets/template.md`（模板，含 `{{...}}` 占位符）、`references/fields.md`（辅助资源）、`scripts/render.py`；
- 只用标准库，产出 `报告.md` 与 `渲染结果.json`。

两个脚本都按 `SKILL.md` 相对定位自己的资源，因此**连"资源位置适配"都不需要**——验收允许适配，这里没用到。

### 依赖版本

```
python      3.14.3
xlsxwriter  3.2.9
```

`tests/test_desktop_skill_acceptance.py::environment_record()` 会把这两项连同解释器路径一起产出，便于将来"结论不一致"与"版本变了"两种情况区分开。A06 的依赖不是假设的：`test_a12_a_missing_dependency_is_named_by_skill_module_and_distribution` 用**真实锁定清单**（`desktop/build/requirements-desktop.txt`）验证声明被满足，再用空清单验证缺项会被指名报出。

---

## 1. 本轮修掉的真实缺口：pin 的技能目录到不了沙箱

**症状**：本地模式要跑技能脚本，脚本在技能缓存里，而沙箱只放行「项目 + 临时目录 + 解释器运行时」。`skill:xxx` 的目录既没进 `readable`，也没进 `skillRoots`，于是脚本连自己都读不到。

**根因**：8.4 造出了 `RunSkillSet.roots()`，注释也写明「这就是喂给沙箱 `skillRoots` 的值」，但从 Python 到启动器这一段**没有任何人把值传过去**：

```
agent/desktop_local/script_executor.py   script_scope()  // 只发标识符
agent/protocol/agent_stream.py           tool_view_for_run(...)  // 不带技能根
desktop/src/main/local-execution/executor-endpoint.ts
                                         buildGrantLaunchPlan({ projectRoot, tempRoot })  // 没有 skillRoots
```

`desktop/src/main/local-execution/sandbox.ts` 早就把 `skillRoots` 当只读授权，`launch.ts` 也早就透传——**缺的只是中间这段**。

**修法**（两侧都改，且把信任锚留在 shell 侧）：

1. `script_scope(identity, target, skill_roots=...)` 增加 `skill_roots`（排序去重，保证同一 pin 集得到同一请求）；
2. `IsolatedScriptTool` / `isolate_script_tools` / `tool_view_for_run` 逐层接收 `skill_roots`；
3. `AgentStreamExecutor._local_skill_roots()` 从**本轮 pin 的** `RunSkillSet.roots()` 取，随视图一起下发（视图与本轮的目录是同一件事，不能一个有一个没有）；
4. `executor-endpoint.ts` 增加 `skillCacheRoot` 配置作为**信任锚**：请求里的每个技能根，只有在 `isInsideRoot(candidate, skillCacheRoot)` 成立时才被授予；越界 → 403 `skill_root_outside_cache`；没有锚点 → 403 `skill_cache_unavailable`；
5. worker 的缓存键从 `grantId` 改成 `grantId + 技能根集合`：沙箱 profile 是每个 worker 建一次，换了 pin 集就必须换 worker，否则会继承上一轮的授权面（宽了是泄漏，窄了是莫名失败）；
6. `launch-token.ts` 导出 `COW_DESKTOP_SKILL_CACHE_ROOT_ENV`，供 shell 侧（Group 10）把锚点告诉自己。

**为什么"后端点名目录"不算把路径信任交给请求**：项目目录由启动器从自己的授权表解析（请求里从不出现路径）；技能版本目录后端才可能知道（是它 pin 的），所以锚点放在 shell 已拥有的 `skillCacheRoot` 上——请求能做的最多是**指向 shell 自己目录下的子目录**。这与"请求不得命名任意路径"并不矛盾，且越界是**拒绝而不是丢弃**：静默的部分授权会让沙箱运行失败在一个没人看得见的原因上。

---

## 2. 本地模式：逐条验收

### A06 —— 汇总 350、报告在项目里、输入摘要不变

`test_a06_the_fixed_workbook_skill_summarises_to_350_in_the_project`

流程全是真的：真实 `SkillManager` 选出技能 → 真实 `LocalSkillRuntime` 校验摘要后发布并 pin → 真实 executor endpoint（node 进程 + loopback + 启动令牌）→ 真实 `sandbox-exec` worker 在项目里跑脚本。

断言：

- `汇总结果.json` 的 `records == [100.0, 200.0, 50.0]`、`summary == 350.0`；
- **测试自己**解 zip 读 `汇总报告.xlsx` 的数值单元格，`350.0` 在其中——脚本写对了 JSON 却写错工作簿也会被抓到（这正是夹具注释里说的那个陷阱：读 `t="s"` 共享字符串索引当金额会得到错误的和）；
- 产出在项目内、不在技能缓存里（`assert_produced_in_project`）；
- 运行前后输入工作簿的 sha256 **逐字节相同**；
- 服务器侧默认工作区（`个人工作区.md` + 哨兵字节）完全没有变化。

### A07 —— 模板与辅助资源齐全、输出字段正确、目录没搬进项目

`test_a07_the_document_skill_renders_from_its_own_template`

- "模板资源齐全"查的是**已部署版本**（pin 目录）里的 `assets/template.md` 与 `references/fields.md`，不是源码树——部署丢了资源才是这条验收要防的；
- 输出文档里五个字段的值逐个出现，且**没有任何 `{{` 残留**（占位符没被替换会当场暴露）；
- 项目目录里不出现 `SKILL.md` / `assets` / `references` / `scripts` / `memory` / `记忆` / `.skills`——运行技能是**就地读**，不是把它搬进项目；
- 产出不在缓存里；服务器侧未变。

### A11 —— 越界技能目录被启动器拒绝

`test_a11_a_skill_root_outside_the_cache_is_refused_by_the_launcher`

- 请求点名缓存之外的目录（这里用服务器工作区）→ 调用被拒，`run_script` 返回 `(None, 理由)`；
- 同一请求在**没有锚点**的启动器上也被拒（`skill_cache_unavailable`）：shell 没说缓存在哪，就不能授权任何目录。

并发升级包（A11 的另一半）由 8.3 的引用计数与 `gc` 覆盖：升级发布的 `v2` 落在 `v1` 旁边，在跑的运行仍读 `v1`（见 `evidence/skill-resources.md`）。

### A12 —— 缺依赖、平台不兼容、未授权技能

三个独立用例，各自都要求**指名**而不是静默：

- `test_a12_a_missing_dependency_is_named_by_skill_module_and_distribution`：真实锁定清单下 `check_skills` 返回空（声明被满足）；空清单下报 `dependency_missing`，消息里同时有技能名、`xlsxwriter` 和该改哪个文件（`desktop/build/requirements-desktop.txt`）。两半缺一不可——只测失败那半，只证明"检查会说缺失"。
- `test_a12_a_skill_the_identity_may_not_use_is_not_deployed`：**真实** `IdentityService` + 真实角色授权。验收账号拿到两个夹具技能的 `skill.use`，两个都部署；第二个真实成员只被授权工作簿技能 → 只部署工作簿，且文档技能的**字节**（按内容而非目录名搜索）不在它的缓存里。
- `test_a12_an_unsupported_platform_is_reported_not_silently_skipped`：以 `win32` 平台请求部署，要么部署成功要么按 id 报问题；两者皆空 = 静默跳过，直接失败。
- `test_a12_a_script_reading_the_skill_cache_is_allowed_but_never_writes_it`：往 pin 目录里写文件**不成功**，而读同一个目录里的 `SKILL.md` **成功**——两条一起才说明这是"只读"而不是"够不着"。只读由 OS 边界强制（`sandbox.ts` 从不把 `skillRoots` 放进 `writable`），不是靠 Python 记账。

### A13 —— 服务器路径被拒绝、落盘输入按项目引用读得到

- `test_a13_a_server_absolute_path_is_refused_not_translated`：裸绝对路径与 `backend:` 引用都抛 `ResourceRefError("server_path_not_local")`，且**消息里带文件名**——一句没有解释的 "not found" 会让模型去找一个同名本地文件，那正是本条要防的；
- `test_a13_a_landed_server_input_is_read_from_the_project`：落到项目里的服务器输入，之后按普通 `project:<相对路径>` 解析成功。落盘那半是 8.6 的证据（`evidence/resource-landing.md`），这里钉的是**之后的引用方式**不是服务器路径。

### A27 —— 读不到不该读的、写不进不该写的

- `test_a27_a_script_may_not_read_the_agent_workspace`：`cat <服务器工作区文件>` 之后内容没有出现在返回里；
- 写技能目录（见 A12 那条）；
- 身份库与主进程配置：由沙箱的「默认拒绝 + 只放行系统运行时」承担，`desktop/src/main/local-execution/sandbox.ts` 的 `(deny default)` 与 `tests/test_desktop_local_execution.cjs` 已覆盖。

---

## 3. 远程模式：技能交付已接线（任务 8.9 闭环）

本节原先记录的是"远程半边没接线"的三处缺口。三处已在任务 8.9 同轮闭合，所以本节
改成**钉住闭合后的行为**，并保留"当时缺什么"的写法 —— 让"这个缺口不会再静默回来"
和"它当年缺在哪"都能读到。

**当时的缺口（三处，各有一个断言在实际变化时失败）**：

1. **设备侧没有技能缓存、也没有包传输**：`project-execution/` 里 `skill` 只出现在"写日志"
   和"契约字段"的位置，没有 `skillCacheRoot`、没有 `skill-cache.ts`，设备拿不到技能的**字节**；
2. **设备不校验声明的摘要**：命令里的 `skill_resources` 只被记进日志，没拿去和本地将要运行的
   版本比对，于是快照过期的设备会安静地跑另一个版本 —— `incompatible_skill`（422）这个错误码
   在契约里存在，却没有任何一侧会产生它；
3. **服务端没把本轮技能集落到 command 行**：集合被校验但没被存下来，所以设备声明技能集只会
   得到冲突、声明空集反被接受，运行**无法被要求**使用某个版本。

**现在每一处对应的行为**（`tests/test_desktop_remote_skill_delivery.py` **21 项**，跑的是
**真技能目录**而不是夹具摘要）：

- 服务端把规范化集合落到 command 行并回读，`canonical_skill_resources` 是**唯一**的规范化
  入口 —— "摘要覆盖的集合"与"交给设备的集合"不可能漂成两张表；
- prepare 与 start 两条**独立**路径都按「声明必须等于行上集合」双向比对：另一个版本 / 子集 /
  **空集** 都是 `incompatible_skill`/422；而"本来就没有技能集"的运行仍接受空声明（兼容面）；
- `GET /api/desktop/execution/skill-package`：先按命令行**授权集合**判定 `(skill_id, digest)`，
  再用**实时** `skill.use` 重建 manifest 并要求重算摘要相等，最后按文件数 / 展开字节 / wire
  字节三个预算切 base64。发回的字节逐项等于磁盘字节、且摘要相符；
- **授权集合是唯一判据**：命令没有技能集、或技能集是别的技能时，请求一个**真实且是当前**的
  版本同样被拒（只有这一条能把"授权集合"那层与"摘要重算"那层分开，理由见 §4）；
- 服务器上被改动过的技能按名拒绝，而不是按旧版本名把新字节发出去。

设备侧（`tests/test_desktop_skill_transfer.cjs` **13 项**、
`tests/test_desktop_device_execution.cjs` **29 项**）：

- `skill-cache.ts` 以摘要为键，`install` 先写 `.partial-*` 再单次 `renameSync`，摘要不符
  **什么都不留下**；
- `skill-transfer.ts` 只补缺失的版本、首个失败即整体拒绝（半个集合比没有更坏）；
- `device-execution.ts` 的 `verifySkills` 在**写日志与运行之前**闸门，失败即 `incompatible_skill`；
- `remote/local-read-assembly.ts` 把解析出的缓存目录交给与 §1 **同一套**启动器，worker 按
  `skillSetKey` 分桶 —— 沙箱 profile 每个 worker 只建一次，换一组 pin 就必须换一个 worker。

**仍未完成**：安装件内真机端到端（随包 Python、签名后的设备、真实模型工具消息）属
10.1—10.3；Windows 未验证。

---

## 4. 变异验证

本轮**实跑**了三处走样，确认断言真的会红：

| 走样 | 改法 | 结果 |
| --- | --- | --- |
| 视图不再携带本轮的技能根 | `isolate_script_tools` 给 `IsolatedScriptTool` 传 `skill_roots` 一行去掉 | A06/A07 + 两条脚本用例共 **4 项失败** |
| 端点不校验技能根是否在锚点内 | `if (!isInsideRoot(candidate, anchor))` 短路成永假 | `test_a11_a_skill_root_outside_the_cache_is_refused_by_the_launcher` **失败** |
| 摘要不覆盖技能集 | `canonical_envelope` 把 `skill_resources` 归成 `[]` | `test_a06_the_digest_is_a_function_of_the_declared_skill_set` **失败** |

改毕已全部还原，复跑 17 passed。

### 8.9（远程交付）的六处走样，实跑 6/6 命中

脚本 `evidence/scripts/mutate_skill_transfer.py`，日志 `evidence/skill-transfer-mutations.log`：

| 走样 | 改法 | 被哪条用例判红 |
| --- | --- | --- |
| 服务端不比对授权集合 | `if {...} not in authorized:` → `if False:` | `test_a_package_this_command_was_never_authorized_with_cannot_be_pulled`、`test_a_package_outside_a_non_empty_set_is_refused_too` |
| 服务端不重算摘要 | `if manifest.digest != digest:` → `if False:` | `test_a_version_the_server_would_not_ship_is_refused_by_name` |
| 服务端重新接受空声明 | `if resources != authorized:` → `if resources and resources != authorized:` | `test_declaring_nothing_is_refused_when_the_run_has_a_set` |
| 设备端接受对不上声明的回答 | `if (String(payload.digest \|\| '') !== request.digest)` → `if (false)` | `an answer for a different digest is refused, not installed` |
| 设备端不重算包摘要 | `if (actual !== expected)` → `if (false)` | `a package whose bytes do not match the declared digest is refused` |
| 设备端不校验声明的技能版本 | `verifySkills(frame)` 结果换成 `undefined` | `a frame whose skills are not here is refused before anything runs` |

**这一轮变异当场证伪了两条"通过但空转"的用例**，这是本节最该留下的部分：

1. **服务端"授权集合"那层**：删掉后 19 项**仍全绿**。原用例请求的是"服务器造不出的摘要"，
   被**下一层**摘要检查兜住了 —— 两个守卫叠在一起，测掉了下面那个就等于两个都没测。
   补上「命令**没有**技能集 / 技能集是别的技能 → 请求一个**真实且是当前**的版本」两条，
   才第一次真正钉住这一层。
2. **设备端"重算包摘要"那层**：删掉后 13 项**仍全绿**。原用例答复里的 `digest` 字段就是
   被换掉字节的摘要，在**传输层**（"答复摘要必须等于请求摘要"）就被拒了，
   `SkillCache.install` 的重算根本没走到。改成"答复**声称**是请求的摘要、字节却不是"
   ——也就是**服务器说谎**这一真实威胁——这一层才可观测。

**设计上会红、但本轮未逐项实跑**（列在这里以免被读成已验证）：

- worker 按 `grantId` 而非 `grantId + 根集` 缓存：换了 pin 集仍复用旧 profile（宽了是泄漏、窄了是莫名失败）；
- `_local_skill_roots()` 返回**全部**授权技能而非本轮 pin 的：授权面宽于本轮；
- 端点没有锚点时按"放行"处理：`skillCacheRoot` 缺省即禁用校验（A11 第二条断言会红）。

---

## 5. 回归

- `tests/test_desktop_skill_acceptance.py` + `tests/test_desktop_remote_skill_acceptance.py`：17 passed（原先 1 项带原因的 skip 已改成正向断言）；
- 8.9 相关子集：`tests/test_desktop_remote_skill_delivery.py` + `test_desktop_remote_skill_acceptance.py` + `test_desktop_execution_broker.py` + `test_desktop_skill_cache.py` + `test_skill_manifest.py`：**123 passed / 7 subtests**；`node --test test_desktop_skill_transfer.cjs test_desktop_skill_cache.cjs test_desktop_skill_package_digest.cjs test_desktop_device_execution.cjs`：**67 passed**；
- `tests/test_desktop_local_script_tool.py`、`tests/test_desktop_run_context.py`、`tests/test_desktop_local_prompt_priority.py`、`tests/test_desktop_resource_refs.py`：144 passed；
- `node --test tests/test_desktop_local_executor_endpoint.cjs`：19 passed；
- `npx tsc -p tsconfig.main.json`：无错误。

### 5.1 8.9 的技能闸门踩到的既有用例（已修）

`DeviceExecution.verifySkills`（8.9）在**工具执行之前**拒绝"声明了技能但本机没有技能缓存"的
帧。`contracts/desktop/samples/v2/execute_tool.valid.json` 后来加上了 `skill_resources`，
于是把该样例当输入、又只关心**产出引用**那一半的 `tests/test_desktop_device_artifacts.cjs`
（9.1）整个变成 `failed`：写文件真的没发生，断言自然全红。这不是用例太弱，而是**闸门
确实生效了**，只是那条用例的夹具不该被顺带改语义。

修法不是给 9.1 的用例补一个技能缓存（那会让它同时测两件事），而是在该套件的 `frame()`
里显式删掉 `skill_resources` 并写明原因：技能闸门由 `test_desktop_skill_transfer.cjs` 与
`test_desktop_device_execution.cjs` 覆盖。修完该套件 16/16 通过，9.1 的变异脚本
`mutate_desktop_artifact_source.py` 重跑仍 10/10 命中（此前它的基线是红的，任何"命中"都
不可信）。

依赖记录（A06/A07 实际运行版本）：Python 3.14.3、xlsxwriter 3.2.9。
