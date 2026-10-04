# 采购订单只读投影与字段拒绝状态收尾

2026-10-04 本轮继续独立实现及隔离测试，完整计划保持 **42/57，剩余 15 项**。新增 `sap_purchase_order_read` 仅查询已有单据，不保存、过账或删除。用户暂停的 SAP/OpenCode 实际操作检查继续暂缓；不扩展权限或统一登录，不注册提交适配器，不归档 change。上一轮 **895 Python pass / 2 skip、161 Node pass、3 Bun pass / 68 assertions** 及原服务快照保留在 [上一轮合同收尾](next-contract-closeout.md)，不能当作本轮新增源码的现场验证。

## 封闭读取链路

- 模型入口只接受 `document_number`，要求非全零的十位数字字符串。Client 来自场景配置，使用当前绑定所属的 MCP consumer；不接受 SQL、expected、用户、连接 ID、密码或身份证明参数。
- 私有 `ConfiguredMcp.read_json` 仅允许 `sap-pyrfc` 的 EKKO/EKPO/EKET 固定订单条件读取及固定 `COUNT(*)`。三表各执行 count/read/count，再重复一轮，共 18 次调用。表、字段和过滤条件由场景代码构造，不开放模型任意 SQL。
- SDK 与 worker pipe 保留完整且有界的 JSON：数据最多 **48,000 UTF-8 字节**，私有 `{"data":...}\n` envelope 另有 10 字节，最大深度 16、节点数 10,000。错误结果、重复键、非有限数、SDK 两份表示不一致、截断或敏感输出均拒绝；不截取业务正文或用脱敏替换值继续比较。
- 单次读取最多 90 秒，使用既有 worker 正常关闭 12 秒及强制结束等待 2 秒的清理机制；OpenCode 工具超时为 110 秒。组装后的模型结果超过 **32,000 UTF-8 字节** 则拒绝，不能返回截断订单。
- 每表最多 500 行，count 与行数、精确列、主键和外键必须一致，所有项目均须有计划行且计划数量合计一致。两轮规范化投影与计数必须相等；依次读取不是 SAP 事务快照，不能排除读取间的并发变化。

源码为 [JSON 边界](../../../../../Scene/sap_workbench/backend/mcp_data.py)、[MCP consumer](../../../../../Scene/sap_workbench/backend/mcp_runtime.py)、[worker](../../../../../Scene/sap_workbench/backend/mcp_worker.py)、[订单 reader](../../../../../Scene/sap_workbench/backend/purchase_order.py)、[受信桥](../../../../../Scene/sap_workbench/backend/runtime.py) 与 [OpenCode 工具](../../../../../Scene/sap_workbench/opencode_adapter/tools.ts)。

## 首批形状及核验限制

支持 `BSTYP=F`、`BSART=NB`、`PSTYP=0` 的普通库存物料投影。删除、暂存/保留、退货、STO、科目分配、服务包、免费、合同引用、特殊库存、确认控制、供应商拒绝/交货阻塞、统计及父子项目形状均拒绝。订单单位与计价单位必须相同，这两个单位的换算比限定 1:1；数量和金额以 Decimal 及固定精度处理，日期和单据/项目/计划行键严格解析。首批只允许已列出的常规两位币种，这项限制不证明实际 TCURX 配置。

