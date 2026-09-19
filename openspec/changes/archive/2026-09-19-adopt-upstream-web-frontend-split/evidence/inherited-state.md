# 承接状态（从 `adopt-upstream-web-split` 继承的输入）

本 change 的 Phase 3 工作不是从零开始：`adopt-upstream-web-split` 已完成其 4.1、
4.2、4.4a、4.4b 四项（分析、移植器、裁定工作清单、独立校验工具），产出随本 change
交付。本文件记录继承边界，使「哪些已完成、哪些是待办」不依赖阅读另一个 change。

## 输入（已在位，可直接重跑）

| 工具 | 作用 | 实测口径（`base=e5e2a52d`, `upstream=8f1b19f1`, `fork=HEAD`） |
| --- | --- | --- |
| `scripts/migration/analyze_frontend_divergence.py` | 归一化 diff、变更簇归属、上游模块定位 | 归一化是前提：按原样行 diff 会把 `console.js` 报成「2 hunk / 18671 增行」，实际是 fork 改了空白与缩进 |
| `scripts/migration/port_frontend.py` | 按上下文再锚定并拼接 fork 原文，产出 `static/js/fork/**`、`static/css/fork/**` | `console.js` 375 簇移植 277、待裁定 98；`console.css` 79 簇移植 71、待裁定 8；产出 25 个 JS 模块（18597 + 4169 行）全部 `node --check` 通过 |
| `scripts/migration/build_frontend_adjudication.py` | 生成人工裁定工作清单 | 109 处（JS 98 + CSS 8 + 跨边界 3），跨 23 个模块；63 处附上游同名符号内容 |
| `scripts/migration/verify_frontend_port.py` | 独立复核「零未交代」 | 以 **fork 独有行**为准：`console.js` 5255 行 + `console.css` 1361 行 = 6616 行；并自行复跑 `node --check`，不采信移植器自述 |

产出位置：`evidence/port_frontend_clusters.json`、`evidence/port_frontend_worklist.json`、
`evidence/frontend_adjudication.{json,md}` 随仓库交付（裁定清单是「被延后增量的上界」的依据，
必须可引用）；`evidence/fork-frontend/` 的生成模块**不入版本控制**，由上面的命令按需重现
——它的空白逐字继承自 fork 单体，清理它会破坏「逐字切片」的保证，而它本身是可重现的产物。

重跑命令（`FORK_MIGRATION_WORKDIR` 指向本 change 的 `evidence/`）：

    FORK_MIGRATION_WORKDIR=openspec/changes/adopt-upstream-web-frontend-split/evidence \
    FORK_BASE_REF=e5e2a52d FORK_UPSTREAM_REF=8f1b19f1 FORK_FORK_REF=c6eb33db \
      ./.venv/bin/python scripts/migration/port_frontend.py
    FORK_MIGRATION_WORKDIR=... \
      ./.venv/bin/python scripts/migration/build_frontend_adjudication.py

移植器确定性：同输入重复运行产出逐字节一致，故「是否有内容漂移」可回答。

## 已确立的约束（继承自父 change 的 D5，本 change 不再重新论证）

1. **不得「之后装载 + 重声明」**：上游前端是无打包器的共享全局作用域经典脚本，同一
   顶层 `const`/`let` 在两文件声明即 `SyntaxError`、整页白屏。已实测：25 个模块中
   早期用搜索锚定的版本有 9 个 `node --check` 失败（重复 `let`、括号不平衡），故改
   为按 base 切片与上游模块对齐**推导**落点。
2. **禁止整函数自动移植**：对 JS 侧待裁定项的大部分可机械适用，但会整体覆盖上游同名
   函数、静默丢弃上游在该函数内的改动，正是要消除的失血方向（父 change 在旧基线上
   测得 87 处中 56 处可机械适用；刷新到 `c6eb33db` 后为 98 处，该比例未重测）。
3. **跨模块边界的 fork 编辑不切分**：按边界切分会切断语句（实测产出
   `function f() { } }`），此类编辑转人工裁定（3 处）。
4. **不原地编辑上游视图模块**：上游 `static/js/{core,chat,views}/*`、`static/css/*`
   全部保持未改动。

## 当前 fork 独有行基线（「零未交代」的分母）

`console.js` 5255 行、`console.css` 1361 行，共 6616 行：3508 已入移植产出，3108 在
裁定清单上。本 change 完成后该分母必须归零——即全部由产出模块承载或由裁定记录说明
处置。

**基线刷新记录（必须连同 SHA 一起读）**：本 change 冻结时，父 change 的收口提交
`c6eb33db` 又改动了 `channel/web/static/js/console.js`（三个搜索提供方的凭据弹窗接线，
+93/-…），故父 change 在其 `evidence/09-*.md` 里记录的旧口径（98 处 / 6583 行）是
**当时**的实测值，不再代表本 change 的输入。上表与本节数字由下列命令在
`base=e5e2a52d`、`upstream=8f1b19f1`、`fork=c6eb33db` 上重跑所得：

    FORK_MIGRATION_WORKDIR=openspec/changes/adopt-upstream-web-frontend-split/evidence \
    FORK_BASE_REF=e5e2a52d FORK_UPSTREAM_REF=8f1b19f1 FORK_FORK_REF=c6eb33db \
      ./.venv/bin/python scripts/migration/{port_frontend,build_frontend_adjudication,verify_frontend_port}.py

结论：父 change 的 `FORK_FORK_REF` 默认 `HEAD`，故**任何人重跑前都应显式指定这三个
ref**。docs-only 提交不改变分母（已实测：`HEAD=b07eaf5d` 与 `fork=c6eb33db` 同为
6616），风险只在 fork 前端文件被再次改动时兑现——届时三元组必须一并更新。
`verify_frontend_port.py` 在该三元组上报 `PASS: 6616/6616`、`0 UNACCOUNTED`、
`node --check` 全通过。

## 父 change 的前端分歧记录

`openspec/changes/adopt-upstream-web-split/evidence/17-frontend-phase3-pending.md`
记录了 Phase 2 结束时「合并带来了什么」与「fork 服务什么」的差距，以及三处因该分歧
而加的 skip（`tests/test_web_console_assets.py`、`tests/test_web_console_routing.py`、
`tests/test_web_console_update.py::test_frontend_contract`）。本 change 的
`evidence/deferred-upstream-frontend.md` 是同一件事的逐条增量清单，两者互补：前者记录
**差距的性质**，后者记录**差距的内容**。
