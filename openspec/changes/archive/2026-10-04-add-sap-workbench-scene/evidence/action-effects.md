# 首批自动动作效果登记

记录日期：2026-10-03。按用户当前安排，本轮继续独立实现和隔离测试，没有新增真实 SAP、OpenCode、模型或 MCP 操作，也没有执行保存、过账、暂存、保留或删除。提交能力继续关闭。本记录描述首批页面适配边界，不作为任务 5.7 或 6.2–6.6 的真实验收证据。

## 登记范围

[effects.py](../../../../../Scene/sap_workbench/browser_service/effects.py) 以只读登记表定义操作、效果和风险。导航目标缩小为 `SPRO`、`ME21N`，不再开放未现场验证的 `VA01`、`FB50`。兼容页面形状包括登记的中文/英文 SAP Easy Access、SPRO 入口、参考 IMG 和 ME21N 创建采购订单标题；未知或定制页面拒绝自动操作。标题仅用于匹配页面适配范围，不能替代绑定身份、权限或 SAP 业务效果的权威证明。

| 操作 | 登记效果 / 风险 | 自动操作范围 |
| --- | --- | --- |
| `read` | `observation` / `read_only` | 保留既有来源检查、登录脱敏和有界观察 |
| `navigate` | `replace_screen` / `may_discard_draft` | 当前登记页面、无模态、无已修改/不确定输入，目标仅 SPRO 或 ME21N |
| `fill` | `edit_unsaved_field` / `draft_change` | ME21N 创建页面、无模态、当前观察中的可编辑非命令字段；保留类型与值校验 |
| `reference_img` | `display_reference_img` / `read_only_navigation` | SPRO 入口及登记的参考 IMG 按钮 |
| `expand` / `collapse` | `expand_img_node` / `collapse_img_node`，`display_change` | 已登记 IMG 页面、SAP 用户化实施指南根标记及带展开状态的树节点；使用左右方向键 |
| `help` | `open_value_help` / `read_only_query` | ME21N 创建页面的当前可编辑非命令字段；派发固定 F4 前核对完整焦点链，结果须符合登记的帮助弹窗类型 |
| `choose` | `select_help_value` / `draft_change` | 属于本 controller 打开的唯一值帮助弹窗，选项须具有显式字符串 `value`，并回读原字段接受值及弹窗关闭 |
| `dismiss` | `close_owned_value_help` / `display_change` | 仅关闭当前已归属值帮助弹窗，使用固定 Escape |
| `validate` | `check_purchase_order` / `validation_only` | ME21N 创建页面、无模态及登记的 Check 按钮 |
| `scroll` | `scroll_purchase_order_grid` / `display_change` | ME21N 当前观察的可见 grid、固定四方向步长和表格具体回读，见 [表格滚动](table-scroll.md) |
| `tab` | 尚未登记 | 当前拒绝自动操作，不能凭 `role=tab` 放行 |

`page.py` 在导航、填写和封闭交互的结果中返回服务端生成的 `action_effect`，包含 operation、effect、risk、profile、可选 transaction 和 `submits: false`。这些字段表达本地适配策略，不代表 SAP 后端确认未发生持久化，也不能由模型提供来扩大执行权限。填写和交互仍返回 `business_validated: false`，不将页面值或页面变化等同于通过 SAP 业务校验。

## 导航 Enter 的派发前核对

[page.py](../../../../../Scene/sap_workbench/browser_service/page.py) 的导航现在要求当前 `revision`。目标、页面形状、模态及 dirty 状态核验通过后，只写入固定 `/nSPRO` 或 `/nME21N`。

发送 Enter 前再次核验控制代次，并调用 [dom.py](../../../../../Scene/sap_workbench/browser_service/dom.py) 的 `COMMAND_READY` 检查：

- 目标仍是同一观察 ID 对应的可见、可编辑命令输入框，当前值完整一致。
- 页面不存在可见模态或密码输入框。
- 命令输入框仍是其文档的 activeElement，嵌套 frame 的完整焦点链也仍有效。
- DOM 检查返回后控制代次仍与请求一致。

