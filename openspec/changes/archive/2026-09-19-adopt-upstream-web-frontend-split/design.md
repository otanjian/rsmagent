## Context

上游 `refactor/split-web-channel` 删除了 `channel/web/static/js/console.js`（14413 行）与 `static/css/console.css`（3671 行 base 行），改为 `static/js/{core,chat,views}/*` 与 `static/css/*` 的模块化前端，并把 `chat.html` 收敛为薄 shell。`adopt-upstream-web-split` 已把上游这套文件引入工作树，但 fork 仍服务自己的单体（该 change 的 `evidence/17`）。

Phase 3 因此是一个**已有输入、只差执行**的改造：

- `scripts/migration/port_frontend.py` 已能按归一化 diff 把 fork 定制再锚定到上游模块，实测 `console.js` 362 簇移植 275、`console.css` 79 簇移植 71，产出 25 个 JS 模块全部 `node --check` 通过；
- `scripts/migration/build_frontend_adjudication.py` 已产出 98 处需人工裁定的区域（JS 98 + CSS 8 + 跨边界 3，跨 23 个模块）；
- `scripts/migration/verify_frontend_port.py` 已能以 fork 独有行为判据复核「零未交代」（`console.js` 5222 行 + `console.css` 1361 行）。

未做的是：109 处裁定、覆盖映射接线、manifest 门禁、删除单体、`.cjs` 与浏览器验收。

## Goals / Non-Goals

**Goals**

- fork 定制落在 fork 拥有的模块，上游视图模块零 fork 改动；
- 109 处两侧相撞的区域逐处裁定并留痕，不静默丢弃任何一侧；
- 上游对这些模块的后续改动可检测（manifest + 漂移门禁），不重复失血；
- 删除单体后控制台行为与迁移前一致，装载顺序与语法有可执行校验。

**Non-Goals**

- 不改后端路由、授权与响应语义（属 `adopt-upstream-web-split`）；
- 不引入打包器或构建期合并产物（上游无此约定，且会把冲突从源文件挪到产物）；
- 不为「迁移期」保留双装载路径——过渡期就是单体本身，一旦切换即单向。

## Decisions

### F1 — 覆盖映射承载 fork 定制，而非「之后装载 + 重声明」

**决定**：fork 模块位于 `static/js/fork/<上游子路径>`、`static/css/fork/<上游子路径>`；fork 页面处理器经上游 `core/template.py` 组装页面后，按覆盖映射把 `assets/js|css/**` 引用替换为 fork 版本。

**理由（含被实测推翻的初版假设）**：初版假设「fork 定制可成为独立模块，在上游模块之后装载并附着到挂载点」。上游 `channel/web/README.md` 明确前端是**共享同一全局作用域的经典脚本**、无打包器，且同一顶层名在两个文件中声明即 `SyntaxError`、整页白屏（顶层 `const`/`let`）。而实测 fork 定制集中在 7 个上游模块内部（占增行 84%：`core/auth.js` 1320、`views/agents.js` 1034、`views/sessions.js` 1029、`views/channels.js` 923、`core/version.js` 894、`core/nav.js` 729、`views/config.js` 653），是既有函数体内的改写，不是可附着的独立单元——「之后装载」既会触发重名白屏，也改不动定制代码所读的 `const`。

**代价与对冲**：fork 因此拥有 33 个 JS 模块中的 25 个、8 个 CSS 模块中的 5 个。代价是上游对这些模块的后续改动不会自动流入，故以 F3 的 manifest 与漂移门禁对冲。

**备选与否决**：① 在上游模块之后装载并重新声明（否决：顶层重名 `SyntaxError` 白屏，且改不动定制代码读取的 `const`）；② fork 直接改写上游 `js/views/*.js`（否决：下次上游重构重复失血）；③ 构建期合并出单一 bundle（否决：把冲突从源文件挪到产物，且上游无构建期扩展约定）。

### F2 — 109 处裁定逐处记录，禁止整函数照抄

**决定**：对 `frontend_adjudication.md` 的 109 处逐处给出 `fork` / `upstream` / `merged` 与理由，结论并入移植器输入后重跑，并以 `verify_frontend_port.py` 复核。

**理由**：自动整函数移植对 87 处 JS 中的 56 处可机械适用，但会整体覆盖上游同名函数、静默丢弃上游在该函数内的改动——正是本 change 要消除的失血方向；反向取上游则丢弃 fork 定制。二者都不能自动判定，故必须逐处裁定。裁定必须先看上游改动是否携带功能或安全修复。

**校验独立性**：`verify_frontend_port.py` 以**fork 独有行**为准核对（`console.js` 5222 行、`console.css` 1361 行），并自行复跑 `node --check`，不采信移植器自述的统计。

### F3 — manifest 记录上游指纹，门禁按指纹失败

