# Tasks

阶段门槛未通过前不得进入下一阶段；「接口占位」「本机可跑」不作为门槛通过。

## 1. 固定版本与准备（阶段 0）

- [x] 1.1 确认目标为 `rdai`、来源为 `origin/master`，记录源/目标/共同祖先 SHA 与本地额外提交范围到 `refs.txt`
- [x] 1.2 建立独立克隆（`git clone --no-hardlinks`），另设真实远端，`fetch origin master rdai`，复核 `origin/rdai` 是目标 HEAD 祖先
- [x] 1.3 从确认的目标 SHA 创建同步分支 `codex/sync-master-to-rdai-<时间戳>`，启用 `rerere`，记录上游提交清单与 `git diff --stat/--name-status`
- [x] 1.4 在克隆内按候选声明安装依赖（含测试依赖），确认 `.venv` 与测试依赖可用；记录环境与命令
- [x] 1.5 运行一次排练（`scripts/sync-from-master.sh`）作为迁移前基线，保存日志并确认排练后工作树干净、无 `MERGE_HEAD`
- [x] 1.6 盘点并记录「迁移前」的 route coverage、接缝测试与权限隔离测试结果，作为阶段 1 的对照基线（见 `evidence/01-phase1-baseline.md`）

## 2. 后端 fork 定制迁出（阶段 1，行为保持）

- [x] 2.1 枚举 `channel/web/web_channel.py` 中 fork 专有符号清单并登记目标归属：实测 290 个模块级符号（79 个 handler + 134 个私有 helper + 其余管道/常量），跨文件边界引用仅 9 处（见 `evidence/02-fork-symbol-map.md`）
- [x] 2.2 建立 fork 授权模块 `channel/web/fork/authorization.py`，迁入请求上下文/作用域辅助（`_db_scope`、`_current_db_identity`、`_authorized_model_codes`、`_web_runtime_identity_snapshot`、29 个 `_require_*`/`_authorize_*` 等），逐字复制
- [x] 2.3 归位授权判定：实测 56/64 个上游 handler 的方法体内**交织** fork 授权与数据作用域（证据 03），原 D2「三层分工」前提不成立 → 按修订后的 D2，fork 在 `channel/web/fork/` 中平行承载实现；路由级与对象级策略层沿用不改
- [x] 2.4 将 79 个 handler 迁入 `channel/web/fork/handlers/<view>.py`（17 个视图模块，镜像上游 `api/` 划分；含 15 个 fork-only 与 64 个 fork 平行实现），逐字复制
- [x] 2.5 ~~为上游 handler 建立 fork 子类~~ **已废弃**：子类覆写对 56 个 handler 等价于复制方法体，收益为零；改由 D2 的平行承载 + hub 接缝达成同一目的
- [x] 2.6 路由权威清单跨模块解析：入口模块 `web_channel.py` 保留 `_WEB_URLS` 与全部 handler 名，`check_route_coverage(vars(web_channel))` 与 `web.application(_WEB_URLS, globals())` 两处解析点均不变；`route_registry.py` 无需改动
- [x] 2.6a 保留入口模块的既有命名空间契约：原 49 条模块级 import 逐字保留、`globals().update(_SCENE_HANDLERS)` 复原、`WebChannel`/`SERVING`/`SSEStreamState`/`WebMessage` 可经 `web_channel` 解析
- [x] 2.6b hub 接缝：fork 模块对「被其它模块经 `web_channel.<name>` 解析/打桩的名字」在函数体内经入口模块惰性解析（实证见 `evidence/04`），使既有接缝语义与全部既有测试保持成立；**迁移提交内测试文件零改动**
- [x] 2.6c 修正 hub 判定遗漏的打桩形式：首轮全量回归暴露 22 处行为失败，根因是 hub 判定只识别 `web_channel.<name>` / `from … import <name>`，未识别 `patch.object(web_channel, "<name>")` / `setattr(web_channel, "<name>")` 的字符串实参形式，导致打桩静默失效。已扩展判定并重生成（见 `evidence/05-verification.md`）
- [x] 2.6d 迁移中唯一的非逐字改写：把 6 处 `os.path.dirname(__file__)` 相对资源路径改为锚定 `channel/web` 的 `_WEB_ROOT`；改写规则与理由记录在 `scripts/migration/emit_fork_web.py` 与 `scripts/migration/README.md`
- [x] 2.7 ~~补齐 `channel/web/fork_routes.py`~~ **以等价方式满足**：fork 路由全部登记在权威清单 `route_registry.py` 内（`source=fork:*`，实测 108 条 / 221 个方法项），无需另设扩展模块；`_load_fork_extensions()` 钩子保留，供未来独立的 fork 路由模块使用
- [x] 2.8 编写/更新测试，断言迁移后的授权结果与迁移前一致（同一请求同一判定），覆盖合法成员正向、跨租户拒绝、跨 owner 拒绝、administrator 治理边界。**采用更强判据：既有行为测试零改动地全部通过**，等价于同一套断言与打桩点在迁移后仍成立
- [x] 2.9 运行并记录阶段 1 门槛：`scripts/check-route-coverage.py`（176 路由 / 221 方法项，OK）、`tests/test_route_registry.py`、`tests/test_upstream_core_seams.py`、`tests/test_no_resurrection_legacy_identity.py`、`tests/test_identity_resource_authorization.py`、`tests/test_http_policy.py`；并做迁移前后全量对照（Python: 33→27 失败、0 新增；Node: 54→46 失败、0 新增）。结果见 `evidence/05-verification.md`
- [x] 2.9a 修正读源码文本的结构性护栏：9 处因代码迁出而失败的断言，改为读「整个 web 层」而非单个入口文件（`tests/_helpers.py::web_layer_source`、`tests/_web_layer.cjs`），避免日后模块再拆分时护栏静默失效
- [x] 2.9b 加固两处**迁移后静默变空**的护栏：`tests/test_channel_signature_seam.py`（原读 `web_channel.__file__`，现为不含方法体的入口模块）与 `tests/test_no_resurrection_legacy_identity.py::test_legacy_auth_helpers_are_absent`（原只在入口模块内搜已退役 helper）；并扩展 `tests/test_route_registry.py` 的「无手写路由字面量」检查覆盖 fork 模块
- [x] 2.10 提交阶段 1 迁移提交（fork 自有提交，非 merge），确认可独立回退；迁移前后对照证据见 `evidence/04-phase1-backend-migration.md` 与 `evidence/05-verification.md`
- [x] 2.11 迁移后重跑排练（`scripts/sync-from-master.sh origin master`），与迁移前冲突清单逐项比对：45 → 46 个冲突文件，仅新增 `tests/test_qianfan_provider.py`（测试重定向的必然结果，`web_channel.py` 本身仍冲突但已从 8409 行单体内战变为 627 行 vs 177 行的可复核组合），fork 实现 686 KB / 22 个模块完全退出冲突面（见 `evidence/06-rehearsal-after-phase1.md`）

