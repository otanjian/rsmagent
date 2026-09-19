# 13 - 阶段 2 首轮合并：逐路径处置

tasks `3.1` / `3.2a` 的延续。本次在**当前 HEAD** 上重开隔离克隆重做合并
（上一次的克隆早于 D8 提交，`web_channel.py` 的解析已过期）。

## 合并上下文

```
目标   e9b9714e  (含 D8 入口模块)
源     upstream/master @ 8f1b19f1
共同祖先 e5e2a52d
克隆   /tmp/merge-20260919-020927/repo  分支 rdai-merge-trial
```

冲突 **46** 个 = 基线 21 个（全部命中，无消失）+ 新增漂移 25 个。已处置 **30**，
剩 **16**。

## 已处置（30）

### 组 1：fork 侧删除 / 上游修改（`DU`，7 个）

全部 `keep-deletion`（基线 D9 的既定决策，逐次复核）：

```
README.md  docs/ja/README.md  docs/zh/README.md  docs/zh/README-Hant.md
channel/web/static/vendor/README.md
desktop/src/renderer/src/components/PermissionSelector.tsx
```

唯一需要**单独判断**的是 `channel/web/README.md`（基线的 4 个 README 之外的新
增 `DU`）：上游这份文档描述的正是本次采纳的 `api/` + `core/` 布局，看起来该
采纳。**结论仍是 keep-deletion**，理由具体：

- 它逐字描述的是**上游的** `web_channel.py`（"the URL table and nothing else:
  77 routes"）。本仓库的入口模块按 D8 同时承载上游 `URLS` 与 fork
  `_WEB_URLS`、`build_app()` 与 `build_web_app()`，**照抄会描述一个本仓库没有的
  文件**；
- 它还把 `/api/scheduler/*` 等路由逐条列出，与 fork 的权威清单
  `route_registry.py` 形成第二份表述；
- 布局图已记录在 `evidence/11-entry-module-composition.md` 与 design D8。

