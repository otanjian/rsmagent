# 证据：RFC 端点的现场只读验证

日期：2026-10-10。对应 `tasks.md` 第 1 组与第 7 组（7.1 / 7.3 / 7.4）。

主机：部署机（Windows，`127.0.0.1`）。目标 SAP：`sap.goodsap.cn`。
凭据：场景库中已保存的 MCP 账号（用户 `S2385`，口令长度 7）——由场景自身的 `auth.crypto` 解密后使用，**口令未打印、未落盘**。
RFC 栈：`pyrfc 3.3.1` + `SAPNWRFC_HOME=C:\nwrfc750\nwrfcsdk`（`pyrfc_installed() == True`）。
参数形状：`ashost=sap.goodsap.cn`、`sysnr=00`（由 Web GUI 端口 `44300` 推得）、`client=200`。

## 1.1 RFC 端口可达性

TCP 探测：`sap.goodsap.cn:3300`（RFC，`3300 + sysnr`）与 `sap.goodsap.cn:44300`（Web GUI）均 `OPEN`。
结论：端口层可达。端口开放≠SAP RFC 监听，因此仍以 1.2 的真实协议握手为准。

## 1.2 真实 PyRFC 只读握手（网关自身代码 + 场景凭据）

用网关生产代码（非替身）建连并调用，全部只读：

```
pyrfc_installed = True
resolve_backend('auto', rfc) = pyrfc
RFC_PING backend = pyrfc
read_table backend = pyrfc   rows = [{'BUKRS': '0001'}, {'BUKRS': '0003'}, {'BUKRS': '0005'}]
read_table has column_meta = False
```

单步计时（同一进程，复用句柄）：

```
 2.55s  Connection() open
 2.61s  RFC_PING
 3.13s  RFC_READ_TABLE      (T001 / BUKRS)
 3.17s  conn.close()
 3.17s  done
```

结论：**真实 RFC 登录与只读 RFC/BAPI 调用成立**，单次调用在 0.1–0.6s 量级，远低于场景 worker 的 30s 读窗口。

### 附带观察：进程退出

`done` 打印于 3.17s，但进程直到看门狗（120s）才结束——卡的是**解释器退出**，不是调用，也不是 `conn.close()`（0.01s 返回）。同一现象在只用一个连接的脚本上重复出现。

这是 NW RFC SDK 的既有特性，与本变更无关：网关是长期驻留进程，只在停机时受影响；场景 worker 不导入 `pyrfc`（它通过 HTTP 调网关），故每次调用不受影响。如实记录，作为「其它受影响面」的说明，不据此声称任何失败。

## 7.3 真实会话端到端（经运行中的网关）

对 `127.0.0.1:8200/mcp`（LISTEN，进程 `12136`）发起真实 MCP 会话，参数与场景将下发的一致（`backend=auto` + `ashost`/`sysnr`）：

| 调用 | 结果 |
|---|---|
| `sap_connect(...)` | `connected: True`；网关自身 ping `{'backend': 'pyrfc'}`；`rfc` 段回显 `ashost=sap.goodsap.cn, sysnr=00, client=200, user=S2385`；同时含 `adt` 段 |
| `sap_whoami` | `rfc` 段同上 → 决策 5 的 `ashost`/`sysnr` 核对可通过 |
| `call_rfc(RFC_PING)` | `{"function": "RFC_PING", "backend": "pyrfc", "result": {}}` |
| `sap_disconnect` | `{'disconnected': True, ...}` |

结论：**7.3 达成**——配好 RFC 参数的真实会话可执行只读 `call_rfc` 并到达 PyRFC 后端。回报仍只是传输结果，不据此声称任何业务已完成。

## 7.1 的关键发现：运行中的网关早于本次网关改动

同一次会话里的 `read_table`：

```
read_table backend = pyrfc | has column_meta = False
rows = [{'BUKRS': '0001'}, {'BUKRS': '0003'}, {'BUKRS': '0005'}]
```

即：**正在运行的网关进程仍是旧逻辑**（早于提交 `1fd2cc6aa`），在 `backend=auto` 下把 `read_table` 解析到 PyRFC，返回形状缺少 `column_meta`。这现场复现了本次要修的缺陷，同时给出发布顺序要求：

> **网关必须先发布/重启，场景再上线。** 顺序颠倒时，场景新下发的 `backend=auto` 会撞上旧网关，`read_table` 的对外形状随即变化（`column_meta` 消失）。

以改动后的网关代码对同一组参数直接求值（离线，`pyrfc_installed()` 为真）：

```
both targets configured; registry backend would be: pyrfc
call_rfc resolves to: pyrfc -> RFC_PING via pyrfc: True
read_table backend = adt rows = [{'BUKRS': '0001'}, ...]
read_table has column_meta = True
```

即改动后：`call_rfc` → PyRFC，`read_table` → ADT 且 `column_meta` 存在。7.1 的要求在**改动后的代码**上成立；在**运行中的旧进程**上不成立，直到发布。

## 现场痕迹（顺带记录）

`rsmCode/dev_rfc.log`（未跟踪，958 字节）：NW RFC SDK `Release 750 Patch Level 18` 的 trace（level 0），记录了真实 RFC 客户端在 `C:\rdai\rsmCode` 下启动的时间与主机信息。文件内**不含口令或凭据**，可作为「确实发生过经 PyRFC 的调用」的旁证。非本次改动产物，未删除。

## 未验证（如实记录）

- **写入类 BAPI 未触发**：全程只读（`RFC_PING`、`RFC_READ_TABLE`），未调用任何会改变业务数据的 BAPI。
- **经场景介导通道（`sap_data_call`）的会话级路径未跑**：本节走的是网关 HTTP/MCP 直连，未经过浏览器内的场景会话与插件桥；场景侧断言由隔离测试覆盖（`test_sap_workbench_mcp_login.py`、`test_sap_workbench.py`）。
- **`purchase_order.py` 读取路径未重放**：该读取器只在默认关闭的旧引擎路径可达，本轮未触发；`read_table` 形状未变这一结论来自「解析结果仍为 ADT」而非该读取器的实跑。
