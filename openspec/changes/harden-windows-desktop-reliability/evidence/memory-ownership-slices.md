# 阶段 3 证据：归属与防替换切片验证（任务 3.4）

日期：2026-10-13（Windows x64 / Python 3.12.13）。前置：`memory-403-root-cause.md`、`memory-403-fix.md`、`memory-error-classification.md`。

## 1. 结论摘要

| 切片 | 本机结果 |
|---|---|
| **junction 替换根** | **发现真实漏洞并已修复**（读、写两条路径都曾被穿过）；修复后拒绝：`unsafe_path/403` |
| junction 作为根内中间组件 | 拒绝（`UnsafePathError`），`is_symlink()` 也报 True |
| symlink（根 / 中间目录 / 条目 / 操作期间替换） | **本机无法验证**：本会话没有创建符号链接的权限（`WinError 1314`），仓库相关用例全部因此失败，且失败原因**没有一条是断言失败** |
| 跨用户 / 跨租户 | `tests/test_memory_console_scope.py`：**27 通过 / 6 失败（全部为符号链接权限）**；`tests/test_personal_memory_scope_protocol.py`：**13 通过 / 4 失败（同上）** |
| 正常路径不误伤 | C: 与 D: 盘中文/空格根，四个连续事务 CRUD **ALL STEPS OK**；`tests/test_safe_fs.py` 的 15 通过项无回归 |

## 2. 发现的漏洞（真实、可复现）

**机制**：仓库所有防替换守卫都问 `os.path.islink`。Windows 的目录 **junction** 是重解析点，会像符号链接一样重定向路径，但从 **Python 3.12 起** `os.path.islink` 与 `lstat` 的 `S_ISLNK` 都把它报成普通目录（`os.path.isjunction` 才是 3.12 新增的 API；≤3.11 时 `islink` 返回 True，所以 3.11 的随包解释器是安全的）。本 change 的支持窗口是 3.8–3.13，3.12/3.13 都在内。

**复现**（`scripts/repro/probe-junction-memory-root.py`，修复前实测）：

```
root    : ...\users\1001  (junction ->)
target  : ...\cow-junction-qeg_4a26
islink=False isjunction=True
  scope_transaction        ENTERED (not refused)
  read_text(MEMORY.md)     READ: "SOMEONE ELSE'S MEMORY\r\n"
RESULT: LEAK -- a junction root reaches another user's files
```

**修复**（最小、集中在共享原语）：

| 文件 | 改动 |
|---|---|
| `common/safe_fs.py` | 新增 `is_link_like(path)`（`islink` **或** `isjunction`）并导出；9 处守卫改用它：根断言、`_resolve_chain`、`_resolve_parent`、`_mkdir_level` 复核、`file_lock` 回退、`read_bytes`、`_write_temp_then_replace`（两级）、`unlink`、`remove_tree`、`rename`；`is_symlink()` 语义扩展为"重定向条目"；**补上写路径缺失的根断言**（该洞由本次新测试抓出：读被拒而写穿过） |
| `agent/memory/personal.py` | `user_root()` 用 `safe_fs.is_link_like` 判根 |
| `agent/memory/service.py` | 同上（智能体工作区根） |

**修复后实测**：

```
  scope_transaction        refused: code=unsafe_path status=403
  read_text(MEMORY.md)     refused: UnsafePathError(root is a symbolic link or a junction)
RESULT: no leak observed through these calls
```

## 3. 新增回归测试（`tests/test_junction_refusal.py`，6/6 通过）

junction 用 `mklink /J` 创建（**不需要特权**，这也是它能测而 `os.symlink` 不能测的原因）：

1. `is_link_like` 对普通/不存在路径为 False；
2. junction 分支在只有 symlink 的平台上也被覆盖（注入 `os.path.isjunction`）；
3. **junction 根被拒**：读与写都抛 `UnsafePathError`；
4. **根内 junction 组件被拒**：`list_names` / `read_text` 拒绝，且 `is_symlink()` 报 True；
5. 重定向的**目标目录本身**仍可正常读取（拒绝重定向不等于拒绝真实目录）；
6. `scope_transaction(junction 根)` → `PersonalMemoryError(code='unsafe_path', status=403)`。

## 4. 未验证（环境限制，如实登记）

- **符号链接相关切片在本机无法执行**：本会话没有 `SeCreateSymbolicLinkPrivilege`（启用开发者模式或提权才能获得），`os.symlink` 抛 `WinError 1314`。受影响用例（含"操作期间替换目录后不泄露内容"）在两个套件里各失败 6 / 4 项，**失败全部发生在测试创建符号链接的步骤**，没有任何断言失败。
  → 建议：在 POSIX（CI 或开发机）跑这两个套件，或在授予符号链接权限的 Windows 会话里复跑；本机不把该部分记为已验证。
- 本次修复只覆盖 `common/safe_fs.py` 与两个记忆根守卫；仓库里**其他**模块若也直接用 `os.path.islink` 做防替换判断（如 `agent/workspace/project_browser.py`、`agent/tools/...`），不在本 change 的"个人记忆"范围内，**未改**——已在此登记为独立范围，避免被误认为已全面加固。

## 4.1 全量记忆套件（19 个文件）与回归排除

| 套件 | 结果 |
|---|---|
| `test_account_memory_management.py` | 21 passed（账号切换/记忆管理） |
| `test_personal_memory_console.py` | 30 passed（保存/清空控制台路径） |
| `test_memory_path_marks_dirty.py` | 19 passed（索引置脏） |
| `test_personal_memory_tool_execution.py` | 18 passed |
| `test_memory_rerank.py` / `test_memory_decay_scope.py` / `test_memory_global_config.py` / `test_memory_get_shared_knowledge.py` / `test_memory_hybrid_fusion.py` / `test_memory_storage_tenant_scope.py` / `test_summarizer_atomic_memory_write.py` / `test_memory_sync_shared_root.py` | 全部 passed（3/9/9/4/3/5/4/1） |
| `test_memory_retryable_classification.py`（本次新增） | 5 passed |
| `test_memory_console_scope.py` | 27 passed / 6 failed（**全部 WinError 1314**） |
| `test_personal_memory_scope_protocol.py` | 13 passed / 4 failed（**全部 WinError 1314**） |
| `test_memory_index_rel_path.py` | 5 passed / 1 failed（WinError 1314） |
| `test_memory_sync_reconcile.py` | 6 passed / 1 failed（WinError 1314） |
| `test_memory_console_write.py` | 17 passed / 3 failed（见下） |
| `test_user_personal_memory.py` | 16 passed / 1 failed（见下） |

**回归排除（基线对照）**：把 `common/safe_fs.py`、`agent/memory/personal.py`、`agent/memory/service.py` 临时换回 `origin/rdai` 原版后跑同一套件：

| 套件 | 原版 | 本 change |
|---|---|---|
| `test_user_personal_memory.py` | **3 failed** / 14 passed | **1 failed** / 16 passed |
| `test_memory_console_write.py` | **5 failed** / 15 passed | **3 failed** / 17 passed |

两处失败在原版就存在（单次运行计数不同，说明这些用例对顺序/时序敏感），**不是本 change 引入的**；换回原版后又恢复了本次改动（已核对 `is_link_like` / `LockBusyError` / 记忆根守卫都在）。

## 5. 判定

任务 3.4 要求"以上切片均不可越界"。本机结论：junction 切片**已在验证中发现越界并修复**；跨用户/租户与正常路径通过；**符号链接切片因环境无法验证**。因此 **3.4 不勾选**，等符号链接可在目标环境执行后再判。
