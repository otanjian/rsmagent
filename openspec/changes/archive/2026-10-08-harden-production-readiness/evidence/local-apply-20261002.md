# 本地实施与验证记录（截至2026-10-03）

## 结论和范围

已按用户要求实施修复，保留实际角色、资源授权、功能开关和业务联网；没有部署、提交、推送或归档。HEAD仍为 `31afbad9f08f7954d7258d9045b2b773c40fc5ec`，工作区包含既有其他未提交修改。风险复评发现此前“剩余仅环境问题”的概括过于宽泛，本轮依用户指定补齐后台容量和固定镜像部署两个局部缺口。本 change 原36项按完整验收条件计为18项完成，加上新增9.1、9.2共20/38，原18项仍保留真实平台验收或能力缺口，详见 tasks.md；这不是“生产可发布”的结论。Electron运行时升级等复评其他项不在本轮指定范围。

本轮最重要的未满足条件为：**当前 macOS Seatbelt 不能阻止同 uid 进程环境读取**。实测使用只有 PATH 和合成 SENTINEL 的测试进程，没有枚举或读取真实其他进程的秘密。`ctypes` 直接调用 `sysctl(KERN_PROCARGS2)` 可取到合成值；即使显式 sysctl deny、名称过滤或 syscall-number 过滤也没有满足门槛。`ps` 不能执行不能证明这个入口被阻断。最终代码保留明确 deny 和真实就绪探测，记录 `filesystem=true, process_environment=false`，`/api/ready` 返回503；功能没有被关闭，完整生产隔离没有被宣称通过。固定探测在冻结包中走早期专用入口，避免将 `-c` 错当应用参数而递归启动实例。

这是目标运行条件未满足，不将失败改为成功断言或 skipped：回归断言**探测必须识别这个缺口并阻止就绪**；本地检查另外显示 `NOT VERIFIED / PREREQUISITE UNMET`，最终镜像门槛要求完整探测成功。Linux 的进程 namespace、继承FD、socket及挂载时序仍需真实宿主/容器实测，当前没有环境。macOS 后端整体安全门槛仍不通过；现有 Desktop 本机项目文件隔离证据范围与之不同。

Linux 尚有明确待验证和适配的边界：现有遮罩只处理启动时存在的路径，保留宿主网络也不构成 abstract Unix socket 隔离。启动后保护文件新建/替换、挂载源软链接竞争与 abstract socket 必须加入真实 Linux 全链路验收，不能用当前就绪/镜像冒烟代替；任务2.2/2.7保持未完成。

## 实现清单

- `agent/execution/sandbox.py` 提供身份/Agent/资源/会话重验、主体路径与运行库根、私有临时区、Mac Seatbelt和Linux bubblewrap构造。`launcher.py` 是前台、后台和重试共用的窄接缝；rdai启动装配强制登记，没有失败后直接Popen的降级。
- 私有容器子树（users/user/agents）、外置身份库及WAL/SHM、其他绑定Agent均作保护。运行库只读；Python虚拟环境与源码数据根共处时只豁免明确运行库/技能子树，配置/数据库仍拒绝。Homebrew Node依赖只读按实际二进制和链接库枚举，未把整个HOME当运行库。
- 默认传递必要环境；业务设置 `execution_environment`、凭据 `execution_credentials` 按租户/Agent/可选用户显式映射。凭据每次调用现有服务解析，支持个人用途凭据和JSON字段；未新增角色或秘密存储。输出/增量输出脱敏，后台owner校验、任务终结回收、取消接口保持。
- 递归search_files使用已有Python后端在读文件前过滤私有后代，保留content/files/count与no_ignore语义；ls过滤未授权子项。仅检查共享父目录不能授权读取其他成员文件。Desktop已有独立worker未安装服务端接缝，原路径保持。
- 限流空桶到期回收，身份解析非预期异常统一503；日志20MiB/5轮转。可信代理来源、HTTPS Cookie与同源scheme比较补齐。`/api/health`保持，新增只读、轻量、脱敏的`/api/ready`。
- 整实例冷备复用CLI，新增格式/清单/哈希、只读登记查询、外置根、包含已提交WAL页的独立身份库快照、其他业务数据库完整目录、内部软链接/空目录；停机证明+互斥；归档校验和停机复查后原子发布。恢复到新根，先校验/解密/重写路径后原子发布；失败不覆盖旧实例。Docker停止证明与卷路径映射有本地模拟测试，但没有真实Docker验收。
- 单一Dockerfile、两架构Linux/Python3.11哈希锁、Python/Node基础摘要、安全源、非root和持久化Compose、HTTPS模板、镜像/层扫描与统一检查、Gitea workflow已编写。CI生效、镜像构建/系统包扫描没有环境证据。
- 保留gTTS语音、Chromium、Node、渠道和文档支持。PDF旧回退依赖迁为pypdf；renderer图标类型与已知夹具问题修复，正常类型检查仍为构建前置。

