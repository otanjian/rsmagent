# 浏览器故障与负载验收准备（4.6）

2026-10-04。本次先做只读评估及本机服务资源单点观察，随后修复独立的跨租户节点容量缺口；**任务 4.6 仍未完成**。用户暂缓 OpenCode 与 SAP Web GUI 的实际操作检查，本轮没有恢复这些检查，没有处理浏览器证书警告、调用模型或执行 SAP/MCP 工具。

没有加载运行中的 SAP 场景配置、修改真实配置、分配真实浏览器租约、启动 Chrome/新控制节点/HTTP 服务，亦未借用已有 SAP Chrome。只读评估阶段仅新增本证据文件；后续源码及资源正确性专项见下文。没有新增模拟负载脚本或 FixtureNode 性能测量；模拟吞吐与内存不能替代需要的真实浏览器负载验收。

## 已有故障覆盖及其范围

以下为初始只读阶段的源码与既有测试核对。当时没有重跑这些测试，最近完整套件结果沿用 [继续交付记录](resumed-delivery.md) / [验收映射](acceptance-matrix.md) 所记的 747 Python pass / 1 skip；不能把总体通过数变成 4.6 的现场测量结果。后续补丁的定向重跑另列，重复覆盖的测试不能再相加为唯一测试总数。

| 故障/边界 | 可核对的现有来源 | 已证明与尚缺的范围 |
| --- | --- | --- |
| 页面慢跳转、协议连接丢失 | [test_sap_workbench_browser.py](../../../../../tests/test_sap_workbench_browser.py)：`test_slow_navigation_preserves_browser_but_protocol_failure_does_not`、`test_chrome_exit_wakes_a_waiting_frame_stream` | 注入导航超时/关闭及异步 Socket 替身验证保留节点或唤醒帧消费者；未真实终止 Chrome 或断开它的 CDP 网络 |
| 画面握手丢失、调用取消 | 同文件 `test_lost_screen_handshake_detaches_allocated_browser_for_retry`；[test_sap_workbench_resource_cleanup.py](../../../../../tests/test_sap_workbench_resource_cleanup.py)：`test_cancelled_screen_handler_stops_all_owned_tasks_before_detach` | prepare 异常/取消、泵任务结束及 detach 的隔离证据；不是丢包、网卡断开或中间代理中断实测 |
| 断开重连、空闲回收 | [test_sap_workbench_manager.py](../../../../../tests/test_sap_workbench_manager.py)：`test_second_pane_cannot_replace_live_node_and_reconnect_preserves_node`、`test_detached_node_is_reclaimed_after_grace` | 第二画面拒绝、短期断开复用同节点、宽限期后关闭的替身验证；没有两个真实 Chrome 登录态或实际网络断线 |
| 执行节点不可用/未注册 | [test_sap_workbench_environment.py](../../../../../tests/test_sap_workbench_environment.py)：`test_unavailable_runtime_fails_before_allocation`、`test_unregistered_node_never_falls_back_to_local`；browser 测试的 `test_gateway_reports_an_unavailable_runtime_instead_of_upgrading` | 本地路径/节点引用准备检查和 factory 失败的 503；当前只有登记本机节点，未证明真实远程执行节点不可达时的恢复 |
| 容量耗尽、正在启动/关闭的占位 | manager 的 `test_pending_start_counts_toward_capacity_and_keeps_tenants_independent`、`test_closing_browser_keeps_capacity_and_cleanup_survives_caller_cancellation`；[test_sap_workbench_runtime.py](../../../../../tests/test_sap_workbench_runtime.py)：`test_capacity_reserves_pending_hosts_before_allocation` | 原有证据证明预留先于 await、关闭完成前仍计租户容量、取消不打断回收、不同租户计数独立；跨租户 manager 总上限缺口现已补齐，见下文。没有真实并发 Chrome 的 CPU/RAM 或输入延迟测量 |
| 部分启动失败与资源丢失 | [test_sap_workbench_lifecycle.py](../../../../../tests/test_sap_workbench_lifecycle.py)：`test_startup_timeout_releases_capacity_and_retry_uses_same_conversation`、`test_partial_browser_failure_keeps_conversation_and_retries_only_browser`、`test_monitor_reclaims_resources_and_marks_interrupted_input_unknown`；resource cleanup 的 executor 所有权测试 | 替身 host/浏览器、真实临时场景存储验证超时、空闲、撤权、退出及 unknown 不重放；没有现场断电或运行中真实浏览器进程终止 |
| 跨会话/租户与撤权 | [test_sap_workbench_access.py](../../../../../tests/test_sap_workbench_access.py)：`test_session_list_resume_and_close_are_owner_scoped`、`test_native_web_gateway_rechecks_real_identity_and_isolates_two_users`；runtime 的 `test_gateway_cookie_origin_and_private_bridge_auth`；browser 的 token/origin 测试 | 真实临时 IAM/场景数据库和 loopback HTTP/WS 验证 owner、cookie、origin、一次性 grant、外部会话路径及撤权；此前两个真实私有 OpenCode host 的隔离另有记录，均不能替代双真实 SAP 用户画面/动作隔离 |

