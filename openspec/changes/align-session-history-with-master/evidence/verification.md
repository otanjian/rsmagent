# 实施与验收（2026-10-02）

## 基线与差异

基线固定为 master `48c0d79c36146950667f8d1884ab823deae62305`，本地 `origin/master` 已核对。六处上游来源的 SHA-256 见 `sources.json`；JS 展示适配同步登记在 `channel/web/static/js/fork/manifest.json`，保留既有 memory 条目。

| 来源 | 采用／保留 |
| --- | --- |
| `templates/layout/session-panel.html` | 面板结构移植到 fork 模板；增加原归档／查看全部入口和共用节点挂载点 |
| `templates/layout/sidebar.html` | 采用 208px 主导航、56px 品牌、14px 菜单、8px／12px padding、分组 4px 间距与 8px 缩进；保留 rdai 名称、分区和配色 |
| `static/css/sessions.css` | 采用 220px 面板、紧凑行、分组／头像和下拉密度；只在面板容器覆盖，完整页维持宽列表；手机头部为新增历史按钮调整留白，避免租户名换行 |
| `static/js/views/sessions.js` | 采用默认收起、开关和手机抽屉行为；只新增展示适配，使用已有身份作用域偏好 |
| `static/js/core/nav.js` | 延用 rdai 的实际导航与离页保护；导航完成回调兼容异步确认后打开面板 |
| `static/js/chat/new-chat.js` | 复用 rdai 现有 NEW_CHAT_SURFACES 与创建流程，接回临时行和成员展示，不复制候选逻辑 |

删除旧侧栏预览及其独立请求、数组、截断、重命名实现。面板／完整页共用 `session-list`、`history-status`、新对话控件、分页和查询，切换失效旧请求。owner、搜索、项目操作、归档、coding、流处理留在现有实现处。临时行按 Agent／session 同键替换，拒绝管理写入。其他在途 memory／菜单改动原样保留，无业务数据迁移。

## 自动化验证

- 前端：145 项通过。覆盖历史搜索与异步失效、重命名 IME／Enter／Escape、流与标题刷新、面板切换／身份／手机、63 条分页、临时行正式替换、归档恢复、团队新建、coding、导航权限。
- 后端：66 项通过（`test_session_history_search.py`、`test_history_agent_workspace.py`、`test_session_archive.py`、`test_shared_conversation_roster.py`、`test_session_store_resolution.py`）。一项既有 `setDaemon()` 弃用警告。
- 扩展初检：另 10 项 workspace 测试通过；`test_web_console_assets.py` 的 12 项为仓库既有 skip（全站拆分尚未完成）。本切片额外用实际 `template.render('chat.html')` 检查：无重复 ID，49 个带版本戳资源全部存在且不重复；新适配在 console 后加载，上游 sessions.js 未重复装载。
- `openspec validate align-session-history-with-master --strict` 通过。
- `node --check`、`channel/web/tools/check-load-order.mjs` 和 `git diff --check` 通过；资源工具确认无使用前声明问题。
- 扩展 `test_recovered_pages_frontend.cjs` 检查时有 3 项 memory 静态断言失败：仍假定 memory 模板内联、`_memoryRefusal` 与 `loadMemoryView` 位于单体。这属于并行 memory 拆分，不是本 change 的历史路径；未修改其 memory 断言。该套其余 16 项通过。

前端命令：

```sh
node --test tests/test_session_history_frontend.cjs tests/test_session_history_refresh_frontend.cjs tests/test_sidebar_session_archive_frontend.cjs tests/test_workbench_sidebar_launch_frontend.cjs tests/test_session_panel_frontend.cjs tests/test_team_chat_launch_frontend.cjs tests/test_coding_frontend.cjs tests/test_nav_area_frontend.cjs tests/test_channel_scope_nav_frontend.cjs
```

旧预览独立重命名测试已退役；同一实际重命名处理的 owner／IME／Enter+blur／Escape 测试保留在历史测试中。新增测试针对展示接线，没有另建业务实现或双缓存测试体系。

## 浏览器验收

通过 CUA 在真实浏览器执行。使用生产模板装配、实际 JS/CSS 和独立内存 API fixture（`tests/support/session_history_preview.cjs`），未登录或修改 localhost:9899 的真实业务会话。以下是带测试数据的页面验收，不冒充生产账号端到端结果。

- 桌面 1280×720：初始无偏好收起；展开、关闭、离开完整页再返回正常。主导航 208px、品牌 56px、面板 220px、聊天和面板头部均 56px；普通历史行 35.5px，13px 字体、8px 10px padding；菜单 14px、8px 12px padding；下拉 13px、7px 9px padding、9px gap。
- 63 条会话：先读取 50 条，滚动读取第二页；项目计数从 17／33 变为 21／42，共 63 条。折叠项目后其他会话仍可访问；旧布局开关关闭也不恢复 5／10 条上限。
- 群聊：六名成员显示前三个头像及 `+3`，长标题截断并保留完整提示；当前项、编码图标、置顶图标可见。
- 完整页搜索 `63`：命中唯一第 63 条；返回面板显示未筛选列表，再进入完整页保持 `63` 搜索词及命中结果。
- 实际操作：重命名 `历史验收会话 05` 为 `已验证重命名`；归档后从正常列表移除；归档弹窗恢复成功。新建显示临时行且更多按钮隐藏。下拉提供既有单人／团队入口。
- 手机 390×844：刷新默认收起；显式展开有遮罩、主导航关闭；更多菜单含置顶／重命名／归档／删除；选中群聊后抽屉和遮罩关闭。页面宽 390px，无横向溢出，租户名保持单行。
- 深浅主题及旧启动布局开关抽查；主导航折叠保持原 72px，历史面板仍为 220px。最终验收页面无 console error。

