## Why

`adopt-upstream-web-split` 完成了后端迁移并把 `master` 合入 `rdai`，但其 Phase 3（前端采用上游模块化布局）未完成。上游 `refactor/split-web-channel` 删除的 `console.js` / `console.css` 因此仍被 fork `keep-fork` 保留并继续服务，`chat.html` 与 `templates/**` 也仍是 fork 单体版本。

把 Phase 3 留在 `adopt-upstream-web-split` 内会阻塞该 change 的交付：后端冲突已解、回归已绿、路由覆盖校验已通过，而前端迁移是**独立的、可延后的一次结构性改造**，其未完成不影响合并后线上行为（线上仍是迁移前逐字节相同的控制台）。`adopt-upstream-web-split` 的 D5 已预见此路径并写明条件：

> ③ 保留 `console.js`/`console.css` 并以 `keep-fork` 处置该 `DU`（…若 Phase 3 确定降级，必须显式改为 `keep-fork` 基线决策并逐条列出被丢弃的上游增量，不得默认发生）

本 change 就是那个降级落点：它承接被延后的前端迁移，并让 `adopt-upstream-web-split` 得以按「后端接缝已解冲突」的真实完成度交付。

## What Changes

- **承接 Phase 3 全部未完成工作**：采用上游 `static/js/{core,chat,views}/*`、`static/css/*`、`chat.html` shell 与 `templates/**`（保持未改动），把 fork 前端定制迁入 `static/js/fork/**` / `static/css/fork/**` 并经服务端覆盖映射装载。
- **逐处裁定 109 个冲突区域**：两侧都改过的区域不得机械移植——整函数照抄会静默丢弃上游改动（可能是修复或安全修复），反向取上游则丢弃 fork 定制。每处记录 `fork` / `upstream` / `merged` 与理由。
- **加漂移门禁**：`static/js/fork/manifest.json` 记录每个 fork 模块的上游来源路径与移植时的 sha256；上游模块变更即失败，使「上游改动需人工重新应用」可检测，而不是静默丢失。
- **删除 `console.js` / `console.css`**，不留兼容层；删除的前置条件是 109 处裁定全部完成（在此之前保留单体是正确状态，不是遗漏）。
- **补 `.cjs` 与浏览器验收**：`node --test tests/test_fork_fragments.cjs`、`node --test tests/test_execution_permission_ui.cjs`，以及登录、上下文切换、流式请求、上传回读、下载预览；并解除 `tests/test_web_console_assets.py`、`tests/test_web_console_routing.py`、`tests/test_web_console_update.py::test_frontend_contract` 因该分歧而加的 skip。
- **收口桌面端消费的未路由接口**：`desktop/src/renderer/**` 是独立前端，随同步按上游版本合并后已在调用父 change 未路由的 8 条接口（`/api/scheduler/{runs,runs/detail,runs/delete,create,recipients,instances}`、`/api/sessions/<id>/{context_usage,compact_context}`），而 fork 的桌面端此前 0 处调用。逐条二选一收口（补路由与授权，或降级/隐藏入口）并做桌面端验收——不得以「属拆分后前端」留空（父 change `evidence/21` §E、本 change 任务 0.6/6.5）。
- **不新增对外能力**：本 change 只改前端装载方式与模块归属，路由、授权、响应与权限语义不变；后端授权唯一事实源仍在 `channel/web/route_registry.py` 与 `auth/**`。上面那条断口的收口只补齐既有能力的入口与授权判定，不放宽任何策略。

## Capabilities

### New Capabilities

- `web-console-frontend-modules`: 规定控制台前端的模块化布局、fork 定制经覆盖模块交付而非原地改写上游视图、109 处裁定的记录义务、模块来源指纹与漂移门禁，以及单体退役的前置条件与校验入口。

### Modified Capabilities

无。`adopt-upstream-web-split` 正在 ADD 的 `web-console-module-seams` 中「前端采用模块化布局且 fork 片段经稳定挂载点装载」一条已从该 change 移出、由本 change 的新 capability 承载（该 change 尚未归档，故不构成 MODIFIED）。

## Impact

**受影响代码**

- 前端：`channel/web/chat.html`、`channel/web/static/js/**`（新增 `js/fork/**`，装载顺序与 `boot.js` 契约不变）、`channel/web/static/css/**`（新增 `css/fork/**`）、`channel/web/templates/**`。
- 服务端：fork 自身页面处理器（页面组装与覆盖映射替换 `assets/js|css/**` 引用），`channel/web/core/template.py` 的 include 与 mtime 版本戳语义。
- 工具与门禁：`scripts/migration/port_frontend.py`、`verify_frontend_port.py`、`build_frontend_adjudication.py`（已在本 change 之前写成，本 change 消费其产出）、`tools/check-load-order.mjs`、`static/js/fork/manifest.json`。
- 桌面端：`desktop/src/renderer/src/api/client.ts`（调用未路由接口的封装）与消费它们的页面/组件（`pages/TasksPage.tsx`、`components/ContextUsage*.tsx` 等），用于断口的「补路由」或「降级入口」两种收口之一。
- 受影响测试：`tests/test_web_console_assets.py`、`tests/test_web_console_routing.py`、`tests/test_web_console_update.py`、`tests/test_personal_console_frontend.py`、`tests/test_tool_display.py`、`tests/test_fork_fragments.cjs`、`tests/test_execution_permission_ui.cjs`。

**数据唯一归属（不变）**

本 change 只搬移前端代码位置与装载方式，不新建第二份身份、授权或配置事实源；fork 前端不新增任何越过后端授权的读路径。

**依赖与门槛**

- 前置：`adopt-upstream-web-split` 已把上游拆分前端引入工作树（`channel/web/static/js/{core,chat,views}/**` 在位）。在此之前本 change 无法开始裁定。
- 门槛：109 处裁定记录齐备且 `verify_frontend_port.py` 报零未交代；`node --check` 全量产出模块通过；`tools/check-load-order.mjs` 通过；`.cjs` 套件不因本 change 新增失败；浏览器验收记录成功与拒绝两侧结果。
- 本 change 不新增审计/凭据/审批/配额消费方。

**未决实施参数**（在 design.md 决定并记录）

- 覆盖映射的落地位置（服务端替换 vs 前端装载器）；
- `doc-editor.js` / `workspace.js` 与上游 `assets/js/doc-editor.js` 的重叠归属；
- 109 处裁定中「上游改动携带真实修复」的比例与逐条清单。
