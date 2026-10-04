# 原合同复核与本轮独立收尾

2026-10-04。当前完整计划为 **42/57，剩余 15 项**；本轮仅新增任务 4.1 的完成勾选。依据是其原合同要求的独立部署组件、版本固定及主服务启动解耦已经提供，没有删除或降低后续真实验收要求。逐项剩余条件见 [范围复核](scope-closeout-review.md)，阶段证据见 [验收矩阵](acceptance-matrix.md)。

用户安排保持不变：SAP/OpenCode 实际操作检查继续暂缓；MCP 使用固定地址与 `scene_config` 加密配置凭据，Web GUI 独立登录；仅指定测试 SAP 的后台连接适用定向 TLS 例外。本轮不扩建权限、工具目录、模型映射或统一登录。运行时没有注册完整 SAP 提交适配器与业务核验器，自动保存/过账仍关闭，change 不归档。

## 4.1 按原组件提供合同完成

- `Scene/sap_workbench/deployment/browser.Dockerfile` 与 `browser-compose.yaml` 提供有头 Chrome、独立 Xvfb 显示、x11vnc、WebSocket 桥和 noVNC；初始页面为空白，不自动登录或访问 SAP。
- `browser-versions.json` 固定双架构基础镜像摘要、APT snapshot、浏览器及显示组件版本与下载完整性参数；构建程序据锁校验，不改系统 Chrome。
- `entrypoint.py` / `healthcheck.py` 提供认证文件检查、真实 RFB/WebSocket 准备检查、组件监督与有界进程组回收。非 root、沙箱、独立 profile、loopback 端口与资源限制均由单元自己的部署文件提供。
- 单元是可选部署；主服务默认启动不依赖容器或浏览器在线。本机工作台继续使用原 CDP 画面/输入路径。

原部署专项及上游校验有记录，详见 [Linux 部署说明](../../../../../Scene/sap_workbench/deployment/BROWSER.md) 和 [实际构建重试](linux-display-retry.md)。实际基础镜像 HEAD 请求仍有 EOF 失败；没有成功构建/启动镜像，也未通过容器沙箱、VNC 认证/画面/输入或远端工作台验收。`runtime_verified` 和 `workbench_binding_verified` 均保持 `false`，远程显示没有登记为可用执行节点。4.3/4.4/4.7 与完整 G1 不因组件交付而完成。

## 其余合同的准确归因

| 任务 | 已有证据与仍保留的条件 |
| --- | --- |
| 1.5 | SAP 版本/语言、交易样例和本机组件元数据已有记录；目标主题、SSO/重定向域仍未核实，历史值或来源漂移不能填造当前兼容。原合同不要求证明所有主题、内核 patch、远程显示或自动冻结浏览器更新。 |
| 1.7 | 源码边界、外置 host、真实原生工具与 MCP 路径有分项证据；汇总不等于完整 G0 通过，目标兼容与 SAP/SSO 前置仍保留。生产权限扩建及远程负载不追加为本项门槛。 |
| 4.7 | 本机真实双栏/人工操作和授权、凭据、恢复、配额专项已记录；完整 G1 仍缺真实多用户、SSO/控件、负载与容器/远端运行条件，关闭策略不能替代实测。 |
| 5.5 | 模型/工具硬配额、审计和逐次平台/coding 复验已有消费路径。非提交工具和模型未接独立资源动作判断，完整生产消费验收仍延后；没有新目录不构成缺口，也不为本轮追加权限方案。 |

其他统一登录、完整分页/有效单据、MCP 真实双用户生命周期、SAP 提交及桌面登录后双栏条件，继续保留在任务清单中。本轮不把独立实现或汇总文档替代这些现场验收。

## 本轮验证与运行记录

提交准备的快照守卫已拒绝超长字段/表格局部、超限或无效行列及截断值，避免两个不同页面值因截为同一前缀而形成相同审批摘要。新增 **13 项定向测试通过，6.81 秒**，使用隔离观察和真实临时 IAM，未调用 SAP 保存。详见 [提交快照边界](commit-snapshot-bounds.md)；这不完成 6.3，也不启用提交。

