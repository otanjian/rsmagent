# 阶段 9 证据：非执行路径的选择与绑定（任务 9.3）

日期：2026-10-13。任务原文：*先验证非执行choose→bind、取消和迟到回调，无需先开放脚本；完整A01/A02在阶段11一次验收，复用同份原生证据，不重复录制。*

## 1. 覆盖对照（既有套件，本机实跑）

`tests/test_desktop_local_selection.cjs` —— **29 passed / 0 failed**：

| 任务要素 | 对应用例 |
|---|---|
| choose→bind 正常路径 | "a selection walks idle -> selecting -> ready and commits **exactly once**"；"the picker is told which authorization the user is about to make"；"the real bridge primitive activates the root the service committed"（bind 步） |
| 取消 | "cancelling reports cancelled and leaves the previous selection in effect"；"cancelling the very first attempt leaves nothing in effect"；"an empty path list or a null result counts as a cancel" |
| 迟到回调 | "every attempt bumps the generation, **including cancelled ones**"；"an older attempt resolving after a newer one started is discarded"；"a superseded attempt does not overwrite the newer attempt state"；"a superseded attempt does not run validation or commit"；"a new attempt during preparation supersedes it and releases its grant" |
| 无需先开放脚本 | 整个套件只走选择/绑定面（`purpose: 'project-execution'` 的 choose→commit），不读取执行面开关；另两条断言"the recorded state never carries the absolute path"与只读用途语义 |

配套：`tests/test_desktop_project_execution_grant.cjs` **10/0**（授权/绑定）、`tests/test_desktop_directory_identity.cjs` **9/0**（目录身份稳定性）。

## 2. 范围说明（与任务一致）

完整 A01/A02 的真实原生验收（真实桌面 + 真实本机后端、清空安装标识后首次选目录、重启后再次选择）**放在阶段 11 一次验收**，复用同一份原生证据，不在本阶段重复录制——本阶段只做"非执行路径"的可重复验证，已如上完成。

## 3. 判定

**9.3 勾选**：choose→bind、取消（含"取消也推进代际"）、迟到/被取代尝试的丢弃均有直接用例并通过；这些用例不依赖执行面开关（"无需先开放脚本"）；完整 A01/A02 按任务要求留给阶段 11 真机。
