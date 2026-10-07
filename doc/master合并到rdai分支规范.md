# master 合并到 rdai 分支规范

> 适用仓库：CowAgent / 容大 AI（RongAI）｜方向：`master` → `rdai`
> 编写：2026-09-15｜修订：2026-10-07（实际装配验收、历史缺口继承与同步门禁）
> 本文是操作规范，不代表已执行合并或通过验收。

## 1. 目标与硬性原则

将固定源版本 `master` 的业务能力、改进和修复接入 `rdai` 的 `identity_mode=database`，保留数据库身份认证、多租户、角色权限、个人资源边界及产品定制。**完成标准：合法用户真实可用、授权隔离成立、实际入口接通。** 源码已合入、页面可打开或越权请求被拒，均不能单独证明完成。

1. **方向与历史**：从确认的 `rdai` 提交建立同步分支，有待合入源提交时使用 `git merge --no-ff --no-commit`；评审后交付到实际目标分支，保留 merge commit，不 squash、不 rebase，不删除或改写远端及已有本地 `master`。源已是祖先时，缺失行为用普通修复提交补齐，再次 merge 不会重放此前舍弃的内容。
2. **隔离与范围**：在独立克隆中排练、解决冲突和验证；只包含上游同步及必要适配，独立产品功能和大规模重构另行提交。不用 `reset --hard`、`clean -fd` 清理正式工作区，不强推改写共享历史。
3. **同功能优先上游实现**：按实际入口、参数、结果和用户行为判断功能是否一致；一致时复用 `master` 的服务、协议、数据结构和执行流程，在既有扩展边界接入 `rdai` 约束。必要 fork 须记录对应上游实现、运行入口、不能复用的原因和回归用例；不复制整套等价实现，不为接线问题新增通用会话、路由或状态框架。
4. **适配缺口必须解决**：补齐身份、租户、owner、工具及传输授权，不能以 database 模式整体禁用代替适配，也不能退回旧认证或免鉴权。适配期间可临时关闭不安全入口，但须继续实现和验收；完成后解除仅因适配缺失设置的关闭，部署条件和用户授权仍须满足。
5. **保留双方有效行为**：无文本冲突也要检查上游增量；含双方业务逻辑的文件不得整文件取 `ours` / `theirs`。`keep-fork` 只保留有依据的定制行为，不授权丢弃该文件的新功能；按调用方、依赖、资源、生命周期和 API 完整接入。测试命中实际运行实现，并保留上游业务断言。
6. **按最终候选交付**：按第 4 节验证、人工复核后提交；旧排练或旧构建结果不能替代本轮证据。完整能力清单全部通过后，才能声明“master 全部能力已支持”。
7. **缺口持续跟踪**：每轮继承未关闭的功能缺口和延期任务；归档 change、更新来源哈希、接受冲突基线均不等于功能验收。实施裁定与验收状态分开记录，必要用例的跳过、未收集或未运行阻止该能力结案。

遵循 [CONTRIBUTING.md](../CONTRIBUTING.md)：上游每次发布后同步，至少每周检查一次；提交信息、PR 标题和正文使用英文。后续替换已继承功能的页面、handler 或执行位置时，同样验证完整用户路径，不能仅凭分支关系把链路缺口归因于 Git 合并。

依据：[冲突基线](../scripts/conflict-baseline.txt)、[上游解耦主规范](../openspec/specs/fork-upstream-decoupling/spec.md)、[database 能力接入](../openspec/specs/database-runtime-consumers/spec.md)、[导航可用性](../openspec/specs/console-navigation-availability/spec.md)及受影响能力的规范。文档与实现或主规范不一致时，先核实并记录处置；历史 SHA、冲突数量和测试结果仅供追溯。

## 2. 准备与排练

### 2.1 确认范围并固定版本

在原仓库检查当前分支、工作区、远端和 `rdai` 跟踪关系：

```bash
git branch --show-current
git status --short
git remote
git config --get branch.rdai.remote
git config --get branch.rdai.merge
git log -5 --oneline rdai
```

确认源仓库、交付仓库及分支，以及本地领先提交是否一并交付。**未提交和未跟踪内容不会进入克隆**：属于本轮的改动应先检查并提交；否则只能声明验证了已提交版本。数据库、备份、凭据、日志及个人配置不得进入同步提交；使用独立测试配置、身份库和数据副本，远端 URL 中的凭据不得写入证据。