截图：`desktop-menu.jpg`、`desktop-dark.jpg`、`mobile-panel.jpg`。它们只作为简洁人工对照，不设像素门禁或全组合矩阵。

## 范围

未改变归档弹窗 50 条上限；切换或刷新允许回到第一页；未添加数据库迁移、空会话持久化、通用模块加载器或草稿管理。主规范 Purpose 按既定流程在归档时与两份 delta 一起更新。

## 后续修正：移除重复新建入口（2026-10-02）

根据用户指出的重复入口，移除历史面板中的「新对话」，聊天时统一从左侧「新建对话」发起。完整历史页的新建控件固定保留在该页，不再随列表移动；删除面板按钮挂载点及专用样式。proposal、design、两份 delta、任务说明和使用说明同步修正。

本次 52 项相关前端回归、脚本语法、`git diff --check` 及 OpenSpec 严格校验通过。浏览器在实际模板与隔离 fixture 中确认：左侧新建能打开面板并产生一个临时行，面板内没有新建按钮；「查看全部」仍显示完整页的新建操作，往返后没有重新出现重复入口。最新截图为 `desktop-single-launch.jpg`；上方三张截图记录的是本次入口修正前的初次验收。

## 后续修正：菜单内嵌、左对齐及审核问题（2026-10-02）

本节记录最终布局，替代上文的独立 220px 历史栏及专用手机遮罩。按用户要求，将同一个分页历史列表移入左侧「历史会话」菜单下，由菜单控制展开／收起。没有新增第二份列表、查询缓存或通用布局框架。聊天主区域左边界由 428px 回到 208px；手机复用主导航抽屉。

四项审核问题均已修正：行操作收敛到占独立位置的更多菜单，保留 F2；普通重命名成功只更新该行及缓存；入口、完整页和顶部提示统一为「历史会话／歷史會話／Session History」；聊天内嵌列表也响应语言切换，编辑或拖动期间延后刷新。搜索结果和已有延后刷新仍会重查，不引入多页回填机制。

六个工作台菜单取消前五项的额外缩进，展开时图标、文字分别左对齐，历史项目及会话保留层级。桌面折叠时继续居中显示图标。

### 本轮自动化结果

- 158 项前端测试全部通过：上文九份历史／导航测试（148 项）加 `test_console_i18n_parity.cjs`、`test_console_i18n_coverage.cjs`（10 项）。新回归覆盖桌面折叠和手机主导航、后续页重命名保位，以及搜索／延后刷新时重新校验。
- `node --check`（console 及 history 展示适配）、脚本装载顺序、`git diff --check` 通过。
- 实际模板渲染检查：646 个 ID 无重复，49 个第一方 JS/CSS 资源有版本戳且存在、无重复；适配脚本在 console 后加载，没有装载第二份上游 sessions 实现。
- OpenSpec 严格校验通过。本轮只改前端及 change 文档；上文 66 项后端结果为初次验收记录，本轮未重复运行。

### 本轮浏览器结果

仍使用生产模板及静态资源、独立内存中的 63 条 fixture 会话，不修改实际业务数据库。

- 1280×900：历史的直接父节点为 `sidebar-recent`；主区域从 208px 开始；六个菜单图标左边均为 24px、文字左边均为 56px。折叠后导航宽 72px，六个图标左边均为 25.5px；顶部历史入口重新展开同一导航和列表。
- 更多按钮和标题布局不重叠；点击置顶会话标题切换会话且不改变置顶。原悬停置顶／重命名／删除覆盖层已删除。
- 加载 63 条后，通过 F2 重命名第 62 条；成功后仍有 63 条，滚动位置保持 2001.5px。
- 切换英文及繁体后，默认空间和更多操作文案立即更新；新建仍从左侧唯一聊天入口产生临时行。完整页搜索 `63` 命中唯一会话，再点历史菜单返回未筛选列表。
- 390×844：刷新不自动打开侧栏；顶部历史按钮打开主导航，历史在菜单下；选择会话后主导航收起，历史展开偏好保留。页面宽 390px，无横向溢出。
- 商务浅色及深色可读；1280×540 时主导航可继续滚动，底部账号可见；浏览器没有 console error。

最终截图：`embedded-history-desktop.jpg`、`embedded-history-mobile.jpg`、`embedded-history-dark-short.jpg`。之前的截图仅记录先前阶段。
