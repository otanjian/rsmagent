# 技能资源与版本缓存证据（任务 8.1 / 8.2 / 8.3）

本文件记录第 8 组前三个任务的实现边界、用例证据与变异结论。8.4–8.8 未完成，
本文件不对其作任何完成性主张（见文末「未完成部分」）。

## 1. 落地物

| 文件 | 位置 | 职责 |
| --- | --- | --- |
| `agent/desktop_local/package_rules.py` | 设备侧 | 两端**共用**的打包规则：相对路径校验、凭据拒绝、内容摘要 |
| `agent/desktop_local/skill_cache.py` | 设备侧 | 作用域版本缓存：校验 → 原子发布 → 引用计数 → 回收 → 授权闸门 |
| `agent/skills/manifest.py` | 服务器侧 | 在 `SkillManager` 已选出的 `SkillEntry` 上构建获权 manifest |
| `tests/test_desktop_skill_cache.py` | 用例 | 35 项 |
| `tests/test_skill_manifest.py` | 用例 | 18 项 |
| `tests/test_mutation_evidence_scripts.py` | 用例 | 10 项（钉住变异脚本自身的失败解析器） |
| `evidence/scripts/mutate_skill_package.py` | 变异 | 12 类实现走样 |

依赖方向是刻意的：`agent/skills/manifest.py` 导入设备侧的 `package_rules`，反过来不行。
设备 worker 必须在沙箱里只靠标准库跑起来，所以它不得导入服务器技能机制；而服务器
**必须**知道设备接受什么，否则会造出设备必然拒绝的包，且失败会表现得像传输问题。

## 2. 两头共用一个实现，而不是两份约定

这是本节唯一「不这样做就会静默走样」的地方。若两端各写一份"这个路径安全吗 /
这是不是凭据"，它们会漂移，而漂移是不可见的：

- 设备**拒绝**一个合法包 → 看起来像传输 bug；
- 设备**接受**一个非法包 → 在被利用之前看起来什么也没发生。

因此规则只有一份，并且有用例强制共用：

```
test_the_manifest_builder_and_the_cache_share_these_rules
test_a_skill_cache_error_speaks_the_shared_rule_codes
```

前者的做法不是断言"两边都 import 了同一个模块"（那是测实现），而是**让服务器造包、
让设备装包**：manifest builder 从真实目录生成包 → `read_skill_payloads` 取字节 →
`SkillCache.publish` 接受并 `verify` 通过。任何一侧偷偷改宽或改严，这条接缝都会断。

限额同理，不允许自造：`DEFAULT_LIMITS` 的每个值都由用例比对
`contracts/desktop/v2.json` 的 `limits`（`test_the_default_limits_are_the_contracts_own_numbers`）。
自造的限额不可评审，契约里的限额是已评审的。

## 3. 摘要的性质

`_content_digest` 每条资源贡献 `路径 + 内容摘要 + 大小`，并在**函数内部**排序：

- 覆盖全部资源，而不只是 `SKILL.md` —— 只哈希说明文件的摘要会在脚本被换掉时保持不变，
  而设备自身无法发现这次替换；
- 排序在函数内而非依赖调用方 → 摘要只是"因为调用方恰好排过序"才稳定，离每次部署都
  看起来像新版本只差一次重构。

`test_the_digest_does_not_depend_on_directory_listing_order` 直接对**逆序**输入比对摘要，
并单独断言产出清单按路径有序 —— 只对同一目录连调两次是测不出顺序依赖的（同一文件系统
状态下 `os.walk` 顺序本就稳定）。

## 4. 作用域命名：既不能碰撞，也不能不可读

`_component` 产出「可读前缀 + 全值 sha256 前 16 位」：

- **只做清洗**会碰撞：`https://a/b` 与 `https://a_b` 清洗后同名，两台服务器的缓存合进
  一个目录，等于把一台服务器的缓存交给另一台。origin 是 URL，合法地含有 `://` 和 `/`，
  所以"拒绝含分隔符的值"会直接拒掉所有正常 origin。
- **只做哈希**则无法阅读，排查缓存时要反查。

空值是唯一被拒的输入：那意味着身份缺失，在其下缓存会把不同身份混在一起。

## 5. 引用计数与"不得复用整体同步逻辑"

`SkillManager.sync_skills_to_workspace` 对目标目录执行 `shutil.rmtree(target_skills_dir)`
再整体复制。本缓存**明确不复用**它，原因是要点本身：