**决定**：`static/js/fork/manifest.json` 形如 `{fork_path: {upstream_path, upstream_sha256}}`；门禁在上游来源内容变化时失败并列出路径（与 `adopt-upstream-web-split` 的后端漂移守护同一机制）。

**理由**：fork 模块与上游模块**本就有意不同**，故不能以「文件内容相同」为判据；只能比对登记时的上游指纹。门禁须能在 CI 独立运行，使上游改进或安全修复不会静默丢失。

### F4 — 删除单体的前置条件是裁定齐备，不是时间点

**决定**：`console.js` / `console.css` 在 109 处裁定齐备且 `verify_frontend_port.py` 报零未交代后删除，不留兼容层。在此之前保留单体是**显式基线决策**（`adopt-upstream-web-split` 的冲突基线已按 `UD` + `keep-fork` 登记并写明「Phase 3 完成前不得按删除处置」）。

**理由**：删除是单向门。提前删除会丢弃尚未迁出的 fork 前端；而把它做成「随合并一起删」则等于默认丢弃。故前置条件写成可判定的形式（裁定记录齐备 + 独立校验零未交代），而不是「Phase 3 开始时」。

### F5 — 与后端 change 的顺序：后端先交付，前端随后

**决定**：本 change 独立于 `adopt-upstream-web-split` 交付；后者合入 `rdai` 不等待本 change。

**理由**：保留单体不影响合并后线上行为——控制台逐字节相同，后端冲突才是合并的硬阻塞。把两者绑在一起会让已解冲突、回归已绿的合并继续等待一次纯结构改造。`adopt-upstream-web-split` 的 D5 备选③ 已把「降级为 keep-fork + 逐条列出被丢弃上游增量」写成合法路径，本 change 即该路径的落点。

**代价**：合并后一段时间内，上游前端改进（`views/knowledge.js` 的空状态修复、拆分 shell、地址栏路由、`?v=` 版本戳、一键更新菜单）不在 fork 控制台生效。逐条清单见本 change 的 `evidence/deferred-upstream-frontend.md`，随本 change 关闭。

### F6 — 迁移顺序：先接线覆盖映射，再删单体

**决定**：先让 fork 页面处理器按覆盖映射装载 fork 模块并跑通浏览器验收，再删除 `console.js` / `console.css`。

**理由**：覆盖映射接线是可回退的（改回装载单体即可），删除不可回退。先接线使「迁移后行为与迁移前一致」可在单体仍在时逐场景对照，而不是删除后才发现差异。

## Risks / Trade-offs

- **上游对本批模块的后续改动不会自动流入**：由 F3 门禁把「静默丢失」变成「必须人工处理」；代价是每次上游改动需要人工重新应用，这是 D2 已接受的同一代价。
- **fork 拥有 25/33 个 JS 模块**：覆盖面大意味着上游演进时的人工成本集中在这里；对冲是覆盖映射只有一条声明、可校验、可逐模块回退到上游版本。
- **109 处裁定是人工密集工作**：以 `verify_frontend_port.py` 的「零未交代」为完成判据，避免以「大致迁完」收尾。
- **删除后无兼容层**：回滚路径是 `git revert` 该删除提交，不是保留双装载——双装载会引入重复顶层声明，反而不可用。

## Migration Plan

1. 接线覆盖映射，fork 页面处理器装载 `js/fork/**` + `css/fork/**`；单体仍在，可对照。
2. 逐处裁定 109 区域，结论并入移植器输入后重跑，`verify_frontend_port.py` 报零未交代。
3. 生成 `manifest.json` 并加漂移门禁；`node --check` 全量 + `tools/check-load-order.mjs` 通过。
4. `.cjs` 套件与浏览器验收（登录、上下文切换、流式请求、上传回读、下载预览），解除三处因该分歧而加的 skip。
5. 删除 `console.js` / `console.css`，更新冲突基线处置，不留兼容层。
6. 全量回归与路由覆盖校验复跑。

**恢复**：步骤 1–4 可回退（改回装载单体、删除新增 fork 模块）；步骤 5 之后以 revert 该提交恢复；两阶段都不改变后端，故不涉及数据恢复。

## Open Questions

- 覆盖映射的落地位置：服务端在组装阶段替换引用，还是前端装载器按映射取模块。倾向前者（不改上游 `core/template.py` 语义，且与上游 `?v=` mtime 版本戳兼容）。
- `static/js/doc-editor.js` / `workspace.js` 与上游 `assets/js/doc-editor.js` 的重叠：需判定是「上游文件的 fork 版」（应纳入覆盖映射）还是独立文件（留在 fork 专有清单）。
- `core/i18n.js` 是反向差异（fork 把翻译移出到 `static/js/i18n/`，该模块 +79/−1295），不可按「移植 diff」处理，需单独裁定装载方式。
