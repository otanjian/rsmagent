## Why

上游 `master` 于第九轮完成 Web 层重构（`refactor/split-web-channel`）：把 8.4k 行 / 389 KB 的 `channel/web/web_channel.py` 单体拆为 `channel/web/api/*.py`（每视图一个模块，共 17 个）与 `channel/web/core/*.py`（共享管道，共 4 个），并删除 932 KB 的 `channel/web/static/js/console.js` 与 197 KB 的 `static/css/console.css`，改为 `static/js/{core,views,chat}/*` 与 `static/css/*` 的模块化前端。

本轮 master → rdai 同步（`origin/master@8f1b19f1` × `rdai@b5c5090f`，共同祖先 `e5e2a52d`）因此产生 **45 处冲突**：基线登记的 21 处全部复现，另新增 24 处漂移，其中 11 处是 web 测试文件。核心相撞点是 fork 侧 `web_channel.py`（649 KB / 13,765 行 / 79 个 handler）与上游删除并拆分后的同名文件（7,973 字节，仅剩 URL 表）。任何一侧的整文件取舍都必然丢弃对方：取 `ours` 会让上游新增的 `api/`+`core/` 模块与 fork 单体里的 handler 重复实现；取 `theirs` 会删掉 fork 的全部身份/租户/RBAC 授权模型。这正是 `fork-upstream-decoupling` 禁止的处置方式。

根因不是冲突规模，而是 **fork 的 Web 定制仍原地写在被上游重构的上游核心文件内**——这本身已违反 `fork-upstream-decoupling` 既有要求「定制逻辑位于稳定接缝而非上游核心文件内」及其场景「上游核心文件不含 fork 专有分支」。上游的拆分恰好给出了该接缝应落地的结构。本 change 借此把 fork 的 Web 定制迁出上游文件，解除同步阻塞，并使后续每次 master 更新不再在同一处重复失血。

## What Changes

- **采用上游模块布局作为 fork Web 后端结构**：`channel/web/api/<view>.py` 承载上游 handler，`channel/web/core/*.py` 承载共享管道；`web_channel.py` 收敛为「URL 表 + `build_app()`」，与上游形态一致。
- **fork 授权与身份逻辑迁入 fork 自有接缝模块**：把 `web_channel.py` 中 fork 新增私有 helper（约 186 个，含 29 个 `_require_*` / `_authorize_*`）与 fork 专有 handler 移出上游文件，按域归入 fork 模块（如 `channel/web/fork/authorization.py`、`fork/handlers/*.py`），上游 `api/` 模块内 MUST NOT 出现 fork 专有分支。
- **路由权威清单跨拆分模块解析**：`route_registry.py`（现有 447 行）改为从拆分后的模块命名空间解析 handler 名称，保留 `source`（`upstream` / `fork:<area>`）登记与三腿覆盖不变量校验；fork 扩展路由继续经扩展注册，不编辑上游路由字面量。
- **前端模块化布局拆为独立 change**：`adopt-upstream-web-frontend-split` 承接 Phase 3（采用上游 `static/js/{core,views,chat}/*` 与 `static/css/*`、前端逐处裁定（移交时 98 处，刷新后 109 处）、覆盖映射、删除 `console.js` / `console.css`）。本 change 因此只交付后端接缝与合并，前端沿用既有单体并按 `keep-fork` 显式登记（D5 备选③ 的合法路径），被延后的上游前端增量逐条记录在该 change 内。
- **删除决策复核**：`console.js` / `console.css` 由「fork 修改 / 上游删除」的 `DU` 形态转为「上游删除且 fork 迁移完成」，按基线登记新处置，`DELIBERATE_REMOVALS` 既有五项保持不变。
- **重新生成冲突基线**：新一轮排练（此时上游已拆分）实际冲突集重新登记处置，24 处漂移项逐条给出 `seam:` / `keep-fork` / `merge-docs` 决策，消除未点名漂移。
- **完成本轮同步交付**：在最终候选上跑通双侧回归与 database 能力验收后，按 `doc/master合并到rdai分支规范.md` 提交 merge commit 并向 `rdai` 提 PR。
- **BREAKING（内部结构，非对外行为）**：`channel/web/web_channel.py` 不再是 handler 与授权的所在地。以模块路径或 globals 名称引用 handler、授权 helper、`WebChannel` 的内部调用方（测试、`route_registry.py`、`app.py`、渠道工厂）必须改为引用新位置。对外 HTTP 路由、策略、响应与权限语义保持不变。

