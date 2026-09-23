# 容大AI平台帮助站

帮助站与主控制台共用 **Python + web.py** 服务，通过 **`/help/`** 访问。页面使用 web.py HTML 模板、原生 JavaScript 和现有 CSS；内容、配置与中英文文案使用 JSON。无需 PHP、额外端口、Node 构建或独立数据库。

## 启动与访问

按项目原有方式启动主服务，并启用 `web` 渠道。例如在仓库根目录执行：

```bash
.venv/bin/python app.py
```

主服务使用默认端口时，访问：

- 首页：`http://127.0.0.1:9899/help/`
- 使用手册：`http://127.0.0.1:9899/help/manual`
- 核心能力：`http://127.0.0.1:9899/help/features`
- 企业级管控：`http://127.0.0.1:9899/help/enterprise`
- 应用场景：`http://127.0.0.1:9899/help/scenarios`
- 场景详情：`http://127.0.0.1:9899/help/scenario/ecn`（`/tasks` = 推荐任务，`/demo` = Live Demo）
- 快速开始：`http://127.0.0.1:9899/help/quickstart`
- 系统架构：`http://127.0.0.1:9899/help/architecture`
- 关于：`http://127.0.0.1:9899/help/about`
- 能力文档：`http://127.0.0.1:9899/help/doc?p=memory`

`/help` 会跳转到 `/help/`；`/help/index.php`、`/help/doc.php?p=...` 等迁移前页面名会重定向到对应的新地址。原独立 PHP 域名若仍有访问者，需要由部署方将其重定向到主服务的 `/help/`。

迁移前的 PHP 站点源码（`*.php`、`includes/`、`lang/*.php`、`docs/manifest.php`、`tools/*.php`）**已从仓库删除**：页面结构、配置、文案与文档清单现在分别由 `templates/`、`config.json` / `content.json` / `icons.json`、`lang/*.json` 与 `docs/manifest.json` 承载，仓库不再需要 PHP 运行时。旧地址重定向由 `webhelp/site.py` 与 `channel/web/help_site.py` 处理，不依赖这些文件。反向回退到 PHP 站点需要从版本历史取回（本目录相关文件在删除前的那一次提交）。

主控制台“帮助与关于”固定打开同源 `/help/`，不会受到品牌官网链接或旧独立站地址影响。帮助页及其公共资源允许匿名读取，页面不读取租户数据。

## 目录与维护

| 文件或目录 | 用途 |
| --- | --- |
| `site.py` | 请求级视图、双语解析、链接、文档及组件渲染 |
| `templates/*.html` | 页面模板；使用 web.py 自带模板引擎 |
| `config.json` | 品牌、导航、部署命令、站外链接白名单与展示配置 |
| `content.json` | 能力卡片、手册主题和步骤等结构数据 |
| `scenarios.json` | 应用场景快照：来源、抓取日期、深链模板、分组与 63 条场景 |
| `scenario_docs.json` | 场景详情快照：63 个场景 × （场景引入 / 推荐任务 / Live Demo）三份结构化正文 |
| `lang/zh.json`、`lang/en.json` | 双语文案 |
| `icons.json` | 原站内联 SVG 图标路径 |
| `docs/manifest.json` | 本地能力文档清单、分组和标题 |
| `docs/*.html` | 已净化的中文文档正文 |
| `assets/` | 原有 CSS、JS、图片、视频与公开部署脚本 |
| `tools/check_manual.py` | 离线检查手册、双语文案、截图和链接 |
| `tools/check_scenarios.py` | 离线检查场景快照、双语文案、站外链接白名单与页面渲染 |
| `tools/check_scenario_docs.py` | 离线检查场景详情快照、原语与标签白名单、站外依赖与「文本不丢」 |
| `tools/build_docs.py` | 显式执行时同步文档的开发工具 |

主服务路由与静态资源处理在 `channel/web/help_site.py`，通过 `channel/web/route_registry.py` 注册；不需要修改独立的权限路由表。

`config.json` 的 `site_url` 可留空，快速开始页会自动使用当前主服务的 `/help` 地址构造部署脚本 URL。如果需要指定公开域名，可填写完整帮助站地址，例如 `https://agent.example.com/help`。该配置只影响部署命令，不改变控制台的帮助入口。

语言通过 `?lang=zh` / `?lang=en` 切换，并由 `webhelp_lang` Cookie 记忆；Cookie 限定在 `/help` 路径。文档正文保持原有中文内容，导航与标题支持双语。页面保留深浅色主题、移动导航、代码标签页、复制按钮和手册原图链接。

新增手册步骤时，在 `content.json` 声明步骤 ID 与截图路径，并同步维护两份语言包的 `manual.topics.<主题>.steps.<步骤>.title/body`。截图位于 `assets/img/manual/`，每步一张，文件名前缀与主题 ID 一致。

HTML 模板默认转义普通变量；`$:` 仅用于经过视图转义的组件及已净化的本地文档正文。不要在这里放置用户上传的未经净化的 HTML。

## 应用场景页（场景目录）

