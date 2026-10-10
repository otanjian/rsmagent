# 本会话修改覆盖清单

日期：2026-10-10（Asia/Shanghai）。本次补记以本会话实际修改和操作为边界，不把 Git 合并引入的全部远端代码都标记为本会话实现。

## 提交与归属

| 提交/操作 | 本会话内容 | OpenSpec 归属与状态 |
| --- | --- | --- |
| `c5a3fe6e` | 工作空间/模型菜单移除首页高度与方向覆盖；提交本地私有/共享统一发布治理方案、设计、12 个 capability delta、36 项任务及演示 | 菜单补入本 change；治理文档在 [add-shared-agent-release-governance](../../add-shared-agent-release-governance/proposal.md)，36 项任务仍待独立验收 |
| `ff8c7ca2` | 合并 Gitea `7f303f25`；处理治理文档 add/add 冲突，保留远端已有运行代码，旧共享方案移入历史证据 | 治理旧范围与新范围的差异见 [merge-notes.md](../../add-shared-agent-release-governance/evidence/legacy-shared-release/merge-notes.md)，不沿用旧勾选状态认定新方案完成 |
| `d797d8b2` | 把被 `README*.md` 忽略规则影响的合并说明改为可跟踪的 `merge-notes.md` 并修复引用 | 已在原治理 change 记录，无需另建治理 change |
| `f550c5d9` | 工作空间菜单参考 master 模型菜单，在工具栏上方展开；更新两份 CSS 与布局回归 | 本 change：工作空间/模型菜单 |
| `95f633d7` | 智能体菜单取消首页高度压缩；手机端保留宽度约束并靠近工具栏；补充长短列表/不同页面状态检查 | 本 change：智能体菜单 |
| `3463be15` | 文件夹按钮悬停路径仅显示最后两级，两份 JS 同步 | 本 change：紧凑目录提示 |
| `27fe5b6b` | `/`、`@` 菜单移除首页向下展开覆盖，更新旧断言 | 本 change：指令/引用菜单 |
| 服务器版本核验、重启和部署 | 核对工作树、分支、远端 SHA、目标进程与实际静态资源；需要更新 Python 的整合部署重启应用及重载代理 | 既有 `production-deployment` 运维范围的操作记录，见 [验证记录](verification-2026-10-10.md)，不新增平台就绪能力 |

## 与远端合并带入内容的边界

`git diff c5a3fe6e^..HEAD` 包含合并父分支之前已存在的远端功能，不能直接当作本会话的修改清单。本次核对同时使用 `git show` 的逐提交清单及 `git diff 7f303f25..27fe5b6b`：相对已部署远端基线，本会话产品代码涉及 3 份 CSS、2 份 JS 和 1 份菜单测试，其余为发布治理文档及历史证据整理。

- SAP/RFC/BAPI、Windows SAP 和 OpenCode 品牌内容由远端既有提交带入，已有 `enable-sap-rfc-bapi-channel`、`add-desktop-sap-windows-support`、`decouple-opencode-branding` 等 change。本会话未据此重新声明其验收完成。
- PDF/OCR 工具与测试也来自较早远端 `b0a4333d`，本会话未修改其行为。它不属于输入区修复，也不因本次合并记录产生新的 PDF 产品验收结论。
- 旧共享发布代码同样来自远端，当前统一治理 change 已明确其历史身份和差异；本次不启用治理开关，不把旧测试当作私有/共享统一保护的验收证据。
- `restore-upstream-web-parity-and-sync-gates` 等其他在途 change 保留各自未完成项。本会话修复菜单不代表其全部同步门槛通过。

## 规范冲突的处理

已归档外观 change 汇入主 spec 的“菜单使用输入组下方剩余空间”条款与本次用户确定的向上展开行为冲突。本 change 提供可应用的 MODIFIED delta，并增加不同菜单的锚点和紧凑路径提示条款。主规范其他首页/品牌/配色责任不在本次重写或重新验收范围内。

本次仅补齐 active change。正式归档时再按 OpenSpec 流程同步主 spec，保留已经存在的历史文档和当前治理方案状态。
