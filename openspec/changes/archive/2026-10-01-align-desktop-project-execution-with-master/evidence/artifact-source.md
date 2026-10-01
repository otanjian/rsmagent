# 本机产出：来源、引用与卡片（任务 9.1）

对应主规范 `desktop-project-artifacts` 的「产出条目的来源与版本」与
`desktop-project-execution` 的「产出经设备核验后发布」。本文记录**实际改了什么、
怎么验证的、哪里没做**。

## 一、要求与它带来的两个约束

要求是：原工件事件与元数据的来源变成**可选**的 `desktop`，记录设备 / 项目 / run /
tool_call / 相对路径 / 源版本，且**只在真实验证文件存在之后**才发布。

这条要求把实现切成**两端**，任何一端单独做完都不成立：

* 服务器**没有**用户本机的路径，所以它不能核验本机文件，也不该拿同名服务器文件去
  stat（那样会得到一张看起来正确、实际指向别处的卡片）；
* 客户端**有**字节，但它不知道卡片该长什么样，也不该决定"这张卡片给谁看"。

所以：**设备造引用，服务器造卡片**。两端之间只有一个字段（结果帧的 `artifacts`），
它的形状由 `contracts/desktop/v2.json` 冻结。

## 二、交付内容

| 关注点 | 实现位置 | 语义 |
|---|---|---|
| 引用形状 | `contracts/desktop/v2.json` → `artifact` | `source='desktop'` + `artifact_id`/`device_id`/`workspace_id`/`run_id`/`tool_call_id`/`relative_path`/`file_name`/`kind`/`size`/`source_version`，全部必填；`relative_path` 不许绝对 |
| 版本不是名字 | 设备 `artifact.ts::sourceVersion` / 服务器 `artifact.py::source_version` | ≤ 8 MiB（`SOURCE_VERSION_DIGEST_MAX_BYTES`）取 **sha256 内容摘要**；超过则退化为 `stat:<size>-<mtime_ns>`——**不是**文件名。两份 `报告.xlsx` 名字相同、内容不同，必须能被区分，否则缓存会拿旧字节喂给用户 |
| 引用 id | 两端同式 | `art_` + sha256(`run_id \0 tool_call_id \0 relative_path`)[:24]，确定性、不含路径 |
| 设备核验后才造引用 | `desktop/src/main/project-execution/artifact.ts::buildArtifact` | 依次：根目录 **realpath 折叠** → 候选按工具**报的**路径 lstat（末段是符号链接即拒）→ realpath 后必须仍在项目内 → 必须是普通文件 → 才算版本。任一不满足返回 `null`（"没有卡片"是诚实结果；打不开或开错文件的卡片不是） |
| 哪些工具算产出 | `device-execution.ts::artifactsFor` 的 `ARTIFACT_TOOLS = {write, edit}` | 与 master 的 `AgentStreamExecutor._ARTIFACT_TOOLS` **同集**，两种模式对"什么算产出"口径一致 |
| 不猜 `bash` 写了什么 | 同上 | `bash` 的产出**不从命令串推断**、也不做"跑前跑后 diff 整个项目"（那会把本次没创建的文件也报成产出）。脚本产物走文件面板——目录列举才是它该在的地方 |
| 失败运行不发布半成品 | 同上 | 只有 `succeeded` / `cancelled` 才进入核验：`cancelled` 可能已写了一半，仍去核验（文件不在就没有引用）；`failed` **不发布**，因为留下的是半成品而不是这次运行声称的产出 |
| 结果帧携带 | `device-execution.ts::resultFrame` | 核验通过才把 `artifacts` 放进 `execution_result`；空数组则整个字段不出现 |
| 服务器接收 | `agent/desktop_remote/dispatch.py::result_for` | 设备写下的 `artifacts` **原样**转成 `ToolResult.ext_data['desktop_artifacts']`（这一层不改写相对路径） |
| 本机事件的来源 | `agent/protocol/agent_stream.py::_local_artifact_origin` | 本机轮次用 `run_local_cwd(identity)` **当下重验**；解析不出来（撤权、无目标）就**不盖**来源，而不是盖一个空设备 |
| 远程事件的来源 | 同上 `_maybe_emit_remote_artifacts` | 只消费设备报回来的 `desktop_artifacts`，先过 `validate_artifact`（契约校验）再发 SSE；**服务器从不 stat 本机路径** |
| 卡片投影 | `channel/web/fork/runtime.py::_build_artifact_payload` / `_local_artifact_payload` | 本机卡片带 `source:'desktop'`、`local:true`、`revalidate:true`、`relative_path`，并**剥掉** `abs_path`/`raw_url`/`preview_url`——留着它们会让前端去请求一个服务器上并不存在的路径 |
| 历史重建 | 同上 `_artifacts_from_steps` | 按生成时的来源重建：从落库的 `result.abs_path` 走 `source_for_session` 判来源，本机条目挂 `desktop_origin` 并产出干净的本机载荷 |

