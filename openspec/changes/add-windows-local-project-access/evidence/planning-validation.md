# 规划校验（2026-10-15）

## 本轮修订与重新校验

本轮承接v3合同、operations授权及可行性审查，并修正DeepSeek指出的设计/任务/验收不同步问题。以下结果来自本轮修改后实际执行；日期未变是因为修订发生在同一天。

- 合同定案为同一project_execution的v3；3.1成为业务接入硬前置，交付expected_version、operations、许可摘要及可选脚本扩展，旧v2 Windows拒绝和POSIX/Bash保持。run_script仅为暂名，最终名称由合同唯一登记。
- D6、任务7.1及验收统一为比较“扩展冻结worker后复用”和“最小随包CPython”后择优；现有worker不是通用脚本入口，不再要求先做无法直接复用的原型证明。
- Cargo从无条件libc依赖改为平台依赖是待实施任务；helper须统一实际.exe名称、开发态/包内候选，再验证accessSync及真包。补Windows构建与extraResources任务。
- 写入采用expected_version写前阻断并验证权限/属性保持；operations缺省无新权限、旧grant不升级。权限生命周期原型覆盖继承、NULL/空DACL、硬链接、移出对象、并发修改及残留恢复。
- helper故障拒绝新脚本；在途脚本按实际安全依赖决定继续或取消，无法验证产物标待核验。此规则取代上一轮“一律停止在途脚本”的表述。
- 映射历史local_execution 23项/project_source 17项skip到当前测试及W/S编号，保留mac专属与旧v2拒绝；两阶段分别完成Windows文件和脚本等价验证，不机械移除skip。
- execution-isolation与harden-windows-desktop-reliability、add-shared-agent-release-governance串行同步，逐次重核基线。未修改这两个change或主规范。

| 实际执行命令 | 结果 |
|---|---|
| openspec validate add-windows-local-project-access --strict | exit 0；Change is valid |
| D:/tools/micromamba/envs/py312/python.exe scripts/check_change_deltas.py add-windows-local-project-access | exit 0；OK (proposed)，增量与基线一致、冲突文件均有说明 |
| openspec status --change add-windows-local-project-access | exit 0；proposal/specs/design/tasks，4/4规划产物齐全 |

当前四份delta按标题实际统计：

| Capability | Requirement | Scenario |
|---|---:|---:|
| execution-isolation | 2 | 8 |
| scoped-project-browser | 2 | 5 |
| windows-local-project-files | 9 | 20 |
| windows-local-project-scripts | 6 | 16 |
| 合计 | 19 | 49 |

任务仍为37项未实施（第一阶段24项、第二阶段13项）；验收编号保留W01–W16、S01–S12。新增operations需求和场景，不新增执行框架或任务编号。

## 历史记录的适用范围

此前18条requirement的统计属于修订前版本；此前“先验证worker复用，不能才补CPython”“v2兼容扩展或升级待选”“helper故障一律停止在途脚本”均已被本轮决策替代。旧校验结果不能作为本轮内容通过的证明，本节以实际重跑结果及当前数量更新记录。

本轮只修改本change规划文档，未修改产品源码、其他change、主规范、平台支持声明、ACL或生产配置。校验通过仅证明规划格式与增量一致性；未执行Windows原生helper、LPAC、运行时或ACL安全探针，不能据此声明两阶段已实施或完整可行。

## 实施：任务3.1（v3合同定稿）

规划已收口，实施从3.1开始，因为D4把它列为业务接入硬前置。本阶段只交付合同与校验，**不接入任何运行时行为**：broker、网关、队列、派发、capability矩阵和桌面客户端均未改动，所以“旧v2声明不变”在每一步都能验证。

### 交付物

| 文件 | 性质 | 说明 |
|---|---|---|
| `contracts/desktop/v3.json` | 新增 | 同一project_execution的v3：`win32.supported=true`并新增`launcher_kinds`（files有launcher、scripts为空）、`tools.by_platform`、`operations`、`version_precondition`、`run_script`可选扩展 |
| `auth/desktop_contracts_v3.py` | 新增 | v3的唯一Python读取/校验模块。`auth/desktop_contracts_v2.py`一字未改 |
| `desktop/src/main/project-execution/generated-contract-v3.ts` | 新增（生成） | 由生成器按major 3渲染 |
| `scripts/gen_desktop_execution_types.py` | 修改 | 改为按协议主版本参数化、多版本生成；新增`inherits`链式解析 |
| `tests/test_desktop_execution_v3_contract.py` | 新增 | 67项断言 |
| `contracts/desktop/samples/v3/` | 新增 | 7个固定样例，含Windows“文件可用、脚本未就绪”的hello与capability块 |
| `tests/test_desktop_execution_v2_contract.py` | 修改 | 仅把v3生成模块加入“生成层”豁免集合（该测试原本会把生成文件误判为`worker_memory_bytes`的新消费者） |

### 验证结果（实际执行）

| 命令 | 结果 |
|---|---|
| `python scripts/gen_desktop_execution_types.py --check --version 2` | exit 0：**v2产物逐字节未变** |
| `python scripts/gen_desktop_execution_types.py --check` | exit 0：两个major均与提交产物一致 |
| `python -m unittest tests.test_desktop_execution_v3_contract` | Ran 67 tests, **OK** |
| `python -m unittest tests.test_desktop_execution_v2_contract tests.test_desktop_execution_types` | Ran 70 tests, **OK** |
| `python -m unittest tests.test_desktop_execution_payload tests.test_desktop_compatibility_matrix tests.test_desktop_meta` | 全部OK |
| `openspec validate add-windows-local-project-access --strict` | exit 0，Change is valid |
| `python scripts/check_change_deltas.py add-windows-local-project-access --conflict-baseline scripts/conflict-baseline.txt` | exit 0，增量与基线一致 |
| `python -m unittest tests.test_desktop_execution_broker` | **42 failures，已证实为既有失败** |

### 实施中修正的设计错误

都是先构建用假帧试出、再修正的真实缺陷，其中两个是安全语义上的：

1. **`operations`的成员原本写成工具名**，那样它就只是工具白名单的副本，**区分不出“文件权限”和“脚本权限”**，v3的存在理由落空。已改为操作名（`files`/`scripts`），并以`required_for`把每个工具映射到它不可或缺的操作，`contract_problems()`强制“files操作不得打开任何脚本工具”。
2. **`write`在无`create`时不要求`expected_version`**，等于默认路径允许无版本覆盖，与D3直接矛盾。已改为只有显式`create: true`才豁免。
3. `bash`被误要求携带language；bash是POSIX shell，无语言字段，会误判合法调用。
4. `tools_under_operation`把`sets`当操作名读，导致`scripts`算出的工具集错误。

另外发现`v1.json`已有`materialize.expected_version`（v1只读协议），因此v3的`expected_version`是沿用既有约定并加强为写前强制，不是新造概念。

### 与既有失败的区别（必须记录）

`tests/test_desktop_execution_broker`的42项failure**在本轮改动之前就存在**：用`git stash`暂存本次全部改动后重跑，结果为同样的`Ran 47 tests / FAILED (failures=42)`，报错为`503 project execution is not available (platform_unsupported)`。`tests/test_mutation_evidence_scripts`的12项error同样是既有问题，指向`openspec/changes/align-desktop-project-execution-with-master/...`这一**归档前的旧路径**。两项均不由本change引入，本change也不据此勾选任何依赖它们的任务。

### broker 42项既有失败的根因（已确认）

不是缺陷，是**测试设计如此**，已有书面记录：`openspec/changes/harden-windows-desktop-reliability/evidence/capability-declaration-and-gates.md:18` 明确写着 `BrokerEndpointTests` 的 42 项在 Windows 主机上**全部**收到 `503 {"code":"platform_unsupported"}`，"这就是'Windows 无可用工具即拒绝'的正确表现（**该组端点在 POSIX 主机上才返回 200**）"，并按 14.4 标为**本平台不适用**。同口径另见 `a-item-local-coverage.md:39`。

触发点确认为 `channel/web/fork/handlers/desktop.py:723` 的端点门禁（`project execution is not available (%s)`），它在 broker 之前先按 `v2.platform_supported(...)` 拒绝；本机为 Windows，故 `win32.supported=false` 直接拦住，开关与授权语义根本走不到。

**对本change的影响**：不构成阻塞。平台门读的是**设备行里申报的平台**（`integrations/desktop/execution_broker.py:59`：`macos/linux→posix`、`windows→win32`），因此后续任务可用申报 posix 的合成设备走通新路径；v3 的 Windows 文件面另有 capability 交集判定，不依赖本机主机平台。本change不修改这批测试的期望值，也不把它们的失败计作通过。

## 实施：任务3.2（平台与运行时验收状态）

### B批（1.1、2.1–2.4）为什么没做

**硬阻塞：本机无Rust工具链。** `cargo`/`rustc` 不在PATH，`%USERPROFILE%\.cargo`、`%USERPROFILE%\.rustup`、`C:\Rust`、`D:\Rust`、`D:\tools\rust` 均不存在，`desktop/native/fs-guard/target` 也没有既有产物。按技能"接口占位或模拟实现不算通过"，B批**一项都未勾选**，也未留下任何假产物。①.1的句柄探针、2.1的平台分层、2.3的写入helper都必须在能编译Rust的环境里做。

### 交付物

| 文件 | 说明 |
|---|---|
| `integrations/desktop/execution_capability.py` | 新增 `execution_state_v3`（多一个「验收」输入）、`_acceptance`、`helper_usability`、`accepted_runtimes`、`execution_state_for(version)` |
| `agent/desktop_remote/device.py` | `_execution_block` 改为**按设备申报的major选校验器**；`SCRIPT_TOOLS` 改为从合同派生（原来硬编码`("bash",)`）；平台取信封记录而非设备自报 |
| `agent/desktop_remote/__init__.py` | `CONTRACT_PLATFORMS` 单一定义，broker改为导入同一份，消除两处重复 |
| `tests/test_desktop_execution_capability_v3.py` | 新增，27项 |
| `contracts/desktop/v3.json` | `capability.reason_values` 增加 `feature_unavailable` |
| `desktop/src/main/project-execution/generated-contract-v3.ts` | 重新生成 |

### 验收状态的表达方式

按D4「有效能力取部署策略、平台证据、双方版本/工具/运行时协商、本机后端、当前授权的交集」，Windows验收状态走**既有 `config.available_setting`** 机制（与两个既有开关同一套读取），**默认空**，因此v3落地本身不打开任何Windows能力：

- `desktop_project_execution_platforms`、`desktop_project_scripts_platforms`、`desktop_project_script_runtimes`、`desktop_project_helper_unavailable_platforms`
- 这些**不是新产品开关**：它们只能记录「某平台/某语言的证据已通过」，无法放宽交集；名字精确匹配，不认前缀或通配。

### 验证结果（实际执行）

| 命令 | 结果 |
|---|---|
| `python -m unittest tests.test_desktop_execution_capability_v3` | Ran 27 tests, **OK** |
| v2合同 + v3合同 + capability_v3 + target_wire + phase2_gates + release_gates + meta | Ran 213 tests, **OK (skipped=1)** |
| `python -m unittest tests.test_desktop_remote_dispatch` 等4套 | Ran 139 tests, **OK (skipped=1)** |
| `scripts/gen_desktop_execution_types.py --check`（两个major） | exit 0 |
| `openspec validate --strict`、`check_change_deltas.py` | 通过 |

### 实施中修正的问题

1. **Windows脚本面在v3下永远不可用**，即使配置了验收列表。原因：`current`的`launcher_kinds.scripts`为空 → `tools.by_platform.win32` 不含脚本工具 → capability 必须报平台事实。这是**正确**的（D6/PowerShell要经过独立原型），已写成断言：`test_windows_scripts_stay_closed_however_the_lists_are_set`。
2. **我一度改动了posix的历史脚本语义**（给posix也加了逐语言前置），违反「posix保持原样」。已修：posix下逐语言列表只是**可选的收窄**，其缺失不得关闭capability矩阵已验收的脚本面。
3. **空runtime列表在win32与posix上语义不一致**：现在区分「平台未被配置」（未记录，不收窄）与「平台被配置为空」（运营明确说无语言验收，关闭）。
4. **helper可用性只对win32生效**：posix的同名记录被忽略，避免一条陈旧条目打断一个本来正常的平台，但记录本身仍会在返回里标出。
5. **v2设备申报的块会被v2校验器判定**：原先硬编码v2，会把v3设备（可能含Windows文件工具）整块丢弃并静默降级为「无执行」。现按申报major选校验器，未知major明确丢弃并告警。

### 未做（属后续）

`/api/desktop/meta` 仍广播 v2 的 major。它是**公开且无设备上下文**的端点，只能广播一个major：改成3会让v2客户端 `negotiate()` 读到3≠2而整体拒绝，违反旧v2语义保持。因此v3协商走 `hello`（按设备申报），meta的版本提升与端点门禁改判归 **3.3**（网关/派发接入）统一处理。本任务未开放任何Windows能力。

### 本阶段未做（属后续任务，不在此处勾选）

3.1只定义`operations`/`expected_version`的合同与校验。**运行时尚未读取它们**：`integrations/desktop/execution_payload.py`仍只产出v2帧，broker仍按`v2.PROTOCOL_MAJOR`判定，capability矩阵、持久字段迁移和跨版本读取属3.2/3.3，v3的跨语言digest实现在3.3。因此本次**未开放任何Windows能力**，也未修改平台声明。

## 实施：任务3.3（operations授权切片，部分完成，未勾选）

**该任务未勾选。** 3.3要求「网关/队列/broker接入有限helper派发」，而helper二进制不存在（B批硬阻塞，本机无Rust）。因此我只交付了**不依赖helper**的服务端授权切片，并明确记为部分完成——派发接线未做，也就没有可验证的端到端。

### 交付物

| 文件 | 说明 |
|---|---|
| `integrations/desktop/execution_payload.py` | `canonical_envelope`/`params_digest` 增加可选 `fields` 参数；新增 `contract_for_major`、`digest_fields_for`。**不传参即v2，逐字节不变** |
| `integrations/desktop/execution_broker.py` | 新增 `_contract`（按**行**的major选合同）、`_check_operations`、`_operations_from_column`；`_check_protocol`/`_check_platform` 改为版本感知；`_open` 接入operations校验；**补上缺失的 `import json`** |
| `auth/store.py` | 迁移50：`desktop_binding_workspaces.operations_json`、`desktop_commands.operations_json`、`desktop_commands.runtime`，全部可空、**无默认值、不回填** |
| `tests/test_desktop_execution_operations.py` | 新增，29项 |

### operations 的三重集合校验

每个集合堵一个不同的洞（`_check_operations`）：

1. **grant的集合**（`desktop_binding_workspaces.operations_json`）是权威——用户确认的那一份，命令不能超出它；
2. **命令的快照**（`desktop_commands.operations_json`）决定该命令被创建时是为了什么——若读live grant，则**入队之后**做的确认会追溯授权一个已排队的命令；
3. **帧的申报**只能再收窄，设备不能拿files的命令去要scripts。

两个概念被严格区分：**NULL = 从未写入（无授权）**，**`[]` = 写了空集（同样无授权，但是记录在案的决定）**。不可解析一律按**未写入**处理——"读不出授权"绝不能读成"没有授权要查"。

### 验证结果（实际执行）

| 命令 | 结果 |
|---|---|
| `unittest tests.test_desktop_execution_operations` | Ran 29 tests, **OK** |
| operations + commands + payload + v2合同 + v3合同 + capability_v3 + 两个迁移演练 | Ran 264 tests, **2 failures** |
| `openspec validate --strict`、`check_change_deltas.py` | 通过 |

**那2项失败是既有问题，已用基线证实**：`git stash` 暂存本次全部产品改动后重跑 `test_desktop_project_migration_drill`，得到**完全相同的2项失败**。其一为路径分隔符（`\Users\drill\...` vs `/Users/drill/...`，Windows本机所致），其二为业务行重写对比；两者均与本change无关，本change不修改其期望值。

**v2摘要逐字节未变**：`params_digest` 对 `samples/v2/digest_envelope.valid.json` 仍算出 `sha256:ccaac830dbfb...`，与样例记录的digest一致；v3字段表多覆盖 `operations`/`runtime`，同一envelope得到不同摘要——这正是新major的证明。

### 本轮修正的实现缺陷

1. **`execution_broker.py` 从未 `import json`**：`_check_declared_resources` 里的 `_looks_like_digest` 路径依赖它，异常被 `except Exception` 吞掉。我的 `_operations_from_column` 因此把所有**字符串**输入静默判为「未授权」——一个会让合法v3命令全部失败的缺陷，而且失败方向是"过严"所以更难发现。已补import，并用14组输入逐一验证。
2. **`_operations_from_column` 把 `'[]'`（写了空集）当成「未写入」**，与我自己写的文档相反。已修，并把 `None`/`[]` 的区分写成断言。
3. **`_check_operations` 一度用 `getattr(command, ...)` 读申报集合**——`command` 是dict不是对象，该分支永不生效。已改为显式参数。
4. **`_check_platform` 对v3行会用v2的工具表判定**，把Windows文件命令误报为「平台不支持」；真因是调用方比行更旧。已改为按行的major选合同，未知major明确拒绝而非猜测。

### 3.3 仍未完成的部分（所以不勾选）

- **helper派发接线**：把命令真正派给本机有限helper的部分未实现，因为helper不存在。
- **`/api/desktop/meta` 的major提升与端点门禁改判**：公开端点只能广播一个major，改3会让v2客户端整体拒绝；需要与派发一起设计。
- **v3的跨语言digest**：服务端已可算v3摘要，但桌面客户端 `contract.ts` 仍用v2的 `DIGEST_FIELDS`，客户端侧未跟。
- **`runtime` 的持久化写入路径**：列已存在，尚无写入方（取决于命令创建路径，属派发接线）。
- 因此3.3要求的「测试撤权、跨租户/设备、超时和审计/配额失败」中，**授权相关的组合已测**，**端到端派发相关的未测**。

### meta 的版本广告：实测得出的不变量（不勾选任何任务）

前一条把「meta的major提升」记为待设计。本轮实测确认为一个**两个方向都坏的困境**，并把它固定成不变量：

| meta广播 | v2客户端 | v3客户端 |
|---|---|---|
| 2（现状） | `available` | `protocol_incompatible` |
| 3 | `protocol_incompatible` | `available` |

公开端点每个协议名只能带一个版本，因此无法同时满足两个major。结论：**meta停在必须继续服务的最旧major（现在是2），v3协商完全走 `hello`**——hello的块是**按lease**存储的，同一台服务器可以对v2设备给v2块、对v3设备给v3块，这正是公开端点做不到的。

已落地为 `auth.desktop_contracts_v3.advertised_major()`（返回 `min(SUPPORTED_MAJORS)`）与5项断言，其中一项**直接读 `channel/web/fork/handlers/desktop.py` 的源码**核对它仍声明 `desktop_contracts_v2.PROTOCOL_MAJOR`——这样以后有人把meta改成3，测试会失败而不是静默打断所有v2客户端。`SUPPORTED_MAJORS` 一旦去掉2，`advertised_major()` 自然变成3。

## 测试基线的两处既有不稳定（供后续接手参考）

完整跑本轮涉及的全部套件（332项）时出现 **2项失败**，均**不是本change引入**，且都表现为**顺序/时序脆弱**而非确定性错误：

| 套件 | 现象 | 基线复测 |
|---|---|---|
| `test_desktop_remote_dispatch.BackgroundHandleTests` | `a withdrawn queue entry produced no work`（`1 != 0`） | **暂存本change全部产品改动后同样失败，且失败数更多**（2项，含 `0 != 1`） |
| 同上（第2项） | 仅在全套一起跑时出现，单独跑该套件有时只有1项 | 同属时序脆弱 |

关键证据：**基线运行比带改动运行失败得更多**（2项 vs 1–2项波动），因此失败方向与"改动引入回归"相反。该套件本轮先前单独运行过并通过，说明它随测试选择与执行时长波动。

`test_desktop_project_migration_drill` 的 2 项失败则**确定且可复现**（基线同样2项）：一是路径分隔符（`\Users\drill\...` vs `/Users/drill/...`，Windows本机所致），二是业务行重写对比。

本change不修改上述任何测试的期望值，也不把它们计作通过或失败。

## 当前阻塞（连续三轮同一条件，未解除）

**B批（1.1、2.1–2.4）无法执行**：本机无 Rust 工具链，且**无可用链接器**。

- `cargo`/`rustc` 不在PATH；`%USERPROFILE%\.cargo`、`%USERPROFILE%\.rustup`、`C:\Rust`、`D:\Rust`、`D:\tools\rust` 均不存在。
- `link.exe` 不存在，`C:\Program Files\Microsoft Visual Studio`、`C:\Program Files (x86)\Microsoft Visual Studio`、`C:\BuildTools` 均不存在——**Rust 的 MSVC target 无法链接**。
- `winget` 可用，但补齐这个工具链需要安装 rustup **加**数GB的 VS Build Tools（通常还需管理员权限），属于环境级变更，未获用户授权前不擅自执行。

连带阻塞：**3.4**（`expected_version` 写前阻断）、**4.x**（面板/模型闭环）、**5.1**（release.yml + extraResources 无产物可验证）。**3.3** 的剩余部分（helper派发接线、客户端v3 digest、`runtime`写入路径）同源于此。

**解除阻塞只需其一**：授权安装 Rust 工具链（rustup + `x86_64-pc-windows-msvc` + VS Build Tools）；或改由具备该工具链的环境接手 B 批。

## 实施：B批解除阻塞与任务2.1完成

### 工具链安装（用户授权后执行）

用户授权后安装。一个关键约束改变了技术路线：**当前账号不是管理员**，因此**无法安装 VS Build Tools**，MSVC target 不可用。改用 **`x86_64-pc-windows-gnu`**——GNU target 只需 MinGW 链接器，而它由 rustup 自带（`self-contained`），无需管理员。

过程：`winget` 因 `msstore` 源与网络中断失败 → 改为直接下载官方 `rustup-init.exe`（gnu host）→ 安装 stable 1.99.0，`--profile minimal --no-modify-path`（不改 PATH，调用方用绝对路径）。验证：`cargo 1.99.0`、`rustc 1.99.0`、`stable-x86_64-pc-windows-gnu` 为默认工具链。

**先验证可行性再动产品代码**，三个探针通过后才开始改：

| 探针 | 结论 |
|---|---|
| 最小 crate + `libc` + serde（GNU target，无系统 `cc`） | 编译、链接、运行全部成功 |
| `NtCreateFile` + `RootDirectory` 逐组件相对打开 | `sub`、`inner` 依次相对父句柄打开成功；含分隔符名字被拒 `0xC0000033`；不存在组件被拒 `0xC0000034` |
| 句柄式目录枚举与 stat | `dir`/`reparse`/`nlinks`/`size`/`write_time` 齐全，枚举出全部条目 |

工具链位置：`%USERPROFILE%\.cargo\bin\cargo.exe`（未改 PATH）。

### 交付物（模块拆分）

| 之前 | 现在 |
|---|---|
| `src/paths.rs`（448行，unix-only） | `src/paths/mod.rs`（平台无关）+ `paths/unix.rs` + `paths/windows.rs` |
| `src/ops.rs`（683行） | `src/ops/mod.rs`（**全部逻辑**）+ 各后端的系统调用原语 |
| `src/grants.rs`（306行） | `src/grants/mod.rs`（共享）+ `entropy` 子模块（unix `/dev/urandom` / Windows `BCryptGenRandom`） |
| `src/main.rs`、`src/protocol.rs` | **未改动**（本就零平台代码） |

`Cargo.toml`：`libc` 移入 `[target.'cfg(unix)'.dependencies]`；新增 `[target.'cfg(windows)'.dependencies] windows-sys`。

**设计要点**：`list`/`stat`/`read`/`search` 的**全部逻辑**放在共享的 `ops/mod.rs`，只把需要系统调用的原语（重开目录、枚举、按名 stat）交给各后端。因此"列目录、有界、可取消"只有**一份**受审实现，两端不可能漂移。

### 验证结果（实际执行）

| 命令 | 结果 |
|---|---|
| `cargo build --release`（Windows，删 target 全量重建） | exit 0，**0 警告**，产出 `fs-guard.exe` **1,090,560 字节** |
| `cargo test --release`（Windows） | 单元 **45 passed** + 集成 **23 passed**，**0 失败** |
| `cargo check --release --target x86_64-unknown-linux-gnu` | exit 0，**0 警告** |

**交叉检查是必要手段**：本机构建永远发现不了 unix 侧的问题。加上 linux target 后立即抓到我自己的错误——`read_names`/`stat_at`/`stat_of` 只实现在 Windows 后端，而它们原本在 `ops.rs` 里。这正是"两端共享逻辑"必须配一个能编译另一端的检查的原因。

### 实施中修正的缺陷（都是靠探针实测，而非推断）

1. **`FileIdBothDirectoryInfo` 不能用于 `NtQueryDirectoryFile`**。`windows-sys` 的 `FileIdBothDirectoryInfo = 10` 是 **`GetFileInformationByHandleEx` 的 `FILE_INFO_BY_HANDLE_CLASS`**，与 `NtQueryDirectoryFile` 的信息类是**不同枚举**；混用得到 `STATUS_INVALID_INFO_CLASS`。用探针遍历 1–14 后确定可用值为 **1、2、3、12**，其中 **10 无效**。改用 **3（`FileBothDirectoryInformation`）**，并自定变长结构（`windows-sys` 未导出该结构）。
2. **`NtCreateFile` 不接受 DOS 路径**。`C:\dir` 得到 `STATUS_OBJECT_PATH_SYNTAX_BAD`；它要对象管理器路径 `\??\C:\dir`。修正后所有绝对打开通过。
3. **NT 的相对打开拒绝 `"."` 作为名字**（`STATUS_OBJECT_NAME_INVALID`）。unix 的 `openat(parent, ".")` 无对应写法，因此 Windows 重开目录改为**按已存根路径绝对打开，再核对句柄身份与授权时是否一致**，不一致即拒绝——路径只用于**到达**对象，绝不用于**授权**它。
4. **`GetFileInformationByHandleEx` 的枚举位置不可靠**：换新句柄后第二次枚举仍可能返回空。改 `NtQueryDirectoryFile` 并显式 `RestartScan`，实测同一句柄可重复得到相同结果。
5. 加 `OBJ_DONT_REPARSE`：绝对打开路径上任何组件是 reparse point 即**失败**，而不是静默跟随。

### 未验证的部分（不得读作已通过）

- **POSIX 侧只做了编译级验证**（`cargo check --target x86_64-unknown-linux-gnu`），**未在 POSIX 主机上运行测试**。本机是 Windows，无 POSIX 主机。因此任务2.1里的"回归POSIX"目前是**类型级**通过，运行级回归仍待 POSIX 主机。
- 集成测试 `a_device_file_is_refused`（字符设备经链接到达）是 **unix-only**，因为夹具需要 `/dev/null`。Windows 侧的 reparse 拒绝由 `paths/windows.rs` 自己的测试覆盖（真实建立 reparse point 并断言拒绝），**没有跳过该保证**。
- **任务2.2、2.3 未勾选**：2.1 的产出是"拆分 + Windows 只读入口可构建可测"；2.2 要求的支持说明与更多路径用例（长路径、中文、大小写、目录替换）以及 2.3 的写入入口与 `expected_version` 仍待做。

## 实施：任务2.2完成（路径形态覆盖与支持说明）

任务2.2的核心——`list`/`stat`/`read`/`search` 的根句柄操作、身份验证、有界帧、分页与取消——在2.1的共享实现里已经具备（`ops/mod.rs` 承载全部逻辑，两端各自提供系统调用原语）。本任务补齐的是它点名要求的**路径形态测试**与**支持说明**。

### 新增测试（集成，Windows 上真实运行）

| 测试 | 覆盖 |
|---|---|
| `non_ascii_and_spaced_names_round_trip` | `资料/明细 2024.csv`、`我的 项目 空格`：列出后**按报告的名字原样再打开**并读回字节 |
| `a_name_that_differs_only_in_case_is_handled_consistently` | 大小写不同的名字：同对象或明确拒绝**二者之一**，不允许"成功但返回错误对象"或崩溃 |
| `a_deep_and_long_path_still_resolves` | 12 层深 + 120 字符组件，stat 与 read 均通过 |
| `a_path_beyond_the_protocol_bound_is_refused_not_truncated` | 4097 字节 → `invalid_params`；**4096 字节不因长度被拒**（边界对照，防止上一条因错误原因通过） |

"目录替换"由既有集成测试 `a_swapped_intermediate_directory_cannot_redirect_a_read` 与 `a_swapped_root_requires_the_user_to_choose_again` 覆盖，本轮未重复。

**实施中修正**：我最初按 4096 字符写"超界"断言，实际 `MAX_PATH_BYTES` 正好是 4096，所以那是我**差一错误**，不是实现问题；已改为 4097 并补了边界对照用例。

### 支持说明

新增 `evidence/fs-guard-platform-support.md`：记明两端的目标三元组与构建结果、依赖的平台表、根锚定语义的两端对照、**两处 NT 细节**（`NtCreateFile` 不接受 DOS 路径；相对打开拒绝 `"."`）、路径形态支持表，以及**明确不支持**与**未验证**两节。其中"POSIX 运行级回归未做、Windows 10 22H2 未实机跑、安装包路径未验证"三项单独列出，不得读作已通过。

**验证结果**：`cargo test --release` → 单元 **45** + 集成 **27** 全过；`cargo check --target x86_64-unknown-linux-gnu` → 0 警告；`openspec validate --strict` 通过。**进度 4/37**。

## 实施：任务2.3完成（project-fs-guard 有限写入入口）

### 两个 entry point，一份源码

`Cargo.toml` 声明两个 `[[bin]]`，**都指向 `src/main.rs`**，写入能力由一个 feature 控制：

```
fs-guard(.exe)         默认（write-ops 关闭）→ src/write.rs 不参与编译
project-fs-guard(.exe) required-features = ["write-ops"]
```

这样"只读 helper 不能写"是**代码不存在**，而不是某个检查说不；同时两个入口的帧处理、取消标志、根句柄与逐组件路径检查是同一份代码，不可能漂移。

**实测两个 exe 大小完全相同（1,106,432 字节）**：链接器把只读版未用到的写入函数全部剥离，所以共享源码在只读 binary 里是零成本。

四种组合均**零代码警告**（仅剩 Cargo 对同文件双 target 的固有提示，那正是刻意为之）：`win-read`、`win-write`、`unix-read`、`unix-write`。

### 三个操作与写前阻断

| 操作 | 语义 |
|---|---|
| `write` | `create=true` 要求目标不存在且不需要版本；其余一律视为覆盖，**必须**携带 `expected_version` |
| `edit` | 字面量替换，`expected_version` 必需；多个匹配而 `replace_all` 未给时**拒绝**（不能替调用方猜改哪一个） |
| `mkdir` | 内部步骤，只创建一级，拒绝已存在；只接受已授权根内的路径 |

`expected_version` 的构造绑定 **grant id + 相对路径 + 对象身份 + size + mtime**（FNV-1a 64）。设计 D3 要求"绑定项目/相对路径、对象身份及内容版本，不仅用mtime/size"，所以：

- 用 **grant id** 而不是 grant 的递增 `version` 计数器——后者在 helper 重启后从 1 重来，一个 grant 下铸造的值可能匹配另一个；
- 只读入口**不产出**这个值（`read` 的响应里尚未加 version 字段，属 3.4 的接入面）；
- 版本不符时**在暂存之前**返回 `file_changed`，目标文件一个字节都不动。

### 发布（publish）用 `ReplaceFileW`

设计 D3 要求"避免向读者暴露半写文件""原有权限不得被静默扩大"。做法是：同目录 `CREATE_NEW` 建暂存 → 写入并 `sync_all` → **`ReplaceFileW`** 替换 → 重读版本。选 `ReplaceFileW` 而不是 `std::fs::rename`，是因为它**保留被替换文件的安全描述符、属性、对象身份与创建时间**；`rename` 会把*暂存文件*的描述符挂到名字上，使一个 ACL 比目录更窄的文件静默获得目录权限，而且目标被打开时会直接失败——而目标正被打开恰恰是这里的常态（调用方刚读过它）。

### 验证结果（实际执行）

| 命令 | 结果 |
|---|---|
| `cargo test --release`（只读版） | 单元 **45** + 集成 **27** 全过 |
| `cargo test --release --features write-ops`（写入版） | 单元 **61** + 集成 **61** + 集成 **27** 全过 |
| 四个组合的 build/check | 0 代码警告 |

写入版新增的 16 项单测覆盖：版本不可从字节复现（同内容不同路径版本不同）、伪造版本拒绝且不写、create 遇到已存在拒绝、无版本覆盖拒绝、**过期版本拒绝且用户新内容存活**、匹配版本发布成功且无暂存残留、父目录不存在拒绝、mkdir 拒绝二次创建与逃逸、edit 的版本前置/歧义拒绝/文本不存在、超界内容拒绝、敏感目标拒绝、**父目录被替换为 reparse point 时写入拒绝且根外无文件**。集成层新增 34 项（写入版）含真实 stdio 往返。

### 实施中修正的缺陷（全部靠实测发现）

1. **`NtSetInformationFile(FileRenameInformation)` 方向是反的**。该调用重命名**句柄所指的对象**，所以我把目标文件改名成了暂存文件名，目标名直接消失。实测症状极具误导性：发布"成功"（`result -> None`），随后重读目标报"系统找不到指定的文件"。改用 `ReplaceFileW` 后一次通过。这条记录在代码注释里，避免以后再犯。
2. **按句柄发布需要目标句柄带 `DELETE` 权限**，而读路径只申请了 `FILE_READ_DATA|FILE_READ_ATTRIBUTES|SYNCHRONIZE`，于是得到 `STATUS_ACCESS_DENIED`。新增 `Access::{Read,Replace}` 访问意图，**只有最后一个组件**按需放大，中间目录仍只读打开；`Access::Replace` 用 `#[cfg(feature="write-ops")]` 而非 `#[allow]`，使只读版在类型层面无法表达这个请求。
3. **空父组件列表**：`"a.txt"` 的父列表为空，而 `open_relative` 正确拒绝空列表。新增 `open_parent`：父为空时按 `reopen_directory` 打开根（含身份复核），其余走 `open_relative`。
4. **`version_of_path` 与 `version_for` 口径不一致**：发布后返回的版本与调用方比对的口径不同。统一为 `version_for(grant, relpath, stat)`。
5. **一次 PowerShell 批量替换把中文注释写成乱码**。`Get-Content -Raw | Set-Content` 按 GBK 误读 UTF-8 造成；已用 .NET UTF-8 API 修回，并逐文件核验（无 BOM、无 U+FFFD）确认只有 1 行受影响。此后所有脚本替换改用 `[System.IO.File]::ReadAllText/WriteAllText` + 显式 `UTF8Encoding($false)`，且用绝对路径（`[System.IO.File]` 用的是进程工作目录，不是 `cd` 后的 PowerShell 位置）。

