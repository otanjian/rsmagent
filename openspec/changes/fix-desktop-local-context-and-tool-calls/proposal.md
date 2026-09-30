## Why

桌面选择本机目录后仅更新标签，没有把目录接到当前会话；模型因而继续使用服务器默认工作区。同一会话还出现 DSML 文本代替实际工具调用，需要修复目录读取链路，并让工具协议异常明确失败。

## What Changes

- 复用已有设备、配对、绑定和目录授权接口，补齐选择器到实际 IPC 的调用；绑定确认后才显示目录可用。
- 聊天请求携带当前本地目录的非秘密引用，服务端校验后注入本轮上下文；`client_files` 使用该引用读取，服务器工作目录维持现有规则。
- 补齐已有设备命令的实际消费，并等待真实结果；本次修复列目录、元数据、搜索和文本读取，排队回执不能作为成功结果。
- 先核对实际模型请求与响应，修正 tools/结构化调用的具体问题；对仍出现的“无标准调用但输出 DSML 调用封装”返回协议错误，不执行文本命令。
- 复用既有失败事件完成最终回复与错误状态展示；增加必要的回归和当前桌面实测，不新增通用流式协议框架、自动纠错重试或历史改写。

## Capabilities

### New Capabilities

- `desktop-session-local-context`：本机目录选择到当前聊天实际读取结果的一致性要求。
- `model-tool-call-integrity`：本次 DSML 异常的识别与失败反馈，保持正常结构化工具调用兼容。

### Modified Capabilities

无。既有 `desktop-tenant-context`、`scoped-project-browser` 和 `agent-runtime-capability-enforcement` 的授权、路径及工具约束继续适用。

## Impact

- **代码接缝**：桌面 remote host IPC 与已有 local-files/设备连接模块、Web host adapter 与当前实际加载的 console、聊天请求/Agent 上下文、`client_files`、DeepSeek 适配与 Agent 流结束处理。
- **数据归属**：绝对路径和 grant 留在桌面；身份、binding/workspace、command 及结果沿用服务端已有记录；页面只保存当前引用。没有新增数据库表、全局“当前目录”或独立执行账本的计划。
- **依赖**：复用 `add-desktop-remote-web-workbench` 的身份、设备、绑定、命令、路径防护及开关。补齐本次读取所需的真实装配，不重新交付其传输、发布、配额、解析器、跨平台发行流程。既有安全门槛仍有效，能力不足时明确不可用，不能仅隐藏入口就宣称修复完成。
- **协同**：`use-personal-workspace-for-shared-agents` 继续决定服务器默认输出目录；本次本地输入引用不改变它，也不以其全部完成为前提。
- **范围**：完整二进制文件物化、全平台打包认证、历史消息治理和新的并发版本协议不属于本次修复；现有相关行为保持兼容。
- **状态（2026-09-29）**：第 1–3 组实现已完成并有针对性回归通过（见 `evidence/phase-1.md`、`phase-2.md`、`phase-3.md`）。**未完成**：4.3 真机实测（选择目录→清单→设备读取→最终回答）与 1.2/4.2 的真实模型出口冒烟——本环境缺少同源设备网关部署与真实 provider 调用条件，故不据此开启任何部署开关，详见 `evidence/verification.md`。
