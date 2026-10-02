# 实施进度（滚动更新）

本 change 的 `tasks.md` 共 81 项，跨 P0—P4 六个阶段与两个发布平台。本文记录
**已完成并验证过**的部分，以及**明确未完成**的部分；未完成项一律不勾选，
避免把“文档就绪”当成实现完成（见 `proposal.md` 的启用门槛与 `acceptance.md`）。

> **本文件曾经严重滞后**：早前版本写于第 2—3 组期间，把 2.5、3.5—3.8、第 5 组、
> 第 6 组、第 7 组列为“未完成”，而它们此后都已落地并验证，与 `tasks.md` 相互矛盾。
> 下方「已完成」表只保留早期批次的细目（未重写历史），后续批次汇总在
> 「后续批次（滚动）」一节里按组指向各自的证据文件。当前计数：
> **已完成 69 / 未完成 12**（第 9 组与第 10 组的 10.4—10.6、第 11 组的 11.1—11.5 均已
> 完成；余下 12 项全部集中在「缺安装包 / 缺 Windows 主机」与 P0 收尾复核，见文末）。

## 已完成并有可运行测试（第 1—4 组，早期批次）

| 任务 | 交付 | 验证 |
|---|---|---|
| 2.1 crypto 接收者修复 | `channel/web/static/js/console.js`：新增 `_desktopRandomBytes` / `_desktopRandomToken`，以 `crypto.getRandomValues(bytes)` 保留接收者；CSPRNG 缺失或安装标识无法持久化时显式报错，不再退回不持久随机值 | `node --test tests/test_desktop_local_dir_crypto.cjs`（7 项通过，含真实接收者检查） |
| 2.2 安装标识收敛到主进程 | 新增 `desktop/src/main/installation-identity.ts`；`remote-host-ipc.ts` 的 `bindContext` 改为主进程 ID，页面值仅在首次运行作为迁移候选；`host-bridge.ts` 不再要求页面提供 `installationId`；`console.js` 仍发送旧值但标注为迁移提示 | `node --test tests/test_desktop_installation_identity.cjs`（9 项通过） |
| 2.3 原生选择链路抽取为共享服务 | 新增 `desktop/src/main/local-files/selection.ts`（`DirectorySelectionService` / `validateDirectory`），全进程唯一接触原生选择器之处；对外只给 `SelectionState`（阶段、代次、生效 `GrantPublic`、错误、授权文案），**不含绝对路径**；`local-files-bridge.ts` 抽出 `activateGrant` 供服务与既有 `applyChooseWorkspace` 共用，用途无法在两处被放宽；`remote-host-ipc.ts::chooseWorkspace` 改走该服务，响应只增 `state` 字段 | `node --test tests/test_desktop_local_selection.cjs`（29 项通过，含"真实 `activateGrant` + 真实注册表"接缝用例与路径不外泄断言） |
| 2.4 代次 / 作用域 / 取消保护与就绪状态机 | 同上文件：`idle → selecting → validating → preparing → ready`（+`failed`），按 server/user/tenant/device 分作用域持有；取消回到先前阶段并保留原生效选择；每个尝试递增代次并在 pick 后三个异步边界复检，被超越者返回 `superseded` 且不提交、不改写新状态；`prepare` 失败有 `release` 时撤销并回退生效项、无 `release` 时如实显示该 grant 仍生效；`markRevoked`/`forget` 接入 `disconnectWorkspace` | `node --test tests/test_desktop_local_selection.cjs`（29 项通过；含快速重选、prepare 竞态、prepare 失败 x2、跨租户隔离、撤销/登出清理、state 拷贝语义） |
| 3.2（部分）授权用途区分 | `grants.ts` 增加 `GrantPurpose`（`readonly-input` / `project-execution`）、`allowsProjectExecution`、按用途区分的选择对话框文案；`local-files-bridge.ts` 增加 `normalizeGrantPurpose` / `chooseWorkspaceMessage` 与 `purpose` 透传；`host-bridge.ts` 拒绝未知用途；preload/adapter 支持可选 `purpose` | `node --test tests/test_desktop_project_execution_grant.cjs`（10 项通过）；`tests/test_desktop_local_files.cjs` 12 项通过 |
| 1.1（部分）复用基线 | `evidence/master-reuse.md`：固定 SHA 的实际 diff 数据与逐接缝处理 | `git diff $MASTER` 实测 |
| 1.5（部分）macOS 平台探针 | `evidence/platform-probes.md`：真实 `sandbox-exec` 边界，含动态路径、软链接与派生进程结论；并补充 §2.1 的接入启动器后的三处必需许可（祖先目录 literal、`file-read-metadata`、`/dev/null` literal）与读边界准确口径 | 真机探针（macOS 26.4）+ `node --test tests/test_desktop_local_execution.cjs` 内核级断言 |
| 3.1 会话 `execution_target` / `project_mode` / 迁移 41 | `agent/workspace/execution_target.py`（仅标识符 + 用途，构造即校验，backend 不得携带本机标识）；`project_store` 新增 `desktop_targets` 独立映射；`auth/store.py` 迁移 41 为 `desktop_workspaces` 增加 `project_mode`（默认 `readonly-input`） | `tests/test_desktop_local_root.py` |
| 3.3 可信本机解析分支 | `POST/DELETE /api/desktop/local-roots`：loopback（拒绝转发头）+ 启动令牌 + native bearer + 自有 device/workspace/binding + grant 用途匹配；路径仅存进程内存，不入库、不回显；`project_store` 个人根校验未改动 | `tests/test_desktop_local_root.py`（含真实 `web.ctx` 传输校验） |
| 3.4 不可变执行上下文 | `RuntimeIdentity.execution_target`（冻结值，随 ContextVar 复制到工作线程）；`channel/web/fork/execution_scope.py` 在消息入口解析一次；`AgentBridge._resolve_local_project` 仅走可信注册表并复核目录存在；撤权后拒绝而非回退 | `tests/test_desktop_execution_target_wire.py`（含跨线程不变性） |
| 4.1–4.3 headless worker、依赖注入、stdio 生命周期 | `agent/desktop_local/{protocol,worker}.py`：复用 master 原 `read/write/edit/bash/ls/search_files` 类与 schema，不复制业务实现；`set_cwd()` 重定向替代全局 `os.chdir()`；无模型循环、无 Web 后端、无 scheduler；单次调用进行中即拒绝并发帧 | `tests/test_desktop_local_worker.py` |
| 4.4 / 4.6 / 4.7 / 4.8 macOS 启动接缝、能力描述、环境清理、预算与取消 | `desktop/src/main/local-execution/{sandbox,env,worker-session,interpreter,launch}.ts`：`sandbox-exec` 下 `(deny default)`，读写两个方向都由策略约束；启动计划由 `launch.ts` 组装（环境在此清理，`TMPDIR` 一定指向授予的 run 目录，调用方无法忘记）；解释器可读路径由 `interpreter.ts` 实测 `sys.base_prefix`/`stdlib`/`purelib` 得出；能力描述明确写出读边界与元数据例外；超时即取消并**真实终止进程树** | `node --test tests/test_desktop_local_execution.cjs`（50 项，含真实沙箱进程的越界读写内核拒绝、超时后命令与后台子进程均消失） |
| 4.9（开发机部分）A17/A26/A27 证据 | `evidence/isolation-acceptance.md`：逐条 A 编号 → 用例映射，含四种 signal 过滤器实测对比与三处真实缺陷的根因；`evidence/platform-probes.md` §2.2/§2.3 | 同左两个套件（50 + 44 项） |
| 1.4（部分）cwd/落盘工具分类 | `evidence/execution-boundary-map.md`：已核对 `device-ops.ts::SUPPORTED_OPS`、fs-guard 的穷举 `match`（构造上只读，用例断言 `exec/write/unlink/...` 为 `UnknownOp`）、分发路径实际门禁、`RuntimeIdentity` 与 cwd 改写现状、服务端表结构 | 直接读代码 + grep 核对 |

