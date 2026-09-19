# Tasks — 控制台前端迁移到上游模块化布局

本 change 承接 `adopt-upstream-web-split` 的 Phase 3。前置输入（移植器、裁定工作清单、独立校验工具）已在该 change 内完成，产出随本 change 的 `evidence/` 交付。

## 0. 承接状态与前置

- [x] 0.1 确认前置在位：上游 `channel/web/static/js/{core,chat,views}/**`、`channel/web/static/css/*`、`chat.html` shell 与 `templates/**` 已在工作树，且 `channel/web/static/js/console.js` / `console.css` 仍在（`adopt-upstream-web-split` 按 `UD` + `keep-fork` 登记）
  - 2026-09-19 实测：`channel/web/static/js/{core,chat,views}`、`channel/web/static/css`、`chat.html`、`templates/` 均在位；`console.js`（20229 行）/`console.css`（6048 行）保留
- [x] 0.2 确认 `scripts/migration/{port_frontend,verify_frontend_port,build_frontend_adjudication,analyze_frontend_divergence}.py` 可重复运行，且以固定上游 `8f1b19f1` 为输入；记录本轮输入的 ref 三元组（base / upstream / fork）
  - 三元组已冻结并写入 `evidence/inherited-state.md`：`base=e5e2a52d`、`upstream=8f1b19f1`、`fork=c6eb33db`。三个脚本均接受 `FORK_{BASE,UPSTREAM,FORK}_REF` 覆盖，默认 `FORK_FORK_REF=HEAD`，故重跑必须显式指定 `c6eb33db`，否则分母随 HEAD 漂移
- [x] 0.3 冻结本轮 fork 独有行基线：`console.js` 5255 行、`console.css` 1361 行（合计 6616），作为「零未交代」的分母
  - 冻结于 `fork=c6eb33db`：3508 已入移植产出、3108 在裁定清单上，`verify_frontend_port.py` 报 `PASS: 6616/6616`、`0 UNACCOUNTED`。父 change `evidence/09-*` 的 98 处 / 6583 行是**旧基线**实测值（收口提交 `c6eb33db` 再次改动了 `console.js`：三个搜索提供方的凭据弹窗接线），不作为本 change 的输入；刷新记录与命令见 `evidence/inherited-state.md`
- [x] 0.4 逐条登记 Phase 3 期间被延后的上游前端增量（拆分 shell、地址栏路由、`?v=` 版本戳、一键更新菜单、`views/knowledge.js` 空状态修复等），作为交付时必须收口或显式转交的清单
  - 已产出 `evidence/deferred-upstream-frontend.md`：§A 结构类（A1–A5，含 A5 上界 = 109 处裁定区域）、§B1 已只缺 UI（后端半边已在位，逐条给提交与现状）、§B2 纯前端修复 14 条、§C 与裁定清单的收口判据、§D1 桌面端未路由接口。生成命令按 §A 头部可重跑，不手抄
- [x] 0.5 说明与 `scripts/conflict-baseline.txt` 的范围边界：该基线是父 change `adopt-upstream-web-split` 的冲突集，本 change 只认领其中的前端三行（`channel/web/chat.html`、`channel/web/static/js/console.js`、`channel/web/static/css/console.css`，均为 `keep-fork`），并在任务 5.3 完成删除后更新它们的处置。基线的三条后端 `seam:` 行由父 change 负责，不在本 change 范围：`agent/memory/conversation_store.py`（`seam:conversation-store`）、`agent/tools/scheduler/integration.py`（`seam:scheduler`）、`tests/test_scheduler_web_update.py`（`seam:scheduler`）
- [ ] 0.6 收口**桌面端消费的未路由接口**（父 change `evidence/21` §E 登记）：`desktop/src/renderer/**` 随本轮同步按上游版本合并（`merge` 处置，非 `keep-fork`），其新界面调用 `/api/scheduler/runs`、`/runs/detail`、`/runs/delete`、`create`、`recipients`、`instances` 与 `/api/sessions/<id>/{context_usage,compact_context}` 共 8 条 fork 后端未路由的接口，而 fork 的桌面端此前 0 处调用。逐条二选一：接上路由与授权判定，或在桌面端降级/隐藏入口；不得以「属拆分后前端」为由留空。清单与影响见本 change `evidence/deferred-upstream-frontend.md` §D
  - 运行期形状已由父 change 的 §6.4 验收实测固定（`evidence/23` §3）：6 条 scheduler 路径回答 **404**；`/api/sessions/<id>/{context_usage,compact_context}` 被既有 `/api/sessions/(.*)` 捕获后回答 **405**——这两条不是「未注册」，收口时须在会话详情 handler 上显式拒绝或补服务

## 1. 覆盖映射接线（先接线，后删除）

- [ ] 1.1 以「fork 拥有模块 + 服务端覆盖映射」实现 fork 前端定制：fork 页面处理器经上游 `channel/web/core/template.py` 组装页面后，按覆盖映射替换 `assets/js|css/**` 引用；不得在上游模块之后装载并重新声明同名顶层符号（顶层重名即 `SyntaxError`）
- [ ] 1.2 覆盖映射与上游 include 语义、按文件 mtime 的 `?v=` 版本戳兼容；fork 专有模块（`todos.js`、`identity-admin.js`、`scenes/` 等）装载顺序不变，`boot.js` 仍最后加载
- [ ] 1.3 接线后单体仍在，逐场景对照迁移前后行为一致（登录、上下文切换、流式请求、上传回读、下载预览）
- [ ] 1.4 判定 `core/i18n.js` 的处置：该模块为反向差异（fork 把翻译移出到 `static/js/i18n/`），不可按「移植 diff」处理，需单独裁定并记录装载方式

