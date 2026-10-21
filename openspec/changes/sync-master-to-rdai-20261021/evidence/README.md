# 规划与实施证据

本目录保留原规划依据，并新增本轮候选的实施与验证记录。本轮 Web 验收（含真实模型、工具、文件及服务账号）已完成；最终 rdai 合入记录见 results/delivery-results.json。Desktop 与远端 CI 自动阻断待补；未执行生产部署。

本目录的纯文本日志副本去除了行末空白，命令输出内容、用例结果和退出状态未改；原始副本保留在隔离工作区的 `.sync-test/raw-evidence-logs/`。证据索引及日志已显式纳入 Git，避免仓库通用忽略规则遗漏。

后续按用户调整先验收 Web，桌面测试延期且不阻塞本轮同步交付；具体要求和未验证状态分别记录如下。

| 文件 | 用途 |
| --- | --- |
| `refs.json` | 固定源、交付目标、本地分支、共同祖先和统计；时间为 UTC，用户日期按 Asia/Shanghai |
| `upstream-commits.txt` | 交付目标尚未包含的 770 个上游提交，含 merge commit，不等同于 770 个独立功能 |
| `upstream-files.tsv` | 共同祖先至固定源的全部 876 个路径及状态 |
| `upstream-numstat.tsv` | 同一区间逐文件增删统计 |
| `both-sides-touched.txt` | 双方相对共同祖先均改动的 284 个路径，仅是重叠普查，不是冲突报告 |
| `delivery-ahead-commits.txt` | 规划时远端目标领先本地 rdai 的 12 个提交；全部保留 |
| `inherited-gaps.json` | 从固定远端目标读取的持久清单摘要，历史状态不代表本候选通过 |
| `file-workstreams.tsv` | 876 个路径的初始工作包映射；最终分类见 source-classification.json，配套文件共用能力/测试记录 |
| `planning-validation.md` | OpenSpec strict、delta 基线和文档一致性检查结果，仅验证规划产物 |
| `web-acceptance.md` | 本轮 Web 与实际后端的 WB-01–WB-12 必测行为和放行标准 |
| `desktop-validation-deferred.md` | 桌面构建/原生/D1–D6/双端/安装包验证延期，承接 tasks 第 12 组 |
| `review.md` | 按用户要求审核前端 master 优先及过度设计的发现、来源依据和规划修正 |

已完成隔离排练、候选合并与适配、依赖准备、本地 Web 构建、后端回归及真实浏览器验证。真实 Desktop、真实模型相关验收和远端阻断门禁仍未验收；不包含操作者凭据、Cookie 或业务正文。

## 工作包

| ID | 范围 | 重点 |
| --- | --- | --- |
| W01 | Web 服务、页面和前端资源 | 真实 fork 装配、路由/方法授权、产物/菜单/API/TLS、配置失败恢复 |
| W02 | Desktop | 本轮审查/整合主进程、preload、renderer 及合同代码；构建、原生测试、D1–D6 归入后补验证 |
| W03 | Agent、Bridge | 会话/索引/上下文、记忆知识、工具/MCP、技能、调度、权限和多 Agent |
| W04 | 模型与语音 | Responses/流生命周期、用量缓存、错误及取消、模型发现、文件回收 |
| W05 | IM/其他渠道 | 实例/发送者/tenant 身份、访客限制、附件、重放/去重、恢复 |
| W06 | 通用组件/CLI/插件 | 原子写/目录替换、配置、备份权限、进程、文档解析及既有产品策略 |
| W07 | 测试 | 保留源行为断言并映射实际 rdai 消费方；必要用例逐名执行 |
| W08 | 文档 | 品牌/链接、真实状态、版本及升级和 API 客户端迁移说明 |
| W09 | 根配置/构建/部署 | 新配置归属、默认值、锁文件与平台依赖、启动及交付目标 |

单个路径可能跨多个能力，初始工作包只确定主要审查位置。实施时从实际调用图补充关联，不允许以分组完成代替逐行为验收。

## 实施结果入口

- [实施状态与未完成事项](implementation-status.md)：代码落点、基线失败、真实模型凭据阻塞和延期边界。
- [固定候选](results/candidate.json)、[完整本地同步门禁](results/required-gate/report.json)、[浏览器 39 场景](results/required-gate/browser/results.json)。
- [后端汇总](results/backend-summary.json) 和 [逐名 JUnit 结果](results/backend-results.xml)：348 个测试文件，保留失败/跳过。
- [Web 构建来源及资源摘要](results/build-info.json)、[核心回归](results/core-final.log)、[源前端回归](results/frontend-final.log)。
- [API 升级与恢复](api-and-deployment.md)、[英文 PR 草稿](pr-draft.md)、[活动 change 重叠](active-change-overlap.json)。
- [基线绑定](results/baseline-bindings.json)、[原目标前端结果](results/agent-workspace-baseline.log)、[当前相同遗留失败](results/agent-workspace-final-2.log)。

本候选用于保留 merge 历史的草稿评审；桌面后补任务、历史缺口和最终交付任务保持原有状态，草稿发布不代表真实验收通过。

- [现用翻译/外观检查](results/locale-current-final.log)、[历史翻译基线](results/legacy-i18n-baseline.json)、[CI 状态](ci-status.md)。
- [Gitea 只读核查](results/remote-ci-status.json)：无分支保护规则，目标 CI 排队且未分配 runner；[真实模型重试](results/real-provider-status.json)：仍为 401，未记录凭据。
- [草稿 PR #2](https://rd.rsmxm.com.cn/Gitea/rsmai/rsmagent/pulls/2) 与 [草稿交付记录](results/draft-delivery.json)：服务器确认 draft，保留双亲 merge 历史；尚未正式验收或合入 rdai。

真实模型及文件闭环：`results/real-provider-status.json`、`real-chat-result.json`、`real-file-browser-result.json`、`real-api-result.json`。最终受影响检查：`results/affected-final/`（376 项通过）。

PR #2 已合入 `c7b981b0068e817cd0cef4fab28f19f0385e2bf1`，合入后 Web 真实模型与文件冒烟通过。任务 53/58；未完成项仅为远端 CI 阻断证明与 4 项 Desktop 延期验证。