形状之外还有一条**两端同式**：`protocol_kind()`（服务器）与 `artifactKind()`（设备）
都把扩展名映到契约的六个桶（`text`/`office`/`image`/`pdf`/`archive`/`binary`），
未知扩展名归 `binary`。这不是巧合而是必须一致——同一个文件在两种模式下不该显示成
不同的种类。

## 三、验证

命令固定为 `.venv/bin/python -m pytest <子集> -q -p no:randomly` 与
`node --test tests/<name>.cjs`。

| 套件 | 结果 | 覆盖 |
|---|---|---|
| `tests/test_desktop_artifact_source.py` | **26 通过** | 服务器端：来源字段、版本≠名字、卡片无服务器 URL、本机/远程发射、撤权不盖来源、历史重建 |
| `tests/test_desktop_device_artifacts.cjs` | **16 通过** | 设备端：真实进程写真实文件，逐项比对 `size`/`source_version` 与磁盘字节；`bash` 不产引用；失败运行不产引用；越界/符号链接/目录/不存在的文件都不产引用 |
| `tests/test_desktop_remote_dispatch.py` | 通过（含 `test_a_device_report_is_never_turned_into_a_server_path`） | 设备结果帧 → `desktop_artifacts` 的转交未被改写 |
| `tests/test_desktop_execution_v2_contract.py` | 通过 | `artifacts` 的契约校验：跨项目、绝对路径都被拒 |
| `tests/test_mutation_evidence_scripts.py` | 10 通过 / 252 子测试 | 变异脚本自身的锚点、期望与"只改声明过的源" |

设备端套件**不用替身**：每个用例都真的 spawn 一个写文件的进程（或在直接测
`buildArtifact` 时真的在临时目录里造文件、符号链接和目录），所以 `size` 与
`source_version` 是**磁盘上的字节**算出来的。

`source_version` 在超过 8 MiB 时退化为 `stat:` 而不是摘要，是**刻意的**：给刚产出
的多 GB 文件算摘要会把它要服务的那一次点击一起卡住（契约 §6）。用例
`a large file uses a cheap version rather than hashing gigabytes` 同时钉住两侧——
8 MiB+1 得 `stat:`，同目录下的小文件仍得 `sha256:`。

## 四、变异检查：两端共 10 类走样，10/10 被用例判失败

脚本 `evidence/scripts/mutate_desktop_artifact_source.py`（可复跑），日志
`evidence/artifact-source-mutations.log`。它同时驱动 pytest 与 `node --test`：设备端
那个套件自己按源文件 mtime 决定是否重编，所以改过 `.ts` 后它读到的一定是变异后的
字节，不需要外挂构建步骤。

| 走样 | 被哪条用例抓住 |
|---|---|
| M1 本机产出不记录来源 | 契约字段 / 发射用例（6 项失败） |
| M2 本机文件也发服务器 `raw_url`/`preview_url`/`abs_path` | 卡片无服务器 URL（4 项） |
| M3 版本取文件名（服务器） | 版本≠名字（2 项） |
| M4 服务器产出也盖本机来源 | 后端目标不盖来源 |
| M5 撤权运行也盖本机来源 | 撤权不盖来源 |
| M6 设备生了文件却不带引用 | 产出引用 / 契约字段（4 项） |
| M7 版本取文件名（设备） | 版本=内容摘要 |
| M8 设备不判越界 | 符号链接目录 / 项目外绝对路径 |
| M9 设备只信工具的一面之词 | 10 项（不存在 / 符号链接 / 目录 / 越界…） |
| M10 失败运行的半成品也发布 | 失败后写过文件的运行不发布 |

