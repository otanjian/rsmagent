# macOS SAP 当前页面读取

实现对应 OpenSpec change `add-desktop-sap-page-reading`。首版只支持 macOS 桌面的原生 SAP iframe；普通 Web、Windows 和旧桌面不会自动获得读取能力。

## 使用与开关

服务端场景配置新增 `desktop_sap_page_read_enabled`，默认 `false`。管理者可在 SAP 场景连接设置中看到“macOS 当前页面读取”。服务端、项目插件和 macOS 桌面都需包含本次更新。2026-10-06 已在当前测试租户启用，macOS + 真实 SAP + OpenCode 页面读取联调通过；其他配置默认仍关闭。

在测试租户临时开启开关并重新进入 SAP 工作台进行验收，使新绑定使用新的配置版本；真实环境验收通过后才保持开启。确认 OpenCode 已加载更新后的项目插件、当前工具列表包含 `sap_page_read`。在同一个 OpenCode 会话中要求“读取左侧当前页面”，模型调用这个无参数工具，无需额外绑定操作。普通获授权成员可读取自己的当前工作台。OpenCode 新建/切换会话仍走平台原有 attach 核验，核验后的链接同步到现有场景绑定；旧读取失效。重新打开历史会话会通过同一 attach 更新工具权限，不创建聊天副本。

关闭开关会使旧配置版本的绑定拒绝后续操作；重新进入后保持原有 SAP 展示、人工操作和后台查询。桌面或服务端重启后需要重新打开工作台，不恢复未完成的读取。

## 数据路径与边界

`sap_page_read` → 既有服务端场景桥 `/api/scenes/sap-workbench/bridge/read` → owner/runtime dispatch → heartbeat → 当前 macOS 宿主 → 固定 DOM 脚本 → 结果回执 → 原始工具调用。

插件携带 OpenCode 的受信 `context.sessionID`；模型不能指定窗口、URL、selector、脚本或身份。服务桥要求同机 OpenCode 服务认证，再按会话解析归属。宿主只接受现有受信主 frame 的 IPC，读取前后通过登录会话向服务端核对 pending；目标必须是该宿主内当前绑定的 SAP 直接子 frame，嵌套文档只允许场景配置的源。

工具复用 dispatch 的鉴权、动作账本、配额与审计。单绑定只允许一个 pending；多个活动视图明确报冲突。返回首页、关闭、会话切换、导航、撤权和过期后的回执不会投到其他会话。退出页面会注销视图；异常退出的心跳记录 5 秒后失效。

结果标记 `source=sap_page_dom`、`scope=rendered_dom`，包括采集时间、标题、字段当前值、表头/行列、选择、活动页签、消息和文本。页面文本属于不受信观察，不能作为模型指令。字段值可能尚未保存，不能据此认定业务已经提交。

脚本不写 DOM、不派发事件、不切换焦点、不滚动；不读取 cookie、存储或页面脚本变量。密码、已识别的认证字段及隐藏内容被排除；检测到登录表单则返回 `login_required`。这不是通用脱敏器，普通可见业务字段仍属于读取范围。

没有新增数据库表、截图、缓存或独立聊天存储。场景进程只暂存当前请求/结果，完成后释放；账本及审计不写正文。交给 OpenCode 的工具结果仍按原有会话历史策略保存，这是原有聊天历史的一部分。

## 上限与已知限制

| 项目 | 上限/行为 |
|---|---|
| 单次读取 | 服务端 15 秒，宿主脚本等待 5 秒 |
| SAP 文档 | 最多 8 个允许源内文档，各自采集，不是原子快照 |
| 遍历 | 每文档 12,000 个元素；文本回退最多 16,000 个文本节点 |
| 字段、表格 | 每文档 200 个字段、5 张表；每表 100 行、40 列 |
| 文本 | 单值 1,000 字符、回退文本 12,000 字符 |
| 总结果 | 128 KiB；截断写入 limitations，过大则拒绝 |

读取的是已渲染 DOM，包括已经渲染但位于滚动区域之外的内容；不保证与屏幕可见区域完全相同。虚拟表格未加载行、隐藏页签、自绘控件、未允许源的子文档及超限内容不会自动补齐。表格明确 `complete=false`。不进行 OCR、截图、滚动加载、稳定页面等待或后台数据替代。

## 已验证及待验证（2026-10-06）

本机 macOS / Electron 33.4.11 的独立跨域 iframe 测试通过：宿主能定位正确 SAP frame、读取未保存输入（测试值 2 改为 7），排除其他 pane，采集前后 DOM、焦点和滚动值相同。固定提取脚本的 Chrome DOM 测试覆盖 SAP ARIA grid 内嵌布局 table、表头/行项目、交货页签、隐藏/认证内容过滤、限长和无事件副作用。

服务端测试覆盖普通成员 HTTP 成功、其他用户/租户拒绝、服务认证/未绑定会话拒绝、会话切换取消、配置关闭、超时、迟到回执、多视图和审计不含正文。前端测试覆盖旧服务端心跳兼容、重复心跳不重复提取、导航丢弃结果、返回首页/关闭后不回传。

验证结果：SAP 与 coding routes Python 回归 1,499 通过、3 跳过（两条既有弃用警告）；历史会话权限刷新附加测试 2 通过。前端、项目插件、宿主边界和 DOM 测试合计 125 通过，其中最终前端回归 71 通过。`npm run build`、最终宿主编译、Electron fixture 和 OpenSpec 严格校验通过。

