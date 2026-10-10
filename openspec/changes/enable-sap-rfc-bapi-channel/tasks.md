## 1. 前置验证（现场）

- [x] 1.1 从部署主机验证 SAP RFC 端口（`33xx`）可达，记录使用的接入方式（`ashost` + `sysnr`）。
      `sap.goodsap.cn:3300` 与 `:44300` 均 OPEN；接入方式取 `ashost=sap.goodsap.cn` + `sysnr=00`（由 Web GUI 端口 `44300` 推得）。
- [x] 1.2 以 RFC 参数形状连接并执行一次**只读** RFC/BAPI，确认到达 PyRFC 后端；记录返回。
      `pyrfc 3.3.1` 真机握手成功：`resolve_backend('auto', rfc) == 'pyrfc'`，`RFC_PING` 与 `RFC_READ_TABLE`（T001）均经 PyRFC 返回；单次调用 0.1–0.6s。离线侧 `resolve_backend` 已实测（见 design 的 Context），本步补上真实连通性。
- [x] 1.3 若 1.1 或 1.2 失败，停止本变更并记录结论。**未触发**：1.1/1.2 均通过。

## 2. 网关（独立仓库 `rsmCode/sap-connect/sap-pyrfc`）

- [x] 2.1 将 `connection.py::read_table` 的后端选择改为「有 ADT 就优先 ADT」（与 `run_query` 同款并保留回退）。
- [x] 2.2 补充单元测试：同时具备 RFC 与 ADT 目标时 `read_table` 仍走 ADT，仅 RFC 时回退 pyrfc。
- [x] 2.3 重放现有取数路径回归，确认 `sap_data_call` 的 `read_table` 返回形状（`backend == 'adt'`、`column_meta`）未被破坏。
      现场以改动后的代码对同一组参数求值：`call_rfc → pyrfc`、`read_table → adt` 且 `column_meta` 存在（`evidence/sap-rfc-live.md`）。**同时发现**：运行中的网关进程仍早于本提交，`backend=auto` 下 `read_table` 落到 PyRFC、`column_meta` 缺失——该错配现场已复现，故发布顺序为「网关先、场景后」。
- [x] 2.4 在该仓库提交并记录提交标识，供场景侧兼容基线与发布锁引用。（`1fd2cc6aa` 为行为改动；`44d4c2379` 为随之修正的 `read_table` 形状文档说明，同为 `dev` 分支）

## 3. 场景配置与凭据绑定（`backend/configuration.py`、`backend/store.py`）

- [x] 3.1 `DEFAULT_CONFIG["sap"]` 增加两个可选字段（RFC 主机、系统号），全部默认空。
- [x] 3.2 增加校验（主机名与系统号形状、允许全空、拒绝非法值）；不引入新的必填项，旧配置可直接读取。
      主机名拒绝 `:`（端口不得混入，RFC 端口由系统号推导）；系统号须两位数字；两者违规都报 `invalid_rfc`。
- [x] 3.3 将 RFC 目标并入 `store.py::_binding`，使 RFC 字段变化导致已存口令失效。**并入方式为仅在字段非空时追加**：从未配置 RFC 的部署绑定串逐字不变，升级不作废既有凭据（已加测试）。
- [x] 3.4 在 `capabilities` 中把 BAPI 行标为依赖「RFC 端点已配置」；未配置时给出明确说明，不标为可用。
      新增 `mcp_bapi_available` / `mcp_bapi_unavailable` 两个注记键（三语文案已补）。凭据缺失时不额外增行，`mcp_credentials_missing` 已覆盖。`configuration_checks` 未改：该处各检查项是「必须修好才能建会话」的阻塞项，而 RFC 是可选增强，混进去会把「可选未配」误报成阻塞。
- [x] 3.5 同步 `Scene/sap_workbench/config.example.json`。
- [x] 3.6 补充测试：改变 RFC 字段后 `resolve_mcp_credentials` 返回需要重新填写凭据（已加入既有参数化用例 `[rfc_host]`），并新增「空 RFC 字段不改绑定串」用例。

## 4. 登录通道（`backend/mcp_login.py`、`mcp_runtime.py`、`mcp_worker.py`）

- [x] 4.1 `connect(...)` 增加 RFC 参数；`connect_from_config` 从配置取 RFC 字段传入（新增 `rfc_target()` 供 `mcp_runtime` 复用）。
- [x] 4.2 pyrfc 分支在 `sap_connect` 参数中补充 RFC 字段，并把 `backend` 由写死的 `"adt"` 改为「填了 RFC 取 `auto`，否则 `adt`」。
- [x] 4.3 身份核对扩展为同时核对 `who['rfc']` 的 `ashost`/`sysnr`；保持 `system: unverified` 的既有口径。
- [x] 4.4 `mcp_runtime.py::_start` 的 initial 报文与 `mcp_worker.py` 透传 RFC 字段。
- [x] 4.5 确认失败语义为显性失败：RFC 不可达时不静默降级，不宣称 BAPI 可用。
      登录期健康检查（`healthcheck` → ping）在 RFC 不通时即以 `mcp_login_failed` 失败，不会拖到第一次写入才暴露。