以下为 **Bash** 示例，从原仓库根目录开始，在同一终端分节执行，异常时停止处理。`origin/master` 是默认源；`gitea/main` 是 2026-10-01 的交付配置示例，每轮重新确认，不能假设远端目标名也叫 `rdai`。

```bash
MERGE_SOURCE_REPO="$(git rev-parse --show-toplevel)"
MERGE_UPSTREAM_REMOTE="origin"
MERGE_DELIVERY_REMOTE="gitea"
MERGE_DELIVERY_BRANCH="main"
MERGE_UPSTREAM_URL="$(git remote get-url "$MERGE_UPSTREAM_REMOTE")"
MERGE_DELIVERY_URL="$(git remote get-url "$MERGE_DELIVERY_REMOTE")"
MERGE_RUN_ID="$(date +%Y%m%d-%H%M%S)"
MERGE_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cowagent-sync.XXXXXX")"
git clone --no-hardlinks --branch rdai "$MERGE_SOURCE_REPO" "$MERGE_RUN_DIR/repo"
cd "$MERGE_RUN_DIR/repo"
git remote rename origin local-source
git remote add upstream "$MERGE_UPSTREAM_URL"
git remote add delivery "$MERGE_DELIVERY_URL"
git fetch upstream master:refs/remotes/upstream/master
git fetch delivery "$MERGE_DELIVERY_BRANCH:refs/remotes/delivery/$MERGE_DELIVERY_BRANCH"
MERGE_DELIVERY_REF="refs/remotes/delivery/$MERGE_DELIVERY_BRANCH"
git merge-base --is-ancestor "$MERGE_DELIVERY_REF" HEAD
git log --oneline "$MERGE_DELIVERY_REF..HEAD"
git status --porcelain

MERGE_TARGET_SHA="$(git rev-parse HEAD)"
MERGE_SOURCE_SHA="$(git rev-parse upstream/master)"
MERGE_DELIVERY_SHA="$(git rev-parse "$MERGE_DELIVERY_REF")"
MERGE_BASE_SHA="$(git merge-base "$MERGE_TARGET_SHA" "$MERGE_SOURCE_SHA")"
MERGE_SYNC_BRANCH="codex/sync-master-to-rdai-$MERGE_RUN_ID"
git switch -c "$MERGE_SYNC_BRANCH" "$MERGE_TARGET_SHA"
printf 'source=%s\ntarget=%s\ndelivery=%s\nbase=%s\n' \
  "$MERGE_SOURCE_SHA" "$MERGE_TARGET_SHA" "$MERGE_DELIVERY_SHA" "$MERGE_BASE_SHA" \
  > "$MERGE_RUN_DIR/refs.txt"
git log --oneline "$MERGE_TARGET_SHA..$MERGE_SOURCE_SHA" > "$MERGE_RUN_DIR/upstream-commits.txt"
git diff --stat "$MERGE_BASE_SHA" "$MERGE_SOURCE_SHA"
git diff --name-status "$MERGE_BASE_SHA" "$MERGE_SOURCE_SHA"
```

交付分支须为目标提交的祖先；落后、分叉、无共同祖先、工作树不干净或额外提交范围不符时，先处理。若 `git merge-base --is-ancestor "$MERGE_SOURCE_SHA" "$MERGE_TARGET_SHA"` 返回 0，**跳过排练和空合并**，仍核对能力清单；适配缺口走普通修复提交和同等验收，不伪造第二父提交。

克隆不携带原仓库的 `.venv`、依赖目录或冲突复用缓存，须按候选安装依赖。可用 `git config --local rerere.enabled true` 和 `git config --local rerere.autoupdate true` 启用复用；自动暂存的解决结果仍须人工检查。

### 2.2 排练并检查清理状态

```bash
bash scripts/sync-from-master.sh upstream master > "$MERGE_RUN_DIR/rehearsal.log" 2>&1
MERGE_REHEARSAL_RC=$?
cat "$MERGE_RUN_DIR/rehearsal.log"
printf 'Rehearsal exit code: %s\n' "$MERGE_REHEARSAL_RC"
test "$(git rev-parse upstream/master)" = "$MERGE_SOURCE_SHA"
test "$(git rev-parse HEAD)" = "$MERGE_TARGET_SHA"
git status --porcelain
git rev-parse -q --verify MERGE_HEAD
```

[排练脚本](../scripts/sync-from-master.sh)只报告冲突并尝试撤销合并，不执行功能验收、不提交、不推送。退出码 `0` 表示报告无冲突，`1` 表示有冲突，`2` 表示环境/fetch/基线等失败；均须核对完整日志。两个 `test` 应通过、工作树应干净，最后一条应返回非零（不存在 `MERGE_HEAD`）。脚本会再次 fetch，源变化时重新固定版本并排练；不要并行运行，当前脚本共用 `/tmp/sync-merge.out`。

