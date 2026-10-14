# 阶段 6 证据：remote 不启本机后端、设备通道与服务器业务分开（任务 6.2）

日期：2026-10-13。规范依据：`specs/desktop-tenant-context/spec.md` 第 5–17 行（远本分离、远程不可用、切换来源）与第 31–33 行场景（"设备网关不可达但服务器聊天正常"）。

## 1. 复验（既有实现）

| 规范要求 | 既有实现 | 复验 |
|---|---|---|
| remote 模式不启动本机 Python 后端 | `index.ts:853-857`：`if (startup.mode === 'remote') { console.log('[remote] … local backend not started …') } else { await startBackend() }` | `tests/test_desktop_startup_contract.cjs` **7/0**，其中一项就是"a remote install never probes for or starts a local Python"（断言 remote 分支内不得出现探针/启动） |
| 远程不可用不自动切本机 | 同一分支：模式是显式存储的（默认 local、升级不翻转）；未配置 remote 时 `syncBrokerOrigin()` 留空 origin，登录失败为 `backend_unavailable` 而**不猜**本机 | 同上分支断言 + `tests/test_desktop_signout.cjs` **9/0**（登出/身份失效路径） |
| 服务器侧 gateway/meta 行为 | `integrations/desktop/gateway.py` 等 | `tests/test_desktop_gateway.py` **15 passed**、`tests/test_desktop_meta.py` **11 passed** |
| 设备通道独立（连接/心跳/重连/空闲） | `device-client.ts`：`DeviceState = idle\|connecting\|ready\|reconnecting\|stopped`、`onState(state, detail)` 独立回调；`IDLE_TIMEOUT_SECONDS=90`；`scheduleReconnect(reason)`；`stop()` 清定时器 | `tests/test_desktop_device_client.cjs` **22/0**、`test_desktop_device_connection.cjs` **7/0**、`test_desktop_ws_client.cjs` **11/0** |

**缺口**：没有任何测试把两条通道**同时**放在"设备网关失败 + 服务器业务正常"这个场景下验证（规范第 31–33 行场景此前只有实现、没有集成证据）。

## 2. 新增集成测试：`tests/test_desktop_remote_gateway_isolation.cjs`（**4/4 通过**）

驱动真实编译产物（`connection.js` 的 `probeServer` + `device-client.js` 的 `DeviceClient`），注入确定性的失败 socket（`openSocket` 抛 `ECONNREFUSED`）与确定性 jitter（`random: () => 1`），因此不需要网络也不需要等待退避：

1. **设备网关死了，服务器结果不变**：服务器 metadata 探测 `ok:true` 的同时，设备通道自报 `currentState === 'reconnecting'` 且 detail 保留自己的原因（`ECONNREFUSED`）；两者同时成立——可选通道失败**不影响**服务器业务状态；随后 `stop()` → `stopped`。
2. **停止设备通道不再产生任何尝试（不重放）**：`stop()` 后等待一段时间，`openSocket` 调用次数不再增加——这正是"切换来源/模式"路径所依赖的性质（旧请求不重放到新来源）。
3. **设备通道无法启动本机后端**：`device-client.ts` 中不出现 `python-manager`/`startBackend`/`PythonBackend`；且 `index.ts` 的 remote 分支内没有 `startBackend()`、local 分支有。
4. **服务器失败就是服务器失败**：探测失败为 `kind:network, code:network_unreachable`；未接入的设备客户端保持 `idle`（服务器失败不构成对设备通道的结论）；文案里**不存在** `remote_err_device_offline` 这类"设备离线"句，服务器失败不会掉进它。

## 3. 判定与未验证

- **6.2 勾选**：remote 不启动本机后端（既有实现 + 既有测试）、设备通道故障与服务器业务分开展示（新增集成测试）、网关失败而聊天正常（新增集成测试覆盖）、不自动切模式（分支断言 + 未配置 remote 留空 origin）与不重放旧请求（停止后无新尝试）均有证据。
- **未验证**：真实网关与真实网络下的握手失败/退避恢复、真实安装包在无 Python 机器上的远程聊天（→ 6.4/6.5 真机验收）；本机验证到模块级集成与源码级不变量。
