# A01–A36 的本机测试覆盖基线（哪些有本地覆盖、哪些只能真机）

日期：2026-10-14。目的：给剩余的真机验收任务一份**可引用的覆盖基线**，并按 14.4 的要求区分"通过"与"不适用/未覆盖"，避免把没跑过的项读作通过。

## 1. 覆盖矩阵（本机实跑）

| A 项 | 本地覆盖套件 | 结果 |
|---|---|---|
| **A01** 首次选目录/重启再选/ID 稳定/取消不建授权 | `test_desktop_installation_identity.cjs`、`test_desktop_local_dir_crypto.cjs`、`test_desktop_local_selection.cjs` | **8/0/1skip、7/0、29/0** ✓ |
| **A02** 快速 A/B、取消、迟到 Promise | `test_desktop_local_selection.cjs`（含代际与迟到回调）+ 9.3 证据 | **29/0** ✓ |
| **A03** 普通 Web 提交绝对路径/伪造 handle/复用他人选择 | `test_desktop_target_resolution_acceptance.py` | **19 + 16 subtests** ✓ |
| **A04/A05** 本机模式选择 + 六工具 | `test_desktop_local_e2e.py`、`test_desktop_remote_execution_acceptance.py` | **15/1skip**、**63/0** ✓ |
| **A06/A07/A11–A13** 技能包/平台缺项/固定技能 | `test_desktop_remote_skill_acceptance.py` 10/0 ✓；`test_desktop_remote_host.cjs` **34/0**；`test_desktop_skill_acceptance.py` **5 失败/4 通过/2 跳过**（见 §2） | 部分 |
| **A08/A09/A10** 附件/知识/切换资格 | `test_desktop_remote_host.cjs`、`test_desktop_remote_execution_acceptance.py`、`test_desktop_target_resolution_acceptance.py`、`test_desktop_web_pages.py`（25 通过/3 失败，§2） | 部分 |
| **A14–A20** 幂等回执/强杀/冲突/退出码/取消/断网/双 alias | `test_desktop_device_execution.cjs` **29/0**、`test_desktop_command_runner.cjs` **31/0**（连跑 4 次全绿；早前 1 项为时序抖动，见 §2）、`test_desktop_local_execution.cjs` **36/0/23skip** | ✓ |
| **A17/A26/A27** 参数作用域/OS 边界拒绝 | `test_desktop_local_execution.cjs` 36/0、`test_desktop_execution_v2_contract.py` 63/0 | ✓ |
| **A21/A22** 文件树/搜索/预览/编辑、跨项目切换 | `test_desktop_project_refresh.cjs` **22/0**、`test_desktop_project_source.cjs` 20/0/17skip、`test_desktop_project_watch.cjs` 6/0/22skip | ✓ |
| **A23** 设备离线/重启后候选恢复 | `test_desktop_local_files.cjs` **12/0**（清空授权**不动候选**、取消不产生授权、租户/服务器变更撤销对应授权）、`test_desktop_local_files_bridge.cjs` **6/0**（断连清授权但**不删候选**、选目录**记住候选**）、`candidates.ts` 明述"candidates are not active grants…a restart shows the candidate list and asks the user to reconnect" | **逻辑层已覆盖**；设备离线与跨设备历史查看需真机 |
| **A24/A25** 预览沙箱/导出边界 | `test_desktop_isolated_preview.cjs` **27/0**、`test_desktop_project_native_actions.cjs` **40/0** | ✓ |
| **A28** 治理失败（未批准/额度/磁盘/审计/许可） | `test_desktop_execution_commands.py` **27/0**、`test_desktop_remote_execution_acceptance.py`（A28） | ✓ |
| **A29–A31** 新旧组合/无项目与只读/scheduler 引用本机 | `test_desktop_compatibility_matrix.py` 11+2 ✓、`test_desktop_target_resolution_acceptance.py`（A30/A31）✓；`test_mutation_evidence_scripts.py` **11 失败**（§2） | 部分 |
| **A32/A33** 关闭开关与在途效果、关开关路径 | `test_desktop_execution_broker.py`（A32）在本平台**全部 503 platform_unsupported** | **本平台不适用**（见 §3） |
| **A34–A36** 结果数据流/越界产物/逐工具落盘与 cwd 分类 | `test_desktop_local_input_staging.py` **9/0**、`test_desktop_run_inputs.py` **27/0**、`test_desktop_tool_disposition.py` **17/1**（见 §4 的实质发现） | 部分 + **发现缺陷** |

## 2. 失败的四个套件（逐个定性）