缺少或过期 revision、人工接管、事件处理造成焦点变化、目标变化或未知模态均在发送 Enter 前拒绝。模型没有任意 Enter 工具，不能把导航所需 Enter 转为其他控件的默认动作。

F4 和 IMG 树方向键派发前另使用 `KEY_TARGET_READY`：目标须仍可见、可操作、保持自身文档焦点及完整 frame 焦点链；F4 不能落到命令字段、密码或只读字段，树目标标签也须与本次观察一致。可见模态、登录输入或 focus 处理改变焦点时不发送按键。按钮和选项定位同时复核聚焦前后的标签，避免将已变化的控件当作原目标。

## 已修改和不确定输入防离页

自动填写在派发输入前设置 `draft_changed`，值帮助选值同样设置该标记。暂停或人工接管使控制代次失效，并将输入状态视为已修改/不确定。该状态下自动导航返回 `draft_navigation_forbidden`，交由用户核对当前草稿后处理，不能用 `/n` 跳转静默丢弃输入。

标记属于当前 controller 的内存状态；重新观察登记的只读入口/IMG 页面且没有弹窗时可清除。它不是 SAP 未保存状态的完整识别或持久证明，不能据此声称所有人工修改、浏览器恢复或自定义事务均已覆盖。恢复后仍需沿用既有人工核对及不重放旧动作的规则。

## 值帮助弹窗归属

F4 只在 ME21N 无模态的登记页面中派发。`value_help_dialog` 要求结果仍是 ME21N 创建页面、恰有一个弹窗且标题完整匹配登记的帮助类型：`值帮助`、`值幫助`、`输入帮助`、`輸入幫助`、`Value Help`、`Input Help`、`Possible Entries`，可附冒号和有界字段说明。弹窗正文不得包含登记的保存、过账、暂存、保留等持久化文案。不是任意 F4 后出现的 dialog 都能建立归属。

只有上述结果连续两次稳定回读后，controller 才保存其 label/text、来源字段和控制代次。`choose`、`dismiss` 要求当前页面恰好包含同一个已归属弹窗；派发前 `VALUE_HELP_READY` 还检查弹窗内容、唯一性、弹窗内焦点及完整 frame 焦点链。选项须从观察中提供显式字符串 `option.value`，不能根据显示标签猜测业务代码；缺少该属性的条目留待人工处理。

来源不明、内容变化、额外弹窗、控制代次变化或焦点离开弹窗均拒绝自动 Escape/选值。暂停、弹窗消失或完成关闭/选值后清除归属，不沿用旧记录处理新弹窗。未归属的保存确认或其他业务弹窗交人工处理，不再因“存在任意 dialog”而允许关闭。当前归属是客户端 F4 后的观察关系，不是 SAP 对弹窗业务效果的权威声明。

更严格的帮助标题和 `option.value` 要求尚未在真实 SAP 重测。实际帮助窗口的定制标题、页签、条目格式或无显式 value 的控件不自动扩大登记范围，须人工操作或后续单独适配、验收。

## 操作特定的回读

交互不再仅因任意 revision 变化而报告目标效果。除已处于目标展开状态的 `already_set` 分支外，均要求观察到变化及连续两次一致的目标结果：

- `help`：符合登记帮助类型的唯一弹窗。
- `dismiss`：页面已没有 dialogs。
- `choose`：dialogs 已关闭，原帮助来源字段仍存在且精确等于选项的 `value`；读取时清除弹窗归属不会丢失本次已绑定来源字段。
- `expand` / `collapse`：仍是 IMG 页面，同一观察 ID、标签的节点 expanded 为所要求的状态。
- `reference_img`：落到登记的参考 IMG 页面 profile。
- `validate`：保持 ME21N 创建页面并返回当前观察及消息，仍不宣称业务校验通过。

