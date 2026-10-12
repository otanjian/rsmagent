## 1. master 编辑器接入

- [x] 1.1 核对固定 master 来源和当前实际装配，定向提取 MCP 编辑器所需函数，记录必要差异，不改未使用的完整上游页面。
- [x] 1.2 装载原弹窗模板、样式和翻译，将系统接入 MCP 新增/编辑/租户覆盖接入；保留其他类型界面、只读详情和能力中心/Desktop 跳转。
- [x] 1.3 沿用表单/JSON、三种传输和高级设置，补齐模式转换前校验；不丢未知字段或修改存量连接名称。
- [x] 1.4 复用当前页面身份、离页保护、请求失效及焦点处理，确保重复打开只绑定一组事件、关闭清理秘密。

## 2. 必要字段与现有服务适配

- [x] 2.1 补齐 type/url/env/headers/前缀的映射及服务端校验，多请求头使用一个加密映射槽，兼容旧单请求头且明确新旧表示优先级。
- [x] 2.2 沿用秘密槽的整组保留/替换/清除，补齐键名与秘密同步及界面提示；表单/JSON 往返、普通字段编辑不丢凭据，清除后不恢复旧槽。
- [x] 2.3 补齐 timeout、独立 OAuth scope 到现有客户端的传递，保留旧配置有效行为和既有授权机制，不引入新授权流程或迁移框架。
- [x] 2.4 给单条 MCP 创建补齐初始 enabled，以及创建和幂等结果同事务、同键复查及重放鉴权；保持默认创建行为和原分配/审计规则。

## 3. 保存与测试接线（依赖对应的第 2 组能力）

- [x] 3.1 JSON 多条输入依次调用现有单条创建接口，显示成功/失败/未提交，失败或结果未知时停止；成功项移出待提交内容，未完成项保留。
- [x] 3.2 实现每项幂等键保留及未知结果核对/原请求重试，修改明确失败项时更新键，不重发成功项或为回滚自动删连接。
- [x] 3.3 扩展已有草稿测试的可选连接 ID/版本与平台作用域，服务端组合当前草稿和整组秘密；修正平台测试中的租户强制条件，复用受控探测。
- [x] 3.4 接通 master 的测试、保存前检查和“仍然保存”，仅跳过探测，不绕过校验/权限/版本；草稿结果不写正式健康记录。

## 4. 针对性验证与交付

- [x] 4.1 扩展既有后端用例验证字段实际传入客户端、旧配置兼容、凭据整组操作、初始停用及创建幂等中断/并发重试；确认稳定 ID、继承和分配不变。
- [x] 4.2 更新 MCP 前端用例覆盖表单/JSON 往返、部分成功/结果未知/重试、重复打开和身份变化；保留其他类型及原权限断言。
- [x] 4.3 对实际注册路由验证租户管理、跨租户拒绝、零租户平台管理员、编辑草稿保留凭据及部署开关；草稿版本变化和平台覆盖不借用秘密。
- [x] 4.4 在实际页面对照 master 布局完成新增/编辑/批量/回读，使用受控 MCP 协议服务完成至少一种远程连接闭环；补充受影响传输/OAuth 映射测试及窄屏、主题、键盘和翻译检查。
- [x] 4.5 运行受影响的路由、加载/来源和脚本检查，确认仅一套编辑器生效；记录复用范围、两项交互差异（逐项批量、整组凭据）和既有部署限制。
- [x] 4.6 更新配置与发布说明，确认旧配置兼容及项目现有恢复方式；运行 OpenSpec 严格校验和适用检查，记录实际通过及未验收项，不把规划完成计为代码交付。

## 验证记录

以下为本次实际运行的证据，未列为通过的项目保持未勾选。

通过：