**一处诚实说明**：M9 是"整段捷径"，它同时打掉存在性、普通文件和越界三重核验，所以
命中的用例最多。三重核验各自**单独**改掉时并不都能单独观测到（例如目录其实还会被
`sourceVersion` 的读取失败兜住），因此没有为"只去掉目录判断"伪造一个能过的变异——
那种变异会让脚本报"未被发现"，而那既是事实、也不值得为它弱化断言。

## 五、未完成（不作完成性主张）

* **本条的"设备"是产品自身的执行端点**（`DeviceExecution` + 真实子进程），不是在
  打包好的安装件里跑的真机；安装件内验收属第 10 组（10.1–10.3）。
* **Windows 未验证**：`buildArtifact` 的路径规则走 `node:path` 的平台分支，但
  Windows 启动器本身尚未验收（4.5/4.9），本条结论仅对 macOS/posix 成立。
* 卡片在**文件面板**中的呈现、系统打开/另存为与"历史里点开旧产出"分别是 9.2–9.6 的
  范围，本条只到「结果帧里的引用 + SSE 卡片载荷」为止。

## 六、本轮修掉的一处真实缺陷：卡片发射把成功的工具调用改写成失败

**发现方式（任务 1.6 的切片核对）**：复核治理切片时跑
`tests/test_action_approval_consumer.py`，两条**期望成功**的用例
（`test_the_same_action_runs_when_its_approval_matches`、
`test_an_undeclared_action_still_runs_with_a_recorded_basis`）变成 `wrong status - error`。

**根因**：`_maybe_emit_remote_artifacts` 的守卫写成 `if not self.on_event`，而这一行**在**
方法自身的 `except Exception`（`# noqa: BLE001 - metadata must not fail the turn`）**之外**。
该方法的调用点在 `_execute_tool` 的 `try` 内，其 `except Exception` 会把逸出的异常
转成 `{"status": "error", "result": str(e)}`。两处相叠的结果是：

- 异常读的是**本应只影响呈现**的字段（事件接收器），
- 造成的却是**把产物写成功的一轮标成失败**，模型读到的是
  `'AgentStreamExecutor' object has no attribute 'on_event'` 这句话，而不是它自己的写入结果。

这正好落在本条自己的禁令上：卡片是投影，不能被伪造成产出，也不允许把产出降级为失败。
`_maybe_emit_artifact`（流式主循环内调用，无任何兜底 handler）更严重——同样的写法会**中断整轮**。

**修复**（`agent/protocol/agent_stream.py`）：

1. 两处守卫改读 `getattr(self, "on_event", None)`——报告步骤不因最小化构造的执行器而抛错；
2. 把本地发射拆成 `_maybe_emit_artifact`（守卫）+ `_publish_artifact`（实际发布），
   后者整体包在 `except Exception` 内并 `logger.warning`，与远端那半的既有口径一致。

**先红后绿**：新增 `tests/test_desktop_artifact_source.py::EmissionIsolationTests` 5 项
（缺失接收器 ×2、校验器抛错、接收器抛错、本地写入仍出卡片）。修复前 2 项失败
（同一条 `AttributeError`），修复后 `test_desktop_artifact_source.py` +
`test_action_approval_consumer.py` 共 **66 passed, 4 subtests**。

**为什么不是改测试**：`object.__new__(AgentStreamExecutor)` 只出现在测试与 harness 里，
生产路径一律走 `__init__`。所以既可"给 fixture 补 `on_event = None`"了事，也可修根因。
选后者，因为**"呈现失败→工具失败"这个耦合本身**是缺陷，即使 token 永远存在，
校验器抛错、载荷形状变化同样会走到同一条路径上；`on_event` 只是最先撞到它的一种触发。
