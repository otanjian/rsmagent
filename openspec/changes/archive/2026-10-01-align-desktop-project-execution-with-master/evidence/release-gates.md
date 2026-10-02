# 迁移、灰度与回退（任务 11.1—11.4）

本轮只做**能在本机真实复现**的那一半：迁移编号与新旧数据回读、默认关闭的两个开关与交集报告、升级不静默扩权、A32 的在途/完成/unknown 回退演练。安装件内（10.1—10.3）与 Windows 仍未验证，**不在此文件里作完成性主张**。

三份用例与一份变异脚本：

| 文件 | 覆盖 | 结果 |
| --- | --- | --- |
| `tests/test_desktop_project_migration_drill.py` | 11.1（迁移编号、新旧回读、原子性、本机根不入库） | **17 通过 / 44 subtests** |
| `tests/test_desktop_release_gates.py` | 11.2 + 11.3（开关默认、`desktop_local_files` 含义、交集报告、不静默扩权） | **18 通过 / 8 subtests** |
| `tests/test_desktop_execution_broker.py::RollbackDrillTests` | 11.4（A32 回退演练） | **7 通过**（该文件 46 通过） |
| `evidence/scripts/mutate_release_gates.py` | 上述断言的变异验证 | **10/10 命中**（日志 `release-gates-mutations.log`） |

回归：`node --test tests/test_desktop_*.cjs` **741 通过**；`pytest tests/test_desktop_*.py tests/test_mutation_evidence_scripts.py` **1276 通过 / 924 subtests**；构造 `WebAppHarness` 的 39 个文件 + 身份/迁移子集 **953 通过 / 1 跳过**（唯一失败 `test_external_connections_api.py::test_a_tenant_catalogue_read_needs_the_read_permission` 在**变更前**同环境同样失败，属既有缺陷）。

---

## 一、11.1 迁移编号与新旧数据回读

### 1.1 编号

本次 change 在既有 head 40 之上**顺序**追加 41—44，没有跳号、没有复用：

| 版本 | 内容 |
| --- | --- |
| 41 | `desktop_workspaces.project_mode`，`NOT NULL DEFAULT 'readonly-input'` |
| 42 | `desktop_commands` 的 17 个 v2 列 + `desktop_execution_permits` 表 + 3 个索引 |
| 43 | `desktop_process_handles` 表 + 3 个索引 |
| 44 | `desktop_commands.skill_resources` |

**v2 没有第二套业务表**：命令仍写 `desktop_commands`、仍走同一套 CAS 状态机与 outbox，新表只有「单次使用启动许可」与「设备侧后台句柄」两条**记录**。用例 `test_no_second_schema_for_the_v2_rows` 直接断言不存在 `desktop_execution_commands` 之类的平行表，并断言 `desktop_execution_permits` 确实外键指向 `desktop_commands(id)`。

### 1.2 回读：旧行必须读成**旧的东西**

把库冻结在 v40（施加迁移 1—40 并写标记，然后只由一次 `IdentityStore` 打开去补 41—44），再读回：

- 变更前的只读目录授权 `project_mode` 读成 **`readonly-input`**，而不是被默认值悄悄升成执行授权；`test_the_upgrade_invents_no_execution_grant` 断言升级后**不存在任何** `project-execution` 行的集合。
- 变更前的命令读成**v1 帧**：`protocol_major = 1`、`cancel_requested = 0`、`tool_name` / `run_id` / `params_digest` / `execution_phase` / `journal_id` / `permit_id` / `skill_resources` 全 `NULL`，而它原本的 `op` / `state` / `params_sha256` 一字不变。
- 设备、绑定、授权注册表行**保留原 id**：`installation_id` 不轮换、`client_version` 不被改写、`grant_version` 不变，且 `revoked_at` 仍为 `NULL`（`test_registry_rows_survive_with_their_identity`）。安装标识是「同一台机器跨版本」的唯一凭据，被重建就等于把用户的笔记本换成一台新设备。

### 1.3 「既有产出/消息不重写」怎么证

不断言「迁移只碰 desktop_* 表」，而是**把话说全**：升级前后把**每一个非 desktop 表**的全部行 dump 出来逐字段比对（`test_business_rows_are_not_rewritten`）。先种入 tenant / user / membership / session 等代表性业务行，快照 `{表: [行...]}`，升级，再快照，两次必须完全相等。这比读 SQL 文本强：它连「某条 UPDATE 顺手改了 created_at」都会抓到。

