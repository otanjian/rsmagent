# 8.6 跨端输入准备与摘要校验，落盘工具逐个明确处置

任务原文：

> - [ ] 8.6 接通获权服务器附件及远程知识/MCP 结果的输入准备与摘要校验；逐个落盘工具明确本机适配、显式传输或拒绝，不静默回退服务端。

契约依据（`execution-contract.md` §6）：

> 服务器附件用于本机任务时，资源准备返回 `(resource_id, version, digest)`；客户端先下载到本轮临时输入目录并校验，再向工具提供执行端位置。读取远程 MCP 返回的原生引用时同理，不把 `server:/...` 当本机路径。

本任务由两半组成，各自独立可验证：

- **输入准备与摘要校验** —— 服务器资源如何变成执行端的一个真实文件；
- **逐个落盘工具的处置**（验收 A36）—— 每个有 cwd/落盘能力的工具属于本机、显式传输还是服务端，不允许静默回退。

---

## 1. 引用语法：`resource:<id>[@<version>][#<digest>]`

`agent/desktop_local/resource_refs.py` 在既有的 `project` / `skill` / `backend` 前缀之外新增 `resource`：

```
resource:att_9f3c                 # 只要最新可用版本
resource:att_9f3c@v7              # 钉住版本，版本漂移即拒绝
resource:att_9f3c@v7#sha256:ab..  # 再钉住摘要
```

关键点是**没有落地就没有本机路径**：`resolve_ref` 遇到 `resource:` 时抛 `resource_not_landed`，而不是解析成字面名为 `resource:att_9f3c` 的目录。否则「未落地」会退化成令人困惑的「文件不存在」，而这是两种必须可区分的失败。

`parse_ref` 保留摘要原文（不做规范化），规范化只发生在校验处 —— 这样 `resource:a/b.txt@v7#sha256:ABC` 能原样往返，不会在解析阶段就把大小写/前缀悄悄改掉。

---

## 2. 落地引擎：先校验，再原子发布

`agent/desktop_local/resource_landing.py`。一次落地按顺序做四件事，任何一步不过就整体拒绝：

| 步骤 | 检查 | 失败码 |
| --- | --- | --- |
| 1 | 资源 id 是安全的相对名（无 `..`、无绝对路径） | `invalid_reference` |
| 2 | 落地根存在且是目录（**缺失即拒绝，绝不现建**） | `resource_unavailable` |
| 3 | 取回的版本与钉住的版本一致 | `resource_unavailable` |
| 4 | 现算 sha256 同时匹配「来源自报」与「调用方钉住」 | `resource_digest_mismatch` |

三条设计约束：

**摘要必须是现算的。** 拿库里存的摘要去比库里存的摘要等于什么都没验证。来源自报摘要也要查 —— 否则「调用方没钉」就成了完全不校验的旁路。两侧都没有摘要时拒绝，而不是「没要求就放行」。

**钉住的摘要解析不了要报错，而不是降级成没钉。** `declared_pin and not pinned` 显式拒绝：`#sha256:` 写错了是笔误，把笔误当成「没有要求」等于把一次未校验的下载伪装成校验通过。

**发布是原子的。** 先写 `.partial-*` 同目录兄弟文件，校验通过后 `os.replace` 就位。任何中途失败都不留下半个文件 —— 因为落地目录就是工具真正会去读的地方，一个半截文件比一个缺失文件更危险。

落地缓存以 `(relative, version, actual_digest)` 为键（不是以资源 id 为键），所以同一轮内重复引用只下载校验一次；而不同摘要的引用不会被缓存串味。

---

## 3. 接进工具输入准备缝

`agent/desktop_local/source_resolver.py::prepare_tool_inputs(source, tool_name, arguments, transfer=..., skills=..., landing=...)`：

遇到 `resource:` 时调 `landing.land(...)`，把参数里的路径改写成落地后的**本机绝对路径**（位于本轮的 `.cow/run-inputs/<run_scope>/` 下）。没有落地传输、或落地失败时，**整个工具调用按名字拒绝**（`REFUSAL_RESOURCE_NOT_LANDED`），不回退成服务器路径。

这是 8.6 的要点：拒绝是按工具名给出的，模型能看到「这个东西没落地」，而不是拿到一个会静默写错地方的服务器路径。

`agent/protocol/agent_stream.py` 的 `_stage_local_inputs` 通过 `_local_landing()` 提供这层传输，所以派发路径上确实是用到的。

---

## 4. 获权来源：`desktop_run_inputs`

`agent/desktop_local/run_inputs.py`。`RunInputFetcher` 是 `resource:<id>` 背后的具体来源：查 `desktop_run_inputs` 里一行属于调用者自己的记录，读出文件字节。

**授权不在这里重新实现一遍。** 查询按调用者自己的 `tenant_id`/`user_id` 收窄；别人的记录一律报「找不到」而不是「无权访问」—— 与 `delete_run_input` 一致，也不泄露他人附件的存在。再写一套更弱的授权规则正是本模块最不能引入的失败。

**摘要从磁盘字节现算**，不读存储列（同 §2 的理由：文件被替换/截断只有现算才抓得住）。

### 4.1 `artifact_rel` 的基准目录