记录实际冲突及相对基线新增/消失的路径和原因；基线由人工核实后更新，不能为消除提示直接删条目。排练结束后须按第 3.1 节重新生成正式候选。

## 3. 合并与适配

### 3.1 生成候选、逐项解决冲突

```bash
git merge --no-ff --no-commit "$MERGE_SOURCE_SHA"
git status --short
git diff --name-only --diff-filter=U
git ls-files -u
```

仅合并固定 SHA；冲突导致的非零退出属预期，其他错误先处理。此轮 `ours` 是 `rdai`，`theirs` 是 `master`；`git show :1:路径`、`:2:路径`、`:3:路径` 分别查看祖先、目标和源，新建/删除冲突可能缺少阶段。

| 基线决策 | 处理要求 |
| --- | --- |
| `seam:<tasks>` | 沿已有扩展边界整合上游行为和 fork 授权，不把定制堆回核心 |
| `keep-fork` | 保留指定定制块，同时合入文件内其他有效上游改动 |
| `merge-docs` | 合并说明，核对品牌、链接、功能状态及多语言一致性 |
| `keep-deletion` | 仅用于已登记的整文件删除，复核理由后对相应路径 `git rm` |
| `take-deletion` | 确认 fork 所需能力已有替代后接受上游删除，清理调用及构建引用 |
| `merge` / `retarget` | 保留双方行为，测试重定向仍须覆盖实际 rdai 路径和原断言 |

删除项以基线为准，尤其复核 `README.md`、`docs/zh/README.md`、`docs/zh/README-Hant.md`、`docs/ja/README.md` 和 `desktop/src/renderer/src/components/PermissionSelector.tsx` 的产品/RBAC 替代依据。文件内旧认证块用 `keep-fork` / `seam:` 处理，不登记为整文件删除。

每个冲突记录路径、双方意图、最终行为、扩展入口和测试；解决后逐路径 `git add` 并检查暂存内容。

### 3.2 用一份能力清单贯通适配与验收

首次建立固定源版本的完整能力基线；后续按上游增量及真实调用关系更新新增/变更项、受影响旧能力和历史缺口，不能只看冲突文件或提交标题。未变且依赖未受影响的能力引用原证据，固定核心回归仍执行。源已是祖先、增量列表为空时，仍须核对历史 open 项及实际入口，不能据此判定无缺口。

| 稳定 ID 与源依据 | rdai 实际入口及依赖 | 身份与资源规则 | 实施裁定 | 验收状态与证据 |
| --- | --- | --- | --- | --- |
| 名称、源 SHA、代码/测试位置、用户可观察行为 | 实际页面/脚本 → 路由/IPC/工具/后台触发 → 服务/数据/设备；辅助符号、资源、启动/清理、模式与开关 | 读取、配置、执行的主体、tenant、owner、对象边界 | 直接复用、适配、等价替代、产品不采用、待实施；裁定理由与差异 | 待验收、通过、失败；必要用例 ID、候选树/产物、正向/隔离/故障证据、负责人及下一任务 |

“产品不采用”须有明确产品依据和评审记录，列为范围差异，不能由合并执行者用来隐藏适配工作，也不能计入全部能力支持。“适配/等价替代”没有验收证据时仍为待验收；待实施项不得在后续轮次悄然删除。旧来源哈希一致只说明文件来源，不能证明 fork 消费方仍覆盖其行为。

持续机器清单已落在 [capabilities.json](../docs/design/upstream-sync/capabilities.json)，由 [本轮 change](../openspec/changes/restore-upstream-web-parity-and-sync-gates/proposal.md) 维护。`history.json` 保存首轮历史，后续必须同时与实际 PR 基线中的清单比较；不得删除未关闭 ID 或用空清单通过。首批七项缺口见 [核查基线](../openspec/changes/restore-upstream-web-parity-and-sync-gates/evidence/baseline.md)；本轮验收追加发现成员模型目录面板缺失，登记为 GAP-008。后续状态以持续清单及候选证据为准，不反写历史证据。

覆盖源版本实际具备的对话/流式、附件/工作区、模型/工具、技能/MCP、知识/记忆、定时任务、渠道/消息、项目、OpenAI 兼容 API 和 Desktop；配置、读取、执行、通知、上传及回读等动作分别核对。未适配、缺依赖或待真实验收的能力保留在缺口清单中。