回归：`node --test tests/test_desktop_*.cjs` → 303 项通过；
`tests/test_desktop_web_pages.py` 等 149 项桌面 Python 用例通过
（其中 `test_the_console_asks_the_adapter_and_nothing_else` 的旧断言在本次
改动**之前**就已失败——HEAD 的 `console.js` 已调用 `bindContext`——已按现状修正）。
本批次另跑：`tests/test_desktop_local_root.py tests/test_desktop_execution_target_wire.py
tests/test_desktop_local_worker.py` → 110 项通过；
`node --test tests/test_desktop_local_execution.cjs` → 50 项通过；
`tests/test_bash_streaming.py tests/test_desktop_local_root.py tests/test_desktop_local_context.py
tests/test_desktop_file_access.py tests/test_desktop_contracts.py` → 136 项通过、3 跳过；
`tests/test_bash_output_encoding.py tests/test_invariant_bash.py tests/test_bash_background.py
tests/test_bash_exit_codes.py tests/test_tool_args_truncation.py tests/test_bash_windows_guidance.py
tests/test_bash_config_propagation.py` → 61 项通过（本轮改了 `agent/tools/bash/bash.py`，
故把 Bash 相关套件全跑了一遍）。

## 读边界的准确口径（不得简化）

macOS 沙箱的**内容读**被限制在授予根 + 系统运行时 + 解释器安装路径内（实测越界
`cat` 报内核 `Operation not permitted`），但**目录元数据**（存在性、大小、时间戳）
全局可读——exec 前必须对每一级祖先目录做查找，`(allow file-read-metadata
(subpath "/"))` 是启动解释器的必要条件，去掉它 `execvp` 直接 EPERM。因此：

