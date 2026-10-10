## Why

SAP 智能工作台的业务数据通道（`sap_data_call`）在工具面上已包含 `call_rfc`，能力说明与 skill 也把「经 BAPI 变更业务数据」标为可用（凭据就绪时），但实际上**这条能力从未真正成立**：实测的失败信息是 `call_rfc(...) requires PyRFC backend. Arbitrary RFC/BAPI calls are not available via ADT.`

根因在传输后端，不在 SDK，也不在缺少 MCP：

- `sap-pyrfc` 网关（`127.0.0.1:8200`）是**双后端**服务器，`resolve_backend` 按连接参数决定这条连接走 PyRFC 还是 ADT（`sap-connect/sap-pyrfc/sap_pyrfc_mcp/connection.py`）。
- 场景登录只把 Web GUI 的 HTTPS 地址当 ADT 端点下发，并写死 `backend="adt"`，从不提供 RFC 主机与系统号（`Scene/sap_workbench/backend/mcp_login.py` 的 `kind == "pyrfc"` 分支）。因此注册表里这条连接的 `rfc` 为 `None`，`call_rfc` 必然命中 ADT 拒绝分支。
- 该机器上 `pyrfc 3.3.1` 已安装、`SAPNWRFC_HOME` 指向存在且各平台布局均有的 NW RFC SDK。以该网关的生产代码对两种参数形状求值：ADT 形状（`url` + `backend="adt"`，无 `ashost`）→ `adt`；RFC 形状（`ashost` + `sysnr` + `auto`）→ `pyrfc`。README 中「原生 RFC SDK 未安装，sap-pyrfc 当前使用 ADT 回退」的记录已过时，本次一并更正。

同时存在一个**不能靠改场景配置绕开的约束**：`read_table` 用的是连接自身的后端。一旦连接切到 `auto`/`pyrfc`，中介通道对外的 `read_table` 会从 ADT datapreview 换成 `RFC_READ_TABLE`——该路径不返回 `column_meta`，行以 `|` 拼接且受长度限制，返回形状与今天不同；而 `run_query` 是硬编码走 ADT 的，于是同一条连接上的两个读工具会各走一套后端。场景的读取路径本就是按 ADT 形状设计的：`purchase_order.py::_rows` 硬性要求 `payload['backend'] == 'adt'`（该读取器今天只在默认关闭的旧引擎路径上可达，但它是这一契约的现存证据）。因此本变更同时需要网关侧一处改动，使 `read_table` 也具备「有 ADT 就优先 ADT」的选择，与既有的 `run_query` 一致。

## What Changes

- **场景配置声明可选的 RFC 端点**：`sap` 段新增两个可选字段（RFC 主机、系统号），默认全空即保持纯 ADT，向后兼容。是否使用 RFC 由字段是否填写唯一决定；MUST NOT 再引入一个需要使用者理解的后端选择项。
- **传输后端按此选择**：`call_rfc` SHALL 解析到 PyRFC 因而可调用 BAPI；`read_table` / `run_query` SHALL 继续解析到 ADT，取数与自由查询的路径和返回结构不变。
- **网关侧对齐取数选择**：`sap-connect/sap-pyrfc` 中 `read_table` 改为「有 ADT 就优先 ADT」（与 `run_query` 同款写法并保留回退）。此改动对当前纯 ADT 场景是 no-op，只在连接同时具备两种目标时才生效，用于避免上述采购订单读取器被误伤。
- **RFC 目标纳入凭据绑定**：更改 RFC 主机或系统号 SHALL 使已保存口令失效并要求重填；RFC 连接字段 MUST NOT 进入工具参数面，出现时与既有身份键同样被拒绝。
- **RFC 不可达时失败要显性**：不以静默降级到 ADT 的方式掩盖，也不把降级结果表述为「已到达 RFC 后端」。
- **能力标注如实更新**：BAPI 行的可用性以「RFC 端点已配置且可达」为前提；未配置时不得标为可用。同步更正文档中过时的 RFC SDK 说明。

## Capabilities

### Modified Capabilities

- `sap-workbench-project-toolkit`：为「业务数据通道（凭据中介）」补充传输后端选择与 RFC 目标绑定两项要求，并相应修订「能力边界与证据标注」。

## Impact

- 场景接入点：`Scene/sap_workbench/backend/configuration.py`（两个可选字段与校验）、`store.py`（凭据绑定）、`mcp_login.py`（登录参数与身份核对）、`mcp_runtime.py` 与 `mcp_worker.py`（透传）、`frontend/workbench.js`（表单项与三语文案）、`Scene/sap_workbench/config.example.json`。
- 网关接入点：`rsmCode/sap-connect/sap-pyrfc/sap_pyrfc_mcp/connection.py`（`read_table` 的后端选择）。该文件位于**独立仓库** `rsmCode`（origin `http://127.0.0.1:3000/tanjian/rsmCode.git`），需与该仓库自身的提交/发布锁定一并处理，本变更不修改其工具面或 `call_rfc` 的既有语义。
- 文档：`Scene/sap_workbench/README.md`、`project/skills/sap-workbench/SKILL.md`、`project/skills/sap-workbench/references/boundaries.md`。更正「原生 RFC SDK 未安装」的过时表述。
- 测试：`tests/test_sap_workbench_business_channel.py`、`tests/test_sap_workbench.py`、`tests/test_sap_workbench_mcp_sdk_wrapper.py`、`tests/test_sap_workbench_project_toolkit.py`、`tests/test_sap_workbench_frontend.cjs`，并新增一条「配置 RFC 后 `read_table` 仍走 ADT」的回归断言。
- 依赖：`add-sap-workbench-coding-agent-entry`（`sap-workbench-project-toolkit` 能力当前仅存在于该未归档变更的增量规范中，本变更在其上追加要求；归档顺序需与之一致）。
- 前置（现场，未验证）：SAP 的 RFC 端口（`33xx`，与 Web GUI 的 `443xx` 不同）必须从部署主机可达。该连通性未经任何测试，属本变更第一条验收门槛。
- 不涉及：不为「新增公司代码」提供路径——该动作属 Customizing（`SPRO`/`OX02`），无标准 BAPI；本变更只让「能调用 BAPI」这件事成立。不修改 `mcp_business.py` 的工具面与参数边界，不新增 MCP 连接，不支持消息服务器组接入形态，不开放左侧页面读写。
