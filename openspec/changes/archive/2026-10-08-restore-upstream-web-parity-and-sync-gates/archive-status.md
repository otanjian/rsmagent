# 归档记录（2026-10-08）

## 归档裁定

用户在获知实际进度为 44/47、剩余 5.1、5.9、7.3 尚未验收后，明确要求归档 `restore-upstream-web-parity-and-sync-gates`。本次按该要求归档，状态为**带未完成验收项归档**，不代表全部实施或生产验收通过。

以 `codex/restore-upstream-web-parity@ce0f87b9b640c0102f4a40ef52807167bbecbc6d` 工作区中的实施记录为依据，保留 44 个已勾选任务和 3 个未勾选任务；当前主工作区原有 0/47 草案已由此完整记录替代。同步 5 份规范：更新 console-route-lifecycle、fork-upstream-decoupling、web-console-frontend-modules，新增 web-console-runtime-parity、web-console-source-update。

## 已交付记录

- Gitea [PR #1](https://rd.rsmxm.com.cn/Gitea/rsmai/rsmagent/pulls/1) 已合并到 `rdai`，合并提交 `be88ae9d0e600ba7746cb7d3b58cd96e245d859c`。
- 本机部署回执 `/Users/jiantan/.codex/deployment-receipts/rsmagent/rdai-be88ae9d.json` 记录：2026-10-08 07:47:35 +08:00 已部署至远程 `rdai` 的 `C:\rdai\rsmagent`，健康检查 200，深链接及刷新冒烟通过，配置和数据保留。
- 部署时原有执行隔离就绪检查仍失败，`/api/ready` 返回 503；本次归档没有复测、修复或改变该状态。
- 原始本地测试结果见 [实施证据](evidence/implementation/README.md)。其中旧 PR 草稿状态、未合并/未部署/不归档表述属于当时的历史快照；后续交付与归档状态以本记录为准。

## 保留的未验收项

| 任务 | 尚缺条件与后续验收 |
| --- | --- |
| 5.1 | 部署者确定受信发布仓库/ref、进程管理器和隔离完整源码实例；已实现的缺配置拒绝逻辑不等于部署配置已验收。 |
| 5.9 | 在受支持的完整源码实例执行真实成功更新及故障恢复；当前 Windows 部署不能替代 Unix 源码更新验收。 |
| 7.3 | 配置可用 CI runner 和分支保护，在真实测试候选注入缺依赖/必要用例跳过，取得阻断及恢复后的通过证据。 |

上述任务继续保持未勾选；`production_acceptance=false` 等历史结论不修改。归档不会启用源码更新能力、补写 CI 成功结果或豁免后续交付门槛。再次推进时应重新核对环境和候选提交，以新的实测证据承接这些任务。

## 归档维护范围

本次仅归档文档、同步规范并更新引用归档证据的路径。能力清单、来源映射和历史清单的路径迁移不改变能力状态；原始测试报告和源码树指纹保持不变，不将历史结果重新标记为当前候选的测试结果。

归档目录中的 `.gitignore` 仅保留此前已跟踪的 README 与日志证据，防止移动后被仓库通用忽略规则漏收。

## 归档检查

- 5 份同步后的正式规范均通过 OpenSpec 严格校验；`check_change_deltas.check_applied` 确认归档中的新增/修改要求及场景已进入正式规范。
- 能力清单和历史清单校验通过；本 change 的来源映射证据路径均可访问。两个工作区的归档记录和 5 份规范一致，活动 change 列表中已移除本项。
- 通用 `check_change_deltas.py` 命令另报全局冲突基线中的 3 个接缝路径未被本 change 点名；归档规范校验单独调用 `check_applied`，未修改全局基线以消除该报告。全量来源映射扫描另发现 `fix-account-memory-management` 的旧活动证据路径缺失，未扩大本次归档范围去改写其他 change。
- 本次未重跑业务测试、源码自更新或远程 CI 验收；历史通过结果的候选范围保持不变。
