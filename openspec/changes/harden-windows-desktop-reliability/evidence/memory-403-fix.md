# 阶段 3 证据：个人记忆 403 的最小修复（任务 3.2）

日期：2026-10-13（Windows x64 / Python 3.12.13）。根因见 `memory-403-root-cause.md`。

## 1. 改动（最小，共 1 处函数 + 1 组用例）

| 文件 | 改动 |
|---|---|
| `common/safe_fs.py` | `file_lock()` 的**无 `dir_fd` 回退分支**不再用 `_resolve_chain()` 解析锁文件（它要求每级都是目录），改为按普通文件语义：`_resolve_parent(..., create=True)` 校验父链并追加文件名，再对末级做 `os.path.islink()` 拒绝，然后 `os.open(O_RDWR|O_CREAT)` |
| `tests/test_safe_fs.py` | 新增 `LockFileTests`（6 项）：连续取锁、强制回退分支连续取锁、锁文件复用同一 inode（不替换）、缺失根与锁文件被创建、符号链接锁路径在回退分支被拒、符号链接锁路径在任何分支都被拒且目标文件未被改动 |

没有重建文件层、没有放宽任何守卫：符号链接拒绝从原来的"隐式依赖 `_resolve_chain` 的逐级检查"改为**显式检查**（回退分支），描述符分支继续由 `O_NOFOLLOW` 保证。

## 2. 验证

### 2.1 复现脚本（修复前 → 修复后）

`python scripts/repro/repro-lock-second-call.py`

| 调用 | 修复前 | 修复后 |
|---|---|---|
| 第 1 次 | ok（并创建 `.memory.lock`） | ok |
| 第 2 次 | `UnsafePathError: non-directory component '.memory.lock'` | **ok** |
| 第 3 次 | 同上 | **ok** |
| 服务层 `scope_transaction` | `code=unsafe_path status=403 message=个人记忆目录被替换`，`__cause__=UnsafePathError` | `scope_transaction: entered`（无异常） |

### 2.2 中文/空格/盘符 CRUD（任务 3.2 的验收语句）

`python scripts/repro/probe-personal-crud-windows.py` —— 在**四个连续事务**里做写/读/列表/stat/正文比对/编辑/删除：

| 根目录 | 结果 |
|---|---|
| 系统盘 `C:\...\cow-crud-*\用户 1001\memory-root` | 9 步全部 ok（含 `body matches=True`、`lock file survived=True`） |
| 另一盘符 `D:\cow-crud-*\用户 1001\memory-root` | 9 步全部 ok |

路径同时包含中文、空格与不同盘符；第二次及之后的事务正是修复前必然失败的情形。

### 2.3 既有安全套件（RED/GREEN + 基线对照）

`tests/test_safe_fs.py`（本机临时 venv 内 pytest 9.1.1，未污染仓库与用户环境）：

| 代码版本 | 结果 |
|---|---|
| 未修复（`git show HEAD:common/safe_fs.py`） | **9 failed / 11 passed / 1 skipped**；其中新增的 `LockFileTests` **3 项失败**（连续取锁、锁文件复用、强制回退分支） |
| 已修复 | **9 failed / 15 passed / 3 skipped**；新增 6 项：4 通过、2 跳过（本机无符号链接权限） |

9 项失败在两版**完全一致**，全部是 `OSError [WinError 1314] 客户端没有所需的特权`（本会话无法创建符号链接），属环境限制而非产品缺陷；最后一次运行耗时 1.7 s。测试因此**有牙**：不修复就红。

### 2.4 其他调用方与套件

`file_lock` 的调用方只有 `agent/memory/personal.py:153`（`scope_transaction`）与测试自身，所以本次修复的影响面就是个人记忆事务（及其未来使用者）。

其余相关套件在本机**无法运行**，原因属环境而非改动：

| 套件 | 结果 | 原因 |
|---|---|---|
| `tests/test_personal_memory_scope_protocol.py` | collection error | 临时 venv 缺 `web`/应用依赖（装齐前无法收集） |
| `tests/test_memory_console_scope.py` | 33 errors → 装 `web.py`/`aiohttp` 后收敛为缺 `requests`；正以 `-r requirements.txt` 全量安装后复跑（见下方结果） | 依赖缺失 |
| `tests/test_memory_index_rel_path.py` | 1 failed / 5 passed | 该项失败为 `WinError 1314`（无法创建符号链接） |

## 3. 未完成 / 边界

- Windows 符号链接用例在本机**跳过**（无权限）：回退分支的符号链接拒绝由代码 + 跳过用例覆盖，未在真实符号链接上实测。
- 未在真实安装包上复验（归阶段 7/14）；本机验证使用同一份 `common/safe_fs.py` 与同一解释器窗口（3.12），随包解释器为 3.11，两者在本函数行为上无版本分支差异（仅 `os.path.islink` 对 junction 的语义不同，见根因记录 §4）。
- **工具事故（如实登记）**：用于本任务的临时脚本 `probe-personal-crud-windows.py` 的初版把清理路径算成了 `dirname×3`，在 `%TEMP%` 下的根目录上解析为 `%TEMP%` 本身、在 `D:` 下解析为 `D:\`。进程被及时终止，`D:\` 与仓库完好；`%TEMP%` 中该路径下的一部分文件被删除，其中包括 2026-10-13 巡检报告附录 A 的 5 个证据文件（`02-login.png`、`admin/10-mcp-editor-form.png`、`admin/11-mcp-editor-json.png`、`runlog-head.txt`、`admin2.json`）。脚本已改为只删除自己用 `mkdtemp` 创建并记录、且名字前缀匹配的目录，并已向用户报告。