- 面向用户与模型的能力描述 MUST 同时给出这两点（`describeScriptCapabilities`
  的 `Reads:` 行与 `Isolation:` 文本已如此），不得只说“已隔离”；
- 早期实现曾用 `(subpath "/")`（全部可读）而探针记录写作 `(literal "/")`，
  两者不一致且前者使读边界形同虚设；现实现统一为 `(literal "/")` + 祖先 literal，
  并以 `file-read*` 段内**不得出现** `(subpath "/")` 的断言固定住。

## 明确未完成（不得勾选）

- **第 1 组其余（1.2 / 1.4—1.8）**：归档状态与主规范冲突 delta 复核、发布平台与
  依赖清单汇总、其余平台探针、治理切片逐项登记、v2 envelope 与默认限额口径固化、
  P0 门槛复核。1.4/1.5 已有部分证据（`execution-boundary-map.md`、
  `platform-probes.md`），但**Windows 一侧空缺**。
- **3.2 其余**：服务端 `agent/desktop_local` 注册表与 `integrations/desktop/local_root.py`
  已落地（作用域 + `grant_version` 严格匹配、可撤销、进程退出即清空），
  Electron 主进程的 gen/ready 状态机由 2.3/2.4 补齐（`local-files/selection.ts`）；
  剩余为“固定 owner/origin/设备/会话/用途/版本/代次”整表的最终复核。
- **第 4 组其余**：4.5 Windows 启动器与探针（当前**明确拒绝**，不得声明脚本可用）、
  4.9 的**安装包内**（签名后、随包 Python）等价探针，以及 A17 的“资源超限”维度中
  **内存/CPU 硬上限**（当前只有墙钟、帧/结果上限与输出截断，见
  `evidence/isolation-acceptance.md` §5）。
