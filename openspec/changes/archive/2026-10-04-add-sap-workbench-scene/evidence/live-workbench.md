# SAP 工作台真实联调记录

> 最新结果（2026-10-03 晚间）：当前 admin 自有工作台已跑通自动参考 IMG、F4、日期往返、首行中文短文本/数量填写及真实 SAP 错误回读。旧的账号阻塞和 IMG 待复测描述为历史记录，当前状态以下方“当前账号完整不保存链路”为准。

日期：2026-10-03，Chrome，SAP S4H / Client 200 / 中文。本文区分真实现场验证与替身测试，不包含账号口令、访问令牌或 SAP 业务正文。

## 当前结论

已完成配置、真实 SAP 登录、双栏、原生 OpenCode 流式对话、MCP 只读调用、同一可见 SAP 页面的读取、交易跳转及真实日期字段填写/回读/恢复。**未保存任何业务单据。** 后续阶段的正式审批、业务提交、多用户生产验收、完整桌面双栏和远程部署尚未验收。本 change 不归档为全部完成。

用户最新要求优先本机跑通，MCP 可以配置在 OpenCode 项目，MCP 账号密码先由场景配置维护；统一登录、完整生产权限/审批不作为本轮继续联调的前置开发任务。已存在的绑定校验保留，业务提交工具未开放。

18:10 后曾发生 SAP 新连接失败：专属 Chrome 和普通 Chrome 均显示 `ERR_CONNECTION_CLOSED`，主机 HTTPS 探测出现 SSL connection timeout。18:38 左侧专属 Chrome 恢复到登录页，经工作台输入桥再次登录成功。18:45 完成下述真实字段验收。主机直连探测与 Chrome 网络路径的结果曾不同，未确定故障根因，不将其归咎于 SAP 服务器或证书。

## 实际通过的链路

| 项目 | 现场结果 |
| --- | --- |
| 场景入口 | `/chat` → 场景应用 → 数据 → SAP 工作台；卡片和配置按钮均可打开 |
| 配置与测试 | 保存 SAP URL/Client/语言、既有 coding 引用、项目目录、两个 MCP 的配置账号；密码不回显 |
| OpenCode | API 4096、Web 3000；每个绑定另建私有 canonical Session V2 host 与原生 Web 网关；右侧历史恢复 |
| MCP 项目配置 | 用户指定项目 `/Users/jiantan/ai_assistant/sapwork/opencode.json` 登记固定 8100/8200 端点；OpenCode MCP 状态均 connected；项目不放 SAP 密码 |
| MCP 实际调用 | 右侧对话调用 `sap_mcp_read`：sap-pyrfc healthcheck 与 sap-abap adt_discover 成功 |
| SAP 浏览器 | 独立 Google Chrome profile；用户亲自完成证书警告；左侧鼠标、逐字符键盘输入登录成功 |
| 页面读取 | 读取 SAP Easy Access，SMEN，SAPLSMTR_NAVIGATION/0101 |
| SPRO 跳转 | 对话实际调用 `sap_page_navigate` 后左侧到“定制：执行项目”；回读 SPRO / SAPLS_IMG_TOOL_5/0100 |
| 参考 IMG | 左侧人工点击 SAP 参考 IMG 后到“显示实施指南”；右侧独立读屏确认 SIMG / SAPLSHI01/0200，读取同一页面 |
| ME21N 跳转 | 对话实际导航并回读“创建采购订单”，ME21N / SAPLMEGUI/0014；识别采购凭证日期等可编辑字段 |
| 日期填写和恢复 | OpenCode 使用最新 revision 将字段 `0:4` 从 `2026-10-03` 改为 `2026-10-04`，独立读屏核对后恢复 `2026-10-03`，再次独立读屏确认；未点击保存/检查/暂存 |
| 原生流式回复 | 修复 session.next 事件兼容后，工具结果与最终回复直接出现，按钮从停止恢复发送；无需刷新 |
| 恢复 | 后端重启后左右两栏共同重接，恢复同一个 OpenCode 会话历史；新 host 配置生效，页面自动控制需重新开启 |
| 桌面启动 | 最新代码构建通过；用既有 `COW_DATA_DIR` 配置分离桌面与 Web 数据目录，9876 和 9899 同时监听；桌面到达登录页。未完成桌面账号登录后的 SAP 双栏验收 |

18:49 最终复测时 sap-pyrfc 首次调用失败，随后 sap-abap `adt_discover` 成功；检查连接初始化路径后，18:52 再次单独调用 sap-pyrfc `healthcheck` 返回 `status: connected`、`backend: adt`。首次失败的根因未确定，不宣称连接稳定性已充分验收。sap-pyrfc 返回 `pyrfc_installed: false`，当前使用 **ADT 回退**；原生 RFC SDK 未安装，RFC/BAPI 直连未验证。MCP 成功不能等价于完整 SAP 页面自动化验收。

18:55 刷新 Chrome 加载最新前端，经场景卡片恢复同一会话，左侧仍在参考 IMG，右侧历史完整。开启对话控制后点击左侧空白区域，状态立即更新为人工控制；验证了最新人工接管提示修复。18:56 右侧再次实际调用 `sap_page_read`，返回“显示实施指南”且 `login: false`；两项 MCP 与最终读屏的持久工具结果摘录见 [final-readonly-checks.json](final-readonly-checks.json)。