逐项确认实际装载的页面、handler、工具注册、启动钩子和后台服务。上游修复落在未使用模块时，将修复转入运行实现或证明已有等价行为；调整测试 import/mock 后核对落点。共享密码、默认用户、全局配置映射为 database 会话/服务身份、显式租户和对象授权，后台调用不得借用全局管理员或虚构租户。状态投影（含 `/auth/context`）、导航和动作须区分未适配、部署未启用、提供方未连接和用户无权，不能以固定关闭状态掩盖缺口。

### 3.3 必须保持的行为

| 范围 | 检查重点（含无冲突的上游增量） |
| --- | --- |
| 路由与认证 | `channel/web/route_registry.py` 是 URL/方法/handler 与授权策略的唯一登记源；新增入口不得默认公开。不复活 `web_password`、`cow_auth_token`、共享密码/HMAC 登录或旧 `_require_auth` 放行；保留 database 及现有 `/auth/*` 契约 |
| 数据与资源 | tenant/owner 来自可信上下文；区分平台、租户共享和个人数据、配置、凭据及存储路径。管理员治理不等于读取成员私有正文。保留会话 schema、复合约束、个人记忆版本和迁移兼容 |
| 任务、启动与渠道 | 联查 tool/store/integration/authorization，保留任务字段、通知、并发原子性和执行时身份重验；保持迁移/启动顺序、具名钩子、强制授权扩展、渠道字段及签名，新动作纳入授权 |
| 桌面身份与文件 | 复用 broker、配对、清理和恢复流程，页面 Cookie 不替代原生会话。贯通 main/preload/client、IPC、SSE/设备网关及上传/预览/下载/导入；选择进入本轮模型和执行上下文，传输租户、落盘及回读完整 |
| 路径与连接 | 区分本地输入和后台工作区，远程后台不把客户端绝对路径当作 cwd；复用来源和执行目标契约。重连不误撤销有效目录授权，撤权/换账号立即停止访问；不放宽租户、owner、路径校验。上游引入/修改 `_import_local_file` 时专项检查本机访问、令牌及文件授权 |
| 跨部署调用 | 复用上游业务协议，在现有 IAM/传输边界补齐可验证主体、租户映射、目标授权、请求/返回完整性、防重放及会话隔离；验证源版本支持的各模式，真实远程中继验收不能由本机替身代替 |
| 产品与部署 | 保留 fragment、角色菜单、品牌及上游交互改进，不复活已替代权限选择器；核对 requirements、锁文件、Desktop 构建、Docker、CI 触发分支和交付目标 |

新增同名 fork 模块、入口、任务字段或传输未分类前，不通过验收。

### 3.4 核对实际入口与完整依赖

以最终候选的启动函数和真实响应反查装配，不能只搜索“代码中存在”。当前 rdai 使用 `build_web_app()` → `route_registry.py` → `fork/handlers/**`，页面加载 `chat.html` 的实际资源；独立上游 `build_app()`/`api/**` 的测试不得代替 rdai 验收。装配改变时同步更新此映射。

| 接入对象 | 必须核对 |
| --- | --- |
| 页面与导航 | 后端路径实际可达、view/区域/页签对应、前进后退与刷新、旧链接迁移、深路径资源；内部 registry 自洽还须对照上游新增入口 |
| JS 与模板 | 真实 HTML 的脚本顺序、惰性模块、辅助符号、状态和 DOM；只搬调用方不搬被调函数不得通过，不能以测试 stub 补掉生产缺依赖 |
| 样式与翻译 | 浏览器实际加载的 CSS/主题/翻译键及作用域；未引用的上游文件和隔离样例通过不足以证明整页正确 |
| 生命周期与后台 | 登录/租户就绪后的启动、离页/退出清理、错误和取消分支、迟到响应与重连；增加轮询或监听须防重复启动 |
| API 与授权 | 用户从实际页面完成操作，读/写/执行/通知共用可信身份和对象规则；不可只测直接 handler 或拒绝分支 |

每项功能先确定唯一生效实现，再选择复用或 fork 适配；不同时装载上游完整模块与单体中的同名实现。审查已解决冲突中与第一父提交完全相同的文件，核对是否漏掉同文件上游增量；相同本身不是错误，但必须有逐功能裁定。`rerere` 自动复用同样适用。

## 4. 验证与准入

### 4.1 固定受测内容与测试范围

在冲突全部解决的候选目录安装依赖、运行检查；测试前执行：

