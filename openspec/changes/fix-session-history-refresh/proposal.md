## Why

会话已经保存后，新消息成功、回复结束和自动标题保存仍未刷新工作台侧栏，导致最近会话长期显示旧记录；后台回复还会跳过标题处理。现有 `_refreshHistoryList()` 已能更新侧栏和完整历史页，本次补齐它的调用及后台完成处理即可。

## What Changes

- 在实际消息保存确认、回复完成、自动标题保存后调用既有历史刷新入口；最近会话由收起变为展开时重新读取。
- 将普通会话的历史更新和首次标题处理放到 SSE 的显示保护之前，复用现有请求缓冲保留并消费首次标题信息；标题请求明确原所属智能体。
- 沿用现有请求序号，补齐身份／租户结果校验及侧栏行内编辑期间的延后刷新，防止增加刷新后引入串身份或丢失输入。
- 未发送消息的新对话继续只准备前端会话 ID，不新增侧栏草稿行、同步状态或后端空记录；已保存预览数量、排序、搜索、归档和编码标题来源保持既有规则。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `session-history-workbench`：明确已保存会话及时刷新、后台完成更新及必要的上下文／编辑保护，不改变历史展示结构。

## Impact

- 主要修改实际装载的 `channel/web/static/js/console.js` 中现有发送、SSE 完成、标题生成和侧栏刷新入口。复用 `_refreshHistoryList()`、`streamBuffers` 及现有请求序号，不新建协调模块、事件总线、通用队列或单飞机制。
- 接口和数据唯一归属不变：业务历史仍由目标智能体的 ConversationStore 管理；普通标题复用现有接口，编码标题继续归 OpenCode。不修改 AuthSession、权限、存储结构或后端协议。
- 以现行 `session-history-workbench`、`agent-chat-launch`、`web-console-frontend-modules` 为基线。本次是既有公共行为的局部修复，不引入 fork 专属功能，也不把全站模块拆分作为前置；不得复制整函数或重复装载同名实现。
- 同 Web 资源的 Desktop 容器随静态资源修复受益，不扩展到独立 Desktop renderer 或桌面登录生命周期。与 `fix-desktop-relogin-session-sync` 只有文件可能重叠，实施时保留其现有改动，不设置整体交付依赖。
- 无迁移、回填、新文案、新样式或新增开关。补充聚焦本次故障的函数回归及实际 Web 页面验证；规划完成不代表代码已修复。