- **第 8 组其余（8.8 / 8.9）**：8.1—8.7 已落地（获权 manifest、作用域版本缓存与引用计数、
  类型化引用与生产接线、依赖锁定与随包交付、跨端输入准备与摘要校验、产出归属与客户端平台），
  证据见 `evidence/skill-resources.md`、`evidence/skill-dependencies.md`、
  `evidence/resource-landing.md` 与 `evidence/prompt-priority.md`。**8.8 已完成本地半边**：
  A06/A07 在真实路径上跑通（真实 `SkillManager` 选技能 → 真实 `LocalSkillRuntime` 校验后
  pin → 真实 executor endpoint → 真实 `sandbox-exec` worker），A11—A13/A27 各自的边界也
  有真实断言，证据见 `evidence/skill-execution-acceptance.md`。**8.8 执行时发现并修掉一个
  真实缺口**：`RunSkillSet.roots()` 从未被传到启动器，本地沙箱因此读不到 pin 的技能脚本；
  修法是把技能根从 `script_scope` 一路传到启动器，并以 shell 拥有的 `skillCacheRoot` 作
  信任锚校验（越界即拒，不是丢弃）。**8.9 已完成远程半边**：服务端把本轮技能集落到 command
  行并在 prepare/start 两条路径上双向强制、`skill_package` 端点只发授权集合内且重算摘要相符
  的版本、设备侧按摘要缓存并校验后才以只读根挂载（详见下表与
  `evidence/skill-execution-acceptance.md` §3、`evidence/skill-transfer-mutations.log`）。
  另注：8.5 的**安装包内**验证（打包产物里技能脚本真能 import 到 `xlsxwriter`）属 10.1—10.3，
  与本文件顶部"缺少安装包"的口径一致；8.6 遗留的 `client_files` 结果路径落地（A13）与
  `delete_run_input` 基准目录缺陷见 `evidence/resource-landing.md` §7。
- **第 9 组其余（9.8）**：本机 source adapter、有界刷新/订阅、原生桥（打开/另存为）、
  无桥隔离预览与 CSP、历史重建、显式上传/分享与数据流说明**均已完成**（9.1—9.7）；
  **9.8 已完成**：验收行 A21—A25/A34/A35 与 A06/A07 逐条实测，并补上真 Chromium 里
  「点卡片 → 到达宿主」的端到端证据（`desktop/e2e/cards-and-system-open.probe.cjs`，
  16 项 0 失败）。这条链路第一次被整体走通时就暴露了一个真缺陷：页面说的**动作名**与
  宿主适配器认的**预载方法名**不是一套词，四项系统动作在真机上全部点不动；修法是一张
  派生表 + 4 项接缝用例 + 跨端比对用例 + 变异 M21/M22/M23。P3 门槛判定见
  `evidence/p3-artifacts-and-actions.md` §四：本地侧满足，远程侧**机制满足、安装件内
  真机执行留给 10.1—10.3**，故 10.x 仍不勾选。
- **第 10 组（10.1—10.7）**：逐平台安装包构建与真机验收、新旧服务端/客户端 A29 组合、
  普通 Web/Channel/无项目/只读项目/定时任务回归 A30/A31、A01—A36 覆盖复核、
  `evidence/release.md` 汇总。**均需安装包与真机，本机无法完成。**
- **第 11 组（11.1—11.6）**：迁移与回滚、feature flag 默认关闭、账本/收敛/可见性的
  最终启用门槛。11.1—11.3 已有实现（`integrations/desktop/publish.py`，
  “Durable publish ledger, reconciler and staging visibility”），但**门禁与
  flag 口径未固化**，故不勾选。

## 后续批次（滚动）

