## Why

`rsmCode/opencode` 是从上游 `anomalyco/opencode` 拉取的整树 fork，且**每次同步为覆盖式重拉**：写进 `opencode/` 树的跟踪文件改动会在下一轮同步丢失。用户可见的 openCode 标识仍出现在 Web 标题/图标/界面文案、模型系统提示词（用户一问「你是谁」即暴露）、以及指向 `opencode.ai` 的外链中，与运营方产品归属不一致。

`fork-upstream-decoupling` 与 `upstream-link-retargeting` 已确立强制要求：**定制逻辑必须位于稳定接缝，MUST NOT 在上游核心文件就地散落硬编码**。因此「逐文件硬改并提交」既不可持续（同步即丢），也违反既有规范。

复核后确定**最小可行方案（MVP）**：只用两个上游已存在的接缝——**运行时插件**（改模型自认知）与**既有 vite 构建覆盖层**（改 Web 品牌）——加一条门禁，**不为本能力新建框架，也不改上游跟踪文件**。内部命名改名、TUI/CLI、桌面端等不属于"用户看得见的标识"且风险独立，移出本 change（见 design「明确不做」）。

本 change 只产出方案文档，**暂不写代码**。

## What Changes

- **模型自认知经运行时插件解耦**：经 `experimental.chat.system.transform` 钩子在运行期把系统提示词中的上游身份句与外链改写为 `RSM Code` 与自有地址，**零上游文件改动**（覆盖所有模型、所有 `session/prompt/*.txt`）。插件位于 `opencode/` 树外（项目 `.opencode/plugin/` 或全局配置目录），不受覆盖式同步影响。
- **Web 品牌经既有构建覆盖层承载**：扩展现有 `vite.rsm-public.config.ts` / `vite.rsm-sap.config.ts`，新增 `transformIndexHtml`（标题/favicon/manifest/og）、i18n（**仅 en/zh**）、Logo alias、外链与资产覆盖。复用既有"上游覆盖后仍存在"的接缝，不新建 `branding/` 框架。
- **品牌与对外目标由单一配置来源承载**：品牌名与外链目标集中于一处（`brand.json` 或共享常量），各面引用，符合既有规范。
- **门禁搭车既有检查脚本**：在 `scripts/check-upstream-sync.py` 增加品牌泄漏层（对 Web 产物与构建字面量扫上游标识），不新建独立扫描工具。
- 明确不纳入本 change：内部命名改名（`.opencode`/`OPENCODE_*` 等）、TUI/CLI、60+ 语言 i18n、桌面端。

## Capabilities

### New Capabilities

- `opencode-brand-decoupling`: 规定开放代码智能体（opencode fork）在**覆盖式上游同步**下，面向上游文件**零改动或被既有构建覆盖层承载**的品牌解耦能力：模型自认知经运行时插件改写、Web 品牌经既有构建覆盖层改写、品牌与对外目标由单一来源承载、品牌泄漏由既有检查脚本拦截；并保留上游源码归属与许可证。

### Modified Capabilities

- 无。`upstream-link-retargeting` 与 `fork-upstream-decoupling` 的要求被复用与引用，不修改其规范文本；外链目标口径差异以 design「待定项」登记。

## Impact

- 上游目标树：`rsmCode/opencode/packages/app`（仅构建覆盖层）、`packages/opencode/packages/core` 通过**插件**间接影响，**不直接编辑上游跟踪文件**。
- 新增（`opencode/` 之外，同步不覆盖）：品牌插件 1 个文件、`brand.json` 单一来源、品牌资产。
- 扩展：`vite.rsm-public.config.ts`、`vite.rsm-sap.config.ts`（既有覆盖层）、`rsmagent/scripts/check-upstream-sync.py`（新增一层）。
- 明确不含：内部命名迁移（不改 `~/.config/opencode`、`.opencode/`、`OPENCODE_*`）、TUI/CLI、桌面端。
- 合规：保留上游 `LICENSE` 与版权声明，仅去除**面向用户**品牌呈现。
- 证据边界：本 change 为方案文档，未实施、未验收。
