# 本机项目执行 v2 契约草案

本文是 `design.md` D7 的实施输入，规范行为以六份 spec 为准。当前 `contracts/desktop/v1.json` 不在本次规划中修改；实施时增加 v2 schema 和样例，禁止直接扩大 v1 的只读操作含义。

## 1. 协商与授权层次

- 现有 bridge 身份、配对 Cookie、native Bearer、设备连接和 origin 校验不变。bridge API 只接收打开/关闭项目、预览/系统打开等用户意图，不暴露 `exec(command)`。
- 在已有 meta/hello 中增加可选 `project_execution` 能力：`protocol_major=2`、已支持工具/schema 版本、平台/runtime、文件写与脚本是否已验收、限额及支持的本机工件协议。
- 新服务器 + 旧客户端、新客户端 + 旧服务器都保留 v1；缺 v2 时新入口明确不可用，不把 v2 操作降为 v1 的 `inspect/materialize`。
- 目录 grant 区分 `readonly-input` 和 `project-execution`。后者的有效操作是用户已确认范围、会话 permission mode、工具/Skill 权限及设备能力的交集。

## 2. 执行命令示例

以下标识全部为示例，不包含秘密；身份由服务端生成和校验，客户端不能靠这些字符串自行取得资格。

```json
{
  "type": "execute_tool",
  "protocol_major": 2,
  "command_id": "cmd_example",
  "run_id": "run_example",
  "tool_call_id": "call_example",
  "binding_id": "bind_example",
  "workspace_id": "ws_example",
  "device_id": "dev_example",
  "grant_version": 3,
  "selection_generation": 8,
  "connection_epoch": "epoch_example",
  "tool": "bash",
  "tool_schema_version": 1,
  "arguments": {
    "command": "python \"$COW_SKILL_DIR/scripts/report.py\" --input \"资料/明细.xlsx\" --output \"output/开票清单.xlsx\"",
    "timeout": 120
  },
  "skill_resources": [{"skill_id": "example-xlsx", "digest": "sha256:example"}],
  "params_digest": "sha256:canonical-envelope-example",
  "expires_at": "2026-10-01T00:10:00Z"
}
```

命令真实 owner/origin 等来自既有服务端记录与主进程当前认证上下文，不由页面 body 选择。示例为 POSIX 平台；Windows 命令描述及变量语法取实际执行端的工具 schema，不能原样套用 `$...`。项目根由主进程解析 `workspace_id`，`arguments` 不接受另设 cwd；每次仅有一个当前 Skill 资源根，多技能通过显式 resource ID 定位。

脚本工具的 `arguments` 有三种合法形状，取自主工具自身 schema，不另造字段：

| 形状 | 示例 | 说明 |
|---|---|---|
| 运行命令 | `{"command": "...", "timeout": 120}` | 前台或后台启动；后台启动需主工具自己的 `run_in_background` |
| 读取后台输出 | `{"bash_id": "job_..."}` | 句柄是执行端返回的不透明 job id，不是服务端进程 ID |
| 终止后台任务 | `{"bash_id": "job_...", "kill": true}` | 只对同一有效作用域开放 |

既不给出非空 `command` 也不给出非空 `bash_id` 的帧无意义，仍被拒绝；`bash_id` 不豁免 `timeout` 上限。句柄的作用域（账户/租户/Agent/会话/设备/binding/项目/grant 版本）由服务端在路由前重验，跨作用域轮询、接续或 kill 一律拒绝，过期句柄返回「已失效」而不是指向别的进程。

`params_digest` 覆盖 tool/schema、完整 arguments、运行关联、执行来源、grant 版本及资源摘要。连接 epoch 用于传输栅栏，不改变同一命令的业务身份；重连重新投递同 ID 前必须状态查询及原作用域重验。

## 3. 执行准备、开始和结果

| 步骤 | 责任方 | 必须确认的事实 |
|---|---|---|
| 创建命令 | 原 Agent 工具门禁 + commands | 当前用户/Membership、Agent.use、工具集合、Skill.use、审批与额度、绑定归属 |
| 接收/准备 | 主进程 | 当前实例/配对/设备、协议、项目根身份、grant 版本、所需技能包和运行时 |
| 请求开始许可 | 主进程经 broker 到执行端点 | 服务端重新检查命令仍可执行，返回短时、单次准许开始的元数据；凭据不进 worker |
| 持久开始意图 | 本机 journal | 固定 command/摘要/来源/技能版本；先落盘再产生副作用 |
| 启动执行 | 本机 worker | 验证后的 cwd、最小环境、只读技能资源及平台强制边界 |
| 进度/心跳 | 本机 → 既有命令通道 | run/tool_call 对应，输出截断及在途许可状态 |
| 本机结束回执 | 主进程 | 实际退出、产出文件及作用域；持久保存之后再发送 |
| 服务端结果接受 | commands + 原 run | 当前归属、命令与摘要一致；实际终态才回填 tool_result |

拟新增端点位于 `/api/desktop/execution/*`，用途分别为 prepare/start、heartbeat、status/reconcile；复用既有 native-only 认证、access service、命令记录和审计。执行端点不接受通用服务器 URL、调用者自定认证头或任意模块路径。

worker 的私有 stdio 接口只对主进程开放，不启动公开 HTTP 端口；参数对象在 schema 验证后交给已登记工具工厂，不支持按输入动态 import 类名。二进制资源通过现有获权分块通道或本机私有字节通道传输，不塞进控制帧。

## 4. 状态与不确定效果

复用现有服务端命令状态，不重建 ExecutionRun；增加可选 `execution_phase`、`effects` 和结构化错误，用来表达 v2 事实。