## 3. 吸收上游（阶段 2，merge commit）

- [x] 3.1 检查阶段 1 证据齐备后，以固定 `$MERGE_SOURCE_SHA` 执行 `git merge --no-ff --no-commit`；记录冲突清单与 `git ls-files -u`
  - 排练（`evidence/13`、`evidence/16`）：隔离克隆 `/tmp/merge-20260919-020927/repo`，源 `8f1b19f1`、目标 `163951b5`（本分支），共同祖先 `e5e2a52d`；先 `git rerere` 关闭以取**真实**冲突集（否则第一次解完就再也看不见同一批冲突，「复现基线」无从谈起）
  - 实测 **46** 处冲突（基线 21 行全部命中、无一消失）；前一轮的中间快照见 `evidence/10`
  - **真实提交已生成**：`163951b5 merge: sync master into rdai`，第一父 `65596a99`、第二父 `8f1b19f1`，树 `07244685…`；`git ls-files -u` 为空
- [x] 3.2 引入上游 `channel/web/api/**` 与 `channel/web/core/**`，确认 `web_channel.py` 收敛为 URL 表 + `build_app()`，且不含业务 handler 实现
  - D8 结论：入口模块现在**定义 0 个** `*Handler` 类（`grep -c '^class .*Handler'` → 0），只有两张 URL 表、`_upstream_namespace()`、`build_app()`、`build_web_app()` 与 fork 的 patch 兼容别名；fork 的 79 个 handler 类在 `channel/web/fork/handlers/*.py`（与上游 `channel/web/api/*.py` 按模块名 1:1 对应）
  - 上游 `URLS` 与 `build_app()` 按字面保留（`build_app()` 必须存在：上游新增的 `channel/web/core/channel.py:1507` 会 `from channel.web.web_channel import build_app`），上游类只经 `_upstream_namespace()` 的**惰性** import 进入，绝不进本模块 `globals()`
  - 见 3.2a / 3.2b 两条落地记录与 `evidence/11`、`evidence/12`
- [x] 3.2a 按 D8 改造入口模块：加入上游 `URLS`（逐字提取自 `origin/master`，非手抄）与 `build_app()`、`_upstream_namespace()`（惰性 import 上游 `api/*`，按 `URLS` 引用的名字逐个取类，缺名显式报错而非回落到 fork 同名声）；fork 路径完全不受影响（`build_web_app()` 仍以 `_WEB_URLS`+本模块 `globals()` 构建，实测 352 项 URL 表正常）
  - 惰性 import 是硬约束而非优化：分支上 `api/**` 尚不存在（随 merge 引入），且 fork 的线上路径不得在 import 期依赖上游栈
  - **连带发现（重要）**：`tests/test_route_registry.py::test_core_files_no_longer_carry_route_literals` 按名禁用 `'/api/health', 'HealthHandler'` 字面量，而上游 `URLS` 逐字包含它——该护栏原把「整个 web 层」等同于「fork 的 web 层」，在吸收上游后会因上游的**合法**代码失败
- [x] 3.2b 据此精确化护栏作用域并把断言改强（不放宽）：`tests/_helpers.py::web_layer_source()` 默认排除上游被逐字采纳的 `channel/web/api/**` 与 `channel/web/core/**`（新增 `include_upstream=True` / `upstream_web_layer_source()` 供确需全层的断言使用）——否则「fork 已退役共享口令登录」这类护栏会被上游**合法保留**的口令登录代码判失败
  - 字面量禁令改为**逐模块正则**匹配任意 `'/…', 'XHandler'` 手写对（比只认一个已知字符串更强），作用域排除入口模块（其承载上游 `URLS`）与 `route_registry.py`（即清单本身），上游 `api/`、`core/` 不在 fork 的管辖范围
  - 新增 `test_upstream_url_table_is_verbatim_and_separate`：以解析后 `(pattern, handler)` 对的 sha256 钉住上游表（重排字面量不受影响、改任一路由/名字/顺序即失败），并断言 `build_app()` 用 `URLS`、`build_web_app()` 用 `_WEB_URLS`（两栈 64 个同名 handler，混用即静默错栈）
  - 非空验证：改一条路由、把 `build_app()` 换成 `_WEB_URLS`、在 fork 模块手写 URL 表，三处诱因分别精确失败于预期测试（见 `evidence/12-route-table-guardrails.md`）
- [x] 3.3 逐项处置 46 处冲突（任务书写的是 45，实测 46）：`seam:` / `keep-fork` / `merge-docs` / `keep-deletion` / `retarget` 各按基线登记，逐路径记录双方意图、最终行为与采用的接缝 —— 见 `evidence/13`（第一轮 30 处）、`evidence/16`（第二轮）、`evidence/19`（桌面凭据接缝）、`evidence/18`（handler 增量缺口）；`scripts/conflict-baseline.txt` 每行都带 disposition，且已按本轮 tip 重新冻结
  - 三条 `seam:` 行的接缝归属（`scripts/check_change_deltas.py --conflict-baseline` 的覆盖判据要求本 change 点名每条 seam 路径）：
    - `agent/memory/conversation_store.py` → `seam:conversation-store`：上游的全局 `agent_id` 存储对 fork 的租户复合键，靠 `_dimensions()` + `ALWAYS_SCOPED_DIMENSIONS` 分层，**不是**两侧取一（`evidence/14`）
    - `agent/tools/scheduler/integration.py` → `seam:scheduler`：上游的单一全局服务对 fork 的授权，`AgentScopedTaskStore` 保持按 Agent 的调用形状（`evidence/16`）
    - `tests/test_scheduler_web_update.py` → `seam:scheduler`：同一接缝的测试面，按「一个全局 store、任务自带 Agent」重写（`evidence/16`）
  - 该检查器的 disposition 词表原为 `{keep-deletion, keep-fork, merge-docs}`，与 D7 扩展后的基线语义不符（会把 `merge` / `retarget` / `take-deletion` 报成拼写错误），同批修正并补测试（`tests/test_change_delta_check.py`，21 passed）；seam 引用同时接受模块名（`seam:scheduler`）与既有的任务号形式（`seam:8.3`），只有裸 `seam:` 才算格式错误