## 修复与可重复验证

- MCP worker 使用模块方式启动，避免 `backend/http.py` 遮蔽 Python 标准库。
- 外置 native_events 投影适配真实 Session V2 与原生 Web reducer，补全 execution 完成和工具结果事件。
- 模型中继载荷上限增至 512 KiB，避免已有 SAP/MCP 历史导致 HTTP 413；模型上下文仍有独立限制。
- 左侧按真实图像区域换算坐标，点击获得键盘焦点；CDP 退出唤醒等待中的画面流。
- 真实文本框承接输入法/粘贴并在转发后清空；独立 Chrome 输入桥页面已验证中文粘贴一次发送，本次真实 SAP 登录也经左侧整段文本输入完成。输入法组合生命周期有自动化测试，不能据此宣称所有 SAP 中文控件已验收。
- 导航等待命令消费和页面变化；填写连续两次回读核对值。返回 `business_validated: false`，不把页面值一致等价于 SAP 业务校验。
- 修复 16K 上下文与 OpenCode 默认 20K 预留不匹配造成的反复压缩；场景配置预留 4K、保留最近 8K。读屏结果把 revision/字段放在长页面文字之前。真实连续读屏、填写、恢复已通过；未修改 OpenCode 核心。
- 已知失败码保留为静态可操作提示，写入结果未确定时记录 unknown 并暂停，避免模型盲目重试。
- 运行 host 端口变化时同时恢复两个面板；首帧清除重连提示；自动恢复及人工重新打开均更新绑定。
- SAP 导航超时保留可见浏览器，不因慢响应无限重启；其他协议错误仍失败。
- 场景数对应新增 SAP 从 26 改为 27；未修改其他场景业务文件。

## 测试

- Python 场景、浏览器、MCP 登录、运行时、路由、场景 API、coding 页面资源：**141 passed**。
- Node SAP 前端、原场景分发、coding 前端：**64 passed**，含双栏共同恢复、中文组合输入/粘贴去重及本地输入清空。
- Bun canonical host：**2 pass / 44 assertions**，真实 HTTP/Session V2/取消、压缩配置迁移和未修改的 Web reducer，模型与 SAP 桥为替身。
- 桌面 `npm run build` 通过；OpenSpec strict 校验通过。两者不代替业务页面验收。
- 资源清单额外回归：4 passed / 236 subtests passed，6 个非 SAP 资源摘要失败；已用 HEAD 文件核对，同样不匹配，属既有基线问题。未覆盖修改无关资源。

## 后续控件与收尾更新

19:07 在真实 SPRO 页面调用新增 `sap_page_interact(reference_img)`，因实际按钮标签为“显示 SAP 参考 IMG (F5)”而被旧的严格标签匹配拒绝。已依据现场标签修复并增加回归；未误报点击成功。随后平台登录身份变化，尚未完成该修复的真实复测。树、页签、F4、检查及弹窗选项工具只接受当前快照目标和登记操作，不提供任意脚本或保存入口。

补充结构化 DOM 观察、服务端暂停/登录状态向界面推送、Chrome 启动容量预留、第二画面拒绝替换、断线复用及回收。配置页返回后重新显示新建/恢复按钮；修复窄屏对话底部被裁切，以及关闭 live 对话框后 CSS 可能残留显示的问题。

最新测试覆盖更新为：Python 相关完整套件 **161 passed**，此后新增配置变更/重启不改变旧绑定目标的测试也通过；Node **66 passed**；Bun **2 pass / 44 assertions**。Chrome 390×844、深色主题与关闭行为使用真实前端代码和模拟后端验证，证据为 [窄屏布局样例](layout-mobile-fixture.png) 与 [深色双栏样例](layout-dark-fixture.png)，不能替代真实 SAP 业务验收。测试样例不连接 SAP，也不写入真实场景配置。

Chrome 当前为 `admin / 默认租户`，租户选择器无原配置所在的 `AI租户1`；桌面端仍需平台登录。原租户、配置及历史保留，未覆盖到另一租户。需恢复原 `test15` 平台账号后才能继续原会话的真实控件和桌面验收。详细阶段结论与证据映射见 [验收矩阵](acceptance-matrix.md)。

真实 ME21N 字段验收的持久工具事件摘要见 [field-roundtrip.json](field-roundtrip.json)：导航 247、填写 257、独立读取 267、恢复 277、独立读取 287。只保留必要页面标题和测试日期，不记录凭据、会话令牌或业务数据。F4、行项目、SAP 业务错误校验、桌面双栏、SSO、多用户并发和提交均不能据本次结果宣称通过。

## 证据

本轮后续修复与最新回归见 [会话恢复与配置故障收尾](runtime-regression.md)。Chrome 已实际验证配置页 404 状态污染修复；没有新增 SAP 控件/业务提交验收。

