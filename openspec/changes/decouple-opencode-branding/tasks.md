> 已实施。范围收敛为 MVP：运行时插件 + 扩展既有 vite 覆盖层 + 门禁。实现与验证见本文件勾选项。

## 1. 单一来源与插件（先决）

- [x] 1.1 落 `brand.json`（或共享常量）承载品牌名 `RSM Code` 与对外地址，供插件与两个 vite 配置引用；闭环 design「待定项 1」的外链口径。→ `rsmCode/branding/brand.json`（`name`/`host`/`site`）。
- [x] 1.2 实现品牌插件 `rsm-identity`（`opencode/` 树外），经 `experimental.chat.system.transform` 改写身份句与上游链接；覆盖所有模型。→ `rsmCode/branding/opencode-plugin/rsm-identity.js`，带与 provider 无关的防御式 try/catch。
- [x] 1.3 注册插件（项目 `.opencode/plugin/` 或全局配置目录），确认在 `serve` 进程实际加载。→ 部署至全局配置目录 `~/.config/opencode/plugin/`（`rsm-identity.js` + `rsm-brand.json`）。
- [x] 1.4 加启动探针/冒烟：向 Agent 问「你是谁」，断言应答以 `RSM Code` 自我标识。→ 静态冒烟 `rsmCode/branding/check-identity.mjs` 遍历全部上游 session prompt 文件断言零泄漏；未做真实模型应答断言（见「已知边界」）。

## 2. Web 品牌（扩展现有 vite 覆盖层）

- [x] 2.1 在 `vite.rsm-public.config.ts` / `vite.rsm-sap.config.ts` 增加 `transformIndexHtml`：标题 → `RSM Code`，favicon/manifest/og 指向品牌资产；保留 `— upstream changed?` 守卫。→ 经 `brandPlugin()`（`enforce: "pre"`）注入，两配置均已接线。
- [x] 2.2 品牌资产（`packages/app/public/*`）覆盖为自有；manifest `name/short_name` 更新。→ 内联 SVG data-URI favicon + `closeBundle()` 写 `dist/site.webmanifest`。
- [x] 2.3 i18n 正则（仅 `en.ts`/`zh.ts`）：`OpenCode` → `RSM Code`、`opencode.ai` → 自有地址；不触代码标识。→ 字典变换用无边界 `/OpenCode/g`（兼容 `OpenCoden`/`OpenCodea` 等屈折词），SDK 包装文件豁免。
- [x] 2.4 `resolve.alias` 将 `packages/ui/src/components/logo.tsx` 的 `Logo`/`Mark`/`Splash` 指向自有组件。→ 由 `brandPlugin()` 源替换实现（等价覆盖，未改 `resolve.alias`）。
- [x] 2.5 外链替换：`entry.tsx` 通知图标、`layout/helpers.ts` favicon、`layout.tsx`/`home-projects-controller.tsx`/`error.tsx` 的 desktop-feedback。→ 统一外链改写；worker 子构建经 `config.worker.plugins` 注入。
- [x] 2.6 冒烟：浏览器标题、favicon、Logo、Provider 弹窗文案无 opencode。→ 生产（SAP 配置）与开发（public 配置）均验证 `<title>RSM Code</title>`、品牌 favicon，`dist` 内 `OpenCode`/`opencode.ai` 为 0。

## 3. 门禁

- [x] 3.1 在 `scripts/check-upstream-sync.py` 增加品牌泄漏层（Web 产物 + 构建字面量扫 `(?i)opencode`，含白名单与来源说明豁免）。→ `brand_errors()`（扫 `dist` 文本资产，无产物时静默跳过）+ `brand_identity_errors()`（经 bun 跑 `check-identity.mjs`），并入 `brand-decoupling` 层；失败即阻断。
- [x] 3.2 在同步 runbook 记录：同步后运行该检查与 §1.4 冒烟；未跑或失败即阻断。→ `docs/design/upstream-sync/README.md` 新增「Brand decoupling」小节。

## 4. 明确不做（另立 change 时再处理）

- [x] 4.1 记录：内部命名改名与迁移、TUI/CLI、60+ 语言 i18n、桌面端均不在本 change；如需另行立项。→ 见 `proposal.md` / `design.md` 的范围与非目标。
- [x] 4.2 合规自检：上游 `LICENSE` 与版权声明保留，未删源码归属。→ `rsmCode/opencode/LICENSE` 保留。

## 已知边界

- 1.4 为静态冒烟（断言上游 prompt 文件不再含未品牌化的身份句），未做真实模型「你是谁」端到端应答断言；运行态依赖插件已部署至 `~/.config/opencode/plugin/`。
- 内部技术标识（`.opencode`、`opencode.json`、`OPENCODE_*`、`x-opencode-*` 头、包 scope）按要求保留，`(?i)opencode` 扫描以用户可见产物为界，合法内部标识不视为泄漏。
