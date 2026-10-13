# 基线证据（落盘轮次）

## 记录范围

本文件记录"把需求补进基线"这一步产生的可复核输出。**它不表示功能已实现或已验收**：验收矩阵 A01–A33（见归档 change `2026-10-01-align-desktop-project-execution-with-master/acceptance.md`）在本 change 中**全部未执行**，`accepted` 在证据齐备前保持关闭。

## 1. 基线的真实状态（本 change 的动机）

在落盘前，`openspec/specs/` 中缺失以下 4 个能力规范；它们在主规范里不存在，只留在归档 change 的增量中：

```
python scripts/check_change_deltas.py 2026-10-01-align-desktop-project-execution-with-master
FAIL (applied): 12 problem(s)
  - desktop-execution-delivery: capability spec is missing from openspec\specs
  - desktop-project-artifacts: capability spec is missing from openspec\specs
  - desktop-project-execution: capability spec is missing from openspec\specs
  - desktop-skill-runtime: capability spec is missing from openspec\specs
  - execution-isolation: MODIFIED '<标题>' is missing applied scenarios: [...]
  - scoped-project-browser: MODIFIED '<标题>' is missing applied scenarios: [...]
  - scoped-project-browser: ADDED '<标题>' was not applied to the main spec
```

（中文标题在控制台按 GBK 显示为乱码，此处以占位 `<标题>` 代替；完整原文以脚本输出为准。）

结论：该 change 虽已归档（代码已落地），其需求**从未并入主规范**，因此当前既无验收口径也无归档承载。本 change 以新增增量把基线补齐。

## 2. 本 change 的增量校验

```
python scripts/check_change_deltas.py accept-desktop-local-project-execution
FAIL (proposed): 3 problem(s)
  - agent/memory/conversation_store.py: marked seam:conversation-store but this change never names it
  - agent/tools/scheduler/integration.py: marked seam:scheduler but this change never names it
  - tests/test_scheduler_web_update.py: marked seam:scheduler but this change never names it
```

**delta 半边无 finding**：没有 `MODIFIED` 标题与基线失配、没有 `ADDED` 标题已存在或跨能力重复。

上述 3 条 finding 全部来自上游同步基线的 `seam:` 行（`scripts/conflict-baseline.txt` 第 50/53/90 行），属于 `fork-decoupling-and-tenant-hardening` / `adopt-upstream-web-split` 等基线 change 的范围，**与本 change 无交集**：本 change 不触碰 conversation store、scheduler 或 `tests/test_scheduler_web_update.py`。仓库既有做法（见 `openspec/changes/add-workspace-panel-upload-and-delete/evidence.md` §6.1）是不为其改写文档以"刷绿"检查。故本 change 如实保留该输出。

## 3. 已落盘的产物

```
openspec/changes/accept-desktop-local-project-execution/
  .openspec.yaml                      schema: spec-driven / created: 2026-10-09
  proposal.md
  design.md
  tasks.md
  specs/desktop-project-execution/spec.md      (ADDED, 7 requirements)
  specs/desktop-skill-runtime/spec.md          (ADDED, 5 requirements)
  specs/desktop-execution-delivery/spec.md     (ADDED, 7 requirements)
  specs/desktop-project-artifacts/spec.md      (ADDED, 6 requirements)
  specs/scoped-project-browser/spec.md         (MODIFIED ×2 + ADDED ×1)
  specs/execution-isolation/spec.md            (MODIFIED ×2)
  evidence/baseline.md                         (本文件)
```

6 份增量逐字取自归档 change `2026-10-01-align-desktop-project-execution-with-master/specs/`；归档目录**未改动**。

## 4. 尚未执行

- A01–A33 全部未执行；`evidence/` 中除本基线文件外无实测输出。
- 阻塞选择目录的文档代际竞态（`desktop/src/main/remote/container.ts:136-145` + `remote-preload.ts:27-64` + `host-bridge.ts:242-244`）**尚未修复**，当前真实桌面仍可复现 `the calling document belongs to an earlier generation`。
- `auth/capability_matrix.py`、`config.py` 默认值、`contracts/desktop/v2.json` **均未改动**。