- [x] 3.4 复核并处置四个 README 的 `keep-deletion`、`PermissionSelector.tsx` 的 `keep-deletion`，以及新增的反方向 `DU`（见 4.3）——不得对文件内删除使用 `keep-deletion`
  - 四个 README + `PermissionSelector.tsx`：均确认 `keep-deletion`（理由见基线末尾的 DELIBERATE_REMOVALS 段）
  - 新增反方向 `UD`（上游删、fork 改）三行：`desktop/build/notarize-dmg.sh` → `take-deletion`（上游 `e3674f89` 退役且删净引用，fork 侧唯一改动是注释里的品牌字样，无能力损失）；`console.js` / `console.css` → `keep-fork`，且基线显式写明「Phase 3（4.4h）完成前不得按删除处置」
  - 另有两条新 `DU`（`channel/web/README.md`、`channel/web/static/vendor/README.md`）→ `keep-deletion`：前者描述的是**上游**入口模块（「URL 表和别的什么都没有：77 条路由」），与 D8 的入口模块不是一回事，故不随之纳入
- [x] 3.5 逐项检查**无冲突文件**的上游增量：路由、HTTP 方法、任务字段、通知语义、凭据响应与请求传输，确认未被静默丢弃
  - 方法（可重跑）：对「两侧自 `e5e2a52d` 起都改过」的全部 348 个文件跑 `git diff --numstat HEAD origin/master`，非零即「上游有、HEAD 无」的候选；未被静默丢弃的判定落在逐行归属，而不是冲突数
  - **路由 / HTTP 方法**：`scripts/check-route-coverage.py` → `176 routes (68 upstream, 108 fork), 221 method entries, OK`；`tests/test_route_registry.py` 的覆盖不变量（登记的每个方法在 handler 上确有实现）通过，故无方法因合并丢失。仅上游有的 12 条路由见 `evidence/21` §E（一键更新、scheduler runs/create/recipients/instances、session context_usage/compact_context、SPA 深链 catch-all），均属拆分后前端，登记为随 4.3/4.4 落地
  - **任务字段 / 通知语义**：`agent/tools/scheduler/task_store.py::list_tasks(enabled_only, agent_id)` + `effective_task_agent_id` 过滤 + `_DescStr` 的 created_at 倒序（`87706bee`）在 HEAD 在位；残留计数是 fork 自己的加固（revision 冲突、`TaskWriteLease` 多写者拒绝），上游无对应物。投递语义见 `evidence/21` §C/§H
  - **凭据响应与请求传输**：桌面令牌链见 `evidence/19`；实例凭据解析见 `evidence/21` §B4/§C/§H；微信公众号上游回退共享凭据文件的那处按 fork 加固保留（§H）
  - 逐项裁定表：`evidence/21`（§B 已移植、§C fork 加固、§D 同名等价、§E 未路由、§H 非 web 层）
- [x] 3.6 保留上游新增行为与安全约束，至少包含：上传预览按所选 Agent 限定、仅读 body 的路由的 Agent 解析、飞书群消息提及门控、QQ 文件接收与 Markdown 回复、钉钉收文件、知识库空状态、ASR 模型取配置值
  - 上传预览按所选 Agent 限定：`_scoped_agent_id(params)` 已接入 `files.py` 的 `UploadHandler.POST` / `VoiceAsrHandler.POST` 与 `knowledge.py` 的 `KnowledgeImportHandler.POST`（multipart 时 query string 优先，与 JSON body 路径同源），有 `tests/test_fork_multipart_agent_scope.py`
  - 仅读 body 的路由的 Agent 解析：`_request_agent_id` / `_scoped_agent_id` 在 `channel/web/fork/runtime.py` 定义并经 `channel.web.web_channel` 再导出；`tests/test_upload_agent_scope.py`、`test_web_multipart_agent_scope.py` 已指向 fork 实际服务的处理链
  - 飞书群消息提及门控：在位 —— `feishu_channel.py:549` 的 `_is_mention_bot` 与 `:740-751` 的群门控（覆盖 `text` **与** `post`，且区分「有 mentions 但只@了别人」），合并未削弱
  - QQ 文件接收与 Markdown 回复：`channel/qq/qq_channel.py` 与 `origin/master` **逐字节相同**，`msg_type=2` 的 Markdown 发送 + 被拒时的纯文本回退 + 原始文件名保留全部在位
  - 钉钉收文件：在位 —— 单聊与群聊两个 handler 都有 `ContextType.FILE` 缓存分支（`file_cache.add(..., file_type="file")`）与 `ctype is None` 守卫，另有「下载失败即丢弃并告警」的负路
  - 知识库空状态：**随拆分前端落地，非本阶段可移植** —— 上游该修复（`cbe14fd1` / `d081f65d`）只改 `static/js/views/knowledge.js`，而 fork 的 shell 尚不装载该模块（4.3/4.4）；不得为它去改 fork 单体 `console.js`（违反「不原地编辑上游视图模块」的反向要求）。已在 `evidence/21` §H 显式登记
  - ASR 模型取配置值：已在 `models.py::_set_asr` 移植（LinkAI 的 `voice_to_text_model` 置空走配置默认值），`tests/test_models_handler.py` / `test_web_search.py` 覆盖
- [x] 3.7 保留 fork 侧 `_import_local_file` 的 loopback 与每启动令牌校验，确认未因合并被移除或放宽
  - 复核结论与任务书假设**相反、但结论更强**：该路由与校验都不是 fork 代码，而是上游新增的 `channel/web/core/channel.py::_import_local_file` 与 `channel/web/core/_common.py::_desktop_token_matches()`，且合并树上这两个文件与 `origin/master` **逐字节相同**（无 fork 分支混入，符合 4.7 判据）；`_desktop_token_matches()` 在 `COW_DESKTOP_TOKEN` 缺省时 **fail closed**
  - 因此校验既没被移除也没被放宽；它只在**上游命名空间**（`build_app()` + `channel/web/api/**`）里被服务，fork 线上路径（`build_web_app()`）的 `/upload` 解析到 `channel/web/fork/handlers/files.py::UploadHandler`（只有 `POST`，无 `local_path` 分支），故桌面端不持有令牌是自洽的
  - 桌面侧由此降落为**功能移植项**而非合并项：见 `evidence/19` 的「Post-merge audit」段与 `evidence/18`