## 功能基线与正向覆盖

`capability-inventory.json` 为只读盘点：2个租户、74个Agent（包含1个私有绑定）、747条grant记录、45条工作区/仓库技能；角色计数、凭据用途种类仅登记标识/数量。全局`.env`只有变量名记录：COW_CREDENTIAL_MASTER_KEY、OPENAI_API_BASE；配置中业务密钥只记录键名dashscope_api_key。Desktop各开关按真实配置键/默认值登记，没有以不存在的别名推定开启。未向真实身份库写测试数据。

| 能力 | 本地证据 | 不包含的证明 |
| --- | --- | --- |
| 成员、租户管理员、平台管理员 | 真实SQLite IdentityService、正常成员临时密码更换、真实AgentStream工具派发；没有模拟授权允许 | 外部远程HTTP/模型服务 |
| Bash、Python、Node、文件 | 三类角色写文件；Node实际执行；计算路径、私有后代、软链接、全局临时区、管理Unix socket负向；自身Unix socket正向 | Linux内核/容器真实挂载与进程范围 |
| 用途凭据/合法网络 | 合成HTTP接收端检查实际Authorization；当前服务真实轮换、撤销；未知父环境不下传 | 同uid宿主进程环境隔离（当前不通过） |
| 真实图片技能 | 三角色运行仓库原`skills/image-generation/scripts/generate.py`，原环境变量名、作用域密钥、受控接收端返回PNG并保存产物 | 真实厂商API额度/网络与模型效果 |
| 文档 | 真实PDF文本经财务/采购原回退路径读取，保留文档功能 | 所有客户复杂文档与外部OA/邮件/会议/媒体端点 |
| 后台任务 | 启动、增量输出、另一owner拒绝读取/取消、正常取消和无需轮询的完成回收 | Linux PID namespace和目标管理器对setsid脱离子孙的整组停止 |
| 整实例恢复 | 真正创建/读取ZIP与SQLite，恢复后登录、凭据解密、外置私有Agent、文件/空目录/调度文件保持；错误密钥/坏包/穿越/内部外部链接、发布失败保留旧备份、互斥拒绝均有断言 | 真实业务实例停止、业务历史引用和调度外部副作用验收 |
| Desktop | 源码Node行为、类型/构建；重新构建冻结后端并运行工具/元信息冒烟；冻结worker额外覆盖5个原安装形态用例 | 正式签名客户端的完整UI与真实远程时序，Windows |

迁移表及切换顺序见 `docs/operations/execution-credentials.md`；目标凭据归属有歧义时必须在部署切换前解决，没有静默共享或删除真实秘密。实际OA、邮箱、会议、摄像头与远端服务环境没有提供，未以合成接收端冒充这些系统验收。

## 续修：备份完整性复核

本次继续核对后修复了四类本地缺口：

- 外置身份库原本可能只复制主文件而遗漏已提交 WAL。现用停机锁内的 SQLite backup API 生成独立快照；真实子进程提交后直接退出，分别覆盖内置/外置数据库，恢复后验证记录和登录，源库与 WAL 字节保持。
- 停机复查原本在替换旧归档之后。现移到原子发布之前；复查失败保留旧备份并清理临时归档和快照。
- 默认渠道状态和微信多实例相邻凭据文件可能遗漏。补齐默认/外置路径，仅归档指定文件；保留实例文件名关系，覆盖基础文件尚未生成的情形，恢复后通过原路径解析函数读取，不扩大到整个 HOME。
- 配置环境覆盖可能使实际数据根不同于 config.json。现按启动相同规则读取声明配置，容器模式使用目标容器环境和工作目录；归档生效配置并在恢复时重定位。未声明的操作者环境变量不归档，主密钥继续独立托管。

新旧备份针对性回归 **35 passed**，新增7例已纳入统一检查。清单补记应用版本；相关规范、设计及运维文档已同步。未修改实际角色、业务开关或真实数据。实际 Docker 停机与挂载证明仍是模拟测试，未标记为真实部署通过。

## 续修：流式脱敏与连接回收（2026-10-03）

