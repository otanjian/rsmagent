# 继续交付与当前边界

2026-10-03，沿用户最新“继续直到完成、不选择方案”的安排推进剩余工作。此前独立收尾的 37/57 是历史状态；本轮完成 6.2、6.4、6.5 后为 **40/57，剩余 17 项**。MCP 使用场景配置账号、Web GUI 独立登录的既有选择保留，没有改为共享浏览器密码。

## 本轮实现

- 新增 `backend/commit.py`，消费现行正式审批、职责分离、硬配额和脱敏审计；审批绑定受信页面值、revision、浏览器 target、控制代次、配置/绑定代次和 SAP 目标。截断快照不能申请审批，审批消费前后再次复验权限、控制和参数。
- 场景动作表幂等增加页面/目标指纹、配置代次与安全回执；原身份、会话和公共审计表不变。提交失败后保留 unknown，不重放保存；只读核对须确认完整参数摘要与十位单据号。
- `runtime.py` 私有桥接 `commit_prepare/execute/status/reconcile`。同一个被锁 controller 和 lease 负责可信观察，target 从左栏使用的同一 CDP 连接读取；执行配额只扣一次。状态和只读恢复不依赖浏览器仍存活。
- 当前生产入口 `submission_adapter=None`，没有注册真正的 SAP 保存执行器和完整业务只读 verifier。配置开关不能绕过缺失适配器，准备/执行/恢复提前拒绝，OpenCode 不提供保存工具。不能将隔离回执 fixture 解释成真实采购订单成功。
- 新增 `backend/identity.py`：受信系统状态三元组/代次绑定、私有引用失效和重新登录计划；固定 ADT 身份探测禁止代理、重定向及任意路径，保留已授权的单一测试 SAP TLS 规则。现行独立手工 Web GUI/MCP 路径没有被这个可选能力全局替换。
- 新增独立 Linux 显示单元，使用真实 Chrome、Xvfb、x11vnc、websockify、noVNC；初始空白页、独立口令/profile、沙箱、loopback 发布、进程回收和下载校验。不是把本机 CDP 截图桥当作 VNC，也没有自动登记为现有远程执行节点。
- 修正专属 OpenCode 允许工具列表漏登记 `sap_page_scroll` 的问题；普通执行器和 `rsmcode` 源码未修改。

## 实际执行证据

1. 当前 OpenCode 来源 `9acdb1d09ff4daed68550c2efb7ddb5250c6ab5b` 的真实 canonical host/Session V2/原生 Web reducer 与取消传播测试通过；双实际私有 host 直达/跨 host 凭据隔离通过：**3 pass，68 assertions**。供应商和 SAP 桥使用 loopback 替身，不把它作为真实 SAP 页面兼容验证。历史版本漂移记录仍保留。
2. 从当前默认租户场景配置私有解析凭据，向已授权的测试 SAP 固定 ADT `/sap/bc/adt/core/http/systeminformation` 发起一次只读请求。实际 SID/Client/用户名与配置匹配。仅记录 `verified/system_client_account_match=true/source=adt_systeminformation`，没有输出凭据、正文或头。此结果证明该配置账号的 ADT 身份，不能证明左侧 Web GUI 登录或已存在 gateway `connection_id` 的身份。
3. 在用户指定 Chrome 的现有工作台页面恢复原历史，右侧原生 OpenCode 可见，左侧专属浏览器显示 `ERR_CERT_AUTHORITY_INVALID`。通过正常输入区发出两个指定 MCP 只读连接检查请求，实际 `sap-abap/adt_discover` 和 `sap-pyrfc/healthcheck` 均成功，原生工具卡片与完成回复可见；场景动作表对应两项 mcp_read 为 succeeded。PyRFC 使用 ADT 后端，原生 RFC SDK 未安装。未操作或保存 SAP 业务，恢复会话没有绕过证书警告。双栏截图见 [Chrome 现场](resumed-dual-pane.jpg)。
4. 临时启动已有 Colima profile（2 CPU / 4 GiB / vz），不改 profile。Docker CLI 缺 buildx，legacy 构建显式指定 `TARGETARCH=arm64` 后真正解析 Dockerfile，固定 Debian 镜像 HEAD 返回 EOF；没有进入 APT、Chrome 或 VNC 启动。无容器创建，Colima 于 23:45:50 成功停机，恢复此前状态。没有跳过 Docker Hub TLS；4.1 实际部署仍未完成。
5. 桌面独立数据根的用户仍需首次改密，Web 登录不会自动共享至桌面。未读取密码/hash、重置用户、复制登录令牌或绕过该状态，7.2 登录后双栏未验收。
6. 只读 MCP 对话结束后，按正常界面暂停并退出工作台视图；确认左侧是证书页、没有未保存 SAP 业务输入。保留原加密主密钥及原 COW/SAP 环境，正常终止 Web 旧进程并从当前源码重启；新 PID 54933、9899 就绪。Chrome 完整刷新后原平台登录、场景卡片及配置正常，恢复同一 `ses_rsm_350fa35892ae469d894128b0f5cb09da` 原生历史，两个成功 MCP 工具和完成回复仍可见，控制权默认人工。新场景 host 端口变化到 58836，没有重放工具/业务操作。Web 最新 Python 代码已加载，桌面旧后端没有在本轮重启。