导航回读也要求已登记的目标页面 profile，不能将命令消失但进入其他页面当作 SPRO/ME21N 导航成功。不相符的稳定通知、无关页面变化或仍未关闭的弹窗返回未观察到目标效果，不能自动重复未知动作。

## Check、树与提交边界

Check 仅接受裸 `检查`、`檢查`、`Check`，以及明确登记的 `Ctrl+F3`、`Ctrl+Shift+F3`、`Cmd Shift F3` 显示后缀，不接受任意括号文字。`Check (Park)`、`检查 (暂存)`、`Check (Ctrl+S)` 等均拒绝。显示后缀用于识别同一登记按钮，不开放通用快捷键执行。

IMG 展开/折叠要求参考 IMG 页面及 SAP 用户化实施指南根标记；没有登记 IMG 活动叶子的 Enter、双击或通用点击。页签没有已接受的身份及效果基线，因此自动 `tab` 关闭；工具 schema 保留旧枚举用于明确拒绝，不表示此操作可用。

[tools.ts](../../../../../Scene/sap_workbench/opencode_adapter/tools.ts) 更新了导航 revision、页面范围和拒绝原因；保持封闭操作枚举。后端拒绝 `save`、`post`、`submit`、任意 `key`、`script`、`raw_cdp` 等动作，没有原始点击/按键/脚本工具。登记控件的持久化禁止词同时覆盖保存、过账、删除、暂存、保留及对应英文效果。

人工输入仍是独立的用户控制通道，接管会撤销自动代次；该登记表不替代用户在 SAP 中已有的业务权限，也不把人工输入包装为自动动作获批。自动工具不能通过自称人工或修改 effect 参数获得额外操作。

## 验证与限制

本轮 `test_sap_workbench_effects.py`、`test_sap_workbench_controls.py`、`test_sap_workbench_runtime.py` 独立执行 **99 项通过**；`test_sap_workbench_dom.cjs` 的 Node DOM/guard 测试 **8 项通过**。使用 mock、临时状态及固定 DOM 片段验证：

- 未登记交易、未知效果/快捷键说明、错误页面上的 tree/tab/popup 拒绝。
- 导航缺少/过期 revision、命令写入期间接管、焦点检查期间接管、焦点变化及 dirty 状态不发送 Enter。
- 成功导航只派发一组 Enter，并返回明确效果；填写前 dirty 标记已设置。
- save/post/key/script/raw_cdp/submit 无原始工具通道。
- 未归属、内容变化、额外、失焦或旧代次弹窗拒绝 Escape；成功 F4 才建立归属，暂停清除归属。
- F4/树按键之前目标失焦时不派发；帮助类型不符或含持久化文案时不建立归属。
- 无关的稳定变化不能代替关闭、选值、树展开或 IMG 导航结果；选值核对来源字段的精确值。

本轮没有重做真实浏览器、SAP 或 OpenCode 操作检查；不把以上隔离验证记录为任务 5.7、业务审批 6.2、回执/不确定结果 6.3–6.4、完整审批边界 6.5 或真实提交 6.6 完成。未知帮助、条目及页签保持人工处理，隔离测试通过不证明其真实 SAP 兼容性。

这组限制针对首批登记的页面形状和封闭输入路径。自定义 SAP 事件可能赋予字段、F4 或标签不同的业务效果，当前实现没有权威的后端事务效果证明，也不能保证阻止一切自定义持久化；定制交易和未知页面仍须单独适配及验收。正式生产审批、真实提交和 G3 验收保持此前延后安排，提交能力继续关闭。

后续独立补充：表格受限滚动及具体读回已实现，另有 69 项 Python / 9 项新增 DOM 验证，见 [表格滚动](table-scroll.md)。桥输入、控制锁内及派发前的绑定复验、提前失败终态与模型预留处理见 [桥边界](bridge-boundary.md)。这些数量与最终全套重叠，不作为新的现场验收。