- 前台原先在截取进度尾部后才匹配完整凭据，分段输出可能先发布前缀，较长凭据还可能被短凭据抢先匹配。前后台现共用流式字节脱敏，在进入缓存和回调前处理；仅暂存可能组成凭据的尾部，优先匹配长值，普通进度立即返回。保持已支持的输出编码回退。
- 后台轮询/列表中的命令文本也过滤已解析凭据；实际命令不改写。退出126的重试日志不再回显参数内容。
- `/api/ready` 的只读 SQLite 连接在查询成功或失败后均显式关闭，不依赖垃圾回收释放文件描述符。

新增8个回归已纳入统一入口，包括真实子进程的stdout/stderr分段输出、进度尾部截断、后台轮询与元数据、普通进度及时返回，以及就绪查询两种结果下的连接关闭。另用固定随机种子检验10,000组分块/重叠字面量组合，与完整输出脱敏结果一致。额外复跑的大输出UTF-8落盘、配置传递和Bash约束18例也通过，独立记录、不与统一检查重复相加。既有前后台执行、取消、超时与三角色技能回归继续通过，未修改功能开关或角色授权。这些修复不消除前文记录的macOS内核边界或Linux待验收项。

## 续修：固定镜像验收对象与构建摘要（2026-10-03）

- 镜像检查原先在扫描、启动时重复使用可变标签，标签改指后可能混用不同镜像的证据。构建现通过 Docker `--iidfile` 获取不可变 ID，扫描成功后再交给 CI 和后续检查；独立检查只解析一次标签，逐层、历史、容器启动和输出记录均使用该 ID。
- 非 root 检查原先只拒绝完整字符串 `root`/`0`。现解析配置中的用户部分，拒绝 `root:group`、`0:group`，并检查容器内实际 UID，覆盖用户名别名解析为 root 的情形。先创建再启动测试容器，启动失败仍可按 ID 删除容器与匿名卷；清理完成后才打印成功记录。
- 构建代码树摘要改为读取已经复制到临时目录的字节及软链接目标，避免原文件随后被编辑导致记录错配。合成敏感标记覆盖软链接时先解除链接，不改写链接指向的外部文件。

新增16个离线流程回归，与既有4个合成镜像层用例一起20项通过；已纳入统一检查。覆盖真实临时文件复制后变化及软链接保护、模拟 Docker 的 ID 传递、扫描/构建失败不发布 ID、root 拒绝、启动失败清理及平台前置拒绝。实际 Docker 未运行，不将这些结果计为 Linux 镜像验收。操作文档、Gitea 调用、规范与设计同步。该轮仅修改发布工具、测试及文档，未改变当时测试应用包中的运行代码。

## 指定续修：后台容量与固定ID部署（2026-10-03）

- 后台任务在现有注册表锁内预留名额，默认单进程最多20个正在启动或尚未完成回收的任务，可由 `COW_BASH_MAX_RUNNING` 调整。进程创建不持有注册表锁，保留输出、取消和前台执行；满载在创建进程前返回可重试提示。自然结束无需轮询即可释放，取消以及进程/线程创建失败均回收名额和临时脚本。
- 最终镜像检查增加 `--deployment-env`，在全部镜像检查及测试容器清理成功后原子写出固定image ID、提交和代码树摘要。失败保留旧配置并返回非零；`--all` 中本地检查失败也不生成新配置。Compose只按该image ID启动，移除重新构建入口，设置禁止拉取；文档移除可变部署标签，并清除可能覆盖ID的shell变量。
- 新增12个后台容量用例（含真实子进程与受控并发），新增11个发布配置/入口用例，与既有16个镜像流程用例合计39项针对性通过，已纳入统一入口。Compose 5.1.4 的真实 `config --format json` 验证了最终image ID、无build、禁止pull以及缺少ID时拒绝；不需要daemon，也不构成Linux镜像运行验收。

本轮只完成风险第3、5项的局部实现。没有引入分布式队列或新身份配额，未升级Electron或改变Windows功能。目标容量数值、Docker实际运行、发布提交和CI准入生效仍待目标环境。

## 最终本地检查

各组有重叠，不相加为总测试数。所有项目测试用隔离临时数据。