- `node --test tests/test_external_connections_frontend.cjs`：69 通过 0 失败，含表单/JSON 往返、批量逐项提交、部分成功重试、结果未知原键重试、身份变化清理、离页保护、未知字段拒绝、传输推断、master 编辑器三种语言标签齐备。
- `NODE_PATH=$(npm root -g) node tests/test_external_connections_browser.cjs`：20 场景 0 失败，`pageErrors` 与 `unexpectedRoutes` 均为空。实际页面（Chromium）覆盖 master 布局的新增、传输切换、表单↔JSON 往返、未知字段点名、批量部分失败与复用原幂等键重试、编辑草稿绑定行版本、测试失败后“仍然保存”、未保存提示层级、窄屏 360px、深色主题、Escape 键盘路径。
- `.venv/bin/python -m pytest tests/test_external_connections_browser.py tests/test_mcp_console.py tests/test_web_console_assets.py tests/test_external_connections_menu.py -q -p no:randomly`：43 通过（含“只装载一套编辑器”与 console 脚本清单断言）。
- `.venv/bin/python -m pytest tests/test_external_mcp_adapter.py -q -p no:randomly`：85 通过，含新增的真实协议闭环 `test_probe_closes_the_loop_against_a_real_mcp_server`——受控 loopback Streamable HTTP 服务按 `initialize` + `notifications/initialized` + `tools/list` 应答，断言请求头值确实来自秘密槽、会话头被回带、且没有任何业务工具调用；同一文件的既有用例继续覆盖传输/认证资格校验与 OAuth scope 映射到客户端。
- `.venv/bin/python -m pytest tests/test_external_connection_service.py tests/test_external_mcp_adapter.py tests/test_external_test_binding.py tests/test_external_connections_api.py tests/test_external_adapter_contract.py tests/test_external_authorization.py tests/test_external_connection_schema.py -q -p no:randomly`：271 通过，1 失败（见下）。
- `.venv/bin/python -m pytest tests/test_external_connections_api.py tests/test_route_registry.py tests/test_external_authorization.py tests/test_external_identity_http.py tests/test_http_policy.py tests/test_http_gate.py tests/test_identity_scope_gate.py -q -p no:randomly`：全部通过（含草稿测试路由的零租户平台管理员与跨租户拒绝）。
- `node --test tests/test_desktop_external_broker.cjs tests/test_i18n_external_identity_keys.cjs`：全部通过。
- `openspec validate align-mcp-editor-with-master --strict`：通过。

未验收（不计为交付）：

- stdio 启动的启用态：部署未接受 execution-isolation，只验证了拒绝路径；不声称启动能力已验收。
- SSE 的真实协议闭环、OAuth 授权流程本身：只覆盖映射、离线校验与既有授权机制，未接真实 SSE 服务端或授权服务器。
- 深色主题只验证偏好生效后编辑器仍完整可用（截图 `mcp-editor-dark.png` 留档），未做像素级颜色比对。

修复的既有测试夹具缺陷（测试代码，非产品行为）：

- `tests/test_external_connections_browser.cjs` 的 shell 夹具没有替换 `{{COW_CONSOLE_ROUTES}}`（`af965f27` 引入 fork router 后遗留），`router.js` 因 JSON 解析失败抛错，整份浏览器契约在 `HEAD`（414bc6b3）就必然失败（`/tmp/rsm-head` 复跑同一文件得到同样的 3 处 page error）。本次改为按服务端方式渲染（`template.render("chat.html")` + `console_page_metadata()`、`shell_entry_paths()`），夹具入口路径改取自注册表，因为 `ConsoleRoutes` 生效后进入视图会把地址改写成该视图自己的路径。

既有缺陷（本 change 之前即存在，未顺手修改）：

- `tests/test_external_connection_service.py::test_plain_member_cannot_manage_tenant_connections`：在 `HEAD`（未含本 change 的 `integrations/external` 改动）下同样失败（已在 `/tmp/rsm-head` 复跑确认）。
- `tests/test_compat_surface_closure.py` 两项（`RetiredDataPlaneTests::test_only_the_retired_component_calls_a_retired_endpoint`、`CapabilitySwitchReaderTests::test_every_capability_switch_has_exactly_the_recorded_readers`）：在 `HEAD` 下同样失败。
- `tests/test_console_i18n_parity.cjs` 三项（合并快照、命名空间键一致、命名空间先于 console.js 装载）：在 `HEAD` 下同样失败（失败点是 `account` 命名空间，与 MCP 编辑器无关）。