```bash
git diff --name-only --diff-filter=U
git diff --check
git diff --cached --check
git diff --cached --stat
git diff --cached "$MERGE_TARGET_SHA"
git diff --exit-code
git write-tree > "$MERGE_RUN_DIR/candidate-tree.txt"
```

冲突列表须为空、检查须通过；所需源码逐项暂存，不能依赖未跟踪/被忽略文件。记录源/目标/候选提交及树哈希、前后端构建来源、客户端版本、OS/运行时、身份/运行模式及开关；安装包加制品校验值，远程服务记实际部署版本。

**测试、启动和交付必须使用同一候选的实际产物**：源码变化后重新暂存、构建并复跑受影响检查；复用构建须确认与源码一致，不能误用原目录或旧 `desktop/dist`。在实际窗口确认连接和登录，进程存在不代表启动成功，启动也不代表批准提交。仅元数据变化且树、构建、环境一致时可关联已有证据。

采用“**每轮核心回归＋受影响能力追加验证**”。核心为第 4.2 节及第 4.4 节 D1、D2、D3 同账号重登，覆盖声明支持的本地/远程形态；身份、工作区、工具、提示词、后台启动、设备连接、前端装载或构建变化触发完整桌面矩阵。纯文档/注释且不改变行为时可只查链接、格式和差异，范围及豁免原因在 PR 可见。

### 4.2 核心回归与追加检查

下例假设候选 `.venv` 和测试依赖已安装；[路由检查](../scripts/check-route-coverage.py)使用[路由基线](../scripts/route-baseline.txt)，[模块边界检查](../scripts/check-web-module-seams.py)检查扩展边界。

```bash
.venv/bin/python scripts/check-route-coverage.py
.venv/bin/python scripts/check-web-module-seams.py
# 先按 docs/design/upstream-sync/README.md 安装锁定的浏览器依赖
NODE_PATH="$PWD/tests/browser/node_modules" .venv/bin/python scripts/check-upstream-sync.py \
  --source <本轮固定上游SHA> --baseline <已验收目标SHA> --output artifacts/upstream-sync
.venv/bin/python -m pytest -q -p no:randomly \
  tests/test_sync_report.py tests/test_upstream_core_seams.py \
  tests/test_no_resurrection_legacy_identity.py tests/test_conversation_schema_seam.py \
  tests/test_scheduler_identity_seam.py tests/test_startup_hook_seam.py \
  tests/test_channel_signature_seam.py tests/test_route_registry.py
.venv/bin/python -m pytest -q -p no:randomly \
  tests/test_http_policy.py tests/test_identity_resource_authorization.py \
  tests/test_scheduler_web_update.py tests/test_upstream_drift_guards.py \
  tests/test_recovered_entry_acceptance.py tests/test_desktop_auth_flow.py
node --test tests/test_fork_fragments.cjs
node --test tests/test_execution_permission_ui.cjs
```

按全部上游增量追加双方行为测试；文件缺失/更名须补等价检查，不能静默删项。同名模块造成 pytest 收集问题时可兼容分组，并在同环境目标基线复现。

| 影响范围 | 追加验证 |
| --- | --- |
| 任务/渠道 | 正常执行、失效授权拒绝、并发编辑、未知字段保留、入站路由和通知目标 |
| 启动/扩展/解耦 | 独立上游、完整 rdai、缺强制授权扩展、仅缺可选 UI 四种状态；上游环境不挂 rdai 身份库/业务数据，可选 UI 缺失不放宽后端权限 |
| Web/Desktop/外部能力 | 受影响 Node/浏览器用例，Desktop 重新构建；客户端、渠道、提供方路径变化须真实端到端验收，mock/构建成功不能替代 |
| 迁移/安装包 | 测试副本验证旧数据升级、幂等、中断恢复、tenant/owner 保持，留备份及恢复步骤；发布前在目标平台安装形态验证启动、登录、选目录后聊天读取、重登及随包 Python/helper、网关和资源 |

开发态结果不等于安装包验收；未测平台/模式不得标为通过。明确支持的客户端/服务端配套版本及不匹配提示，不要求穷举历史版本组合。

### 4.3 database 业务验收与交付门槛

使用明确启用 database 模式的最终候选、独立身份库、至少两个租户及多个用户，验收本轮新增/受影响能力和拟关闭的历史缺口：

