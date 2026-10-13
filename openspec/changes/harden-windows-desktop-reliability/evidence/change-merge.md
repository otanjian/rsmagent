# Change 合并记录

日期：2026-10-13。根据用户明确要求，将 `accept-desktop-local-project-execution` 并入 `harden-windows-desktop-reliability`，核验后删除原 change 目录。此操作是规划内容合并，不是执行验收或归档。

结果：迁移文件哈希、需求/场景保留、任务编号和完成状态检查通过，OpenSpec strict 与仓库 delta 检查通过，原目录已删除。合并后共 10 份规范、45 条需求、82 项任务（继承 1 项已完成）。

后续精简：本文件保留合并当时状态和编号，历史材料不改写。用户授权调整后，当前任务为55项；9.1仍继承完成，原15.4复验合入9.2，原14.7对应当前14.6。其余以当前tasks.md和planning-validation.md为准。

## 内容归属

- 原目录 13 个文件（metadata、proposal、design、tasks、6 份增量、3 份证据）完整保存在 `merged-source/`，SHA-256 见 `merge-manifest.json`。这些快照按历史原文保留，其旧路径、任务编号、环境描述和当时结论不是当前计划。
- 四个新增执行 capability 与 `execution-isolation` 增量迁入当前 `specs/`；`scoped-project-browser` 将原 MODIFIED 和 ADDED 与现有两条能力提示 ADDED 合并，完整保留原 requirement/scenario。
- 三份运行/基线历史证据在 `local-project-execution/`，逐字保留且不提升其证明范围；原 generation 探针仍需在当前代码重构建后复跑。
- 原 design/proposal 的当前有效方案整合入 proposal 与 design D8；原文及当时未决选项保存在快照。错误文案拆分已由 D5 决定，Windows 仍不启用，脚本面与文件面各自验收后开放。
- 原验收来源归档文件未修改；当前完整 A01–A36 计划为 `../acceptance-local-project-execution.md`，明确 Windows 的执行正向项不适用与平台拒绝/普通业务验收范围。
- 原阶段 1–8 映射当前阶段 8–15；任务小编号保持（原 8.2 归档任务移为 Workflow follow-up，原 8.3 映射 15.2）。原 2.1 → 当前 9.1 保留已完成状态；其余原任务仍未完成。新增 14.7 补 A34–A36，15.3 补当前文档与证据索引，15.4 补当前构建复验。
- 巡检报告的原 change 路径已指向当前 proposal 和迁移证据；其他历史快照中的旧名称只用于追溯，不作为活动依赖。

## 合并后边界

稳定性及普通远程连接为阶段 1–7；受支持平台的本机项目执行为阶段 8–15。两个工作流可以分阶段交付，完整结项必须满足各自任务及证据。合并不开放 Windows 执行，不实施 enterprise-local，不修改主规范、产品代码、能力开关、业务数据库或已归档历史。
