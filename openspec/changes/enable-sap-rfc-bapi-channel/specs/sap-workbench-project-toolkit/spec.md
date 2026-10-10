## ADDED Requirements

### Requirement: 业务数据的传输后端选择

中介通道 SHALL 支持在场景配置中声明可选的 SAP RFC 端点（主机与系统号），并按字段是否填写选择该连接使用的传输后端。填写后连接 SHALL 以自动选择后端建立：`call_rfc` 解析到 PyRFC 因而可调用 BAPI，`read_table` 与 `run_query` 仍解析到 ADT；MUST NOT 因启用 RFC 而改变取数与自由查询的传输路径、返回结构或列元数据。未填写时连接 SHALL 保持纯 ADT，界面与文档 MUST NOT 把 BAPI 调用标为可用。系统 MUST NOT 为此新增一个需要使用者理解的后端选择项，MUST NOT 从 Web GUI 地址隐式推导并启用 RFC 端点。

#### Scenario: 配置 RFC 后调用 BAPI

- **WHEN** 场景配置了 RFC 端点，会话经中介通道调用 `call_rfc`
- **THEN** 该调用经 PyRFC 后端到达 SAP，返回值仍只作为 MCP 传输结果回报，MUST NOT 据传输声称业务已完成

#### Scenario: 配置 RFC 后取数仍走 ADT

- **WHEN** 同一连接同时具备 ADT 与 RFC 目标
- **THEN** `read_table` 与 `run_query` 仍在 ADT 后端执行，返回结构（含 `backend` 与列元数据）不变，依赖该结构的既有读取器不受影响

#### Scenario: 未配置 RFC 端点

- **WHEN** 场景只配置了 Web GUI / ADT 目标
- **THEN** 连接保持 ADT，`call_rfc` 不被视为可用，能力表与工具说明如实标注

#### Scenario: RFC 端点不可达

- **WHEN** 配置了 RFC 端点，但 RFC 端口从部署主机不可达
- **THEN** 连接建立以可辨识的失败码结束，界面与检测结果 MUST NOT 呈现「BAPI 可用」，MUST NOT 静默降级到 ADT 后把该路径的结果表述为已到达 RFC 后端

### Requirement: RFC 目标纳入凭据绑定

RFC 端点 SHALL 与既有目标字段（SAP 系统标识、Web GUI 地址、Client、MCP 账号）一起构成凭据绑定。更改 RFC 主机或系统号 SHALL 使已保存的 MCP 口令失效并要求重新填写。RFC 连接字段 MUST NOT 出现在工具参数面，出现时 SHALL 与既有身份键同样在连接 SAP 之前被拒绝。

#### Scenario: 更改 RFC 目标

- **WHEN** 已保存口令后，配置中的 RFC 主机或系统号被更改
- **THEN** 旧口令不再对该目标生效，读取凭据时返回需要重新填写凭据

#### Scenario: 参数中出现 RFC 字段

- **WHEN** 业务参数中出现 RFC 主机或系统号
- **THEN** 参数校验在连接 SAP 之前拒绝，且不扣配额、不产生账本行

## MODIFIED Requirements

### Requirement: 能力边界与证据标注

界面与文档 SHALL 按实际可用性分别标注：左侧画面导航为有限可用；SAP 业务数据读取可用，前提是配置凭据就绪；经 BAPI 的业务数据变更可用，前提是凭据就绪**且 RFC 端点已配置并可达**；两者任一前提缺失时 SHALL 标注为对应缺失而非可用；左侧页面读写不可用。MUST NOT 以 skill 已安装、插件已注册、工具已出现在工具面或配置项存在宣称 BAPI 或页面读写可用，MUST NOT 把未通过现场验收的能力标为已通过。

#### Scenario: 凭据缺失

- **WHEN** 配置中没有可用的 MCP 账号
- **THEN** 业务数据各行不标为可用，界面呈现凭据缺失，不呈现可提交的假象

#### Scenario: 仅有 ADT 目标而无 RFC 端点

- **WHEN** 凭据就绪但未配置 RFC 端点
- **THEN** 业务数据读取可标为可用，经 BAPI 的变更行不得标为可用，并说明缺的是 RFC 端点

#### Scenario: 页面读写始终不可用

- **WHEN** 会话在跨域 iframe 中运行
- **THEN** 左侧页面读写保持不可用标注，业务数据能力不以页面读取的形式呈现

#### Scenario: 工具未经验证

- **WHEN** 某工具尚未在真实执行路径验证
- **THEN** 该工具标注为不可用，不以配置存在或以模拟结果宣称兼容