| 可见阶段 | 服务端现有状态映射 | 含义 |
|---|---|---|
| queued | queued | 已入队，尚未投递 |
| preparing | dispatched / acknowledged | 已投递或设备确认接收，尚无已确认副作用 |
| running | running | 已获开始许可并持久记录开始意图 |
| succeeded | succeeded | 实际结果完成，可能包含工件 |
| failed | failed | 已知失败，effects 为 none/partial/completed 中已知项 |
| cancelling | running + cancel_requested | 已请求取消，尚未确认进程树结束 |
| cancelled | cancelled | 实际终止已确认，已有写入不承诺回滚 |
| expired | expired | 未开始即过期，或按明确超时原因完成停止 |
| outcome_unknown | failed + code=outcome_unknown + effects=unknown | 可能已发生副作用，无可靠完成结论；禁止自动重跑 |

以上状态名已与 `contracts/desktop/v1.json` 的 `commands.states` 核对；`cancel_requested` 是新增阶段/字段，不是旧状态枚举。实施时继续核对数据库约束和状态转换服务，不能直接写入不存在的字符串；增量字段及映射提供旧端回读测试。

unknown 后只查询原 command/journal/文件；收到可靠结束回执时增加 reconciliation 结果及审计说明，不重开旧运行、不重复发布第二次成功工具消息。确需重试时由用户明确发起新的 command ID，说明原任务可能已经改过文件。

去重键为 `(origin, user, tenant, device, command_id)`，同时核对 Agent/session/run/tool_call、workspace/grant、参数及资源摘要。ID 相同摘要不同返回 `command_conflict`。记录存在 started 无 completed 时，既不能推断“未执行”也不能推断“已成功”。过期命令即使 journal 被清理仍不可执行。

## 5. 默认资源和生命周期参数

| 参数 | 默认/上限 | 依据或调整方式 |
|---|---|---|
| 控制/结果帧 | 64 KiB | 复用 v1；大结果使用引用和分块 |
| 待执行命令 | 每设备 32 | 沿用既有队列限制 |
| 活动普通调用 | 每设备至多 4 | 同项目有副作用至多 1；只读可并行 |
| 普通脚本 timeout | 120 秒，单次上限 600 秒 | 沿用 Bash 现有语义；长驻必须显式后台模式 |
| 未在线等待 | 至多 30 秒 | 不无限占用会话执行线程 |
| 开始许可有效期 | 10 秒、单次 | 过期重新核验，不能重跑已 started 命令 |
| 运行心跳/失联存活期 | 10 秒 / 30 秒 | 失联后封锁新调用，存活期后终止进程树 |
| 单次内联 stdout/stderr | 合计不超过控制帧剩余预算 | 标记截断；有界完整日志仅本机保存并获权分块查询 |
| 预览字节 | 单次至多 16 MiB，文本分页更小 | 复用现有文件类型能力，不无限读盘 |
| Skill 包 | 传输 64 MiB、展开 256 MiB、至多 10,000 文件 | 拒绝链接/越界/展开炸弹；不足时明确限额，不忽略资源 |
| 文件传输 | 沿用既有 transfer/file/job 限制及硬配额 | 技能包还须满足更严格包限制 |
| worker 内存 | 默认 1 GiB，部署可收紧 | 必须实际强制并验收，不能只在参数中声明 |
| 本机回执保留 | 终态至少 7 天且长于命令重投窗口 | unknown 未处理不得过早回收；无无限期全文日志 |

文件大小、磁盘空间、解包预算与任务取消需实际实现。未能强制的限制不能在 meta 中声称已支持；平台参数经探针及测试调整后同步机器可读契约和文档。

## 6. 产出与资源引用

```json
{
  "source": "desktop",
  "artifact_id": "artifact_example",
  "device_id": "dev_example",
  "workspace_id": "ws_example",
  "run_id": "run_example",
  "tool_call_id": "call_example",
  "relative_path": "output/开票清单.xlsx",
  "file_name": "开票清单.xlsx",
  "kind": "office",
  "size": 20480,
  "source_version": "opaque-version-example"
}
```

客户端实际路径映射不主动进入服务端 artifact。source_version 由文件身份、大小、修改状态与必要摘要生成，不靠用户文件名唯一性。预览前再次验证；变化就标记变化。大文件摘要计算不得阻塞 UI。

服务器附件用于本机任务时，资源准备返回 `(resource_id, version, digest)`；客户端先下载到本轮临时输入目录并校验，再向工具提供执行端位置。读取远程 MCP 返回的原生引用时同理，不把 `server:/...` 当本机路径。

## 7. 主要错误

沿用已有标准错误，以下缺失项在 v2 schema 中增补，页面提供中文原因与恢复入口：

| 错误 | 含义/恢复 |
|---|---|
| `feature_unavailable` / `protocol_incompatible` | 未开放或版本不兼容，升级/启用对应能力 |
| `stale_context` / `grant_revoked` | 绑定或授权失效，重新选择连接 |
| `permission_denied` / `approval_required` | 当前资格不足或现有审批未满足 |
| `device_offline` | 目标设备不在线，不能改派其他机器 |
| `runtime_unavailable` / `dependency_missing` | 本机执行环境缺失，显示确切缺项 |
| `incompatible_skill` / `resource_unavailable` | 技能资源或平台不适配 |
| `command_conflict` / `file_changed` | 命令 ID 摘要冲突或文件版本变化 |
| `deadline_exceeded` / `limit_exceeded` | 截止或资源上限触发 |
| `cancelled` | 已确认停止，可能有部分文件 |
| `outcome_unknown` | 核对原文件/回执，不自动重跑 |

不存在与路径无关的“成功空结果”兜底。错误日志只记所需关联 ID、错误码、工具和阶段，不记录认证秘密、完整环境或无关文件内容。
