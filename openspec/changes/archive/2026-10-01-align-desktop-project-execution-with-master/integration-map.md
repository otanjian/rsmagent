# 基线、依赖与进行中 change 协调

本表是实施和归档的协调清单，不修改其他 change 的完成状态。`openspec/specs/` 为现行规范；未归档 change 提供现有实现及待验收上下文，不可替代主规范。

## 1. 来源与代码复用

参考 `origin/master@8f1b19f1e72db0b46772f78f9c760b04b1836428`。实现时记录每个修改文件的上游接缝和保留的企业化差异；至少覆盖选择器、项目存储、Agent cwd、SkillManager、文件/Bash 工具、提示词、工件和预览。新增代理不改变原工具的名称、参数和合法调用结果语义。

## 2. 已有 capability

| capability | 本 change 的关系 | 验证要求 |
|---|---|---|
| `scoped-project-browser` | delta：独立桌面原地授权；普通个人根、复制导入保持 | 伪造路径不能进入；原始文件与导入副本语义不同 |
| `execution-isolation` | delta：本机注册根、实际执行边界、双端执行 | 动态脚本/子进程/链接探针；旧配置值不替代平台证据 |
| `desktop-tenant-context` | 不重写认证，消费当前 PKCE/broker 与已实现配对 | 两用户两租户、切换、撤权、秘密不进 worker |
| `agent-runtime-capability-enforcement` | 保留工具/技能绑定，代理仍在原门禁之后 | 黑白名单、skill.use 撤权、团队实际 Agent 重验 |
| `resource-execution-authorization` | 消费逐次资源执行授权 | 入队后撤权与开始前重验 |
| `audit-log` | 消费命令开始/终态/unknown/恢复审计 | 审计失败策略沿用切片，不能假完成 |
| `credential-management` | 消费已声明凭据/网络授权 | 不复制主进程或服务器环境，缺凭据明确失败 |
| `action-approval` | 依动作判适用性，普通读取不新增重复审批 | 需审批动作绑定实际命令/摘要，用户选目录不代替该审批 |
| `resource-quota` | 消费调用并发、传输、缓存等额度 | 原子预留/释放、取消清理，不建第二套业务配额 |

## 3. 与未归档 change 的边界

**归档状态已核对（任务 1.2）**：下表五个 change **全部仍未归档**（位于 `openspec/changes/`，不在 `openspec/changes/archive/` 的 101 项中）。因此：

- 它们声明的 v1 增量**尚未进入** `openspec/specs/`。现行主规范里**没有**「本机能力一律只读」这类全局要求，本 change 的 delta 与之**当前不冲突**，不需要为「已归档」做任何回填修订。
- 但它们的**原文**确实存在只以「首版」限定、未绑定执行模式的表述，归档时会逐字落进主规范。风险见 §5，不是现在可以忽略的问题。
- 本 change 只改 `execution-isolation`（MODIFIED）与 `scoped-project-browser`（MODIFIED/ADDED），并新增 `desktop-project-execution`、`desktop-skill-runtime`、`desktop-project-artifacts`、`desktop-execution-delivery`；**不修改**其他 change 的任何文件（含各自 spec delta）。

| change | 保留内容 | 本次新增/替代范围 | 合并/归档动作 |
|---|---|---|---|
| `add-desktop-remote-web-workbench` | 统一 Web、配对、IPC 身份、只读 v1、固定解析器、gateway/commands/transfers | 独立协商执行 v2 和本机 worker；旧 fs-guard 不扩展成 Shell | 联合核对协议文档；将“禁止脚本/写回”的适用范围限定为旧 v1，说明新能力由本 change 负责；不能抹去 v1 禁令 |
| `fix-desktop-local-context-and-tool-calls` | 只读引用校验、真实读取终态、模型结构化工具调用完整性 | project-execution 模式改为本机工具和本机产出；readonly-input 仍维持服务器 cwd | 其“服务器输出保持原规则”仅适用于只读来源模式；若先归档，在其最终 spec 中增加模式限定；若后归档，不覆盖本 change 新 capability |
| `use-personal-workspace-for-shared-agents` | 未选项目时的本人默认目录及原产出来源 | 本机项目具有更高优先级；关闭项目后恢复默认 | 用同一 resolver 表达优先级，避免两边重复判断；不把本人目录删掉或搬到本机项目 |
| `land-shared-agent-panel-on-own-files` | 普通后端面板落点及用户隔离 | 新增 desktop source adapter | 不把本机项目当后端路径；后端面板回归必须通过 |
| `guard-shared-knowledge-skill-writes` | 共享资料维护资格和只读指引 | 项目业务产出不受默认 outputs 文案误导 | 区分业务输出与共享维护建议，不能为了项目写入删除共享规则 |