### 未完成（不属2.3）

- **暂存对象注册表与跨请求清理**：当前暂存文件在**单次请求失败路径**上被删除，但没有"本任务已记录暂存对象"的注册表，也不跨请求清理。这属**任务2.4**。
- **`read` 尚未返回 version 字段**：调用方目前要自己按同一口径构造，或依赖 `write`/`edit` 的返回。把 version 接入 `read` 响应属 3.4 的接入面。
- **POSIX 侧仍是编译级验证**：`cargo check --target x86_64-unknown-linux-gnu`（两种 feature 组合）通过，但无 POSIX 主机做运行级回归。
- 集成层尚未覆盖"恶意帧/未知 op 在写入入口"的完整矩阵；单测已覆盖超界与未知参数，完整帧矩阵待 2.4 与验收阶段补。

## 实施：任务2.4完成（临时对象注册表与错误/恢复说明）

### 新增模块 `src/temp.rs`

设计 D3 的硬约束是："用户原文件禁止作为清理目标，清理只根据本任务已记录的暂存对象身份执行，**绝不通过 dirname 向上推算根目录**。" 之前的实现是 `remove_file(拼出来的路径)`，正是这句话禁止的形状。改成注册表后有三条性质使约束**成立而非被声明**：

1. **只有被登记的对象可删。** 注册表外没有任何函数接受外部路径；可删的唯一途径是被 `track` 登记，唯一调用者是暂存步骤。
2. **删除通过持有的目录句柄。** 新增后端原语 `remove_relative(parent, name)`：unix 用 `unlinkat(parent_fd, name)`，Windows 用相对打开 + `NtSetInformationFile(FileDispositionInformation)` **按句柄**标记删除。名字由内核在**创建暂存文件时那个目录对象**内解析，事后目录改名或替换都不能把删除引到别处。
3. **删除前复核对象身份。** 为此给 `EntryStat` 增加 `identity` 字段（unix `st_ino`，Windows 文件索引）。名字已不指向当初创建的对象时**拒绝删除**并上报残留。

### 顺手抓到的两个真 bug（都在我自己刚写的代码里）

1. **`discard` 删除了对象却没有把条目移出列表**，于是 `len()` 仍为 1。测试 `a_tracked_object_is_removed` 直接抓到。
2. **`discard_all` 因此永不结束**——它写成 `while !tracked.is_empty()`，而保留未成功条目正是设计意图（残留要可上报）。这就是全量测试"挂起"的真正原因：不是死锁，是死循环。改为**每次调用对每个条目只尝试一次**（`(0..len).rev()`），并在注释里记下这段历史。

第二个 bug 值得单独说：它只在**多线程跑全部测试**时表现为挂起，单跑同一测试是断言失败——如果只单跑排错，会得出"这是个断言问题"的错误结论。

### 错误/恢复说明

新增 `evidence/fs-guard-error-recovery.md`：12 个错误码各自的"何时返回 / **磁盘状态**（实测） / 调用方可安全执行什么"。核心区分是 `file_changed` 是**冲突**而非参数错误，保证"一个字节都没写"，恢复动作是"重新读取"而不是"重试写入"；而 `io_error`/`internal` 是**不确定结果**，此时 helper 刻意**不删除暂存文件**（暂存内容可能是新内容的唯一副本），调用方不得重放。文件同时写明四项**未覆盖**（暂存残留无结构化上报、`read` 不返回 version、写入路径的 `cancelled` 未实测、`io_error` 不确定分支未做故障注入），不得读作已支持。

### 根外哨兵回归

新增 `tests/write.rs`（仅在 `write-ops` 下编译），5 项进程级探针：

| 探针 | 内容 |
|---|---|
| `no_write_path_touches_a_file_outside_the_root` | 根旁哨兵 → 7 条拒绝路径 + 3 次**真实**写入（create→overwrite→edit）→ 哨兵**字节/大小/mtime 全不变** |
| `a_successful_write_leaves_no_staging_object_behind` | 嵌套目标（走父目录句柄）写两次，根内无 `.fsguard-tmp-*` 残留 |
| `the_write_binary_refuses_a_second_root_and_unlisted_operations` | 写入入口同样拒绝 `unlink`/`rename`/`chmod`/`shell`/`spawn`/`exec`，且拒绝携带 `root_path`/`cwd`/`module`/`argv_path`/`absolute_path` 的写入 |
| `an_edit_that_loses_the_race_changes_nothing` | 用户手改后，过期 `edit` 与过期 `write` 都返回 `file_changed`，用户新内容存活，无暂存残留 |
| `the_conflict_code_is_stable_and_distinct` | `file_changed` 与 `invalid_params`、`no_such_grant` 三者可区分 |

### 验证结果（实际执行）

| 命令 | 结果 |
|---|---|
| `cargo test --release`（只读版） | 单元 **45** + 集成 **27** 全过 |
| `cargo test --release --features write-ops`（写入版） | 单元 **69** + 集成 **69** + 集成 **27** + `write` **5** = **170** 全过 |
| 四组合 build/check（win/unix × read/write） | **0 代码警告** |

### 实施中修正的其它问题

- **测试帧协议写错了**：我最初按"换行分隔 JSON"发帧，而协议是 **4 字节大端长度前缀**。子进程把 `{` 当长度高位、继续等剩余 3 字节，表现为"进程还活着但 stdout EOF"。改用与 `tests/stdio.rs` 相同的帧格式，并在注释里写明为何必须一致。
- **`tests/write.rs` 在只读版无法编译**（`CARGO_BIN_EXE_project-fs-guard` 不存在）。与 bin 同样处理：`autotests = false` + 显式声明 `[[test]] stdio` 与 `[[test]] write`（后者 `required-features = ["write-ops"]`）。
- **只读版的死代码**：`EntryStat.identity`、`remove_relative`、`NtSetInformationFile` 都只在写入版有意义，全部按 `#[cfg(feature = "write-ops")]` 门控（而不是 `#[allow]`），使只读版在类型层面无法表达这些能力。

**进度 6/37**。B 批（1.1–2.4）中的 2.1–2.4 已完成；1.1–1.3 为 Win10/Win11 真机句柄探针与 W01–W16 证据索引，需要目标 OS 实机，届时按批推进。

## 实施：任务3.3进行中（helper 解析接线；任务**未**完成）

3.3 是"网关/队列/broker 接入有限 helper 派发并复用业务运行、逐次身份/grant/operations 及动作校验"。本轮完成了其中**可实测且不依赖真机**的第一块：把 desktop 主进程的 helper **解析**从"一个只读 binary、且无平台后缀"改为"两个入口、按角色解析"。

### 缺口来源（设计文档已点名）

`design.md` 的影响面表里写着：

> `desktop/src/main/local-files/{...fs-guard-binary}.ts` … 开发态构建候选未带.exe，打包候选为resources/fs-guard且先做accessSync检查，**不能假设自动补后缀**，须统一产物名后真包验证

而 `release.yml`/`electron-builder{,.win}.js` 一向标注"当前仅mac分支构建/注入fs-guard；Windows缺helper构建及extraResources配置"。检查证实：`electron-builder.js`（mac）注入 `{from: <target/release/fs-guard>, to: 'fs-guard'}`，`electron-builder.win.js` **完全没有 helper 条目**（属任务 5.1）。

### 本轮改动

`desktop/src/main/local-files/fs-guard-binary.ts`：

| 之前 | 现在 |
|---|---|
| 只解析 `fs-guard` 一个 binary | `role: 'read' \| 'write'`，后者解析 `project-fs-guard` |
| 候选名一律不带后缀 | `guardExecutableName(role, platform)`：win32 加 `.exe` |
| 单一 `COW_FS_GUARD` 覆盖 | 写入口另有 `COW_PROJECT_FS_GUARD`，**只覆盖自己的角色** |
| 打包候选只有裸名 | 裸名（`extraResources` 会重命名）+ 构建名，两者都尝试 |
| 无法在非 Windows 上测 Windows 行为 | `platform` 可注入 |

**为什么按角色而不是加参数**：D1 让只读入口在**编译期**就没有写操作，所以角色必须体现在**选哪个二进制**上，不能是一个可以被打开的开关。解析写入口是一个**显式动作**，因此读路径不可能"解析着解析着"获得写能力。

**打包候选为何两个名字都留**：`extraResources` 的 `to: 'fs-guard'` 会把产物重命名，所以 mac 包内是 `Resources/fs-guard`；而 Windows 包内会是 `Resources/fs-guard.exe`。同一条目同时覆盖"已重命名"与"保留构建名"两种布局，避免再猜一次。

### 验证（实际执行）

新增 `tests/test_desktop_guard_binary_roles.cjs`（`node --test`，与仓库既有 JS 测试同一套基建），**9/9 通过**：

| 用例 | 钉住的属性 |
|---|---|
| 角色名与平台后缀 | 四个组合（read/write × win32/posix）的名字，且两角色**永不重名** |
| 角色不互相满足 | 读角色的候选名集合与写角色的**无交集**（防"读路径拿到写 binary"） |
| dev 候选全带后缀 | win32 全 `.exe`，posix 全不带 |
| 打包位置两种名字都在 | 重命名与保留构建名都可解析 |
| 覆盖只影响自己角色 | 两个 override 互不串门 |
| 无 helper 时返回 `null` | "这个构建没有 helper"是事实，不是猜 |
| 按顺序取第一个存在者 | 第 3 个候选存在就取第 3 个 |
| **真实检出可被真实候选列表找到** | 用本仓库 `desktop` 目录构造候选，必须找到 cargo 刚构建的 `fs-guard.exe` |
| 写角色不被只读 binary 满足 | 只读 binary 存在时，写角色仍返回 `null` |

**测试不是空转——已做反向验证**：临时把 `guardExecutableName` 改回"不加后缀"的旧行为，**4 项立刻失败**，其中最关键的一条正是

```
the dev candidate list must include D:\AIPJ\rsmagent\desktop\native\fs-guard\target\release\fs-guard.exe
```

也就是说，这个测试确实能抓住那个真实缺陷。恢复修复后重新 9/9 通过。

**回归**：`npx tsc --noEmit -p tsconfig.main.json` → 0 错误（调用方 `remote/remote-container-ipc.ts` 的现有 `resolveGuardBinary({resourcesPath, appPath})` 调用仍然成立，因为 `role` 缺省为 `'read'`）；`tests/test_desktop_local_read.cjs` → **24/24 通过**（该测试会编译并**真跑** Rust helper 的 stdio 协议，因此这次 Rust 重构没有破坏既有读取链路）。

### 3.3 **尚未完成**的部分（故未勾选）

- **写入口的派生尚未接线**：`remote-container-ipc.ts` 目前仍只用读角色。把 `role: 'write'` 接到项目执行通道，并且**只在** 3.4/6.x 的启用条件满足时才可达，是下一步。
- **broker 的 `operations` 逐次校验未端到端验证**：`_check_operations` 已实现（授权集合＝grant 集合，命令快照超出即拒，声明集合必须等于快照），但"逐次身份/grant/operations"还缺集成层测试。
- **`runtime` 落库路径未完成**：v3 digest 已含 `runtime`，但 `commands.py` 的 `claimed_runtime` 仍被显式丢弃（`del claimed_runtime  # never trusted`），尚未定义"由谁、依据什么"写入 `runtime` 列。
- **3.3 点名的四类测试未写**：撤权、跨租户/设备、超时、审计/配额失败。
- 本机 desktop 执行测试基线**不是全绿**（`tests/test_desktop_execution_broker.py` 42 项在此 Windows 主机失败，已在 `harden-windows-desktop-reliability/evidence/capability-declaration-and-gates.md:18` 记为平台 N/A），因此 3.3 的 Python 侧验证需要以"与基线对比"的方式给出，不能只报绝对通过数。

### 已确认的两项决策（用户答复，2026-02）

1. **写入口暂不接入生产路径。** helper 解析已支持 `role: 'write'`，但 `remote-container-ipc.ts` 继续只用读角色。写能力面向用户的接通留给任务 4.2/4.3，理由是 W02–W08 真机验收（5.2）与审计/配额证据尚未完成，提前接线会让桌面端先于验收获得本地项目写权限。**这不影响 3.3 的 broker 侧工作。**
2. **`runtime` 由服务端记录，不读调用方字段。** 与 `commands.py` 现有的 `del claimed_runtime  # never trusted` 一致。

### 待澄清：`runtime` 的**取值**有两个互不兼容的解读（故 3.3 未继续实现该列）

核实合同后，`runtime_verified` 这个检查的语义指向一处矛盾，需要确认后才能写这一列：

| 解读 | 依据 | 取值示例 |
|---|---|---|
| 甲：**脚本运行时语言** | `refusal_codes.runtime_not_verified = "runtime_unavailable"` 挂在 `run_script` 的 precondition 下；原文 "`language` SHALL be one of `runtime_languages`, that language SHALL be verified on this platform"；能力块的 `runtime` 字段与 `accepted_runtimes` 都是语言清单 | `"python"` / `"powershell"`；文件工具为空 |
| 乙：**分派来源** | 用户答复的文字是"按 origin 判定并记录" | 字面量为来源标记（如 `"runtime"`） |

两者对 `desktop_commands.runtime` 的**取值**不同，而这列**参与 v3 摘要**（`digest.fields` 含 `runtime`）。因此选错会让服务端与客户端算出不同摘要、或让一个检查永远读不到它该读的东西。

**这处矛盾不宜由我自行消解**，理由：乙是用户答复的字面表述，甲是合同文本与拒绝码的指向，两者都有依据。

**已答复（用户确认）**：取 **甲——脚本运行时语言**，由服务端从**已校验**的 `run_script` 参数取得，不读设备或调用方声明字段。

**不受此阻塞、可继续的部分**：`operations` 的逐次校验（`_check_operations` 已实现但缺集成层测试）与 3.3 点名的四类测试（撤权、跨租户/设备、超时、审计/配额失败）。

## 根因定位：broker 基线 42 项失败的成因（本轮）

用户指定本轮优先 3.3 不受阻部分。按此先复测基线，随后定位到那 42 项失败的真实成因。

### 基线确认（这次做了严谨对照）

先跑当前树：`tests/test_desktop_execution_broker` → **47 项中 42 项失败**。由于我改过 `execution_capability.py`（新增平台/运行时设置），**这有可能是我的回归**，所以不能直接引用旧记录。执行 `git stash push -u` 取干净树复跑：**同样是 47 项中 42 项失败**。`git stash pop` 恢复。结论：这 42 项**确实先于本次改动存在**，不是回归。

### 但失败**并非**"平台 N/A"这么简单——根因是版本意识缺口

旧证据把 42 项记为"需 POSIX 主机"。逐条看实际断言文本后，这个归因**不完整**：

```
test_prepare_refuses_a_platform_with_no_accepted_launcher
  期望: refusal(status=422, code="unsupported_platform")
  实际: 503 {'code': 'feature_unavailable',
             'message': 'project execution is not available (platform_unsupported)'}
```

顺着 `integrations/desktop/execution_broker.py:298` 的 `_check_platform` 读：它先查 `platform_supported`（应 422），再查 `tool_supported_on`（503）。得到 503 说明**第一步通过了**，即某处把 win32 当成了受支持平台。而 `auth/desktop_contracts_v2.py`（**未被本次改动触及**，`git status` 可证）对 win32 正确返回 `False`。

真正的成因在 **`agent/desktop_remote/device.py` 里同一个判定存在两个版本**：

| 函数 | 位置 | 平台判定 |
|---|---|---|
| `refusal_for` | `device.py:260-283` | 读 `lease_row["platform"]` → `CONTRACT_PLATFORMS` 映射，**能区分 win32**，并按声明 major 选 v2/v3 |
| `unavailability_code` | `device.py:285-302` | 仍硬用 `v2.tool_supported_on(tool_name, state.platform)`，**不感知 major** |

而 `unavailability_code` 的文档字符串写着：

> One mapping, read by both the gate-time plan and the pre-send re-check, so a refusal cannot be reported under one code when it is decided and another when it is delivered.

即它**承诺与 `refusal_for` 同口径**，实际已不同口径。且它不是死代码：`agent/desktop_remote/dispatch.py:289` 与 `:685` 都调用它，调用点的前置条件恰恰是"`refusal_for` 刚报过一次拒绝"（`dispatch.py:277-289`、`683-685`）。于是当 `refusal_for` 因平台而拒（win32 在 v3 合同下 `supported=true`，而 v2 表里 `supported=false`）时，`unavailability_code` 用 **v2** 的表把同一件事报成了 `feature_unavailable`。

**这属于 3.3/3.5 范围的版本意识缺口，不是"缺一台 POSIX 主机"。** 修它需要：让 `unavailability_code` 与 `refusal_for` 读同一份已解析的平台与 major 结论（而不是各自查表），并为"两个函数对同一状态给出同一码"补一条测试。

**本轮未修**：该函数位于设备派发拒绝路径上，而本机 broker/dispatch 基线大面积失败（42 项），无法在本轮取得可信的回归信号。拿不到验证就改动一条拒绝路径，风险高于把它准确记录下来。下一轮应先修这个缺口，再让 42 项基线随之收敛。

### 顺带确认的两项

- `tests/test_desktop_execution_operations.py` → **29/29 通过**（operations 列解析、major 选择、授权三集合、迁移）。
- operations 的**单元**覆盖已较全（29 项），所以 3.3 该补的是 **HTTP 集成层**的"逐次身份/grant/operations"与四类拒绝测试；而这些测试需要 broker 端点在本机能跑通，正卡在上面的缺口上。**3.3 的剩余测试与这个缺口是同一件事**，先修缺口是正确顺序。

## 实施：修掉 `device.py` 的版本意识缺口（任务 3.3 的一部分）

### 改动

`agent/desktop_remote/device.py` 有同一个判定的两个版本，且都硬编码 v2 合同：

| 函数 | 之前 | 现在 |
|---|---|---|
| `refusal_for` | `v2.tool_supported_on(...)` | 经 `_classify`，用**该设备声明 major 对应的合同** |
| `unavailability_code` | 自己再写一遍 if 链，同样硬用 v2 | 从**同一个** `_classify` 结论派生 |

1. 新增 `contract_module_for(state)`：按 `state.protocol_major`（优先读块内声明的 major）选用 `desktop_contracts_v3` 或 `_v2`。**这与 `_execution_block` 校验该块时用的是同一个 major**，所以"校验用的合同"和"判定用的合同"不再可能不是同一个。
2. 新增 `_classify(state, tool)`：返回**一个**拒绝类别。`refusal_for` 把类别渲染成给用户看的话，`unavailability_code` 渲染成机器码。**两者是同一个判定，而不是两次独立判定**——这才是它文档字符串承诺的性质。
3. **对外错误码取值保持不变**（`platform_unsupported` / `runtime_unavailable` / `device_offline` / `protocol_incompatible` 的既有映射逐条保留），所以这是行为修复而非语义变更。

### 修好的那个用户可见缺陷

v3 合同声明 Windows 文件工具受支持（`platform_supported("win32") = True`），v2 声明 `win32.supported = false`。此前无论设备声明哪个 major，都拿 v2 的表去判：

- **v3 Windows 设备被误判为平台不支持**——合法设备被告知"这个平台没有可用启动器"。
- 且拒绝以 `feature_unavailable`（`unavailability_code`）交付，而门控以 `platform_unsupported`（`refusal_for`）判定——**同一件事两个码**，正是文档承诺不会发生的情况。

### 新增契约测试

`tests/test_desktop_remote_device_refusal_pair.py`，**12/12 通过**，按**成对**断言：

| 用例组 | 钉住的属性 |
|---|---|
| `ThePairAgreesTests` | 离线三态、v1 对端、平台受限工具、未声明工具、脚本面关闭——每一态下两函数必须同时给出（拒绝语, 已知码），且码属已知词汇 |
| `ContractSelectionTests` | v2 状态选 v2、v3 状态选 v3；**块内 major 不可解析时回退到 row 自身的 major** |
| `V3WindowsTests` | v3 Windows 设备的五个文件工具**不再被拒**；`bash` 仍被拒且码为 `platform_unsupported`；同一对在 v2 下仍整体拒绝 Windows（旧客户端答案不变） |
| `KnownCodeVocabularyTests` | 穷举 (major × platform × tools × scripts × online) 扫过，产出的每个码都在已知词汇内 |

**测试抓到我实现里的一个真缺陷**：`contract_module_for` 初版在块内 major 不可解析时保留了先前的值，`"not-a-number"` 仍走 v3；对应用例直接失败。已改为回退到 row 的 major。这也说明我最初写的该用例**本身不成立**（row 也是 3，测不出回退），已重写成两个方向都能区分的情形。

### 验证与基线对照（每项都做了干净树对照）

| 套件 | 当前树 | `git stash` 干净树 | 判定 |
|---|---|---|---|
| `test_desktop_remote_dispatch` | 92/92 通过 | — | 无回归 |
| `test_desktop_remote_device_refusal_pair`（新） | 12/12 通过 | — | 新覆盖 |
| operations + v3 合同 + capability_v3 + v2 合同 | **192/192 通过** | — | 无回归 |
| `test_desktop_remote_skill_delivery` | 21 项 / **16 失败** | 21 项 / **16 失败** | **先于改动存在** |
| `test_desktop_execution_broker` | 47 项 / **42 失败** | 47 项 / **42 失败** | **先于改动存在** |
| `test_desktop_remote_mode_semantics` | 模块级 ERROR | — | 缺 pytest，与改动无关 |

### 关键结论：42 项 broker 失败的闸门不在 `device.py`

修好后 **broker 仍是 47 项中 42 项失败**（整套耗时从 111s 降到 45s）。继续追到真正的闸门：

```
channel/web/fork/handlers/desktop.py:705-724  _execution_enabled()
    state = execution_capability.execution_state(        # ← v2 的读法
        settings=conf(), platform=_execution_platform(), runtime=_execution_runtime())
    if not state.get("available"): → 503 feature_unavailable
```

`_execution_platform()` 在本机返回 `win32`，而 **v2 状态**下 win32 不是 accepted 平台，于是**所有 broker 端点在本机被 503 拦死**。即 `device.py` 是"发给设备前"的闸门，而这个 handler 闸门在它之前就把请求挡掉了。两者都是 3.3/3.5 的接线点。

### 本轮**未**改 `_execution_enabled`（属需决策项，不是遗漏）

把它改为按 major 读取，会让 v3 设备的 broker 端点可达；但**本机没有任何"用户已确认平台 / 运行时已验收"的接受态**，所以改完仍会被"未接受"合理拦下——除非我同时配上接受态，而那会**在 W02–W08 真机验收之前让 Windows 文件写入变得可达**，直接违背用户已确认的决策（写入口暂不接线，验收后再开）。

因此这一步需要用户明确（下一轮按答复执行）：

1. **允许在真机验收前先让 v3 broker 端点在本机可达**（仅为让 3.3 集成测试可跑，并配好接受态）；或
2. **接受 3.3 的集成测试在验收前无法在本机执行**，改为以合成状态直接驱动 `service_for`，绕开 handler 闸门（我倾向此项：它不触碰可达性，且仍能覆盖 `operations`/撤权/跨租户/超时/审计配额的判定）。

## 实施：v3 命令入队链路（任务 3.3，本轮实际推进的那一块）

用户在上一轮未答复上述选项，故按我标注的**偏好第 2 项**推进，并在此明确记录该假设：**不触碰端口可达性**，先补全与可达性无关的实质缺口。

### 探路时发现的更要紧的事实

顺着"3.3 要求队列接入逐次 operations"彻查后，发现的问题比 `_check_operations` 缺测试严重得多：

| 事实 | 证据 |
|---|---|
| **全仓库没有任何地方把 `protocol_major` 写成 3** | `grep protocol_major=3` 只命中 `agent/desktop_remote/device.py` 的 `EXECUTION_MAJORS` 常量与合同常量，**没有一处入队** |
| **`operations_json` 在生产代码里只被读、从不被写** | 除 `auth/store.py` 建列与 `execution_broker.py` 读列外，全仓库无写入点 |
| **`runtime` 列同样从不被写** | 同上 |
| `create_execution_for_context` **没有 `operations`/`runtime` 形参** | `commands.py:527-537` |

即：**v3 命令根本无法入队**，而 `_check_operations` 对 v3 行要求"授权集合"非空且可读。所以 3.3 的真正内容不是"补测试"，而是**把 v3 入队链路接通**——没有它，v3 执行整体不可用。

### 本轮改动

| 文件 | 改动 |
|---|---|
| `integrations/desktop/commands.py` | `_enqueue_command` 新增 `operations`/`runtime` 形参并写入两列；新增 v3 前置校验（`protocol_major >= 3` 且 operations 为空 → `invalid_request`）；`create_execution_for_context` 新增 `operations`/`runtime`/`protocol_major` 形参 |
| 同上 | **按 major 选合同**：`execution_payload.contract_for_major(major)` 决定工具表、帧校验器、`TOOL_SCHEMA_VERSION` 与 digest 字段列表——与 `device.py` 用**同一个解析点**，所以两端不可能对同一命令用不同字段表算摘要 |
| `integrations/desktop/execution_payload.py` | `envelope_for_command` 新增 `operations`/`runtime` 两个字段 |

**两处刻意的保守设计**：

1. **v2 行为零变化**：`protocol_major` 缺省仍为 2，`operations`/`runtime` 只在 **v3 的字段列表**里被覆盖（`canonical_envelope` 按 `fields` 选取），所以每一个已存的 v2 digest 一个字符都不会变——这正是"v3 是新 major 而不是 v2 加字段"的含义。
2. **`operations` 用 NULL 而非 `[]`**：与既有的 `skill_resources` 同一规则——"没有记录授权集合"与"确认了一个空集合"必须可区分，因为前者是拒绝、后者是合法的空授权。broker 的 `_operations_from_column` 正是按这个区分读的。

### 验证（实际执行，均无回归）

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_operations` + v3 合同 + v2 合同 + capability_v3 | **192/192 通过** |
| `test_desktop_execution_commands` + payload + target_wire + types | **79/79 通过** |
| 合计 | **271 项通过** |

其中 v2 合同套件包含摘要不变性断言，所以"v2 digest 未被这次改动影响"是**实测**而非推断。

### 本轮**未**完成的部分（故 3.3 未勾选）

- **`device_execution_frame` 仍硬编码 v2**（`execution_payload.py:243` 的 `protocol_major: v2.PROTOCOL_MAJOR`）。行可以为 v3，但发给设备的帧仍自称 v2，且不带 `operations`/`runtime`。这与 `commands.py` 的入队侧形成新的一致性缺口，**必须一并改**，否则 v3 命令能入队却不能正确下发。
- **`create_execution`（原始调用方入口）未透传** `operations`/`runtime`/`protocol_major`，只有 `_for_context` 变体接受。
- **v3 入队的测试尚未补**：新路径（按 major 选合同、两列落库、空 operations 拒绝、digest 覆盖 operations/runtime）目前只有既有套件回归，**没有针对性用例**。
- `_execution_enabled` 的 42 项 broker 闸门问题仍在，等用户对上面两个选项的答复。

## 实施：v3 下发帧与命令投影（任务 3.3，续）

上节列出的四项里，前两项（帧硬编码 v2、帧不带 operations）本轮完成。

### `device_execution_frame` 按行的 major 选合同

`device_execution_frame` 此前硬编码 `protocol_major: v2.PROTOCOL_MAJOR`、用 `v2.validate_execute_frame`、且完全不携带 `operations`/`runtime`。这形成了一个新的一致性缺口：**行可以是 v3，但发给设备的帧仍自称 v2**——设备会拿与摘要不同的字段表去校验，而服务器却以为自己已经校验过。

现在按 `command["protocol_major"]` 选合同（与 `_execution_block`、`create_execution_for_context` 同一个 `contract_for_major` 解析点），v3 帧携带 `operations` 与 `runtime`；无法服务的 major 直接 `ValueError`，**不是**降级成 v2 发出。

### 只有一个 operations 解析器

`_public_command` 新增 `operations_json` 与 `runtime` 两个字段。`operations_json` **原样透出存储值**，由 `execution_broker._operations_from_column` 解析——`device_execution_frame` 也调用同一个函数，而不是自己再解码一次。

理由：那个函数的全部价值在于把**缺省 / 空集 / 不可解析**三态区分开（缺省是"从来没有 v3 授权"、空集是"确认了空授权"、不可解析是"我读不出来"且绝不能读成"没有可查的"）。若下发侧另写一个解码器，就可能与**授权这个帧**的解码器给出不同答案。所以宁可在同一包内做一次惰性导入，也不复制这段逻辑。

`runtime` 列则原样透出为字符串，与设备侧 `DeviceState.platform` 的读法一致。

### 新增帧测试（8 项，`test_desktop_execution_operations.py`）

| 用例 | 钉住的属性 |
|---|---|
| v3 行 → v3 帧且带 operations | major、schema 版本、operations 都对 |
| v2 行 → v2 帧且**不带** operations | 旧帧不多长一个字段（设备会以"未知字段"拒绝） |
| v3 行缺 operations → 拒发 | 迁移前的行必须**在服务器侧**拒绝，而不是发出让设备拒 |
| 旧行不因工具名升级为 v3 | `read` 在两种 major 下都是文件工具；按工具名推断 major 就是"静默升级授权" |
| 脚本帧携带已校验的 runtime | `run_script` + `runtime="python"` |
| win32 上的脚本帧被拒 | 合同在 win32 不提供脚本启动器，帧不得离开服务器 |
| 合同不认识的语言被拒 | `ruby` 被点名拒绝 |
| 无法服务的 major 被拒 | `protocol_major=9` 直接 `ValueError` |

**我又一次写错了测试前提**：最初写"posix 上 powershell 应被拒"，实测 `runtime_supported_on("powershell","posix") = True`——两个语言在 posix 上都受支持，没有"不支持语言"的平台组合。已改为在 **win32**（脚本面关闭）上验证该约束。

### 验证（实际执行）

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_operations` | **37/37 通过**（含新增 8 项帧测试） |
| commands + payload + target_wire + types + operations + v2/v3 合同 + capability_v3 + remote_dispatch + refusal_pair | 383 项 / **1 失败** |

那 1 项失败经严格对照判定为**既有 flaky，且我的改动让它变好**：

| 运行 | 结果 |
|---|---|
| 当前树，单独跑该测试 | **OK（通过）** |
| **`git stash` 干净基线树**，跑整个 `BackgroundHandleTests` | **FAILED (failures=2)** |

即干净基线上失败**更多**（2 项 vs 1 项）。该套件的顺序/时序不稳定性在本次 change 开始前就已记录。上轮新增的 `test_desktop_remote_device_refusal_pair` 与 `remote_dispatch` 均全绿。

### 3.3 仍未完成（故未勾选）

- **`create_execution`（原始调用方入口）未透传** `operations`/`runtime`/`protocol_major`；目前只有 `_for_context` 变体接受。经原始入口的 v3 入队仍不可达。
- **v3 入队的落库测试未补**：`_enqueue_command` 写两列的路径、以及"v3 且 operations 为空 → `invalid_request`"的前置校验，目前只有既有套件回归，没有针对性用例。
- **`_execution_enabled` 的 42 项 broker 闸门**仍在，等用户对两个选项的答复（我按偏好第 2 项推进，未触碰可达性）。
- 3.3 点名的四类测试（撤权、跨租户/设备、超时、审计/配额失败）尚未补。

## 实施：入队入口透传与两列落库测试（任务 3.3，续）

上节列出的前两项本轮完成。

### `create_execution` 显式命名三个 v3 形参

此前它靠 `**kwargs` 把参数转给 `_for_context`，虽然能用，但**签名上看不见** `operations`/`runtime`/`protocol_major`。一个在签名里看不见这三个字段的调用方，就是会忘记传它们的调用方——而"忘记传"的直接后果是 v3 命令以无授权集合入队，然后在派发时被拒，且没人找得到原因。改为显式形参，让 v3 路径成为这个入口**可见的契约**。

### 落库测试（`test_desktop_execution_operations.py`，现 46 项全过）

新增两个测试类：

| 类 | 覆盖 |
|---|---|
| `AV3CommandMustNameItsAuthorizationTests` | v3 无 operations → `invalid_request`；v3 空列表 → 同样拒绝；**v2 不被要求 operations**（守住校验的作用域，否则会打断所有既有调用方） |
| `OperationsPersistenceTests` | INSERT 语句确实列出两列；列数与绑定值数一致（32）；写入值经**唯一的读取器** round-trip；脚本命令记录 runtime；无 operations 时存 **NULL 而非 `[]`** |

### 这组测试我写错了三次，值得记下来

前两版都想造一个"完整的身份服务替身"来跑真实 INSERT，结果每次都卡在 `_enqueue_command` 碰到的下一个依赖上：

1. 第一版：事务守卫只实现了 `__enter__`/`__exit__`，但调用方在 `with` **之外**就把它当连接用（`con.execute(...)`）→ `no attribute 'execute'`；且未关闭连接导致 Windows 上临时库删不掉（`PermissionError`）。
2. 第二版：补齐守卫后 → `_Svc` 没有 `_audit`。
3. 第三版：补上 `_audit` 后 → `ctx.user` 没有 `username`。

到第三步可以看清：**为一个 INSERT 去桩掉整个服务，写出来的测试是在描述这个桩，而不是描述被改动的代码**——而且它每轮都会因为下一个无关依赖再失败一次。所以改为直接断言**写入器本身**（打印出的 SQL 与其绑定元组），断言落在真正变化的东西上。这条弯路本身是该记的证据：它说明为什么这个文件的测试形态与其它套件不同。

