# 本机独立收尾与最新代码加载

> 本文为上一收尾阶段的原始 819 pass / 40/57 记录。后续已完成 3.2、窄范围 F4 修复及再次原环境重载；当前为 859 pass / 1 skip、41/57、剩余 16 项，见 [后续收尾](incremental-closeout.md)。本文旧截图、进程号和源摘要保留为历史，不覆盖当前运行记录。

2026-10-04。本次按用户安排继续完成可独立实施的工作，暂缓 OpenCode 与 SAP Web GUI 实际操作检查。MCP 使用场景配置账号，Web GUI 独立登录；普通执行器、公共 IAM、桌面主进程和 OpenCode 核心未改。

本次独立修复、源码回归、后端重载和入口核对已完成。**完整 change 仍为 40/57，剩余 17 项**，不是完整 G0–G4 或桌面/SAP 现场验收通过。实质实现缺口与延后验收逐项保留在 [范围复核](scope-closeout-review.md)，change 不归档。

## 本次修复与回归

| 问题 | 修复结果 | 新增用例及范围 |
| --- | --- | --- |
| 无效 sessions action/binding_id/control 可导致 500 | 资源读取、预留和派发前返回 400，正常 owner 请求不变 | 19 项；真实临时平台 API，零副作用；[范围复核](scope-closeout-review.md) |
| 多租户可合计超过浏览器总容量 | 服务端总限额默认 4，环境项限定 1..32；pending/live/releasing 去重计数；重连不重复占额 | 31 项；受控 node/事件，不是实际 Chrome 性能测量；[负载准备](load-readiness.md) |
| 后端退出、组件异常和迟到 spawn 可遗留子进程 | 首次成功启动才注册退出钩子；并行清理、异常隔离、既有每节点 worker 与精确自有句柄兜底，保留 paused/unknown 和历史 | 22 项；真实临时 Python 子进程，不是实际 Chrome/Bun/SAP 操作；[退出清理](shutdown-cleanup.md) |

```sh
SAP_WORKBENCH_LIVE=0 SAP_MCP_PYTHON=/Users/jiantan/ai_assistant/rsmcode/sap-connect/sap-pyrfc/.venv/bin/python \
  .venv/bin/python -B -m pytest -q -rs tests/test_sap_workbench*.py
```

最终 **819 passed、1 skipped，82.38 秒**。跳过的是需 `SAP_WORKBENCH_LIVE=1` 的真实 Chrome fixture；未通过它操作浏览器或绕过证书。72 个新增用例均包含在最终套件内，各专项组合有重叠，不另相加。Node **153 pass**（含 70 项固定 DOM）与 canonical host/双 host 隔离 **3 pass / 68 assertions** 沿用 [上一继续阶段](resumed-delivery.md) 的结果，对应前端/host 源码本轮未改、未重复执行。历史非 SAP 资源摘要失败保留原证据，不扩大全库通过声明。

浏览器上限分别属于 Web 和桌面后端，不是整机共享配额。55 秒为场景 stop 的共享等待预算，不涵盖其他退出钩子、阻塞 OS I/O、SIGKILL 或掉电；已采用旧 profile 的 Chrome 仅经原 launcher 正常 close，没有 PID 扫描兜底。

## 原启动环境重载

先在 Chrome 正常点击“结束工作台会话”，释放活动 Chrome/host，将绑定置为 paused/manual，保留配置和原生历史；不是删除 SAP 数据或聊天历史。随后重载：

| 服务 | 原 PID → 新 PID | 核验 |
| --- | --- | --- |
| 桌面后端 9876 | 29487 → 58386 | 一次 SIGTERM 后由原 Electron 29265 的既有恢复路径重启，仍归原启动器 29262 管理 |
| Web 后端 9899 | 54933 → 58422 | 继承原命令及完整环境正常重启 |

完成于 **00:41:47 +08:00**。数据根、租户根、主密钥及场景运行设置只在私有内存比较，均相等；未输出环境值或凭据指纹、未生成新密钥、复制身份数据库/cookie/token或重置密码。桌面根仍为 `/Users/jiantan/.cow`，Web 根仍为项目目录。桌面前端 5173、OpenCode API 4096/Web 3000、MCP 8100/8200 原监听进程保留，未重启或对它们发起业务联调。

**00:44:50** 两后端的健康、公共页面、SAP JS/CSS、共享 runtime 和未登录拒绝共 18 项检查通过。SAP 资产与源码逐字节一致；共享 runtime 为 644251 字节、SHA-256 `aa21fdb56b2296b339795be05ec13ee381386b11b0f7be159356bac3afeba0e9`。配置/sessions 无租户为 400/missing_tenant，仅租户 header 为 401/unauthorized。安全快照及源文件摘要见 [local-services-reloaded.json](local-services-reloaded.json)，不含认证材料。

## 入口结果与剩余范围

通过 Google Chrome 刷新 [Web 入口](http://localhost:9899/chat)，原 admin/默认租户登录保持。点击“场景应用 → SAP 工作台”可打开；准备页的 SAP、OpenCode、项目、MCP/账号密码及浏览器节点均为“已填写”，新建/恢复按钮可用。没有点击新建/恢复、启动新的工作台 runtime、调用模型、操作 SAP 或保存业务。“未联调”表示没有本轮检测结果，不以旧结果改成当前通过。

![重载后的 SAP 工作台准备页](local-closeout-ready.jpg)

桌面原窗口在恢复后显示正常“在浏览器中登录”入口，没有启动失败页；正常登录/首次改密后的双栏仍未验收。旧 SAP 证书页、真实只读 MCP/ADT 和历史恢复均保留为 [上一阶段](resumed-delivery.md) 证据，本次未重测。

17 项分为后续实现/部署 **7 项**（2.9、3.2、3.11、4.1、5.3、5.5、6.3）、现场/负载验收 **8 项**（1.5、4.3、4.4、4.6、5.7、5.8、6.6、7.2）及阶段汇总 **2 项**（1.7、4.7）。完整条件见 [范围复核](scope-closeout-review.md)，不能概括成“全部写完仅待测”。当前 `submission_adapter=None`，保存/过账/删除无工具入口，G3 关闭；审批替身成功不算真实 SAP 提交成功。

新增程序仅限 SAP 场景目录和三份 SAP 专项测试，其他工作区差异保留，见 [改动边界](change-boundary.md)。源码未提交、推送或归档。

最终 OpenSpec strict、Git diff 空白检查、118 个场景/change/专项文本文件、75 个本文档集的本地链接及 3 项 SAP 资产摘要均通过。`agent/auth/bridge/scenes/desktop` 没有已跟踪代码差异，参考 `rsmcode/opencode` 工作区干净。40 个已完成和 17 个未完成编号与任务清单相符；没有新增任务勾选。