- **业务成功**：获权普通成员与管理员分别从真实客户端/渠道，经实际路由、工具或后台触发完成业务；核对源版本的结果、格式、事件、文件、任务字段和通知语义，必要差异记录适配依据。
- **隔离成立**：覆盖匿名、无权、同租户他人、跨租户/owner、伪造主体、撤权/停用及相关重放；拒绝后无未授权写入、模型调用或工具副作用。
- **状态一致**：清单中的实际入口、能力投影、导航、页面/API 与执行结果一致。直接调用 handler、mock 成功或仅检查拒绝分支不能替代业务闭环。

可复用 `tests/test_web_consumer_closure.py`、`tests/test_consumer_closure_acceptance.py`、`tests/test_capability_matrix.py` 和 `tests/test_personal_console_multi_tenant_authorization.py`，按变化补足覆盖。

每项通过须满足第 1 节完成标准。本轮能力失败、既有关键链路回归、授权回退、未分类入口或无法解释的基线漂移阻止交付；未验收的新能力保持关闭并继续适配，不能靠关闭既有能力规避回归。未受影响的历史缺口保留原因、负责人和后续任务；其他既有测试失败须提供同环境基线并经评审处置，不能写“全部通过”。

记录命令、环境、候选及构建、通过/失败数、跳过原因和日志；缺依赖、收集失败、未运行或跳过均不算通过。实际交付分支的 PR 必需检查须包含核心及受影响回归，复用现有 pytest、Node、Electron runner 和 CI，汇总不得把缺失/失败算成功。required checks、触发分支及阻断效果用实际 PR 验证；人工项目由评审核对同一候选证据。**文档不证明 CI 门禁已启用**，未自动化项须明示并按同等标准人工验收。

### 4.4 桌面完整路径

准备文本和二进制文件，并在后台放同名不同内容文件；记录实际 React/Web 容器入口、模式、开关、读取输入/本地执行类型及权限。**文件面板与聊天工具分别验证**，断言来源、内容和执行位置，不绑定模型措辞。

| 用例 | 动作与断言 |
| --- | --- |
| D1 登录与选择 | 获权普通成员登录并选目录；页面、原生会话、绑定的用户/租户一致，面板返回真实内容 |
| D2 聊天读取 | 从聊天列举并读取指定文件，核对本轮来源、工具调用和执行位置；二进制传输核对租户、可分析路径及字节/hash，本地执行核对真实目标，不读后台同名文件或猜路径 |
| D3 退出重登 | A 登录→选目录→聊天读取→退出→A 重登重选再读，再测 A→B；旧授权及迟到回调不回流，页面登录不掩盖原生未登录 |
| D4 连接恢复 | 首次握手、断线重连、后台重启；有效选择不被误撤销，不可用/退出/配对失败可见且可重试，不报空目录或假成功 |
| D5 权限与来源 | 无工具权限、跨用户/租户、撤销授权、换目录、清除选择；拒绝原因明确，下一轮不沿用旧来源，本地文件关闭时仍能退出/登录 |
| D6 运行形态 | 在声明支持的本机/远程后台及实际页面入口执行 D1–D5，不要求未支持组合；只测回环不代表远程通过 |

走真实主进程、后台、授权、传输和内容；固定模型响应只证明工具链路，还须用真实模型的自然语言请求验证来源选择。现有提示词、网关、设备及文件传输单测可复用，但不替代端到端组合。

```bash
npm --prefix desktop ci
npm --prefix desktop run build
node desktop/e2e/run-remote-workbench.mjs --spec ./relogin-session-sync.spec.mjs
```

运行前安装 runner 声明的 Playwright、Python 依赖和 fs-guard helper，构建 Electron 主进程/renderer/helper，使用隔离身份库，不借用操作者登录态。上述重登用例通过 `projectRead` 验证文件面板，**不覆盖 D2 聊天读取**；D2 未自动化时保留当前候选实机证据。测试入口及已知限制参见[桌面链路证据](../openspec/changes/archive/2026-10-03-fix-desktop-relogin-session-sync/selected-source-evidence.md)。

### 4.5 同步专项门禁与已知缺口回归

同步检查按五层汇总，任何必需层缺失、失败或未执行均不能声明该能力通过：

| 层次 | 验收证据 | 必须能拦截的情况 |
| --- | --- | --- |
| 来源与历史 | 源增量、逐行为分类及持续清单 | 新模块未分类、删除历史未修复项、只改 hash |
| 入口对照 | 上游入口与 rdai 实际路由/页面映射 | registry 内部检查通过但上游新页面遗漏 |
| 装配与依赖 | 渲染后的资源、依赖及生命周期检查 | 缺辅助函数、样式未加载、重复模块/轮询 |
| 必要执行 | 每个必要 pytest/Node/浏览器用例的机器结果 | skip/xfail、零收集、缺浏览器或只执行替代入口 |
| 行为与交付 | 正向、权限、故障和同候选产物结果 | 页面刷新丢状态、错误事件未清理、仅修改 CI YAML |

