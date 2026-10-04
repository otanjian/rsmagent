# MCP 响应与 DOM 回读收尾

2026-10-04。本轮修复当前已开放消费者的 MCP 响应格式、worker 管道容量和页面目标回读，不新增工具、授权方案或任务勾选。完整计划保持 **42/57，剩余 15 项**；实际 SAP/OpenCode 操作检查继续按用户安排暂缓，提交适配器仍未注册，没有自动保存、过账或删除。

上一轮采购订单投影、测试和服务快照保留在 [采购订单只读收尾](purchase-order-read-closeout.md) 与 [该轮服务重载](purchase-order-services-reloaded.json)。本记录不把其历史现场或隔离结果当作这轮补丁的真实 SAP 重测。

## MCP 响应格式与传输边界

`backend/mcp_data.py` 支持实际 MCP 1.29 FastMCP 对返回类型为 `str` 的函数所生成的 `structuredContent={"result": JSONstring}`：仅接受这个单键、字符串类型包装，完整解码一次后校验业务 JSON；原有直接业务对象仍可读取。文本和 structured 两者都存在时，完整解码后的规范 JSON 必须一致；布尔值与数字、不同内容、额外包装键、错误标志、重复 JSON 键和不完整结果均不能成为成功数据。

业务 JSON 的 **48,000 UTF-8 字节**、深度 **16**、节点数 **10,000**、敏感字段与私有连接标识限制不变；超限或含敏感数据时拒绝，不截断、不脱敏后冒充完整结果。包装的 JSON 转义与包装层深度不缩小已登记的内部业务数据预算。实际安装的 MCP SDK 在 worker Python **3.10** 中已通过离线转换合同检查，只构造固定 SDK 返回值，没有连接或启动 SAP/MCP 服务。

`backend/mcp_runtime.py` 为自有 worker 的 stdout `StreamReader` 显式设置 **262,144 字节（256 KiB）**上限，修复合法的 32,000 字符文本因多字节 UTF-8 或 JSON 转义超过默认管道容量而提前失败的问题。超过管道上限仍拒绝并清理该 consumer。这个传输上限不放宽私有业务 JSON 的 48,000 字节上限，也不改变订单模型投影超过 **32,000 UTF-8 字节即拒绝**的既有合同；字符预算和字节预算分别执行。

## 页面目标与 F4 归属

`browser_service/dom.py` 在每次观察的文档中清除场景自有的旧 field/control/table marker，再为本次可见对象登记目标。隐藏、替换或跨 frame 的旧对象不能依靠旧 marker 与复用的序号成为本次输入目标。`GRID_READY` 与 `FIELD_WRITE` 在派发或写入前重新核对编辑器、cell 与所属 grid 的可见、只读和禁用状态，状态变化时停止。

`browser_service/page.py` 的 F4 lease 保留有界的完整原字段语义，包括标签、类型、输入类型、可编辑/命令标志，以及适用的行列、选项和字段拒绝状态；仅保留旧字段 ID 的历史 lease 不能重建为有效归属。lease 同时绑定控制代次、页面 title、已登记 profile 和唯一帮助弹窗。选值后须仍为原页面和字段语义，原字段精确读回显式 option.value、弹窗关闭，且不存在显式或未知 `aria-invalid` 拒绝状态；连续两次稳定观测才接受页面效果。

这些检查只证明已登记目标的页面结果，不证明 SAP 完整业务校验或持久化成功。未知帮助、搜索/页签及完整分页的执行范围没有扩大；更严格的真实 SAP 控件兼容尚未重测。采购订单只读投影仍保留 `business_validated=false`、`complete_business_document=false`、`submission_authority=false`，私有 `.compare` 不接提交消费者。

## 实际验证

