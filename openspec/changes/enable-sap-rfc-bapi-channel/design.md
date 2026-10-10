## Context

问题与根因见 `proposal.md`。本文件记录当前代码接入点、取舍、门槛与未决参数。

现状（均为实测，非推断）：

- `sap-pyrfc` 网关是双后端服务器：`resolve_backend(preferred, rfc, adt)` 仅在「PyRFC 已安装**且**同时提供了有效 RFC 配置」时返回 `pyrfc`，否则回退 ADT（`config.py`）。
- 场景登录走 `kind == "pyrfc"` 分支，参数是 `url=https://<web-gui-host>` + `backend="adt"`，`kind` 只是内部标记而非后端选择（`mcp_login.py`）。
- 因此注册表这条连接的 `rfc` 为 `None`，`call_rfc` 命中 ADT 拒绝分支（`connection.py`）。
- 环境已验证：监听 8200 的进程所用解释器中 `pyrfc 3.3.1` 可导入，`SAPNWRFC_HOME` 指向存在的 SDK；以该网关生产代码对两种参数形状求值，ADT 形状 → `adt`，RFC 形状 → `pyrfc`。
- 场景读取路径按 ADT 形状设计：`purchase_order.py` 要求 `payload['backend'] == 'adt'` 且消费 `column_meta`（该读取器今天只在默认关闭的旧引擎路径可达，作为契约证据使用）。RFC 的 `read_table` 路径两者都不满足，而 `sap_data_call` 对外的 `read_table` 是同一条路径，形状变化对模型可见。

## Goals / Non-Goals

**Goals：** 让中介通道的真实能力与已有工具面一致——配置 RFC 端点后 `call_rfc` 可到达 BAPI，且现有取数与采购订单读取路径不发生行为变化。

**Non-Goals：** 不新增 MCP 连接或工具；不扩大 `mcp_business.py` 的参数面；不新增用户可见的后端选择项；不支持消息服务器组（`mshost`/`msserv`/`group`/`r3name`）接入形态，也不处理 `saprouter`；不提供 Customizing（新增公司代码等）路径；不开放左侧页面读写；不实现网关的通用「按工具指定后端」配置（本变更只对齐 `read_table` 的选择）。上述 RFC 接入变体若现场确需，另开变更。

## Decisions

1. **网关让 `read_table` 也「有 ADT 就优先 ADT」**。与 `run_query` 现有的 `resolve_backend("adt", ...)` 写法一致，并保留 `resolve_backend` 自带的回退。理由：切到 `auto` 后 `read_table` 会落到 `RFC_READ_TABLE`，该路径不返回 `column_meta`、行以 `|` 拼接且有长度上限，会改变中介通道对外 `read_table` 的返回形状（`purchase_order.py::_rows` 对 `backend == 'adt'` 的硬性要求即这一契约的现存证据），并让 `read_table` 与 `run_query` 在一条连接上各走一套后端。对当前纯 ADT 场景这是 no-op，风险最低。
2. **场景一侧只新增两个可选字段**：RFC 主机与系统号。是否使用 RFC 由「字段是否填写」**唯一**决定，`sap_connect` 的 `backend` 相应取 `auto` 或 `adt`；不新增后端选择项，理由是后端与这两个字段一一对应，多一个可填项只多一处能填错的地方。全部为空时行为与今天完全一致（纯 ADT）。
3. **失败语义取显性失败（fail loud）**。现状是 pyrfc 网关注册后若 ping 失败，`connected` 不为真，场景将整次登录判为 `mcp_login_failed`。本变更保留该强度：配了 RFC 就把它当作硬前置，不以静默降级掩盖配置或网络错误。
4. **RFC 目标属于凭据绑定**。`store.py::_binding` 目前已包含 `system_id`、Web GUI 的 scheme/host/port、`client` 与 MCP 账号；RFC 目标须并入，否则改 RFC 主机却沿用旧口令属于绑定泄漏。并入方式为**仅在字段非空时追加**：从未配置 RFC 的部署绑定串逐字不变，升级不会作废既有凭据（已加测试）。
5. **身份核对扩展而非放宽**。pyrfc 分支现在核对 `who['adt']['url']`。加上 RFC 参数后 `public_info()` 会同时含 `rfc` 段，可再核对 `ashost`/`sysnr`。语义仍沿用既有口径：这些是**网关登记元数据**，`system` 保持 `unverified`，不等于从 SAP 服务器实测的系统标识。
6. **不为 BAPI 单独开一条 MCP 或工具**。`call_rfc` 已在工具面内、已进账本/配额/审计、失败记为 `unknown`，本次只补齐它到得了的后端，不重复造通道。
7. **不做任何预填**。原方案允许按 Web GUI 端口（`44300 + n` → 系统号 `n`）预填主机与系统号作为录入便利；实现时放弃。字段语义是「填了即启用」，预填之后用户只要保存一次就等于隐式启用 RFC——与「MUST NOT 从 Web GUI 地址自动推导并启用」是同一件事的两种写法。两个字段一律初始为空，只由用户显式填写；配置语义仍以显式字段为准。