- [x] 4.6 新增 RFC 登录路径用例（参数形状、`auto`/`adt` 选择、身份核对扩展、不可达即失败）。
      用例落在 `tests/test_sap_workbench_mcp_login.py`（该文件才是登录通道的测试归属，而非 `business_channel`），并在该文件的网关替身中补上 `rfc` 元数据回显。`test_sap_workbench_mcp_sdk_wrapper.py` 未改：它测的是 `mcp_data` 解析，不涉及 `connect` 签名或 `sap_connect` 参数。

## 5. 前端（`frontend/workbench.js`）

- [x] 5.1 配置表单增加两个 RFC 字段；保存时的 key 列表纳入新字段。
- [x] 5.2 ~~依 Web GUI 端口预填主机与系统号~~ **取消**：字段语义是「填了即启用」，预填后保存一次即等于隐式启用 RFC，与「MUST NOT 从 Web GUI 地址自动推导并启用」是同一件事。改为两字段一律初始为空。
- [x] 5.3 中文（简体/繁体）与英文三份文案补齐标签、帮助说明与 `invalid_rfc`；帮助说明写明「留空即纯 ADT」。
- [x] 5.4 `tests/test_sap_workbench_frontend.cjs` 补配置字段用例：两字段渲染为空、系统号限长 2、且触发 `web_gui_url` 的 `blur`/`change` 也不会被预填。

## 6. 文档与能力标注

- [x] 6.1 更新 `Scene/sap_workbench/README.md` 能力表：BAPI 行以 RFC 端点已配置为前提，并补一段后端解析与两路径形状差异说明。
- [x] 6.2 更正 README 中「原生 RFC SDK 未安装，sap-pyrfc 当前使用 ADT 回退」的过时表述（就地加日期更正并保留当轮历史，不改写历史记录）。
- [x] 6.3 更新 `project/skills/sap-workbench/SKILL.md` 与 `references/boundaries.md`：`call_rfc` 可达性取决于 RFC 端点；`read_table`/`run_query` 仍走 ADT；两路径的形状差异如实记录。
      更正了自己的过度表述：原先写作「RFC 侧只返回 512 字节截断的单列文本」，实际 `read_table` 会用 `|` 拆分重建出行，真实差异是**缺 `column_meta`、值含 `|` 会错位、`row_skip` 仅 PyRFC 侧支持**；README 与 boundaries 均已改为该口径。

## 7. 测试与验收

- [x] 7.1 新增回归断言：**配置 RFC 后 `read_table` 仍解析到 ADT**，且返回形状（`backend`/`column_meta`）不变。
      网关侧由 2.2 的单元测试覆盖；现场以改动后代码求值确认 `read_table → adt` 且 `column_meta` 存在。运行中的旧网关反例见 2.3。
- [x] 7.2 运行 SAP 相关隔离回归（Python 与相关 Node），记录通过/跳过数量与原因。
      - `tests/test_sap_workbench.py`：**56 passed / 2 failed**；`tests/test_sap_workbench_mcp_login.py`：**30 passed**
      - `business_channel` + `mcp_runtime` + `mcp_pipe` + `project_toolkit`：**84 passed**
      - 更宽批次（`mcp_data`、`mcp_lifecycle`、`mcp_sdk_wrapper`、`identity`、`access`、`dispatcher`、`shutdown`、`project_plugin`、`bridge_boundary`）：**345 passed / 8 failed / 10 errors / 1 skipped**
      - Node（`test_sap_workbench_frontend.cjs`）：**76 passed / 0 failed**
      - 失败与报错全部为**本机环境限制**，与 RFC 改动无关，均已定位到具体错误：
        - `WinError 10048` 绑定 `127.0.0.1:9911` 失败——该端口由本机正在运行的场景占用（进程 33200），影响 `test_sap_workbench.py` 的 2 例。
        - `ValueError: the environment variable is longer than 32767 characters`——Windows 环境变量长度上限，影响 `mcp_data` / `mcp_sdk_wrapper` 传大 payload 的用例（该文件另假设 POSIX 版 `.venv/bin/python` 路径）。
        - `OSError: [WinError 10038]`——POSIX socket/进程语义假设，影响 `shutdown` 的 7 例参数化。
      - 因此**完整隔离回归在本机不可能全绿**；上述数值均限定为「本机可跑部分」，不据此声称全量通过。
- [x] 7.3 现场验收：在配好 RFC 端点的租户上，经真实会话执行一次只读 `call_rfc`，确认到达 PyRFC 后端。
      对运行中的网关（`127.0.0.1:8200`）发起真实 MCP 会话：`sap_connect(backend=auto, ashost, sysnr)` → `connected: True`；`call_rfc(RFC_PING)` → `backend: pyrfc`；`sap_disconnect` 收尾。结果仅作传输结果回报，未声称业务完成。
- [x] 7.4 校验本变更并补齐 evidence 记录。
      `evidence/sap-rfc-live.md` 明确标注哪些结论来自现场、哪些来自隔离测试、哪些**未验证**（写入类 BAPI 未触发、经场景介导的会话路径未跑、`purchase_order.py` 读取器未重放）。