## Capabilities

### New Capabilities

- `web-console-module-seams`: 规定 fork Web 后端层的模块布局与接缝契约——fork handler/授权逻辑的归属模块、上游 `api/`+`core/` 模块不得含 fork 专有分支、路由权威清单跨模块解析，以及这些约束的可执行校验。前端模块化布局与 fork 前端定制的交付方式不在此 capability，由 `adopt-upstream-web-frontend-split` 的 `web-console-frontend-modules` 承载。

### Modified Capabilities

- `fork-upstream-decoupling`: 把「定制逻辑位于稳定接缝」的接缝目标结构由旧单体内接缝明确为「对齐上游 `api/`+`core/` 与 `static/js|css` 模块布局」；补充上游重构后冲突基线的重新登记义务，以及「上游删除 fork 修改文件」这一新 `DU` 形态的处置规则。

## Impact

**受影响代码**

- 后端：`channel/web/web_channel.py`（拆分收敛）、新增 `channel/web/fork/**`，`channel/web/route_registry.py`、`channel/web/api/**`、`channel/web/core/**`（引入上游）、`app.py`（`build_app` / `SERVING` / `WebChannel` 引用点）、`channel/channel_instances.py`、`auth/http_policy.py`。
- 前端：本 change 不改前端代码位置。上游拆分前端文件随合并进入工作树但**不被装载**；`channel/web/chat.html`、`static/js/console.js`、`static/css/console.css` 按 `keep-fork` 保留，切换与删除属 `adopt-upstream-web-frontend-split`。
- Desktop：`desktop/src/main/preload.ts`、`desktop/src/renderer/src/api/client.ts`、`desktop/src/renderer/src/types.ts`、`desktop/package.json`。
- 受影响测试：11 个本轮漂移的 web 测试文件，以及 `tests/test_route_registry.py`、`tests/test_upstream_core_seams.py`、`tests/test_no_resurrection_legacy_identity.py`。前端装载相关的 `tests/test_fork_fragments.cjs`、`tests/test_execution_permission_ui.cjs`、`tests/test_web_console_assets.py`、`tests/test_web_console_routing.py` 随前端 change 处理。

**数据唯一归属（不变）**

身份、成员与私有归属仍归身份域（`auth/**`、身份库）；任务正文归既有 TaskStore；记忆归可信个人/智能体工作区；渠道密钥归既有密文凭据存储；路由与策略的权威来源仍唯一为 `channel/web/route_registry.py`。本 change 只搬移代码位置，不新建第二份身份或授权事实源。

**依赖与门槛**

- 前置：无跨 change 硬前置。上游 Web 重构已在固定源 `8f1b19f1` 内，本 change 不等待其他 change。
- 门槛：权限隔离回归（`tests/test_http_policy.py`、`tests/test_identity_resource_authorization.py`、`tests/test_route_registry.py`）与路由覆盖校验（`scripts/check-route-coverage.py`）必须通过；`database-runtime-consumers` 既有要求（已适配消费者在 database 模式开放、越权拒绝、上传回读与写入同源）作为回归义务保持通过，不因本 change 放宽。
- 本 change 不新增对外业务能力，因此不新增审计/凭据/审批/配额消费方；现有前置能力不受影响。

**未决实施参数**（在 design.md 决定并记录）

- fork 接缝模块的具体目录与命名；
- `route_registry.py` 解析 handler 的命名空间来源；
- 旧 `console.js` / `console.css` 的最终处置——**已决**：本 change 内按 `keep-fork` 保留（D9），删除与前端装载方式的切换移交 `adopt-upstream-web-frontend-split`。