- [x] 3.8 逐路径 `git add`，检查暂存内容无无关文件；运行 `git diff --check` / `git diff --cached --check` —— 通过：暂存集仅含合并相关的 342 个文件与本次证据/脚本，`git diff --cached --check` 无告警（曾报 `evidence/18` 文件尾空行的告警已修）；工作树无残留（临时软链 `desktop/node_modules` 仅供 `.cjs` 套件转译用，未暂存即删除）
- [x] 3.9 运行阶段 2 门槛：规范 §6.2 全量基础回归（含 `tests/test_sync_report.py`、`test_conversation_schema_seam`、`test_scheduler_identity_seam`、`test_startup_hook_seam`、`test_channel_signature_seam`、`test_scheduler_web_update`、`test_upstream_drift_guards`、`test_recovered_entry_acceptance`、`test_desktop_auth_flow`）与路由覆盖校验 —— 门槛子集 **138 passed**；路由覆盖 `scripts/check-route-coverage.py` → `176 routes (68 upstream, 108 fork), 221 method entries, OK`；全量回归见 3.11 的验证段
- [x] 3.10 处理本轮 11 处 web 测试漂移：逐文件确认该测试对应的能力已进入目标版本，按新模块位置更新引用；不得删除测试或放宽断言后声称通过
  - 处置方式与逐文件对照见 `evidence/21` §A；判定标准是「该测试断言的对象是否仍是被请求实际执行的那份代码」——`build_app()` 组装的 `channel/web/api/**` 在本 change 后不再被线上服务，控制台走 `build_web_app()` + `channel/web/fork/handlers/**`（D8），因此凡仍 import `channel.web.api.*` 的测试都在断言无人调用的代码
  - 已改指：`test_web_search.py`、`test_chat_model_fallback.py`、`test_web_channel_disconnect.py`、`test_web_console_update.py`、`test_console_channel_manager_resolution.py`、`test_web_multipart_agent_scope.py`、`test_upload_agent_scope.py`、`test_history_agent_workspace.py`、`test_model_catalog_api.py`、`test_route_registry.py`、`tests/_helpers.py`、`test_web_console_assets.py`（该文件仍按 Phase 3 跳过，理由写在文件头）
  - **漂移本身就是探针**：改指后 `test_web_channel_disconnect.py` 的 5 条路由用例立刻失败（fork 对无 `instance_id` 的旧卡片拒绝 disconnect），版本用例的 `update_supported` 断言也失败——两处都不是「测试过时」，而是真实缺口，已在 §B1/B2/B5 移植修好。未删除任何测试、未放宽任何断言；唯一新增的 skip 是上游的「复活」用例，其前置（`bootstrap_legacy_instances` 合成记录）在 fork 的 no-op passthrough 下不成立，理由写在测试内
  - `grep -rl 'channel\.web\.api' tests/` 现在只命中 docstring；`tests/e2e` 不在本轮范围（Phase 0 既定）
- [x] 3.11 生成候选并记录暂存树哈希（`git write-tree`），提交 merge commit `merge: sync master into rdai`，校验第一父为 `$MERGE_TARGET_SHA`、第二父为 `$MERGE_SOURCE_SHA`、树哈希一致
  - 隔离克隆解析后的候选树：`07244685012289d58d5d541b4c6fff632ab4ad21`
  - 工作区分支上的提交：`163951b5`，父为 `65596a99`（rdai 线 + web-split 工作 + 本次证据）与 `8f1b19f1`（`origin/master`），`git rev-parse HEAD^{tree}` 与候选树**逐字节一致**（先用 `git merge --no-commit` 开出 46 处冲突，再 `git read-tree -u --reset <候选树>` 收敛，因此提交树就是被验证过的那棵树）
  - 验证（提交后、同一棵树）：python `30 unique failures / 5717 passed / 33 skipped`，基线（fork HEAD）为 32 failures → **零合并引入失败**，且合并修好了两项基线失败（`test_subagent` 的 shipped guide、知识库租户管理员用例）；node `.cjs` 套件 `47 unique failures`，与基线完全相同（0 merged-only / 0 base-only）；路由覆盖 OK；阶段 2 门槛 138 passed
  - 全量回归在 `tests/` 上运行（`pytest tests`）；对仓库根直接 `pytest` 会同时收集 `Scene/**` 与 `scenes/**` 两套同名测试文件而报 collection error，这是仓库既有结构（两目录均为 fork 既有），与本次合并无关

## 4. 前端模块化迁移（阶段 3，已移交）与基线重生成（阶段 4）

> **移交说明（D9）**：4.1、4.2、4.4a、4.4b 的分析与工具已在本 change 完成，产出（移植器、裁定工作清单、独立校验工具、98 处区域清单）随新 change `adopt-upstream-web-frontend-split` 的 `evidence/` 交付。**4.3、4.4、4.4c–4.4h、4.5 已整体移交该 change**，编号对应关系为 `1.x`（覆盖映射接线）、`2.x`（98 处裁定）、`3.x`（manifest 与门禁）、`4.x`（语法与装载顺序）、`5.x`（删除单体）、`6.x`（验收）。本 change 不再以阶段 3 为交付前置，`console.js` / `console.css` / `chat.html` 在本 change 内按 `keep-fork` 保留。

- [x] 4.1 枚举 `console.js` / `console.css` 中全部 fork 定制，登记为迁移清单（外观、身份管理、待办、场景工作台、外部连接、渠道工作台、品牌、i18n 扩展、片段加载）
  - 证据 `evidence/07-frontend-divergence.md`（原始输出 `07-frontend-divergence.txt`，逐 hunk 明细 `frontend_divergence.json`）
  - `console.js`：365 hunk，+7832 / −2061 行，相似度 0.71；`console.css`：79 hunk，+2606 / −229，相似度 0.71
  - 归一化 diff 是前提：按原样行 diff 会把 `console.js` 报成「2 hunk / 18671 增行」，实际是 fork 改了空白与缩进
  - 定制集中在 7 个上游模块（占增行 84%）：`core/auth.js` 1320、`views/agents.js` 1034、`views/sessions.js` 1029、`views/channels.js` 923、`core/version.js` 894、`core/nav.js` 729、`views/config.js` 653
  - `core/i18n.js` 为反向（+79 / −1295）：fork 把翻译移出到 `static/js/i18n/`，该模块不可按「移植 diff」处理
  - fork 专有文件（`appearance.js`、`identity-admin.js`、`todos.js`、`scenes/`、`external-connections.js`、`channel-workbench.js`、`i18n/`、`fragments.js`、`appearance.css`、`fragments/appearance-dialog.html`）本已是独立文件，不在本次拆分范围内
