# 规划依据与问题追踪

日期：2026-10-13。此文件记录规划依据，不是实施或运行验收证据。

## 来源与边界

- 原报告：`docs/design/windows-desktop-verification-2026-10-13.md`，记录一次 Windows 开发模式、125% DPI 的巡检。
- 对照源码：日志包装、Python 探测、错误页、记忆事务、安全文件访问、workspace 文案、remote 模式、桌面 metadata、独立设备网关及代理样例。
- 用户补充讨论：服务端在远程服务器时，本机作为客户端使用，需明确部署条件、网络、登录和本机能力边界。
- 原报告“CLI 未安装”式环境记录及技能中的陈旧环境描述不能覆盖当前实测：本机 openspec CLI 可读取此项目。

## 报告项与交付映射

| 报告项 | 判断 | 设计 / 任务 |
|---|---|---|
| 4.1 日志 EPIPE 风暴 | 代码确认递归风险，原报告提供历史运行证据 | D1 / 阶段 1 |
| 4.2 Python 9009 与缺诊断 | 代码确认仅文件存在检查，已有错误页需扩展 | D2 / 阶段 2 |
| 4.3 记忆 403 | 包装位置确认；底层异常未定位 | D4 / 阶段 3 |
| 4.4 本机目录平台死路 | 本期 Windows 能力未开放，保持限制 | D5 / 阶段 4 |
| 4.5 device_error 被折叠 | 三处映射确认 | D5 / 阶段 4 |
| 4.6 缺依赖、继承变量 | 必要/可选依赖分类，启动前清理变量 | D2 / 阶段 2 |
| 4.6 第二次 spawn | start 已有 guard；来源未定位 | D3 / 阶段 5 |
| 4.6 窗口落地差异 | 按既有路由契约定向复现 | D7 / 5.3 |
| 4.6 截图挂起 | 巡检超时，不直接认定产品缺陷 | D7 / 5.4 |
| Windows 本机执行对齐 | 后续独立建设，非本次开启范围 | tasks Workflow follow-up |
| 远程部署和连接 | remote 跳过本机 Python；网关独立，需同源代理 | D6 / 阶段 6 |

## 与既有 change 的协调

- 原 `accept-desktop-local-project-execution` 已并入：当前阶段 8–15 负责执行基线补齐与验收，本期仍不启用 Windows；原文保存在 `merged-source/`，历史证据在 `local-project-execution/`。
- `add-desktop-local-runtime-with-enterprise-services`：负责 enterprise-local 和同步，不能与此次 remote 服务器执行模式混为一谈。
- `optimize-console-login-startup`：代理 gzip、缓存与账号检查的已做改动须保留，与 connect 专用路由合并验收。
- 已归档 change 仅可提供历史证据，不能用完成标记替代本次真实 Windows/NSIS/远程验收。