历史现场记录有单用户浏览器画面/输入及 SAP 网络错误后恢复的观察，见 [现场工作台](live-workbench.md)；它们没有代表性并发、受控故障注入或延迟分位数，本轮不扩大其结论。`test_sap_workbench_browser_live.py` 的 opt-in Chrome 输入/画面检查本轮仍未执行。

## 真实节点与协议边界

- [node.py](../../../../../Scene/sap_workbench/browser_service/node.py) 为一节点一专属 Chrome，通过同一 page target 的 CDP 取得 JPEG 帧和输入通道；这与 [Linux noVNC 单元](../../../../../Scene/sap_workbench/deployment/BROWSER.md) 的 Xvfb/RFB/WebSocket 不同，不能把本机 CDP 结果冒充 VNC 负载或远程节点结果。
- [runner.py](../../../../../Scene/sap_workbench/browser_service/runner.py) 将画面 aiohttp 网关放在独立线程/event loop、绑定临时 loopback 端口；主 WSGI 服务不负责 WebSocket 升级。监听服务 CPU/RSS 不能等同 Chrome renderer/GPU 整棵进程树资源。
- [manager.py](../../../../../Scene/sap_workbench/browser_service/manager.py) 的节点键为 `(tenant,user)`，profile 路径由该键派生；pending/closing 计入容量。`max_screens` 是租户计数边界；本次新增独立的 manager 总节点上限，缺省 4、允许部署环境设置 1..32。两者均为实现策略，**不是已测物理节点总容量**，多个租户的合计负载仍需测量。Web 与 desktop 的独立 manager 没有共享整机协调配额。
- `REATTACH_GRACE` 源码缺省 120 秒，截图缺省上限 1280×800 / JPEG quality 60，主 host 启动超时为 70 秒。本轮没有读取有效运行配置或环境覆盖，不能把源码缺省值声称为当前配置值或实测推荐值。

## 本轮控制工具不可用

先后只读调用受支持的浏览器入口 `cua.getState()`：首次 30 秒、第二次 10 秒均返回执行超时并重置 kernel，没有取得可用浏览器 surface/控制文档。未因此访问或操作 SAP 页面，也未通过 shell、直接 CDP 或 Runtime.evaluate 代替浏览器 UI 控制。

这些失败只说明本轮工具接口没有返回，不能推断 SAP 不可达、系统 Chrome 损坏或场景节点失败。没有建立可控制的独立 fixture profile，所以本轮没有实际 Chrome 并发、CPU/内存峰值、帧解码/渲染、输入响应、网络断线或真实跨画面测量。也没有新增替身基准来补出看似通过的数字。

## 已修复的跨租户节点容量缺口

初始评估发现 `max_screens` 只按租户计数：多个租户各有空额度时，可合计分配超过同一 manager 的预期节点数。此次仅修改场景内 `browser_service/manager.py`、`browser_service/runner.py`，新增 [test_sap_workbench_node_capacity.py](../../../../../tests/test_sap_workbench_node_capacity.py)，没有改通用执行器、配置表、客户端 claims 或其他场景。

- `SAP_WORKBENCH_NODE_MAX_SESSIONS` 是部署方环境项，缺省 4，接受 1..32 的整数。非法、空、零、负数、小数、布尔及过大值明确拒绝；runner 在线程启动、Chrome 路径和 profile root 解析之前验证，未启动节点。此值不是性能验收建议，也未修改任何运行中服务的真实环境。
- 容量按 `_live`、`_pending`、`_releasing` 的 `(tenant,user)` 并集去重，覆盖所有租户。创建前同步预留，starting 与 closing 仍占额度；达到总上限只拒绝新 slot，不驱逐已有用户。view claims 中的 `max_screens` 或额外同名参数不能抬高总上限；租户的既有上限继续独立生效。
- 短期断开重连复用同一节点；同 slot 的死节点重建先等待旧资源关闭再创建新节点，不重复占两个 slot。调用者在启动/关闭时被取消，包括重复取消，不能提前释放尚未清理的资源占位。
- 硬上限属于单个 manager／后端进程。不同 Web、desktop 或其他进程各自计数，不能据此声称整机 Chrome 总数受到同一个协调器限制。

定向执行：

```text
.venv/bin/python -B -m pytest -q tests/test_sap_workbench_node_capacity.py tests/test_sap_workbench_manager.py tests/test_sap_workbench_resource_cleanup.py
47 passed in 0.52s
```