## 2. 109 处裁定（4.4c）

- [ ] 2.1 逐处裁定 `frontend_adjudication.md` 的 109 个区域（JS 98 + CSS 8 + 跨边界 3，跨 23 个模块），每处记录 `fork` / `upstream` / `merged` 与理由；集中处优先：`core/auth.js` 16、`views/agents.js` 12、`views/models.js` 11、`core/nav.js` 9、`views/sessions.js` 7、`core/i18n.js` 6
- [ ] 2.2 裁定不得整函数照抄：须检查上游在同区域的改动是否携带功能或安全修复，避免以 fork 版本整体覆盖；被否决一侧的处置须写明
- [ ] 2.3 裁定结果并入移植器输入后重跑 `port_frontend.py`，产出确定性（重复运行逐字节一致）
- [ ] 2.4 以 `verify_frontend_port.py` 复核零未交代，并自行复跑 `node --check`（不采信移植器自述）
- [ ] 2.5 记录「上游改动携带真实修复」的逐条清单与占比，供后续同步轮次参考

## 3. manifest 与漂移门禁（4.4d）

- [ ] 3.1 生成 `static/js/fork/manifest.json`，形如 `{fork_path: {upstream_path, upstream_sha256}}`，覆盖全部 fork 模块（JS 与 CSS）
- [ ] 3.2 加漂移门禁：上游来源模块内容变化即失败并指出需人工重新应用的路径；上游新增视图模块未登记同样失败
- [ ] 3.3 门禁可独立运行、无噪音（上游未改动时通过），并纳入 CI
- [ ] 3.4 为门禁写结构不变量测试：注入一次上游改动使其失败、复原后通过

## 4. 语法与装载顺序校验（4.4e/4.4f）

- [ ] 4.1 以 `node --check` 校验全部产出模块（当前 25 个 JS 模块 / 18597 + 4169 行通过，须在全量裁定后复跑）
- [ ] 4.2 以 `tools/check-load-order.mjs` 校验 fork 实际装载顺序，确认无重复顶层声明、`boot.js` 最后加载
- [ ] 4.3 处置 `static/js/doc-editor.js`、`workspace.js` 与上游 `assets/js/doc-editor.js` 的重叠：若为上游文件的 fork 版则纳入覆盖映射与 manifest，而非留在 fork 专有清单

## 5. 删除单体（4.4g/4.4h）

- [ ] 5.1 **删除前置条件**：2.1 的 109 处裁定全部完成且 2.4 报零未交代。在此之前不得删除 `console.js` / `console.css`
- [ ] 5.2 删除 `console.js` / `console.css`，不留兼容层；确认无上游视图模块（`js/views/*.js`、`js/core/*.js`、`js/chat/*.js`、`css/*.css`）被 fork 原地编辑
- [ ] 5.3 更新 `scripts/conflict-baseline.txt` 中该 `UD` 行的处置：由「keep-fork + Phase 3 完成前不得按删除处置」改为删除留痕并指向替代模块；`DELIBERATE_REMOVALS` 五项保持不变
- [ ] 5.4 确认缺少 fork 前端模块时独立上游形态仍可组装：上游视图保持可用，不白屏、不报错

## 6. 验收（4.5）

- [ ] 6.1 运行 `.cjs` 套件：`NODE_PATH="$(npm root -g)" node --test tests/test_fork_fragments.cjs`、`node --test tests/test_execution_permission_ui.cjs`，与切换前基线比较，不新增失败
- [ ] 6.2 浏览器验收：登录（含凭据提交路径）、上下文切换、流式请求、上传回读、下载预览；记录成功与拒绝两侧结果
- [ ] 6.3 解除因该分歧而加的 skip：`tests/test_web_console_assets.py`、`tests/test_web_console_routing.py`、`tests/test_web_console_update.py::test_frontend_contract`；`tests/test_tool_display.py`、`tests/test_personal_console_frontend.py` 改指新模块位置，不得删除测试或放宽断言
- [ ] 6.4 复跑全量回归与 `scripts/check-route-coverage.py`，确认后端路由与授权语义未因前端切换发生变化
- [ ] 6.5 桌面端验收：以本 change 产出（含 0.6 的结论）构建 `desktop/`（`desktop/dist` 为 gitignore 产物，由 `desktop/src` 经 vite 构建），逐项确认任务页运行历史/详情/删除、任务创建与收件人/实例选择、上下文用量与压缩在 fork 后端的**真实**结果（可用则成功路径，不可用则确认降级为空态/隐藏而非报错），记录两侧证据

## 7. 文档与交付

- [ ] 7.1 更新 Web 前端布局说明（模块职责、fork 模块边界、覆盖映射与 manifest 判据、校验入口）
- [ ] 7.2 收口 0.4 登记的延后增量清单：逐条给出「已随本 change 生效」或「显式转交下一轮同步」，不留未交代项
- [ ] 7.3 推送并向 `rdai` 创建 PR；正文含 Summary / 与 `adopt-upstream-web-split` 的边界 / 裁定统计 / 校验证据 / 回滚方式
