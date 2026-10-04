# 页签与表格只读观察合同

日期：2026-10-04。对应 task 5.3 的可独立开发部分；源码已冻结。本文件记录观察能力、来源及隔离测试，**不代表页签执行、完整分页或 task 5.3 已验收完成**。

## 官方依据与适用边界

- [SAP：Tabstrip Controls](https://help.sap.com/doc/saphelp_nw74/7.4.16/en-US/4a/44b861954c0453e10000000a421937/content.htm)：原生页签存在 SAP GUI 本地分页和应用服务器分页两种模式；本地模式的函数类型为 P，选择页签不触发 PAI；服务器模式选择页签会触发 PAI 并设置 OK_CODE。因此，`role=tab`、`aria-selected` 或中文标签不能证明动作仅改变显示。
- [SAP：Table Controls in ABAP Programs](https://help.sap.com/doc/saphelp_nw74/7.4.16/en-US/4a/44b62c954c0453e10000000a421937/content.htm)：`TOP_LINE`、`LINES` 与 `sy-loopc` 分别涉及页窗位置、总行数和显示行数；滚动条可触发空函数码 PAI。文档还说明隐式初始化的 LINES 可能不正确，因此不能把像素高度或当前行窗口推导为完整业务数量。
- [SAP：IRPA SDK V2 — SAPWebGui](https://help.sap.com/doc/9297ac6cb5be425383f61f7e76cbc7c4/Cloud/en-US/modules/SAPWebGui.html)：定义 TABSTRIP、TABSTRIPPAGE、TAB_BUTTON 和 SAPGUI_GridViewCtrl_1。`scrollToNextPage` 使用服务器滚动，步长为 VertScrollExtent；`scrollVertically(position)` 使用从 0 开始的位置，而 `getCellValue` 的行参数从 1 开始。这是 SAP 自动化 Agent 的活动合同，不能直接冒充当前浏览器 DOM 适配器的已接受接口。
- [W3C：WAI-ARIA，tab](https://www.w3.org/TR/wai-aria/#tab)、[tabpanel](https://www.w3.org/TR/wai-aria/#tabpanel)、[aria-rowcount](https://www.w3.org/TR/wai-aria/#aria-rowcount)、[aria-colcount](https://www.w3.org/TR/wai-aria/#aria-colcount)、[aria-rowindex](https://www.w3.org/TR/wai-aria/#aria-rowindex)、[aria-colindex](https://www.w3.org/TR/wai-aria/#aria-colindex)：定义页签与面板的 ID 引用关系，以及行列总数和索引声明；总数为 -1 表示未知，ARIA 行列索引从 1 开始。这里读取的是页面声明，不是 SAP 业务正确性证明。

SAP 文档用于说明原生控件存在不同效果与索引合同；它们没有确认本测试系统 NetWeaver 758 当前渲染器的具体事件、函数代码或 HTML 属性。历史截图可见 ME21N 表格和供应商搜索弹窗，但保存的摘要没有完整原生页签/分页 DOM。相邻 `rsmcode/sap-connect` 源码提供 ADT/RFC 工具，没有可复用的 WebGUI 页签/分页控制协议。本轮没有取得或虚构新的现场控件证据。

## 实现的观察内容

`SNAPSHOT` 为 `role=tab` 控件增加 `tab` 元数据：唯一 `native_id`、最近 tablist 的唯一 `tablist_id`、唯一面板的 `panel_id`、面板可见性及关联来源。`aria-controls` 与 `aria-labelledby` 引用只在所属 document 内解析，区分双向、单向和未解析关系；重复 ID、冲突引用、多个受控目标、非面板目标和过长引用不能形成已解析关联。身份不静默截断为另一个合法 ID，面板可见性包含所属 frame 链。

表格增加 `structure` 元数据：ARIA 声明的行列总数、declared/unknown/unavailable/invalid 状态，以及可见窗口的索引来源。接受的总数范围为 0–1,000,000；-1 保留为未知；不从当前可见行数填补总数。来源均为有效 ARIA 索引时记录基数 1；SAP `lsmatrix*`、混合、非法或缺失来源的基数保持 null。声明的表格总数不能当作采购订单业务项目总数。

`model_observation` 对新增元数据再次作类型、长度、状态和来源约束，不透传任意附加字段。页签的 `paging_mode` 固定为 unknown、`automatic` 固定为 false；表格的 `complete` 与 `pagination_supported` 固定为 false。工具说明明确这些元数据不开放页签执行或完整分页。

完整 SNAPSHOT 的 `truncated` 经 `PageController.read` 保留。模型投影继续将该标记与字段/控件/表格的投影上限合并为 `omitted`，专项测试显式验证该路径。既有单表最多 20 行、单元格最多 160 字符等局部截取不等于完整业务观察；本补丁没有将这些有限窗口提升为完整提交数据。

## 涉及的五个源码与测试文件

1. `Scene/sap_workbench/browser_service/dom.py`：仅修改固定 SNAPSHOT 的只读元数据。
2. `Scene/sap_workbench/browser_service/page.py`：新增元数据投影约束并接入 `model_observation`。
3. `Scene/sap_workbench/opencode_adapter/tools.ts`：更新 `sap_page_read` 说明。
4. `tests/test_sap_workbench_observations.py`：新增 16 项 Python 隔离测试。
5. `tests/test_sap_workbench_observations.cjs`：新增 8 项固定 DOM 观察测试。

本轮未修改 F4/effects 规则、通用核心、普通智能体、其他场景或 `rsmcode` 源码。后续根代理维护全套结果，本文件数量仅属于这一轮定向验证，不与全套重复相加。

## 实际验证结果

```text
.venv/bin/python -m pytest -q \
  tests/test_sap_workbench_observations.py \
  tests/test_sap_workbench_controls.py \
  tests/test_sap_workbench_effects.py \
  tests/test_sap_workbench_scroll.py \
  tests/test_sap_workbench_f4_compatibility.py

186 passed in 0.26s
```

```text
node --test --test-reporter=spec \
  tests/test_sap_workbench_observations.cjs \
  tests/test_sap_workbench_dom.cjs

78 tests, 78 passed, 0 failed
```

Python AST 解析与上述五个文件的尾空白检查通过。测试覆盖唯一/冲突/重复引用、隐藏面板/frame、属性界限、未知/非法计数、混合索引来源、投影类型防护、截断标记传递和执行关闭。测试运行在可控 Python/Node fixture 中，没有启动或操作真实 Chrome、SAP、OpenCode、MCP 或模型，没有加载真实配置、启停服务或保存业务。

## 5.3 尚未完成的明确合同

页签执行仍需把具体交易/程序/屏幕、原生控件身份及函数效果绑定到一个已接受基线，确认本地或后台分页模式，并定义唯一 selected tab 与实际面板的稳定回读。完整分页还需原生页窗位置、步长、总数或 has-more 的可信来源、索引基数、末页条件和数据窗口一致性；固定像素滚动不能替代这些信息。

以后可以在现有 `interact(tab)` 合同中增加已接受的封闭控件映射，并用受约束的分页方向和预期回读扩展表格协议。当前不会提供任意 click、Enter、函数代码、selector、页码或后台 custom action。实际 SAP/OpenCode 操作检查按用户要求继续暂停，task 5.3 保持未完成。