`/help/scenarios` 按领域列出 63 个已接入容大AI平台的场景智能体，支持按岗位筛选与关键词搜索。清单唯一来源是 `scenarios.json`：`groups` 记录分组，`items` 记录场景名称、领域、岗位、痛点、使用说明、业务资料提示与演示任务。

- **一键体验**：`assets/js/main.js` 跳转当前站点的 `/?open_agent=<智能体键>&message=...`，控制台打开对应智能体并发送“你能做什么？”。用户了解能力后，再发送“用内置演示数据跑一遍”或上传自己的文件并描述任务。已登录用户应先切换到所需租户；找不到智能体时联系租户管理员确认授权。
- **推荐任务**：复制任务到同名智能体对话中发送。第一条为完整演示，其余任务提供不同分析角度；涉及额外月份、历史记录等资料时需补充文件。处理真实业务时，应将演示描述改为本次上传的文件和业务范围。
- **查看做法**：指向本地详情 `/help/scenario/<slug>`。列表底部可直接进入平台或查看使用手册。
- **来源记录**：`source`、`captured_at` 与 `detail_url` 保留原始材料的来源信息供维护者追溯，不作为用户操作入口，也不下发外部安装脚本。界面与提示词使用平台自身的智能体操作说明。
- **界面语言**：界面文案随 `?lang=` 切换；场景业务正文保留中文。

## 场景详情页（本地内容）

每个场景有三个本地页面：

| 地址 | 内容 |
| --- | --- |
| `/help/scenario/<slug>` | 场景引入、处理规则与交付说明 |
| `/help/scenario/<slug>/tasks` | 推荐任务（可一键复制）与资料准备方式 |
| `/help/scenario/<slug>/demo` | 场景演示，展示处理步骤与交付形式 |

正文唯一来源是 `scenario_docs.json`：`catalog` 登记场景名称与分组，`items` 按 slug 分组，每个场景含 `intro` / `tasks` / `demo` 三份文档。`source`、`extracted_at`、`extractor` 保留材料来源与初始抽取记录；正文已按容大AI平台的操作方式修订。演示用于说明业务流程，实际结果以本次资料与核对结果为准。

- **正文结构**：`blocks` 使用 `heading`、`prose`、`note`、`cta`、`pains`、`compare`、`cards`、`steps`、`demo_steps`、`outputs`、`files`、`score`、`tasks`、`table`、`chips`、`neutral` 等固定类型。未知类型会使校验失败。
- **标签与链接**：正文只允许少量强调、换行、列表标签；来源页链接已移除。原始来源地址由 `config.json` 的站外主机白名单校验，不用于客户端唤起。
- **文本完整性**：每页 `text` 保存已审核的平台文案基线。修改文案时需同步更新相应片段，保留其他业务内容的覆盖；校验器同时检查数据与渲染结果，防止丢失正文。
- **内容维护**：历史抓取脚本保留在 `openspec/changes/` 供来源追溯。重新抓取时先输出到临时文件，核对业务内容并适配平台操作说明后再合入，不能直接覆盖当前平台文案。完成后运行场景目录与详情校验器。

## 静态资源缓存

`config.json` 的 `version` 会以 `?v=` 追加到 `assets/` 下的 CSS/JS 引用上，响应带 `max-age`。**改动 `assets/css/style.css` 或 `assets/js/main.js` 时必须同步改 `version`**，否则浏览器会继续用旧样式（表现为新组件「没有样式」，而服务端返回的文件其实是对的）。

## 校验

在仓库根目录执行：

```bash
.venv/bin/python webhelp/tools/check_manual.py
.venv/bin/python webhelp/tools/check_scenarios.py
.venv/bin/python webhelp/tools/check_scenario_docs.py
.venv/bin/python -m pytest tests/test_help_site.py tests/test_help_site_url.py tests/test_route_registry.py -q
node --test tests/test_sidebar_account_frontend.cjs
```

路由测试覆盖中英文页面、全部文档、189 个场景详情页、站内链接与资源、语言 Cookie、旧地址重定向、404、路径穿越及软链接边界、资源缓存与范围读取；站外链接按 `config.json` 的白名单校验，白名单之外的目标会让页面测试失败。

## 文档同步

现有本地文档随仓库发布。站点运行时不会抓取外网。只有维护者显式执行以下命令才同步来源站：

```bash
.venv/bin/python webhelp/tools/build_docs.py --source-url https://docs.example.com
```

将示例地址换成真实文档源的根地址。来源需提供清单中登记的文档路径和 `#content` 正文容器。工具抓取已登记文档及正文中的关联页面，过滤脚本和外链、本地化配图，并生成 JSON 清单。所有页面抓取和校验完成后才替换现有正文；失败不会以空内容覆盖已有文档。

## 打包

Docker 本地构建包含仓库内容；桌面 PyInstaller 配置已加入帮助站模板、JSON（含 `scenarios.json` 与 `scenario_docs.json`）、文档及静态资源。发布桌面安装包时需重新构建 Python 后端。修改 Python 或模板后重启主服务生效。