| 套件 | 结果 | 定性 |
|---|---|---|
| `test_desktop_skill_acceptance.py` | 5 失败/4 通过/2 跳过 | **环境**：`[LocalSkillRuntime] builtin:summary-workbook not deployed: [WinError 3] 系统找不到指定的路径: C:\Users\admin\AppDat…`（内置技能部署路径在本机不存在） |
| `test_desktop_web_pages.py` | 3 失败/25 通过 | **既有**：已用**原版 `workspace.js` 基线对照**，同样 3 失败（对 `console.js` 上传实现的期望过期），与本 change 无关 |
| `test_mutation_evidence_scripts.py` | **11 失败** | **仓库状态**：它加载 `openspec/changes/align-desktop-project-execution-with-master/evidence/…`，该目录在本检出中不存在（另一 change 的产物） |
| `test_desktop_tool_disposition.py` | **1 失败**/17 通过 | **实质发现**，见 §4 |

另记（**同日复核更正**）：`test_desktop_command_runner.cjs` 早前一次出现 1 项失败（`A20: … the run that raced the edit is warned`）。随后**连跑 4 次全部 31/0**，且该套件只驱动 `command-runner.js`/`journal.js`，而这两者**都不在本 change 改动之列** → 结论更正为：**该套件本机全绿**，早前那次失败是**时序抖动**（该用例把同一文件连续写两次、大小相同 v1→v2，依赖版本判定的时间戳粒度）。

## 3. A32/A33 在本平台不适用（不是通过）

`test_desktop_execution_broker.py` 中 A32/epoch 相关的 9 个用例（`test_closing_the_switch_stops_new_calls`、`test_the_declaration_is_what_closes_it`、两个 superseded-epoch 用例、revoked/disabled 系列）在本机**全部**收到
`503 {"code": "feature_unavailable", "message": "project execution is not available (platform_unsupported)"}`——broker 端点先被**平台门**拦截（`win32.supported=false`），开关关闭语义根本走不到。**没有放宽任何门禁测试去凑**；按 14.4 的要求，这些项应标 **不适用**（需受支持平台），不读作通过。

## 4. 实质发现：写工具清单守卫对 `write`/`edit` 漏判（A36 相关）

`tests/test_desktop_tool_disposition.py::CanWriteTests::test_a_disk_writing_tool_is_detected_by_its_imports` **第 141 行**（断言前者）：

```python
self.assertTrue(td.module_writes_files(os.path.join(REPO, "agent", "tools", "write", "write.py")))   # ← 失败：返回 False
self.assertTrue(td.module_writes_files(os.path.join(REPO, "agent", "tools", "edit", "edit.py")))
self.assertTrue(td.module_writes_files(os.path.join(REPO, "agent", "tools", "web_fetch", "web_fetch.py")))
```

**原因**：`agent/desktop_local/tool_disposition.py` 的 `_WRITE_PATTERNS` 认 `open(…, [wax])`、`.write_text(`、`.write_bytes(`、`os.replace`、`json.dump` 等，但 `write.py` 与 `edit.py` 实际是经 **`common.atomic_write.write_text_atomic(path, …)`** 落盘（两文件里唯一的 `open` 都是**读**模式），而模式表**不认识这个 helper**——所以两个核心写工具被**漏判为非写者**。

**为什么这值得记录**：该模块自己的 docstring 写着 *"a false negative is exactly the silent server write A36 exists to prevent"*。漏判不会让已分类的工具出错，但会让**这道清单守卫**对"将来经 `write_text_atomic` 写盘的新工具"失去拦截能力——正是 A36 要防的那类遗漏。本 change 不修改该文件（不属 10 份增量），故**记录待定**。

（顺带核对：`_WRITE_PATTERNS` 的 `open\s*\([^)]*,\s*['\"][wax]` 能匹配 `web_fetch.py` 的 `open(local_path, "wb")` ✓，故第三条断言本应通过——失败点确在 `write.py`。）

## 4.1 复核更正与补充（同日，自查）

- **更正 A23**：我先前写"未见本地用例"是**错的**。实际有：`test_desktop_local_files.cjs` **12/0**（清空授权**不动候选**、取消不产生授权、租户/服务器变更撤销授权）、`test_desktop_local_files_bridge.cjs` **6/0**（断连清授权但**不删候选**、选目录记住候选）；`desktop/src/main/local-files/candidates.ts` 的文档字符串亦明述 *"candidates … are not active grants … a restart shows the candidate list and asks the user to reconnect (D6)"*。故 A23 的"候选≠授权、不自动恢复"部分**本机已覆盖**，只剩"设备离线"与"设备 B 查看设备 A 历史"需真机。矩阵相应单元格已改正。
- **补充**：`test_desktop_project_execution_grant.cjs` **10/0**（授权/撤销路径）。
- **`test_desktop_artifact_source.py`**：**34 通过 / 6 失败**，失败原因全部是**平台路径分隔符**（断言 `'output\\report.txt' != 'output/report.txt'`——POSIX 字面量在 Windows 上不成立）；该套件**不引用本 change 改动的任何文件**，属既有平台差异。

## 5. 判定

- 本矩阵**不改变任何勾选状态**：剩余 A 项仍需真机（11.2–14.6）。
- 用途：真机执行时可直接引用"逻辑层已覆盖"的部分，只跑设备相关步骤；并保证 A32/A33 之类在本平台标**不适用**而非通过。