显示监督器已在停止请求后拒绝补启组件，并在 readiness 返回成功后复验停止状态。固定真实上游 noVNC/websockify 的小型资源及私有 loopback RFB/WebSocket 双向 fixture 专项见 [显示组件源码与桥接核验](linux-display-source-audit.md)；该阶段显式组合为 48 pass，默认网络专项跳过，其传输证据不代替 Linux Chrome/Xvfb、容器沙箱、VNC 认证/像素/键鼠或平台绑定验收。

只读 DOM 观察新增有界页签/面板关联和表格 ARIA 总数/索引来源，重复或冲突引用不形成可信关联，未知总数与索引基数不自行推导；观察截断继续进入模型投影的 `omitted` 提示。页签自动执行、完整分页和完整业务数据均保持关闭，5.3 不勾选完成。实现、官方来源和定向隔离结果见 [页签与表格只读观察](tabular-observation-contract.md)，本轮没有新的现场控件证据。

首轮完整 SAP 套件为 **1 failed / 889 passed / 2 skipped，91.84 秒**：macOS 已登记的 orphan process group 在退出窗口收到 SIGKILL EPERM，此前定向通过不能覆盖这次失败。只读重现仅操作自己创建的临时 PID/组；没有将 EPERM 直接解释为已释放，也没有把未观察到的 zombie 机制或 Linux 行为写成事实。

修复只对已登记组有界重试；后续实际信号成功或 ESRCH 才结束，持续错误明确失败。所有组件仍继续尝试 TERM/等待/KILL/等待，再汇总固定错误；测试也等待精确组实际 ESRCH。最终部署定向 **52 pass / 1 skip**，真实临时进程组文件独立重复十次、每次 **6 pass**。详细重现、预算与失败语义见 [进程组退出窗口修复](linux-display-cleanup-eperm.md)。这些专项相互重叠且包含在全套中，不累计为新增通过总数。

源码冻结后的第二轮完整 SAP 套件为 **895 passed / 2 skipped，91.57 秒**；跳过项分别为用户暂停的真实 Chrome fixture 和默认未启用的固定上游资源 smoke。相关 Node **161 passed**；当前 canonical host 的 Bun 隔离测试 **3 passed / 0 failed / 68 assertions，2.11 秒**，供应商与 SAP 桥为受控 fixture。没有把这些测试当作真实 SAP/OpenCode 操作、容器运行或提交验收。

## 本轮原环境重载与公共资源检查

重载前没有活动工作台。桌面后端 `9876` 沿原 Electron `29265` 的自动恢复生命周期由 `61515` 更新为 `64390`；Web `9899` 沿原命令及完整环境由 `61556` 更新为 `64430`。两份完整原环境相等，主密钥与数据根保持；没有复制登录/token/数据库、重置密码或新建密钥。重载期间八份相关源码摘要不变，结果见 [本轮重载快照](next-contract-services-reloaded.json)。

共 **18 项**只读检查通过：两个后端的 health/chat、SAP JS/CSS、共享运行资源，以及不带登录的配置/会话 API 拒绝状态。静态文件与对应源码/原构建一致。该结果只确认原后端已加载及公共资源边界，没有创建新工作台 runtime、浏览器或模型请求，没有新的 Chrome UI、登录后双栏、SAP/OpenCode 操作或业务提交验收。

最终 OpenSpec strict 与 Git 差异空白检查通过。场景/change 和 SAP 专项共 135 份文本完成语法/格式核对，178 个本地文档/截图引用、3 个 SAP 资产清单摘要及剩余 15 项与范围表一致性通过。重载后八份源码保持不变；普通 Agent、IAM、bridge、旧 scenes 与桌面核心无已跟踪差异，两个 rsmcode 源码工作树仍干净。结果保存在本轮重载快照的 `final_validation`。源码重载、公共 HTTP 成功及测试通过仍不替代尚未完成的现场条件。

此前 **819** 与 **859** 的 Python 回归、两个后端重载、Chrome 入口与配置显示分别保留在 [上一本机收尾](local-closeout.md) 和 [上一后续收尾](incremental-closeout.md)。旧 PID、监听、截图与真实 SAP/OpenCode/MCP 记录只证明各自轮次，不代表本轮新源码已经加载、重测或重新进行现场操作。