`client_files` 记录 `artifact_rel` 时用的是相对 **Agent 用户工作目录**（`<workspace>/user/<id>/work`）的路径。这里有一个必须写明的坑：`desktop_publish_ledger` 上**同名列**是相对**发布暂存根**的，两者不可互换。用 `PublishService.abs_of` 去解析本表的列会查到另一个目录。

本模块因此显式按工作目录解析，并新增两条不变量：

- **包含性**：解析结果必须落在该用户工作目录内，否则拒绝（`../` 不能越出）。
- **两侧都 resolve**：基准目录本身可能经过软链（macOS 的 `/var` → `/private/var`），只 resolve 一边会把所有合法输入误判为越界。

> 旁注（**不属本任务、未改**）：`integrations/desktop/publish.py::delete_run_input` 用 `self.abs_of(row["artifact_rel"])` 删除运行输入文件。按上面的基准差异，该路径指向暂存根而非工作目录，`path.is_file()` 为假，于是删除静默地什么都没删 —— 运行输入的磁盘占用不会随删除/过期释放。这是本次改动之前就存在的独立缺陷，未顺手修改，另行提出。见 §7。

---

## 5. 逐个落盘工具的处置（A36）

`agent/desktop_local/tool_disposition.py` 把每个工具有 cwd/落盘能力这件事变成一张**可核查的表**：

| 处置 | 工具 |
| --- | --- |
| `LOCAL` | `read` `write` `edit` `ls` `search_files` `bash` `send` |
| `TRANSFER` | `web_fetch` `browser` |
| `SERVER` | `memory` `knowledge` `scheduler` `env_config` `mcp` `todo` `subagent` `agent_delegate` `vision` `web_search` `client_files` `evolution_undo` `external` |

`disk_writing_tools(tools_dir)` 扫 `agent/tools`，用 AST/正则找出所有会开写句柄的模块（`open(..., 'w'/'a'/'wb')`、`.write_text`、`os.replace`、`shutil.*`、`json.dump`、`.to_excel` 等），得到一个**从代码自动得出的**清单。

然后 `inventory()` 把这张表与自动清单取并：`unclassified_writer_refusal(tool_name)` 对「在自动清单里、但不在处置表里」的工具返回拒绝。`agent_stream.py::_run_tool` 在派发前调用它 —— 于是在本机项目轮次里，一个**新增的、忘了分类的落盘工具会被 `capability` 拒绝执行**，而不是默默按服务端语义跑。

这解决了「分类表会不会过时」的问题：表是靠自动化清单兜底的，新工具不可能因为没人想起更新表就绕过闸门。

---

## 6. 验证

### 6.1 用例

```
.venv/bin/python -m pytest \
  tests/test_desktop_resource_landing.py \
  tests/test_desktop_resource_staging.py \
  tests/test_desktop_tool_disposition.py \
  tests/test_desktop_run_inputs.py \
  tests/test_desktop_local_input_staging.py \
  tests/test_desktop_resource_refs.py -q -p no:randomly
```

`102 passed, 31 subtests passed`。

### 6.2 变异检查（19 类走样，全部被对应用例判为失败）

```
.venv/bin/python \
  openspec/changes/align-desktop-project-execution-with-master/evidence/scripts/mutate_resource_landing.py
```

```
19 类实现走样都被对应用例判为失败，且还原后源文件与原文一致。
```

19 项覆盖：不查版本、不查来源自报摘要、不查调用方钉的摘要、摘要解析不了就当没钉、两侧都没摘要也照样落地、缓存命中不看钉、资源 id 允许上跳、缺目录就现建、把服务器资源当项目路径、无落地传输时退回服务器路径、跨用户可取、跨租户可取、取字节时用库里存的摘要、问路径就建目录、未分类落盘工具照样执行、派发路径不调闸门、run 目录名不过滤分隔符、运行输入上跳不拦、`artifact_rel` 用错基准目录。

用例本身也被「元测试」盯着（`tests/test_mutation_evidence_scripts.py`）：每项变异必须写出预期失败的用例名（否则无法失败）、锚点必须仍在源码里（避免重构后静默跳过）、且只能改声明的源文件。

---

## 7. 本任务范围与边界

**已完成**：`resource:` 引用语法；校验后原子落地的引擎；接进 `prepare_tool_inputs` 的落地缝与按名拒绝；获权来源 `RunInputFetcher`（含基准目录、包含性、现算摘要）；落盘工具处置表与派发闸门；上述用例与 19 项变异证据。

**未做，另行跟进**：

1. **`client_files` 结果路径的落地（属 A13）。** `client_files` 提交附件后返回的是 `agent_user_work_dir` 下的**服务器路径**，并附注「pass this to read/skills, never a client URI」。本机运行要消费它，应当走本文的落地缝返回本机路径。之所以没在 8.6 接线：其字节来源正是 §4.1 的 `artifact_rel` 基准不一致问题，而这牵连到服务端自身的产物记账与授权口径 —— 与 A13 的验收属同一件事，应连同证据一起做（8.8）。
2. **`delete_run_input` 的基准目录缺陷。** 见 §4.1 旁注：删除/过期不释放磁盘。既有缺陷，非本次引入，未顺手修改。