执行 v2 对根路径的原则是“不主动同步宿主路径映射、不作服务端路径或授权依据”。用户明确请求 `pwd` 等命令的结果可能包含路径；这是有副作用执行模式的获权工具内容，与旧 v1 不向模型主动投影绝对根的规则区分。产品文案不许作绝对脱敏保证。

本地执行模式并不意味着全部工具转到客户端：远程知识、MCP、企业 API 保持服务端归属。只有已适配的项目工具本机运行；资源跨端时必须显式落地或明确拒绝。

## 4. 已知证据缺口

前序 `fix-desktop-local-context-and-tool-calls/evidence/verification.md` 记录真实设备网关与模型出口实测未完成。开始远程执行验收前需补同源 WebSocket gateway、真实模型结构化工具调用和真实桌面 chooser/worker；不能以其 mock 回归、只读控制协议或原 E2E harness 通过来替代本 change 的远程写入证据。

## 5. 文档归档规则与跨 change 归档顺序

本次只创建自身 change，不提前更改主规范或其他 change。实施阶段先核对相关 change 是否已归档；若基线已变化，按新主规范重做 delta，并保留同名 requirement 的完整旧行为。完成前须记录兼容矩阵和归档顺序，避免某个旧 change 后归档时把新版执行能力覆盖为「所有本机能力只读」。

### 5.1 会被误读为全局禁令的 v1 原文（已定位）

以下三条位于**未归档**的 `add-desktop-remote-web-workbench`，均以「首版」限定，**归档后会把「首版只读」写进主规范**。它们本身正确，必须保留；风险是下游读者把它当成对设备能力的不变约束，从而否定本 change 的 project-execution：

| 位置 | 原文要点 | 归档时必须保留的限定 |
| --- | --- | --- |
| `desktop-local-file-access` / 「本地根由用户明确选择且首版只读」 | 首版仅开放列目录、元数据、受限文本搜索、读取和文件导出，**不提供删除、覆盖、Shell 或任意脚本** | 「首版」指**只读来源模式（`readonly-input`）**，不是设备上限；有写权限的 project-execution 模式由本 change 的 `execution-isolation` delta 与新能力承载 |
| `desktop-local-processing` / 本地计算默认关闭 | 只接受签名客户端已验收的固定解析器，**MUST NOT 传入脚本、Shell、Python 表达式** | 限定在**固定解析器契约**上；本 change 的 worker 走独立 v2 执行协议与独立 `skill_execution_*` capability，不复用该解析器入口，也不放宽其禁令 |
| `desktop-file-transfer` / 另存为边界 | 首版 **MUST NOT** 允许 Agent 自动指定本机任意路径、自动覆盖原文件，或把只读目录授权解释为写权限 | 「把只读目录授权当写权限」**永远成立**（本 change 同样禁止：只读 grant MUST NOT 因选择项目升级）；「首版」部分仅约束自动写回，不影响用户在 project-execution 下显式授权写入 |

### 5.2 归档顺序（推荐）

1. **先归档 `add-desktop-remote-web-workbench`**，落进主规范时按 §5.1 为三条加模式限定（只加限定，不删禁令）。
2. 再归档 `fix-desktop-local-context-and-tool-calls`：其「服务器输出保持原规则」只在 `readonly-input` 成立。
3. 再归档 `use-personal-workspace-for-shared-agents` / `land-shared-agent-panel-on-own-files` / `guard-shared-knowledge-skill-writes`：均为**更弱优先级**的来源或文案规则，不覆盖项目模式。
4. **最后归档本 change**：此时主规范已带模式限定，其 `execution-isolation` MODIFIED 与会话权限模式的冲突在归档时须保留 `readonly-input` 的全部旧行为（服务器 cwd、只读、无脚本），仅对 project-execution 增加本机语义。

**若顺序被迫反转**（本 change 先归档）：不必回改本 change，但必须先确认其后归档的 v1 change 的 delta 与主规范不产生「所有本机能力只读」的净效果；产生的冲突按 §5.1 表格逐条加模式限定，并保留本 change 新能力的全部 requirement。

### 5.3 判定口径

- 「冲突」只看主规范净效果：是否使 project-execution 模式下的本机写执行、本机脚本或本机工件产出变成**不可能**。
- 仅措辞重叠、作用域不同（v1 解析器 vs v2 执行协议）不算冲突，不得借此删改对方的禁令。
- 每次归档动作后重跑 `openspec validate --strict`，并复查本表 §3 的归档状态列。