| 组 | 状态 | 证据 |
|---|---|---|
| 2.5 真实 Electron A01/A02 | ✅ 6/6，连续 3 次复跑一致 | `evidence/desktop-e2e-a01-a02.md`、`evidence/e2e-a01-a02-run.log` |
| 3.5—3.8 运行中切目录/来源统一/团队 Agent/编号级 E2E | ✅ | `evidence/run-context-and-revocation.md`、`evidence/source-resolution.md`、`evidence/target-resolution-acceptance.md`、`evidence/team-agent-and-scheduler-authorization.md` |
| 第 5 组 本地模式接通与本地 E2E | ✅ | `evidence/local-e2e.md`、`evidence/local-e2e-run.log` |
| 第 6 组 执行 v2 协议与远程命令通道 | ✅ | `evidence/remote-e2e.md`、`evidence/governance.md` |
| 第 7 组 journal/去重/取消/恢复 | ✅ 74 项用例 + 12 类注入 12/12 命中 | `evidence/faults.md`、`evidence/journal-fault-mutations.log` |
| 8.1—8.3 manifest/版本缓存/引用计数 | ✅ 63 项用例 + 12 类变异 12/12 命中（首轮 3 项未命中，2 项为用例缺陷、1 项为验证脚本缺陷，均已修） | `evidence/skill-resources.md` |
| 8.4 类型化引用与执行端定位 | ✅ 36 项用例 + 18 类变异 18/18 命中 | `evidence/skill-resources.md` |
| 8.5 依赖锁定与随包交付 | ✅ 56 项用例（34 守卫/锁 + 22 守卫语义与接线，含 gitignore 陷阱）+ 22 类变异 22/22 命中（M19—M22 为本条新增）；修掉 `xlsxwriter` 未随包交付的真实缺口 | `evidence/skill-dependencies.md`、`evidence/skill-dependency-mutations.log` |
| 8.6 跨端输入准备与摘要校验、落盘工具处置 | ✅ 102 项用例 / 31 子测试 + 19 类变异 19/19 命中；`resource:<id>[@ver][#digest]` 校验后原子落地、按名拒绝不回退服务端；A36 处置表以自动清单兜底 | `evidence/resource-landing.md` |
| 8.7 产出归属 / 维护权限 / 客户端平台 | ✅ 51 项新增用例（回归 437 + 184 通过）+ 12 类变异 12/12 命中；产出落项目、项目授权不覆盖技能与记忆、平台未知即未知（M3 用服务器平台冒充会被判失败） | `evidence/prompt-priority.md` |
| 8.8 代表技能执行的编号级验收（本地） | ✅ 11 项用例无 skip；A06 的 350 由测试自解 zip 核对工作簿单元格、输入摘要逐字节不变、产出在项目内且不在缓存；A07 查**已部署版本**的模板与辅助资源、字段齐且无 `{{` 残留、技能/记忆目录未进项目；A11 越界技能根与无锚点两种请求均被拒；A12 真实锁定清单与真实角色授权两向验证；A27 读不到服务器工作区。**执行中发现并修掉 `RunSkillSet.roots()` 从未到达启动器的真实缺口**（技能根 → `script_scope` → 视图 → 端点 `skillCacheRoot` 信任锚 → worker 按 `grantId+根集` 缓存）；3 处走样实跑均被判红；回归 491 项通过、端点契约 19 项通过、`tsc` 无错 | `evidence/skill-execution-acceptance.md` |
| 8.9 技能包到设备与摘要校验（远程） | ✅ 21 + 13 + 29 + 10 项用例；服务端把本轮集合落到 command 行并在 prepare/start 双向比对（空声明也被拒），`skill_package` 端点只发命令行授权过的版本且重算摘要、按三个预算切包；设备侧摘要为键的缓存、补齐传输、`verifySkills` 闸门与只读技能根全部接通。6 类变异 6/6 命中，**并因此证伪两条原本通过但空转的用例**（服务端授权集合层、设备端重算摘要层），已补齐 | `evidence/skill-execution-acceptance.md` §3、`evidence/skill-transfer-mutations.log` |

| 10.1 后端打包段（本机 macOS arm64） | ⚠️ 部分：onedir 构建 + 冻结 worker 实跑通过；**修掉两处会让包"能装不能用"的 P0**（`excludes=['wheel']` 与 setuptools hook 别名冲突导致构不出包；随包 3.11 上两处 PEP 701 语法使 `agent.tools` 启动即 import 失败、PyInstaller 却照常出包）；新增 `check-python-syntax.py` / `verify-backend-bundle.py` 两道构建门槛并接入四个 workflow | `evidence/packaged-bundle.md` |

## 为什么停在这里