- [x] 4.2 定位每个 fork 定制所属的上游模块所有者（迁移清单 → 上游模块映射）
  - 判据：以归一化后的**具判识度**行（长度 ≥ 8 且被 ≤ 3 个模块包含）为锚点；短结构行（`}`、`});`）会命中所有模块，首版分析因此给出「36 个模块各 ≈7000 行」的无意义结果
  - 结果：base 行 100% 可映射（JS 14413/14413、CSS 3671/3671），覆盖 33 / 8 个上游模块
  - 可移植性实测：JS 258/365 hunk（70.7%）可机械再锚定，107 处需人工移植；CSS 67/79（84.8%），12 处需人工
  - 人工移植量最大的模块：`views/sessions.js` 21、`views/agents.js` 20、`core/nav.js` 12、`views/config.js` 7、`core/auth.js` 7、`css/sessions.css` 7
- [~] 4.3 **已移交 `adopt-upstream-web-frontend-split`（任务 1.x）** 采用上游 `static/js/{core,chat,views}/*`、`static/css/*`、`chat.html` shell 与 `templates/**`，全部保持未改动；fork 挂载元素按 `seam:` 重新登记
  - 上游 shell 与 `core/template.py` 的 include 语义、按文件 mtime 的 `?v=` 版本戳、`tools/check-load-order.mjs` 门禁一并采用
- [~] 4.4 **已移交 `adopt-upstream-web-frontend-split`（任务 1.x）** 以「fork 拥有模块 + 服务端覆盖映射」实现 fork 前端定制（`evidence/08-frontend-port-strategy.md`）
  - 上游脚本是**共享同一全局作用域的经典脚本**，同名顶层 `const`/`let` 重复声明即 `SyntaxError`（整页白屏），因此**不得**用「在上游模块之后加载并重新声明」的叠加方案
  - 做法：`static/js/fork/<上游子路径>` 与 `static/css/fork/<上游子路径>` 承载 fork 定制；fork 自有页面处理器经上游 `core/template.py` 组装后按覆盖映射替换 `assets/js|css/**` 引用；`boot.js` 仍最后加载
  - fork 专有模块（`todos.js`、`identity-admin.js`、`scenes/` 等）顺序不变，仍在上游模块之后
  - 判据：上游模块零 fork 改动
- [x] 4.4a 编写移植器 `scripts/migration/port_frontend.py`：归一化 diff → 变更簇归属 → 按推导出的落点拼接 fork 原文 → 生成 `static/js/fork/**`、`static/css/fork/**`；可重复运行
  - 实测（`e5e2a52d`..`HEAD` → `origin/master`）：`console.js` 362 簇移植 275、待裁定 87；`console.css` 79 移植 71、待裁定 8；25 个产出 JS 模块 `node --check` 全部通过（18597 + 4169 行）
  - 定位方式为**推导而非搜索**：按 base 切片与上游模块的对齐求出落点。早期版本用 3 行上下文搜索，25 个模块中 9 个 `node --check` 失败（重复 `let`、括号不平衡）——静默错位，故弃用
  - 跨模块边界的 fork 编辑**不切分**：fork 的替换文本是一次编辑，按边界切分会切断语句（实测产出 `function f() { } }`）。此类编辑转人工裁定
  - 每个 splice 应用后校验模块仍可解析，破坏解析即回退并登记，绝不产出坏模块
- [x] 4.4b 生成人工裁定工作清单与独立复核（不得静默丢弃）
  - `build_frontend_adjudication.py` → `frontend_adjudication.json` / `.md`：98 处（JS 87 + CSS 8 + 跨边界 3），跨 23 个模块；47 处附上游同名符号内容
  - `verify_frontend_port.py` 以 **fork 独有行**为准核对：`console.js` 5222 行、`console.css` 1361 行，共 6583 行**零未交代**（3505 已入产出，3078 在清单上）；并自行复跑 `node --check`，不依赖移植器自述
  - **基线已在移交后刷新**：上述 98 处 / 6583 行是本 change 执行 4.4a/4.4b 时（`fork=4cd829ff`）的实测值。收口提交 `c6eb33db` 再次改动了 `channel/web/static/js/console.js`（三个搜索提供方的凭据弹窗接线），故承接方以 `fork=c6eb33db` 重跑后的输入是 **109 处（JS 98 + CSS 8 + 跨边界 3）/ 6616 行**，命令与三元组见 `adopt-upstream-web-frontend-split` 的 `evidence/inherited-state.md`「基线刷新记录」。重跑必须显式指定三个 ref（默认 `FORK_FORK_REF=HEAD` 会随 HEAD 漂移）
  - **已否决「自动整函数移植」**：对 87 处 JS 中的 56 处可机械适用，但会整体覆盖上游同名函数、静默丢弃上游在该函数内的改动，正是规范禁止的失血方向；反向取上游则丢弃 fork 定制。故每处必须人工裁定
  - 证据 `evidence/09-frontend-port-verification.md`
- [~] 4.4c **已移交 `adopt-upstream-web-frontend-split`（任务 2.x）** 逐处裁定 98 个冲突区域（`frontend_adjudication.md`），每处记录 `fork` / `upstream` / `merged` 与理由；裁定结果并入移植器输入后重跑，并以 `verify_frontend_port.py` 复核零未交代
  - 裁定不得整函数照抄：须检查上游在同区域的改动是否携带功能或安全修复，避免以 fork 版本整体覆盖
  - 集中度：`core/auth.js` 16、`views/agents.js` 12、`core/nav.js` 9、`views/sessions.js` 7、`core/i18n.js` 6、`chat/new-chat.js`/`views/channels.js`/`views/skills.js`/`views/tasks.js`/`css/sessions.css` 各 4
