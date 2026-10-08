# 归档记录（2026-10-08）

## 归档裁定

用户在已获知各 change 进度后明确要求「尚未实施的 change 外，其他几个都归档」。本项按该要求进行带未完成任务的归档，schema 为 `spec-driven`。

归档时完成 **7/8**，保留 **1** 个未勾选任务。规划文档完整不代表实施或验收全部完成；归档仅结束本 change 的活动管理状态，不改变能力开关、运行环境或任务事实。

## 规范同步

本次已将以下 delta 同步到正式规范：`account-administration`, `console-information-architecture`, `tenant-management`。已有要求按 requirement 合并，保留其他要求及同批 change 的增量。

## 未完成事项

剩余 3.1 为相关回归及 classic/split 真实浏览器验收。保留当前管理员卡片的账号启停与重置、全局影响提示和最后管理员保护要求；归档没有执行账号操作。

- 3.1 运行相关 Python/Node/i18n 及既有账号启停/重置回归；以测试账号在 classic/split 验证两个按钮、跨租户影响说明、最后管理员拒绝、密码结果和隐藏菜单，证据不保存密码。

完整任务及历史实施记录见 [任务清单](tasks.md)。历史文档中“进行中”或“尚未归档”等表述属于原阶段记录，当前归档状态以本文件为准；未验证部分继续有效。

## 本次操作范围

本次仅归档、同步规范并修正归档证据路径；未重跑业务验收、部署服务或变更真实业务数据。原始证据保持其记录时的候选、平台与环境范围，不因归档扩大通过结论。

## 归档校验

本 change 的归档增量已通过 `check_change_deltas.check_applied` 校验，涉及的正式规范通过 OpenSpec 严格校验。原有 requirement 未丢失、原始证据内容保留、未勾选任务数量不变，`git diff --check` 通过。批量归档完成后仅保留 `add-desktop-local-runtime-with-enterprise-services` 为活动 change，其文件内容保持不变。