- 版本目录以自身摘要命名，升级的 `v2` 落在 `v1` **旁边**，不替换；
- `gc` 只回收计数为 0 且不在 `protect` 中的版本，所以在跑的运行读完整个版本；
- 提供 `protect` 参数，使调用方的在途运行在计数记错时仍不被删。

索引损坏时 `gc` 返回空并保留文件：计数未知时，"没人引用"不是可推断的事实，猜错就是
删掉在跑的技能。

## 6. 撤权后不能借缓存继续使用

`resolve` 与 `acquire` 都接受一个**现网**授权回调，默认回落到缓存自身的 `authorized`：

- `resolve(...)` 在未授权时返回 `None`（不是替代版本）——调用方拿不到它要的那一版就必须
  如实报告，静默跑一个更旧或不同的版本正是"被撤销的授权又生效"的路径；
- 构造 `SkillCache` 时若不传 `authorized`，用例会显式传入回调，任何用例都不可能意外依赖
  "字节在盘上就等于有权限"。

## 7. 用例与变异

```
.venv/bin/python -m pytest \
    tests/test_desktop_skill_cache.py tests/test_skill_manifest.py \
    tests/test_mutation_evidence_scripts.py -q -p no:randomly
# 63 passed, 55 subtests passed
```

分文件：`test_desktop_skill_cache.py` 35 项 / 7 subtests、
`test_skill_manifest.py` 18 项、`test_mutation_evidence_scripts.py` 10 项 / 48 subtests。

回归子集（技能 + 桌面 local 共 10 个文件）：

```
264 passed, 55 subtests passed in 50.03s
```

### 变异结果

```
.venv/bin/python \
    openspec/changes/align-desktop-project-execution-with-master/evidence/scripts/mutate_skill_package.py
```

12 类走样全部被对应用例判为失败，且每轮还原后源文件与原文逐字节一致
（完整输出见 `evidence/skill-package-mutations.log`）：

| 变异 | 走样内容 | 判失败的用例 |
| --- | --- | --- |
| M1 | 摘要只覆盖说明文件 | `test_changing_a_script_changes_the_digest` |
| M2 | 摘要随遍历顺序变化 | `test_the_digest_does_not_depend_on_directory_listing_order` |
| M3 | 跳过载荷摘要校验 | `test_a_tampered_payload_is_refused_by_digest` |
| M4 | 允许包外文件 | `test_an_undeclared_file_cannot_ride_along` |
| M5 | 不查已发布版本里的软链接 | `test_a_symlink_inside_a_published_version_is_refused_on_verify` |
| M6 | 回收忽略引用计数 | `test_gc_never_removes_a_pinned_version` 等 3 项 |
| M7 | 解析不查授权 | `test_resolve_refuses_a_cached_skill_once_authorization_is_gone` |
| M8 | 作用域丢掉服务器维度 | `test_a_different_server_never_lets_a_tenant_name_collide` |
| M9 | 不拒绝凭据文件 | `test_a_credential_shaped_file_is_refused_as_skill_content` 等 3 项 |
| M10 | manifest 打包时无视软链接 | `test_a_resource_reaching_outside_the_skill_is_refused` |
| M11 | 授权检查变成可选 | `test_the_authorization_check_is_required_not_optional` |
| M12 | 发布失败后不清理暂存 | `test_a_crash_at_the_atomic_step_leaves_no_version_and_no_staging` |

### 变异暴露出的三处真实问题（均已修）

首轮 12 项中 3 项"未命中"，逐个查明后只有一项是用例缺陷：

1. **M3 —— 用例缺陷。** 原用例的篡改载荷比原文**短**，于是 `size_mismatch` 先触发，
   用例写成"`digest_mismatch` 或 `size_mismatch` 均可"就通过了。这使一条**长度**用例
   伪装成摘要用例。改为等长不同内容（`b"A"*32` vs `b"B"*32`）并只接受 `digest_mismatch`。
2. **M12 —— 用例缺陷（覆盖面缺口）。** 原用例的"发布失败"发生在**校验**阶段，而校验
   在 staging 之前，因此它从未走到清理路径，断言"暂存已清理"是空话。新增
   `test_a_crash_at_the_atomic_step_leaves_no_version_and_no_staging`，patch `os.replace`
   在原子落位处抛出，直接驱动清理路径。
