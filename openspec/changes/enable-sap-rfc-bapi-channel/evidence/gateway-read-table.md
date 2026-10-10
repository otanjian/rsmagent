# 证据：网关侧 `read_table` 的后端选择

日期：2026-10-10。对应 `tasks.md` 第 2 组。

仓库：`rsmCode`（独立仓库，origin `http://127.0.0.1:3000/tanjian/rsmCode.git`），分支 `dev`。
提交：**`1fd2cc6aa` — fix(sap-pyrfc): read tables through ADT whenever it is reachable**（3 files changed, 88 insertions, 1 deletion）。

## 改动

| 文件 | 改动 |
|---|---|
| `sap-connect/sap-pyrfc/sap_pyrfc_mcp/connection.py` | `read_table` 的后端解析由 `resolve_backend(preferred, …)` 改为 `resolve_backend("adt", …)`，并加注释说明为何忽略 `preferred` |
| `sap-connect/sap-pyrfc/tests/test_read_table_backend.py` | 新增（2 个用例） |
| `sap-connect/sap-pyrfc/README.md` | 在「Tool availability by backend」表后补一段选择规则说明 |

理由：`read_table` 原先按连接自身的 `preferred` 后端解析。连接同时具备 ADT 与 RFC 目标时会解析到 PyRFC，从而改用 `RFC_READ_TABLE`——该路径不返回 `column_meta`，且把每行折进一个长度受限的 `|` 拼接 `WA` 字符串，与 ADT datapreview 的返回形状不同；而 `run_query` 是硬编码 ADT 的，同一条连接的两个读工具于是各走一套后端。改法与 `run_query` 现有的写法一致（`run_query` 同样带 `preferred` 参数但忽略它）。

## 改动前后的解析对照（离线实测）

用网关自身代码逐格求解，`pyrfc_installed()` 置真：

| 目标组合 | 注册后端 | 改动前 | 改动后 | 变化 |
|---|---|---|---|---|
| 仅 ADT | `adt` | adt | adt | |
| 仅 ADT | `auto` | adt | adt | |
| 仅 ADT | `pyrfc` | adt | adt | |
| 仅 RFC | `adt` | pyrfc | pyrfc | |
| 仅 RFC | `auto` | pyrfc | pyrfc | |
| 仅 RFC | `pyrfc` | pyrfc | pyrfc | |
| 两者都有 | `adt` | adt | adt | |
| 两者都有 | `auto` | pyrfc | **adt** | **YES** |
| 两者都有 | `pyrfc` | pyrfc | **adt** | **YES** |

结论：**只有「两者都有且注册后端为 `auto`/`pyrfc`」两格改变**，正是原先解析到 `RFC_READ_TABLE` 的两格。纯 ADT 场景（当前部署的形态）与纯 RFC 场景逐格不变，符合「对当前纯 ADT 场景是 no-op」。

## 测试

```
sap-connect/sap-pyrfc $ .venv/Scripts/python.exe -B -m unittest discover -s tests -p "test_*.py" -v
Ran 7 tests in 0.148s
OK
```

新增两例：两者都有时走 ADT（断言 `AdtClient.read_table` 被调用一次、`call_rfc` 未被调用）；仅 RFC 时回退 PyRFC（断言 `call_rfc` 首参为 `RFC_READ_TABLE`，并按 `FIELDS`/`DATA` 解析出行）。

另做导入冒烟：`sap_pyrfc_mcp.server` 可导入，工具面仍为 `read_table` / `run_query` / `call_rfc` / `get_rfc_function_description` 等，未增减。

## 尚未验证（如实记录）

- `tasks.md` 2.3 的「重放现有取数路径回归」**未完成**：网关仓库没有 `read_table` 的既有回归可重放，真实取数重放需要在线 SAP。离线侧以上表对照与单元测试替代，属证据强度较低的一档，**不**据此声称现场行为已确认。
- 现场取数（无论 ADT 还是 RFC）均未在本轮触发；RFC 端口的可达性仍属 `tasks.md` 第 1 组的前置，未验证。

## 供场景侧引用

场景侧的兼容基线与发布锁应引用提交 **`1fd2cc6aa`**（`rsmCode` 的 `dev` 分支）。第 3–5 组实现时，若场景下发 RFC 参数而部署的网关仍早于该提交，会出现「场景已传 RFC、网关仍按 `preferred` 解析」的错配。