## 验证

身份专项 146 项、审批专项 52 项、受信桥接 14 项、Linux 部署专项 42 项均有隔离验证；这些数量包含在场景总套件中，不重复相加。Python 的 SAP callbacks、显示进程/网络和 ADT HTTP 使用明确替身，IAM 与场景 SQLite 使用真实临时实现。最终场景套件 **747 pass / 1 skip，73.14 秒**，相关 Node **153 pass**，当前来源 Bun host **3 pass / 68 assertions，2.27 秒**；pytest 跳过的是未启动的真实 Chrome fixture，实际浏览器联调用独立接口完成且未绕过证书。

本轮修复后重新验证 controller 排队替换、非法 action_id 的零配额消耗和审批活动的空闲时钟；未将旧 controller 的锁当作新 target 的锁。提交取消不声称能撤销已经送达 SAP 的保存。

## 剩余范围

| 剩余任务 | 未完成条件 |
| --- | --- |
| 1.5、1.7 | 完整 SAP 主题/内核/SSO 与发布版本、G0 的所有前置验收；本轮 host 测试只补源码漂移后的 canonical 接缝 |
| 2.9、3.11 | 可信 Web GUI 身份采集、统一重新登录与真实改密/退出/切号联动；保留用户当前独立登录选择 |
| 3.2、5.5 | 完整生产 SAP/节点资源注册授权与模型/工具计量验收；现有身份、coding 授权、配额及审计接线已有实现，未扩改公共身份模块 |
| 4.1 | 镜像拉取/构建、Linux 沙箱、VNC 画面/输入以及远程工作台会话接入 |
| 4.3、4.4、4.6、4.7 | 双真实 SAP 登录态、SSO/所有键与控件、代表性资源/延迟、完整 G1 证据 |
| 5.3、5.7、5.8 | 完整分页/控件、有效采购订单保存前校验、两个真实 SAP 账号及实际凭据生命周期 |
| 6.3、6.6 | 实际 SAP 保存 adapter 和完整只读业务核验器；获准有效测试单据、真实回执/单据号及 unknown 现场恢复；当前保持提交关闭 |
| 7.2 | Web 最终源码加载后的完整双栏现场及桌面正常登录后的双栏 |

已经请求必需的测试条件：专属 Chrome 证书信任、桌面正常登录/首次改密，以及获准的 ME21N 测试业务数据和第二个 SAP 测试账号。账号秘密仅通过配置/登录入口输入；没有要求用户选择实现路线。没有承诺用无效空采购订单保存或用单一 SAP 账号代替双账号验收。

Web Python 后端已按上述条件加载当前源码；新 Bun host 读取当前外置适配源码。恢复后的双栏与原对话历史在 Chrome 可见，但左侧仍需证书信任，不能将历史的页面业务操作当作本轮新 Python 适配器的现场验收。桌面旧后端本轮未重启、登录后双栏未验收。完整 change 保持未归档。
