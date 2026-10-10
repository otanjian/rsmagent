# design: 去 opencode 品牌标识（简化 MVP · 覆盖式同步下）

> 状态：方案（未实施）。目标品牌 **RSM Code**；外链 host 待定（见 §6）。
> 经复核，采用**最小可行方案**：两个上游已有接缝（运行时插件 + 既有 vite 覆盖层）+ 一条门禁，不新建框架、不改上游跟踪文件。

## 1. 为什么可以更简单（复核结论）

初版方案曾引入 `branding/` 六件套框架、12+ prompt 文件锚点补丁、内部命名全量迁移。复核后判定属于过度工程，理由与替代如下：

| 初版设计 | 复核发现 | MVP 替代 |
| --- | --- | --- |
| 为改模型自认知维护 12+ prompt `.txt` 锚点补丁 | 系统提示词在发送前经过可改写钩子 | **运行时插件**，零文件改动（§2.1） |
| 新建 `apply.ts` 重放器框架 | 已有 vite 覆盖层就是"覆盖后仍存在"的接缝 | **扩展既有 2 个 vite 配置**（§2.2） |
| 新建 `scan.ts` 扫描工具 | 已有 `check-upstream-sync.py` 统一入口 | **加一层**（§3） |
| 内部命名全量迁移 | 用户界面看不到；高风险（伤 SAP 工作台/MCP/部署脚本） | **不做**（§5） |
| 60+ 语言 i18n 正则 | 实际只用到 en/zh | **仅 en/zh** |

## 2. 两层实现

### 2.1 模型自认知：运行时插件（零上游文件改动）

**证据链**：`session/system.ts` import 各 `prompt/*.txt` 并由 `provider(model)` 选择；该内容在 `prepare` 中进入系统提示词数组；数组随即经过插件钩子，钩子允许原地改写：

- `packages/opencode/src/session/llm/request.ts:57-74`：组装 `system` 后 `plugin.trigger("experimental.chat.system.transform", …, { system })`。
- `packages/plugin/src/index.ts:291-297`：钩子签名 `output: { system: string[] }`，可原地修改。
- 插件加载：`packages/core/src/config/plugin/external.ts:45-69` 读取 config `plugins` 数组，并自动 glob 各配置目录（含项目 `.opencode/`、全局配置目录）下的 `{plugin,plugins}/*.{ts,js}`。

**做法**：写一个插件（`opencode/` 树外，如项目 `.opencode/plugin/rsm-identity.js` 或全局配置目录），在钩子里把 `You are opencode/OpenCode`、`Your name is opencode` 改写为 `RSM Code`，并把 `opencode.ai/docs`、`github.com/anomalyco/opencode` 改写为自有地址或删除相关段落。**一次覆盖所有模型与所有 prompt 文件**，无需维护锚点。

**兜底**：钩子为 `experimental.*`。加一条**启动探针/冒烟**（向 Agent 问「你是谁」）断言身份已改写；若上游移除/改名该钩子，冒烟失败即可发现。

### 2.2 Web 品牌：扩展现有 vite 覆盖层

复用 `packages/app/vite.rsm-public.config.ts` 与 `vite.rsm-sap.config.ts`（既有 `enforce:"pre"` transform 插件 + `— upstream changed?` 守卫惯例），新增：

| 目标 | 手段 |
| --- | --- |
| `packages/app/index.html` 标题/favicon/manifest/og | `transformIndexHtml`：`<title>OpenCode</title>` → `RSM Code`，资产 href 指向自有 |
| i18n（仅 `en.ts`/`zh.ts`） | 正则 `\bOpenCode\b → RSM Code`、`opencode.ai/zen →` 自有地址；仅作用字典文件 |
| Logo（`packages/ui/src/components/logo.tsx` 的 `Mark`/`Splash`/`Logo`） | vite `resolve.alias` 指向自有组件 |
| 外链（`entry.tsx` 通知图标、`layout/helpers.ts` favicon、`layout.tsx`/`home-projects-controller.tsx`/`error.tsx` 的 desktop-feedback） | 替换为自有地址 |
| 品牌资产（`packages/app/public/*`） | 覆盖为品牌资产；manifest `name/short_name` |

不新建 `branding/` 目录框架；`brand.json`（或共享常量）只承载**品牌名与对外地址**这一单一来源，供插件与两个 vite 配置引用。Provider 弹窗的 Zen 文案只改 i18n；`packages/core/src/plugin/provider/opencode.ts`（上游网关，功能性）部署侧隐藏，不重建。

## 3. 门禁（搭车既有脚本）

在 `rsmagent/scripts/check-upstream-sync.py` 增加"品牌泄漏"层：对 Web 构建产物与关键构建字面量扫 `(?i)opencode`，白名单 = 包 scope、`x-opencode-directory`、service tag、`仅作来源说明`的提及（对齐 `upstream-link-retargeting` 对 Markdown 正文/注释的豁免）。命中非白名单即失败。辅以 §2.1 的身份冒烟。

## 4. 取舍与风险

| 取舍 | 说明 |
| --- | --- |
| 依赖 `experimental.chat.system.transform` | 上游若移除/改名则身份改写失效；以启动探针/冒烟发现，非静默 |
| 保留上游跟踪文件不编辑 | 覆盖式同步天然安全；`vite.rsm-*.config.ts` 与插件均在定制层 |
| 不带内部命名改名 | 深挖用户仍可见 `.opencode`/`OPENCODE_*`；若不可接受，另立迁移 change |
| 合规 | 保留 `LICENSE` 与版权声明，只去面向用户的品牌呈现 |

## 5. 明确不做（移出本 change）

- 内部命名改名（`.opencode/`、`opencode.json`、`OPENCODE_*`、xdg 目录、localStorage 键、项目缓存、`.opencode-version`）及任何迁移。
- TUI 字标/终端标题、CLI 命令名与输出文案（本部署经 `serve` + Web UI，未加载 TUI）。
- 60+ 语言 i18n（仅 en/zh）。
- 桌面端（本次不发布）。
- `apply.ts`/`scan.ts`/`templates`/`patches` 等新框架。

以上如需，另立独立 change，避免与"看得见的标识"捆绑。

## 6. 待定项（实施前须闭环）

1. **外链目标口径**：本 change 暂取 `rd.rsmxm.com.cn`（与当前部署一致），但 `upstream-link-retargeting` 现行取值为 `https://www.rsm.global/china/zh-hans`。二者须统一为**单一决策**并落入 `brand.json`；未统一前门禁白名单不得同时放行两套域名。
2. 品牌资产（logo/favicon/manifest/social-share）由谁提供、格式与尺寸。
