# 规划验证记录

- 日期：2026-10-21（Asia/Shanghai）。
- 范围：本记录对应 `add-im-guest-accounts` 的规划与复杂度审查；没有修改业务代码、现行主规范、运行配置或生产数据，提交规划产物不代表功能实现。
- 状态：已完成复杂度审查并同步 proposal、design、6 份 capability delta 和 tasks；20 项实施任务全部未勾选。规划完成不代表运行能力已实现或验收。

## 复杂度审查与调整

| 原设计 | 本次调整及理由 |
| --- | --- |
| 首期完成正式账号关联、转正、租户成员升级 | 移出本期；当前目标只要求 IM 访客可用且不能登录 |
| User 类型 + Membership.access_mode 两层身份访问类型 | 只保留 User.account_type；本期不转正，无需提前改变所有普通账号的租户选择规则 |
| 实例访客关系、blocked/revoked/version/到期、独立撤销证据 | 复用账号/成员 active，访客解绑在原映射保留 active=false；不新增关系表或撤销表 |
| 独立访客列表及按人按实例状态管理 | 复用成员页；单人停用影响本租户所有机器人，按实例关闭用现有渠道访客模式 |
| 第二份 Agent 名单、附件/记忆开关、开户配额体系 | 复用现有目标、角色、Agent 能力和运行配额；保留当前渠道角色交集避免跨实例授权串用 |
| 四阶段、39 项任务、30 项 requirement、98 个 scenario | 两个实施阶段、20 项任务、21 项 requirement、70 个 scenario；保留 MODIFIED 的全部基线场景标题 |

不能一并删掉的必要边界：可信三元组及唯一约束、平台会话签发和消费拒绝、首次发送者不认领创建者、停用不重新开户、真实调用授权与同库审计。

**代码核实后的修正**：`channel/chat_channel.py:69` 读取全局群白名单；`channel/feishu/feishu_channel.py:308` 和 `channel/wecom_bot/wecom_bot_channel.py:161` 将其设为 ALL_GROUP。因此不能宣称已有实例群白名单可直接复用。最终保留第三个最小配置 `guest_group_ids`（默认空，仅私聊），按可信群 ID 匹配；模式、角色和群 ID 共三个字段，无新群管理模块。

**明确取舍**：本期不支持把已自动创建的访客映射转给正式账号；已有成员不会在再次消息时自动补角色；更换渠道角色后权限不足由管理员在原成员页处理。开户成功而后续调用超限时保留访客，避免把已有身份事务和计费事务改造为跨存储事务。

## 已执行检查

| 检查 | 结果 |
| --- | --- |
| `openspec validate add-im-guest-accounts --strict` | 通过 |
| `openspec status --change add-im-guest-accounts` | 4/4 规划产物完成 |
| `scripts.check_change_deltas.check_deltas(Path('openspec'), 'add-im-guest-accounts')` | 通过，无增量问题；MODIFIED 标题存在且保留全部原场景标题，新增标题无重复 |
| proposal capability 声明与实际文件集合 | 完全一致：1 个新增、5 个修改 |
| requirement/scenario 结构 | 21 项 requirement 均有 SHALL/MUST，70 个 scenario 均有 WHEN/THEN |
| 任务编号、状态、占位符及文本格式 | 20 个唯一未勾选任务；无模板占位符；UTF-8、末尾换行、无行尾空白 |
| 当前活动 change 的 capability/requirement 标题交叉检查 | 与 `add-shared-agent-release-governance` 在渠道配置、资源授权两个 capability 相交，没有相同 requirement 标题修改；语义协作要求已记入 design D6 和任务 1.1/5.3 |

## 全仓同步清单检查的范围区别

完整命令 `.venv/bin/python scripts/check_change_deltas.py add-im-guest-accounts` 返回 1。该命令除了规范增量，还无条件要求所选 change 覆盖 `scripts/conflict-baseline.txt` 中的全仓上游同步接缝，报告：

- `agent/memory/conversation_store.py`：`seam:conversation-store` 未在 change 根 Markdown 点名。
- `agent/tools/scheduler/integration.py`：`seam:scheduler` 未在 change 根 Markdown 点名。
- `tests/test_scheduler_web_update.py`：`seam:scheduler` 未在 change 根 Markdown 点名。

已单独调用同一脚本的 `check_deltas` 验证本次规范增量通过。上述三条属于附加的全仓同步覆盖要求，不是增量标题/场景缺失；本次不承担整个上游同步清单，也没有为消除输出而更改该清单或扩大任务。完整命令的非零结果如实保留，不宣称该命令通过。

## 尚未执行

没有执行业务实现、身份库迁移、账号创建、真实飞书/企业微信接入、自动化业务测试或生产开关变更。真实渠道、并发/撤权、登录拒绝、工具权限、迁移/回退等均为 tasks.md 中待实施验收内容。
