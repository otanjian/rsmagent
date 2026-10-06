# 能力边界的依据

本文件记录每条边界**为什么**成立，以及它有哪些已验证证据。标注为"不可用"的项不是尚未实现，而是**在当前架构下没有可行路径**。

## 左侧画面：跨域 iframe

左侧 SAP 画面是工作台页面里的一个 `<iframe>`，内容从 SAP 源站直接载入。

- 浏览器同源策略使**父页面无法读取**该 iframe 的 DOM；
- 工作台会话运行在服务端，与用户浏览器中的那个 iframe **没有任何通道**；
- 因此：字段值、表格内容、标签页、系统消息、事务标题——**都读不到**。

推论：任何"看到左侧页面上写着……"的回答都是编造的。这包括看似无害的复述，例如"看起来你已经登录了 ME21N"。

已归档的实测：曾尝试通过 `rsm_parent_origin` / `postMessage` 通道读取嵌入页内容，**未通过**。不要重新尝试这条路。

## 事务导航：只有单向投递

唯一可用的路径是**服务端投递一条导航指令**，由左侧 iframe 应用它：

1. 工具调用携带事务码；
2. 服务端用**自己保存的**场景配置拼出目标地址（含 `~transaction=<CODE>`）；
3. 指令交给该绑定正在运行的浏览器租约；
4. 左侧 iframe 把 `src` 换成该地址，并回报"已应用"。

回报的语义**仅限**第 4 步。工作台**无法**观察 SAP 是否接受了这次导航。

## 业务数据：场景中介调用，不是直连 MCP

数据能力由项目插件工具 `sap_data_call` 提供，**不是**由对话直接连 MCP 网关。这条选择是实测倒逼的，不是偏好：

- MCP 服务器上 `read_table` / `run_query` / `call_rfc` 的 `connection_id` **是必填参数**，而它只能由**带凭据的** `sap_connect(user, password, ...)` 生成（`sap-connect/sap-pyrfc` 的 `registry.py`：每次 `sap_connect` 生成一个 UUID；`.env` 只喂 `healthcheck`，不预注册连接）；
- OpenCode 的 MCP 客户端**不携带会话身份**，注册表又按 `connection_id` 索引，因此服务端无法在 MCP 那条路上判断"这是哪个会话"；
- 插件钩子也救不了：`tool.execute.before` 由**每个内置工具自己**触发，MCP 工具调用**不经过插件钩子**。

三条合起来意味着：直连 MCP 要么把凭据/连接标识交给模型，要么无法按会话授权。因此调用被放回**场景桥**后面（与导航同一条路）：插件只带会话标识与业务参数，服务端按会话解析绑定与该绑定的已登记连接，**连接 id 在 MCP worker 内部才被附加**（`SapMcpLogin._call`）。模型既拿不到凭据，也拿不到连接标识。

**可达工具面**（`backend/mcp_business.py`）固定为三个：`read_table`、`run_query`、`call_rfc`。

- `run_query` 只允许**单条只读 `SELECT`**，所以这条通道不会退化成通用 SQL 控制台；
- `where` / `sql_query` 拒绝语句分隔符与注释起始，参数长度、行数、列数都有上界；
- 参数里出现任何身份或连接字段（`connection_id`、`user`、`password`、`client`、`host`、`url`、`tenant_id`、`session_id`）一律拒绝；
- **未暴露**改系统本身的 ADT 工具（`adt_write_source`、`adt_create_object`、`adt_delete_object`、`adt_activate`、`adt_create_transport`、`adt_release_transport`、`bw_*`）——SAP 网关自己会列出这些工具，但场景桥不转发它们。

## 写入：有通道，但"传输成功"不是"业务成功"

`call_rfc` 能调用 BAPI，因此**可以**改变业务数据。这条通道是刻意打开的，但结论必须如实区分：

- 动作按普通动作进入账本、配额与审计（`sap_data_call` **不**被当作只读动作：一次失败在有歧义时记为 `unknown`，而不是干净失败）；
- 返回的是 MCP 传输结果。RFC 返回里通常要检查 `RETURN` / `BAPIRET2` 之类的消息结构才能判断业务是否成功；
- 在检查之前，**任何**"已创建/已保存/已过账/已审批"的说法都缺少依据。这与编造没有区别。

## 相关配置

| 配置 | 作用 | 注意 |
|---|---|---|
| `sap.web_gui_url` | 左侧画面的目标地址 | 导航地址只从这里拼出 |
| `sap.allowed_origins` | 允许的 SAP 源 | 导航目标必须落在其中 |
| `sap.client` | SAP 客户端 | 不从对话取得 |
| `mcp.username` + 加密口令 | 场景中介调用所用的 SAP 账号 | 口令加密存储，**从不下发给模型**；未配置时 `sap_data_call` 会以 `mcp_login_failed` 失败 |
| `mcp.connections` | 已登记连接 | 业务工具面只指向其中的 `sap-pyrfc` |
| `commit_enabled` | 提交开关 | 与 `sap_data_call` 无关：BAPI 写入不经此开关 |

**未配置的代价是真实的**：`mcp.username` / 口令没有保存时，服务端建连失败，`sap_data_call` 会如实返回 `mcp_login_failed`。**不要**因此改用读页面或让用户把口令打进对话——那是另一条边界。

## 一条经验规则

如果某件事**只能**通过"观察左侧页面"或"在左侧页面操作"来完成，那么在当前工作台里它就是**不可用**的。请直接说明不可用，并给出可替代的读取路径（`sap_data_call`），而不是绕路尝试。
