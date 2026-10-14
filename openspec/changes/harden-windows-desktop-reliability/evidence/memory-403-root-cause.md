# 个人记忆 403 根因记录（任务 3.1）

日期：2026-10-13（Windows x64，Python 3.12.13）。对应报告：`docs/design/windows-desktop-verification-2026-10-13.md` §4.3。

## 1. 结论（一句话）

`common/safe_fs.py` 的 `file_lock()` 在**没有 `dir_fd` 支持**的平台（Windows）走路径回退分支，用 `_resolve_chain()` 解析**锁文件本身**；而该函数要求每一级组件都是**目录**，于是：

- 第一次取锁：`.memory.lock` 还不存在 → 走 `allow_missing_tail` → 成功并创建该文件；
- 第二次起：`.memory.lock` 已存在且是普通文件 → `UnsafePathError("non-directory component '.memory.lock'")`；
- `agent/memory/personal.py::scope_transaction` 恰好只捕获 `UnsafePathError`，把它包装成 `PersonalMemoryError('个人记忆目录被替换', code='unsafe_path', status=403)`。

于是页面显示「个人记忆目录被替换」，且**刷新永远无效**（锁文件一直存在）。真正被拒绝的不是"目录被替换"，而是**锁文件自己**。

POSIX 不触发：那里 `_DIR_FD_OK` 为真，`file_lock` 走描述符分支（`os.open(..., dir_fd=...)`），根本不调用 `_resolve_chain`。

## 2. 复现（可重复脚本）

```bash
python scripts/repro/repro-lock-second-call.py
```

本机实测输出（截取）：

```
== as shipped on this platform
   root: C:\Users\admin\AppData\Local\Temp\cow-lock-repro-...\用户 1001\memory-root
   _DIR_FD_OK: False
   call 1: ok (lock file exists: True)
   call 2: UnsafePathError: non-directory component '.memory.lock'
      File "common/safe_fs.py", line 106, in file_lock
        path = _resolve_chain(_root_path(root), parts, allow_missing_tail=True)[0]
   call 3: UnsafePathError: non-directory component '.memory.lock'

== the effect seen at the service layer
   scope_transaction raised: code=unsafe_path status=403 message=个人记忆目录被替换
   __cause__: UnsafePathError("non-directory component '.memory.lock'")
```

脚本控制项说明：把 `_DIR_FD_OK` 强行置真**不能**在 Windows 上模拟 POSIX（它会真的去用 `dir_fd` 打开而失败，报 `FileNotFoundError`），所以脚本只把它作为「分支选择」的说明，不作为 POSIX 行为证据；POSIX 分支由仓库既有测试覆盖。

## 3. 现场证据（报告环境自身的日志）

报告所用开发后端（数据根=仓库，`tenant_shared_base=~/.cow/tenant-roots`，见 `run.log` 启动行）在自己的日志里留下了同一错误的 16 次记录，且没有堆栈——因为它被当作"已处理的安全拒绝"记录：

```
[ERROR][2026-10-13 10:37:13][memory.py:44] - [WebChannel] Memory API error: 个人记忆目录被替换
[ERROR][2026-10-13 10:40:41][memory.py:44] - [WebChannel] Memory API error: 个人记忆目录被替换
[ERROR][2026-10-13 10:43:31][memory.py:44] - [WebChannel] Memory API error: 个人记忆目录被替换
```

与页面文案对照可确认：`个人记忆目录被替换` 这一字符串在代码里只出现在 `agent/memory/personal.py:160`，即 `scope_transaction` 捕获 `UnsafePathError` 的位置——**不是** `user_root()` 的"本人记忆根目录被替换"，也不是 `memory_console.py` 的"记忆路径被替换"。定位因此不依赖页面文案。

## 4. 排除的假设（都实测过）

| 假设 | 实测结论 |
|---|---|
| Windows 路径含中文/空格导致 safe_fs 拒绝 | **排除**：`scripts/repro/probe-safe-fs-windows.py` 在 `中文 目录 with space` 下 `file_lock`（首次）/`write_text_atomic`/`read_text`/`list_names`/`stat`/`unlink` 全部正常 |
| 个人根目录被 junction 替换（`os.path.islink` 把 junction 当链接） | **本机 3.12 排除**：junction 的 `os.path.islink=False`、`os.path.isjunction=True`；且真实路径 `~/.cow`、`~/.cow/tenant-roots`、`tenants`、`~/cow` 均无重解析点。（注：Python < 3.12 的 `islink` 对 junction 语义不同，若随包解释器为 3.11，此假设需在 3.11 上另测——未做，如实登记） |
| 租户共享根落在 home/数据根被 `state_dir` 拒绝 | **另一个真实问题**，但它是 503 `memory_unavailable`、报错文案不同：`repro-personal-memory-403.py` 用合成实例复现过（`... resolves inside the home/global workspace root; refusing to fall back`）。与本次 403 不是同一个缺陷，另行记录 |

## 5. 影响面

- 任何在 Windows 上走 `scope_transaction` 的操作（个人记忆读取/保存/清空、后台固化）在**第二次**调用起全部 403；首次调用（或锁文件被删除后）会成功——这正是"看起来偶尔能用、刷新无效"的形态。
- 同类风险不止个人记忆：`common/safe_fs.py::file_lock` 是通用原语（知识库、工作区等也可能取锁），但是否受影响取决于各调用方是否只在 Windows 走该分支，需要在修复时一并核对（任务 3.4/3.5）。
- 修复方向（任务 3.2，尚未实施）：回退分支改为按**普通文件**语义解析——校验父链（`_resolve_parent`）后追加文件名，并对末级做符号链接拒绝，保持 `file_lock` 文档承诺的"所有进程锁同一 inode"。