早期版本的结论是“第 3 组起的核心路径改动无法在缺少真机环境时验证”，这个前提
**已经不成立**：第 2—7 组此后都在本机用真实 Electron、真实网关/设备帧、真实子进程
沙箱完成了验证（见上表）。因此停下的原因不再是“无法验证”，而是**缺少安装包与
Windows 主机**：第 10 组要求逐平台构建并签名实际安装包、在安装包内重复真机验收，
第 4.5/4.9 要求 Windows 探针，这些在本机确实无法完成。

第 8/9 组（技能包与本机产出）不依赖安装包，属于可推进的工作。8.1—8.3 已完成：
技能包的两端共用一套打包规则、摘要覆盖全部资源且与遍历顺序无关、版本按摘要寻址并
按引用计数回收（**不复用**会删在用目录的 `sync_skills_to_workspace`）、缓存存在不等于
已授权。**8.4（8.1—8.3 的生产接线）已完成**：类型化引用（`project:`/`skill:`/`backend:`）、
`RunSkillSet` pin 与只读技能根、以及 `AgentStreamExecutor._stage_local_inputs` →
`prepare_tool_inputs` 的真实路径，证据见 `evidence/skill-resources.md`（含 18 项变异验证）。
**8.5（依赖锁定与随包交付）已完成**：代表技能 `rfq-quote` 的交付 Excel 全部经 `xlsxwriter`
写出，而它此前**不在**桌面锁定清单里（延迟导入 → 失败落在产出阶段、沙箱子进程内）；
现已补 `xlsxwriter` 与技能声明，并新增打包守卫 `desktop/build/check-skill-dependencies.py`
（退出码 0/1/2 区分"没查成"与"查过且没问题"），接线进 `build-backend.sh` 与三条 release
工作流（均在 PyInstaller 之前跑 `--verify-imports`），证据见 `evidence/skill-dependencies.md`。
**8.6（跨端输入准备与摘要校验、落盘工具处置）已完成**：`resource:<id>[@<version>][#<digest>]`
引用在落地前同时校验版本、来源自报摘要与调用方钉住的摘要（均现算），发布走 `.partial-*` +
`os.replace` 保证不留半截文件；接进 `prepare_tool_inputs`，没有落地传输或落地失败时**按工具名
拒绝**而不是回退成服务器路径；获权来源 `RunInputFetcher` 按调用者租户/用户收窄、摘要从磁盘
字节现算；落盘工具处置表（A36）以 `agent/tools` 的自动写盘点兜底，未分类的新落盘工具会被
`capability` 拒绝派发。证据见 `evidence/resource-landing.md`（含 19 项变异验证）。
其中两处**未做、另行跟进**（详见该证据 §7）：`client_files` 结果路径的落地属 A13/8.8；
`integrations/desktop/publish.py::delete_run_input` 的 `artifact_rel` 基准目录不一致（既有
缺陷，删除/过期不释放磁盘），本次未顺手修改。
**8.7（产出归属 / 维护权限 / 客户端平台）已完成**：本地项目提示把"成果落进项目"写成规则
（A06），明确项目授权**不**覆盖技能与记忆维护、且不得把客户端路径交给在服务端解析路径的
工具（A07）；`Agent.client_platform()` 按本机/远程/服务端三条分支取平台，**未知即未知**，
不退回 `sys.platform`（A12）；`Bash` 的平台说明抽成常量，桌面视图不再继承服务器平台，
`script_platform()` 取不到就返回 `""` 而非 `posix`。证据见 `evidence/prompt-priority.md`
（含 12 项变异验证）。另：`tests/test_shared_asset_prompt_guidance.py::test_guidance_reaches_the_llm_request`
为**既有失败**（用例未设 `workspace_scope`），已用改动前源码复跑确认，未顺手修改。
**8.8（代表技能在两种模式的执行验收）本地半边已完成，远程半边未接线并单列为 8.9**：
本地 A06/A07 在**真实路径**上跑通——真实 `SkillManager` 选出技能、真实 `LocalSkillRuntime`
校验摘要后发布并 pin、真实 executor endpoint（node 进程 + loopback + 启动令牌）、
真实 `sandbox-exec` worker 在项目里跑脚本；夹具技能**未改写**（按 `SKILL.md` 相对定位资源，
连"资源位置适配"都没用到）。A06 的 350 由**测试自己**解 zip 读工作簿单元格核对（不信技能
自己的 JSON），输入工作簿前后 sha256 逐字节相同，产出在项目内且不在缓存，服务器哨兵未动；
A07 的"模板资源齐全"查的是**已部署版本**（pin 目录），字段齐、无 `{{` 残留、技能/记忆目录
未进项目；A11 越界技能根与无锚点两种请求都被启动器拒绝；A12 用**真实锁定清单**证明声明被
满足、再用空清单证明缺项被指名（技能名 + `xlsxwriter` + 该改哪个文件），并用**真实
`IdentityService` 的角色授权**证明未授权技能既不部署也不落缓存（按**内容**搜索，因为版本
目录以摘要命名），另有"能读 `SKILL.md` 但写不进 pin 目录"两条一起钉住只读；A13 裸绝对路径
与 `backend:` 都抛 `server_path_not_local` 且消息带文件名；A27 读不到服务器工作区。
**本次执行发现并修掉一个真实缺口**：8.4 的 `RunSkillSet.roots()` 注释写明"这就是喂给沙箱
`skillRoots` 的值"，但从 Python 到启动器**没有任何人把值传过去**（`script_scope` 只发标识符、
`tool_view_for_run` 不带技能根、端点 `buildGrantLaunchPlan` 只有 `projectRoot/tempRoot`），
于是本地跑技能脚本时沙箱连脚本自己都读不到——而 `sandbox.ts` 与 `launch.ts` 早就支持。
修法：技能根从 `script_scope` 逐层传到端点，端点以 shell 拥有的 `skillCacheRoot` 作**信任锚**
校验（`isInsideRoot` 不成立即 403 `skill_root_outside_cache`，无锚点即 403
`skill_cache_unavailable`；**拒绝而非丢弃**），worker 缓存键从 `grantId` 改为 `grantId+根集`
（profile 每个 worker 建一次，换了 pin 集必须换 worker）。证据见
`evidence/skill-execution-acceptance.md`（含走样点表与依赖版本 Python 3.14.3 / xlsxwriter 3.2.9）。
**8.9（远程模式技能交付）已接线并完成**：三处缺项同轮闭合。**服务端**把本轮技能集落到
command 行（`commands.py` 的 `skill_resources` 列 + `_skill_resources_from_row` 回读），
`canonical_skill_resources` 是唯一的规范化入口（摘要覆盖的集合与交给设备的集合不可能漂成
两张表），broker 在 prepare 与 start 两条独立路径上按「声明集合必须等于行上集合」双向比对
（另一个版本 / 子集 / **空集** 均 `incompatible_skill`/422，无技能集的运行仍接受空声明）；
`GET /api/desktop/execution/skill-package`（`contracts/desktop/v2.json` 冻结路径与三个预算）
只发命令行授权过的 `(skill_id, digest)`，用**实时** `skill.use` 重建 manifest 并要求重算摘要
相等，因此服务器上被改过的技能是按名拒绝、而不是按旧版本名把新字节发出去。**设备侧**
`skill-cache.ts` 以摘要为键、先写 `.partial-*` 再单次 `renameSync`（摘要不符什么都不留下），
`skill-transfer.ts` 补齐缺失版本且首个失败即整体拒绝，`device-execution.ts` 的 `verifySkills`
在写日志与运行**之前**闸门，`local-read-assembly.ts` 把解析出的缓存目录交给与 8.8 同一套
启动器（worker 按 `skillSetKey` 分桶）。原先那条"带原因的 skip"已改成正向断言。
**变异检查当场证伪了两条"通过但空转"的用例**：服务端授权集合层被删掉后 19 项仍全绿（原用例
请求的是服务器造不出的摘要，被下一层摘要检查兜住），设备端重算摘要层被删掉后 13 项仍全绿
（原用例答复里的 `digest` 就是被换掉字节的摘要，传输层就拒了，缓存重算没走到）；两条都已按
可观测的形态重写。证据见 `evidence/skill-execution-acceptance.md` §3 与
`evidence/skill-transfer-mutations.log`。**未完成**：安装件内真机端到端属 10.1—10.3；Windows 未验证。