### 验证（实际执行）

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_operations` | **46/46 通过** |
| commands + payload + target_wire + types + v2/v3 合同 + capability_v3 + refusal_pair | **254/254 通过** |

### 3.3 仍未完成（故未勾选）

- **`_execution_enabled` 的 42 项 broker 闸门**仍在（`channel/web/fork/handlers/desktop.py:718` 用 v2 读法，win32 在本机被 503 拦死）。等用户对两个选项的答复；我按偏好第 2 项推进，未触碰可达性。
- **3.3 点名的四类测试**（撤权、跨租户/设备、超时、审计/配额失败）尚未补。
- **broker 的 `runtime_verified` 检查**尚未实现（合同 `broker.checks` 列出它，但 broker 里没有该检查；`runtime` 列的语义已按用户确认取"脚本运行时语言"）。
## 实施：broker 的 `runtime_verified` 检查（任务 3.3，续）

合同 `broker.checks` 列出 `runtime_verified`，但 broker 里此前**没有这个检查**；`runtime` 列的语义已由用户确认为**脚本运行时语言**（甲）。本轮把它补上。

### 判定规则

新增 `BrokerService._check_runtime(command, declared, platform)`，接在 `_check_operations` 之后（**故意最窄**：它只能拒绝一种语言，永远不能授予工具；排在 operations 之后，使"这条命令根本没有脚本面授权"仍报那个更可行动的答案）。

| 情形 | 结果 |
|---|---|
| 文件工具且无 runtime | 放行（文件工具不承载语言声明） |
| 文件工具但命令上有 runtime 值 | 同样放行——它覆盖摘要，但对文件工具**不是**语言声明，不得当语言判 |
| 脚本工具且行上无 runtime | 拒绝 `runtime_not_verified`（"哪种语言"正是这里不能凭空编的东西） |
| 行上有 runtime，且平台**已配置**但该语言不在已验证清单 | 拒绝 `runtime_not_verified` |
| 行上有 runtime，平台**完全未配置** | 放行——见下 |
| 帧声明的 runtime 与行上的**不同** | 拒绝 `command_conflict`（摘要覆盖 `runtime`，声明不同也意味着设备将执行摘要未描述的东西） |

**那个"完全未配置"的分支是本轮最容易做错的地方。** `accepted_runtimes` 的文档明确区分两种情况：平台**根本没配置过**（什么都没记录，posix 保留其能力切片已接受的脚本面）与**配置为空列表**（`runtime_recorded` 为真，操作者写下"这里没有已验证语言"，关面）。如果把两者都当作"没有已验证语言"，就会**静默关掉每一个从未写过该设置的部署的脚本面**，包括 POSIX。所以代码显式保留了这个区分。

### 新增测试（9 项，`test_desktop_execution_operations.py`，现 **55/55 通过**）

文件工具放行、文件工具不被杂散 runtime 值判为语言、脚本工具缺语言被拒（`runtime_not_verified`）、已配置但语言未验证被拒、已配置且语言已验证被接受、**未配置时保留能力切片答案**、帧替换语言 → `command_conflict`、帧重述同一语言不算冲突、v2 行的脚本命令同样按脚本工具被拒。

### 实施中修正的一处

初版用了 `SCRIPT_TOOLS` 这个模块级常量，但 broker 里并没有它——合同把脚本工具放在 `TOOLS["script_tools"]`。已改为从 `self._contract(command)` 解析出的合同读该表，与 `_check_operations` 用同一个合同实例。

### 验证（实际执行）

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_operations` | **55/55 通过** |
| commands + payload + target_wire + types + v2/v3 合同 + capability_v3 + refusal_pair | **254/254 通过** |

### 3.3 仍未完成（故未勾选）

- **`_execution_enabled` 的 42 项 broker 闸门**仍在（`channel/web/fork/handlers/desktop.py:718` 用 v2 读法，win32 在本机被 503 拦死），等用户对两个选项的答复；我按偏好第 2 项推进，未触碰可达性。
- **3.3 点名的四类测试**（撤权、跨租户/设备、超时、审计/配额失败）尚未补。这些同样需要 broker 端点或服务层可得，前者受阻于同一个闸门。
## 实施：四类拒绝的服务层覆盖（任务 3.3，续）

用户未答复上一轮的两个选项，故按我标注的偏好**第 2 项**执行（不触碰端口可达性），并在此记录该假设。

### 为什么改为服务层

`channel/web/fork/handlers/desktop.py:718` 的 `_execution_enabled` 用 **v2** 的 `execution_state()` 门控 broker 的每条路由，而本机平台是 `win32`——v2 状态下它不是 accepted 平台，于是**每条路由在 broker 被调用之前就返回 503**。这是刻意的部署闸门（Windows 文件执行在自己的验收证据出来前保持关闭），不是要绕开的 bug。

后果是：**HTTP 层的四类拒绝覆盖在闸门打开前不可得**，而这些判定本身都在 `BrokerService` 里、可直接到达。新增 `tests/test_desktop_execution_broker_service.py`，继承既有 `BrokerFixture`（复用真实设备/绑定/grant/租约夹具，而不是再造一份可能出错的），直接调用 `broker.service_for(...).prepare(...)`。

**这不是降低标准**：会调用服务的测试仍能抓住"某个检查被删掉"；而根本跑不起来的测试什么也抓不住。

### 覆盖（6 项，全部通过）

| 用例 | 覆盖 |
|---|---|
| 服务层越过了路由闸门 | 拒绝码**不是** `feature_unavailable`——证明测的是检查而非闸门 |
| 脚本命令无可信 runtime 被拒 | `runtime_verified` 在**真实命令行**上生效（不只是合成 dict） |
| 撤销 workspace grant | 拒 `grant_revoked` |
| body 指向另一台设备 | 拒 `resource_not_found`，且**行状态仍为 `queued`**（拒绝不得消耗命令） |
| 设备无活动租约 | 被拒，行状态不变 |
| runtime 检查排在 epoch 检查之前 | 见下 |

### 两条从失败中学到的事实

1. **撤销后的错误码是 `grant_revoked`**，不是我原先猜的 `stale_context`/`permission_resources` 之一。服务用的码更准确——用户撤销了这个项目，客户端可以直说，而不必让用户重选工作区。测试已按真实码断言。

2. **`_check_runtime` 排在 `_live_epoch` 之前**。我最初写"过期 epoch 应被拒为 `stale_context`"，实际得到 `runtime_not_verified`。原因是 `_open` 的顺序是 `… → _check_operations → _check_runtime → _live_epoch`。于是**尚未写入 runtime 的 v3 脚本命令**（即当前全部 v3 脚本命令）会先撞上 runtime 检查，epoch 拒绝经由这个接缝**不可达**。

   两者都是真实检查，所以我把它**钉成一条显式的顺序断言**并写进注释：将来若有人调整顺序，那应当是一次刻意的改动；同时避免后来者把这里的 `runtime_not_verified` 误读为"epoch 检查不见了"。

### 验证（实际执行）

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_broker_service`（新） | **6/6 通过** |
| operations + v2/v3 合同 + capability_v3 + refusal_pair | **230/230 通过** |

### 3.3 仍未完成（故未勾选）

- **`_execution_enabled` 的 42 项 broker 闸门**仍按用户偏好第 2 项处理（不触碰可达性）。**若要勾选 3.3，需要用户明确**：是接受"HTTP 层四类覆盖待闸门开启"，还是解开闸门后在端点层补齐。
- **超时类**与**审计/配额失败类**尚未覆盖：本轮覆盖的是撤权、跨租户/设备、设备离线、runtime 与顺序。超时需要 `_check_run_window` 的时钟控制，审计/配额需要注入失败——两者都还没写。
## 实施：修正 `_check_runtime` 的位置（任务 3.3，本轮）

### 上一轮那个"顺序后果"其实是**位置放错**

上一轮我把 `_check_runtime` 放在 `_open` 里，于是它排在 `_check_run_window` 与 `_live_epoch` 之前（后两者都在 `start` 中）。后果不只是"某个码先出现"：在**尚未写入 runtime 的 v3 脚本命令**（即当前全部 v3 脚本命令）上，调用方**完全看不到**过期截止或 epoch 拒绝——`runtime_not_verified` 一律抢先。

更糟的是它在 `prepare` 上也生效：`prepare` 回答的是"我还能运行这个吗"，它应当报告命令的状态、摘要与截止时间；一个 `prepare` 因 runtime 未记录而失败，会让设备**连自己该校验什么都不知道**。

**修正**：把 `_check_runtime` 从 `_open` 移到 `start`，与 `_check_tool_eligibility` / `_check_approval` / `_check_quota` / `_check_run_window` 并列，并排在 `_live_epoch` 之前。理由写在代码注释里：`_open` 回答身份与授权，这个检查回答"那个具体的东西能不能跑"，属于同一类资格检查。**能遮住更基本拒绝的检查，是放错位置的检查。**

### 修正后的可观测性

| 调用 | 修正前 | 修正后 |
|---|---|---|
| `prepare`（v3 脚本，无 runtime） | 拒 `runtime_not_verified` | **正常返回**命令状态与 `expires_in_seconds` |
| `start`（过期 epoch） | 拒 `runtime_not_verified`（epoch 不可见） | 拒 `stale_context`（epoch 可见） |
| `start`（v3 脚本，无 runtime） | 拒 `runtime_not_verified` | 拒 `runtime_not_verified`（不变） |

### 测试里踩到的性能陷阱（值得记下来）

我最初把 epoch/deadline 两类改成**调用 `start`**，结果 8 项测试跑了 **1716 秒（28 分钟）**：`start` 一旦走到 `issue_start_permit`，在这个 harness 里要等租约/存活机制，耗时以分钟计。

改为**直接调用被判定的那个方法**（`_check_run_window`、`_live_epoch`），并在 docstring 里写明为何不绕经 `start`。现在 **8 项 9.5 秒**。

这不是降低标准：被测的判定就是那两个方法本身，`start` 只是它们的调用者；而一个 28 分钟的套件实际上等于没有覆盖，因为没人会跑它。

### 验证（实际执行）

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_broker_service` | **8/8 通过，9.5 秒** |
| operations + v2/v3 合同 + capability_v3 + refusal_pair | **230/230 通过** |

### 3.3 仍未完成（故未勾选）

- **审计/配额失败类**尚未覆盖（`_check_quota` 需要注入配额耗尽的失败，`_check_approval` 同）。这是 3.3 点名四类里剩的最后一项，下一轮可补。
- **`_execution_enabled` 的 42 项 broker 闸门**仍按用户偏好第 2 项处理，未触碰可达性。**若要勾选 3.3 需要用户明确**：接受"HTTP 层覆盖待闸门开启"，还是解开闸门后在端点层补齐。
## 实施：配额重读（任务 3.3 点名四类的最后一项之一）

3.3 要求覆盖"审计/配额失败"。本轮补上**配额**这一半：新增 `QuotaRecheckTests`（3 项）。

### 为什么是"重读"而不是"检查一次"

`_check_quota` 的文档写着 "Re-check the tool-call quota without consuming it"，而 `prepare` 与 `start` **都**会调用它。两次调用的意义在于：入队时充足的配额，到真正调用时可能已被耗尽——所以答案必须来自**当前状态**，绝不能来自入队时的结论。这是任务 3.3 里"逐次身份/grant/operations 及动作校验"的同一原则。

同时它**只询问、不消耗**：在这里扣减会为一个可能永远不会启动的调用计费。

### 测试形态（3 项）

`_check_quota` 通过 `self._svc` 到达计量器，所以一个**只有两个方法的替身**就能驱动真实判定，不必搭起身份库。这与之前那次失败的"桩掉整个服务"有本质区别：那次断言的是桩，这次断言的是**判定**，而该判定只读一样东西。

| 用例 | 断言 |
|---|---|
| 配额耗尽 | 拒 `limit_exceeded` |
| 配额充足 | 返回 `{"metric": "tool_calls", "within_limit": True}`，且**只询问不消耗** |
| 以调用者身份询问 | `user_id`/`tenant_id` 是调用者自己的——问错人会让一个成员花掉另一个成员的额度 |

### 验证（实际执行）

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_operations` | **58/58 通过** |
| broker_service + commands + payload + v2/v3 合同 + capability_v3 + refusal_pair | **231/231 通过** |

### 3.3 仍未完成（故未勾选）

- **审计失败类**（`_check_approval` 的 `approval_required` 路径）未覆盖。它需要 `approvals` 表里的一行，属"要真库"的那一类；端点套件已有 3 项覆盖它（`test_prepare_refuses_a_declared_action_without_an_approval` 等），但那些在本机因闸门不可达。是否在本轮口径下补一条服务层版，取决于用户对闸门选项的决定。
- **`_execution_enabled` 的 42 项 broker 闸门**仍按用户偏好第 2 项处理。**若要勾选 3.3，需要用户明确**：接受"HTTP 层覆盖待闸门开启"，还是解开闸门后在端点层补齐。
## 决定性问题已测得：解开闸门后端点测试**基本可跑**（不再需要用户猜）

上一轮我说"3.3 是否可勾选取决于用户选 1 还是 2"。本轮不再让用户凭空判断，而是**直接测量**。

### 测量方法

`_execution_enabled`（`channel/web/fork/handlers/desktop.py:718`）是**路由层**的环境前提，问的是"本部署是否已接受该平台的启动器"。测试套件本来就在用同类手法：`BrokerFixture.setUp` 已经 `patch("auth.capability_matrix.slice_for", ...)`。所以我把 `execution_capability.execution_state` 一并 patch 成"可用"（**只在测试内**，不改任何生产代码），再跑整套。

### 结果

| 配置 | 结果 |
|---|---|
| 当前基线（未 patch） | 47 项 / **42 失败** |
| patch 闸门为"可用" | 47 项 / **16 失败**（40 秒） |

即 **26 项失败纯粹来自环境前提**，一旦把闸门按测试意图钉开就通过——包括我用来试探的 `test_prepare_refuses_a_platform_with_no_accepted_launcher`（期望 422 `unsupported_platform`）与 `test_a_supported_platform_still_refuses_a_tool_it_cannot_run`（期望 503 `feature_unavailable`），两项都 ok。

**这直接回答了上一轮的问题**：选项 1 不是空想——端点测试在这台机器上**确实可以跑**，不需要 Windows 真机，只需要在测试内钉开那一个环境前提。

### 剩余 16 项分两类，且**不是同一类问题**

1. **语义类（应当保持失败）**：`ExecutionGateTests.test_the_declaration_is_what_closes_it`、`RollbackDrillTests` 的 4 项等。它们**就是在测闸门本身**（声明关闭时应当拒、回滚后答案应当恢复），所以把闸门钉开反而**破坏了被测前提**。这类不该被 patch，需要按类分别处理——闸门开/关各自成立。
2. **实质类（需单独查明）**：permit 过期、heartbeat 需要 journal+permit、superseded epoch、terminal 行拒绝 start、status 的 unknown/running 语义、配额耗尽等约 11 项。它们与闸门无关，因此是**真实的失败**，或需要平台特定的夹具。

### 为什么本轮**没有**落地这个 patch

我把 patch 写进 `BrokerFixture.setUp` 试跑后**已回滚**（`git status` 对该文件无输出）。原因是：**部分修复混在套件里比不修更危险**——26 项会因此变绿，而另 16 项（含 11 项实质失败）仍然红着，却会被读成"闸门问题、已处理"。

正确的落地方式是分步的：先让不涉及闸门语义的类获得一个"闸门已接受"的基类，再单独处理 GateTests/RollbackDrillTests 的开闭两态，最后查明那 11 项实质失败。这是一次性的重构，不该在一轮里和别的工作混做。

### 对 3.3 勾选口径的影响

- **HTTP 层覆盖是可得的**，前提是按上面的方式重构夹具，而不是改产品行为，也不是等真机验收。这比上一轮我给出的两个选项都更好：**不需要在验收前让 Windows 写入变得可达**。
- 因此 3.3 的下一步是**测试夹具重构**，不是产品改动，也不依赖用户决策。我已撤回上一轮那个二选一的请求。
## 夹具重构的两次尝试与结论（任务 3.3，本轮）

上轮测得"patch 闸门后 42 → 16 失败"。本轮尝试把它落地，两次都不成立，结论是**这个改动比看起来大**，需要按类分别处理。两次都已回滚（`git status` 对 `tests/test_desktop_execution_broker.py` 无输出）。

### 尝试一：在 `BrokerFixture.setUp` 里 patch `execution_capability.execution_state`

结果：**47 项 / 16 失败**（26 项修复）。但 `RollbackDrillTests` 的 5 项因此**变红**——它们测的正是"关闭部署开关后应当拒"，而一个无条件返回 `available: True` 的 patch 把开关逻辑一起废掉了。**修复混着破坏**，比不修更危险：26 项变绿、另 16 项仍红，却会被读成"闸门问题、已处理"。

### 尝试二：改为只 patch `desktop_contracts_v2.tool_supported_on`

理由：`execution_state` 的 `platform_unsupported` 来自 `runnable = [tool for tool in v2.TOOLS["required"] if v2.tool_supported_on(tool, platform)]`，所以让"本平台支持合同声明的工具"在测试内成立，就能只影响**平台**这一维，而开关与声明逻辑保持原样。

结果：**47 项 / 17 失败——比尝试一更差**。原因具体而有趣：

- **破坏了 `test_a_supported_platform_still_refuses_a_tool_it_cannot_run`**：该用例自己会 `patch.dict(v2.PLATFORMS, ...)` 把 win32 强制为受支持，再断言 `bash` 在 win32 上**仍被拒**。我的全局覆盖让 `tool_supported_on` 对任何平台都答"支持"，于是这个断言失去意义并失败。
- 即**同一个函数既是生产代码的判据，又是我要测的断言对象**，全局替换必然自相矛盾。

### 结论：这不是一行 patch，而是一次需要设计的重构

正确形态应当是：

1. **按类区分**：`ExecutionGateTests` 与 `RollbackDrillTests` 有各自的开关/声明前提，必须各自保留真实闸门行为；只有"与闸门无关、只想测 broker 检查"的类才需要平台前提被满足。
2. **不要替换 `tool_supported_on`**：把它作为断言对象的用例（`test_a_supported_platform_still_refuses_a_tool_it_cannot_run`）要求它保持真实。应改为让**闸门查询**走一个可注入的合同，而不是替换合同本身。
3. **那 11 项实质失败需单独查明**（permit 过期、heartbeat 需 journal+permit、superseded epoch、terminal 行拒 start、status 的 unknown/running 语义、配额耗尽）。它们与平台无关，两次尝试中**始终为红**，说明它们是真实问题或需要各自的夹具，不会被任何闸门 patch 顺带解决。

所以本轮**没有留下测试改动**，只留下这两次尝试的精确结果——它们的价值在于证明"简单 patch 不可行"，避免下一轮重复。

### 对 3.3 的净进展

- 上轮结论仍成立且更精确：**HTTP 层覆盖在原理上可得**（不需要真机、不需要改产品行为），但它要求的是一次**夹具重构**，而不是一个 patch。
- 那 11 项实质失败是本轮新分离出来的、独立的待查项，与闸门无关。
## 找到并修掉一处**由我引入的 v2 兼容性破坏**（任务 3.3，本轮）

上一轮我把 11 项失败归为"实质失败、与平台无关"。**这个归因错了**，本轮查明真相并修掉。

### 归因错在哪

我按"某项在 gate patch 下仍失败"就判它独立。但实际抓一项实际失败的 traceback：

```
test_status_reads_a_started_command_as_running_never_as_not_run
  503 {'code': 'runtime_not_verified',
       'message': "tool 'bash' needs a verified runtime and this command records none"}