| 检查 | 实测结果 | 范围 |
| --- | --- | --- |
| `check-production.sh --local` Python | **580 passed、9 skipped、4 subtests passed，46.38s** | 本轮新增12个后台容量和11个部署配置回归；9个为平台不匹配用例；Windows桌面未测 |
| 同一入口 Node | **65 passed、0 failed、5 skipped** | 源码模式，5个安装形态用例在下述冻结组补测 |
| 冻结worker Node | **44 passed、0 failed、15 skipped**，其中5个installed用例全部通过 | 本轮容量修复后重建.app，在包内Resources/backend验证。15个为需要源码Python的互斥形态用例，源码组已跑，不计通过 |
| 真实三角色原图片技能 | 3 passed | 和Python组重叠，实际原脚本/OS/凭据服务/合成HTTP/PNG产物 |
| 路由覆盖 | 231 routes、282 method entries，OK | 新就绪端点经唯一registry |
| Web模块接缝 | 22 modules、365 symbols、0 findings | 上游/fork接缝检查 |
| Desktop | renderer/main typecheck、Vite/main build通过 | 保留原字体/大chunk构建提示 |
| macOS测试应用包 | arm64应用包生成；包内worker 44 passed/15互斥形态skipped、后端工具及元信息冒烟通过 | 未签名本地测试包，不是正式分发/远程UI验收 |
| 冻结后端构建 | Python3.11/PyInstaller成功，415MiB；6类工具、6个数据路径、`/api/desktop/meta`通过 | 更新现代Python的aiohttp下限，原语音/渠道功能保留 |
| Dockerignore | 20个禁止路径排除，1894个tracked运行资源保留 | Moby patternmatcher规则验证，不能替代实际镜像层扫描 |
| 镜像扫描器回归 | 安全层、已删除敏感层、元数据、禁止路径4类合成tar用例通过 | 实际Docker image未构建 |
| 镜像构建/验收流程 | 新增16项通过 | 文件复制/摘要使用真实临时文件；Docker调用模拟，不是Linux验收 |
| 后台容量/固定ID部署 | 本轮新增23项通过 | 真实后台进程、并发启动、失败回收、真实Compose配置解析；镜像运行仍模拟 |
| 依赖审计 | 两架构各116包、版本一致；1包1条保留报告 | 详见dependency-audit.json，非系统镜像扫描 |
| 静态/规范 | 新增代码Ruff F、git diff --check、shell语法及OpenSpec strict | 最终交付前执行，见候选记录 |
| 原生生产探测 | **NOT VERIFIED / PREREQUISITE UNMET** | 当前macOS filesystem=true / process_environment=false，不能称安全验收通过 |

最新完整检查输出在会话临时日志 `/tmp/rsmagent-production-local-20261003-capacity-pin.log`；此前镜像流程续修记录在 `/tmp/rsmagent-production-local-20261003-image-gate.log`，脱敏/就绪续修记录在 `/tmp/rsmagent-production-local-20261003.log`。本记录保存结论，脚本可复跑，不依赖临时日志长期存在。

本轮以现有Python3.11构建环境检查技能依赖及717个运行源码文件语法后重建冻结后端（415MiB），未重新解析或升级构建依赖。构建日志 `/tmp/rsmagent-backend-build-20261003-capacity-pin.log`，应用包构建 `/tmp/rsmagent-app-build-20261003-capacity-pin.log`，包内Node用例 `/tmp/rsmagent-app-bundle-20261003-capacity-pin-tests.log`，6类工具/6个数据路径及元信息接口冒烟 `/tmp/rsmagent-app-backend-20261003-capacity-pin-smoke.log` 均通过。

依赖审计保留 Click8.1.8 / PYSEC-2026-2132（CVE-2026-7246，GHSA-47fr-3ffg-hgmw）：修复版8.3.3与gTTS2.5.4的Click<8.2约束冲突；仓库没有click.edit调用，当前入口不可达。没有删语音功能、强装冲突版本或隐藏审计结果。aiohttp/PyPDF2旧报告和websocket-client约束冲突已处理。系统镜像漏洞/干净重建待Linux环境。

## 交付与剩余环境

部署/恢复操作、受控备份与主密钥分开保管、可信代理、资源观测及回滚步骤见 `docs/operations/production-readiness.md`。构建脚本记录提交与代码树摘要，镜像工具记录image ID/RepoDigest；当前尚无最终Linux镜像/正式签名安装包/目标容量结果，不能固定为可发布候选。

剩余：Linux原生/容器/最终镜像及全链路；当前macOS完整进程隔离条件；目标业务凭据/外部测试系统；真实停机恢复和容量；实际Gitea runner与分支准入；真实远端与正式客户端登录/重登/切换/断线/重启/撤权时序。Windows为 `deferred: environment_unavailable`。这些均未创建虚假的通过记录；change保持未完成、未归档。

最新本地测试应用包位于 `/tmp/rsmagent-candidate-macos-20261003-capacity-pin/mac-arm64/容大AI.app`。本轮包含新的后台容量代码，已重建冻结后端并重新打包。继续使用已安装的Electron33.4.11，运行时升级属于复评另一风险，不在用户本轮指定范围。`CSC_IDENTITY_AUTO_DISCOVERY=false`仅用于本地未签名测试包，未改变正式签名配置；此包不是获准生产分发的产物。代码树和产物摘要见 `local-candidate.json`。