**9.6（按生成时来源重建历史）已完成**：判定收敛到一处
（`LocalRootRegistry.entry_for_path`：只认仍在册的注册、同 user+tenant、最内层优先、
两侧 realpath 且前缀带分隔符），服务器端用**注册**而不是会话当下的 `execution_target`
造卡片来源，所以切过目录的老卡片仍带着生成时的 device/workspace/binding；文件不在时
保留卡片并标 `resolution:"missing"`（而不是像实时产出那样丢弃、也不是改成服务器引用）；
另一台机器的路径命中不了任何注册，因此永不被声称为本机产出；面板在 `act`/`preview`
之前用 `wrongProject(expectedWorkspaceId)` 拒绝跨项目重解析，且**不上传**任何东西
（用例断言服务器工作区目录仍为空）。用例：`test_desktop_local_root.py`（10）、
`test_desktop_artifact_source.py::HistoryTests`（4 新增）、
`test_desktop_project_native_actions.cjs`（8 新增）；变异脚本
`evidence/scripts/mutate_history_cards.py` **16 类走样 16/16 命中**
（`evidence/history-cards-mutations.log`）。顺带修掉一处既有 i18n 缺口：
`console.js` 早已引用 `ws_sel_select_failed` 而三份词典都没有它
（`tests/test_console_i18n_coverage.cjs` 在 HEAD 上就红），已补齐并同步快照 fixture。
证据 `evidence/history-cards.md`。