- [工作台实际导航 SPRO](workbench-spro-live.png)：网络故障前截取，左侧目标与工具相同。
- [普通 Chrome 的参考 IMG](chrome-spro-img.png)：人工打开的参考页面，不能冒充工作台自动点击证据。
- [历史 SAP 网络故障](sap-network-failure.png)：连接恢复前，普通 Chrome 独立复现。
- [故障期间工作台与历史](workbench-current.png)：连接恢复前的错误页面，不能代表当前状态。
- [最终工作台参考 IMG](workbench-img-final.png)：刷新恢复后，左侧 SAP 参考 IMG 和右侧真实读屏回复。
- [真实字段填写与恢复](workbench-field-roundtrip.png)：18:45 验收完成后截取。
- [Chrome 中文输入桥验证](chrome-input-bridge.png)：独立测试页，不能冒充 SAP 控件验收。
- [桌面启动](desktop-started.png)：最新构建已到达账号登录入口。

## 当前账号完整不保存链路（20:40–21:15）

通过正常 UI 创建当前 `admin / 默认租户` 自有私有 coding `sap-workbench-test`，引用已获准的 `/Users/jiantan/ai_assistant/sapwork`。根据用户提供的 SAP 测试信息另存场景配置；原 test15 配置与历史保留，没有跨租户复制凭据或改动身份。用户亲自处理新专属 Chrome 的证书警告后，经工作台左侧登录 SAP 成功。

| 验收 | 真实结果 |
| --- | --- |
| MCP | 原生 OpenCode 对话实际调用 sap-abap `adt_discover`、sap-pyrfc `healthcheck`，均成功；后者依旧是 ADT 回退，非原生 RFC |
| 自动 IMG | 同一绑定中 SMEN → SPRO → `reference_img` → “显示实施指南”，独立回读 SIMG / SAPLSHI01/0200；实际按钮为“显示 SAP 参考 IMG (F5)” |
| F4 | ME21N 供应商字段 `help` 打开“限制值范围 (1)”，读取后 `dismiss` 关闭，无供应商选择 |
| 空单据检查 | 实际 `validate` 点击“检查 (Cmd Shift F3)”，独立回读 E“凭证不包含项”；`business_validated:false` |
| 日期 | 采购凭证日期 2026-10-03 → 2026-10-04 → 2026-10-03，各次独立读屏核对 |
| 首行填写 | 短文本=“SAP工作台测试（不保存）”、订单数量=“1”，均由对话工具填写，并用相同 query/row 独立回读确认 |
| 填写后检查 | SAP 已识别项目 10，返回“PO抬头数据仍有错”“请输入采购组织”“请输入物料编号或科目分配类别”等错误，以及交货期限警告；检查结果独立回读一致，不能视为可保存单据 |
| 重启恢复 | 加载两次场景修复后恢复相同 OpenCode 会话历史；左侧从 SAP 首页重新进入，自动控制需显式重新启用 |

实际运行时发现并修复两处场景适配缺口：SAP CBS 表格占位控件需真实点击后才生成 input；长字段列表经模型上下文压缩后丢失后部列。因此新增稳定行/列身份、同一单元格激活与回读，以及 `sap_page_read(query,row)` 定向读取和精简工具投影。完整快照继续参与 revision 检查。第一次行字段未暴露、第二次长结果被截断均如实停止，修复后才获得成功证据；没有改 OpenCode 核心。

证据：[实际自动 IMG](workbench-img-current.png)、[空单据错误](workbench-check-current.png)、[行项目文字和数量](workbench-grid-current.png)、[填写后业务错误](workbench-grid-check-current.png)、[脱敏工具事件](current-account-tools.json)。后者记录真实成功/失败事件、测试值及错误，不保存密码、令牌或原始业务页面转储。

本轮最终代码回归：**415 Python passed / 1 既有弃用告警，76 Node passed，Bun host 2 pass / 45 断言**。此前双 host 部署隔离 1 pass / 18 断言未冒充本次重跑。OpenSpec strict 与 Git 空白检查通过，OpenCode 核心工作区无改动。

本机 Web 的不保存链路已通过。桌面端独立后端授权页面仍显示“登录信息有误，请重试”，已保留正常登录入口等待用户完成平台账号登录；登录后的桌面双栏不宣称验收。完整可保存单据、表格分页、SSO、双真实 SAP 账号并发、生产权限/配额、远程部署及 G3 仍按原清单保留为未完成，提交能力保持关闭。

### 最终页面恢复（21:14–21:22）

21:14 清理前读取返回 `login:true / control:manual`，执行器未继续操作。随后通过左侧正常登录入口重新登录，再启用对话操作；21:21 实际完成 SMEN → SPRO → `reference_img` → 独立读取“显示实施指南”，最后 revision 为 `19:acff166454894aa92fa89484d95a927457da906fff66d745bbd85c0800135448`。交付前切回人工控制，页面见 [最终可见工作台](workbench-ready-current.png)。

未执行保存、暂存、提交或删除；也没有恢复或重新填写测试订单。返回登录的根因与旧草稿的持久状态未经核验，不能据此声称系统已丢弃草稿或没有残留。右侧曾作出该推断，已要求更正；验收以脱敏工具事件和可见页面为准。