## Risks / Trade-offs

- **RFC 可达性已验证，残余风险在发布顺序** → 2026-10-10 实测：`sap.goodsap.cn` 的 RFC 端口（`3300`，由 Web GUI `44300` 推得 sysnr `00`）可达；经 PyRFC 完成 `RFC_PING` 与 `RFC_READ_TABLE`；真实会话经运行中的网关（`127.0.0.1:8200`）完成 `sap_connect(ashost, sysnr, backend=auto)` 与只读 `call_rfc`（见 `evidence/sap-rfc-live.md`）。残余风险在发布顺序：运行中的网关若早于 `1fd2cc6aa`，场景下发 `backend=auto` 后 `read_table` 会落到 PyRFC——现场已复现（`column_meta` 缺失）。因此**网关须先发布/重启，场景再上线**。
- **`read_table` 的 RFC 路径本身较弱**（无 `column_meta`、行由 `|` 拼接串重建、值含 `|` 会错位；`row_skip` 仅 PyRFC 侧支持，ADT 侧直接报错） → 本变更刻意让 `read_table` 不落在这条路径上；该上限记入两侧文档。
- **网关改动位于独立仓库** → `rsmCode` 有自己的提交与来源锁定；场景侧发布的兼容基线需与该提交对应，避免「场景已传 RFC 参数、网关仍是旧逻辑」的错配。
- **接入形态收窄** → 只支持 `ashost` + `sysnr`。现场若是消息服务器组或需要 `saprouter` 的拓扑，本变更不能直接用，需要另加字段。
- **能力标注的诚实性** → 工具面早已含 `call_rfc`，但配置未就绪时调用必然失败。界面与文档须以「RFC 端点已配置且可达」为可用前提，不能因为工具存在就标为可用。

## Migration Plan

1. 先独立验证 RFC 端口可达性与一次只读 `call_rfc`，确认链路成立再改配置形态。（已完成，见 `evidence/sap-rfc-live.md`）
2. 网关侧先合并 `read_table` 的优先 ADT 改动并确认对现有纯 ADT 场景无行为变化（重放现有取数与采购订单读取回归）。
3. **网关先发布/重启，场景侧后上线**：现场已复现「旧网关 + `backend=auto` → `read_table` 落到 PyRFC、`column_meta` 缺失」的错配，顺序颠倒会破坏 `read_table` 的对外形状。
4. 场景侧新增可选 RFC 字段（默认全空 → 纯 ADT，向后兼容），随后端重载。
5. 填写了 RFC 的租户需重新填写 MCP 口令（绑定变化所致），属预期；未填写 RFC 的租户绑定串不变，无需重填。
6. 回滚：清空配置中的 RFC 字段即回到纯 ADT；网关回退该提交即恢复 `read_table` 的 `preferred` 取法。两者都不涉及数据库结构迁移。

## Open Questions

- 现场 SAP 是否需要消息服务器组或 `saprouter` 才能用 RFC 接入？若是，需在本变更之后追加配置字段，届时才需要扩要求。
- 后台是否存在不使用 ADT 网关（`sap-abap`）的部署，需要单独核对启用状态对登录回滚语义的影响（属既有行为，非本变更引入）。