本次核查确定以下专项场景。首次补齐必须全部验收；后续按调用与依赖变更触发，并保留必要的核心冒烟，不按文件名是否冲突决定是否测试。

| 缺口 | 专项验证 |
| --- | --- |
| GAP-001 路径路由 | 直接进入、刷新、前进后退、旧 `/chat`/hash、深路径资源、工作台/管理区、权限和离页保护 |
| GAP-002 SSE 错误依赖 | 真实 error 事件有提示并关闭流，done/cancel/迟到事件不回归；不得仅 stub 缺失函数 |
| GAP-003 跨会话任务通知 | 非当前会话完成、长任务迟完成、多页及同时间事件、断线、双窗口去重、换身份、点击重验授权 |
| GAP-004 流式滚动 | 少量上滚也保持阅读位置，显式回到底部恢复跟随，程序滚动不误判 |
| GAP-005 知识库面板 | 空库、docs/graph/sources 切换及迟到列表响应，只有当前面板可见，旧身份数据不回流 |
| GAP-006 消息换行 | 实际加载样式下长链接/行内代码不撑破布局，代码块/表格保留局部滚动 |
| GAP-007 Web 更新 | fork 发布源与固定目标、平台授权、并发锁、隔离准备、失败恢复及不支持安装形态拒绝 |

先列必要用例标识，再核对真实结果，不能用固定总通过数代替覆盖。2026-10-07 基线中的 25 个跳过及历史刷新夹具的 6 个失败均不算通过：前者须改测实际装配，后者补真实依赖并保留原断言。基线只作问题定位；后续数量可随合理拆分变化，最终按行为和用例标识核对。必要用例不得用环境跳过规避；不适用形态须有明确范围与评审依据。

**自动化实施状态**：`scripts/check-upstream-sync.py`、持续 JSON 清单、实际入口/资源探针及必要用例结果核对已实现，接入 `scripts/check-production.sh`、Gitea production-readiness 的 `local-regression` 与 GitHub `upstream-sync`。本地故障注入已证明缺路由/依赖/样式、删历史项、只改来源，以及 skip/xfail/零收集/缺浏览器结果会失败。受控源码更新的正向、并发、准备失败保全与代码/数据恢复用例已实现，并保持为必要用例；不得以删除用例或只验证拒绝来放行。实际部署必须配置并验收受信源和独立进程管理器。Gitea 仓库/组织 runner 清单为空，分支保护未配置；远端必需检查和测试 PR 阻断仍未验证，CI 交付任务不得关闭。运行说明及范围见 [同步检查说明](../docs/design/upstream-sync/README.md)。

验证门禁时，使用测试候选故意移除依赖/页面、删除未关闭清单项或跳过必要用例，确认本地汇总失败且实际交付检查阻断，恢复后再跑同候选。未取得远端 runner/阻断证据时写明未验证并保留任务，不得标记自动门禁交付完成。

## 5. 提交与交付

### 5.1 人工复核清单

- [ ] 源/目标/交付仓库及分支、SHA、共同祖先和本地额外提交范围已确认。
- [ ] 隔离排练和正式候选有记录；源已包含时按普通修复路径处理。
- [ ] 冲突、删除项和基线漂移有依据；无冲突增量也已检查。
- [ ] 能力映射、上游复用、必要 fork 原因及历史缺口已更新，符合第 1、3 节。
- [ ] 每项变更的实际装配、辅助依赖、资源、启动/清理和故障分支已核对，既有未关闭 ID 完整继承。
- [ ] 第 4 节核心与受影响验收有同一候选证据，文件面板未冒充聊天读取，失败/跳过未误报通过。
- [ ] 必要用例逐项真实执行；自动/人工检查状态明确，实际交付分支的 CI 触发、runner 和阻断有依据。
- [ ] 审查者已复核暂存差异和实际构建，无无关文件/运行数据；历史保留、证据保存及回滚方案明确。

### 5.2 提交、检查远端并创建 PR

实际产生 merge 候选时，复核后执行下列命令。源已包含的普通修复路径使用相应英文提交信息，不检查第二父提交；无待修复内容时无需提交。

