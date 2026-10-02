# 平台执行边界探针（P0 / A26 / A27）

目的：为 `design.md` D5 选定**真实**的进程启动与文件访问边界，证明本机脚本隔离
不依赖 cwd、参数串检查或现有配置开关。本页是**开发机探针**记录，不等于安装包验收。

## 1. 环境

- macOS 26.4（Build 25E246），Apple Silicon。
- 工具：`/usr/bin/sandbox-exec` 存在（`which sandbox-exec` 命中）。
- 探针目录：`/private/tmp/cow-probe/{proj,skills,tmp,outside.txt}`。
  注意 `/tmp` 是符号链接；沙箱按**解析后**路径匹配，过滤器必须写 `/private/tmp`。

## 2. 探针过程与结论

最小可执行策略（经多轮二分得到）：

```
(version 1)
(deny default)
(allow process-exec)
(allow process-fork)          ; 缺它则派生进程报 fork: Operation not permitted
(allow sysctl-read)
(allow mach-lookup)
(allow signal (target self))
(allow file-read-metadata (subpath "/"))   ; 缺它则 execvp 直接 EPERM（见 §2.1）
(allow file-read*
  (literal "/")               ; 缺它则连 /usr/bin/true 都 SIGABRT(134)
  (subpath "/System/Volumes/Preboot")   ; dyld 共享缓存
  (subpath "/usr") (subpath "/System") (subpath "/bin") (subpath "/sbin")
  (subpath "/Library/Apple") (subpath "/dev") (subpath "/etc")
  (subpath "/private/etc") (subpath "/private/var/db") (subpath "/private/var/select")
  (subpath "/private/tmp/cow-probe/proj") (subpath "/private/tmp/cow-probe/skills")
  (literal "/System/Volumes") (literal "/System") (literal "/Library")   ; 祖先目录
  (literal "/private") (literal "/private/var") (literal "/private/tmp")
  (literal "/private/tmp/cow-probe") ...)
(allow file-write*
  (subpath "/private/tmp/cow-probe/proj") (subpath "/private/tmp/cow-probe/tmp")
  (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))
```

实测行为（`sandbox-exec -f policy.sb /bin/sh -c '<cmd>'`）：

| 操作 | 结果 |
|---|---|
| `cat .../proj/inside.txt` | 允许（exit 0） |
| `cat .../skills/s.py` | 允许（技能缓存只读读） |
| `cat .../outside.txt` | **拒绝** `Operation not permitted` |
| `echo x > .../proj/new.txt` | 允许 |
| `echo x > .../outside.txt` | **拒绝** |
| `echo x > .../skills/hack.py` | **拒绝**（技能缓存不可写） |
| `echo x > .../tmp/scratch.txt` | 允许（运行临时目录） |
| `d=...; f=outside.txt; cat "$d/$f"` | **拒绝**（动态拼接同样被拦，非字符串匹配） |
| `ln -s outside.txt proj/link.txt; cat proj/link.txt` | **拒绝**（符号链接逃逸被解析后拦截） |
| `cat proj/../../outside.txt` | 拒绝/不存在（越界路径不落到允许根） |

### 2.1 三处「缺了就无法启动」的实测补充（2026-09-30 复核）

上面表格是**策略手写**的探针。把它接进真实启动器（`desktop/src/main/local-execution/`，
以 `python -m agent.desktop_local.worker` 启动）后，又暴露三处必须项，均已实测：

1. **`(allow file-read-metadata (subpath "/"))` 不可省。** 只给 `(literal "/")` 时，
   `sandbox-exec` 直接以 exit 71 失败：
   `execvp() of '.../.venv/bin/python' failed: Operation not permitted`。
   原因是 exec 前要对**每一级祖先目录**做目录项查找，而 `file-read*` 的
   `subpath`/`literal` 过滤不覆盖祖先查找。补上元数据许可后即可启动。
   **代价必须写明**：文件**内容**仍被拦（实测 `cat` 越界文件报
   `Operation not permitted`），但越界文件的**存在性、大小、时间戳**可见。
   这是「读边界」的准确口径，能力描述里不得省略。
2. **祖先目录要单独给 `(literal ...)`**，不能指望 `(subpath ...)` 兼作可达性。
   实测只给 `(subpath "/Users/jiantan/ai_assistant/rsmagent")` 时 execvp 仍 EPERM；
   补 `(literal "/Users")` 可启动。因此实现按每个允许路径**逐级生成祖先 literal**，
   而不是整体放开父目录——`(subpath "/Users")` 虽也能启动，但等于放开整个家目录。
3. **`/dev/null` 必须作为 literal 可写。** 否则 Python `subprocess.DEVNULL`
   报 `[Errno 1] Operation not permitted: '/dev/null'`，表现为 bash 工具整体失败。
   只给 `(literal "/dev/null")`，不得给 `(subpath "/dev")`。
4. **信号过滤器必须是 `(target children)`，`(target self)` 不够。**
   只给 `(allow signal (target self))` 时，worker **杀不掉自己的子进程**，于是
   取消路径整体报 `Operation not permitted`，被取消的命令继续运行。
   实测：`(target children)` 放行后代、且仍然拒绝向非后代发信号；
   `(target others)` 是补集，无用；裸 `(allow signal)` 能通过但会放开「向机器上
   任意进程发信号」，**不得**使用。
   双向断言在 `tests/test_desktop_local_execution.cjs` 的
   `A17: a script may signal its own children, and only its own`：
   沙箱内脚本杀死自己的子进程必须 rc=0，同时向沙箱外的进程 `kill -0` 必须被拒。
   变异校验：删掉 `(target children)` → 2 项失败；换成裸 `(allow signal)` → 2 项失败。