- [~] 4.4d **已移交 `adopt-upstream-web-frontend-split`（任务 3.x）** 生成 `static/js/fork/manifest.json`（`{fork_path: {upstream_path, upstream_sha256}}`）并加漂移门禁：上游模块变更后必须失败，使「上游变更需人工重新应用」可检测
- [~] 4.4e **已移交 `adopt-upstream-web-frontend-split`（任务 4.x）** 以 `node --check` 校验全部产出模块，并以 `tools/check-load-order.mjs` 校验 fork 实际装载顺序（装载顺序须在 4.4 覆盖映射接入后才有意义）
- [~] 4.4f **已移交 `adopt-upstream-web-frontend-split`（任务 4.3）** 处置 `static/js/doc-editor.js`、`workspace.js` 与上游 `assets/js/doc-editor.js` 的重叠：若为上游文件的 fork 版则纳入覆盖映射，而非留在 fork 专有清单
- [~] 4.4g **已移交 `adopt-upstream-web-frontend-split`（任务 5.x）** 删除 `console.js` / `console.css`，不留兼容层；确认无上游视图模块（`js/views/*.js`、`js/core/*.js`、`js/chat/*.js`、`css/*.css`）被 fork 原地编辑
- [~] 4.4h **已移交 `adopt-upstream-web-frontend-split`（任务 5.1）** **删除前置条件**：4.4c 的待裁定区域全部完成（移交时的 98 处已在其内刷新为 109 处，见上方 4.4a 备注）。在此之前不得删除 `console.js` / `console.css`，合并也不得对该 `DU` 按删除处置（否则丢弃未迁出的 fork 前端）
- [~] 4.5 **已移交 `adopt-upstream-web-frontend-split`（任务 6.x）** 运行 `.cjs` 与浏览器验收：`node --test tests/test_fork_fragments.cjs`、`node --test tests/test_execution_permission_ui.cjs`，以及登录、上下文切换、流式请求、上传回读、下载预览
- [x] 4.6 为 D4 的上游模块集合与 fork 专有符号集合编写结构不变量校验，且可独立运行并在注入违规时失败
  - `scripts/check-web-module-seams.py`（可 `--root` 指向任意树，故测试能对副本注入违规）：显式声明上游模块集合（`channel/web/api/**`、`core/**`、`web_channel.py`、`README.md`，共 22 个），并报告「位于上游区域却不在声明集合内」的模块——上游新增模块时该集合必须被更新，而不是被静默吸收
  - 三条独立判据：上游视图/管道模块内**不得出现** fork 专有符号（定义或引用皆算）、不得出现 fork 注册块（`register_fork_routes` / `channel.web.fork`）、入口模块**不得定义** fork 专有符号（import 与转出是它的职责，D8）
  - 注入违规验证：`tests/test_web_module_seams.py` 分别注入「上游模块内引用 fork 授权符号」「上游模块内引用 fork 路由 handler」「上游模块内出现 fork 注册块」「入口模块内重新定义 handler」四类，四者全部失败；并验证「无 fork 包时通过」（独立上游形态不误报）
  - 实测：`OK: 22 upstream module(s), 213 fork-only symbol(s), 0 findings`；测试 11 passed
- [x] 4.7 校验不得以关键字（如 `tenant`）为判据；以 `route_registry.py` 的 `fork:*` handler 名与 fork 授权模块公开符号为判据，并验证对独立上游形态不误报
  - 判据为**推导出的 fork 专有符号集合**：`route_registry.py` 中 `fork:*` 行的 handler 名 + `channel/web/fork/{authorization,common,runtime}.py` 与 `fork/handlers/**` 的顶层符号，再减去上游视图/管道模块已定义的同名符号
  - 减法不是便利而是必要条件，否则误报上游自身代码：`_live_channel_manager` 是上游 `core/_common.py` 的符号（fork 只是同名再包装），`ChatHandler` 是 64 个同名 handler 之一——同名是**共同拥有**，不是 fork 分支
  - 入口模块不参与「同名减法」：它是两套栈名字唯一同时在场之处，若让它投票，入口模块内重定义的函数体就会把自己洗进「同名」集合而检查永不触发（该场景有专门的注入用例）
  - 无误报验证：`test_upstream_tenant_vocabulary_is_not_a_finding` 以整棵只有上游代码、且通篇 `tenant` 词汇的树断言通过；真实树亦通过
- [x] 4.8 扩展 `scripts/conflict-baseline.txt` 与 `scripts/sync_report.py` 语义以覆盖「上游删除 / fork 修改」方向，为 `console.js`、`console.css`、`desktop/build/notarize-dmg.sh` 登记「迁移后删除 → 指向替代模块」处置
  - 基线新增 `UD` 状态（上游删除 / fork 修改）并写入三行：`notarize-dmg.sh` 为 `take-deletion`（上游在 `e3674f89` 退役该脚本并删除全部引用，fork 侧唯一改动是 usage 注释里的品牌字样），`console.js` / `console.css` 为 `keep-fork` + 「Phase 3 完成前不得按删除处置」
  - `sync_report.py` 无需改代码：它从不对 `status` 分支（只解析四列并按键比对），故语义扩展落在基线的状态图例与行上；`take-deletion` 而非 `keep-deletion` 的写法使该行**不**触发 `DELIBERATE_REMOVALS` 交叉检查（该常量保持五项不变，task 4.9）
  - `tests/test_sync_report.py` 9 passed
- [x] 4.9 重新运行排练，将实际冲突集与基线比对，逐条登记 24 处漂移的处置；确认 `DELIBERATE_REMOVALS` 五项保持不变
  - 重跑（rerere 关闭以取真实冲突集）：**46 处** = 基线 21 处全部命中、无消失 + 新增漂移 **25 处**（基线原文记的是 24，实测 25）
  - 25 处漂移的处置：7 处 fork 测试被静默改指上游拆分模块 → `retarget`（3.10 同批处理，共 12 个文件）；`agent/admin.py` / `agent/protocol/agent_stream.py` / `agent/registry.py` / `agent/tools/scheduler/task_store.py` / `config.py` / `desktop/package.json` / `preload.ts` / `types.ts` 与 5 个 `docs/**` → `merge` / `merge-docs`；`channel/web/README.md`、`channel/web/static/vendor/README.md` → `keep-deletion`（沿用 9.7 的 README 决策并写明理由）；`console.css` / `console.js` → `UD` + `keep-fork`（Phase 3）；`notarize-dmg.sh` → `take-deletion`
  - 基线已按新的一组 tip 重新冻结：`origin/master@8f1b19f1` × `codex/adopt-upstream-web-split@163951b5`，46 行；用真实冲突集回放，`sync_report.py` 报 46 个「已知冲突」、漂移段为空、`DELIBERATE_REMOVALS` 五项不变