返回 `scope=standard_material_purchase_order_v1`，并始终标明 `business_validated=false`、`complete_business_document=false`、`submission_authority=false`。税码只作为投影字段比较，没有核验全部税额、价格条件、伙伴、完整付款/交货条件、GUI expected 或实际 SAP 身份。官方 [采购订单 CDS 字段映射](https://help.sap.com/docs/SAP_S4HANA_ON-PREMISE/af9ef57f504840d2b81be8667206d485/1d6f6bea1c3b4f049742e15a81ff86a0.html) 将条件和伙伴分别映射到 PRCD_ELEMENTS、EKPA；三表不能据此称为完整业务单据。EKKO MEMORY/MEMORYTYPE 的拒绝依据见官方 [Park & Hold](https://help.sap.com/docs/SUPPORT_CONTENT/spmm/3362167263.html)。

`.compare(expected)` 是私有代码接口，只比较完整支持投影，缺少或多余字段均拒绝；模型及 HTTP 入口不开放 expected。它不返回 CommitConsumer 的通用成功合同，也不接入该消费者，`submission_adapter` 仍为 `None`。可见 GUI 摘要不是完整 expected，没有当前系统保存消息 class/number/severity 的确定依据，不新增候选文本 parser。查询未找到、超时或不匹配不能证明此前未保存，不能触发自动重存。

## 字段状态观察与填写判定

页面快照和模型投影保留编辑器及所属 grid cell 的 `aria-invalid` 状态。明确 `true`、`grammar`、`spelling` 或未知值时，填写即使值回读相等也不能通过接受判定；任一来源的明确错误优先。无标记保留 `unavailable`，不推断没有 SAP 校验错误。仍需同一字段、页面及稳定回读；`business_validated=false` 不变。这是有界观察及页面值接受规则，不代替完整 SAP 校验或保存回执。源码见 [DOM 观察](../../../../../Scene/sap_workbench/browser_service/dom.py) 与 [字段接受判定](../../../../../Scene/sap_workbench/browser_service/page.py)。

## 隔离验证

- 重点 Python 组合 **260 passed，1.65 秒**。首次为 **259 passed / 1 failed**，原因是撤权测试的 Mock 迭代值耗尽；修复 fixture 后同组合通过，没有将该夹具失败当作 SAP 错误或省略记录。
- PO 模块专项 **139 passed**，与重点组合重叠，不累加为总数。
- 相关 Node 六文件 **165 passed，310.998541 毫秒**，使用固定 DOM/前端/工具 fixture。
- Bun host/deployment/purchase_order **4 passed / 0 failed，81 assertions，1.99 秒**，真实 OpenCode 运行模块使用替身供应商及 SAP 桥。
- 完整 SAP Python **1123 passed / 2 skipped，92.25 秒**；两项分别为暂停的真实 Chrome fixture、默认不启用的固定上游资源 fixture。上述专项有重叠，不与完整回归累加。

```sh
.venv/bin/python -B -m pytest -q tests/test_sap_workbench_purchase_order_bridge.py tests/test_sap_workbench_mcp_data.py tests/test_sap_workbench_bridge_boundary.py tests/test_sap_workbench_purchase_order.py
node --test tests/test_sap_workbench_field_validation.cjs tests/test_sap_workbench_observations.cjs tests/test_sap_workbench_dom.cjs tests/test_sap_workbench_frontend.cjs tests/test_scenes_frontend.cjs tests/test_coding_frontend.cjs
SAP_OPENCODE_ROOT=/path/to/rsmcode/opencode bun test Scene/sap_workbench/opencode_adapter/host.test.ts Scene/sap_workbench/opencode_adapter/deployment.test.ts Scene/sap_workbench/opencode_adapter/purchase_order.test.ts
```

独立字段状态专项另见 `tests/test_sap_workbench_field_validation.py`；上述专项之间有重叠，不与完整回归相加。

完整隔离回归命令为：

```sh
SAP_WORKBENCH_LIVE=0 SAP_WORKBENCH_UPSTREAM=0 SAP_MCP_PYTHON=/path/to/sap-connect/sap-pyrfc/.venv/bin/python \
  .venv/bin/python -B -m pytest -q -rs tests/test_sap_workbench*.py
```

## 本机加载与公共资源核对

按既有启动环境重载桌面 9876（PID 64390 → 66363，由原 Electron 29265 自动恢复）和 Web 9899（PID 64430 → 66404，继承完整原命令/环境），数据根、主密钥和配置保留。重载前没有活动工作台绑定；15 份相关源码摘要在重载后保持一致。18 项公共 health/chat、SAP JS/CSS、共享 runtime 资源及匿名配置/会话 API 检查通过，未复制身份数据、重置密码或产生 SAP/OpenCode/MCP 请求。原始安全运行快照见 [purchase-order-services-reloaded.json](purchase-order-services-reloaded.json)。

这证明当前本机后端已加载本轮源码且公共入口可用，不是登录后的双栏、真实工具调用或 SAP 业务验收；新 `tools.ts` 仍由随后创建/恢复的场景 host 加载。没有新的 Chrome UI 操作；桌面正常登录后的完整双栏仍待验收。

## 保留的完整验收合同

没有新的 Chrome UI、真实模型、SAP 保存或 OpenCode 操作验收。6.3 的实际保存执行器、可信完整 GUI expected、确定回执及完整业务只读核对尚未齐备；5.3 的完整表格分页、5.7 的真实兼容及 6.6 的提交验收不勾选。Linux `runtime_verified` / `workbench_binding_verified` 仍为 `false`，4.3/4.4/4.7 未验收。独立读取工具和隔离测试不使 G3 开放，也不替代生产权限、统一登录或桌面登录后的完整双栏验收。