| 范围 | 实际结果 | 边界 |
| --- | --- | --- |
| MCP SDK 包装合同新增测试 | 47 项；包含实际安装 SDK / worker Python 3.10 离线转换 | 固定业务对象，无 SAP/MCP 请求；SDK 合同测试本轮实际运行，没有跳过 |
| worker 管道新增测试 | 6 项 | 真实有界 StreamReader，worker 与 SAP 为替身；保留私有数据上限及超限清理 |
| F4 回读新增测试 | 6 项 Python | 替换字段/页面、旧 lease、显式拒绝及两次稳定回读，节点与页面为替身 |
| DOM marker 与状态新增测试 | 5 项 Node | 固定 DOM、嵌套文档及编辑器/cell/grid 状态，无浏览器或 SAP 操作 |
| 完整 SAP Python | **1182 passed / 2 skipped，92.49 秒** | 仅真实 Chrome 和显式启用的固定上游下载 fixture 跳过；专项包含于全套，不累加 |
| 相关 Node 六文件 | **170 passed / 170，204.801875 ms** | 固定 DOM/前端合同，没有新的现场 UI 验收 |
| Bun / TypeScript | 本轮未修改相关源码、未重跑 | 上一轮 Bun **4 passed / 81 assertions，1.99 秒**记录保留在 [采购订单只读收尾](purchase-order-read-closeout.md)，不写成本轮执行结果 |

完整回归命令：

```sh
SAP_WORKBENCH_LIVE=0 SAP_WORKBENCH_UPSTREAM=0 SAP_MCP_PYTHON=/path/to/sap-connect/sap-pyrfc/.venv/bin/python \
  .venv/bin/python -B -m pytest -q -rs tests/test_sap_workbench*.py
node --test tests/test_sap_workbench_field_validation.cjs tests/test_sap_workbench_observations.cjs tests/test_sap_workbench_dom.cjs tests/test_sap_workbench_frontend.cjs tests/test_scenes_frontend.cjs tests/test_coding_frontend.cjs
```

新增 Python 用例位于 `tests/test_sap_workbench_mcp_sdk_wrapper.py`、`tests/test_sap_workbench_mcp_pipe.py`、`tests/test_sap_workbench_help_readback.py`；新增 DOM 用例位于 `tests/test_sap_workbench_dom.cjs`。MCP SDK 检查使用 `SAP_MCP_PYTHON` 指定的实际 worker 解释器，默认位置为相邻 `rsmcode/sap-connect/sap-pyrfc/.venv/bin/python`；解释器缺失不能视为 SDK 合同通过。

## 运行与部署状态

本轮源码冻结后，桌面后端 **9876 的 PID 66363 → 68018**，由原 Electron **29265** 自动恢复；Web 后端 **9899 的 PID 66404 → 68062**，沿原命令重新加载。两者完整原环境一致，原数据根和主密钥保持，重载前无活动工作台 runtime。**18 项**公共 health/chat、SAP JS/CSS、shared runtime 及匿名 config/session API 检查全部通过，**15 份**相关源码 hash 在重载后未变，见 [本轮服务重载快照](mcp-dom-services-reloaded.json)。未复制账号、凭据、token 或数据库，未创建工作台绑定，也没有新的 Chrome UI 或 SAP/OpenCode 页面操作验收。

独立 Linux 显示镜像路径本轮只读请求官方 `mirror.gcr.io` 的锁定 Debian 双架构 manifest。配置网络和 direct 的 arm64 HTTPS 请求仍为 `SSLEOFError`，没有 HTTP 响应、镜像下载、VM 启动或 daemon 配置改变；Colima 保持停止，context 为 `default`。`deployment/browser-versions.json` 的 `runtime_verified` / `workbench_binding_verified` 仍为 `false`。不宣称镜像已在本机或独立部署完成，4.3/4.4/4.7 的真实运行验收保留。

本轮没有真实 SAP、MCP、模型或 OpenCode 页面操作；替身测试和公共后端资源检查不替代 5.7/5.8、6.3/6.6 或桌面登录后双栏验收。统一登录及生产权限扩展仍按已确认范围延后，change 不归档。