- [x] 4.10 运行 `scripts/check_change_deltas.py`，确认无未被本 change 点名的冲突文件
  - 首次运行报 26 处：检查器的 disposition 词表仍是 `{keep-deletion, keep-fork, merge-docs}`，与 D7 扩展后的基线语义不符（把 `merge` / `retarget` / `take-deletion` 报成拼写错误），且 seam 引用被硬编码为 `seam:[0-9]`（只认任务号，不认模块名）。**这不是基线的问题，是检查器没跟上**：一个「通过忽略输入而通过」的门禁比没有门禁更糟，故修正检查器而非改写基线
  - 修正：词表扩为六项并写明各自适用场景（`take-deletion` 与 `keep-deletion` 分开，正是为了不让前者进入 `DELIBERATE_REMOVALS`）；seam 引用接受 `seam:<模块>` 与 `seam:<任务号>` 两种形式，只有裸 `seam:` 算格式错误；`tests/test_change_delta_check.py` 补 3 条用例（新词表被接受、模块名 seam 通过、裸 seam 被拒），21 passed
  - 结果：`OK (proposed): adopt-upstream-web-split — deltas consistent with the baseline, every conflicted file covered`；三条后端 `seam:` 行的接缝归属已在 3.3 逐条点名；前端 change 亦通过（其中说明它只认领基线的前端三行，见该 change 的任务 0.5）

## 5. database 能力验收与交付（阶段 5）

> **状态说明（2026-09-19 交付；2026-09-19 验收更新）**：5.1–5.3 已执行，逐项证据见
> `evidence/23-database-capability-acceptance.md`；5.4–5.7 仍有未执行部分，逐条保留未勾选。规范 §6.4 要求每项能力同时取得
> 「database 正向业务成功 + 授权隔离通过 + 真实入口可达」三类证据。本轮已在**双租户、多用户、真实
> `build_web_app()`** 上对 §6 能力清单逐项取证：平台/租户平面的 Web 与后端切片三类齐备；依赖外部条件的
> 切片（一键更新决策、控制台前端模块化、Desktop 真实客户端、个人渠道真实执行、真实模型推理）如实保持未通过。
> 因此交付口径更新为「**合并已交付、Web/后端切片能力验收已执行（部分通过）、外部条件切片仍待验收**」，
> 报告中不得出现「master 能力已并入 database 模式」或「全部能力已通过」。
>
> 本轮合并没有新增对外能力：12 条上游未分类路由逐条处置（延后或按 `closed`/策略登记，见 `evidence/21` §E），
> fork 侧授权判定未放宽。

- [x] 5.1 建立独立测试身份库（≥2 租户、多用户，含普通成员与管理员），按规范 §3.3 建立 master → database 能力对照清单
  - 夹具：`tests/test_web_database_capability_acceptance.py` 的 `_ensure_state()`——私有 `identity.db`、
    `acme`（平台管理员）与 `globex`（真租户管理员 + 成员），每租户管理员 + builtin `member`
  - 能力对照清单沿用同步报告 §6，并在 `evidence/23` §2 逐行给出正向/隔离/可达判定
- [x] 5.2 对每项能力取得三类证据：database 正向业务成功、授权隔离通过、真实入口可达；逐项记录候选 SHA、真实路径、成功结果、拒绝结果与日志
  - 26 个用例（`PlatformPlaneAcceptance`/`SearchProviderAcceptance`/`FallbackChainAcceptance`/
    `ModelCatalogAcceptance`/`VersionAcceptance`/`TenantPlaneAcceptance`/`KnownGapAcceptance`）；
    `26 passed`（顺序与随机各一次），逐项判定与未通过项见 `evidence/23` §2/§5
  - 依赖外部条件的行（一键更新、控制台前端、Desktop、个人渠道执行、真实推理）**未**标为通过
- [x] 5.3 覆盖身份/租户/个人资源边界：合法 owner 正向、同租户他人、跨租户、伪造 tenant/owner、管理员治理与私有内容边界
  - 跨租户/匿名/无租户由本轮矩阵直接断言；同租户他人、伪造 tenant/owner、管理员治理与私有内容边界由
    `tests/test_personal_console_multi_tenant_authorization.py`、`test_scope_consistency_acceptance.py`、
    `test_plan_3_1_joint_acceptance.py` 覆盖（同批 `184 passed`）
- [ ] 5.4 覆盖四种装配状态：独立上游形态、完整 rdai、rdai 缺失强制授权扩展、仅缺失可选 UI 扩展
  - 本轮未执行：四种装配形态需要独立构建/装配矩阵，与"合并候选是否可用"是两件事，保持未勾选
- [~] 5.5 覆盖调度与渠道：正常执行、身份/授权失效后拒绝、并发编辑、未知字段保留、入站路由与通知目标
  - 已覆盖：正常执行与授权失效后拒绝（`test_scheduler_task_authorization.py`、`test_recovered_entry_acceptance.py`、
    `test_tenant_channel_isolation_acceptance.py`，全量回归内绿色）
  - 未单列：并发编辑、未知字段保留、入站路由与通知目标沿用既有切片，本轮未做专门验收
- [~] 5.6 按变化追加验证：Web/Desktop 受影响用例，Desktop 变化时 `npm --prefix desktop ci` 与 `npm --prefix desktop run build`，并在隔离测试服务上检查登录、上下文切换、流式请求与文件传输
  - 已覆盖：Web 受影响用例随全量回归与 §6.4 批次执行；`node --test` 前端套件与基线一致（§0.4）
  - 未执行：Desktop 真实客户端构建与演练（承接方 change 任务 0.6/6.5）；这正是 Desktop 行未标为通过的原因
- [~] 5.7 逐项填写提交前检查表（规范 §7.1），确认无「仅存在于 legacy / 仅保留源码 / 整体关闭 / 待验收却标为已完成」的能力；存在缺口时只报告阶段性进展
  - 已确认：注册表 0 条 `closed`；未收口端点如实 404/405；未验收项（Desktop/个人渠道执行/一键更新/前端模块化）保持未通过
  - 未完成：审查人一栏待 PR 评审填写（不在本 change 内自证）