**9.7（显式上传/分享与数据流说明）已完成**：缺的不是服务器能力（transfers/publish/配额/
审计早已存在），而是**设备那一半**——`device-ops.ts` 自
`fix-desktop-local-context-and-tool-calls` 起就把 `materialize` 一律答成
`feature_unavailable`，「不自动上传」成立而「明确上传」不存在。本轮补齐仅从显式命令进入的
一条路径：`local-files/materialize.ts`（必须是普通文件 → 大小上限 → 批准版本必须相等 →
复用既有单窗口上传 → 服务器必须给回 `artifact_ref`，否则 `publish_failed`）、
`remote/materialize-transport.ts`（既有三个端点，origin 校验、无会话即 `auth_required`、
服务器错误码原样保留）、`device-ops.ts` 的 `materialize` 分支（缺上传口答
`feature_unavailable` 并说清原因）。字节走 fs-guard，**这条路径里没有任何地方能拼出绝对
路径**；窗口 4 MiB 由有界 32 KiB 读取拼成（helper 的 1 MiB 请求上限让这条边界可观测）。
来源关联为 `desktop-file:<workspace>:<相对路径>` + `<size>:<modified>`；本地文件只读不写。
数据流说明补在三份词典并挂在本机项目面包屑上（服务器路径不带这句话），用例同时断言那句
绝不能被写的谎。用例 `tests/test_desktop_materialize.cjs`（17）、
`tests/test_desktop_local_read.cjs`（+4）、`tests/test_desktop_project_native_actions.cjs`（+2）；
变异 `evidence/scripts/mutate_materialize.py` **16 类走样 16/16 命中**
（`evidence/materialize-mutations.log`）。回归 737（node）+ 1233（pytest）通过。
证据 `evidence/materialize-and-data-flow.md`。**未完成**：面板上没有每张卡片的
「上传到服务器」按钮（触发点是用户在会话里明确要求）；安装件内真机属 10.1—10.3；Windows 未验证。

第 9 组 9.1—9.8 已完成；第 1—8 组的滚动状态见上表，第 10/11 组的逐条状态与未完成项以
`tasks.md` 为准（10.1—10.3 缺签名安装包与 Windows 真机，10.7 待汇入，11.6 待最终复核）。