3. **M9 —— 验证脚本缺陷。** `assertRaises` + `subTest` 的失败以 `SUBFAILED(name='.env')`
   出现在汇总里，而解析器锚定 `FAILED\s+`，`SUBFAILED` 后面紧跟的是 `(name=...)` 而非
   空白，因此匹配不到。当时用例**已经失败**，脚本却报"未命中"。这正是仓库已记录过的
   `rg "^FAILED"` 误判同类问题，故新增 `tests/test_mutation_evidence_scripts.py` 把解析器
   本身钉住（含 `SUBFAILED`、去重、纯 `F` 进度行不得被当作失败），并新增"锚点必须仍命中
   源码"的用例 —— 锚点漂移必须**响亮失败**而不是静默跳过。

## 8. 8.4 类型化引用与执行端定位

8.1–8.3 都是**库**：一个 manifest 构建器、一个版本缓存、一套引用计数。库本身正确、用例也
正确，但**没有生产入口调用它**。本仓库已经出现过"库和用例全绿、生产从不调用"的案例
（清理逻辑存在、自测通过、从未执行），所以 8.4 的验收标准是**接缝可达**，而不是"库又多了一个
函数"。两条真实路径各自有用例：

```
SkillManager.filter_skills → LocalSkillRuntime.prepare → SkillCache.publish → RunSkillSet.pin
AgentStreamExecutor._stage_local_inputs → prepare_tool_inputs → 真实本机路径
```

### 8.1 三类根，各有一条规则

| 类型 | 根 | 可写 | 拒绝条件 |
| --- | --- | --- | --- |
| `project:` | 所选项目 | **是**（唯一） | 越界分量 / `..` / 反斜杠 |
| `skill:` | 8.2 缓存中为本轮 pin 的版本 | **否** | 未 pin、资源缺失、越出该版本 |
| `backend:` | 无本机等价物 | —— | 一律 `server_path_not_local` |

逻辑写法 `skill:builtin:excel/templates/report.xlsx` 是给模型看的**可解析位置**；技能 id 自带
冒号，故资源边界取第一个 `/` —— 正好是 `source:name` 本来就有的文法。

关键点：**裸路径行为不变。** `parse_ref` 对"本模块不拥有的形式"（裸绝对路径、反斜杠、
`..`）返回 `None`，交回调用方原有规则；只有显式带前缀或干净的裸相对路径才由本模块处理。
所以 3.6 的既有语义与拒绝文案都没变（`./notes.txt`、`notes.txt`、`project:notes.txt` 三者
用例断言等价）。

### 8.2 只读与产出落点

- `ensure_writable`：技能资源 → `skill_cache_read_only`。项目产出**不可能**落进缓存：
  `test_project_output_never_resolves_into_the_skill_cache` 直接断言解析结果不在缓存根下。
- `ensure_readable`：技能资源可读。沙箱侧不需要新机制 ——
  `sandbox.ts` 早已把 `skillRoots` 排除在 `writable` 之外，`RunSkillSet.roots()` 就是喂给它的值。
  即"只读"由 OS 边界强制，本模块只负责给出正确目录。

### 8.3 不全局替换（spec 明文禁止）

本模块**只读**：它解析路径，从不改写技能文件，也从不对 `bash` 命令串做文本替换。
两个理由都写进代码注释：改写技能源码是把"这里跑不了"伪装成"能跑"，正好掩盖要报告的不兼容；
替换命令文本则是"用字符串过滤冒充隔离"，既会漏掉 `$(cat ...)`，又会破坏任何**恰好提到路径**
的普通命令。两条用例钉住：

- `test_resolving_a_skill_resource_does_not_touch_the_skill_source`：解析前后，技能源目录与
  缓存版本目录**逐文件字节比对**。
- `test_a_shell_command_string_is_never_text_substituted`：命令原文一字不改，而同一调用里的
  `path` 参数**照常**解析 —— 区分"是程序"与"是路径"。

### 8.4 自己引入的两个泄漏（用例先红后修）

写 8.4 时引入了两类"引用计数只增不减"的缺陷，而且都**由用例发现**，不是靠读代码：

1. **`RunSkillSet.pin` 不幂等。** 对同一版本连 pin 两次 = acquire 两次，而 `release_all`
   只减一次，计数永不归零 → `gc` 永远回收不了。修法是同版本直接返回；换版本时先 release 旧的。
2. **`LocalSkillRuntime.prepare` 不幂等。** 每次调用都新建一个 `RunSkillSet`，**丢弃**上一套
   pin：每轮泄漏一个版本。修法是已构建则复用（带 filter 时先 release 再重建）。

### 8.5 变异验证（18 项，本机全中）

`evidence/scripts/mutate_skill_package.py` 扩到 **18** 项，新增 M13–M18 针对 8.4：
把 `skill:` 当项目路径、重复 pin、重复 prepare、允许写技能缓存、缺失资源用项目文件顶替、
放行 `backend:`。