### 1.4 「本机绝对根不主动同步到服务器元数据」

用例先**验证检测器本身有效**：往同一张库的一个文本列里写一个哨兵字符串，再在整份文件里搜到它（`test_the_search_finds_a_string_that_is_there`）——否则一个空的或编码不对的 `not in` 断言会永远通过。然后注册一个含中文与空格的真实目录（`/Users/drill/我的 项目 A`），断言该字符串与其片段**都不在 `identity.db` 里**，并进一步在 schema 层拦一道：这四张表的**任何列名**都不许带 `path` / `root` / `dir` / `folder` / `abs_` 这类词（`test_no_new_column_is_a_place_to_put_a_path`），让未来新增的列必须自己论证一遍。

### 1.5 发现的既有缺陷：迁移体与标记不在同一个事务（已修）

回读用例第一次跑就红了，而且红在一个**当时并没打算测**的性质上：模拟「迁移体成功、标记未写」的半状态后，下一次打开库**永远打不开**（`duplicate column name: project_mode`）。

根因不在迁移 41，而在框架：

```
con.execute("BEGIN")
_migrations[version - 1](con)   # 体里几乎都是 con.executescript(...)
con.execute("INSERT INTO schema_migrations ...")
con.commit()
```

`sqlite3.Connection.executescript` 在跑脚本**之前会先提交当前事务**（实测 Python 3.14：`executescript` 之后 `in_transaction` 立刻是 `False`）。于是体的 DDL 落在**自己的**事务里并立即提交，标记落在第二个事务里。进程若在两者之间被杀，库就停在「结构改了一半、没有任何记录」的状态；下次启动重放该体，撞上 `duplicate column name` / `table already exists`，**因为这个状态而永久打不开**。

这不是本次新引入的写法问题：44 个迁移里有 **29 个**（含 `CREATE TABLE` 与 `ADD COLUMN` 两类）在这条路径上都不可重放。所以按根因修，而不是逐个打补丁：

- `_MigrationConnection`：交给迁移体的连接把 `executescript` **按完整语句逐条 `execute`**（用 `sqlite3.complete_statement` 切分，所以 `CREATE TRIGGER ... BEGIN ... END;` 不会被 `;` 切两半——这类触发器真的存在）。语法、顺序、事务都不变，只是不再隐式提交。其余属性一律委托给真连接。
- **没有用 `autocommit = False`**：它同样能解决问题，但该属性 **Python 3.12+** 才有，而 `desktop/build/build-backend.sh` 明确优先用 **Python 3.11** 打包，`pyproject.toml` 还写着 `requires-python = ">=3.7"`。用 3.12 专属 API 会在出货机上直接崩。
- `_add_missing_column`：`ADD COLUMN` 前先查 `PRAGMA table_info`。这是**纵深防御**，作用对象是**已经被旧版本弄成半应用**的真实机器——它们不会因为框架被修好而自己恢复，只会在重放时再次撞上 `duplicate column name`。用例 `test_a_store_left_half_applied_by_an_older_build_still_opens` 手工造出那个状态并断言能自愈。
- 迁移 41—44 改成用 `con.execute` / `_add_missing_column` 写，本身就在框架事务内，不依赖上面的包装。

`EveryMigrationIsAtomicTests` 对**全部 44 个版本**逐个 subTest 验证：让体在跑完后抛异常，标记必须未写，且**库必须仍能打开**。修复前这条会红 21 个版本（R1 变异日志即为该形状）。

`_split_sql_script` 用 `sqlite3.complete_statement` 而非 `split(';')`：R2 变异（朴素切分）会让触发器语句残缺，直接打断建库。

---

## 二、11.2 默认关闭与「只报交集」

