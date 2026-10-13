# Design

## Context

动机见 `proposal.md`。依据为 `docs/design/windows-desktop-verification-2026-10-13.md` 和 2026-10-13 源码核对；巡检属于开发模式，不能替代真实 NSIS 安装包或远程服务器验收。

| 接入点（当前行号） | 已确认事实 | 方案接缝 |
|---|---|---|
| `desktop/src/main/index.ts:43` | console 包装写文件后调用原终端输出；全局异常又调用 console | 提取无 console 依赖的宿主日志模块 |
| `common/log.py:17` | Python 已使用 RotatingFileHandler | 保留后端轮转，分离主进程文件 |
| `desktop/src/main/python-manager.ts:419,700,775` | 候选仅校验存在；start 已有 starting/ready 去重 | 异步解释器探测，诊断与生命周期代次 |
| `desktop/src/renderer/src/hooks/useBackend.ts:26,124`、`components/StatusScreen.tsx:26` | 已有错误查询与错误页，但立即终止错误集合有限 | 扩展现有错误契约，不另建启动 UI |
| `agent/memory/personal.py:159`、`common/safe_fs.py:94,140` | 事务的 UnsafePathError 被统一包装为目录替换；Windows 存在路径回退和文件锁分支 | 保留异常链并定向复现，不推断根因 |
| `channel/web/static/js/workspace.js:562,1055,1200` | 三处将 device_error 和 device_offline 合并 | 统一错误映射与恢复动作 |
| `desktop/src/main/remote/profiles.ts:68`、`connection.ts:117` | 精确 HTTPS origin 校验及 metadata 探测已经存在 | 增加请求期限和分阶段诊断 |
| `desktop/src/main/index.ts:883` | remote 明确跳过本机 Python 后端 | 回归验证并保持模式区别 |
| `integrations/desktop/gateway.py:1`、`docker/nginx.conf.example` | 远程网关可独立启动；当前代理示例仅转发 Web 端口 | 增加明确的 connect 网关路由与运维示例 |

合并后新增五个 capability 规范，其中四个来自已有本机执行代码尚未应用的增量；另修改五个既有 capability。`scoped-project-browser` 合并原 MODIFIED/ADDED 和此次能力提示 ADDED，`execution-isolation` 保留原 MODIFIED 的完整场景。`openspec/config.yaml` 没有 capability 注册表，不引入额外运行配置或更改全局规则。主规范在实现验收和同步时更新，规划阶段不提前宣称实现完成。

## Goals / Non-Goals

**Goals:** 每类错误能落到有效恢复动作；桌面日志和启动资源占用有界；本机和远程模式均有真实 Windows 证据；修复保持共享服务、身份及数据唯一归属。

工作包 A（阶段 1–7）交付稳定性与普通远程业务；工作包 B（阶段 8–15）补齐受支持平台的本机执行基线与验收。两者可独立交付，整体归档需两者完成。隔离和治理证据约束执行能力开放，不阻止日志、文案、generation 或非执行选择流程的修复。已有实现先复验，有具体缺口才补代码，不把验收清单当作全部重新开发清单。

**Non-Goals:** Windows 原生 helper、安全执行边界、enterprise-local、跨端数据同步和 SAP 现场验收不在本次交付。普通远程聊天不以本机执行能力开放为前置，上传也不等于挂载本机目录。

## Decisions

### D1. 日志拆为单写入者文件，异常通道不回到 console

主进程新增 `desktop-main.log`，Python 保留 `run.log`；本机开发模式与打包模式的实际目录由日志入口准确展示。后端启动前的 stdout/stderr 捕获写入宿主日志，避免两个进程共同轮转 run.log。保留后端自身日志策略。

console 包装同时处理同步抛错和 stdout/stderr 的异步 `error`，EPIPE/已销毁流停用对应终端。不能使用 `process.on('EPIPE')`，也不能仅依赖 writable 标志。全局异常使用文件直写通道和重入保护；非 I/O 异常仍记录，不把所有异常当作可忽略断管。参数格式化、队列、轮转和错误回调均不得调用 console。

首期仅要求断管保护、异常文件直写、分文件及可靠轮转。采用成熟实现或小范围轮转封装，初始单文件 10 MiB、2 个备份；轮转关闭旧句柄，写入失败不回到 console。优先使用现有流背压控制积压，不预设自建队列、签名聚合、丢弃计数或可配置日志框架。持续写入和不可写测试须证明文件、内存有界；确有积压证据时才补最小保护。