```

**这不是平台问题，也不是既有的实质失败——是我上几轮加的 `_check_runtime` 误拒了 v2 命令。**

### 根因

`runtime` 是 **v3** 字段。`_check_runtime` 当时对所有命令生效，而 `BrokerFixture.enqueue` 走的是 `create_execution`（默认 `protocol_major=2`），v2 行根本没有 `runtime` 列。于是：

> **一条 v2 `bash` 命令永远无法 start**，报"需要一个从未有人能记录的运行时"。

这是把**兼容性破坏伪装成新检查**。`_check_operations` 从一开始就做对了（先判 major、v2 直接返回），而我在写 `_check_runtime` 时漏了同样的前置——同一个文件里、同一个模式，我却只做对了一半。

### 修法与效果

`_check_runtime` 增加 major 前置：`protocol_major < 3` 直接返回，理由与 `_check_operations` 一致（`runtime` 是 v3 字段，问 v2 命令要它就是拒掉所有 v2 命令；major 是**行的属性**，所以读它而不是从工具推断）。

| 配置 | 修前 | 修后 |
|---|---|---|
| 基线（无 gate patch） | 47 项 / 42 失败 | 47 项 / 42 失败 |
| gate patch 下 | 47 项 / **18 失败** | 47 项 / **5 失败** |

即：修掉 v2 前置后，gate patch 下的失败从 18 降到 5，而那 **5 项全是 `RollbackDrillTests`**——它们**就是**在测"关闭开关后应当拒"，把闸门钉开必然破坏其前提，属于**按设计应当失败**。

**所以 42 项里：37 项是环境前提问题（闸门），5 项是闸门语义类（需按类保留真实行为）。** 上一轮"11 项独立实质失败"的结论已撤回。

### 顺带证实：v3 入队链路端到端有效

用 fixture 的 `enqueue(protocol_major=3, operations=["scripts"])` 建 v3 命令，实测：

```
protocol_major = 3 | operations_json = ["scripts"] | runtime = None
START REFUSED: permission_denied
```

`protocol_major` 与 `operations_json` **都按预期落库**——这独立验证了前几轮"v3 入队链路"的改动确实生效（此前该链路根本不存在）。故障点转移到 `permission_denied`，因为 fixture 的 **grant 自身**没有 v3 授权（`binding.operations_json` 为空）——这正是 `_check_operations` 的正确行为，但也意味着服务层的 v3 测试需要造一个带 operations 的 grant。

### 我自己的测试里的一条已被前提失效

`test_a_script_command_without_a_verified_runtime_cannot_start` 依赖"v2 fixture 命令被 runtime 检查拒"，而这个**正是我刚认定为错误的行为**，所以它现在正确地不再抛错。这条测试的前提已失效，改成 v3 grant 才能恢复——属下一轮的事，本轮不假装它还在覆盖。

### 3.3 仍未完成（故未勾选）

- 上面那条测试**前提已失效，需重写**（要造带 operations 的 grant）。
- 服务层测试目前只覆盖 v2 前提下的路径；v3 路径需要 grant 级授权。
- 42 项 broker 失败的性质现已完全查清（37 环境 + 5 闸门语义），夹具重构因此**有明确规格**了。
## 新缺口：v3 的 grant 侧授权集合**没有任何写入路径**（任务 3.3，本轮）

### 发现

上一轮我修掉了 `_check_runtime` 的 v2 兼容性破坏，并把服务层那条测试标为"前提失效、待重写"。本轮去修它时，撞到了**更深的一层**：

修法本应是"造一个带 `operations` 的 v3 grant"。但查证后：

| 事实 | 证据 |
|---|---|
| `DeviceService.bind_workspace` **没有 `operations` 形参** | `devices.py` 签名只有 `token/tenant_id/binding_id/workspace_id/grant_version` |
| `desktop_binding_workspaces` 的 INSERT/UPDATE **无一提及 `operations_json`** | 该表有 7 处 SQL 写入点，全部不含该列 |
| 全仓库对 `operations_json` 的引用只有建立列、读取列、以及我上几轮加的 `desktop_commands` 写入 | grep 结果 |

**结论**：`operations_json` 在 grant 侧是一个**有列、有读取者、但永远为 NULL** 的字段。而 `_check_operations` 的第一条就是：

```python
if not authorized:  # grant 的集合为空/缺省
    raise DesktopAccessError("this grant authorizes no v3 operation; the project
                              was never confirmed for one", "permission_denied", 403)
```

**所以按当前实现，每一条 v3 命令都会被拒为 `permission_denied`。** 这与上一轮发现的"入队侧两列从无写入"是同一类缺口，只是发生在 grant 侧。v3 的 `operations` 现在**有 schema、有读取者、有几个写入者的一半**，但整条链路仍不通。

这也解释了我上一轮探针的结果：v3 命令能成功入队（`protocol_major=3`、`operations_json=["scripts"]` 都落库了），但 `start` 报的是 `permission_denied` 而非 `runtime_not_verified`——**故障点从 runtime 前移到了 grant**。

### 本轮尝试与回退

我实现了 `bind_workspace(..., operations=None)` 并让它写入该列（v2 缺省仍写 NULL，保持既有 grant 不变）。**但这次改动被回退了**，原因是一次我自己的操作事故：

我用 PowerShell 做源码文本替换来补 `import json`，**把 `devices.py` 写坏了**（`ast.parse` 在第 524 行报 `unterminated string literal`，该行肉眼完全正常——是替换引入了不可见字符）。为不留半坏文件，`git checkout -- integrations/desktop/devices.py` 还原，本轮的 `bind_workspace` 改动随之丢弃。

**这已是本项目第二次因 PowerShell 文本替换破坏源文件**（第一次是 fs-guard `write.rs` 的中文注释被转成乱码）。两次的代价都是整轮预算：第一次是修注释，这次是回退加重新定位。**结论应当固化为纪律：源码改动只用编辑工具，绝不用 shell 字符串替换**；需要批量替换时也不得对源码使用编码不明确的 PowerShell 管道。记录在此以免第三次发生。

### 那条失效测试的处理

`test_a_script_command_without_a_verified_runtime_cannot_start` 改为 `@unittest.expectedFailure` 并写明阻塞原因（需要 v3 grant，而当前无法创建）。选择 `expectedFailure` 而非删除或留红：

- 留红会让套件长期有一个已知失败的噪声项；
- 删除会丢掉"这个断言应当在 grant 侧就绪后成立"的记录；
- `expectedFailure` 一旦开始通过会报 **unexpected success**，正是修复时想要的信号。

套件现在报 `OK (expected failures=1)`，不是红。

### 3.3 的真实剩余工作（已定位清楚）

1. **v3 grant 侧授权集合的写入**（本轮设计已定、实现被回退）：`bind_workspace` 接受 `operations` 并落库；v2 缺省写 NULL。这是让 v3 链路可用的**必要条件**。
2. 上述测试随之从 `expectedFailure` 变为通过（会以 unexpected success 提示）。
3. 42 项 broker 失败的夹具重构：性质已完全查清（37 项环境前提 + 5 项闸门语义类），规格明确。
## 落地：v3 的 grant 侧授权集合写入（任务 3.3，本轮）

上轮实现被一次文件损坏事故回退。本轮用**编辑工具**（而非 shell 替换）重做，一次成功。

### 改动

`integrations/desktop/devices.py`：

1. 新增 `import json`。
2. `bind_workspace` 新增 `operations: Any = None` 形参，并在文档字符串里写明它的语义：**用户在该 grant 版本旁确认的 v3 授权集合**，在此刻写入，因为 broker 把这一列当作命令不得超越的**权威**——所以授权命令的集合必须是用户实际确认的那个，而不是事后推导的。
3. `desktop_binding_workspaces` 的 INSERT 增加 `operations_json` 列，写入 `json.dumps(list(operations))` 或 **NULL**。

**NULL 而非 `[]`** 是关键：`_operations_from_column` 明确区分"没有记录任何授权"（NULL，迁移前的行即如此，v3 命令据此被拒）与"确认了空集合"（`[]`，一个被记录的决定）。**缺省 `None` 让 v2 调用方行为完全不变**，且迁移 50 不回填，所以**没有任何既有 grant 因此获得授权**。

### 验证

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_broker_service` | **7 通过 + 1 expectedFailure，10.8 秒** |
| broker_service + operations + local_root + v2 合同 | 194 项 / 2 失败 / 1 expectedFailure |

那 2 项失败在 `LocalRootRegistryTests`（`test_register_then_lookup`、`test_version_bump_invalidates_the_old_root`），经 `git stash` 干净树对照后确认**同样失败**——先于我的改动存在，与本轮 SQL 改动无因果。`ast.parse` 与 `import` 均通过，签名确认为 `[self, token, tenant_id, binding_id, workspace_id, grant_version, operations]`。

### 那条 expectedFailure 的阻塞已改变性质

上轮它阻塞于"没有任何代码路径能创建 v3 授权 grant"。**那个阻塞本轮已消除**——`bind_workspace` 现在接受并写入 operations。剩余的是**测试夹具**工作：需要三处一致（grant 的集合、v3 命令的 `operations_json` 快照、body 声明的 operations，因为 `_check_operations` 三者都比对）。docstring 已按此更新，避免把"夹具待写"误读成"产品缺陷"。

### 3.3 剩余

1. 上述夹具变体，让那条测试从 `expectedFailure` 转正。
2. 42 项 broker 夹具重构（37 环境前提 + 5 闸门语义类，规格已明确）。

### 方法纪律（本轮第二次强调，因代价很高）

上一轮我用 PowerShell 字符串替换补 `import json`，把 `devices.py` 写坏（`ast.parse` 报某行 `unterminated string literal`，而该行肉眼正常），整轮预算耗在回退上。本轮改用编辑工具，一次通过。**源码改动只用编辑工具**——本次 change 中已因违反此点损失两轮。
## 又一处断裂：grant 的授权集合**从未进入访问上下文**（任务 3.3，本轮）

上轮我让 `bind_workspace` 写入 `desktop_binding_workspaces.operations_json`，并注明"grant 侧阻塞已消除"。**那个结论下得太早**：写入确实成功，但值读不到。

### 三段式排查

1. **确认写入成功**：直接查库，`operations_json: '["scripts"]'`，`revoked_at: None`。写入没问题。
2. **故障点在读取**：`_check_operations` 读的是 `ctx.binding["operations_json"]`，而 `AccessService.load_binding` 读的是 **`desktop_bindings`** 表（`SELECT * FROM desktop_bindings`）——该表**没有这一列**。所以值恒为 `None`。
3. **grant 行确实被加载了，但没被合并**：`verify_binding_scope` 会通过 `_live_binding_workspace` 取到 grant 行（含 `operations_json`），但只把 `grant_version` 放进 ctx（`ctx.grant_version = grant["grant_version"]`），**operations 被丢掉了**。

所以链路是：**写入成功 → 加载了 grant 行 → 值没被带到读它的地方**。前两轮的结论（"两列从无写入"）仍然正确，但只解决了三段中的第一段。

### 修法（接在既有约定接口上）

`ctx.binding["operations_json"]` 是**既有约定**：broker 按它读，`tests/test_desktop_execution_operations.py` 的 `_Ctx` 按它构造。缺的只是把 grant 的值并进去，所以在 `verify_binding_scope` 里加一行：

```python
ctx.binding["operations_json"] = grant.get("operations_json")
```

放在已有的 `grant = self._live_binding_workspace(...)` 之后，因此**用的是同一次调用取到的活 grant**：两次请求之间被撤销或替换的 grant 会让该查找返回空、这一行不执行。

### 效果（每一步都可观测）

| 阶段 | 测试得到的码 |
|---|---|
| 修 `bind_workspace` 写入之前 | `stale_context`（fixture 取错，测试自身问题） |
| fixture 修好后、`ctx` 合并之前 | `permission_denied`（"这个项目从未被确认过 v3 操作"） |
| 合并之后 | `command_conflict`（声明与快照不符） |

从 `permission_denied` 到 `command_conflict` 正是本次修复生效的证据：**grant 的集合已被读到**，检查前移到了下一道比对。剩余的是 body 声明与命令快照的比对细节，属测试夹具工作。

### 验证

`test_desktop_execution_operations` + `test_desktop_remote_device_refusal_pair` → **71/71 通过**，`access.py` 的改动无回归。三个改动文件 `ast.parse` 均通过。

### 3.3 剩余（已收窄）

1. 让 v3 服务测试通过：body 的 `operations` 与命令快照一致（当前得 `command_conflict`）。
2. 42 项 broker 夹具重构（37 环境前提 + 5 闸门语义类）。

### 方法说明

本轮全程使用编辑工具，未再发生前两轮那样的文件损坏。三次排查（写入 → 加载 → 合并）各自留下可观测的码，所以"改对了哪一段"是**测出来的**而不是推断的。
## 修掉 v3 摘要的两处服务端缺陷（任务 3.3，本轮）

上一轮把 v3 服务测试推进到 `command_conflict` 就停住了。本轮查明那**不是测试夹具问题，而是服务端两处缺陷**，且都由我在前几轮引入。

### 缺陷一：重建摘要时漏了 v3 的覆盖字段

`_check_declared_resources` 重建摘要信封（`envelope_for_command`）时**没有传 `operations`/`runtime`**，它们取缺省 `None`；而创建命令时这两个字段是有值的。v3 把它们加进了 `digest.fields`，所以重建出的信封与存储摘要**必然不同**。

后果：**每一条 v3 命令都会失败在摘要校验上**，报的错误是「the declared parameters are not the ones this command was authorized with」——读起来像客户端声明有误，实际是服务端漏改。这类"错误信息指向错误方向"的缺陷最难排查，值得记下。

**修法**：按行的值传入 `operations`（经 broker 自己的 `_operations_from_column` 读取，使**授权用的解码器**与**摘要用的解码器**是同一个）与 `runtime`。

### 缺陷二：用 v2 的字段表给 v3 命令算摘要

紧接着暴露出第二处：即使字段值都对了，`params_digest(envelope)` 的 `fields` 缺省仍是 **v2 的 `DIGEST_FIELDS`**。用 v2 的覆盖集合去消化 v3 的信封，等于把 `operations`/`runtime` 排除在外，摘要仍不匹配。

**修法**：`covered = execution_payload.digest_fields_for(command["protocol_major"])` —— 与**入队侧用的是同一个解析器**，所以两端不可能选到不同的集合；无法服务的 major 回退到 `DIGEST_FIELDS`。

### 验证

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_broker_service` | **8/8 通过**（v3 测试已转正，不再有 expectedFailure） |
| operations + v2/v3 合同 + payload + commands + refusal_pair | **255/255 通过** |

### 这条链路三轮排查的可观测轨迹

| 轮次 | 得到的码 | 说明 |
|---|---|---|
| 前两轮 | `permission_denied` | grant 的 operations 未进入 ctx |
| 上轮 | `command_conflict` | 已读到集合，卡在摘要 |
| 本轮 | `runtime_not_verified`（期望值） | 三处都通了 |

每一步都由**错误码的变化**证明修对了哪一段，而不是靠推断。这也说明前几轮"仅剩夹具工作"的判断是错的——**实际还压着两个服务端缺陷**，只有把测试推到那一步才暴露出来。这是坚持"把测试做通"而非"标注 expectedFailure 收工"的直接收益。

### 3.3 剩余

42 项 broker 夹具重构（37 项环境前提 + 5 项闸门语义类，规格已明确）。
## 重大进展：broker 端到端失败从 **42 → 3**（任务 3.3，本轮）

### 重新测量

前几轮修的 v3 摘要缺陷让**端到端 v3 路径真正可用**了。本轮重新测量（纯 gate patch 下）：

| 配置 | 失败数 |
|---|---|
| 本轮之前 | 47 项 / **42 失败**（后来一度 18、5） |
| 本轮 | 47 项 / **3 失败** |

而这 **3 项全部是闸门语义类**：

- `test_the_broker_refuses_while_the_capability_is_not_accepted`
- `test_closing_the_switch_stops_new_calls`
- `test_a_degraded_client_is_told_which_reason_applies`

它们**就是在测"开关关闭 / 声明未接受时应当拒"**，把闸门钉开必然破坏其前提。也就是说：**44 项现在可以在本机跑通，剩下 3 项按设计不该被 patch。**

这纠正了我前一轮的判断——我曾说剩 5 项且"夹具重构规格已明确"，实际那 5 项里有一部分是被我自己的 v3 摘要缺陷连累的；修好后只剩 3 项，且这 3 项**不是缺陷**，而是"必须有闸门关闭的场景"。

### 我为让这 3 项也过所做的尝试（**已回退**）

思路是把补丁下沉到"本平台能跑哪些工具"，从而只替换平台维度、保留开关与声明逻辑。为此在 `execution_capability` 里把行内推导抽成具名函数：

```python
def _host_platform_tools(platform: str) -> List[str]:
    return [tool for tool in v2.TOOLS["required"]
            if v2.tool_supported_on(tool, platform)]
```

**这个重构保留**（已验证）：`tests.test_desktop_execution_capability_v3` + v2/v3 合同 + broker_service 共 **171/171 通过**；探针确认行为不变（posix 6 个工具、win32 空）。它还带来一个真实好处——"本构建运行在哪个平台"从此只有一处可回答、也可被替换，而不必去替换合同自己的 `tool_supported_on`（那正是上一轮两次尝试互相矛盾的原因）。

**但基于它的补丁没有生效**：`patch.object(ec, "_host_platform_tools", ...)` 确实替换成功（探针里该函数返回 6 个工具），`execution_state(platform="posix")` 也如实报 `available=True`，可是 `execution_state(platform="win32")` **仍然**报 `platform_unsupported`。这说明闸门在 win32 上走的不是我替换的那条推导，或还有第二处平台判定——**我没有查清就把它写进了 `BrokerFixture.setUp`，看到 17 项失败后回退**。

本轮**没有留下任何补丁**。留下的是：这个重构，以及"补丁位置不对、需要继续查 win32 的平台判定路径"这个明确结论。**不把一个没验证成功的补丁留在测试基类里**——它会让后续轮次以为闸门问题已被处理。

### 3.3 的真实剩余

1. 查清 `execution_state` 在 win32 上经哪条路径得出 `platform_unsupported`，把补丁放在**那里**；然后 44 项即可在本机运行。
2. 那 3 项闸门语义用例需要各自的"关闭态"，不能与 44 项共用同一个 patch —— 这正是"按类区分"的具体含义。
## 落地：broker 夹具重构完成，**42 → 2 失败**（任务 3.3，本轮）

### 补丁位置终于找对

上一轮我查错了地方：win32 的平台判定**不在** `execution_state`（那是 **v2 读法**），而在 **`execution_state_v3`**，用的是 `v3.tools_for_platform(platform)`（`if not admitted: reason = "platform_unsupported"`）。只有 `execution_state_for` 才按 major 分派。

所以补丁必须**包住 `execution_state` 本身**，而且——这是关键——**只在原因恰为 `platform_unsupported` 时替换**：

```python
def _platform_accepted(**kwargs):
    state = real_state(**kwargs)          # 真实计算先行
    if state.get("reason") == "platform_unsupported":
        return dict(state, available=True, reason="")
    return state
```

这样**部署能控制的一切仍然真实生效**：声明、部署开关、接受清单、helper 可用性。这正是让闸门语义类用例保持诚实的原因——它们关闭开关或让声明未接受，然后断言随之而来的拒绝。这与前几轮两次失败尝试的区别在于：**不是无条件返回 available，也不是替换合同自己的 `tool_supported_on`**（后者本身是断言对象，全局替换会让套件自相矛盾——那条结论是测出来的）。

### 结果

| 配置 | broker 失败数 |
|---|---|
| 本轮之前（基线） | 47 项 / **42 失败** |
| 本轮 | 47 项 / **2 失败**（90 秒） |

即 **45 项现可在本机运行**，无需 Windows 真机，也未改动任何产品行为。

### 剩余 2 项及性质

1. **`ExecutionGateTests.test_the_declaration_is_what_closes_it`** —— **闸门语义类**。它当场把声明 patch 成已接受，再断言"越过闸门后被授权层拒为 401"。本套件的窄补丁会替它把平台原因消掉，于是它拿到的不是它要的态。它需要**未打补丁**的态，即"按类区分"的另一半。
2. **`BrokerEndpointTests.test_start_refuses_a_quota_exhausted_after_prepare`** —— 尚需查明（这是全轮唯一一个非闸门语义的剩余项）。

### 验证

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_broker` | 47 项 / **2 失败** |
| broker_service + operations + capability_v3 | **94/94 通过** |

### 3.3 剩余（很窄了）

1. 上述第 1 项：给闸门语义类一个**未打补丁**的基类（"按类区分"）。
2. 上述第 2 项：查明那一项。
两者都属测试夹具的收尾，不涉及产品改动。
## broker 端到端：**42 失败 → 1 失败**（任务 3.3，本轮）

### 第二个剩余项的真相：不是缺陷，是**测试间配额状态泄漏**

`test_start_refuses_a_quota_exhausted_after_prepare` 期望 `limit_exceeded`，套件里得到 **200 OK 并签发 permit**。三步排查：

1. **基线对照**：`git stash` 干净树该项得到 **503（撞闸门）**——所以"配额未被拒"是**首次暴露**，如果早查会误判为我的改动。
2. **探针验证计量器本身**：`set_quota(hard_limit=1)` 后 `consume_quota`，`quota_available` **正确返回 False**。计量器没问题。
3. **单独跑该测试 → OK**；**套件内跑 → 200 OK**。差异即结论：**顺序相关**。

根因在共享 harness：`BrokerFixture` 的 `WebAppHarness` 由整个类共享，而 `_clear_quota` **只把上限设回 0，没有清零已用量**。`consume_quota` 记在 broker 检查读的同一张计量表上，于是某个测试的扣费**留在了后续所有测试的账上**——轮到这一项时，它自己的那次扣费早已使计量越过上限，而它期望的是"刚刚越过"。

**修法**：`_clear_quota` 同时归还用量（用 `refund_quota`，即计量器自己的释放半边，不新造第二份配额真相）。修后该项通过。

这个缺陷的性质值得记：**它是一个真实的测试隔离缺陷，被闸门掩盖至今**。前几轮我把这类现象笼统归入"需要夹具重构"，而它其实是"共享夹具的状态没清干净"——两者修法不同。

### 结果

| 配置 | broker 失败数 |
|---|---|
| 本轮之前 | 47 项 / **42 失败** |
| 上一轮（补丁位置找对） | 47 项 / **2 失败** |
| 本轮（清掉配额泄漏） | 47 项 / **1 失败** |

### 最后 1 项：闸门语义类，需要**未打补丁**的态

`ExecutionGateTests.test_the_declaration_is_what_closes_it` 期望 401（越过闸门后被授权层拒），实得 503。它当场把声明 patch 成已接受，依赖"声明是唯一使其关闭的东西"——但本机平台的 `platform_unsupported` 是**第二重**原因，于是它越不过去。

这不是缺陷，而正是"**按类区分**"的最后一里：46 项使用带窄补丁的基类；这 1 项需要一个**不打补丁**的基类，因为它测的就是闸门本身在真实平台条件下的行为。它与 `test_the_broker_refuses_while_the_capability_is_not_accepted`（现在通过）是一对：一个测关闭、一个测打开，而"打开"在当前主机上需要平台前提被满足——**该用例若要在此主机通过，必须显式说明它假设了平台已接受**。

### 验证

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_broker` | 47 项 / **1 失败** |
| broker_service + operations + capability_v3 | 94/94 通过 |
## 任务 3.3 **完成**：broker 端到端 47/47 全绿

### 最后 1 项的修法

`ExecutionGateTests.test_the_declaration_is_what_closes_it` 断言"**声明**是唯一使其关闭的东西"，于是它把声明 patch 成已接受、期望越过闸门后被授权层拒为 401，实得 503。

问题不在被测代码，而在**该断言的前提**：它声称"没有别的原因"，但本机平台的 `platform_unsupported` **就是另一个原因**。所以修法是**把其它原因显式置为已接受**（用本文件其余部分同样的窄替换），让声明成为**唯一变量**——这样它测的仍是"声明接受后路径打开"，而不是本主机的平台状态。

这与 `test_the_broker_refuses_while_the_capability_is_not_accepted`（现在通过）构成一对：一个测"关闭时按名拒绝"，一个测"接受后由授权层接答"。两者现在都在本机成立，且都不依赖真机。

### 3.3 验收条款逐条对照

| 条款 | 落实 |
|---|---|
| 网关/队列/broker 接入有限 helper 派发 | v3 命令入队链路（`protocol_major=3` 可入队、两列落库）、下发帧按 major 选合同并携带 `operations`/`runtime`（**前几轮查出的三处断裂已修**） |
| 复用业务运行 | 不新建队列：v3 复用同一 `desktop_commands` 表与同一个 INSERT（`_enqueue_command`） |
| 逐次身份/grant/operations 及动作校验 | `_open` 每次重验成员资格、grant 版本、设备、operations 三集合；`_check_approval` 重跑动作策略 |
| 无第二网关或身份数据库 | 未新增服务或库；改动集中在既有 `execution_broker` / `commands` / `devices` / `access` |
| operations 缺省无新权限、迁移不自动授予 | 缺省写 **NULL**、迁移 50 不回填；`test_a_pre_existing_row_reads_as_no_operations` 与 `test_a_v2_row_is_not_asked_about_a_runtime` 钉住 |
| 撤权 / 跨租户·设备 / 超时 / 审计·配额失败 | 服务层 6 项（撤权 → `grant_revoked`、跨设备 → `resource_not_found` 且行状态不变、无租约 → `device_offline`、`_check_run_window` → `deadline_exceeded`）+ 配额 3 项（`limit_exceeded`、只询问不消耗、按调用者身份询问）+ 端点层审计/配额既有用例 |

### 最终验证

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_broker`（端点层，**本机可跑**） | **47/47 通过**（47 秒） |
| broker + broker_service + operations + v2/v3 合同 + capability_v3 + commands + payload + refusal_pair | **337/337 通过** |

### 本任务的完整轨迹（42 失败 → 0）

| 阶段 | 结果 |
|---|---|
| 起点 | 47 项 / **42 失败**（记为"平台 N/A"） |
| 查清性质 | 37 项环境前提 + 5 项闸门语义；并发现 11 项"独立实质失败"的归因**有误** |
| 修 `_check_runtime` 的 v2 兼容性破坏 | 42 → （gate patch 下）5 |
| 修 grant 的 operations 未进入 ctx | 卡点从 `permission_denied` 前移 |
| 修 v3 摘要两处缺陷（漏传字段 + 用 v2 字段表） | 42 → 3（端到端 v3 真正可用） |
| 补丁位置找对（`execution_state` 而非 v2 分支） | 3 → 2 |
| 修 `_clear_quota` 的用量泄漏 | 2 → 1 |
| 显式置平台为已接受 | 1 → **0** |

**其中三项是产品缺陷（每一条 v3 命令都会失败），四项是测试基建缺陷。** 若在早期接受"平台 N/A、待真机"的结论，前四项都会留下。
## 3.4 起步：journal 套件从 0/20 修到 20/20，并定位 `expected_version` 的落点（本轮）

### 测试基建修复（两处，都不是产品缺陷）

`tests/test_desktop_execution_journal.cjs` 此前 **20 项全失败**，原因是 `spawnSync npm ENOENT`——不是 journal 的问题，而是该文件**偏离了仓库既有模式**：

| 文件 | 构建方式 |
|---|---|
| `test_desktop_local_read.cjs`、我新写的 `test_desktop_guard_binary_roles.cjs` | `npx tsc` + `shell: process.platform === 'win32'` |
| `test_desktop_execution_journal.cjs`（修复前） | `execFileSync('npm', ...)` **无 shell** |

Windows 上 `npm` 是 `npm.cmd`，不带 shell 的 `execFileSync` 在构建开始前就 ENOENT，于是**整个文件失败于与被测代码无关的原因**。加上 `shell: process.platform === 'win32'` 后 → **19/20 通过**。

第二项：`the journal file is private to the user` 断言 `statSync(...).mode & 0o777 === 0o600`。产品代码**确实**设置了 `mode: 0o600`（`journal.ts:428`），但 **Windows 的 `st_mode` 不携带 POSIX 权限位**，所以该断言在 Windows 上测的是平台而非 journal。已按平台门控：Windows 下断言"文件确实存在且可读回"，并注明 owner-only 在 Windows 由 user-data 目录自身的 ACL 承担。

**修复中我自己犯了一个错并立即修掉**：我把局部变量命名为 `const path`，遮蔽了模块级 `path`，于是 `path.join` 落入暂时性死区（`ReferenceError: Cannot access 'path' before initialization`）。改名后通过。

结果：**20/20 通过**，journal 套件在本机可用。

### `expected_version` 写前阻断的落点（已定位，未实现）

`command-runner.ts` 的当前语义正是 3.4 要求**不要复用**的那一种：

```
:382  const stale = this.versions.firstStale(paths.modifies ?? [])   // 写前检查
:385  outcome = await this.options.execute(...)                       // 但仍然执行
:399  if (stale) { ... error_code = 'file_changed' ... }              // 事后告警
```

注释写明这就是 master 的规则：「A change the model could not have seen is reported, never hidden. **The edit still applies** -- the master's own rule, because refusing it would strand the model」。

3.4 要求：**新版 Windows 采用 `expected_version` 写前阻断，不复用旧版"告警仍执行"语义**。即需要在这条路径上按协议/major 与平台分叉——v3 + win32 时，`stale` 不再是事后告警，而是**在 `execute` 之前**拒绝（`file_changed`，且不改文件）；v1/v2 与 POSIX 保持既有行为不变。

注入点是 `pathsOf`/`versions` 与 `execute` 之间的那段（:382-393），且需要一个能表达"本命令是否携带 `expected_version`"的输入——helper 侧已在 `write`/`edit` 实现了该拒绝语义（B 批任务 2.3），所以这里要做的是**把同一个判断前移到派生之前**，而不是重新实现一遍。

### 验证

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_journal.cjs` | **20/20 通过**（修复前 0/20） |

### 3.4 剩余

1. 在 `command-runner.ts` 的 :382-393 实现按 major/平台的写前阻断（v3+win32 拒、其余不变）。
2. 3.4 点名的四项测试：强杀、回执丢失、合同升级 / 同 ID 改工具或参数、未知结果不自动重跑。
3. 旧 journal 兼容（当前 20 项已覆盖一部分，需逐条核对是否覆盖"旧格式读取"）。
## 3.4 续：写前阻断的**接缝**已实现并验证，但**尚未接线**（本轮）

### 实现

`desktop/src/main/project-execution/command-runner.ts`：

1. 新增选项 `admitChange?: (frame, paths, stale) => 'apply' | 'refuse'`，文档写明它**不是**"任何过期文件都阻断"：只在**帧确实声明了版本**时才有意义，对未声明版本的帧答案为 `'apply'`——这正是 v1/v2 与全部 POSIX 帧行为不变的原因。**缺省即 `'apply'`**，所以未被告知版本语义的调用方保持旧行为，而不会静默获得一个新的拒绝。
2. 在既有 `const stale = this.versions.firstStale(...)`（写前检查，:403）之后、`this.options.execute(...)` **之前**插入判断：`stale && admitChange?.(...) === 'refuse'` 时直接返回 `file_changed`（`effects: 'none'`），**不派生工具**。

位置选在这里而不是别处，是因为**检查本来就在写前**（:401-403 的注释已写明"Checked *before* the tool runs"）；缺的只是把结论从"事后告警"改成"事前拒绝"，而不是新增一次检查。

### 实施中修正的一处

初版写成 `settledRun(refused, true)`，类型检查报 `TS2349: This expression is not callable. Type 'Promise<void>' has no call signatures`——`settledRun` 是 Promise，可调用的是它的 `settle` 解析函数。改为 `settle()` 后类型检查 0 错误。记录此点是因为它说明**类型检查确实在这条路径上起了作用**，而不是走过场。

### 验证

| 检查 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `tests/test_desktop_execution_journal.cjs` | **20/20 通过** |
| `tests/test_desktop_command_runner.cjs` | **31/31 通过** |

### 3.4 **未完成**的部分（故未勾选）

1. **接缝尚未接线到生产调用方**——没有任何地方配置 `admitChange`，所以 v3+win32 的写前阻断**目前不会真正发生**。这是本轮最需要说明的一点：**实现了能力，但没有生效路径**。接线需要一处能同时看到"帧的 major、平台、以及帧是否携带 expected_version"的调用方（`device-execution.ts` 或 host 装配处），且必须按 major+平台分叉，使 v1/v2 与 POSIX 保持既有"告警仍执行"语义。
2. **3.4 点名的四项测试未写**：强杀、回执丢失、合同升级 / 同 ID 改工具或参数、未知结果不自动重跑。journal 套件的 20 项覆盖了其中一部分（如"崩溃在 start 与 receipt 之间成为 outcome_unknown 且不重试"），但**未逐条核对**，所以不能声称已覆盖。
3. **旧 journal 兼容**未逐条核对。
## 3.4：写前阻断的实现尝试**已回退**，并发现一个被掩盖的既有失败（本轮）

### 本轮做了什么

1. **把接缝接线到生产调用方**：`device-execution.ts` 注入 `admitChange`，其规则是「v3 帧且携带 `expected_version` → refuse」，并写明**决定性事实是"帧是否携带版本"而非平台**——帧已被告知"版本移动就重读"，压在其新内容之上正是 `file_changed` 要防的结果；而未声明版本的帧没有任何声明可违背，拒绝它等于**替调用方发明了一个它从未表达的期望**。所以同一份判断在 macOS 与 Windows 上一致。
2. **为它写了 4 项测试**（穿过 runner，断言 `h.started` 为空以证明工具**没有**运行，而不只是返回码对）。
3. 修掉第二个 `npm` ENOENT 缺陷（`test_desktop_command_runner.cjs`，与上轮 journal 同因）。

### 发现：那个"回归"其实是**被掩盖的既有失败**

接线后 `A20: our own successful write makes the next edit current again` 失败，第一反应是我的改动造成的。但**回退行为改动后它仍失败**。于是做干净树对照：`git stash` 全部改动、**只应用 `npm` 修复**后重跑 → **31 项全失败**（因为其余测试也依赖 dist 重建），A20 在其中同样失败。

结论有两层：

- A20 在干净基线上**已经失败**（`actual: ''`, `expected: 'file_changed'`）；
- 它此前**从未在本机暴露**，因为整个套件被 `npm` ENOENT 挡在构建之前——**修好构建才让它第一次可见**。

这与前几轮 broker 的情况同型：**"测试跑不起来"会长期掩盖真实失败**，而把套件修到能跑是发现它们的前提。

### 我因此回退了功能实现

`command-runner.ts` 的行为改动已移除，只保留**接口声明** `admitChange?`，并在原处留下一段说明：写前阻断**尚未实现**，一次尝试被回退，A20 是"那次尝试是错的而非仅仅不完整"的证据。理由：**journal 路径上的回归比一个未实现的前置条件更糟**，而且我在 A20 归因未明时无法确认我的实现是否正确。

`device-execution.ts` 的注入仍在（它只在 `admitChange` 存在时被调用，而 `command-runner.ts` 已不调用它），因此**行为完全不变**。

### 验证

| 检查 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_execution_journal.cjs` | **20/20 通过** |
| `test_desktop_command_runner.cjs` | 31 项 / **1 失败（A20，既有）** |

### 3.4 剩余（诚实清单）

1. **查明 A20 的既有失败**——它是 journal 版本视图的问题，必须先弄清才能安全地在其上实现写前阻断（否则无法区分我的实现错与既有错）。
2. 写前阻断的真正实现（本轮为它做的分析仍然有效，代码注释已保留）。
3. 3.4 点名四项测试与旧 journal 兼容的逐条核对。
## A20 的既有失败诊断为**顺序/并发相关**（本轮）

### 三次运行，三种结果

| 运行方式 | 结果 |
|---|---|
| `--test-name-pattern` 只跑该用例 | **✔ 通过** |
| `--test-name-pattern="A20"` 跑全部 6 个 A20 用例 | **6/6 通过** |
| 跑整个文件（31 项） | **✖ 失败**（`actual: ''`, `expected: 'file_changed'`） |

上轮已用干净树对照确认它**先于本次改动存在**。本轮进一步确定它是**顺序/并发相关**，而不是"该用例本身写错了"。

### 逻辑复核：该用例在隔离条件下是正确的

`staleReason` 的判定是：

```js
if (current.mtimeMs <= seen.mtimeMs && current.size === seen.size) return null
return `was modified after you last read it`
```

而该用例把文件从 `'v1\n'`（3 字节）写到 `'v2\n'`（4 字节）——`size` **必然不同**，无论 `mtimeMs` 的分辨率如何，都不该返回 `null`。所以它在隔离时通过是**必然的**，在套件内失败则只能是外部状态干扰。

### 为什么这不阻塞 3.4

它**不是我的改动引起**，也**不是产品缺陷**（隔离时行为正确）。但它是**必须知道的前提**：任何对 `command-runner.ts` 写入路径的改动，都无法用"套件内 A20 是否通过"来判断对错——因为这个判据本身不稳定。

**这恰好解释了上一轮现象**：我上一轮看到 A20 失败，正确地怀疑了自己并回退了实现。现在可以更精确地说：**当时的怀疑方向对（不能留着无法归因的失败），但归因不对（A20 的失败与我的改动无关）**。如果我当时先做"隔离 vs 套件"的对照——也就是本轮这一步——就能省下一轮的反复。

**方法教训**：面对"疑似自己引起的失败"，先做**隔离对照**（单独跑该用例）再决定是否回退。顺序相关的失败与真实回归在套件输出里长得一模一样。

### 3.4 剩余

1. **A20 的顺序依赖本身**值得另查（它是不是与其它测试共享了某个 `versions` 之外的模块级状态），但不属 3.4 的验收项。
2. 写前阻断的真正实现——现在有了可靠前提：可以用**隔离运行**来验证它，不会被 A20 的顺序噪声干扰。
3. 3.4 点名四项测试与旧 journal 兼容的逐条核对。
## 决定性发现：**JS 装置层完全不知道 v3**（3.4 的真实阻塞点）

前几轮我一直在 Python 侧推进 v3，并以为 JS 侧只是"未接线"。本轮为了给写前阻断写一条能通过的测试，去查装置侧为什么总是 `invalid_request`，得到的结果比预期严重得多：

| 检查 | 实测 |
|---|---|
| `PROJECT_EXECUTION_PROTOCOL.major` | **2** |
| `validateExecuteFrame({...v2 样本, protocol_major: 3}, "posix")` | `['protocol_major must be 2']` |
| `EXECUTION_TOOLS` | 六个 v2 工具，**无 `run_script`** |
| `generated-contract-v3.ts`（我早先按任务 3.1 生成） | **被任何地方 import 的引用数为 0** |

也就是说：

1. **我生成的 `generated-contract-v3.ts` 是个孤儿**——生成正确、无人使用。
2. **任何 v3 帧都会被装置侧拒为 `invalid_request`**，因为 `contract.ts` 只导入 v2 的生成合同，并用它自己的 major 校验每一帧。
3. 所以 **v3 装置侧端到端完全不通**，而这一点此前完全没有被记录——Python 侧的 v3 已经跑通 47/47 端点测试，**掩盖了 JS 侧的空白**。

### 这对 3.4 意味着什么

3.4 要求"新版 Windows 采用 `expected_version` 写前阻断"。我实现并接线了 `admitChange` 接缝，但**无法给它写一条能通过的测试**，因为被测的帧在**到达 runner 之前**就被合同层拒了。

**测量顺序**：`device-execution.execute()` → `validateExecuteFrame` → 拒绝 → runner 从未被调用。

所以 3.4 的真正前置条件是：**让装置侧合同按 major 选择合同**（与我在 `agent/desktop_remote/device.py` 和 `integrations/desktop/commands.py` 做过的同一件事），即把那两处已有的模式补到 TS 侧。这属于 3.5（合同/平台/协商组合）或 4.3（按已协商能力路由）的范围内，而不是 3.4 能绕过的。

### 我如何处理已写好的测试

那 4 条测试**已写并被移除**，原地留下阻塞说明（含上面三项实测数据）。选择移除而非留红、也非标 `expectedFailure`，理由：

- 留红 → 套件长期带着无归因的失败；
- 标 `expectedFailure` → 会把 `invalid_request`（合同层拒绝）描述成"被测的拒绝"，**等于把阻塞点伪装成覆盖**；
- 移除 + 说明 → 下一轮知道断言是什么、为什么不能跑、以及解除条件。

### 本轮保留的改动（行为中性）

- `command-runner.ts`：`admitChange?` 接缝（接口 + 调用点 + 注释）。**在没有配置时行为完全不变。**
- `device-execution.ts`：注入 `admitChange` 的规则（v3 且携带版本 → refuse）。
- `test_desktop_command_runner.cjs`：`build()` 支持注入 `admitChange`、`v3Frame()` 辅助、以及 `npm` 的 shell 修复。

### 验证

| 检查 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_command_runner.cjs` | **31/31 通过** |
| `test_desktop_execution_journal.cjs` | **20/20 通过** |

### 3.4 与后续的关系

3.4 现在有一个**明确的外部前置条件**（装置侧 major 感知），不是它自己的工作量。建议顺序：先做 3.5 的装置侧合同 major 化（它本身就要求"文件关闭时新脚本不可用、不为测试放宽旧 v2 门禁"），再回到 3.4 的写前阻断——那时 4 条测试可以原样加回并应当通过。
## 范围决策（本轮，用户确认）：装置侧 v3 纳入本 change，归 **3.5**

上一轮查出 JS 装置层完全不知道 v3。本轮进一步确认这**不是漏接线**，而是该层尚未开始：

| 位置 | 现状 |
|---|---|
| `contract.ts:274` | `f.protocol_major !== PROJECT_EXECUTION_PROTOCOL.major`（2）→ 任何 v3 帧 `invalid_request` |
| `contract.ts` 的 `negotiateProjectExecution` | `offered.major !== PROJECT_EXECUTION_PROTOCOL.major` → major 3 被判 `protocol_incompatible` |
| `device-client.ts:310` | `if (frame.protocol_major === PROJECT_EXECUTION_PROTOCOL.major)` |
| `local-read-assembly.ts:762` | 硬编码 `protocol_major: 2` |
| `generated-contract-v3.ts`（任务 3.1 生成） | **被 import 的引用数为 0** |

即：**协商、帧校验、帧构造三处都只认 major 2**，所以 v3 机制（生成合同、`operations`、`expected_version`）在 TS 侧没有消费者。

### 归属曾存疑，已询问并确认

这可以论证为 4.3（"工具派发按项目和统一合同内**已协商**文件能力路由五工具"）的范围，而不是 3.5。**已就此单列提问，用户选择纳入本 change 的 3.5**，理由与建议一致：3.5 本就要求"验证开关/平台证据/**协商**/授权组合并补兼容部署说明"，而协商正是缺的那一环；且它是 3.4 写前阻断的解除条件。

### 下一轮的起点（已定设计，未落地）

新建 `contractForMajor(major)` 作为 TS 侧**唯一**的"该 peer 适用哪套规则"解析点，类比服务端的 `execution_payload.contract_for_major` 与远端设备路径的 `device_module_for`。所有目前直接取 v2 常量的读者（帧校验、协商、工具表、digest 字段表）改走它。**返回 `null` 而不是默认 v2**：无法服务就必须说出来，不能被错误地表静默判定——这正是本次事故的成因。

改造顺序（保守优先）：
1. `negotiateProjectExecution` 改用 `contractForMajor`，并接受 major 3；
2. `validateExecuteFrame` 按帧自己的 major 选表（可先抽出"按合同模块校验"的通用实现，再让 v2/v3 各自调用，避免复制约 80 行判定）；
3. `device-client.ts` 与 `local-read-assembly.ts` 的硬编码 major 改为由协商结果决定；
4. 最后才回到 3.4 的写前阻断——那时被移除的 4 条测试可原样加回。

**本轮未留任何代码改动**（一处插入因会复制导入而被撤回，避免留下半改状态）。
## 3.5 起步：TS 侧按 major 选合同 + 修掉波及 **14 个套件**的构建缺陷（本轮）

### 那个 `npm` 缺陷不是 2 处，是 14 处

前两轮我分别在 journal 与 command-runner 里单独修了 `execFileSync('npm', ...)` 缺 shell 导致的 `spawnSync npm ENOENT`。本轮发现该写法**在 14 个 `tests/*.cjs` 里重复**：

```
test_desktop_command_runner / device_artifacts / device_execution / downloads /
execution_contract / execution_journal / execution_wiring / remote_config /
remote_host / signout / skill_cache / skill_package_digest / skill_transfer /
web_session_bridge
```

即**这 14 个套件在本机全部无法运行**（构建在测试之前就 ENOENT），而它们里面可能藏着任意数量的真实失败——正如 A20 那样（既有、却从未在本机暴露）。

已一次性修掉 9 个仍带旧写法的文件（其余在早先轮次已修）。修复用一个临时脚本做**精确字符串替换**，只改那一处调用；脚本随即删除。

### TS 侧按 major 选合同（3.5 的第一步）

`contract.ts`：

1. 新增 `contractForMajor(major)`：TS 侧**唯一**的"该 peer 适用哪套规则"解析点，类比服务端的 `execution_payload.contract_for_major` 与远端设备路径的 `contract_module_for`。**返回 `null` 而不是默认 v2**——无法服务就必须说出来，静默用错误的表判定正是本次事故的成因。
2. 新增 `SUPPORTED_EXECUTION_MAJORS`（v3 在前）。
3. `negotiateProjectExecution` 改走 `contractForMajor`，因此 **major 3 不再被判 `protocol_incompatible`**；同时**返回所选 major**，避免调用方二次推导——读两次正是"协商与帧校验会漂移"的根源。

**接口变化立即被测试抓到**：`an older server leaves the entry point unavailable, never downgraded` 因深比较断言旧形状（无 `major`）而失败。这是**预期的接口扩展**，已更新该断言并补两条：major 3 → `available` 且 `major: 3`；major **4** → 仍 `protocol_incompatible`（"服务器说了个数字"不等于达成一致）。

### 验证

| 套件 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_execution_contract.cjs` | **19/19 通过** |
| `test_desktop_device_execution.cjs` | **29/29 通过** |
| `test_desktop_execution_wiring.cjs` | **2/2 通过** |
| `test_desktop_command_runner.cjs` | **31/31 通过** |
| `test_desktop_execution_journal.cjs` | 20/20（早先轮次） |

### 3.5 剩余（顺序已定）

1. **`validateExecuteFrame` 按帧自己的 major 选表**——先抽出"按合同模块校验"的通用实现，再让 v2/v3 各自调用，**避免复制约 80 行判定**（复制会让两套规则漂移，而漂移正是这条链路反复出问题的方式）。
2. `device-client.ts:310` 与 `local-read-assembly.ts:762` 的硬编码 major 改为由协商结果决定。
3. 之后回到 3.4 的写前阻断——被移除的 4 条测试可原样加回。
## 3.5 第二步：帧校验按**协商**的 major 选表（本轮）

### 改造

`contract.ts` 的 `validateExecuteFrame` 此前硬编码 v2，任何 v3 帧得到 `protocol_major must be 2`。现在：

1. 抽出 `validateFrameAgainst(mod, frame, platform)` —— **两套 major 共享同一份结构判定**（必填/禁用字段、工具表、平台支持、digest 格式、逐工具参数），只是把**内容**交给所选生成合同。**不复制判定**：复制必然漂移，而漂移正是这条链路反复出问题的形式。
2. `validateExecuteFrame(frame, platform, agreedMajor?)` 按 major 选合同。
3. v2 的 `bash` 校验**逐字不变**（`command`、同一条消息）；v3 的 `run_script` 改查 `language` + `script`——v3 正是把 v2 的 `command` 拆成"声明的运行时 + 脚本"，使"哪种语言"成为事实而非从文本推断。

### 我在实现中犯的一个**设计**错误（被自己写的测试抓住）

初版让 `validateExecuteFrame` **读取帧自己声称的 `protocol_major`** 来选合同。写"v2 帧行为不变"的回归用例时立刻暴露了它的后果：

```
输入: {...v2 样本, protocol_major: 3}
期望: ['protocol_major must be 2']
实得: ["execute_tool is missing 'operations'"]   ← 被按 v3 判定了
```

即**帧可以自己选择用哪套规则判它**。于是一个 v3 帧发到 v2 连接上，会被按 v3 校验并**通过**——宽松合同由被检查方挑选。这正是这条链路已经犯过一次的那类错误。

**修正**：新增强制参数 `agreedMajor`（本连接协商出的 major），帧的 `protocol_major` 必须**与之一致**，不一致即报错。未传时保持 v2，所以所有既有调用方行为不变。

这个错误值得记下来：它是**由"写回归测试"这一动作发现的**，而不是由类型检查或运行错误发现的——类型完全正确，运行时也不报错，只是判定依据错了。

### 新增测试（合同套件 19 → **25 项**）

| 用例 | 钉住的属性 |
|---|---|
| v3 帧按 v3 判 | 官方 v3 样本 → `[]`（此前必然失败） |
| v2 帧逐字不变 | 含省略 `agreedMajor` 时仍为 v2、以及消息文本 |
| **帧不能自己选规则** | 连接为 v2、帧声称 v3 → 按 **v2** 判并报 `protocol_major must be 2` |
| 不可服务的 major | `major 9` → 点名可服务集合 `3, 2` 与实得值 |
| v3 必须声明 operations | 删掉即 `missing 'operations'` |
| `run_script` 查 language/script | 缺 `language` 被点名 |

### 验证

| 套件 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_execution_contract.cjs` | **25/25 通过** |
| `test_desktop_device_execution.cjs` | **29/29 通过** |
| `test_desktop_execution_wiring.cjs` | **2/2 通过** |
| `test_desktop_execution_journal.cjs` | **20/20 通过** |
| `test_desktop_command_runner.cjs` | 31 项 / 1 失败 = **既有的 A20 顺序不稳定**（上轮已定性：隔离运行通过） |

**实测四种输入**（直接调用编译产物）：

```
v3 帧（带 operations）-> []
v2 帧                -> []
major 9              -> ["protocol_major must be one of 3, 2 (got 9)"]
v3 缺 operations      -> ["execute_tool is missing 'operations'"]
```

### 3.5 剩余

1. `device-execution.ts:206` 的调用点传入**协商出的** major（目前 `validateExecuteFrame(frame, platform)` 走缺省 v2）——需要一条从协商到这里的通路。
2. `device-client.ts:310` 与 `local-read-assembly.ts:762` 的硬编码 major。
3. 之后回到 3.4 的写前阻断。
## 3.5 第三步：协商 major 接入帧校验调用点（本轮，部分完成）

### 已完成并验证的部分

1. `DeviceExecutionOptions` 新增 `executionMajor?: number`，文档写明它**必须由协商决定、不能读帧自己的声明**——否则宽松合同由被检查方挑选。
2. `device-execution.ts` 的调用点传入它：`validateExecuteFrame(frame, platform, this.options.executionMajor)`。
3. **生产路径安全优先的默认值**：`validateExecuteFrame` 的 `agreedMajor` 缺省为 **v2**（不是"问帧"）。我最初的实现让缺省回退到帧声称的 major，**又被自己写的测试抓住**——那等于把"帧自选规则"的缺陷以默认值形式放回来。已改为缺省 v2。
4. 合同套件 **25/25 通过**，实测：v3 帧 + `agreedMajor=3` → `[]`；缺省 → `protocol_major must be 2`；major 9 → 点名拒绝；v3 缺 operations → 点名缺失。

### 未完成：`DeviceExecution` 层面的接线

我为它写了端到端用例（`executionMajor: 3` 时 v3 帧不应因合同被拒），**但该用例失败**，且我**未能定性**：

- 已排除的：`executionMajor` 确实到达校验器（探针在构造实例上读到 `3`）；`contractForMajor(3)` 可解析；直接调用 `validateExecuteFrame(v3Sample, 'posix', 3)` 返回 `[]`。
- 现象：该调用仍返回 `invalid_request`，消息为 `protocol_major must be 2`——即**选项与校验器之间的某处**发现帧的 major 与所选合同不符，最可能是"到达校验器的帧不是本用例构造的那个"。

该用例**标记为 skip 并把失败写在注释里**。理由：上一轮的教训说明"留下无归因的红测试"代价很高，而**断言当前的错误答案会恰好掩盖本任务要消除的那个缺口**。

### 我在测试文件里犯的两处错误（都被后续验证抓到）

1. **读错字段**：`DeviceExecution.execute` 返回的是 `ExecutionResultFrame`（`error_code`/`error_message`），不是 `CommandResult`（`code`/`message`）。我按后者断言，于是 `result.code` 恒为 `undefined`，得到"实际 undefined、期望 invalid_request"这种误导性输出。
2. **编辑造成重复测试定义**：一度出现两条同名用例（805 与 817 行），Node 执行两者，其中一个因共享状态失败。表现为"单独跑通过、套件内失败"，我因此一度误判为顺序依赖——**与 A20 的既有 flakiness 混淆了**。

这两条都说明：**同名/同型失败在套件输出里长得一样，必须核对"失败的是哪一条、字段是什么"，而不是从现象直接推断成因。**

### 验证

| 套件 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_execution_contract.cjs` | **25/25 通过** |
| `test_desktop_device_execution.cjs` | 31 项 / **30 通过 / 1 skip**（见上） |
| `test_desktop_execution_wiring.cjs` | 2/2 |
| `test_desktop_execution_journal.cjs` | 20/20 |

### 3.5 剩余

1. 定性 `DeviceExecution.execute` 的帧路径（为什么传入协商 major 后仍得到 `protocol_major must be 2`），然后解除那条 skip。
2. `device-client.ts:310` 与 `local-read-assembly.ts:762` 的硬编码 major。
3. 之后回到 3.4 的写前阻断。
## 找到并修掉第二处帧校验：`CommandRunner.admit`（任务 3.5，本轮）

### 我被"查错文件"困了两轮

上一轮我把那条端到端用例标为 skip，理由是"选项到了校验器却仍被拒"。本轮查明：**问题根本不在 `DeviceExecution`，而在另一处校验点**。

```
dist/main/project-execution/command-runner.js:123
  validateExecuteFrame(frame, this.platformFor(frame))    ← 缺协商 major，缺省 v2
```

**帧被校验两次**：`DeviceExecution.execute` 一次（我上一轮接好了），`CommandRunner.admit` 再一次（完全没接）。于是现象是"调用方接受了、流水线中间又拒了"，而我一直只读 `device-execution.ts`。

### 修法

1. `CommandRunnerOptions` 新增 `executionMajor?: number`，文档写明**为什么它也必须知道**：runner 会二次校验，不告诉它就会在调用方已接受之后拒绝，表现为"流水线中段报帧格式错误"。
2. `admit` 传入它。
3. `device-execution.ts` 构造 runner 时传入同一个值（**一处协商、两处使用**）。

### 结果

| 套件 | 本轮之前 | 本轮之后 |
|---|---|---|
| `test_desktop_device_execution.cjs` | 30 通过 / **1 skip** | **31/31 通过** |
| `test_desktop_command_runner.cjs` | 30 通过 / 1 失败（A20） | **31/31 通过** |
| `test_desktop_execution_contract.cjs` | 25/25 | 25/25 |
| `test_desktop_execution_wiring.cjs` | 2/2 | 2/2 |
| `test_desktop_execution_journal.cjs` | 20/20 | 20/20 |
| `test_desktop_skill_cache.cjs` | — | 15/15 |
| `test_desktop_device_artifacts.cjs` | — | 16/16 |

**A20 这次也通过了**——它是顺序不稳定的（此前隔离通过、套件内失败），本轮套件内通过，进一步印证它不是产品缺陷。

那条 skip 已解除，注释改为记录**这里曾是失败点**（"调用方的检查接受了、runner 的检查拒绝了，因为只有调用方被告知了协商 major"）。

### 教训（本轮最值得留下的）

前一轮我在 `device-execution.ts` 里反复确认"选项到达了校验器"，那是对的，**但它不是唯一调用点**。真正的定位动作是**在编译产物里搜索该函数的所有调用点**：

```
Select-String -Path dist/main/project-execution/*.js -Pattern 'validateExecuteFrame'
```

一条命令就暴露了第二处。**我前一轮应当先做这个搜索，而不是在单个调用点内部反复深挖。** 记录于此，以免下次重复：怀疑"传参没生效"时，先枚举所有消费该参数的位置。

### 3.5 剩余

1. `device-client.ts:310` 与 `local-read-assembly.ts:762` 的硬编码 major。
2. 之后回到 3.4 的写前阻断（被移除的 4 条测试可原样加回；现在 v3 帧能穿过合同与 runner 两层校验了）。
## 3.5 收尾 + 3.4 写前阻断的测试加回（本轮）

### 3.5：剥掉最后两处硬编码 major

| 位置 | 之前 | 现在 |
|---|---|---|
| `device-client.ts:310` | `frame.protocol_major === PROJECT_EXECUTION_PROTOCOL.major`（只认 2） | `SUPPORTED_EXECUTION_MAJORS.includes(...)` |
| `local-read-assembly.ts:762` | 拒绝帧硬编码 `protocol_major: 2` | 回显 `Number(frame.protocol_major) \|\| 2` |

第一处的理由值得记：只认 v2 会把 v3 帧**静默丢在地上**——最坏的结果，因为设备看起来连着而什么都不执行。第二处：拒绝是**对那个帧**的答复，用 v2 结果帧回 v3 请求会让对端无法匹配。

### 3.4：4 条写前阻断测试已加回，**35/35 通过**

前几轮被移除的 4 条测试现已可运行：v3 帧现在能穿过**两层**校验（`DeviceExecution.execute` 与 `CommandRunner.admit`），才轮到 `admitChange`。

### 本轮我又踩了同一个坑（值得记下）

加回测试后 3 条失败，我一度怀疑 runner 的接线。实际原因是**测试自己的 `build()` 没有解构也没有转发 `executionMajor`**——我加了参数到 v3 用例，却没让辅助函数把它传下去。定位靠一行探针打印 `h.runner.options.executionMajor` 得到 `undefined`。

**这与上一轮"查错文件"是同一类错误的两面**：上一轮我在错误的文件里找对的参数，这一轮我在正确的文件里忘了转发参数。两者的共同解法是同一条：**打印那个值在运行时到底是什么**，而不是读代码推断。

### 验证

| 套件 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_command_runner.cjs` | **35/35 通过**（含加回的 4 条写前阻断） |
| `test_desktop_device_execution.cjs` | **31/31 通过** |
| `test_desktop_execution_contract.cjs` | **25/25 通过** |
| `test_desktop_execution_journal.cjs` | **20/20 通过** |
| `test_desktop_execution_wiring.cjs` | **2/2 通过** |

### 3.4 验收条款对照（**尚未勾选**）

| 条款 | 状态 |
|---|---|
| journal 接入有限 helper、复用同根串行与命令摘要 | **已覆盖**（任务 2.x 的 helper + 既有 journal 套件 20 项） |
| 新版 Windows 采用 `expected_version` 写前阻断 | **已实现并测试**（`admitChange` 接缝 + 4 条测试，含 `h.started === []`） |
| 不复用旧版"告警仍执行"语义 | **已落实**：v3 携带版本 → 事前拒绝；v1/v2 与 POSIX 保持旧语义（各有测试） |
| 强杀 / 回执丢失 / 合同升级 / 同 ID 改工具或参数 / 未知结果不自动重跑 | **未逐条核对**：journal 套件覆盖了其中一部分（如"崩溃在 start 与 receipt 之间 → `outcome_unknown` 且不重试"），但我**没有逐条对照**，故不能声称完成 |
| 旧 journal 兼容 | **未逐条核对** |

**故 3.4 不勾选**：产品面已完成，但 3.4 点名的两类测试核对与旧 journal 兼容尚未做，而"未逐条核对"正是不能声称覆盖的理由。
## 3.4 点名测试逐条核对 + 发现并修掉 journal 的**版本盲读**（本轮）

### 逐条核对：多数项目**已被既有用例覆盖**

| 3.4 点名项 | 覆盖它的既有用例 |
|---|---|
| 强杀 | `A15: a crash after the start intent leaves outcome_unknown, never a silent re-run`；`A18: a cancel resolves only after the process ended`；`a run that dies mid-way records outcome_unknown` |
| 回执丢失 | `A14: a redelivery before the receipt is durable returns the stored result and does not re-run`；`A14: a second delivery while the first is still running is not started alongside it`；journal 的 `a completion is reported from the receipt, not re-run` |
| 合同升级 / 同 ID 改工具或参数 | `A16: the same command id with different arguments is refused, and the first payload is untouched`；journal 的 `a redelivery with a different payload is refused…` |
| 未知结果不自动重跑 | `A15` 与 journal 的 `a crash between start and receipt becomes outcome_unknown, and is not retried` |

所以**不为它们重复写测试**——重复的测试是第二处会出错的地方，而指针足够。核对动作本身是本轮的产出：此前这三项只是"可能覆盖"，现在是"由哪一条覆盖"。

### 真正缺的一项，且查出一个**真实缺陷**

「旧 journal 兼容」此前无覆盖。查实现时发现：**`version` 被写入，但读取时从不校验**（`loadFromDisk` 只处理解析失败）。

后果有两层，第二层更严重：

1. 未来版本的文件会被当作当前版本**读入**，字段按旧含义解析；
2. **下一次 `persist` 只写本构建认识的字段**——于是旧构建静默丢掉它不认识的字段。**一个丢掉 start intent 的 journal，正是"重复副作用"得以发生的那种状态**，而这恰是这个文件存在的唯一理由。

**修法**（与"不可读文件"同一处置，理由也相同——"我读不了"的安全答案是不猜格式）：

- `version` 存在且不等于 `FILE_VERSION` → **隔离到 `<file>.version-<n>` 并 fail closed**（`degraded`），字节保留给操作者，隔离名里带版本号以便诊断；
- **`version` 缺失则接受**——version 1 一直都会写它，所以缺版本只可能是**更旧**的格式，拒绝它会白白困住一个刚升级的设备；
- 当前版本正常读取。

### 新增 3 条测试（journal 套件 20 → **23 项**）

| 用例 | 钉住的属性 |
|---|---|
| 缺 version 仍可读 | 旧格式不被误判为未来格式；条目可 `lookup` 到 |
| 当前版本正常 | 重开后 `version === 1` 且条目可查到 |
| 未来版本被隔离且 fail closed | `degraded` 为真、理由含 `version 99`、原文件已移走、隔离文件存在 |

**实施中修正两处测试细节**：`begin(record, scope)` 需要 `scopeOf(rec)` 与单参数 scope（我最初写了 `{origin, user_id, …}` 字面量），以及 `lookup` 的 `kind` 断言值。都由套件直接反馈。

### 验证

| 套件 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_execution_journal.cjs` | **23/23 通过**（20 → +3） |
| `test_desktop_device_execution.cjs` | **31/31 通过** |
| `test_desktop_execution_contract.cjs` | **25/25 通过** |
| `test_desktop_command_runner.cjs` | 35 项 / **1 失败 = A20 的顺序不稳定**（重跑即通过，非产品缺陷） |

### 3.4 是否可勾选

产品面与点名测试的**核对**现已完成：4 项由既有用例明确覆盖（上表可逐条追溯），第 5 项「旧 journal 兼容」本轮补齐并**顺带修掉一个真实缺陷**。

但我不在本轮勾选，原因是：**新增的版本检查是"本轮才发现并加入"的行为，尚未经过一次完整的仓库级回归**（我只跑了四个 JS 套件与类型检查）。下一轮跑一遍更广的回归后再勾，避免把一个刚加入的 fail-closed 路径直接标为完成。
## 任务 3.4 **完成**（本轮跑完更广回归后勾选）

上一轮加入 journal 版本检查后，我没有立即勾选，理由是"刚加入的 fail-closed 路径还没经过一次更广回归"。本轮补上。

### 回归范围与结果（**零失败**）

| 套件 | 结果 |
|---|---|
| `test_desktop_device_client.cjs` | **22/22**（我改过它的 major 判定，最关键的一条） |
| `test_desktop_local_selection.cjs` | **29/29** |
| `test_desktop_project_execution_grant.cjs` | **10/10** |
| `test_desktop_isolated_preview.cjs` | **27/27** |
| `test_desktop_project_native_actions.cjs` | **40/40** |
| `test_desktop_project_refresh.cjs` | **22/22** |
| `test_desktop_project_source.cjs` | 37 项 / 20 通过 / **0 失败**（其余为平台 skip） |
| `test_desktop_project_watch.cjs` | 28 项 / 6 通过 / **0 失败**（其余为平台 skip） |
| `test_desktop_local_executor_endpoint.cjs` | 19 项 / 16 通过 / **0 失败** |
| `test_desktop_skill_package_digest.cjs` | **10/10** |
| `test_desktop_skill_transfer.cjs` | **13/13** |
| `test_desktop_local_dir_crypto.cjs` | **7/7** |
| `test_desktop_command_runner.cjs` | **35/35**（含 4 条写前阻断；A20 偶发顺序失败，重跑即过） |
| `test_desktop_device_execution.cjs` | **31/31** |
| `test_desktop_execution_contract.cjs` | **25/25** |
| `test_desktop_execution_journal.cjs` | **23/23**（含 3 条版本用例） |
| `test_desktop_execution_wiring.cjs` | **2/2** |
| `test_desktop_skill_cache.cjs` | **15/15** |
| `test_desktop_device_artifacts.cjs` | **16/16** |
| Python：operations + broker_service + v2/v3 合同 + capability_v3 | **230/230** |

`npx tsc --noEmit -p tsconfig.main.json` → **0 错误**。

### 3.4 验收条款终核

| 条款 | 依据 |
|---|---|
| journal 接入有限 helper、复用同根串行与命令摘要 | 任务 2.x 的 helper；journal 套件 23 项；`A20: two aliases of one real root serialise effectful commands` |
| 新版 Windows 采用 `expected_version` 写前阻断 | `admitChange` 接缝 + 4 条测试（含 `h.started === []` 证明工具未运行） |
| 不复用旧版"告警仍执行"语义 | v3 携带版本 → 事前拒绝；v1/v2 与 POSIX 保持旧语义，各有测试 |
| 强杀证明不重复写 | `A15`、`A18: a cancel resolves only after the process ended`、`a run that dies mid-way records outcome_unknown` |
| 回执丢失证明不重复写 | `A14` 两条、journal 的 `a completion is reported from the receipt, not re-run` |
| 合同升级 / 同 ID 改工具或参数 | `A16: the same command id with different arguments is refused, and the first payload is untouched`；journal 的 `a redelivery with a different payload is refused…` |
| 未知结果不自动重跑 | `A15`；journal 的 `a crash between start and receipt becomes outcome_unknown, and is not retried` |
| 旧 journal 兼容 | 本轮新增 3 条：缺 version 可读、当前版本正常、未来版本隔离并 fail closed |

**其中"旧 journal 兼容"一项在本轮之前无任何覆盖，且查证时发现 `version` 只写不读**——旧构建会静默丢掉不认识的字段，而丢掉 start intent 正是重复副作用得以发生的状态。修法与"不可读文件"同一处置（隔离 + fail closed + 保留字节）。

### 3.5 / 后续

3.5 的实现性工作（装置侧按 major 选合同的三个层面）已在 3.4 的连带工作中完成大半：`contractForMajor`、`negotiateProjectExecution` 接受 major 3、两处校验点按协商 major、`device-client` 与 `local-read-assembly` 的硬编码已除。3.5 剩下的主要是**在合成测试环境验证开关/平台证据/协商/授权组合并补兼容部署说明**，以及确认"不为测试放宽旧 v2 门禁或提前验收 Windows"这条边界。
## 3.5：修掉 v3 下的**平台准入 ≠ 工具可用**缺口（本轮）

### 发现

v3 合同的两张表在 win32 上不一致：

```
v3 PLATFORM_SUPPORT = {"posix":true,"win32":true}
v3 PLATFORM_TOOLS   = {"posix":[...,"bash","run_script"], "win32":["read","write","edit","ls","search_files"]}
```

而 `validateFrameAgainst` 的平台检查是：

```ts
if (!mod.PLATFORM_SUPPORT[platform]) { problems.push(...) }
```

**v3 下 win32 为 true，于是整个平台检查被跳过**，`PLATFORM_TOOLS.win32` 从未被查询。后果：一条 v3 帧可以在 Windows 上命名 `bash` 并通过校验——正是合同自己刻意排除的组合（`launcher_kinds.scripts` 在 win32 为空）。

这与本 change 反复出现的形状相同：**两张表都正确，缺的是问第二张。**

### 修法

平台准入之后**再问工具**：

```ts
const offered = mod.PLATFORM_TOOLS[platform]
if (Array.isArray(offered) && !offered.includes(tool)) { problems.push(...) }
```

对 v2 是 no-op（v2 没有逐平台工具表，`Array.isArray(undefined)` 为假），所以**v2 的答案连消息文本都不变**。

### 实测矩阵（直接调用编译产物）

| 输入 | 结果 |
|---|---|
| v3 文件工具 @ win32 | `[]` **通过**（这是 v3 在 Windows 上真正提供的东西） |
| v3 `bash` @ win32 | `["tool 'bash' is unsupported on platform 'win32'"]` |
| v2 `bash` @ win32 | 同一条消息（**不变**） |

### 新增测试（合同套件 25 → **26 项**）

`v3 admits win32 as a platform but not every tool on it` 把上面三行全部钉住，含"v2 连消息文本都不变"。

### 验证

| 套件 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_execution_contract.cjs` | **26/26 通过** |
| `test_desktop_device_execution.cjs` | **31/31 通过** |
| `test_desktop_device_client.cjs` | **22/22 通过** |
| `test_desktop_command_runner.cjs` | 35 项 / 1 失败 = A20 顺序不稳定（重跑无失败） |

### 3.5 剩余

`test_desktop_execution_capability_v3.py`（28 项）已覆盖开关优先级、平台准入、逐语言验证、helper 存在、POSIX 不变、按 major 选块——**能力层不必补**。3.5 还差的是"补兼容部署说明"与守住"不为测试放宽旧 v2 门禁、不提前验收 Windows"这条边界，属文档与断言层面，下一轮做。
## 3.5：兼容部署说明与边界（本轮）

新增 `evidence/execution-compatibility-deployment.md`，把 3.5 要求的"验证组合 + 补兼容部署说明 + 守边界"落成一份可核对的文件。

### 文件的核心是把三个被混同的概念分开

| 概念 | 由谁决定 |
|---|---|
| **合同是否准入该平台** | `contracts/desktop/v{2,3}.json` 的 `platforms` |
| **本部署是否接受该平台/表面** | `desktop_project_execution_platforms` 等设置（缺省全空） |
| **本构建是否具备该组件** | 装配了哪个 helper、协商到哪个 major |

**合同准入 ≠ 本部署接受**：v3 准入 Windows 是"协议上承认该平台"，不是"这台机器上 Windows 写文件已可用"。这正是此前把 42 项 broker 失败误读为"平台 N/A"的同一处混淆。

其余各节：协议 major 的选择与"帧不能自选规则"、平台×工具矩阵、开关与接受态的**八级优先级**（含 helper 为何排最后、以及"空 vs 缺省"必须区分）、Windows 部署方该做什么、以及 3.5 明确要求的四条边界。

### 明确写入的边界（3.5 的后半句）

- **不为让测试通过而放宽旧 v2 门禁**——v2 在 win32 整体拒绝的答案与消息文本逐字不变（有测试钉住）；
- **不提前验收 Windows**——合同准入是协议事实，`files_write_verified`/`scripts_verified` 仍须真机证据；
- **不在未协商时执行**——不回退到 v1 只读操作；
- **不让帧自选规则**；
- **不把读写合成一个入口**——只读 helper 里没有写操作，因为那些操作不在该构建中。

### 验证（含文档断言的核对）

文档 §7 把每条结论映射到具体测试，且**逐条跑过**：

| 套件 | 结果 |
|---|---|
| `test_desktop_execution_contract.cjs` | **26/26 通过** |
| `test_desktop_guard_binary_roles.cjs` | **9/9 通过** |
| `test_desktop_execution_capability_v3.py` + `test_desktop_execution_v2_contract.py` | **90/90 通过** |

文档 §8 另列三项**不得读作已支持**：Windows 真机与真包未验收、POSIX 运行级回归未做、`device-client`/`local-read-assembly` 的 major 处理未经真机连接验证。

### 3.5 仍未勾选的原因

3.5 的实现性工作（装置侧按 major 选合同、两处校验点、硬编码消除、平台×工具矩阵）**已完成并有测试**；本轮补上了兼容部署说明与边界。但我**没有逐条对照 3.5 原文**确认无遗漏，且本轮的文档是新增产物、尚未经一次完整仓库级回归——按上一轮 3.4 的同样理由，下轮核对后再勾。
## 3.5 逐条核对与交接（本轮，第 40 轮）

### 3.5 原文逐条

| 条款 | 状态 | 依据 |
|---|---|---|
| 在合成测试环境验证现有开关/平台证据/协商/授权组合 | **完成** | `capability_v3.py` 28 项（开关优先级、平台准入、逐语言验证、helper、POSIX 不变、按 major 选块）+ 合同套件 26 项 |
| 补兼容部署说明 | **完成** | 新增 `evidence/execution-compatibility-deployment.md` |
| 文件关闭时新脚本不可用 | **完成** | `test_windows_scripts_stay_closed_however_the_lists_are_set`、`test_the_script_surface_needs_its_own_switch` |
| **在途脚本按 D6 实际安全依赖处理** | **未实现** | 属脚本侧，是第二阶段（7.x/8.x）的工作；本文件不声称覆盖 |
| 不增加无效状态组合 | **完成** | `test_a_posix_script_surface_needs_no_per_language_list`、`test_a_script_acceptance_without_any_verified_language` 等 |
| 不为测试放宽旧 v2 门禁或提前验收 Windows | **完成** | `test_a_v2_peer_still_gets_v2s_refusal`；本变更另有多条钉住 v2 消息文本逐字不变 |

**3.5 不勾选**：第四项明确未实现。把它留空是这份记录的全部意义——一个含"在途脚本"字样的任务在脚本层未动的情况下被勾选，会让后续读者以为第二阶段已开始。

### 最终验证（第 40 轮）

| 套件 | 结果 |
|---|---|
| Python：operations + broker_service + v3/v2 合同 + capability_v3 + **broker 端点全套** + refusal_pair | **289/289 通过** |
| `test_desktop_execution_contract.cjs` | 26/26 |
| `test_desktop_device_execution.cjs` | 31/31 |
| `test_desktop_execution_journal.cjs` | 23/23 |
| `test_desktop_command_runner.cjs` | 35/35（A20 偶发顺序失败） |
| `test_desktop_device_client.cjs` | 22/22 |
| `npx tsc --noEmit -p tsconfig.main.json` | 0 错误 |

**其中 `test_desktop_execution_broker`（47 项）在本机全绿**——这正是本次会话开始时被记为"平台 N/A、需 POSIX 主机"的那一套。

### 本次会话（轮次 4–40）的净产出

**任务：3.1、3.2、3.3、3.4 完成；3.5 除"在途脚本"外完成。进度 8/37。**

**四项产品缺陷**（都是"每一条 v3 命令都会失败"级别）：

1. `commands.py` 的 `_enqueue_command` 从不写 `operations_json`/`runtime`，v3 无法入队；
2. `access.py` 的 `verify_binding_scope` 不把 grant 的 operations 并入 `ctx.binding`，v3 一律 `permission_denied`；
3. broker 重建摘要时漏传 `operations`/`runtime` **且**用 v2 字段表，v3 一律 `command_conflict`；
4. `_check_runtime` 未按 major 收窄，**拒绝了全部 v2 命令**（兼容性破坏，伪装成新检查）。

**两项测试基建缺陷**（掩盖了上述全部）：

5. 14 个 `tests/*.cjs` 的 `execFileSync('npm', …)` 缺 shell，在 Windows 上**整个套件无法运行**；
6. `_clear_quota` 只清上限不清用量，共享 harness 下配额跨测试泄漏。

**一项静默损坏**：journal 的 `version` 只写不读，旧构建会丢掉不认识的字段——包括 start intent。

### 交接

- **3.5 的在途脚本**与 **7.x/8.x（脚本层）** 是同一件事，应在同一批里做。
- **4.1–4.5、5.1、6.3** 未开始；**5.2–5.4、6.1–6.2、8.x** 需真机/真包/签名凭据，按目标约束不勾选。
- 本机 broker 端点测试现已可用（`BrokerFixture.setUp` 内对平台原因做了**窄替换**：只在原因恰为 `platform_unsupported` 时替换，声明/开关/接受态/helper 仍真实生效）。后续改动写入路径时可用它做端到端回归。
## 1.3 的缺口：新增 W01–W16 证据索引（本轮）

任务 1.3 要求"建立 W01–W16 证据索引及平台/路径支持说明"。查证发现**索引此前不存在**：`fs-guard-platform-support.md` 覆盖了平台与路径，但没有把 16 个验收项逐条映射到实际证据。

新增 `evidence/w01-w16-evidence-index.md`，每一行给出**当前实际拥有的证据 / 落在哪个文件 / 还差什么**，并把"未执行"如实写出。

### 索引读出来的真实分布

| 状态 | 项 |
|---|---|
| **本机已过（helper 或合成级）** | W02、W03、W04（Windows 侧）、W05、W07、W08、W11、W12、W15 |
| **部分**（本机覆盖了其中一层） | W01（helper 层过了，UI 快速A/B 未做）、W06（错误表已写，**磁盘满/强杀未做故障注入**）、W13（alias 串行已过，重启未做） |
| **未执行** | W09、W10、W14、W16 |

### 由此暴露的两个**真实未做项**（此前未单独记录）

1. **W05 的"同 mtime/size 不同内容"**未单列用例。版本绑定含对象身份，理论上覆盖，但**未实测该构造**。
2. **W06 的磁盘满与 helper 强杀未做故障注入**：`io_error` 的"不确定"分支目前只有结构保证与 `publish_is_ambiguous` 的分类单测，**没有注入证据**。这在 `fs-guard-error-recovery.md` §5 已提过，索引把它归到了具体验收项上。

### 顺带核实的 1.3 后半句

"缺符号链接权限时配置专用测试环境，不将跳过计为通过"——逐一核对后：夹具的 `Sandbox::symlink` 在 Windows 上改用 **junction**（普通用户可建），源码注释明写这是为了让测试"tests real on Windows instead of skipping them"；唯一需要 `/dev/null` 的一项标为 `#[cfg(unix)]` 且注释说明"not counted as a Windows pass"。**因此当前没有需要专用环境的用例，也没有任何用例靠 skip 计为通过。**

### 环境约束（实测，非推断）

| 约束 | 实测 |
|---|---|
| 本机 OS | **Windows 11**（10.0.26200），**非** Win10 22H2 |
| 管理员 | **无** |
| 签名凭据 | **无**（`CSC_*`/`SIGN*`/`CERT*` 为 0 个） |
| NSIS | **未安装** |
| POSIX 主机 | **无**——`wsl` 可执行文件在，但**未安装任何发行版**（需管理员+重启），故 POSIX 运行级回归不可行 |

### 1.3 仍未勾选

索引已建立、平台/路径支持说明已有、原生 API 封装与威胁模型已记录、专用环境问题已核实并说明。但 1.1/1.2（Windows 根句柄原型与句柄原子替换探针的**真机**部分）仍未完成，1.3 的"证据索引"本身虽已就位，其内容大量指向"未执行"的项——**勾选它等于宣称 1.x 批次完成，而 1.1/1.2 未完成**。故不勾选。
## 4.1 起步 + 构建缺陷的真实规模（本轮）

### `npx` 形态的构建缺陷：**27 个文件**，上轮只修了 `npm` 形态的 9 个

上轮我发现 `execFileSync('npm', …)` 缺 shell 导致 `spawnSync npm ENOENT`，修了 9 个文件。本轮在 `test_desktop_project_execution_grant.cjs` 上再次遇到，但形态是 **`execFileSync('npx', ['tsc', …], { stdio: 'pipe' })`**——**同一个缺陷的第二种写法**，我上轮的搜索只覆盖了 `npm`。

一次性修掉 **27 个文件**（脚本只插入 `shell: process.platform === 'win32',`，不触碰其它内容；改后 `git diff -U0` 抽查确认仅新增该片段）。

### 由此得到一张**本机 desktop JS 套件的真实失败表**

修复后跑全部 `tests/test_desktop*.cjs`（脚本汇总 pass/fail），**只有 6 个套件有失败**：

| 套件 | fail | 原因 |
|---|---|---|
| `test_desktop_consent_browser.cjs` | 1（pass=0） | `Cannot find module 'playwright'` |
| `test_desktop_login_gate_browser.cjs` | 1（pass=0） | 同上（计为 1 是因为它在单条用例内做断言） |
| `test_desktop_sap_login_memory.cjs` | 1（pass=0） | 同上 |
| `test_desktop_sap_page_read.cjs` | 1（pass=0） | 同上 |
| `test_desktop_remote_config.cjs` | 1 | `config must not be world-readable`（POSIX 权限断言） |
| `test_desktop_downloads.cjs` | 1 | 严格相等断言，待查 |

### 基线核对（干净树 `git stash -u` 后重跑）

| 套件 | 基线 | 修复后 |
|---|---|---|
| `consent_browser` / `login_gate_browser` / `sap_login_memory` / `sap_page_read` | fail=1 | fail=1 **不变**（playwright 缺失） |
| `downloads` | fail=1 | fail=1 **不变** |
| **`remote_config`** | **pass=0 / fail=44** | **pass=43 / fail=1** |

**结论**：6 个失败**全部先于本轮存在**；而 `npx` 修复**净改善**了 `remote_config`——它此前有 **44 项因构建失败而全部报错**，现在 43 项通过，只剩 1 项真实失败。

这条值得记：**同一缺陷的多种写法会让"修复"看起来已完成**。上轮修 `npm` 后 14 个套件转绿，我据此认为该类缺陷已清；实际还有 27 个文件用的是 `npx` 写法。本轮的搜索覆盖了两种写法。

### 4.1 的实现：grant 的 operations 集合

**缺口**：`allowsProjectExecution()` 用**一个布尔值**表达两种能力，而 v3 把它们分成 `files`（五个文件工具）与 `scripts`（两个脚本工具）两个名字。这正是 4.1"明确只读输入与项目读写的 operations 集合"所指。

`local-files/grants.ts` 新增：

1. `operationsFor(purpose)` —— `readonly-input` → **`[]`**（空集，不是"无限制"：合同把缺省/空读作"未授权任何新操作"）；`project-execution` → **`['files']`**。
2. `authorizesOperation(grant, name)` —— 按名字查询；**`operations` 存在时以它为准**，不从 purpose 重新推导（否则一个被确认得更窄的集合会被"推导"拓宽，正是 v3 要防的静默升级）。
3. `GrantPublic.operations?` 可选字段，缺省即"从 purpose 推导"，故全部既有记录继续工作。

**`scripts` 刻意不在两者之中**，包括 `project-execution`：在用户目录里运行代码是**单独的确认**（合同的 `requires_extra_confirmation`），在这里凭空发放等于交出唯一一个其设计目的就是"保持独立"的能力。

新增 4 条测试（grant 套件 10 → **14 项**，14/14 通过）：只读授权不含任何 v3 操作、`project-execution` 含 `files` 但**不含 `scripts`**、缺省从 purpose 推导、**记录集合优先于 purpose**。

### 验证

| 检查 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_project_execution_grant.cjs` | **14/14 通过** |
| 全部 desktop `.cjs` | 6 个既有失败（playwright×4 + 权限/下载各 1），**无本轮引入** |

### 4.1 仍未勾选

4.1 还要求"复用原生选择/绑定流程"已由既有 `local_root.py`/`host-bridge` 承担，"测试仅选择不等于就绪、取消/快速切换/迟到回调不误绑定"需逐条核对现有覆盖。本轮只完成了 **operations 集合**这一明确缺口，故不勾选。
## 4.1 **完成**（本轮）

### 逐条对照

| 4.1 条款 | 状态 | 依据 |
|---|---|---|
| 复用原生选择/绑定流程 | **既有** | `integrations/desktop/local_root.py`、`desktop/src/main/local-execution/root-registration.ts`、`host-bridge` 的 `chooseWorkspace` |
| 明确只读输入与项目读写的 operations 集合 | **本轮实现** | `local-files/grants.ts` 的 `operationsFor` / `authorizesOperation`；`contract.ts` 的 `requiredOperationForTool` / `coversTool` |
| 说明项目内容（含 `.env`）可能进入模型 | **既有** | `PROJECT_EXECUTION_PICKER_MESSAGE`（含"原地执行"提示）；`evidence/fs-guard-platform-support.md` 的威胁模型 |
| 宿主控制数据重叠则拒绝授权 | **既有** | `ExecutionTarget.__post_init__` 的校验；`local_root.py` 的 `_project_mode` 对未知取值抛错（不强制转换） |
| 仅选择不等于就绪 | **既有** | `local_selection.cjs` 的 `a selection walks idle -> selecting -> ready and commits exactly once`；`a commit that refuses (invalid scope) fails without inventing a selection` |
| 取消不误绑定 | **既有** | `cancelling reports cancelled and leaves the previous selection in effect`、`cancelling the very first attempt leaves nothing in effect`、`an empty path list or a null result counts as a cancel` |
| 快速切换不误绑定 | **既有** | `a new attempt during preparation supersedes it and releases its grant`、`every attempt bumps the generation, including cancelled ones` |
| 迟到回调不误绑定 | **既有** | `an older attempt resolving after a newer one started is discarded`、`a superseded attempt does not overwrite the newer attempt state`、`a superseded attempt does not run validation or commit` |
| 旧只读授权不可写 | **本轮接通** | 执行路径改按**操作名**判定：只读 → 空集 → `authorizesOperation` 为假 |
| 旧只读授权不可执行脚本 | **本轮接通** | 同上，且 `scripts` 不在 `project-execution` 的集合中 |

### 本轮的实现：把"用途"换成"操作"，并按**帧自己的工具**判定

三处改动：

1. `contract.ts` 新增 `requiredOperationForTool(tool)` 与 `coversTool(operations, tool)`，映射**取自合同的 `required_for` 表**（生成产物里已有 `TOOL_REQUIRED_OPERATION`），不另抄一份，因此不会与两端共同校验的文档漂移。
2. `local-read-assembly.ts` 的 `executableRootFor(workspaceId, operation = 'files')` 从 `grant.purpose !== 'project-execution'` 改为 `!authorizesOperation(grant, operation)`。缺省参数保证既有调用方行为不变。
3. `runExecution` 改为**按帧携带的工具**推导所需操作：

```ts
const needed = requiredOperationForTool(frame.tool)
if (!grant || !needed || !authorizesOperation(grant, needed) || …)
```

**这一处是本轮最实的改动**：此前`runExecution` 只问"是不是项目执行授权"，因此一个 `files` 授权也能提交 `bash`/`run_script` 帧——`files` 与 `scripts` 分成两个名字的全部意义正在于此。现在同一个授权对**不同工具给出不同答案**。

### 新增测试（grant 套件 10 → **16 项**，16/16 通过）

| 用例 | 钉住的属性 |
|---|---|
| 只读授权不含任何 v3 操作 | 空集 ≠ 无限制 |
| `project-execution` 含 `files`、**不含 `scripts`** | 「为编辑打开项目」不等于「在其中运行代码」 |
| 缺省从 purpose 推导 | 既有无 `operations` 字段的记录继续工作 |
| **记录集合优先于 purpose** | 阻止"推导式拓宽"这一静默升级 |
| 工具→操作映射来自合同 | 7 个工具逐一断言，且未知工具覆盖为假 |
| **仅 files 的授权拒绝脚本工具** | 同一授权对 `read` 为真、对 `bash`/`run_script` 为假；缺省集合不是"无限制" |

### 验证

| 检查 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_project_execution_grant.cjs` | **16/16 通过** |
| `test_desktop_local_read.cjs` | **20/20 通过** |
| `test_desktop_local_execution.cjs` | **36/36 通过** |
| `test_desktop_device_execution.cjs` | **31/31 通过** |
| `test_desktop_local_files.cjs` | **12/12 通过** |
| `test_desktop_device_client.cjs` | **22/22 通过** |

### 边界（诚实记录）

- 服务端侧的 `bind_workspace(operations=…)` 已存在（任务 2.x），本轮的 TS 侧 `operations` 字段**尚未接入该上行路径**——即桌面端目前把这个集合算出来了、也用在了本地判定上，但还没有发给服务端存储。这属 4.2"接通统一来源 adapter"的范围，已在下一批。
- `scripts` 目前**没有任何路径可以授予**（这是刻意的，脚本层属第二阶段）；因此 `authorizesOperation(grant, 'scripts')` 在现有流程下恒为假，只有测试通过构造 `operations: ['files','scripts']` 覆盖了真分支。
## 4.2 起步：operations 上行接通（本轮）

### 缺口（上轮记录的边界，本轮补上）

服务端 `bind_workspace(operations=…)`、`desktop_binding_workspaces.operations_json`、broker 的 `_check_operations` **都已存在**（任务 2.x），但桌面端**从未把 operations 发上去**：

```
PUT /api/desktop/bindings/{id}/workspaces/{workspace_id}
  body: { grant_version }          ← 只有版本，operations 缺失
```

于是服务端把 `operations_json` 写成 **NULL**，两端对"用户确认了什么"各持一说——**设备算出 `['files']` 并据此执行，服务端存的是"什么都没授权"**。这正是 v3 要把"为编辑打开项目"与"在其中运行代码"分开所要消除的那种分歧。

### 实现（贯穿两端）

**桌面端**：

1. `LocalContextSetupRequest` 新增 `operations?: string[]`，文档说明它必须与 grant 同行发送——"稍后再授权这些"的独立请求可能与它所属的 grant 分离，而**单独到达的授权是这台设备从未确认过的**。
2. `attachWorkspace(..., operations?)` 在**同一个 PUT** 里发送；空/缺省则维持历史 body 形状（v2 不变）。
3. `remote-host-ipc.ts` 传入 `operations: grant ? operationsFor(grant.purpose) : []`——**从活的 grant 推导，不在此重新决定**：确认文案所依据的事实与记录在服务端的事实必须是同一个。

**服务端**：

4. `DesktopBindingWorkspaceHandler.PUT` 透传 `body.get("operations")`，并在注释里写明**服务端不得代为推导**：它不知道用户被问过什么，凭空给出集合等于代替设备授权。
5. `devices.py` 新增 `_validated_operations(raw)`：**校验后才写入**。该列是 broker 衡量命令的**权威**，客户端若能自由写入就等于能自我授权；未登记的名字意味着一个"没有任何东西知道如何拒绝"的操作。

校验规则（实测）：

| 输入 | 结果 |
|---|---|
| `None` | `[]` → 存 NULL（v2 形状，既有 grant 完全不变） |
| `['files']` | `['files']` |
| `['scripts','files','files']` | `['files','scripts']`（去重并排序，同一集合写两次是同一行） |
| `'files'` / `{'files':1}` / `['rm_rf']` / `[1]` / `['files','rm_rf']` | **`invalid_request` 拒绝**，不强制转换 |

规则**取自合同**（`desktop_contracts_v3.operation_known` / `OPERATIONS["names"]`），不硬编码，故不会与文档漂移。刻意不用 `validate_operations`：那是**帧**校验（拒绝空集），而绑定允许"未确认任何操作"。

### 新增测试

`test_bind_records_only_contract_operation_names` —— 端到端：绑定 `operations=['files']` 后**直接查库**确认列里是 `["files"]`；四种非法输入逐一断言 `invalid_request`。

**实施中修正三处测试细节**（均由套件直接反馈，非产品问题）：类名（方法在 `_DesktopFilesBase`，实际跑在 `BindingAndWorkspaceTests`）、用户名需已登记且 `SESSION_A` 属 `u1`、连接需用 `sqlite3.connect(self.app.db_path)` 而非 `self.app.conn`。

### 验证

| 检查 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| 新测试 `test_bind_records_only_contract_operation_names` | **通过** |
| `test_desktop_execution_broker` | **47/47 通过** |
| `test_desktop_file_access` + `broker_service` + `broker` | 74 项 / 1 失败 = **既有的跨套件顺序不稳定**（单跑 barrier 47/47 通过）；另 `local_root` 的 2 项为 **Windows 路径分隔符**（基线同样 2 项，已用 `git stash -u` 核对） |

### 4.2 仍未勾选

4.2 还要求：统一产物名与候选路径（**3.3 已做** `guardExecutableName`/`guardBinaryCandidates`）、**模型和面板传递 `expected_version`**、**验证五工具始终同一本机项目**、**helper 缺失给修复提示**、**聊天保持**。这些尚未逐条核对，故不勾选。
## 4.2 逐条核对：三项已有、一项**真实缺口**（本轮）

### 已有的三项（核对后不必再做）

| 4.2 条款 | 现状 |
|---|---|
| 统一实际 `.exe` 产物名、开发态与包内路径 | **3.3 已完成**：`guardExecutableName(role, platform)`（win32 加 `.exe`）、`guardBinaryCandidates({role, resourcesPath, appPath, env, platform})`、`resolveGuardBinary` |
| helper 缺失给修复提示 | **已完整**：`remote-container-ipc.ts:652` 抛协商码 `helper_missing`；`workspace.js` 有单一映射表 `WS_LOCAL_REFUSAL_KEYS` + `wsLocalRefusalText(code)`，**三处站点共用**；`i18n/core.js` 三语言齐备（简中/繁中/英文，文案明确"重新安装或重新构建桌面客户端，重连无效"）；`tests/test_workspace_local_refusals.cjs` 覆盖，且断言四码文案互不相同 |
| 聊天保持 | 既有：`remote-container-ipc.ts` 的 `projectBindings` 按聊天记录，`liveLocalContext` 供替换文档恢复；`agentId`/`businessSessionId` 刻意**不**进 binding |

### 一个**被测试推翻的假设**（值得记下来）

我读代码时认为发现了一个真实缺陷：`FileVersions.noteServed()` 在整个生产代码里**没有调用方**（`grep` 命中 0 处外部调用），而 `noteWritten()` 只在**运行成功之后**被调用（`command-runner.ts:469`），陈旧判定却在**运行之前**（:412）。据此推断"一次 `read` 之后从未被记为已交付，后续 `edit` 会误报陈旧"。

**我写了一条测试来证实它**——不手工调用 `noteServed`，先跑 `read` 再跑 `write`，断言不得报 `file_changed`。**测试通过，假设错误。**

真实机制是：`noteWritten` 同时记录 `contents` 与 `modifies`（读与写都算"视图已当前"），而**陈旧判定比较的是上一次运行留下的记录**；没有记录时不报告陈旧（"自交付以来是否变化"，而非"是否见过"）。所以不存在该缺陷。

**这条测试被保留**，理由：其余每一条陈旧测试都**手工** `noteServed`，因此**没有一条能发现"runner 是否自己记录已服务的读取"**。这条是唯一验证该性质的。注释已改为陈述真实机制，不留错误论断。

**教训**：`grep` 未命中调用方**不等于**该行为缺失——内部别名调用（`noteWritten` → `noteServed`）与"无记录即不陈旧"的语义都可能让推断落空。**先写测试证实，再下结论**，这一步本轮省下了一次错误实现。

### 一个**真实缺口**：`expected_version` 没有产生方

4.2 要求"模型和面板传递 `expected_version`"。核对结果：

| 环节 | 现状 |
|---|---|
| 合同定义 | `EXPECTED_VERSION_FIELD`/`EXPECTED_VERSION_STALE_CODE`/`REQUIRED_BY`/`RETURNED_BY` 均已生成 |
| 装置侧**消费** | 已实现：`DeviceExecution.admitChange` 判定"帧是否携带版本"；`command-runner.ts:413` 的写前阻断；测试 4 条 |
| 装置侧**消费**（另一条路径） | `device-ops.ts:258` 的 materialize 读 `params.expected_version` |
| **产生方 / 返回给调用者** | **不存在**。全仓 `expected_version` 的 TS 出现共 12 处，**无一处生成它或把它写进任何结果** |

而版本值目前**只由 Rust 写助手**产生（`src/write.rs` 的 `version_for`，FNV-1a 64 绑定 `grant.id|relpath|stat.identity|size|modified`），TS 侧从未调用或复现它。

后果：**v3 的写前阻断虽然实现并测试完整，在真实使用中不会触发**——没有任何路径把版本交给模型。它需要"读结果携带版本"这一环，而该环尚未实现。面板侧同样：`browser.ts` 的 edit 帧是 v2 形状（`protocol_major: 2`、`arguments: { path, content }`），**没有 `expected_version` 字段**。

**这属 4.2 的范围且是它点名要求的**，因此 4.2 不勾选。下一轮的实现方向：让读路径（`read` 工具结果 / 面板的 `open`）返回与 Rust `version_for` **同构**的版本串，并让模型与面板把它放进 `arguments.expected_version`。

**注意**：这需要 TS 侧复现 Rust 的 FNV-1a 64 配方，**或**改由 helper 在读取时返回版本。前者是两份实现（有漂移风险，而本变更反复出现的正是漂移），**后者更符合"生成只在一处"**——倾向后者，但涉及 helper 协议变更，下一轮先确认再改。

### 验证

| 检查 | 结果 |
|---|---|
| `test_desktop_command_runner.cjs` | **36/36 通过**（含新增的自动记录用例） |
## 4.2 实现：`expected_version` 由 helper 在读取时产生（本轮）

### 设计选择由合同裁定，不是偏好

上轮我提出两条路并"倾向"helper 产生。本轮读到合同 `version_precondition`，它**排除了另一条**：

```
"opaque": true,
"covers": ["workspace_id", "relative_path", "object_identity", "content_version"],
"returned_by": ["read", "write", "edit"],
"required_by": ["edit"],
"conditional_by": {"write": "required when create is false or absent"},
"note_not_checksum": "A client cannot mint an acceptable value from the file it holds;
                      only a prior read through the same authorised root can produce one."
```

**「客户端无法铸造」不是风险提示而是规范条款**——版本绑定 `object_identity`（文件对象身份），而 Node 的 `statSync` 给不出该值。所以 TS 侧复现**在规范上就被排除**，不只是"会漂移"。用户的确认与合同一致。

### 实现：单一实现，两种构建共享

**关键约束**：只读 helper 也要返回版本，而 `version_for` 原本在 `write-ops` 门内。

新建 `src/version.rs`（**不**加 feature 门），承载 `version_for` / `fnv1a64` / `looks_like_version`：

- 原 `write.rs` 的副本删除，改为 `pub use crate::version::version_for` —— 既有调用点（`write.rs` 内 8 处、测试 6 处）**一行未改**；
- `looks_like_version` 保留 `#[cfg(feature = "write-ops")]`：只读构建只**铸造**版本，不**校验**调用方的版本，那个检查器在只读构建里没有调用方；
- `FileStat.identity` 本就未加门（源码注释：read-only build 也填充它，"because both backends return one shape"），因此只读构建**已经具备**铸造版本所需的一切。

`ops::read` 的结果新增 `"version"`：**每个分块都返回**，不只在第一块。理由写在代码里：分块读大文件的调用方必须能从它最后看到的位置开始编辑，让版本取决于"恰好结束在哪一块"是个陷阱；而它描述的是**对象**，所以对象真正变化前该值不变。

### 验证

| 构建 | 命令 | 结果 |
|---|---|---|
| 只读 | `cargo check --release` | **0 错误 0 警告** |
| 只读 | `cargo test --release` | **47 + 27 通过** |
| 写入 | `cargo check --release --features write-ops` | **0 错误 0 警告** |
| 写入 | `cargo test --release --features write-ops` | **73 + 73 + 27 + 5 通过** |
| 版本单测 | 只读 2 项 / 写入 4 项 | **通过** |

版本测试包含 **FNV-1a 64 的公开测试向量**（空串 → 偏移基、`"a"`、`"foobar"`）以及"五个受覆盖事实各自改变 material"的断言。**钉住哈希是必要的**：该值跨两个构建交换，若哈希悄悄不同，每次读的结果都无法被随后的写接受。

**实施中修正三处**（均由编译器直接反馈，非产品问题）：`Grant` 有私有字段（`root`/`identity`），无法在模块外构造，故测试改为直接测 material 与摘要；`looks_like_version` 需加 feature 门；删函数体时留下一个孤立 `}`（`unexpected closing delimiter`）。

### 4.2 剩余（下一轮）

helper **已产生**版本，但链路尚未接通：

1. TS 客户端（`fs-guard` 读取结果）携带 `version` 并透传；
2. `device-ops.ts` 的读操作把它放进工具结果；
3. 模型与面板把它放进 `arguments.expected_version`；
4. 面板的 edit 帧目前是 v2 形状（`protocol_major: 2`、`arguments: {path, content}`），需决定是升为 v3 还是在 v2 下传递。

**故 4.2 仍未勾选**：本轮完成的是"产生方"，"传递"未完成。
## 4.2：TS 链路接通 + 查出**两种 version 语义**（本轮）

### 链路（Rust → TS 客户端 → 工具结果 → 模型）

| 环节 | 改动 |
|---|---|
| Rust helper | 上轮已让 `read` 结果携带 `version` |
| `local-files/fs-guard.ts` | `ReadResult` 新增 `version: string`；`read()` 从 `raw.version` 填充（**此前被丢弃**） |
| `remote/device-ops.ts` | `read_text` 的结果带上 `version`（每块都带） |
| 模型可见性 | **无需改动**：`client_files.py` 的注释写明设备 payload 是逐字透传（`"result": command.get("result")`），故 `version` 自动到达模型 |
| `project-browser/browser.ts` | 面板的 `read()` **已经收到** `page.version`，但返回结果只暴露 `mtime`、把 `version` 丢了——已补上 |

### 查出：存在**两种互不相同的 version 语义**

我一度以为全仓只有一种（`fgv1:<hex>`），并据此改写了 `client_files.py` 中 `expected_version` 的 schema 描述。**核对后证明我改错了**：

| 用途 | 值 | 校验点 |
|---|---|---|
| **materialize**（把本机文件送到服务端） | **`<size>:<modified>`** | `local-files/materialize.ts` 的 `localVersionOf(size, modified)` → `` `${size}:${modified}` `` |
| **项目读写**（v3 `write`/`edit` 的写前阻断） | **`fgv1:<16位十六进制>`**（不透明） | Rust `version_for`（绑定 grant/路径/**对象身份**/size/mtime） |

原描述（`<size>:<modified>`）**对 materialize 是正确的**。我把描述改成 `fgv1:` 是错的，已回退，并改为**如实说明两者并存及为何不同**（前者可由 size+mtime 算出，后者因绑定文件对象而**不能**）。

**教训与本变更反复出现的主题同型**：同名概念在不同子系统里语义不同，而"看起来是同一个字段"正是误判的来源。本轮靠**读校验点**（而非字段名）纠正了自己。

### 验证

| 检查 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_project_source.cjs` + `test_desktop_local_files.cjs` | **49/49 通过** |
| `test_desktop_file_access` + `test_desktop_transfer` + `test_desktop_remote_dispatch` | **122/122 通过** |
| 快照依赖检查 | schema 描述的旧文本在 `tests/` 中**无任何引用**（`Select-String` 0 命中），故无快照需同步 |

### 4.2 剩余（下一轮，含一个待决设计点）

1. **面板的 edit 帧仍不传 `expected_version`**：`browser.ts` 的帧是 v2 形状（`protocol_major: 2`、`arguments: {path, content}`）。现在 `read()` 已给出 `version`，但 `writeFrame` 尚未把它放进 `arguments.expected_version`。
2. **模型侧**：`read_text` 已返回 `version`，但 v3 `write`/`edit`（项目执行工具，非 `client_files`）的调用方是否把它放进去，未核对。
3. **待决**：Rust 写助手要求"每次覆盖都需要版本"（`expected` 只在 `create` 为真时可省略）。面板若不传版本且不声明 `create`，**接线后会变成新的拒绝**——所以第 1 步必须与"面板如何声明 create/覆盖语义"一起决定，而不是单独加字段。

**故 4.2 仍未勾选。**
## 4.2：发现并修掉**面板保存必然失败**的既有缺陷（本轮）

### 缺陷链条（每一环都读过代码，不是推断）

| 环节 | 事实 |
|---|---|
| 合同 | v3 `write` 的 `precondition`：「create=true 要求目标不存在且不需版本；**其他任何 write 都是覆盖，要求 `expected_version` 命名读到的版本**」 |
| Rust | `write.rs:151` 是 `let expected = expected.ok_or(Code::FileChanged)?;`；`main.rs` 的 `create` 缺省 `false` |
| 面板帧 | `browser.ts` 发 `arguments: { path, content }`，**无 `expected_version`** |
| 结论 | **面板的保存必然被拒为 `file_changed`**——既有缺陷，与本轮改动无关，但正是 4.2 要解决的 |

链路核实：面板 `write` → `options.executeTool` → `assembly.runExecution` → `DeviceExecution` → helper worker（`remote-container-ipc.ts:714`）。

### 修法：三处传递 + 一处渲染进程

1. `browser.ts` 的 `write(params)` 读 `params.version`；`editFrame(..., expectedVersion)` 把它放进 **`arguments.expected_version`**（v2 helper 读自己的 `params`），**并同时**放在**帧顶层**（v3 的 `admitChange` 先读信封）——一份帧形状在两种合同下都有效。
2. `host-bridge.ts` 的 `projectWrite` **校验形状**（`/^fgv1:[0-9a-f]{16}$/`）而非盲目放行：它流向一个原生调用并与文件比较，页面不能递给它一个「不是版本的东西」。
3. `project-source.js`（渲染进程）`projectRead` 的结果暴露 `version`；`projectWrite` **条件携带** `version`（用 `Object.assign` 而非 `version: undefined`——后者会让下行开始看到一个**新键**，已有一个既有测试因此失败，是我先犯的错）。
4. `remote-host-ipc.ts` 的 `projectWrite` 把 `params` 整体透传给 `port.write`，因此 `version` 自动到达。

### 验证

| 检查 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_project_source.cjs` + `test_desktop_project_native_actions.cjs` | **78/78 通过** |

新增用例 `a save names the version it was built from, in the frame and in the arguments`：断言 `frame.arguments.expected_version` **与** `frame.expected_version` 都等于传入值（前者给 v2 helper，后者给 v3 `admitChange`）。

**实施中修正一处自己的错误**：我最初写了一条 `assert.equal(x, cond ? x : x)` 的**恒真断言**，以及用 `version: undefined` 引入新键。前者删除，后者改为条件加入。

### 4.2 状态：产品面已闭环，但仍**不勾选**

现在从读（helper 铸造版本）到写（helper 校验版本）的路是通的：

- helper 读结果 → `fs-guard.ts` → `device-ops.ts` → 模型与面板可见
- 面板读 → 渲染进程 → `projectWrite` → `browser.write` → `arguments.expected_version` → helper 校验

**但 4.2 的验收还含一条未核对**：模型侧走的是项目执行工具（v3 `write`/`edit`），其**工具 schema 是否声明 `expected_version` 且调用方是否填充**，本轮只核到 `contracts/desktop/v3.json`（合同层已声明 `optional`/`required`），未核到运行时的工具定义。故不勾选。
## 4.3 摸底 + 4.2 收口判断（本轮）

### 4.2 收口：模型侧的唯一缺口是**工具 schema 未声明** `expected_version`

核对链条：

| 环节 | 事实 |
|---|---|
| 服务端建帧 | `device_execution_frame` 的 `arguments = command["params"]`（模型参数**整体**），不构造顶层 `expected_version` |
| 合同 | v3 `frames.execute_tool.optional` **含 `expected_version`**；v3 `tools` 一节写明 schema "stay the master's" |
| 模型工具 schema | `agent/tools/write/write.py` 与 `edit/edit.py` 只声明 `path`/`content`（`required: [path, content]`），**无 `expected_version`** |

**结论**：模型若把 `expected_version` 放进参数，它会经 `arguments.expected_version` 一路到达 Rust 助手并被校验（助手从自己的 `params` 读它，已在上一轮确认）。**缺的只是 schema 声明**——而"master 的 schema 不改"是合同自己写的，所以这一条不能靠改 `write.py` 满足，需在设计上决定 v3 如何给模型一个可传的版本参数。

**这属于 4.3**（"工具派发按项目和统一合同内**已协商**文件能力路由五工具"），故 4.2 不勾选、并入 4.3 处理。

### 4.3 摸底：模型侧本机执行子系统是 `agent/desktop_local/`

| 文件 | 角色 |
|---|---|
| `protocol.py` | `PROTOCOL_VERSION = 1`（worker 协议，**与 v3 合同版无关**）、`ALLOWED_TOOLS = ("read","write","edit","bash","ls","search_files")` |
| `worker.py` | `_build_tools` 按 `ALLOWED_TOOLS` 精确实例化；`call` 对不在名单者抛 `unknown_tool`（**明确失败，非回落**）；`hello` 返回 `protocol_version` 供桌面侧核对 |
| `tool_disposition.py` | `LOCAL_TOOLS`（七个，含 `send`）/ `TRANSFER_TOOLS` / `SERVER_TOOLS`；`disposition()` **未知默认 SERVER**（理由写在 docstring：猜 local 会把未分类工具跑在项目上，猜 refuse 会破坏合法不碰文件的工具） |

**对上 4.3 的现状**：

- 「未知工具明确失败而非回落」——**worker 侧已做到**（`unknown_tool` 抛错，不静默回落）；`disposition()` 的 SERVER 默认是**跨面路由**的语义，不是"回落执行"，两者不冲突。
- 「按已协商文件能力路由五工具」——**worker 侧按 `ALLOWED_TOOLS` 静态枚举，不读已协商能力**。`run_script` 不在名单中，与第一阶段不给脚本一致，但它也**不因 Windows 文件面已协商而出现**。
- 「真实模型调用验证服务器同名哨兵不变」——**需真机 + 真实模型**，属不可本机完成的部分。

### 本轮结论

4.3 的可本机部分集中在两处：**让本机工具名单来自已协商能力**（而非静态枚举），以及**给模型一个可传的 `expected_version` 参数**（需先决定它如何进入 master 的 schema 视图，而合同说 schema 不改——这是**设计矛盾点**）。

按目标约束「遇到设计矛盾处停下询问用户」，下一轮先就此提问，而不是自行选一种改法。
## 打通「打包后能用」：helper 收集链路（本轮，任务 5.1 的本机可做部分）

### 缺陷（一句话）

应用在 `<安装目录>/resources/fs-guard[.exe]` 找 helper，`extraResources` 把 `desktop/resources/` 复制到那里，而**没有任何步骤把 cargo 产物放进 `desktop/resources/`**：`release.yml` 只在 macOS 上编译**一个** helper，两个都没收集。开发态能用只是因为候选列表里另有一条直指构建树的路径。

### 三处修复

1. **`release.yml`**：把「Build the macOS local file helper」（`if: matrix.platform == 'mac'`）换成不分平台的 **Build the local file helpers** —— 构建**两个**（`fs-guard` 与 `project-fs-guard --features write-ops`）并 **`cp` 进 `desktop/resources/`**，按平台选后缀（win → `.exe`）。mac 的架构校验改为**校验收集后的产物**（`desktop/resources/fs-guard`），并新增一步**校验两个 helper 都在 resources 里**——缺 helper 不是应用能绕过的状态（文件工具会一律拒绝），所以让构建在这里失败，而不是到用户机器上表现为「权限问题」。
2. **`desktop/package.json`** 的 `extraResources` filter：原本只放行 `icon.png`/`app-config.json`/`themes/**`，**helper 会被过滤掉**。已加入四个名字（两平台两种后缀）。
3. **`.gitignore`**：`desktop/resources/{fs-guard,fs-guard.exe,project-fs-guard,project-fs-guard.exe}`。约 1.1MB 的平台相关产物由 CI 从旁边的源码构建，提交进来只会静默过期。

### 本机验证（实测，非推断）

本机执行与 CI 相同的收集动作后：

```
read   packaged -> D:\AIPJ\rsmagent\desktop\resources\fs-guard.exe
write  packaged -> D:\AIPJ\rsmagent\desktop\resources\project-fs-guard.exe
```

**修复前这两行都是 `(none found)`。** 并确认 helper 真的可执行（返回协议 JSON）、`package.json` 仍合法解析。

### 新增回归测试

`tests/test_desktop_packaged_helpers.cjs`（3 项，通过）——分三条独立断言，因为任一单条都能被错误实现骗过：

| 断言 | 防的是什么 |
|---|---|
| `extraResources` filter 放行四个名字 | 配置层面漏掉（文件在也不会进包） |
| `release.yml` 含 `--features write-ops` **且** 含 `cp ... desktop/resources/` | 只编译不收集（就是本次缺陷） |
| `.gitignore` 覆盖四个名字 | 二进制被误提交 |

### 验证

| 检查 | 结果 |
|---|---|
| 打包布局解析（两角色） | **找到**（此前均 `none found`） |
| `test_desktop_packaged_helpers.cjs` | **3/3 通过** |
| `test_desktop_guard_binary_roles.cjs` | **9/9 通过** |
| `test_desktop_local_e2e`（本机读写端到端） | **16 项，15 过 1 skip**（skip 为脚本沙箱，与文件读写无关） |

### 仍未完成（5.1 的其余部分）

真机验证（普通用户安装/升级、Defender 隔离、文件丢失时的可信加载与降级）与**签名**需要目标 OS 与凭据，本机不可做，**不勾选**。
## 打包链路：配置已改对，**端到端验证被本机 Electron 解包阻断**（本轮）

### 先纠正我上上轮的一个错误说法

我说 5.1 的验证「本机做不到」。**这是错的。** 实测：

| 事实 | 证据 |
|---|---|
| `electron-builder` 已安装 | `node_modules/.bin/electron-builder.cmd --version` → 25.1.8 |
| Windows NSIS 目标已配 | `build.win.target = nsis`、`arch: x64` |
| **不需要管理员** | `nsis.perMachine: false`（per-user 安装） |
| **不需要预装 NSIS** | electron-builder 自己下载 |
| 后端产物已就绪 | `build/dist/cowagent-backend` 存在 |
| **无签名≠不能装** | 未签名包会触发 SmartScreen 警告，但**能安装**；签名只影响分发时的信任提示 |

**本机第一次尝试就成功产出了安装包**：`desktop/release/容大AI-Setup-2.2.0-x64.exe`。

### 但那次包里**没有 helper**——而问题不在我原先以为的 filter

第一次成功构建后，包内 `release/win-unpacked/resources/` 只有：`backend/`、`app-update.yml`、`app.asar`、`elevate.exe`、`icon.png`。

`icon.png` **在**（说明 `extraResources` 从 `resources` 复制确实生效、位置也对），两个 `*.exe` **不在**。

所以我原先"filter 把 helper 过滤掉了"的判断**方向对但机制没查清**：`filter` 里逐条列 `fs-guard.exe` 仍不生效。与其继续猜 `filter` 的匹配语义，改为**逐文件 `extraResources` 条目**——`from`/`to` 每项一个文件，**无法静默跳过**：

```json
{ "from": "resources/fs-guard.exe", "to": "fs-guard.exe" },
{ "from": "resources/project-fs-guard.exe", "to": "project-fs-guard.exe" },
{ "from": "resources/fs-guard", "to": "fs-guard" },
{ "from": "resources/project-fs-guard", "to": "project-fs-guard" }
```

（四个，因为 mac/Linux 用无后缀名。）四项源路径均已核对存在。`filter` 同时改为 `["**/*"]` 作为第二道保险。

### 阻断点：Electron 解包不完整（环境问题，非配置）

重新构建时 electron-builder 每次都失败于同一步：

```
⨯ ENOENT: rename 'release\win-unpacked\electron.exe' -> 'release\win-unpacked\容大AI.exe'
```

实测根因：`release\win-unpacked` 只有 8 个文件、**没有 `electron.exe`**，且 Electron 缓存目录 `%LOCALAPPDATA%\electron-builder\Cache\electron` **不存在**。日志显示那次下载：

```
downloading url=.../electron-v33.4.11-win32-x64.zip size=115 MB parts=8
retrying attempt=1
downloaded duration=19m55.444s
```

**115 MB 下载耗时近 20 分钟并重试过一次**，解包结果不完整。这是本机网络/下载环境的问题，与打包配置无关——配置层我已核对无误。

### 已交付

| 项 | 状态 |
|---|---|
| `release.yml`：Windows+mac 都构建**两个** helper 并 `cp` 进 `desktop/resources/` | 完成 |
| `release.yml`：新增「两个 helper 必须在 resources 里」的构建期校验 | 完成 |
| `package.json`：`extraResources` 逐文件列出四个 helper 名 + `filter: ["**/*"]` | 完成 |
| `.gitignore`：四个产物名（约 1.1MB 平台相关二进制不入库） | 完成 |
| 本机收集后**解析器实测能找到**两个 helper（此前均 `none found`） | 完成 |
| `tests/test_desktop_packaged_helpers.cjs`（3 项，通过） | 完成 |
| **安装包内 helper 的端到端确认** | **未完成**——被 Electron 解包阻断 |

### 下一步（下一轮）

清掉不完整的 `release/win-unpacked`，让 electron-builder 重新下载/解包 Electron（需网络耐心），然后确认 `release\win-unpacked\resources\` 里出现两个 helper。**这一步成功才算 5.1 的本机部分真的闭环**——配置改对不等于包里有文件，而我已经在"以为改对了"上面栽过一次。
## 打通闭环：安装包内含两个 helper（本轮，5.1 本机部分完成）

### 镜像解决了下载阻断

根因是 **Electron 的运行时 zip 下载不完整**，位置在 `%LOCALAPPDATA%\electron\Cache\`：

| 现象 | 说明 |
|---|---|
| `electron-v33.4.11-win32-x64.zip` 大小 **178.3 MB**（预期约 115 MB） | 下载损坏 |
| 旁边一个 **0 字节的哈希文件** | 校验未完成 |
| 症状 | 解包只有一半文件、**缺 `electron.exe`** → 最后一步 `rename electron.exe -> 容大AI.exe` 报 `ENOENT` |

**解法**：`ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/` + 清掉损坏缓存重下。（此前误查了 `electron-builder\Cache\electron`，真正的缓存是 `electron\Cache`——查错目录让我多花了一轮。）

### 闭环证据（实测）

构建日志自己就报了 helper 的路径：

```
• signing with signtool.exe  path=release\win-unpacked\resources\project-fs-guard.exe
• signing with signtool.exe  path=release\win-unpacked\resources\fs-guard.exe
• signing with signtool.exe  path=release\win-unpacked\容大AI.exe
• no signing info identified, signing is skipped
```

包内核对（`process.resourcesPath` 即该目录，与 `fs-guard-binary.ts:105` 的候选路径一致）：

```
release\win-unpacked\resources\fs-guard.exe           1083 KB  -> True
release\win-unpacked\resources\project-fs-guard.exe   1083 KB  -> True
```

**产出安装包**：`desktop/release/容大AI-Setup-2.2.0-x64.exe`，**234.5 MB**。

### 5.1 本机部分：完成

| 项 | 状态 |
|---|---|
| `release.yml` 构建两个 helper 并收集进 `desktop/resources/` | 完成 |
| 构建期校验两个 helper 存在（缺则构建失败） | 完成 |
| `package.json` 逐文件列出四个 helper 名 + `filter: ["**/*"]` | 完成 |
| `.gitignore` 四个产物名 | 完成 |
| **安装包内含两个 helper，路径与解析器一致** | **实测确认** |
| `tests/test_desktop_packaged_helpers.cjs` | **3/3 通过** |

### 5.1 仍未完成（需真机/凭据，不勾选）

- **签名**：`no signing info identified` —— 未签名包能装，但分发时有 SmartScreen 警告。
- **普通用户安装/升级/回退** 与 **Defender 隔离/文件丢失时的可信加载与降级**：需在干净的目标 OS 上跑，且这些行为以**签名**为前提。

### 我这一轮的两处失误（记录以免重复）

1. **误说「本机做不到」**：实际本机能打包（`perMachine: false` 不需管理员、NSIS 自动下载、无签名≠不能装）。第一次尝试就产出了安装包。
2. **查错缓存目录**：`electron-builder\Cache\electron`（空）不是 Electron 的缓存位置，真正的是 `electron\Cache`。一条 `Get-ChildItem` 找对目录就能省下一轮。
## 7.4 起步：原生脚本隔离 launcher（本轮，**只做 7.4 的本机可做部分**）

### 先纠正一个我自己的错误判断

我此前把 7.1–7.4（LPAC/Job/ACL 生命周期原型）整块归为「无管理员做不到」。**实测推翻**：

| 能力（标准用户） | 实测 |
|---|---|
| 改自己目录的 ACL（`AddAccessRule`+`Set-Acl`） | **可以** |
| 启动进程 | **可以** |
| 创建 junction | **可以** |
| **`CreateAppContainerProfile`** | **可以**，`hr=0x00000000`，删除也成功 |

`CreateAppContainerProfile` **历史上**需要提权，我一直据此认为 LPAC 本机做不了——**没有实测就下了结论**。真正挡住脚本层的不是权限，是**代码没写**与 **7.1 决策未做**。

### 交付：`desktop/native/script-launcher/`

Rust，一个 CLI + 一个库（隔离策略只有**一份**实现），产物 `script-launcher.exe`（1478 KB）。`cargo build --release` 与 `cargo check --release` 均**零警告零错误**。

**已实现的 Job Object 半边（D8 的资源与生命周期）**：

- **挂起创建 → 赋入 Job → 才恢复**（`CREATE_SUSPENDED` → `AssignProcessToJobObject` → `ResumeThread`）：先跑再约束的进程已经有过机会派生
- `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`：**launcher 崩溃也不残留**，不依赖它活着做清理
- 超时/取消 → `TerminateJobObject` 终止**整棵树**
- 进程数上限（`ACTIVE_PROCESS` + 轮询 `QueryInformationJobObject`，把越界转成具名的 `limit_exceeded`）
- 内存上限**同时**设 `JOB_MEMORY`（整树）与 `PROCESS_MEMORY`（单成员）——只设前者会放任一个吃满的大子进程
- 输出每流 1 MiB 上限，超限置 `*_truncated`
- stdout/stderr **各一线程并发排空**：串行等待会因管道缓冲填满而**死锁**，表现为「卡住」
- 环境块**只由调用方清单构成**，不与父进程合并
- 自行实现 `CreateProcess` 的引号规则（`Command` 无法挂起创建）——**写错就是参数注入**
- **拒绝即不执行**：Job 创建/赋值失败一律返回错误；**没有任何路径恢复未受约束的子进程**

### 实测（4 条命令的行为已钉进测试）

```json
// 正常
{"status":"exited","exit_code":0,"stdout":"hello-from-launcher \r\n","stderr":"err-line \r\n","terminated":false}
// 超时
{"status":"timed_out","exit_code":null,"terminated":true}
// 请求访问边界（未实现）
{"refused":"unsupported: the AppContainer access boundary is not implemented in this build"}
```

第三条是关键设计：**请求隔离而拿不到时拒绝，不静默降级**——降级是唯一用户无法从结果察觉的失败。

`tests/test_script_launcher_isolation.py`（**6/6 通过**）驱动**真实二进制**（不 mock，因为被断言的都是 OS 边界性质），覆盖：正常读写分离、非零退出如实报告、超时终止且 `exit_code` 为 null、AppContainer 被拒且不执行、缺工作目录被拒、**环境白名单不含父进程变量且白名单变量确实传入**。

### **未实现**（已写入 `docs/isolation-status.md`，不得读作已支持）

1. **访问边界（LPAC/AppContainer）——最关键的一半。** `Access::AppContainer` 恒返回 `unsupported`，所以**当前只管住「进程树与资源」，不管住「能碰到什么」**，脚本仍以用户自己的文件权限运行。按 D8「Job 只管理资源/生命周期，**LPAC 才承担访问边界，二者不可替代**」，**7.4 只完成一半**。
2. **未接线到生产路径**：launcher 是独立可执行文件，桌面主进程**没有调用它**。**脚本功能并未因此可用。**
3. **ACL 生命周期（7.2/7.3）**：`desktop/native/` 下无任何添加/跟踪/恢复 ACE 的代码；7.2 全部未做，而它是**接业务的硬前置**。
4. **运行时选择（7.1）**：`executable` 由调用方给定，7.1 的比较与决策记录未做，**没有随包运行时**，也不保证调用方给的是受信解释器。
5. **stdin 未接通**（子进程读到 EOF，`JobSpec.stdin` 被忽略）。
6. **未做真机验证**：只在本机 Win11 标准用户实测；Win10 22H2 未跑，S04/S06/S07 未接。

### 测试中我犯的一处错误

「环境白名单」用例最初用 `echo [%COW_LAUNCHER_LEAK_PROBE%]` 断言，失败并**看起来像真的泄漏**。诊断后是我的测试写错：**cmd 对未定义的 `%VAR%` 不展开而是保留字面量**，所以 `[]` 永远匹配不到。改用 `set` 直接 dump 子进程环境后确认：**父进程变量未泄漏、白名单变量确实传入**。

**教训**：断言「某物不存在」时，必须先确认该断言**在存在时也会失败**——否则它是一条恒真断言。

### 任务状态

**7.1–7.4 均不勾选。** 7.4 只完成 Job 半边且未接线；7.1/7.2/7.3 未做。
## 7.4 续：AppContainer 访问边界已实现，但**进程创建未打通**（本轮）

### 先纠正我上一轮的一个说法

我上轮说「`Access::AppContainer` 恒返回 `unsupported`」。**现在不再是**——代码真的会尝试创建受限进程。那条拒绝已移除。

### 已落地（`desktop/native/script-launcher/src/appcontainer.rs`）

| 环节 | 实现 |
|---|---|
| SID 取得 | `DeriveAppContainerSidFromAppContainerName` |
| profile | `CreateAppContainerProfile`，名 `cow-project-script`，**一个共享 profile**——将来 ACL 写的是一个 SID，每运行换 SID 就是每运行撒一个 ACE |
| 属性列表 | `InitializeProcThreadAttributeList`（**两趟**：先问大小）→ `UpdateProcThreadAttribute(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)` |
| 进程创建 | `STARTUPINFOEXW` + `EXTENDED_STARTUPINFO_PRESENT` |

**实测**：profile **确实建成**（`%LOCALAPPDATA%\Packages\cow-project-script` 存在）。

### 未打通：`CreateProcessW` 在 AppContainer 路径失败

| 尝试 | 结果 |
|---|---|
| 直接创建 | `error 87` `ERROR_INVALID_PARAMETER` |
| 修掉一个真实 bug 后 | `error 31` `ERROR_GEN_FAILURE` |
| 显式 `lpDesktop = WinSta0\Default` | `error 203` `ERROR_ENVVAR_NOT_FOUND` |
| 补 `=C:` 驱动器变量 | 仍 203 |
| 补 `SystemRoot`/`windir`/`TEMP`/`USERPROFILE`/`LOCALAPPDATA`/`APPDATA`/`ComSpec` | 仍 31/203 |
| `cwd` 改为可读目录（System32） | 无变化 |

**已排除**：属性列表未填、`cb` 大小、flag 缺失、`cwd` 不可读、上述环境变量逐项。

**最可能的下一步（未验证）**：设 `lpDesktop` 得 203、不设得 31，指向 **profile 的环境/注册状态**而非创建参数。建议先用**最小原生 C 程序**（不经 Rust 与环境块构造）验证 `CreateProcessW`+AppContainer 能否跑通 `cmd /c echo`；能则问题在本实现的参数构造，不能则问题在 profile 或 OS 状态。

### 修复过程中发现并修掉一个**真实内存安全 bug**

`SECURITY_CAPABILITIES` 原本是 `attribute_list()` 的**局部变量**，而 `UpdateProcThreadAttribute` 存的是**指针**。函数返回后指针悬空，`CreateProcess` 稍后才读——**use-after-free**，且症状只是一个「参数无效」错误码。已改为 `Box` 并由 `AttributeList` 持有。同类问题也修在 `StartupInfoEx._desktop`（`lpDesktop` 指向的字符串必须比 `info` 活得久）。

**这是本轮最有价值的产出之一**：一个在安全边界代码里、只表现为错误码的悬垂指针。

### 一条必须说清的约束

**即使进程能创建，它仍然读不到项目目录。** AppContainer 默认什么都不允许，必须由 7.2 在该目录上写一个指名该 SID 的 ACE。所以本模块单独存在时，受限子进程会启动后立刻失败——**「边界有了、授权还没有」**。这正是 7.2 被 D8 定为硬前置的原因。

### 验证

| 检查 | 结果 |
|---|---|
| `cargo build --release` | **0 错误 0 警告** |
| `test_script_launcher_isolation.py` | **6/6 通过** |

其中一条测试**改了断言**：它原本断言"AppContainer 被拒为 unsupported"，现在断言**关键性质**——子进程**未执行**（`should-not-run` 不出现）、**未静默降级**（不得返回 `exited` 状态）、且失败被报告。这样它同时覆盖"边界可用"与"边界不可用"两种世界。

### 7.1–7.4 状态

仍**均不勾选**。7.4 现在有了 Job 半边 + 访问边界代码，但访问边界**未打通**、**未接线到生产路径**；7.2/7.3 未做。
## 7.4 续：AppContainer **能跑但不稳定**（本轮，量化结论）

### 结论

上一轮我说「AppContainer 进程创建未打通」。**更准确的结论：打通了，但只有约一半的启动成功。**

同一作业连续 6 次（`cwd`=System32，`cmd /c echo x`）：

| 路径 | 结果 |
|---|---|
| `access=app_container` | **成功 3 / 失败 3** |
| `access=inherited`（对照） | **成功 6 / 6** |

失败是 `ERROR_NOT_APPCONTAINER`（4250）与 `ERROR_GEN_FAILURE`（31）**交替**出现，而同一命令手工运行时曾连续成功。**非确定性**，不是参数配错。

**故意不算完成**：一半时间失败的访问边界不是边界——它把"隔离了"变成随机事件。三条使用 AppContainer 的测试标为 `skip` 并写明启用条件；`inherited` 路径的测试**仍硬失败**，不受影响。

### 用最小原生探针定性的过程（本轮的主要工作）

写了 `src/bin/probe-appcontainer.rs`——绕开 launcher 的全部构造，直接调 API，并**逐个变量试**。这一步把「我的参数错了」与「OS 拒绝」彻底分开了：

| 变体 | 结果 |
|---|---|
| `--inherit`（`lpEnvironment = NULL`） | **成功** |
| `--null-env`（空环境块 `\0\0`） | 失败 203 |
| `--env-low`（只 SystemRoot/windir） | 失败 203 |
| `--env`（+ `=C:` 驱动器变量） | 失败 203 |
| `--env-userprofile`（+ USERPROFILE） | 失败 203 |
| `--env-appdata`（+ LOCALAPPDATA/APPDATA） | **成功** |
| `--env-full`（再加其他） | **成功** |

**量化出最小必需集合**：`LOCALAPPDATA` + `APPDATA`（加 `SystemRoot`/`windir`）——AppContainer 无法继承父环境，要从中派生容器目录。仅 `USERPROFILE` 不够。

launcher 已据此**只对受限路径**注入这组系统变量（从本进程取，不是合并父环境；`inherited` 路径一字未改）。

### 又一处**真实内存安全 bug**（本轮修掉）

探针里我写出了与 launcher 同型的错误：环境块作为 `if`/`else` 的**临时值**构造，表达式结束即析构，`env_ptr` 随即悬空。这正是"用错误码掩盖的内存问题"这一类。

### 一条被实测确认的设计前提

**项目目录必须获得 ACL 授权才能作为 `cwd`**：未授权时启动**直接失败**（`ERROR_NOT_APPCONTAINER`）。

这不是 bug，而是 **7.2 作为硬前置的直接证据**——AppContainer 连自己的 cwd 都读不了。**「边界」与「授权」是两件事，缺后者则前者让功能完全不可用。**

### 验证

| 检查 | 结果 |
|---|---|
| `cargo build --release`（含探针） | **0 错误 0 警告** |
| `test_script_launcher_isolation.py` | **8 项：5 通过 / 3 skip**（skip 项写明「一半成功一半失败，测得 3/6」） |
| 稳定性量化 | AppContainer **3/6**，inherited **6/6** |

### 下一步（写给接手者）

失败**交替**出现，指向 profile 的**首次初始化/并发**而非创建参数。建议：①启动前等 profile 目录就绪；②用 `CreateAppContainerProfile` 的返回值区分"新建"与"已存在"，对新建情况做一次预热启动丢弃结果。

### 7.1–7.4 状态

仍**均未勾选**。7.4 现有 Job 半边（稳定）+ 访问边界代码（**不稳定**）+ 环境要求（已量化）；**未接线到生产路径**；7.2/7.3 未做。
## A：修 AppContainer 不稳定——**逐个排除，未找到根因**（本轮）

### 量化基准（清掉残留后台进程后）

| 路径 | 多次测量 |
|---|---|
| `inherited`（对照） | **15/15**、**6/6**、**20/20** |
| `app_container` | **9/15**、**13/20**、**19/25** |

**约 60–80%**，而 `inherited` 始终满分——**问题在本实现，不在机器**。

（顺带清掉一个干扰源：机器上有 8 个父进程为 `codex.exe` 的残留 `cmd`，来自我早先的后台任务，横跨数小时。它们让测量噪声更大；清掉后 `app_container` 从 3/6 升到 8/10，但**仍未解决**。）

### 逐个排除的变量

| 假设 | 手段 | 结论 |
|---|---|---|
| 环境块构造错误 | `COW_LAUNCHER_NULL_ENV` 传 `NULL` | NULL 10/15 vs 真实 12/15 → **无关** |
| `CREATE_NO_WINDOW` | 开关对比 | 12/20 vs 16/20 → **噪声内** |
| Job 赋值（探针没有这步） | `COW_LAUNCHER_SKIP_JOB` | 16/20 → **无关** |
| 失败是瞬时的 | 重试 4 次 + 20ms | 19/25 **无改善** → **按次复现** |

### 最锐利的线索（留给接手者）

**同一个 AppContainer 启动，探针程序 20/20，launcher 9/15。** 两者差异已被逐个关掉（环境、`CREATE_NO_WINDOW`、Job），**仍未找到那一个差异**。

下一个该测的：把 launcher 的**管道**（`CreatePipe` + 两个读取线程）单独开关化——它是我尚未排除、且探针完全没有的一项。

### 我删掉了无效代码

`CreateProcess` 的 4 次重试**测出无效**，已删除；三个诊断环境开关也已删除。**留着无效的缓解手段比不写更糟**——它让代码看起来"处理过了"，而问题仍在。

### 验证

| 检查 | 结果 |
|---|---|
| `cargo build --release` | **0 错误 0 警告** |
| `test_script_launcher_isolation.py` | **8 项：5 通过 / 3 skip** |
| `inherited` 路径回归 | **15/15** |

### 结论

**根因未定位。** 边界在 60–80% 的时间生效，**不足以称作边界**——三条 AppContainer 测试保持 `skip`，原因写明实测数字。

任务 7.4 仍**不勾选**。
## 接线：launcher 增加**透明包装模式**并接入 `WorkerSession`（本轮）

### 先说我上一轮设计错在哪

我最初把 launcher 造成「跑一个命令、收一次输出」（stdin 一个 JSON → stdout 一个 JSON）。**那个形态接不进现有系统**：`WorkerSession` 启动的是一个**长期双向 JSON-RPC 对端**，通过 stdio 收发带请求 id / 超时 / 取消的消息。一个有自己协议的包装器会把那条会话吃掉。

**这是我选错了形态**，不是接线难度问题。

### 正确形态：透明包装（`--wrap`）

```
TypeScript ──spawn──▶ script-launcher ──Job──▶ worker
     ▲                      │                    │
     └──── stdio 直通（不解析）──────────────────┘
```

`--wrap <exe> [args...]`：创建 Job → 挂起创建 → 赋入 Job → 恢复 → 等待，**stdio 原样交给子进程**。配置走环境变量（`COW_WRAP_CWD`/`COW_WRAP_ENV`/`COW_WRAP_TIMEOUT_MS`/…），这样**命令行就只是被包装的命令**——包装器不必去分辨哪些参数是自己的。

### 为什么这比现状好

`WorkerSession` 现在用 **`taskkill /T`** 收进程树，它遍历**活的进程表**：一个已被重新挂靠的后代、或在遍历之后才出现的后代，都会漏掉。Job 没有这个失败模式——`KILL_ON_JOB_CLOSE` 由**内核**在最后一个句柄关闭时结束全部成员，**包括 launcher 自己被杀死、根本没机会清理的情况**。

### 实施中发现并修掉的 bug

第一版**输出丢失**（只有 exit 0）：我没有设 `STARTF_USESTDHANDLES`。未设置时子进程只拿到**可继承**的句柄，而调用方（实测 PowerShell、以及 Python 的 `PIPE`）给的 stdout **不是**可继承的——子进程拿到一个死句柄，**退出码却看着正常**。显式传 `GetStdHandle(...)` + 该标志后恢复正常。

**这个 bug 的形状值得记**：输出消失而状态码正确，是最难从结果察觉的一类。

### 新测试（`WrapModeTests`，5 项）

| 用例 | 钉住的性质 |
|---|---|
| stdout/stderr 原样透传 | 调用方给的是**不可继承管道**，靠朴素继承会丢输出 |
| **双向会话存活** | 最贴合真实用途：一边写一边读，包装器吃掉任一方向都会表现为缺回复 |
| 退出码回放 | 监督者隐藏子进程状态则毫无用处 |
| 拒绝只写 stderr | **stdout 属于被包装的协议**，错误写进去就是破坏会话 |
| **被杀死时树也结束** | 取代 `taskkill` 的那条性质：让子进程派生孙进程，包装器结束后孙进程必须无法完成工作 |

### 验证

| 检查 | 结果 |
|---|---|
| `cargo build --release` | **0 错误** |
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_script_launcher_isolation.py` | **13 项：10 通过 / 3 skip** |
| `test_desktop_local_execution.cjs` | **36/36 通过** |

### 接线状态

`WorkerSessionOptions.wrapper` 已加入；给了 `wrapper` 就经 launcher 启动，**没给就与以前完全一致**（无 launcher 的平台保持既有行为，而不是悄悄失去约束）。

**尚未做**：调用方（`executor-endpoint` / `remote-container-ipc`）**还没有传入 `wrapper`**，所以实际运行仍是旧路径。原因是 launcher 的**路径解析**要与文件 helper 放在一处（`fs-guard-binary.ts` 已有 `guardBinaryCandidates` 的模式）——这是下一步。
## 接线续：launcher 路径解析就位（本轮）

`local-files/fs-guard-binary.ts` 的 `GuardRole` 扩为第三个角色 `launcher`：

| 角色 | 产物名 | crate 目录 |
|---|---|---|
| `read` | `fs-guard` | `native/fs-guard` |
| `write` | `project-fs-guard` | `native/fs-guard` |
| **`launcher`** | **`script-launcher`** | **`native/script-launcher`** |

**放在这里而不是新写一个解析器**：`'我们的哪个二进制在哪里（构建树与安装包）'` 是**一个问题、一个答案**；答两遍正是打包时恰好漏掉其中一个的那种方式——文件 helper 就是这样漏掉的（任务 5.1）。

改动：
- `BASE_NAME` 增加 `launcher: 'script-launcher'`
- crate 目录按角色命名而非假定（`launcher` 用 `script-launcher`），因为两个 crate 在不同目录
- 新增 `COW_SCRIPT_LAUNCHER` 覆盖，与既有的 `COW_FS_GUARD`/`COW_PROJECT_FS_GUARD` 同形——测试可以驱动刚构建出来的二进制，而不必假装自己是已安装的应用

### 验证

```
read      ...\native\fs-guard\target\release\fs-guard.exe
write     ...\native\fs-guard\target\release\project-fs-guard.exe
launcher  ...\native\script-launcher\target\release\script-launcher.exe
```

| 检查 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_guard_binary_roles.cjs` | **9/9 通过** |
| `test_desktop_packaged_helpers.cjs` | **3/3 通过** |
| `test_script_launcher_isolation.py` | **13 项：10 通过 / 3 skip** |

### 接线还差的一步

**调用方仍未传入 `wrapper`**：`executor-endpoint.ts` / `remote-container-ipc.ts` 需要解析出 launcher 路径并放进 `WorkerSession` 的选项。解析能力本轮已就位，**传参是一行**，但需要决定**哪些运行该走包装器**（例如：只有脚本运行，还是包括文件 helper 的 worker）。这是设计选择，不是机械改动，故留待下一轮。

### 端到端效果

`WorkerSessionOptions.wrapper` 给了 → 经 launcher（Job 管树）；不给 → **与以前逐字一致**。所以接线的**风险面是零**：不传就是旧行为。
## 接线完成：项目 worker 经 launcher 启动（本轮，7.4 的接线项）

### 按推荐方案：**只包装项目 worker**

选择依据（实测量取，非推断）：

| | 文件 helper (`fs-guard`) | 项目 worker (`worker.py`) |
|---|---|---|
| 子进程 | **不派生**（源码里 `spawn`/`shell` 是**被拒绝的 op**；唯一 `thread::spawn` 是读线程） | **会派生**（`process_group_hook` 专门记录每次命令的组 id） |
| 代码来源 | 我们自己的 Rust，op 白名单限定 | **用户项目里的脚本/命令**（不受信任） |
| 超时/内存上限 | 单次 op，天然有界 | **需要** |
| 取消 | 单次请求 | **需要杀整棵树** |

**Job 的三件事里有两件对文件 helper 是空的**（不派生、天然有界），所以为它建 Job 只是绕路。**只包装会派生的那条**，将来若给文件 helper 也加，那是一次可独立验证的决定。

### 改动（三处）

| 文件 | 改动 |
|---|---|
| `local-read-assembly.ts` | 配置类型加可选 `wrapper`；构造 `WorkerSession` 时**有条件**传入（无则逐字旧行为） |
| `remote-container-ipc.ts` | 用 `resolveGuardBinary({role:'launcher'})` 解析路径——**与文件 helper 同一个解析器**；只在解析到时传 `wrapper` |
| （已有）`worker-session.ts` | 上一轮已实现 `wrapper` 分支与 `--wrap` 调用 |

**上限初值刻意放宽**：`maxProcesses: 64`、`maxMemoryBytes: 4 GiB`。**误杀正常工作的 Job 比没有 Job 更糟**，所以这两个值应当从实测收紧，而不是猜。

### 验证

| 检查 | 结果 |
|---|---|
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |
| `test_desktop_local_execution.cjs` | **36/36** |
| `test_desktop_local_read.cjs` | **24/24** |
| `test_desktop_isolated_preview.cjs` | **27/27** |
| `test_desktop_project_source.cjs` | **38/38** |
| `test_desktop_guard_binary_roles.cjs` | **9/9** |
| `test_script_launcher_isolation.py` | **13 项：10 通过 / 3 skip** |

**合计 134 项通过、0 失败。**

### 现在这条路径是什么状态

```
模型调用 read/write/edit/ls/search_files/bash
        │
        ▼
LocalReadAssembly.runDeviceTool
        │
        ▼
WorkerSession ──spawn──▶ script-launcher --wrap ──Job──▶ python worker
                                    │                      │
                                    └── stdio 直通 ────────┘
```

**进程树现在由内核（Job）持有**，取代 `taskkill /T` 的遍历。launcher 崩溃、被杀，树都随之结束。

### 仍然做不到的（不改口径）

| 做不到 | 原因 |
|---|---|
| 脚本层（`run_script`/PowerShell 7） | 未实现：随包运行时只做了决策 |
| **真正的访问边界** | AppContainer **60–80% 不稳定**，根因未定位；脚本进程仍以当前用户权限运行 |
| Windows 目标 OS 验收 | 本机无 Win10 22H2 |
| 签名与真包安装 | 无凭据 |

**关键**：Job 层与 AppContainer 层**互相独立**。Job 层现在 15/15 稳定且已接线；AppContainer 的不稳定**不阻塞**它，也不被它掩盖（访问边界仍是缺的一半）。
## 4.4 / 4.5 逐条核对（本轮）

### 4.4 条款 → 覆盖

| 4.4 条款 | 依据 |
|---|---|
| 复用工件/预览/显式传输接缝 | `test_desktop_device_artifacts.cjs` 16 项、`test_desktop_isolated_preview.cjs` 27 项、`test_desktop_transfer.py` 11 项 |
| **写入后验证真实文件及版本才发布引用** | `a successful write emits one desktop artifact naming the real file`、`the source version is the content digest, not the file name`、`a frame whose file never reached disk emits no artifact`、`a write that a failed run never completed emits no artifact`、`a write run that failed after writing the file emits no artifact` |
| 跨项目历史 / 无真实产物 | `a write that escapes the project names no file`、`a symlink pointing outside the project is not a local reference`、`a file reached through a symlinked directory is not in the project`、`a directory is not an artifact` |
| 恶意 HTML | `test_desktop_isolated_preview.cjs`（预览在隔离 scheme 下） |
| **显式上传** | `test_desktop_materialize.cjs`：`a file that is not the one approved is refused before any transfer opens`、`a commit with no artifact reference is a failure, not a silent success` |
| **不默认整目录上传** | `no read op touches the upload transport`——读操作**不碰**上传通道 |
| 离线 | `test_desktop_publish.py`：`test_client_files_materialize_committed_offline` |
| 更新双语来源和数据流说明 | **未见专门产物**；`execution-compatibility-deployment.md` 覆盖了数据流，双语来源未单列 |

### 4.5 条款 → 覆盖

| 4.5 条款 | 依据 |
|---|---|
| 撤权 | `test_desktop_local_root.py`：`test_revoke_is_narrow`、`test_revoke_everything_for_a_user`、`test_version_bump_invalidates_the_old_root`；broker：`test_prepare_refuses_a_revoked_workspace_grant`、`test_prepare_refuses_a_revoked_binding` |
| 退出 | `test_desktop_command_runner.cjs`：`A18: a sign-out stops this session`、`A18: a tenant switch stops only the session that is going away` |
| **旧结果隔离** | **本轮补**（见下） |
| 文件 grant 失效 | `test_version_bump_invalidates_the_old_root`、`test_scope_mismatch_never_falls_back_to_a_nearby_entry`、`test_require_mode_is_enforced` |
| 重启恢复 | `test_desktop_execution_journal.cjs`：`a crash between start and receipt becomes outcome_unknown, and is not retried`、`recovery leaves finished commands finished` |
| helper 崩溃 | `test_desktop_execution_capability_v3.py`（helper 不可用 → `feature_unavailable`）、`test_workspace_local_refusals.cjs`（协商码与三语言文案）；**未做端到端崩溃注入** |
| 在途脚本按 D6 处理 | **未实现**——属脚本层 |
| **待确认写入与重新选择** | **本轮补**（start 一侧） |
| 不靠放宽项目 ACL 取得权限 / 私有暂存权限按 D3 / 无需管理员或 Python | `fs-guard-error-recovery.md`、`fs-guard-platform-support.md`；Rust 侧 `mode 0o600` 与暂存命名有测试 |

### 本轮补的测试

`tests/test_desktop_execution_broker.py` 新增 `test_start_refuses_after_the_workspace_grant_was_revoked`：

**为什么补它**：现有只测了 **prepare** 拒绝撤权。而设备在**每次执行前**都会再问一次，所以「prepare 说过可以」不可能是最终答案——用户可能**在中间**撤权，而那个窗口正是排队命令会跑掉的时候。只有 **start** 真正花掉授权，所以必须在 start 上验证。

写法上先做一次成功的 prepare，使随后的拒绝**可归因于撤权**，而不是归因于"根本没 prepare 过"。

**实测**：隔离运行 **OK**。

### 我在这轮的一处失误

我按记忆写了 `self.assertEqual(prepared.status_code, 200)`——但 `self.call` 返回的是 web.py 响应对象，没有 `status_code`。套件直接报 `AttributeError`。改用既有的 `self.ok(response)` 辅助。

**教训**：这个仓库的测试有自己的断言辅助（`ok` / `refusal`）；**先用既有的，别按通用直觉写**。

### 套件状态（一个既有的共享状态问题）

`test_desktop_execution_broker`：48 项，**2 项失败在 quota 相关用例**。**隔离运行这两个用例都 OK**——即**跨测试的用量状态泄漏**（与本会话早先修过的 `_clear_quota` 同类），**不是产品缺陷**。

说明：本轮改动只增了一个测试，未触及 broker 逻辑（`git diff` 确认）。但**我无法用 `git stash` 建干净基线**，因为本 change 的改动大部分是**未跟踪文件**，stash 后基线退化为「未打门禁补丁」的 42 失败状态，不可对照。故此处只能声明「隔离通过 ⇒ 非产品缺陷」，**不声称已定位泄漏源**。

### 4.4 / 4.5 是否勾选

**均不勾选**：

- **4.4**：双语来源说明未见专门产物；其余条款有覆盖。
- **4.5**：**helper 崩溃的端到端注入**与**在途脚本按 D6**未做（后者属脚本层）。旧结果隔离与待确认写入本轮已补。
## 修掉 broker 套件的 quota 状态泄漏（本轮）——套件回到全绿

### 症状

`test_desktop_execution_broker`：48 项中 **2 项 quota 用例失败**；**隔离运行则通过**。这是我在 4.4/4.5 那轮记录为「跨测试共享状态泄漏、非产品缺陷」的那一个。

### 根因（两个层次，都不是产品缺陷）

**第一层：`_clear_quota` 调 `quota_status` 的参数与返回结构都不对**

```python
def quota_status(self, *, actor_user_id, tenant_id) -> Dict:
    return {"tenant_id": …, "limits": [...],
            "usage": [{"user_id":…, "metric":…, "used":…}, ...]}
```

而清理代码传的是 `user_id=`/`metric=`（**TypeError**），并读 `status.get("used")`（**键不存在**）。异常被 `except Exception: return` **吞掉**，于是 `used = 0`，**退款从未执行**。

这是我早先写的那段代码——**它看起来修好了，实际是静默地什么都没做**。

**第二层：用量记在两个桶里，只退一个不够**

`consume_quota` 同时为**租户桶（`user_id=''`）**与**成员桶（`u1`）**计费，而 broker 的 `_check_quota` 两个都读（`used + amount > hard_limit` 即拒）。即便第一层修好，只退成员桶仍会让租户桶的用量永久留存。

### 修复

两处都改：按 `show` 的真实结构解析 `usage` 列表；**两个桶都退**。

### 为什么值得记下来

**这个 bug 的形状不是「代码写错」，而是「错误被吞掉后看起来正常」**：

- `except Exception: return` 让 `TypeError` 变成静默跳过
- 结果是**测试仍然失败，但失败搬到了别的用例上**——于是看起来像"别的测试有问题"

这正是本会话反复出现的那一类：**一个静默的失配，被另一个地方的症状暴露。**

### 验证

| 检查 | 结果 |
|---|---|
| 两条 quota 用例一起跑 | **OK** |
| `test_desktop_execution_broker` | **48/48 全绿**（修复前 2 失败） |

**附带**：本轮新增的 `test_start_refuses_after_the_workspace_grant_was_revoked`（4.5 的 start 侧撤权）也在这 48 项之内，一并通过。
## AppContainer 根因定位：**管道**（本轮）

### 对照实验（决定性）

| 模式 | 句柄 | 稳定性 |
|---|---|---|
| JSON 模式 | **管道** | **13–14 / 20** |
| `--wrap` 模式 | **继承本进程标准句柄** | **20 / 20** |

同一个 AppContainer 启动，**唯一差别是子进程拿到的是管道还是继承句柄**。这就是根因所在。

### 机制

JSON 模式的管道句柄是**可继承**的（`CreatePipe` 时 `bInheritHandle = 1`）。而 `bInheritHandles = 1` 时，**本进程所有可继承句柄都会被提供给子进程**——AppContainer 打不开其中大多数，于是 `CreateProcess` 失败。**失败与否取决于当时还开着什么**，这正解释了间歇性。

API 为此提供的机制是 `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`。

### 我试了 `HANDLE_LIST`，**结果更差**，已回退

加句柄列表后：**7 / 25**，且 `ERROR_INVALID_PARAMETER`（87）成为主导失败（9/25）。同一条列表上设两个属性是明显嫌疑，但**我没有把它留在树里**——**一个让实测变差的改动，在其缺陷未查清前不该留在代码中**。

回退后确认回到 13/20（原水平），`--wrap` 与 `inherit` 路径均未受影响（20/20、15/15）。

### 这轮最有价值的产出

**根因从「未定位」变成「已定位到管道」**，且是**对照实验**得出的，不是推断：

- 已排除（前几轮）：环境块、`CREATE_NO_WINDOW`、Job 赋值、`lpDesktop`、超时/时钟
- **本轮确定**：管道 vs 继承句柄 = 14/20 vs 20/20

### 下一步（写给接手者）

`HANDLE_LIST` 方向正确但我的接线有缺陷。建议检查顺序：
1. **`UpdateProcThreadAttribute` 的 `lpValue` 必须是句柄数组的指针**，且该数组**必须比属性列表活得久**（我把 `Vec` 放进了 `AttributeList`，但两次补丁可能破坏了这一点——重写时已按此要求放置）
2. **两个属性时 `InitializeProcThreadAttributeList` 的 `dwAttributeCount` 必须是 2**（第一趟问大小也要用 2）
3. **`cb` 必须是 `size_of::<STARTUPINFOEXW>()`**，且 `EXTENDED_STARTUPINFO_PRESENT` 必须设置
4. 若仍失败：改用**只设一个属性**的路径先验证管道能否继承（例如把 `SECURITY_CAPABILITIES` 换成 `HANDLE_LIST` 单独跑一次，确认哪一个是罪魁）

### 验证

| 检查 | 结果 |
|---|---|
| `cargo build --release` | **0 错误 0 警告** |
| `test_script_launcher_isolation.py` | **13 项：10 通过 / 3 skip** |
| JSON 模式 + AppContainer | **13/20**（回到基线，未变差） |
| `--wrap` 模式 | **20/20** |
| JSON 模式 + inherit | **15/15** |

**当前状态**：访问边界**可用但不稳定**（约 65%）。任务 7.4 仍不勾选。
## AppContainer：正确实现 `HANDLE_LIST` 后仍不改善（本轮，**有效排除**）

### 上一轮我说「方向正确、接线有缺陷」——本轮把它做对了，结果没变

上一轮加 `HANDLE_LIST` 得到 **7/25**（比不加以往的 14/20 更差），我据此回退。本轮按三个已知踩点重做：

1. **两趟 `InitializeProcThreadAttributeList` 用同一个 count = 2**（第一趟问大小也要是 2——这是最可能的原缺陷）
2. **句柄数组比属性列表活得久**（放进 `AttributeList` 持有，与 `_capabilities` 同理：属性存的是**指针**）
3. `UpdateProcThreadAttribute` 的 `cbSize` 用 `len * size_of::<HANDLE>()`

编译 **0 错误 0 警告**。

### 结果：**没有改善**

| 配置 | 稳定性 |
|---|---|
| JSON + AppContainer（**带** HANDLE_LIST） | **13/25、16/20** |
| JSON + AppContainer（不带） | 13–14/20 |
| JSON + inherit（对照） | **20/20** |
| `--wrap`（对照） | **20/20** |

失败仍是 `ERROR_NOT_APPCONTAINER`（4250）为主、`ERROR_GEN_FAILURE`（31）次之。

### 判断：保留而不回退

上一轮我回退它，理由是「让实测变差的改动不该留」。**现在它不再让结果变差**（正确实现下与基线持平），且它是 **API 文档为这个问题指明的机制**。所以保留，并在模块文档里写明**它是什么、不是什么**：

> 句柄列表**已传入且实现正确**，但**不是全部答案**——`ERROR_NOT_APPCONTAINER` 仍主导。

### 这一轮的产出性质

**这是一个有效的排除结果**，不是失败：它把「HANDLE_LIST 接线错误」这个假设**彻底排除**了。剩下的假设必须解释**为什么子进程仍不被认为是 AppContainer**——`ERROR_NOT_APPCONTAINER` 的字面含义是「这个操作要求进程在 AppContainer 内」，而失败发生在 **`CreateProcess` 时**，说明**安全能力属性本身没有被采纳**。

**下一步应该转向的属性是 `SECURITY_CAPABILITIES` 而不是句柄**。可能的检查方向：
1. 属性列表里**只放 `SECURITY_CAPABILITIES`、不放 `HANDLE_LIST`**，同时**只传一个标准句柄**（stdout），把变量降到最少
2. 确认 `SECURITY_CAPABILITIES` 的 `AppContainerSid` 与 `CapabilityCount=0` 是否被接受——**空能力列表**在某些 Windows 版本上可能是问题（可试加一个 `internetClient` 之外的无害能力，如 `lpacCom`，或反过来确认空列表合法）
3. 用**最小原生 C 程序**（不经 Rust 与任何管道）复现「带管道 + AppContainer」，确认问题可脱离本实现独立复现——这能一刀切开「我的代码」与「OS 行为」

### 验证

| 检查 | 结果 |
|---|---|
| `cargo build --release` | **0 错误 0 警告** |
| `test_script_launcher_isolation.py` | **13 项：10 通过 / 3 skip** |
| JSON + AppContainer | 13–16/20 |
| JSON + inherit | **20/20** |
| `--wrap` | **20/20** |

**任务 7.4 仍不勾选**：访问边界可用但不稳定（约 65–80%）。
## 6.3 **完成**：strict / delta / 活动 change 重叠审查（本轮）

### 三项检查

| 检查 | 命令 | 结果 |
|---|---|---|
| **strict** | `openspec validate add-windows-local-project-access --strict` | **valid** |
| **仓库 delta / 接缝** | `check_change_deltas.py add-windows-local-project-access --conflict-baseline scripts/conflict-baseline.txt` | **OK**：deltas 与基线一致，每个冲突文件都被覆盖 |
| **活动 change 重叠** | 与 6 个活动 change 逐个比对改动文件 | 见下 |

### 重叠审查结果

**发现一处需要注意的**：`harden-windows-desktop-reliability`（37/55，**仍活动**）与本次改动**共享文件**，且它的 design 明确写了自己的接缝约束：

> 复用当前 `execution_capability.py` 的组合态、metadata/hello 与宿主能力查询…**不新增第二套能力快照、缓存或授权状态机**；只在某个错误链路确实丢失必要字段时补兼容字段。

**核对结论：未违反。** 我在 `execution_capability.py` 加的是 **v3 分支**（`execution_state_v3`、`execution_state_for`），**共用** `_acceptance` / `helper_usability` / `_setting_*` 等既有读取——**没有第二套快照、缓存或状态机**，与此前 v2 报告的仍是同一条链路。

**迁移编号**：`auth/store.py` 当前最高是 **migration 50**，**由本次改动新增**。逐个查过其余活动 change 声明的迁移编号，只有 `add-shared-agent-release-governance` 提到 `48`，而它 **0/36 未开始**。**无编号冲突。**

**其余交集**（`.gitignore`、`Cargo.toml`、`access.py`、`client_files.py` 等）属**追加式**改动，无同一行的竞争。

### 本任务点名的两件事都已遵守

- **不改历史 change 完成状态**：本次只改 `add-windows-local-project-access` 的 `tasks.md`，未触碰任何其他 change。
- **不提前同步主规范**：`openspec/changes/` 之外**未改动任何 spec**；delta 仍留在 change 内，等归档时再同步。

### 未实现的项仍不勾选

按任务原文「未实现不勾选」，本文件里所有未实现任务（1.1–1.3、3.5、4.2–4.5、5.1–5.4、6.1–6.2、7.x、8.x）**保持未勾选**。本轮只勾 **6.3**，因为它的内容是**执行三项检查并记录**——已全部执行。

**进度：10/37。**
## 撤回 6.3 的勾选（本轮，自我纠正）

### 我上轮勾错了

6.3 的原文是三项：

1. 执行 strict、仓库 delta/接缝检查和活动 change 重叠审查
2. **记录最终证据/任务状态**
3. 未实现不勾选，不改历史 change 完成状态或提前同步主规范

我上轮只做了**第 1 项**就勾了，理由是"6.3 的内容就是执行三项检查并记录"。

**这个理由站不住**：第 2 项要求的是**最终**证据与任务状态——而本 change 有 28 项未完成、处于进行中，**"最终"这个状态现在根本不存在**。把「进行中的阶段记录」当成「最终记录」，与原文「未实现不勾选」是同一类错误：**勾选表达的是"这一条要求的产物已经存在"，而它还不存在。**

**已撤回。** 进度回到 **9/37**。

### 这一轮同时发现的事实

6.3 真正的完成条件是**本 change 收尾**时——那时才有"最终证据/任务状态"可记录。它应当与 8.5（终局声明）同期完成，而不是现在。

**记录于此以免再勾**：6.3 的依赖不是"三项检查跑过"，而是"**本 change 到达终态**"。

## 4.4 的两半（本轮完成其一）

### 双语来源：**已确认齐备，无需新建**

先核实了「双语」指什么：界面文案的**简中 / 繁中 / 英文**三份，集中在 `channel/web/static/js/i18n/core.js`。

| 检查 | 结果 |
|---|---|
| 本机文件相关的键（`ws_local_*`） | 每个键 **3 份**（`ws_local_file_changed`、`ws_local_helper_missing`、`ws_local_read_only` 各 3 次出现） |
| 快照夹具 | `tests/fixtures/console_i18n_snapshot.json` **覆盖**这些键 |
| 本次改动是否引入新文案 | **没有**——`git diff` 显示只改了两个 js/ts 文件，未新增用户可见字符串 |

所以 4.4 的「双语」那半**本就完整**，我的改动没有让它缺项。

### 数据流说明：**本轮新建**

新增 `docs/desktop-local-project-dataflow.md`。

**为什么不是补进既有文档**：我先查了 `docs/desktop-remote-deployment.md`——它是部署文档（进程分离、nginx、升级回退），承载数据流会把两件事混在一起。

**文档的核心是把两条链路分开**——这是最有价值的对照，也是混同最容易出错的地方：

| | **模型链路** | **面板链路** |
|---|---|---|
| 谁发起 | 模型工具调用 | 用户在编辑器点保存 |
| 执行者 | `agent/desktop_local/worker.py`（Python 子进程） | `fs-guard` / `project-fs-guard`（Rust 原生） |
| 工具 | 六个 | 只有读与写 |
| 版本校验 | 帧带 `expected_version` 时写前阻断 | 读返回 `version`，保存带回 |

其余各节：模型链路每一跳的性质（**模型不能指定目录**）、面板链路为何 `version` **必须由设备给**（合同 `note_not_checksum`）、**写入之后什么才算产物**（六种情况逐一列出，含"`bash` 写了文件也不算"）、**没有默认整目录上传**、撤权与失效、以及隔离边界的**当前真实状态**（Job 稳定 20/20；AppContainer 13–16/20 未授予项目访问权）。

### 4.4 是否勾选

**不勾选**。它的条款里还有一条未核：**"复用工件/预览/显式传输接缝"**的逐项确认我只做了部分（`device_artifacts` / `materialize` 读过，`transfer` / `publish` 的接缝复用未逐条对）。双语与数据流两半已完成。
## 4.4 **完成**（本轮）

### 逐条对照

| 4.4 条款 | 依据 |
|---|---|
| 复用**工件**接缝 | `artifact.ts` 的 `buildArtifact` 是**唯一实现**，由 `device-execution.ts` 引用；`test_desktop_device_artifacts.cjs` **加载该模块**（`dist/main/project-execution/artifact.js`）驱动 16 项 |
| 复用**预览**接缝 | `preview.ts` 的 `planLocalPreview` 被**两处共用**：`remote-container-ipc.ts`（自定义 scheme handler）与 `host-bridge.ts`；`test_desktop_isolated_preview.cjs` **加载该模块**驱动 27 项 |
| 复用**显式传输**接缝 | `materialize-transport.ts` **实现** `materialize.ts` 定义的 `MaterializeTransport` 接口（依赖注入接缝）；`test_desktop_materialize.cjs` 引用它 2 次驱动 17 项；服务端 `test_desktop_transfer.py` 用**真服务**（`xfer_mod.service_for`）11 项 |
| 写入后验证真实文件及版本才发布引用 | `a successful write emits one desktop artifact naming the real file`、`the source version is the content digest, not the file name`、`a frame whose file never reached disk emits no artifact`、`a write that a failed run never completed emits no artifact`、`a write run that failed after writing the file emits no artifact`、`a bash frame emits no artifact, even when it really wrote a file` |
| 跨项目历史 / 离线 / 恶意 HTML / 无真实产物 / 显式上传 / **不默认整目录上传** | 逃逸四例（符号链接指向项目外、经符号链接目录、绝对路径越界、目标是目录）；`test_client_files_materialize_committed_offline`；预览在隔离 scheme 下；`a commit with no artifact reference is a failure, not a silent success`；**`no read op touches the upload transport`** |
| 更新双语来源和数据流说明 | **双语**：`ws_local_*` 各键**各 3 份**（简中/繁中/英），`console_i18n_snapshot.json` 覆盖；本次改动**未引入新文案**（`git diff` 确认）。**数据流**：本轮新建 `docs/desktop-local-project-dataflow.md` |

### 「复用接缝」这条的核实方式

关键不是"有测试"，而是**测试驱动的是接缝本身、而不是平行实现**。所以逐个查了：

- 每个接缝**只有一个实现**（`artifact.ts` 1 处引用、`preview.ts` 2 处**共用**、`materialize-transport.ts` 实现 `materialize.ts` 的接口）
- 每个套件**加载真模块**（`artifact.js`、`preview.js`、`materialize-transport`），不是替身

**没有发现平行副本**——即"复用"是真的，不是声称的。

### 数据流文档的核心

把**两条容易混淆的链路分开**：

| | 模型链路 | 面板链路 |
|---|---|---|
| 执行者 | `worker.py`（Python） | `fs-guard` / `project-fs-guard`（Rust） |
| 工具 | 六个 | 只有读与写 |
| 版本校验 | 帧带 `expected_version` | 读返回 `version`，保存带回 |

并写明 `version` **必须由设备给**（合同 `note_not_checksum`：客户端算不出来）、**写入后什么才算产物**（六种情况）、以及隔离边界的**当前真实状态**（Job 稳定 20/20；AppContainer 13–16/20 且未授予项目访问权）。

**进度：10/37。**
## 4.5 的最后一环 + 修掉一个真实回归（本轮）

### 4.5：helper 崩溃端到端注入 —— 补上了

**先摸清了「重启恢复」的真实机制**：`LocalReadAssembly.ensureGuard()` —— guard 报告关闭时，**下一次调用会重新 spawn 一个**。这就是文件 helper 的**全部**恢复路径，而**此前没有任何测试碰过它**。

所以一条回归（崩溃后 `isClosed` 仍为 false，或继续用死进程）只会表现为「**在应用重启前所有读取都失败**」。

**新测试**（`test_desktop_local_read.cjs`，进程级、驱动真二进制）：

1. 开 guard、`openRoot`，断言未关闭
2. **kill 掉子进程**（模拟崩溃）
3. 轮询等 `isClosed` 翻转——**若它不翻转，调用方会继续用一个死进程**
4. 用**旧 grant** 调用 → 必须失败（`helper_exited`），**不能挂起**
5. 新 guard 能服务同一目录 —— **这才是关键性质**

**结果：通过（423 ms）。**

### 顺带发现：套件里的二进制重建**静默失败**

跑完后该套件显示 **25 项 / 5 skip**（此前 24 项 / 1 skip）。查下来：`fs-guard.exe`（10:50）比源码（16:37）旧，套件里的 `ensureBinary()` 用 `cargo build --release --offline` 重建，但**异常被 `catch { return false }` 吞掉**，于是 `hasBinary = false`，**需要二进制的测试全部 skip**。

**这不是我这轮引入的**（该逻辑一直在），但形状值得记：**构建失败伪装成"跳过"，而跳过在输出里看起来像正常**。手动重建后 25/25 全过，跳过归零。

### 修掉一个**真实回归**：Windows 上 `stat` 无法报告目录

`test_desktop_materialize.cjs` 的 `a directory is refused, and a path escape never reaches the transfer` 失败：目录 `'nested'` 得到 **`path_outside_root`**，而期望 **`not_a_file`**。

**根因是两个后端不等价**：

| | unix | 我的 Windows 实现 |
|---|---|---|
| 打开对象 | `O_RDONLY`——**目录也能开** | `as_directory=false` → **`FILE_NON_DIRECTORY_FILE`**，内核**拒绝目录** |

`stat` 的职责**就是报告对象类型**，却要求内核先限定它是文件——于是目录这一半答案不可达。而 unix 的 `stat` 在生产（macOS）上是用着的，所以这是**我的 Windows 实现与它不等价**。

**修法**（把「不限定形状」变成可表达的意图）：

- 新增 `Access::ReadEither`：读权限，**不主张**对象是文件还是目录
- 新增 `Shape { Directory, File, Either }` 取代 `open_component_at` 的 `bool`；`Either` **两个 `FILE_*_DIRECTORY_FILE` 标志都不设**
- 新增 `resolve_any()`（Windows 走 `ReadEither`，unix 直接委托 `resolve`）
- `ops::stat` 改用 `resolve_any`
- `stat_at` 原本是「先按目录试、失败再按文件试」两次打开——改为**一次 `Shape::Either`**：两次打开是**两次让对象变化的机会**，而内核一次就能回答

**实测**：`stat('nested') -> kind=dir`、`stat('a.txt') -> kind=file`。

### 验证

| 检查 | 结果 |
|---|---|
| `cargo test --release`（只读） | **47 + 27 通过** |
| `cargo test --release --features write-ops` | **73 + 73 + 27 + 5 通过** |
| `test_desktop_materialize.cjs` | **18/18**（修复前 17/1 失败） |
| `test_desktop_local_read.cjs` | **25/25**（含新增的崩溃恢复） |
| `test_desktop_local_files.cjs` | **12/12** |
| `test_desktop_device_artifacts.cjs` | **16/16** |

### 4.5 是否勾选

**不勾选**。它还有一条：**"在途脚本按 D6 处理"**——属脚本层，未实现。其余条款（撤权、退出、旧结果隔离、文件 grant 失效、重启恢复、helper 崩溃、待确认写入与重新选择、不靠放宽 ACL、私有暂存按 D3、无需管理员/Python）现有依据。
## 5.4 历史 skip → W/S 编号映射（本轮）

### 两个历史计数不是同一件事——这一点必须先说清楚

acceptance.md 第 53 行说「harden 记录的 `test_desktop_local_execution.cjs` **23 项**、`test_desktop_project_source.cjs` **17 项**是历史计数，不是当前硬门槛」。

**实测这两个数字现在状态完全相反**：

| 套件 | 历史 skip | 当前实测 |
|---|---|---|
| `test_desktop_local_execution.cjs` | 23 | **仍是 23 skip**（36 通过） |
| `test_desktop_project_source.cjs` | 17 | **0 skip，38 全绿** |

**17 项已经全部解决**；**23 项一个都没解决**，因为它们**依赖 macOS 的 `sandbox-exec`**，在 Windows 上不可能不加改造地跑。

### 23 项逐项映射

分组依据是每一项依赖的 **OS 原语**，而这正是 harden 的延后说明里写明的技术根因（Windows 缺 `dir_fd`/`O_NOFOLLOW`、无进程组/SIGKILL）。

| # | 组 | 测试名（`test_desktop_local_execution.cjs`） | 原 skip 原因 | Windows 等价测试 | W/S 编号 |
|---|---|---|---|---|---|
| 1–5 | **A26** OS 边界 | `A26: Python os.open on a dynamically built outside path is refused`；`A26: a symlink inside the project does not become a way out`；`A26: a grandchild subprocess inherits the same boundary`；`A26: project, temp directory and runtime still work normally`；`A26: a temp file can actually be created inside the run` | `!(onMac && seatbelt)`：需要 `sandbox-exec` 提供内核级边界 | Rust：`open_component_at` 的 `..`/reparse/硬链接拒绝单测；JS：`test_desktop_local_read.cjs`（25/25）；`test_desktop_device_artifacts.cjs` 的符号链接逃逸四例 | **W02–W04、W08** |
| 6–8 | **A27** 秘密/缓存/网络 | `A27: no secret from the launcher environment reaches the worker`；`A27: identity data and main-process config outside the project are unreadable`；`A27: a skill cache is readable but cannot be modified`；`A27: the network is unreachable from inside the sandbox` | 同上；沙箱负责隔离秘密与网络 | `test_desktop_local_execution.cjs` 的**环境擦洗**用例（非 skip 部分）；`test_script_launcher_isolation.py` 的环境白名单 | **W07、W15** |
| 9–14 | **A17** 进程树与预算 | `A17: a non-zero exit is reported as the real exit code`；`A17: a large drain is truncated with a stated reason`；`A17: a background job started by a run does not outlive it`；`A17: a backgrounded child does not outlive the run`；`A17: a script may signal its own children, and only its own`；`A17: a tool past its budget is stopped, not left running` | 需要 POSIX 进程组 `setsid`/`killpg` | **`script-launcher` 的 Job Object**：`test_script_launcher_isolation.py` 的 `a wrapped tree is ended when the wrapper is killed`（子进程派孙进程，包装器结束后孙进程必须无法完成）；`--wrap` 20/20 | **W05–W07**（文件部分）、**S06/S11**（脚本部分） |
| 15–17 | 其余三处直接 skip | `a confined worker reads and writes the project`；`the sandbox, not a string check, blocks a write outside the project`；`a directory name cannot widen the sandbox, however hostile it is` | 同上（沙箱既做边界又做隔离） | 第三条的**注入形态**在 Windows 无对应（它利用 macOS 文件名允许引号/换行去破坏 profile 文本）；前两条由 `test_desktop_local_read.cjs` + `test_desktop_materialize.cjs`（18/18）覆盖 | **W05、W08** |
| 18 | 打包运行时 | `installed: the runtime comes from the bundle, and the build machine is not named` | 需 `COW_A26_BACKEND` 指向打包后的 `resources/backend` | **本机已实测**：`release/win-unpacked/resources/` 内含两个 helper，且 `fs-guard-binary.ts` 的查找与之匹配（任务 5.1） | **W16**（仅部分） |

### 关键结论

**23 项一个都不能在 Windows 上直接跑通**，这不是可以靠"取消 skip"解决的：它们验证的是**我们这边没有的 OS 机制**（`sandbox-exec`、`setsid`/`killpg`）。acceptance.md 的态度也明确——**"mac 专属测试及旧 v2 Windows 拒绝仍保留，新增 v3 正向测试，不直接移除平台 skip 冒充真实执行"**。

所以映射的结论是：**这 23 项在 Windows 上由另一批 v3 正向测试承担**，即上表的"Windows 等价测试"列。它们**不是同一批断言的移植**，而是**同一目标的另一实现**（Job Object 取代进程组、句柄锚定取代 `dir_fd`/`O_NOFOLLOW`）。

### 我核实的与我没核实的

**核实了**：两个套件的当前 skip 数、23 项的分组与各自依赖的原语、每一组的 Windows 对应测试**存在且通过**。

**没核实**：逐条一对一（23 项 → 23 条）的**覆盖充分性**。上表是按组对应的，不是逐项对应——例如 A26 的"孙进程继承同一边界"在 Windows 上**没有等价测试**，因为 Windows 的边界是 Job（进程树）而非文件系统沙箱，两者**不是同一件事**。这一点必须在阶段二（脚本层）用真机验收补上，不能靠本表勾选。
## 5.4 的其余条款 + 一个抖动测试（本轮）

### 5.4 不勾选

`tasks.md` 的 5.4 比 acceptance.md 第 53 行的映射要求更宽：

> 回归旧客户端/服务器、POSIX 原只读和 v2、普通聊天/上传、原记忆与 scheduler 边界；验证未协商工具不进入旧路径，文件授权不赋予后台任务或脚本能力。

**映射那一半本轮完成**（见上节）。**回归那一半未做**——那一批需要跑旧端/服务器与本机 POSIX 断言，属"回归"性质，且其中"文件授权不赋予脚本能力"依赖脚本层（阶段二）。故不勾选。

### 顺带发现一个抖动测试，已定位性质

`test_desktop_project_watch.cjs` 的 `a nested addition is reported at the directory that gained it`：

| 跑法 | 结果 |
|---|---|
| 隔离单独跑 | **通过** |
| 套件内跑（首次） | **失败** |
| 套件内跑（连跑 3 次） | **2 次全过、1 次失败** |

**失败形态**：期望 `changed === ['output']`，实得 `['', 'output']`——即**根目录与嵌套目录都报了**。

**性质**：**抖动，非确定性**。断言依赖 `fs.watch` 在**同一个 tick 内**把两次写入合并、并只报最深目录；这在 Windows 上不保证——根目录与 `output` 的事件可能落在相邻两次 tick 里，于是一个 tick 报根、下一个报 `output`，而测试只取一次 tick。

**我未改动 watcher**（本轮只改了 `fs-guard` 的 `stat` 打开意图与新增读套件的崩溃恢复测试），且失败可复现于同一套件的不同运行——即**与本次改动无关的既有抖动**。

**我没有"修"它**，因为修法取决于一个**产品决策**：监视器应当**合并**成最深目录（测试的期望），还是**如实报告两个**目录（实现的行为）？两者都说得通，而选哪一个会改变面板收到的事件。**这需要你定，我不替你定。**

**当前口径**：该用例计入"已知抖动"，与 `test_desktop_remote_config.cjs`、`test_desktop_downloads.cjs`、`test_desktop_local_root.py` 同列。
## 修正：管道**不是**根因，AppContainer 本身才是（本轮，推翻我上一轮的结论）

### 我上一轮的结论错了，错在哪里

上一轮我报告「**根因是管道**」，依据是这张对照：

| 模式 | 句柄 | 稳定性 |
|---|---|---|
| JSON 模式 | **管道** | 13–14/20 |
| `--wrap` 模式 | **继承标准句柄** | 20/20 |

**这个对照有两个变量**：既换了句柄类型，**又换了 access 模式**（JSON 那次是 AppContainer，wrap 那次是 Inherited）。我把差异归给了管道，但真正变化的是 **AppContainer**。

### 正确对照（单变量）

| 配置 | 管道 | access | 结果 |
|---|---|---|---|
| JSON | **有** | Inherited | **20/20** |
| JSON | **有** | **AppContainer** | **16/20** |
| `--wrap` | 无 | Inherited | **20/20** |

**管道在场时 Inherited 是满分**。所以**管道无关**；唯一与失败相关的变量是 **AppContainer**。

### 本轮同时做的：探针加 `--pipes` 模式

给 `probe-appcontainer` 加了 `--pipes`（真实 `CreatePipe` + `bInheritHandles=1` + `STARTF_USESTDHANDLES`），想把「探针 20/20」与「launcher 不稳定」的差异定位到探针代码里。

**探针在这个模式下提前退出**：它只打印了 `mode:` 与 `CreateAppContainerProfile hr=0x800700B7`（`ERROR_ALREADY_EXISTS`，即 profile 已存在的正常态），随后进程结束、**没有打印 `CreateProcessW ->` 那一行**。原因是我给 `--pipes` 配了**空环境块**（`block_storage` 的 `match` 没有 `--pipes` 分支，落到 `_ => Vec::new()`），而带 `CREATE_UNICODE_ENVIRONMENT` 传空块会出问题。

**所以这个探针结果不可用**，我没有从中得出任何结论。探针的 `--pipes` 分支**留在代码里但尚未修好**（需给它一个合法环境块），它是一条**仍未走完**的诊断路径。

### 修正后的当前认识

| 已排除 | 依据 |
|---|---|
| 环境块 | NULL vs 真实无差别 |
| `CREATE_NO_WINDOW` | 噪声内 |
| Job 赋值 | 跳过无改善 |
| `lpDesktop` | 已按需设置 |
| **管道** | **Inherited 在有管道时 20/20** |
| `HANDLE_LIST` 接线错误 | 正确实现后仍不改善 |

**唯一确认相关的变量**：`access = app_container`，约 **16/20（80%）**。

**下一步该问的问题变了**：不再是"哪个句柄设置错了"，而是"**为什么同一个 `SECURITY_CAPABILITIES` 属性有时被采纳、有时不被采纳**"。已知 `ERROR_NOT_APPCONTAINER`（4250）与 `ERROR_GEN_FAILURE`（31）交替出现。

### 本轮验证

| 检查 | 结果 |
|---|---|
| `cargo build --release`（launcher） | **0 错误** |
| `cargo build --release --bin probe-appcontainer` | **0 错误** |
| JSON + Inherited | **20/20** |
| JSON + AppContainer | **16/20** |
| `--wrap` | **20/20** |

### 教训

**我的对照实验有两个变量，而我把它读成了单变量。** 这不是测量错误（数字是真的），是**归因错误**——我把"两个配置的差异"当成了"某个配置项的差异"。

**写在这里以免再犯**：对照实验必须确认**只有被试变量在动**。
## 探针的当前状态与一个必须记住的陷阱（本轮）

### 决策：**不使用探针的失败率数字**

我修好探针的 `--pipes` 环境块后跑基准，得到：

| 模式 | 结果 |
|---|---|
| 探针 `--env`（无管道） | **0 / 20**，**每次都 error 203**（`ERROR_ENVVAR_NOT_FOUND`） |
| 探针 `--pipes`（有管道） | **0 / 20**，同样每次 203 |
| **launcher，JSON + AppContainer（有管道）** | **15–16 / 20** |

**探针现在是恒定坏的**（20/20 全败），而 launcher 在同一 AppContainer 下是 75–80% 成功。所以：

**探针不再代表 launcher 的这条路径，它的失败率不能用来推断 launcher 的行为。**

### 这一点为什么重要

探针的**用途**是「区分失败在 OS 还是在我的接线」。它现在**恒定失败于 203**——一个**环境块**错误码——而它的 `--env` 分支**确实设置了 `SystemRoot`/`windir`/`=C:`**。也就是说：**探针自己现在有一个环境块缺陷**，在它能重新代表该路径之前，它的任何"works/refused"结论都不可用。

**我没有继续修它**：修探针会让它恢复成一个**已知不可靠的**对照物（历史上给过 20/20，现在给 0/20，而 launcher 一直是 75–80%），继续投入它换取的信息不如**直接改进 launcher**。这条诊断路径**到此封存**，留待需要时重开。

**写在这里以免误用**：`desktop/native/script-launcher/src/bin/probe-appcontainer.rs` 的当前输出**不可作为 launcher 的证据**。

### 本轮同时排除：管道（再一次，用不经过管道的方式）

上一轮我改正了「管道是根因」的错判。本轮用**另一种**方式再确认一次——把 launcher 的输出**重定向到文件**（不经任何管道），并同时测两种 access：

| access | 结果 |
|---|---|
| **Inherited** | **20 / 20** |
| **AppContainer** | **15 / 20** |

**同一条命令行、同一种输出方式**，唯一差别是 access。**管道彻底排除**。

### 修正后的状态

| 项 | 状态 |
|---|---|
| 唯一确认相关的变量 | `access = app_container`，约 **75–80%** |
| 交替出现的错误 | `ERROR_NOT_APPCONTAINER` (4250)、`ERROR_GEN_FAILURE` (31)、`ERROR_ENVVAR_NOT_FOUND` (203) |
| 已排除 | 环境块、`CREATE_NO_WINDOW`、Job 赋值、`lpDesktop`、**管道**、`HANDLE_LIST` 接线 |
| 诊断助手 | **已封存**（探针本身恒定失败于 203，不可作对照物） |

### 下一步（写给接手者，未经尝试）

不要再调句柄或环境——两者都已排除。剩下的假设只有一类：**`SECURITY_CAPABILITIES` 属性本身的传递方式不可靠**。要验证它，应当换 API 而不是换参数：

1. **`CreateAppContainerToken` + `CreateProcessAsUser`**：先从 AppContainer SID 建**受限令牌**，再用该令牌启动进程。这是 Windows 上为"以 AppContainer 身份启动"提供的**直接**途径，而 `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 是随 `CreateProcess` 走的一条**间接**途径。若前者稳定、后者不稳定，那么结论就是"用前者"，而不是"继续修后者"。
2. 若仍不稳定，再考虑**最小原生 C 程序**（不经 Rust、不经本 crate 任何代码）复现，以彻底切开"我的实现"与"OS 行为"。

**注意**：方向 1 需要新增 `Win32_Security_Isolation` 之外的能力（令牌创建与 `CreateProcessAsUser`），是一次**接口替换**，不是参数调整。
## 脚本主干接线：endpoint 从「没有调用者」到「真正启动并作答」（本轮）

### 修掉的断点

`startLocalExecutor`（`desktop/src/main/local-execution/executor-endpoint.ts`）**没有任何调用者**。于是：

- Python 侧 `script_executor.executor_endpoint()` 恒为 `None`
- `run_script` 恒返回 `REFUSAL_SCRIPT_NOT_ISOLATED`（「本机没有可用的启动器」）
- **脚本一次都跑不起来**，且没有任何启动失败日志——因为**没人尝试启动**

`COW_DESKTOP_EXECUTOR_URL` / `_TOKEN` 这两个常量在 TypeScript 里定义了（`launch-token.ts`）却**从未被设置**，与 Python 侧读取的变量名一致却无人填写。

### 改动

| 文件 | 改动 |
|---|---|
| `local-execution/executor-lifecycle.ts`（新增） | 启动/关闭 endpoint，并把 URL+token 暴露成后端环境变量 |
| `python-manager.ts` | 新增 `setExtraEnv(provider)`，在 spawn 的 env 中合并 |
| `index.ts` | `startBackend` 中**先**启动 endpoint、注入 env、**再**启动后端；`before-quit` 中关闭 |
| `executor-endpoint.ts` | 新增可选 `wrapper`，脚本 worker 也走 launcher（Job Object） |

### 为什么必须在这个顺序

后端**从自己的环境变量读** endpoint，并在首次使用时**缓存**——而进程环境在 spawn 时固定。所以「先启动后端再启动 endpoint」会让后端**永久**认为没有启动器。这不是偏好，是顺序约束。

### 为什么 grants 不需要顺序处理

`remoteGrantRegistry` 是 `local-files-bridge.ts` 的**模块级单例**，endpoint 查询的就是面板与读取路径用的**同一份**授权，不是副本。所以启动之后新确认的项目，脚本运行**立刻**可见。

### 端到端验证（真实进程，不是桩）

同一次运行内：启动 endpoint → 用真实 Python 子进程、真实环境变量调 `run_script`。

| 环节 | 修复前 | 修复后 |
|---|---|---|
| Python 读 endpoint | `None` | **`ExecutorEndpoint(url='http://127.0.0.1:64865', token='tok')`** |
| `/desktop-exec/script` | 从不被调用 | **被调用并作答** |
| `run_script` 的拒绝 | 「本机没有可用的启动器」 | **endpoint 自己的原因** `unsupported_platform` |

**关键区别**：不再是「没有启动器」（那是接线缺失），而是 **endpoint 明确说「Windows 上没有已验证的执行边界」**——即**接线通了，剩下的是那个刻意的平台闸门**。

### 回归

| 套件 | 结果 |
|---|---|
| `test_desktop_local_executor_endpoint.cjs` | **16/16** |
| `test_desktop_local_execution.cjs` | **36/36** |
| `test_desktop_execution_wiring.cjs` | **2/2** |
| `npx tsc --noEmit -p tsconfig.main.json` | **0 错误** |

### 还没做的一步（需要决策，不是技术问题）

`local-execution/sandbox.ts:173` 的 win32 分支**刻意**返回：

```
unsupported_platform —— Windows has no probed execution boundary in this build...
It is not a permissions problem: the isolation this app relies on has not been verified on Windows yet.
```

**这是设计上的闸门，不是缺陷**。打开它意味着在 Windows 上声明「本机脚本可执行」，而当前真实强制能力是：

| 边界 | 状态 |
|---|---|
| **进程树 + 资源上限（Job Object）** | **已实现且稳定**（实测 20/20；孙进程随包装器结束有专门测试） |
| **文件系统访问边界（AppContainer）** | **不稳定**（13–16/20）**且尚未授予项目目录访问权**（7.2 未做） |

所以打开闸门后，脚本将**以用户自身权限**运行——能读写用户的任何文件。这与契约「绝不宣称本构建不具备的强制能力」并不冲突（只要 capability 描述如实写明），但**是一次真实的安全姿态变更**，且 D8 说 AppContainer 失败应「修订设计」，因此**必须由用户决定**，不由实施者代拍。