- 两个开关 `desktop_project_execution_enabled` / `desktop_project_scripts_enabled` 在 `config.available_setting` 里**都是 `False`**；**不认识的取值一律当作关闭**（`_as_bool('maybe')` 为假），与 v1 三个阶段开关同一套约定；键缺失时回落到声明默认值而不是 `None`。
- **`desktop_local_files` 含义不变**：slice 仍声明 `read` / `transfer` / `publish` 三个动作、`implemented` 与 `accepted` 都为真、`enabled` 为真；meta 的 `features` 只读 `desktop_local_files_enabled`，`_FEATURE_SLICES` 里**不出现** v2 两个开关的任何一个（`test_it_is_not_gated_on_the_v2_switches` 同时断言键集合与读出的可用性）。这一条是本任务最容易做错的方向：新增一个更强的能力时顺手把既有的只读面收窄或改名，是**回归**而不是收紧。
- **报告是交集**，不是任何一个输入：`implemented × accepted × configured` 的 8 种组合逐一验证 `available == 三者之与`，并验证原因判序为 `not_implemented` → `not_accepted` → `disabled_by_deployment`。判序错位的后果很具体：把「还没验收」说成「被开关关掉」，会让运维去找一个根本不存在的配置项。R4 变异正是调换这个判序，被抓。
- **开关单独翻不动它**：即使两个开关都置真，`execution_state` 仍报 `not_accepted`，files/scripts 两个面都不可用（R5 变异把合成块改成「只看开关」，被抓）。
- **验收单独也开不了门**：`availability` 报可用是「报告」，而 handler 读的是 slice 的 `enabled`（= `open` 非空）。`desktop_project_execution` / `desktop_project_scripts` 的 `open` 都是空集，所以**当前授权**下没有任何动作被打开；`test_acceptance_alone_does_not_open_the_handler_gate` 同时断言这两件事，把「报告」与「授予」分开钉住。
- 消费者标签说实话：两个 v2 消费面报 `not_accepted`（代码在、批次未验收），而 `desktop_local_processing` 这种代码确实缺失的报 `not_implemented`——两者的区别正是这个标签存在的意义。

---

## 三、11.3 升级不静默扩权

「旧用户升级后必须先明确打开本机项目才能获得执行授权」在两个方向上都可能被绕过，两个方向各有用例：

1. **旧记录被读强**：升级前落盘的 `ExecutionTarget` 没有 `project_mode`。解析器给的默认值是**更弱**的 `readonly-input`（`from_dict`），于是 `allows_project_execution` 为假。进一步验证**后果**而不只是字段：该 identity 既不被委派（`remote_mode_for` 为假，即便开关是开的），也拿不到本机目录（`run_local_cwd` 返回 `(None, REFUSAL_UNAVAILABLE)`）。`project_mode` 取未知值（如 `project_execution`）会被**拒绝**而不是默认化，防止拼写差异被当作可接受。
2. **凭空造出授权**：设备上「已知项目 / 最近项目」不是授权。用例断言一个**从未被显式打开**的会话在 `project_store` 里查不到 target（`get_execution_target` 为 `None`），因此会话仍跑在服务器侧——这正是「最近候选不是授权」的可检查形式（A02/9.6 的判定同源）。
3. **标识符不是路径**：`workspace_id` 恰好长得像一个路径时，解析表只会**查不到**，绝不会把标识符当目录拼出来。用例注册一个真目录，然后用**该目录字符串**当 `workspace_id` 去解析，断言拿不到 root（`test_a_path_shaped_identifier_resolves_to_nothing`）。同时断言 target 序列化后只有标识字段。
4. 注册表**比对 `grant_version`**：重选目录必然递增版本，旧授权因此立即失效；R10 变异去掉这层比对（旧授权复活）被抓。升级本身不撤销也不重建注册表行（§1.2）。

---

## 四、11.4 A32 回退演练

`RollbackDrillTests` 先用**真实创建路径**造出三个命令（设备 claim → ack → prepare → start → heartbeat 走真接口）：一个 `running`（带 journal 与 permit）、一个 `succeeded`、一个 `phase=outcome_unknown`；前两个终态结果也由真设备结果帧产生。然后关掉开关，逐条核对：

