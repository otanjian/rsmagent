# MCP 凭据与连接生命周期隔离证据

日期：2026-10-03。用户要求继续完成可独立实现的任务，同时暂停 OpenCode 与 SAP Web GUI 的实际操作检查。本次仅验证场景代码、真实临时平台身份与存储、模拟 IPC/网关，以及离线 worker 解释器兼容性；未启动或调用真实 SAP、MCP 网关、OpenCode、模型或浏览器。

## 实现与结果

- `ConfiguredMcp` 在每次初始化、工具调用前及响应返回后重新检查拥有者权限与场景配置版本。检查失效时立即进入有界 worker 清理，不等待工作台的后台监控；原配置冲突或平台拒绝错误保留。
- 配置密码轮换、清除，或更改 SAP 目标、Client、MCP 账号均增加场景配置版本。旧绑定不自动采用新配置；其旧 consumer 关闭后，继续使用旧代次仍被拒绝。清除凭据或更换目标/账号后，存储层不能重新解析旧凭据。
- 正在读取时发生配置轮换，响应后的重新检查会丢弃旧代次结果并关闭 consumer；测试断言旧结果未返回。
- 平台注销或单个用户的 Agent 使用权撤销，使该用户的旧 worker 在下一次调用前关闭。另一个仍获授权、使用相同项目目录的用户保有独立 consumer，可继续调用。
- 初始化持有同一个实例的锁；同时发起的初始化共享同一次 worker 分配与凭据交付。两个平台用户的绑定、远端会话 ID 和 worker 实例仍分别独立，平台会话 token 不传入 worker。
- 模型只能选择固定公开连接别名，不能将私有 `connection_id` 用作别名或注入工具参数；未初始化的连接别名也不会发往 IPC。登录适配器只为工具注入其自身保存的 connection ID。
- `SapMcpLogin` 的可信身份重新检查抛出拒绝时，也关闭已有连接并保留原错误；读到的选用 ADT 账号、Client、连接 ID 与目标元数据不一致时，销毁部分连接。

## 可重复测试

以下命令通过 **73 项隔离测试**：

```sh
.venv/bin/python -m pytest -q tests/test_sap_workbench_mcp_login.py tests/test_sap_workbench_mcp_runtime.py tests/test_sap_workbench_mcp_lifecycle.py tests/test_sap_workbench_deadline.py tests/test_sap_workbench_probe.py
```

本次结果：`73 passed in 19.10s`。这是上述五个测试文件的合计，不代表全部项目或全部 OpenSpec 任务已经验收。

| 验证项 | 测试输入与边界 | 已证明的结果 |
| --- | --- | --- |
| 密码轮换、清除 | 真实临时平台管理员通过配置 HTTP handler 更新；真实场景 SQLite/加密存储；fake worker IPC | 旧版本不能解析凭据或继续调用；worker 接收关闭，不收到新的读取请求；新密码只属于新配置代次 |
| 目标、Client、账号变更 | 同一 HTTP/存储边界分别更改 target/client/account | 旧 consumer 失效；绑定不会被静默切换到新目标或新账号；旧绑定凭据不复用 |
| 平台注销、单用户撤权 | 真实临时平台 session 与独立用户角色授权；fake worker | 原拒绝码保留；失效用户立即清理；同项目另一用户的 worker 与使用权不受影响 |
| 清理超时 | 模拟 stdin drain/进程等待停滞，测试中缩短关闭时限 | 超时进入 kill 路径并清空 process/ready；取消清理也先进入 kill 路径再传播取消 |
| 并发初始化 | 内存事件阻塞 fake subprocess 分配，使两个初始化等待同一实例 | 仅一次分配与初始凭据交付，两个请求取得同一 ready 状态 |
| 读取途中轮换 | fake stdout 返回前，由真实临时配置 HTTP handler 轮换密码 | 响应后检查拒绝旧代次；旧结果不返回，consumer 关闭 |
| 私有 connection ID 冒用 | 公共调用层注入外来别名/参数；两个模拟 SAP 账号各有独立网关 connection ID | 注入被拒绝；各自只调用和断开自身 connection ID；ready 结果不包含私有 ID |
| 网关身份/目标混淆 | 模拟 whoami 返回错误 target，或旁支 RFC 账号与选用 ADT 账号不一致 | 失败并清理部分连接，不采用旁支身份作为成功依据 |

`test_sap_workbench_mcp_lifecycle.py` 使用真实临时平台认证、授权与场景存储，但 subprocess、MCP response、SAP 账号/身份和 connection ID 使用替身。其他 probe HTTP 仅为本机隔离测试服务器。上述证据没有把 fake IPC 关闭等同于实际网关的现场回收，也没有把两个模拟 SAP 身份等同于两个真实 SAP 账号验收。

## 实际 worker 解释器的离线兼容边界

此前离线检查已确认专用 `sap-pyrfc/.venv/bin/python` 是 **Python 3.10.0**，安装 **AnyIO 4.14.2 / MCP 1.29.0**，没有 `async_timeout`，也没有 `asyncio.timeout`、`Task.cancelling()` 或 `Task.uncancel()`。场景已使用同任务 deadline 兼容实现，防止把 AsyncExitStack 的退出移到另一个任务。

本次 73 项中保留并通过了 `test_sap_workbench_deadline.py` 的实际 worker 解释器烟测：仅在内存中创建两个 AnyIO task group，断开模拟连接、在进入上下文的同一任务退出两组，并验证超时后可继续 await。主平台解释器还覆盖 native/fallback deadline、外部取消、嵌套超时、定时器撤销，以及网关提交超时的请求取消。

这是离线 SDK/解释器兼容证据，未创建真实 MCP transport、网络会话或 SAP 连接。既有解释器证据见 `worker-compatibility.json`；本次不把早先现场连通结果计为重测结果。

## SAP 系统身份核验限制

已核对本地参考实现：

- `rsmcode/sap-connect/sap-abap/src/connection-registry.mjs` 的 `publicInfo()/whoami()` 返回 registry 保存的 `host/port/user/client/https` 等连接参数。
- `rsmcode/sap-connect/sap-pyrfc/sap_pyrfc_mcp/registry.py` 的 `public_info()/whoami()` 返回 registry 保存的 `adt`/`rfc` 参数；当前场景选用 ADT 路径，不能以旁支 RFC 账号代替 ADT 账号核对。

两者的 whoami 都未提供从 SAP 服务端取得的系统 SID。因此 ready 增加 `identity_verification`：账号与已报告目标仅标记为 `gateway_metadata`，未报告目标标记为 `unverified`，系统始终标记为 **`unverified`**。不把配置中的 `system_id` 填成已核验的 SAP 系统，也不把 registry 参数等同于服务端身份声明。

本次未重测真实双 SAP 账号、真实网关在上述变更后的销毁行为，以及 SAP SID 的服务端核验。统一 SAP 登录、完整生产权限与相应现场验收仍按此前范围延后。本证据只支持任务 5.8 的独立实现部分，不勾选该完整任务，也不修改 tasks 或归档 change。