2026-10-06 后续联调已重启 macOS 桌面，并修正开关关闭被误报为 Windows 的问题：服务端现在返回独立的 `page_read_disabled`，插件区分远程服务端系统与桌面客户端能力。测试租户配置版本由 1 更新为 2 并开启读取。真实 `sap_page_read` 已读取 SMEN 首页和 ME23N 订单 4500000127、供应商 EWM17-SU01、两条 EWMS4-01 行项目（各 2 CAR）、交货计划页签。OpenSpec 1.1 完成；3.3 的真实未保存输入仍待验证，Windows 桌面未验证。

订单号、供应商、行项目和交货计划已对照真实画面。尚需在可丢弃测试单据验证未保存输入，然后恢复输入且不提交业务；目前该项只有独立 fixture 证据。

## 远程部署记录（2026-10-06）

已按用户授权更新 `rdai` 的 `C:\rdai\rsmagent`，定向替换 15 个页面读取相关文件；部署前逐文件核对原始摘要，保留已有 SSO、模型偏好和服务器配置。没有执行全仓库覆盖或 Git 拉取。

实际 OpenCode 项目是 `C:\rdai\rsmCode\sapwork`。通过场景自带 `project_toolkit.install` 升级插件、SKILL 和边界说明，并补充项目默认拒绝 `sap_page_read` 的规则；场景会话重新打开时经原有权限配置放行普通获授权成员。

备份在远程 `C:\rdai\backups\sap-page-read-20261006-164134`，包含平台及项目文件原件、部署前后摘要和验证记录。回退应按其中 `manifest.json` 恢复原文件，原先不存在的新增文件也由清单标识，再重启对应服务；无需恢复或替换聊天数据库。

已重启平台进程与 OpenCode API；OpenCode 项目 dispose 后仍缓存旧插件，本次通过 API 进程重启使新插件生效。沿用现有启动配置与用户数据目录，Web 前端服务无需重启。一次性启动任务已删除。

验证：公网 `/chat` 与 `/code/` 均返回 200；公开 SAP 前端文件与本地字节一致；新读取桥无凭据返回 `401 bridge_unauthorized`，受信服务调用未绑定会话返回 `403 session_not_bound`；OpenCode 健康检查通过，实际工具注册列表包含 `sap_transaction_open`、`sap_data_call`、`sap_page_read`。更新前后项目 28 个会话的 ID 集合一致。初次部署没有更改模型设置、业务数据或场景读取开关；后续仅按修复请求开启测试租户读取。

修正备份为 `C:\rdai\backups\sap-page-read-20261006-165323`，包含启用前场景数据库副本。桌面与远程插件均已更新，并完成上述真实页面读取验证。初次部署的结构化证据见 `openspec/changes/add-desktop-sap-page-reading/evidence/remote-deployment.json`。

## 本地验证命令

```sh
cd desktop
npm run build
cd ..
NODE_PATH=$(npm root -g) node --test tests/test_desktop_sap_page_read.cjs tests/test_desktop_remote_host.cjs tests/test_sap_workbench_frontend.cjs tests/test_sap_workbench_project_plugin.cjs
desktop/node_modules/.bin/electron tests/fixtures/desktop-sap-page-read.cjs
.venv/bin/python -m pytest tests/test_sap_workbench*.py tests/test_coding_session_routes.py -q
openspec validate add-desktop-sap-page-reading --strict
```

DOM 测试使用本机已安装的 Playwright 和 Chrome。Electron 测试只打开独立的隐藏 fixture 窗口，不使用用户 SAP 会话。

## 当前登录用户字段

`sap_page_read` 新增 `currentUser`。能确认时返回 `account`、可空的 `client` / `systemId`、`source: "sap_session_ui"` 和 `sourceDocument`（1–8）。未显示或冲突时为 null；旧桌面可能没有该字段，同样按未知处理。它是采集时刻的界面信息，不是服务端认证证明。

识别当前可见的会话状态栏（statusbar / SAP sbar），以及明确标为「系统状态」「系统信息」「System: Status」「System Information」的区域；支持完整的「用户: 值」状态项、只读的带标签字段、两列状态表和定义列表。SAP WebGUI 的只读字段也可通过明确的「ABAP 系统字段：当前用户的名称/客户端标识」提示识别。「系统：状态」只采用使用数据区域或这些明确提示，排除其中的数据库用户。不会把订单创建人、SU01 用户字段、可编辑输入或已记住账号当作当前身份。身份信息未显示时可由用户打开系统状态再读取；提取器不会自动点击菜单。自绘信息或未覆盖的 SAP 主题仍返回未知。

本次账号字段回归：Python 62 通过、1 跳过；宿主边界、DOM 与项目插件 JavaScript 60 通过；macOS Electron 跨域读取 fixture 通过。完整桌面构建与 OpenSpec 严格校验通过。联调同时修正宿主文档代次重复递增：只在主文档导航时更新，资源加载完成不再使已建立的桥失效；测试覆盖加载完成、同页导航和真正文档替换。

真实联调已通过：重启 macOS 桌面后，已记住的 SAP Cookie 恢复；在 SAP「系统：状态」中，助手调用 `sap_page_read` 返回 `currentUser.account=S2385`、`client=200`、`systemId=null`、`source=sap_session_ui`，与使用数据区一致，未误用数据库用户。系统标识缺少支持的明确字段时保留 null。远程平台 4 个文件及实际项目插件/skill 已更新；平台与 OpenCode API 已重启，公网入口及工具注册正常。备份：`C:\rdai\backups\sap-current-user-20261006-180453`。