其中容量新专项 31 项、既有 manager／资源回收 16 项。专项用受控 Node 和异步事件检查跨租户争用、pending/closing 预留、取消、重连、同 slot 替换、两种额度、进程边界及 env 验证；runner 接入用 AppRunner/TCPSite 替身，未监听真实 socket 或创建 Chrome。测试结束所有受控任务、短时 Python gateway 线程均已回收，临时目录未产生 browser profile。本次没有重复跑完整套件，没有访问 OpenCode/SAP/MCP，也没有测任何吞吐、资源峰值或响应延迟；**4.6 的真实负载验收仍未完成**。

## 无新增负载的资源单点

本机元数据只读取 `Info.plist` / `SystemVersion.plist` 和 `sysctl`：Chrome 154.0.8037.95、macOS 26.4、10 个逻辑 CPU、24 GiB 物理 RAM。没有执行 Chrome 来取得版本，没有查询配置或凭据。

2026-10-04 00:07:23 +08:00，通过 `lsof -t` 找出已获授权端口的监听 PID，再仅对这些 PID 执行 `ps -o pid=,%cpu=,rss=,etime=`。没有读取命令行、环境、业务正文或子进程 profile，亦未向服务发起 HTTP/WS 请求。

| 服务/端口 | PID | ps CPU % | RSS（KiB） | 进程已运行 |
| --- | ---: | ---: | ---: | --- |
| 桌面后端 9876 | 29487 | 0.0 | 37,088 | 03:28:23 |
| Web 后端 9899 | 54933 | 3.4 | 318,304 | 11:04 |
| 桌面前端 5173 | 60378 | 0.0 | 27,056 | 11:25:19 |
| OpenCode API 4096 | 40462 | 5.2 | 170,688 | 01:55:42 |
| OpenCode Web 3000 | 40466 | 0.0 | 62,000 | 01:55:42 |
| SAP ADT MCP 8100 | 89118 | 0.0 | 34,928 | 08:32:07 |
| SAP PyRFC MCP 8200 | 90085 | 0.1 | 41,840 | 08:31:19 |

这是**监听进程单点**，不保证进程或系统空闲，不包括 Chrome/GPU/renderer/完整子进程树。`ps` 的 CPU 比例不是某次页面交互消耗；RSS 单点不是并发峰值、泄漏或容量证明。没有测量 P50/P95/P99、cold start、帧率或超限负载，不根据这张表调大/调小容量、宽限期或超时值。

## 最小真实 fixture 容量验收流程

以下为待执行流程，本轮均未执行。先完成不依赖 SAP/OpenCode 的真实本机 fixture 测量；SAP 代表性业务负载仍在用户暂停范围，不能由 fixture 结果替代。

1. 恢复受支持的浏览器控制工具，并确认能够操作新建的独立临时 profile。为测试建立独立控制节点、临时租户/主体、profile 和 loopback fixture 页面；不得继承运行配置、SAP cookie、用户 Chrome 或生产租约，不修改证书验证或浏览器沙箱。
2. 记录实际浏览器版本、机器预算、截图参数、协议、有效容量和超时，以及同期背景负载。测试前写下项目的资源与响应准入指标，避免按结果倒推通过阈值；CDP、VNC 和远程节点各自验收。
3. 以 1、2、4 个真正独立 Chrome 节点分级启动，每级预热后做至少 3 分钟稳定样本并重复三轮；各页面包含可见输入框、按钮、长表格和画面变化。通过受支持 UI/正式测试入口交替人工输入、点击、滚动与 resize，保留同 target 的可见结果，测冷启动/首帧、输入到可见回读的 P50/P95/P99、帧率/丢帧和字节量。fixture 应固定并公开其内容；测量不得访问 SAP 或调用模型/MCP。
4. 采集每个自建 Chrome 的 browser/renderer/GPU 完整子进程树 CPU/RSS/峰值和网关开销；同时包括 pending/closing、慢消费者、短时间突发和至少两个临时租户的合计负载。源码租户上限不等于宿主上限；达到预设机器预算即停止扩压，不影响已运行用户服务。
5. 仅对自建节点执行浏览器退出、画面链路关闭、控制链路中断、执行节点不可达和 `N+1` 超限；检查存活识别、错误时间、旧 grant 拒绝、恢复同 target、隔离主体不能消费他人画面/控制、closing 仍占位、宽限期后回收，以及待核对动作不重放。真实网络注入需要独立可控链路，不能断开宿主或用户服务网络。
6. 结束后核对所有测试 Chrome/子进程/控制节点和 sockets 已结束、临时 profile/租约/令牌已回收，并确认用户服务未受影响。保存原始计时/资源序列与分位数计算方式，据最差可重复样本及预先写下的预算确定节点容量和超时；只更新测试控制节点配置。后续真实 SAP/远程验收需另行记录，不能据本机 fixture 把 4.6 全项勾选。

本轮没有创建真实浏览器、profile、租约、fixture 服务或基准脚本，所以无新增真实进程/端口/测试配置需要回收。初始只读阶段仅新增本证据文件；容量补丁及受控测试的清理见上述章节。此前监听服务均未停用或重启；`rsmcode/opencode` 仍无源码改动。