**一处需要说明的发现：M14 起初未被检出。** 去掉幂等早返回后，"release 旧 pin 再 acquire"
确实让**计数仍然平衡**（1 → 0 → 1），所以只断言计数的用例看不出差别。但"平衡"不等于"安全"：
中间存在计数为 0 的窗口，并发 `gc`（另一轮结束、或清理扫描）可能在该窗口回收目录，于是
版本从仍在读它的运行脚下消失。因此补了
`test_pinning_the_same_version_does_not_release_it_first`，直接断言**不发生 release**，
而不是只断言计数。M14 随后被检出。这条也是全组里唯一"变异揭示了用例强度不足"而非
"变异揭示了实现缺陷"的例子。

```bash
# 本机结果
$ python evidence/scripts/mutate_skill_package.py
# 36 处子用例命中（18 变异 × 2），0 处漏检，退出码 0
```

回归：`test_desktop_resource_refs.py`(37) + `test_desktop_skill_runtime.py`(18) +
`test_desktop_source_resolver.py` + `test_desktop_local_e2e.py` +
`test_desktop_target_resolution_acceptance.py` + `test_desktop_skill_cache.py` +
`test_skill_manifest.py` + 其余触及 `agent_stream`/`skill_manager` 的 50 个文件
= 992 项中 990 通过；2 项失败为**既有环境依赖**（`test_shared_asset_prompt_guidance`、
`test_private_agent_capability_save`，在**未含本次改动**的基线上同样失败，见下节）。

### 8.6 明确的边界（不作完成性主张）

- **`skill:` 引用的解析依赖已部署的版本。** 生产接线做的是"把本轮已授权的技能发布到缓存并
  pin"；设备端**增量下载**（只传变更版本、带断点/凭据的传输通道）属打包与传输范畴
  （8.5/10.x），当前未做。因此本机模式下"部署"= 从本机已有技能目录发布进缓存。
- **`backend:` 与"远程输入落地"是两件事。** 前者是"服务器路径"，本机一律拒绝；后者是
  `source_resolver.prepare_tool_inputs` 的 `transfer` 注入点（3.6 已留），真正的获权落地
  传输在 8.6。
- **`skill.use` 现网授权**已接到 `SkillManager.is_authorized`，且 `acquire` 每次复核；但
  "撤销后在**同一轮内**立即停用已 pin 的版本"需要每轮重新 pin，当前粒度是每轮一次。

## 9. 未完成部分（不作完成性主张）

- **8.5**：已完成（依赖锁定与随包交付），见 `evidence/skill-dependencies.md`。
- **8.6**：未接服务器附件与远程知识/MCP 结果的输入准备与摘要校验。
- **8.7**：未调整项目与共享维护提示的优先级。
- **8.8**：未执行 A06/A07/A11–A13/A27 的两种模式验收，也未记录依赖版本与产出证据。

另需说明两处**已知边界**，以免被当成已完成：

- 载荷目前是**明文文件**，因此"展开预算"与"传输预算"数值相同。`_check_budgets` 仍把两者
  分开检查，以便将来接入归档路径时不会绕过较大的展开预算；但**当前没有归档解包实现**。
- `SkillManifest.base_dir` 是服务器本地路径，`to_dict()` 不含它
  （`test_the_manifest_never_carries_the_servers_own_path_to_the_device` 断言 wire 形式里
  没有服务器路径）；设备侧把它解析成实际路径由 **8.4 的 `resolve_ref`** 完成
  （`resource_refs.py`），即逻辑位置 → 本机路径的映射已经落地。

## 10. 本次回归中**非本次改动引入**的失败

子集 992 项里 2 项失败，两者都在**未含本次改动**的工作区（`git stash` 掉
`agent_stream.py` / `manager.py` 后）**同样失败**，因此是既有缺陷，按仓库规则**不顺手修**，
单独列出：

| 用例 | 现象 | 判断依据 |
| --- | --- | --- |
| `tests/test_shared_asset_prompt_guidance.py::test_guidance_reaches_the_llm_request` | 期望的提示文案与现网不同，且日志出现 `'Agent' object has no attribute 'workspace_scope'` | 基线同样失败 |
| `tests/test_private_agent_capability_save.py::ModelDependencyTests::test_a_model_the_owner_was_granted_passes` | `403 Forbidden`（读本机真实授权/身份数据） | 基线同样失败 |

两者的共同点是**依赖本机真实部署数据**（花名册/授权库），属于该仓库已记录的
"换台机器可能红"的类型，与 8.4 的类型化引用无关。