```bash
git diff --exit-code
git diff --cached --check
test "$(git write-tree)" = "$(cat "$MERGE_RUN_DIR/candidate-tree.txt")"
git commit -m "merge: sync master into rdai"
MERGE_CANDIDATE_SHA="$(git rev-parse HEAD)"
git show --no-patch --format='%H%n%P%n%s' HEAD
test "$(git rev-parse 'HEAD^{tree}')" = "$(cat "$MERGE_RUN_DIR/candidate-tree.txt")"
git merge-base --is-ancestor "$MERGE_SOURCE_SHA" HEAD
git status --porcelain
```

确认 merge 第一父为 `$MERGE_TARGET_SHA`、第二父为 `$MERGE_SOURCE_SHA`，普通修复核对目标基线；工作树须干净。提交钩子改变内容时按第 4.1 节更新受测证据。交付前 fetch 并与 `refs.txt` 比较：

```bash
git fetch upstream master:refs/remotes/upstream/master
git fetch delivery "$MERGE_DELIVERY_BRANCH:refs/remotes/delivery/$MERGE_DELIVERY_BRANCH"
git rev-parse upstream/master "$MERGE_DELIVERY_REF"
```

交付分支前移时先整合新目标、更新候选并重跑核心及受影响检查，不覆盖他人提交；源前移时明确仍交付固定 SHA，或重新固定源并排练。检查完成后执行 `git push -u delivery "$MERGE_SYNC_BRANCH"`，在交付仓库创建 base 为 `$MERGE_DELIVERY_BRANCH`、head 为同步分支的 PR，按仓库流程评审并保留同步 merge 历史。分支保护/合并队列改变受测内容时重新验证实际候选。

PR 标题示例：`merge: sync master into rdai`。英文正文引用一份证据索引，包含版本与构建、交付目标、本地提交范围、能力映射与验收、冲突/基线决策、测试/CI/人工结果、桌面覆盖限制、剩余缺口及负责人、审查人和回滚/迁移恢复方案。合入后记录交付最终提交，确认源 SHA 是其祖先并检查 CI 与必要冒烟；发布按项目正常流程。

`$MERGE_RUN_DIR` 为临时目录，交付前把证据转存到团队可访问的 PR、CI 制品或版本管理目录；复用已有链接，不复制历史材料。日志不得包含密码、令牌、Cookie 或业务文件正文。`.gitignore` 仅为本文设置例外，按普通文件纳入版本管理；其他 `/doc/` 本地资料仍被忽略，不批量强加整个目录。

## 6. 中止与回滚

| 阶段 | 处理 |
| --- | --- |
| 合并中未提交 | 先保存冲突决策，在独立克隆 `git merge --abort`，确认 HEAD/工作树恢复 |
| 已提交未交付 | 保留同步分支分析，可从确认目标 SHA 新建分支重做，不改写共享历史 |
| 已合入共享分支 | 从最新交付提交建回滚分支，核实范围/父提交后 revert，经回归和 PR 交付 |

已交付回滚示例（新终端先按第 2.1 节确认远端、分支及变量）：

```bash
git fetch delivery "$MERGE_DELIVERY_BRANCH:refs/remotes/delivery/$MERGE_DELIVERY_BRANCH"
git switch -c "codex/revert-master-sync-$(date +%Y%m%d-%H%M%S)" "$MERGE_DELIVERY_REF"
git log --first-parent --merges --oneline
MERGE_REVERT_SHA="填写实际合并提交SHA"
git show --no-patch --format='%H%n%P%n%s' "$MERGE_REVERT_SHA"
git diff "$MERGE_REVERT_SHA^1" "$MERGE_REVERT_SHA" --stat
# 仅当父提交 1 确实是要保留的 rdai 历史时执行。
git revert -m 1 "$MERGE_REVERT_SHA"
```

同步 merge 和平台 PR merge 可能不同，按实际撤销范围选提交，不凭标题判断；回滚后复跑受影响回归。代码 revert 不恢复数据库/外部状态，按预先验证的迁移恢复方案处理。被 revert 的源仍在祖先历史中，恢复时评估撤销回滚或提交修复，**再次 merge 同一 SHA 不会重新应用被撤销内容**。

历史参考：[合并保全与回归证据](../openspec/changes/archive/2026-09-15-complete-database-capability-parity/evidence/10-master-merge-preservation.md)、[数据库能力交付](../docs/design/database-capability-parity-delivery.md)、[个人控制台交付及限制](../docs/design/member-personal-console-delivery.md)。历史证据只解释决策，不替代当前候选验收。