- [x] 5.8 交付前再次 `git fetch origin master rdai` 并与 `refs.txt` 比较；若 `rdai` 前移则整合新目标并重新验证候选
  - `git fetch` + `git ls-remote` 权威复核（2026-09-19）：`origin/master` = `8f1b19f1`、`origin/rdai` = `b5c5090f`，与 `doc/sync-evidence-2026-09-18/refs.txt` 逐字节一致且**均未前移**，故规范 §7.1 的重新验证不成立；被验证过的候选树仍有效，提交树 `07244685012289d58d5d541b4c6fff632ab4ad21` 与之逐字节一致
- [x] 5.9 ~~推送同步分支并向 `rdai` 创建 PR~~ **改为直接合入 `rdai`（用户决定，2026-09-19）**
  - **分支已推送**：`codex/adopt-upstream-web-split` → `origin`（`* [new branch]`），跟踪已建立；该分支后被删除（见下）
  - **PR 正文已备好**：`evidence/22-pr-body.md`（英文，七节齐备）。**本机 `gh` 未认证**（`gh pr create` 报 `gh auth login`），PR 从未创建
  - **实际集成方式**：用户指示不再走 PR，改为本地把 `codex/adopt-upstream-web-split` 合入 `rdai`。该分支相对 `rdai` 为 **领先 250 / 落后 0**，故以 `git merge --ff-only` **快进**合入，无冲突、无新提交：`rdai` 由 `b5c5090f` 前进到 `9133816d`
  - **合并树校验**：`rdai^{tree}` 与分支尖端同为 `f70cdaf6fc99bb7ce5234a0b4e39c758f3590025`，即与刚跑完全量套件（`30 failed / 5775 passed`）的工作树逐字节相同；并在合并树上复跑验收与门禁 `118 passed`
  - **分支清理**：本地删除 `codex/adopt-upstream-web-split`（用 `git branch -d`，即由 git 自身证明已合入）与三个 `backup/lowcode-*`（`-D`，用户明确选择；这 8 个提交不在任何远端，删除前已记录 SHA：`96b2980d` / `0fdd4ce9` / `614f6497`）
  - **交付前的 ref 复核见 5.8**：`rdai` 当时未前移，故合并的 base 是 `b5c5090f`
- [~] 5.10 合入后记录 `rdai` 最终提交，确认固定源 SHA 是其祖先，检查 CI 与冒烟结果；把 `$MERGE_RUN_DIR` 中的证据转存到 PR / CI 制品 / 版本管理目录，不保留临时路径作为唯一证据
  - **已记录**：`rdai` 最终提交 `9133816d`（快进前为 `b5c5090f`），树 `f70cdaf6fc99bb7ce5234a0b4e39c758f3590025`；固定源 `8f1b19f1`（`origin/master`）是其祖先（经 `163951b5` 的 merge commit）
  - **未完成**：推送 `rdai` 到 `origin` 与删除远端工作分支遇到网络故障（`github.com:443` 在 2026-09-19 12:47–12:55 多次连接超时，DNS 正常解析、无代理配置）——本地合并与分支清理已完成，远端同步待网络恢复后重试；CI 与冒烟结果因此尚未取得

## 6. 文档与交接

- [x] 6.1 更新 Web 后端布局说明（模块职责、fork 模块边界、接缝归属），并在其中说明「上游模块零 fork 分支」的判据与校验入口
  - 新增 `docs/design/web-layer-module-layout.md`（随仓库交付）：模块职责与目录树、两个应用工厂与为何必须分命名空间（64 个同名 handler 会静默错误授权）、路由权威清单与三腿不变量、D4 的三条判据、为何不用关键字判据（并说明同名减法为何是必要条件而非便利）、三个校验入口、上游 handler 变更的移植义务
  - 判据与校验入口同处一节，使「上游模块零 fork 分支」不靠文档声明而靠代码事实；`CONTRIBUTING.md` 的上游合并复查清单同步加入 `scripts/check-web-module-seams.py`
- [x] 6.2 记录本轮同步的冲突决策与基线漂移说明，便于下一轮以基线自动取舍；如需要随仓库交付，按规范 §9 对 `doc/` 下的规范文件显式 `git add -f`，不批量强制添加
  - `doc/master合并到rdai-同步报告-2026-09-18.md` 就地更新：新增 §0「交付结果」（固定版本复核、merge commit 两个父与树哈希、46 处处置分类计数、校验结果、前端拆分的范围调整与被丢弃增量的清单指向、§6.4 验收状态），并把 §7/§8/§9/§10 就地改为交付后的事实——保留排练当时的原始陈述作为决策输入，不覆盖
  - 冲突决策与基线漂移的可自动取舍形式落在 `scripts/conflict-baseline.txt`（46 行、六种 disposition）与 `scripts/check_change_deltas.py` 的覆盖校验；`doc/` 被 `.gitignore` 忽略，故按规范 §9 **不** `git add -f`（与既有做法一致：报告与排练证据留在本地记录集，change 的 `evidence/` 随仓库交付）
- [x] 6.3 按规范 §9 保存本轮最低记录集：版本标识、上游变更清单、能力对照与缺口、逐项验收证据、排练日志、冲突决策、候选树/提交、验证结果、审查人、PR 与回滚信息
  - 版本标识与上游变更清单：`doc/sync-evidence-2026-09-18/refs.txt`、`upstream-commits-228.txt`、`upstream-diffstat.txt`、`namestatus`、`github-compare-*.json`
  - 能力对照与缺口：报告 §6（盘点）与 §0.3/§0.6（裁定与验收状态）；逐条裁定见 change 的 `evidence/21`
  - 逐项验收证据：报告 §0.4 的五个校验数字 + §8 的逐项状态；前端验收随新 change
  - 排练日志与冲突决策：`rehearsal.log`、`candidate-merge.log`、`conflict-detail.txt`、基线 46 行
  - 候选树/提交：树 `07244685012289d58d5d541b4c6fff632ab4ad21`、提交 `163951b5`
  - PR 与回滚信息：报告 §9；**审查人一栏待 PR 评审时填写**（当前仅有作者自审，不在本 change 内自证）