已有打开日志目录/查看入口准确定位两类文件即可，不新建日志大小面板、轮转状态 UI 或导出流程。现有 `remote/diagnostics.ts` 的导出与脱敏边界保持，不给远程网页任意读取本机日志的权限。

不采用“两边各自轮转同一文件”，原因是 Windows 句柄占用与备份竞争。旧 run.log 可能混合后端记录，不实现自动识别写入者或尾部迁移系统；提供停止写入者后人工处理旧大文件的说明，后端继续使用已有轮转。禁止盲目截断活动文件或无限另存 GB 备份。

### D2. 解释器只在开发本机模式探测

首期优先级：显式路径（`COW_PYTHON_PATH` 优先于本机开发配置 `python_path`）→ 平台项目 venv（Windows 优先 `.venv-win`，再 `.venv/Scripts`、`venv/Scripts`）→ PATH。Conda/micromamba 用户通过显式路径指定已有环境，不自动搜索其所有环境或增加 py Launcher 发现链。准备脚本创建/复用 `.venv-win`，不覆盖 macOS .venv；远程页面不能指定可执行路径。

使用无 shell 的异步短探针检查版本和 sys.executable；每候选 3 秒、整轮 15 秒为初始上限，限制输出并清理超时进程。Python 支持范围复用项目要求，Store 路径仅作线索，实际结果才是依据；显式配置无效立即失败。探针不导入全部可选工具或建立独立依赖注册表。

保留现有 backend_*，按确有需要补 python_not_found/python_invalid/dependency_missing，贯通现有类型、useBackend、StatusScreen 和双语文案。核心依赖问题复用已有构建检查和后端启动 stderr/错误通道，必要时只增加已证实缺失模块的检查，不建立全量依赖管理系统。可选工具复用首次使用前的依赖检查，将 Playwright 模块/浏览器缺失转为明确缺项提示；不自动安装，不启动扫描全部工具。

打包版只使用随包后端，构建阶段检查必要及声明支持的可选依赖，后端丢失提示修复安装。remote 模式完全跳过本机探针，不因本机缺 Python 显示启动失败。

### D3. 先定位第二次启动，再决定局部修复

现有 status guard 和恢复预算已存在。先用启动来源、PID、端口及必要调用记录确定第二次 spawn 是外部开发进程、多个实例还是内部竞态。若现有保护有效，提交复现/回归结论即可；只有证实内部缺口才局部增加共享启动 Promise、停止等待或旧回调身份检查，按真实原因选择，不预设引入串行器或统一生命周期框架。

端口占用只处理可证明属于本实例的子进程；其他监听者应返回冲突或选择允许的端口。不能将报告的 WinError 10048 直接当成 status guard 缺失。

### D4. 记忆修复在同一安全服务内完成

先捕获锁创建、路径解析、实际文件操作等失败阶段与原异常，在合成本人目录中复现中文、空格、盘符大小写和不同盘符，并覆盖 junction、symlink、目录替换及并发锁。普通 Windows 路径兼容修复集中于 safe_fs，personal 服务只负责稳定错误映射及 owner/version 上下文。

仅真实安全拒绝返回对应 403；锁竞争和服务故障按现有 HTTP/错误约定表达可重试状态，身份失效由身份层确认。先按真实根因做最小路径/锁/映射修复，不预设重做 safe_fs 或建设原生文件层。若复现发现独立的底层防替换缺口，应明确记录所需方案和范围后再调整规划，不顺带扩大本次实现，也不得跳过守卫、弱化安全规范或勾选未满足的验收。用户提示不暴露其他成员路径，诊断无正文。

不自动删除 .memory.lock、清空目录或复制独立记忆 CRUD；回归保存/清空 generation、索引屏蔽和恢复。远程记忆使用服务器文件系统，不套用客户端盘符修复。

### D5. 本机能力与连接状态组成独立维度

复用当前 `execution_capability.py` 的组合态、metadata/hello 与宿主能力查询，补缺失的 platform/helper 原因及 UI 映射即可。已有身份/文档代次负责切换失效，不新增第二套能力快照、缓存或授权状态机；只在某个错误链路确实丢失必要字段时补兼容字段。实际授权继续由原有服务与宿主逐次检查，页面声明不是 grant。