| 断言 | 用例 |
| --- | --- |
| 新调用被封锁：`prepare` / `start` / `heartbeat` / `status` 四条路径**都**以 `503 feature_unavailable` 拒绝，且是在任何授权判断之前 | `test_closing_the_switch_stops_new_calls` |
| 一个都不删、一个字都不改：`desktop_commands` / `desktop_execution_permits` / `desktop_process_handles` 三张表在这些命令上的**全部列**关闭前后逐字段相等 | `test_closing_the_switch_deletes_and_rewrites_nothing` |
| 在途效果与回执保留：`journal_id`、`permit_id`、`started_at` 原样 | `test_the_in_flight_row_keeps_the_journal_and_permit_it_started_with` |
| 重开后回答不变：running 仍是 running 且带「先看原文件与 journal、不要自动重跑」的 `reconcile`；succeeded 仍 succeeded 且**没有** `reconcile`；unknown 仍 `outcome_unknown` 且 `effects=unknown` | `test_reopening_restores_the_same_answers` |
| 未知状态**绝不**被自动重跑：连查两次 `status` 得到同样的未知答复，行快照与 outbox 行数都不变（没有「补发一次」） | `test_the_unknown_row_is_never_auto_rerun` |
| 降级客户端能解释：meta 报 `available=false`、`reason=disabled_by_deployment`，开关回来即恢复可用 | `test_a_degraded_client_is_told_which_reason_applies` |
| **不改投服务端**：开关关闭时既不委派（`remote_mode_for` 为假），本进程又解析不到本机目录时 `run_local_cwd` 返回**拒绝**而不是某个服务器目录 | `test_the_rollback_never_routes_the_work_to_the_server` |

最后一条是本任务真正要防的「省事写法」，R8（委派不再看开关）与 R9（解析不到就退回 `.`）两个变异分别打在它的两半上，都被抓。

**边界**：原文件与设备侧 journal 在**设备上**，本进程碰不到，所以本文件的主张严格限定为「服务器侧不删、不改、不补发，且不把工作改投服务器目录」；「在途脚本真的还在跑、且恢复后能接着跑」需要真实设备，属 10.1—10.3。关闭开关时 broker 全部路径一律 503（包括 `status`）是一个**取舍**：它让客户端立刻看到「这个部署不提供项目执行」这个可解释的原因，代价是关闭期间服务器侧也不回答问题；行本身没有丢，重新打开即可原样读回（上表第四行）。

---

## 五、变异对照（10/10 命中）

| 变异 | 走样 | 抓它的用例 |
| --- | --- | --- |
| R1 | 迁移体退回原生 `executescript` | `EveryMigrationIsAtomicTests`（21 个版本逐个 subTest 失败） |
| R2 | 脚本用朴素 `split(';')` 切分 | `test_fresh_store_applies_every_migration_once` 等 5 项 |
| R3 | `ADD COLUMN` 不判断存在 | `test_a_store_left_half_applied_by_an_older_build_still_opens` |
| R4 | `availability()` 判序调换 | `test_the_reason_names_the_first_missing_condition` 等 4 项 |
| R5 | 合成块只看开关 | `test_a_switch_flip_alone_never_opens_the_composed_block` |
| R6 | 拆掉 broker 合成闸门 | `test_closing_the_switch_stops_new_calls` 等 2 项 |
| R7 | `status()` 不认 `outcome_unknown` | `test_the_unknown_row_is_never_auto_rerun` 等 3 项 |
| R8 | 委派不再看开关 | `test_the_rollback_never_routes_the_work_to_the_server` |
| R9 | 解析不到就退回服务器目录 | `test_the_rollback_never_routes_the_work_to_the_server` |
| R10 | 注册表不比对 `grant_version` | `test_version_bump_invalidates_the_old_root` |

**一个必须写下来的坑**：变异脚本自带的失败解析正则写成了 `::(?:[\w.]+\.)?(\w+)`，只允许 `模块.类` 分隔，而 pytest 的 subTest 失败行是 `SUBFAILED(version=1) path::类::用例` —— **双冒号**。结果 R1 实际红了 21 项，脚本却只认出 1 项，看起来像「这条变异只影响一个用例」。已改为允许 `::`，并把这条教训写进 `failed_py_tests` 的 docstring。

---

## 六、未完成（不作完成性主张）

- 11.1 的「安装件内真实升级」：本机只验证了 store 层迁移与回读；**随包 Python 3.11 上的实机升级**属 10.1—10.3。实现刻意避开了 3.12+ 的 `autocommit`，但没有在 3.11 解释器上实跑过（本机未安装 3.11）。
- 11.2 的 `accepted=True` 只由**测试补丁**驱动；把它真正置真是 10.x 验收通过后的动作，本轮**没有**修改任何 `accepted`。
- 11.4 的设备侧一半（原文件仍在、journal 仍在、恢复后能继续）需要真实设备。
- Windows 全部未验证。
- 既有缺陷（本次未引入、本次不修）：`tests/test_external_connections_api.py::test_a_tenant_catalogue_read_needs_the_read_permission` 在变更前的 commit 上、相同环境下同样失败。