这与基线「Re-decide if the fork ever needs an upstream-style README (then take
upstream's file and edit it, do not resurrect the deletion)」一致：我们**没有**
改而采纳，只是继续不采纳。

### 组 2：上游删除 / fork 修改（`UD`，3 个）

| 路径 | 处置 | 依据 |
| --- | --- | --- |
| `desktop/build/notarize-dmg.sh` | **take-deletion** | 上游在 2.1.8 发布提交 `e3674f89` 退役该脚本，并把公证改为发布流程里的手工步骤；同时删掉了**全部**引用（`.gitignore` 的 `!` 反忽略行、`release.yml` 两处注释、`electron-builder.js` 注释）。合并后的树里 `grep -r notarize-dmg` 已无任何调用点。fork 侧唯一的改动是 usage 注释里的示例文件名改品牌（`CowAgent-` → `容大AI-`），无行为。留一个没人调用的孤儿脚本，只会固化「上游重构过的文件 fork 留一份」的模式。 |
| `channel/web/static/js/console.js` | keep-fork（暂） | 前端单体的整体后置，见下 |
| `channel/web/static/css/console.css` | keep-fork（暂） | 同上 |

`console.js` / `console.css` / `chat.html` 按 task 4.4h 作为**一个 keep-fork
单元**整体保留：它们的最终形态是「删除单体 + 由服务端覆盖表提供
`static/js/fork/**`」，而那依赖 98 处前端裁决（task 4.4c）完成。本次不拆开处置，
否则会留下「删了单体但覆盖表还没接上」的空档。

### 组 3：两侧都改（`UU`，20 个）

| 路径 | 处置 | 具体判断 |
| --- | --- | --- |
| `.gitignore` | 合并两侧 | 上游新增 `.obsidian/` 放在 fork 分区横幅**之上**，fork 分区保持文件末尾——这正是该分区横幅自己写的规则（"keep this section LAST"） |
| `channel/web/web_channel.py` | keep-fork | fork 侧已是 D8 入口模块（含上游 `URLS` 逐字表），信息量覆盖上游侧 |
| `channel/web/chat.html` | keep-fork（暂） | 前端单元，同上 |
| `desktop/package.json` | 合并 | 版本取上游 `2.1.9`，`description` 取 fork 品牌 |
| `desktop/src/main/preload.ts` | 合并 | 上游的 `import * as electron` + `webUtils` 运行时查找（Win7/Electron 22 构建需要）**与** fork 的 `import type {…} from './broker-protocol'`（该文件上游没有，fork 独有）都要 |
| `desktop/src/renderer/src/types.ts` | 合并 | 取上游新增的 `tool_retrieval` 字段组；`tool_end` 的说明保留 fork 的措辞（"permission gate"，与紧随其后的 `permission_denial_kind: "mode"\|"role"\|"isolation"\|"quota"` 同一套词表） |
| `config.py` | 合并 | 上游新增 `default_agent_name` / `agent_bindings` 与更完整的 `agent_max_context_tokens` 说明，fork 的 `personal_assistant_*` 键组全部保留，一组不少 |
| `agent/protocol/agent_stream.py` | 合并 | fork 的「按当轮 actor 重算外部连接工具」块 + 上游订正后的 `tools_schema` 说明（其下方代码正是上游的 `tools_schema = None`） |
| `agent/registry.py` | 合并 | 取上游的可注入内置 agent id（`default_agent_id`、`_AGENT_ID_RE`、`DEFAULT_AGENT_ALIAS` 回退），`name` 回退值用 fork 品牌 `"RongAI"`（该文件 `_profile_from_raw` 已有把 `cowagent` 映射为 `RongAI` 的同类改写） |
| `agent/tools/scheduler/task_store.py` | 合并 | 两侧是**互不重叠的新增**：fork 的 `TaskRevisionConflict` / `MultiWriterDeploymentError` / `TaskWriteLease`，上游的 `_DescStr` 排序助手。已确认上游该文件**没有**自己的 revision/lease 机制，不存在「一问两答」 |
| `app.py` | 合并 | 两个函数都要；**启动顺序**取上游的 `_migrate_conversations()` 在前、fork 的 `_migrate_conversation_tenancy()` 在后——依据是 fork 该函数自己的 docstring：「the fork's tenancy dimension is a *filter* dimension on the store's composed schema… only fills `owner`/`tenant_id` columns and never splits the store per Agent」，即上游先把各 Agent 的库折成一份（加 `agent_id`/复合键），fork 再在这份 schema 上补租户列 |
| `channel/channel_instances.py` | 逐 hunk | 见下 |
| `docs/**`（8 个） | merge-docs | 见下 |

#### `channel/channel_instances.py`：两个 hunk 的处置不同

- **上游的 `_CHANNEL_TYPE_LABELS`：采纳。** 该文件自身有 D4b 分区：
  `UPSTREAM REGION … / PARTITION DIVIDER … / FORK REGION`，并明确写着
  「Upstream-only additions such as `_CHANNEL_TYPE_LABELS` and
  `default_instance_name` belong above the divider below」——即 fork **预留**了
  这两个上游符号的位置。实测该常量并非装饰：合并带入的上游
  `default_instance_name()` 在 `label = _CHANNEL_TYPE_LABELS.get(ctype, ctype)`
  处**确实使用**它。核对后确认常量落在分区线之上（第 99 行 vs 第 562 行）。
- **上游从 `config.json` 凭据自动播种 `channel_instances` 的循环：keep-fork（删除）。**
  fork 的 `resolve_channel_instances` docstring 写明策略：「synthesizes the legacy
  set from `channel_type` **only in legacy identity mode**. In database identity
  mode, `channel_type` alone MUST NOT start channels — only explicit roster/tenant
  registrations run.」该循环会在数据库身份模式下启动没有任何 roster/租户登记
  的通道，与 fork 的核心不变式冲突。

#### 文档（8 个 `.mdx`）：上游内容 + fork 品牌

9 个文档冲突都是同一形状：上游重写正文，fork 只改了产品名/链接。处置统一为
**取上游的新正文并把 fork 的品牌写回**（不是二选一）：

- `architecture.mdx`（en/ja）：取上游句子；图片取上游的 `.jpg`，`alt` 用 `容大AI`。
- `index.mdx`（en/ja/zh）：取上游句子（新增「快速搭建多智能体团队」），产品名用
  `容大AI`。其中 `docs/zh/intro/index.mdx` 的 `description` 行保留 fork 的
  `容大AI 2.0` 写法（上游该行除产品名外未变）。
- `features.mdx`（en/ja）：保留 fork 的文档链接（fork 把上游文档站链接统一指向
  `www.rsm.global/china/zh-hans`，本文件内共 3 处，属既定品牌化），并**追加**上游
  新增的第 6 节「多智能体团队」，正文里的 `CowAgent` 改写为 `容大AI`。

**另外修正一处干净的自动合并造成的回归**：`docs/zh/intro/features.mdx` 未冲突
（上游新增的第 6 节直接落入），因此该节带着未品牌化的 `CowAgent` 进了 fork 文档。
全库核对「HEAD 已存在、合并后新增 `CowAgent` 提及」的文件，**仅此 1 处**，已改写。
（其余 80 余处新增提及全部落在上游新增文件——release notes、`multi-agent/team.mdx`
等——那些是上游自身的历史与文档，不该被品牌化。）

## 剩余（16）与已完成的判断

### 深水区（4）

| 路径 | 冲突实质 | 处置方向（尚未落地） |
| --- | --- | --- |
| `agent/memory/conversation_store.py`（16 hunk） | 上游把各 Agent 的会话库折成一份全局文件（`agent_id` 列 / 复合键）vs fork 的租户维度复合主键（基线 seam:6.1-6.11，决策 0.1） | 约束级接缝：两份 schema 要合成「全局一份 + `agent_id` + `owner`/`tenant_id`」的一套约束，不能各保留一套 |
| `agent/admin.py`（2 hunk） | 上游的 `skill_mode` 校验/`_bootstrap_workspace` 路径 vs fork 的 `scene_id` 校验/`_materialise_workspace`+`_build_profile` 路径 | fork 侧带 scene/knowledge_mode 等自定义参数，需确认上游新增的 `_seed_user_profile`/`_seed_name` 等步骤并入 fork 的物化流程 |
| `agent/tools/scheduler/integration.py` | fork 的 per-workspace `_task_stores` 缓存 + `_make_execute_callback(agent_bridge, agent_id, task_store)` vs 上游的单一全局服务 + 从任务解析身份 + `identity_scope` | 基线 seam:8.15-8.16,3.4 写明「converge to upstream global service + identity seam」——方向是取上游，但须确认 fork 的租户隔离由 `identity_scope` 承接 |
| `desktop/src/renderer/src/api/client.ts`（4 hunk） | 基线 seam:2.11-2.12,4.19,8.1,8.4「database session Bearer; **refuse cow_auth_token**」 | 取上游的 `fetch` 重试实现会**重新引入 `cow_auth_token`**，与基线决策冲突；上游的重试洞察（瞬时连接重置）应移植到 fork 的 `desktopContext.sendForm` 传输上，属**代码移植**而非文本合并，需单独提交 |

### 测试漂移（12）

`tests/` 下 12 个文件的冲突成因一致：上游把测试改为指向新模块
（`channel.web.api.*`、`conftest.web_backend_py()`），而 fork 侧在阶段 1 已把同一
批测试指向 `channel.web.web_channel`（hub）与 `tests/_helpers.web_layer_source()`。

**判定规则**（task 3.10，尚未逐文件落地）：先确认该测试对应的**能力**已在目标
版本中存在，然后二选一——

- 测试驱动的是 **fork 的 console**（`build_web_app()`，或其 handler 经 hub 解析）：
  保留 fork 侧的 patch 目标，断言若上游同期改强则取上游的断言；
- 测试直接实例化/驱动**上游的 handler**：取上游侧。

不允许删除测试或放宽断言后声称通过。逐文件需要读该测试的 fixture 与启动方式才能
定性，因此本轮未动。

## 本轮验证

- 已处置的 Python 文件全部 `ast.parse` 通过；`desktop/package.json` JSON 合法。
- `channel_instances.py` 的 D4b 分区线校验：`_CHANNEL_TYPE_LABELS`(99) 在
  `PARTITION DIVIDER`(562) **之上**。
- `app.py` 三个函数各定义一次（无重复 `def`），启动顺序为
  `_migrate_conversations` → `_migrate_conversation_tenancy` →
  `_migrate_scheduled_tasks` → `_guard_external_store_version`。
- 合并带入的上游新目录 `channel/web/api/**`、`channel/web/core/**`、
  `channel/web/templates/**`、`channel/web/tools/**` 全部干净落地（无冲突），
  这是 D8 里 `build_app()` 在完整树上可运行的前提。
- **尚未**跑门槛：冲突未清零前树不可导入，§6.2 全量回归与路由覆盖（task 3.9/g9）
  必须等 16 个冲突清零后执行。

## 下一步

1. 处置 4 个深水区冲突（每个都需读双侧实现，不得按 hunk 盲选）；
2. 按上述规则逐文件重定向 12 个测试；
3. 冲突清零后跑完整回归 + 路由覆盖 + 接缝测试，与阶段 1 基线对照；
4. 生成合并提交，重生成 `scripts/conflict-baseline.txt`（D7），开 PR。