沿用既有 `platform_unsupported` 拼写，helper 缺失使用独立原因字段或协商错误码；`device_error` 保留一般失败语义，`device_offline` 仅用于确实离线。多个 workspace 分支共用映射；新增字段兼容旧客户端，未知字段关闭对应新能力而不阻断已兼容核心业务。generated-contract.ts 如需变化，先改源契约，再运行生成脚本，禁止只手改生成文件。

Windows 未支持的能力关闭新增入口；已有绑定显示限制，可清除或切换到云端，不删除用户项目。本 change 不修改 win32.supported，不修改 accepted/open 来绕过平台验收。

### D6. 远程部署区分 Web 与设备网关

推荐独立 HTTPS 域名，客户端配置精确 origin，不部署在未支持的路径前缀。普通业务请求转 Web 服务；使用设备通道时，单独将 `/api/desktop/connect` 代理到真实网关，设置 Upgrade/Connection、超时与来源规则。网关示例启动为 `python -m integrations.desktop.gateway --bind 127.0.0.1 --port 9877`；实际端口以部署配置为准，不默认 Web 的 9899 同时接收网关。

部署文档提供 Web 与网关的独立进程管理、同一身份库/命令数据库配置、挂载、权限及重启方式；容器网络可用服务名，宿主部署可用 loopback。对外客户端只需出站 HTTPS/WSS，服务器通常暴露 443；本机回环登录回调不需公网开放。现有数据库进程约束和网关共享存储支持范围必须验证，不增加 Web worker 绕过限制。

metadata 保持公开、no-store、直接 JSON，认证代理不能将它重定向到登录。TLS 不允许降级或跳过验证；企业 CA 分别验证 Node 探测、Electron 页面和 WSS 的实际信任链。代理更新保留现有 gzip、缓存头和 SSE 非缓冲行为，与 optimize-console-login-startup 合并协调。

复用现有连接状态机与错误界面，检查 metadata/授权/配对是否缺少期限或原因，只有缺项才补，不重写 HTTPS、PKCE、协议协商或重连机制，不要求新增完整阶段仪表盘。设备故障只影响依赖它的能力；Bearer 主进程独占、租户验证和退出撤销保持，重启重新授权。

### D7. 附属问题按既有契约定向修复

命名窗口导航记录 target、初始 URL、复用窗口和路由恢复过程；按 console-entry-consistency 和 console-route-lifecycle 的现有要求验证控制台/工作台落地，不贸然改成应用内导航。CDP 截图为巡检基础设施问题：每次操作设超时，失败保留状态证据，不能据两次挂起宣称产品截图接口有缺陷。

### D8. 本机项目执行作为同一 change 内的独立验收工作流

并入原 change 的四个新 capability 与两个既有能力增量。阶段 8–15 对应原阶段 1–8；原 2.1 映射到 9.1，保留其上游已完成标记，其证据是在等价实现上取得，当前构建复验仍未完成（精简后的 9.2）。原 proposal/design/tasks/specs/metadata/evidence 全部按 SHA-256 保存在 `evidence/merged-source/`，历史证据另迁入 `evidence/local-project-execution/`，不将历史现场描述当作当前事实。

文档代际接入点为 `remote/container.ts`、`remote-preload.ts`、`remote/host-bridge.ts`：保留上游仅挂 did-navigate 的一次加载一代行为，补同文档调用和旧文档拒绝侧测试。禁止为绕过 stale_context 给旧文档自动重握手；只有真实安装包证明现方案不足时才审查替代实现。choose→bind、原生取消及迟到回调都需真实桌面验证。

执行接入点为 `auth/capability_matrix.py`、`integrations/desktop/execution_capability.py`、`execution_broker.py`、`channel/web/fork/handlers/desktop.py`、`agent/desktop_remote/mode.py` 和桌面 worker/技能缓存/工件接缝。只读输入是文件引用，不升级为项目写入或脚本授权；项目模式将文件工具固定到实际设备项目，远程模型循环仍在服务器，不能再创建第二个本机模型循环。服务器服务能力保持原有资源归属。

声明与部署开关双门禁：配置默认 False；先准备/测试动作映射，后凭真实隔离、治理切片和 A01–A36 适用证据放开 accepted/open，部署再显式启用。文件面与脚本面可按验收分别开放，但整体执行工作流不得仅凭文件面结项。Windows 维持 win32.supported=false，无可用工具且返回 platform_unsupported；macOS/POSIX 也必须以实际 launcher/helper、隔离和安装包证据为准，不因平台标签就宣称支持。