### 2.2 信号许可：`(target self)` 不够，且必须只给到 children（2026-09-30 新增）

取消/超时要求「进程树真实结束」，而 worker 必须能给自己启动的命令发信号。
实测四种过滤器（同一探针：起一个 `sleep`，再 `kill -9` 它）：

| 过滤器 | 结果 |
|---|---|
| `(allow signal (target self))` | **KILL_DENIED**（`Operation not permitted`） |
| `(allow signal (target children))` | KILL_OK —— **取此值** |
| `(allow signal (target pgrp))` | KILL_OK（更宽：整个进程组） |
| `(allow signal (target others))` | KILL_DENIED（它是 children 的补集，不能替代） |
| `(allow signal)`（无过滤器） | KILL_OK（最宽，不采用） |

`(target self)` 的语义是「只能给*自己*发信号」，它**不包含**自己的孩子，
因此原来的策略让所有取消路径静默失败。取 `(target children)` 保留了「不能跨会话
kill」的约束：A17 要求不得跨界终止，而无过滤器的 `(allow signal)` 会允许终止同用户
的任意进程，与 A17/A18 冲突。

**仍有一个内核无法表达的情形**：命令若「派生子进程后自己退出」（`sleep 300 &`），
该子进程在会话/进程组上仍属于本次运行，但已被 reparent 到 init，不再是本进程的
children，因此内核按成员逐个拒绝。实测 `os.killpg` 对**部分被拒**的成员**不报错**，
所以实现改为**复核**该组是否存活，存活则把组 id 回传给不受沙箱约束的 Electron 主进程
补杀。详见 `evidence/isolation-acceptance.md` §4。

### 2.3 `TMPDIR` 必须改指运行临时目录（2026-09-30 新增）

`TMPDIR` 若从父进程继承，会指向沙箱写入授权之外，`tempfile.gettempdir()` 随即报
`[Errno 2] No usable temporary directory found in [...]`。表现不只是脚本失败：
`bash` 工具在输出超过内联上限时会把完整输出**溢出到临时文件**，于是普通的长输出命令
整体失败。因此 `TMPDIR`/`TEMP`/`TMP` 一律**设置**为授予的 run 目录
（`env.ts` 与 `worker.py` 两侧一致），而不是列入保留名单。

另有一处**不是**必需项的结论，用于澄清：`(subpath "/")` 也能让进程启动（早期实现即如此），
但它使全部文件可读，「隔离」只剩写边界。现实现使用 `(literal "/")` + 祖先 literal，
读、写两个方向都被 deny-by-default 约束。

## 3. 直接结论（约束实现）

1. macOS 可用 `sandbox-exec` 建立**真实文件/子进程边界**；但必须先 allow 根目录
   字面量与 `/System/Volumes/Preboot`，否则进程在 dyld 阶段 SIGABRT——这解释了
   为什么“只 deny default 加项目根”会整体不可用。
2. **派生进程需要 `process-fork`**；只给 `process-exec` 会导致脚本内 `cat`/子进程失败。
   子进程能力与取消实现相关（A17）。终止子进程还需 `(allow signal (target children))`，
   见 §2.2。
3. 过滤器按解析路径匹配，**必须用真实路径**（`/private/tmp`，不是 `/tmp`）；软链接与
   `..` 逃逸在执行边界被拦，而不是靠检查命令字符串。
4. **读边界的准确口径**：内容读被拦（仅授予根 + 系统运行时），目录**元数据**全局可读。
   任何面向用户/模型的能力描述都必须同时给出这两点，不能只说“已隔离”。
5. 不允许在未实现该边界的平台声明“脚本可用”；`execution_isolation` 配置值不能替代本探针。

## 4. 复现方式

- 形态与断言：`node --test tests/test_desktop_local_execution.cjs`（50 项，含真实
  `sandbox-exec` 进程，实测写越界与读越界的**内核**拒绝、动态路径、符号链接替换、
  孙进程、本机监听 socket 不可达、临时目录可用、超时后进程树真实终止，
  而非策略文本比对；A 编号映射见 `evidence/isolation-acceptance.md`）。
- 启动计划由 `desktop/src/main/local-execution/launch.ts` 组装，解释器可读路径由
  `interpreter.ts` 实测 `sys.base_prefix`/`stdlib`/`purelib` 得出（虚拟环境是符号链接，
  三者与二进制所在目录互不包含，任一缺失都会让 worker 在握手前退出）。

## 5. 未完成项

- [ ] Windows 平台同等探针（Job Object / 受限令牌或等效），当前**未做**，Windows 脚本能力不得声明可用。
- [ ] Python 解释器随包启动的探针：本机 `python3` 经 xcode shim 需要 `/private/var/select/developer_dir`
      读取；随包 Python 需单独验证依赖与边界。（本页 §2.1 已在**开发机 venv** 上验证，
      不等于随包 Python 的验证。）
- [ ] 安装包内启动器（签名后）的同等探针，属 P4/A33。