当前 `acceptance-local-project-execution.md` 为执行矩阵；历史归档 acceptance 含 A34–A36 而旧任务未覆盖，精简后的 14.6 保留数据流、无真实产物拒绝及逐工具 cwd/落盘分类。支持平台跑正向与负向执行；Windows 本期验证拒绝/提示及普通远程业务，未支持的执行正向项明确不适用而非通过。

依赖 execution-isolation、audit-log、credential-management、action-approval、resource-quota 的适用真实切片；这些证据控制执行能力开放，不阻止阶段 9 的 generation 或原生选择非执行流程修复。命令 journal、epoch/租约、幂等、未知副作用、取消、工件与预览按 A01–A36 验证。优先复用已有自动测试/进程集成与关联到当前版本的证据，仅补缺口；一份证据可覆盖多个任务/用例，不逐任务另建文件。开发态测试与真实安装包层次不可互相替代。

关闭开关封锁新任务，保留在途效果、回执、历史和原始文件；重启后项目 grant 不自动复活，历史候选仅作提示，用户重新原生选择。失败保留原有效绑定或明确未就绪，取消不创建 grant。这些规则与 D5 的能力提示及 D6 的远程网关共同验收。

## Risks / Trade-offs

共享冲突基线的接缝处置：`agent/memory/conversation_store.py`（seam:conversation-store）继续唯一承载既有会话存储，本次不创建本机/远程双份权威记录；`agent/tools/scheduler/integration.py` 与 `tests/test_scheduler_web_update.py`（seam:scheduler）继续保留既有 scheduler 接缝和更新回归，不为桌面诊断另建调度器。这三个文件无本次规划中的直接改动；远程运行位置和后端恢复回归须确认既有会话/定时消费者兼容，若实施涉及它们则按原接缝合并，不整文件覆盖。

- [日志分文件影响旧入口] → 现有入口定位两类文件，旧大文件按说明人工处理，回归已有诊断和升级行为，不建设新导出界面。
- [启动自检拉长等待或选错环境] → 探针异步、有总期限；明确选择优先级和显式配置失败语义，不遍历所有环境。
- [误将记忆安全拒绝变为成功] → 正常功能与恶意路径/并发替换一起验收；不可满足守卫时不发布对应修复。
- [旧协议与新错误字段不一致] → 添加兼容字段和一般失败回退；缺可选能力不得开放入口。
- [代理修改影响流式或账号检查优化] → 定向回归 SSE、上传限制、gzip/cache、来源校验及 WSS。
- [报告只能证明一次开发运行] → 发布门槛要求真实 Windows x64 安装包、普通用户、125% DPI 与远程 HTTPS 服务证据。

## Migration Plan

1. 实施前保存构建版本和合成复现证据；不修改真实 config.json、身份库或用户记忆。日志/启动修复可先独立交付，不等待本机能力建设。
2. 部署分文件轮转和启动诊断，旧大日志按说明人工处理；不引入自动日志迁移，新增字段不使旧服务器误开启能力。
3. 在合成数据环境完成记忆和能力提示验收，再部署服务端错误字段与客户端消费。无需业务数据迁移；失败回退代码并保留诊断，不恢复不安全行为。
4. 在候选远程部署补网关与代理路由，先验证 meta/TLS/PKCE/配对/聊天，再验证网关。记录版本及端口，按固定产物回退；数据恢复遵循 production-deployment，不重建身份库。
5. 发布前执行真实安装包矩阵及相关身份/记忆/项目回归。受支持平台的本机执行按 D8 与 A01–A36 验收及门禁推进；Windows 本机执行仍关闭，后续建设需独立 change 和 execution-isolation 等切片证据。

## Open Questions

- 使用现有日志入口和成熟轮转实现的具体接入方式可在实施中确定，不要求异常聚合、新导出流程或新日志面板。
- 报告中的记忆 403、第二次 spawn、命名窗口落地来源需要定向复现选择具体修复点；任务和验收已覆盖这些结果，不预先声称某个底层原因成立。
- 本机执行 open 的具体动作名须与当前路由及授权契约逐项对应，并通过组合态测试；不改变双门禁、受支持平台范围或文件/脚本分别验收的已定方案。
