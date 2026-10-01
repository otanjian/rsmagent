# A17 / A26 / A27：本机隔离与进程树的可复核证据

`acceptance.md` 把 A26/A27 定为 P0/P4 门槛，A17 定为 P2。本文把每条判定映射到
**真实进程、真实内核拒绝**的可运行用例，并明确写出**没有做到**的部分——证据只能
支撑到它实际跑过的范围。

用例位置：

- `node --test tests/test_desktop_local_execution.cjs`（50 项，需 macOS + `/usr/bin/sandbox-exec`）
- `.venv/bin/python -m pytest tests/test_desktop_local_worker.py -q -p no:randomly`（44 项）

## 1. A26：越界由内核拒绝，而不是命令字符串黑名单

判定要求：脚本通过 Python `os.open`、动态路径、链接替换及子进程访问项目外文件时被
**实际 OS 边界**拒绝，且测试不依赖命令字符串黑名单，同时项目/必要运行库/临时目录
仍能正常工作。

| 场景 | 用例 | 断言 |
|---|---|---|
| `os.open` + 运行时拼出的路径 | `A26: Python os.open on a dynamically built outside path is refused` | 目标路径不出现在命令文本中；结果为 `DENIED` + 内核 `Operation not permitted`，密钥内容未泄漏 |
| 项目内软链接指向外部 | `A26: a symlink inside the project does not become a way out` | shell `cat` 与 Python `open` 两条路径都被内核拒绝（字符串过滤无法发现链接替换） |
| 子进程继承同一边界 | `A26: a grandchild subprocess inherits the same boundary` | `subprocess.run(["/bin/cat", ...])` 同样被拒绝——只授予父进程不算隔离 |
| 正常路径仍可用 | `A26: project, temp directory and runtime still work normally` | 项目读写、`run` 临时目录写入、stdlib 导入全部成功 |
| 临时文件真的能建 | `A26: a temp file can actually be created inside the run` | `tempfile.gettempdir()` 等于授予的 run 目录，且 `NamedTemporaryFile` 真的创建成功 |

为什么这些断言不可能是“字符串过滤”伪造的：`bash` 工具**不做**参数过滤
（`worker.py::_check_paths` 显式豁免 `bash`，见 `tests/test_desktop_local_worker.py::
test_bash_commands_are_not_string_filtered`），所以上面每一条都走真实内核路径；
拒绝信息是 `Operation not permitted`，不是工具自己编的文案。

## 2. A27：秘密、缓存与网络

| 场景 | 用例 | 断言 |
|---|---|---|
| 不继承环境秘密 | `A27: no secret from the launcher environment reaches the worker` | 注入的 `COW_DESKTOP_TOKEN` / `OPENAI_API_KEY` / `COW_SESSION_TOKEN` / `COW_IDENTITY_DB` 在 worker 的 `env` 输出中均不出现；且不存在任何 `*_TOKEN=`/`*_SECRET` 残留 |
| 身份库/主进程配置不可读 | `A27: identity data and main-process config outside the project are unreadable` | `identity.db`、`config.json` 的 `cat` 都是内核拒绝，内容未泄漏 |
| 技能缓存只读 | `A27: a skill cache is readable but cannot be modified` | 技能文件可读；改写、新建、删除三种写入均非零退出且为内核拒绝；文件事后内容不变 |
| 网络不可达 | `A27: the network is unreachable from inside the sandbox` | 本机 127.0.0.1 上**真实监听**的 socket 也连不上（`CONNECTED` 不出现）——这是最强的探针，放行的话它一定会打印 `CONNECTED` |

## 3. A17：退出、预算、排水与进程树

| 场景 | 用例 | 断言 |
|---|---|---|
| 非零退出如实上报 | `A17: a non-zero exit is reported as the real exit code` | `exit 3` → `exit_code === 3`；且被归类为 `tool_error` 而非 worker 崩溃 |
| 大量 stdout 截断并有原因 | `A17: a large drain is truncated with a stated reason` | 200 KB 输出 → `details.truncation.truncated === true` 且 `total_bytes > max_bytes`，模型能看出自己没看到全量 |
| 后台子进程不随运行残留 | `A17: a backgrounded child does not outlive the run` | 运行结束后用 `kill(pid, 0)` 轮询确认后台进程真的消失 |
| 工具级后台作业同样随运行结束 | `A17: a background job started by a run does not outlive it` | `run_in_background: true` 起的作业在会话停止后也被轮询确认消失 |
| 超时后命令本身停止 | `A17: a tool past its budget is stopped, not left running` | 超时后**命令进程与其后台子进程都必须消失**（pid 由命令自己写入项目文件，避免依赖超时帧里不存在的输出） |

第 5 条是对 master 语义的**有意偏离**，必须写明：`agent/tools/bash/background.py`
的模块文档说后台进程「刻意不随 agent 结束而终止」（通常是用户要求常驻的服务）。
对共享服务器成立，但本机受限运行不成立——运行结束（含关闭项目、取消、长期失联）后
在用户机器上留下常驻进程是泄漏，而 A17/A18/A19 明确要求进程树随运行结束。
因此 `background.start` 也接入了同一 `process_group_hook`；**未安装该 hook 的调用方
行为完全不变**（服务器侧仍是原语义）。

第 4 条曾经只断言 `session.running === false`，也就是只证明 worker 停了——而 worker
停止**并不等于**命令停止。这正是本轮发现并修掉的问题，见下节。

## 4. 本轮由这些用例发现并修复的真实缺陷

四条都是“代码看起来对、只有真实进程才会暴露”的问题：

1. **取消根本杀不掉命令（P0 级）**。沙箱策略只给了 `(allow signal (target self))`，
   而它**拒绝**worker 给自己的孩子发信号：`kill(2)` 返回
   `Operation not permitted`。实测过滤器对比（`/usr/bin/sandbox-exec`）：

   | 过滤器 | 杀自己的孩子 |
   |---|---|
   | `(target self)` | **DENIED** |
   | `(target children)` | KILL_OK（最窄可用） |
   | `(target pgrp)` | KILL_OK（更宽） |
   | `(target others)` | DENIED（是 children 的补集，没用） |

   现取 `(target self)` + `(target children)`。此前 bash 工具自身的
   `_kill_process`（`os.killpg`）在沙箱里同样被拒绝，即“工具自己的超时”也杀不掉进程。

2. **命令的后台子进程无人认领**。命令用 `start_new_session=True` 起在自己的会话里，
   所以 desktop 侧 `kill(-workerPid)` **永远到不了**它；而“命令派生后台进程后退出”
   会让该进程被 reparent 到 init，从 worker 的“孩子”变成非孩子，内核同样拒绝。
   现在由 worker 记录命令行创建的每个 process group（`_ProcessGroups`），
   在 `cancel`/`shutdown`/SIGTERM 时终止；**超出许可的部分**（`unreaped_groups`）
   回传给不受沙箱约束的 desktop 主进程补杀。

3. **`killpg` 会“部分成功”**。对同组内的多个目标，内核逐个判定许可；被拒绝的成员
   不会让 `os.killpg` 报错。因此 `kill_all` 不再假定成功，而是**复核**该组是否还在，
   仍在则列入 `unreaped_groups`。

4. **`tempfile` 在沙箱里找不到可用临时目录**。`TMPDIR` 从父进程继承，
   指向沙箱写入授权之外，于是 `tempfile.gettempdir()` 报
   `[Errno 2] No usable temporary directory found`；`bash` 工具一旦输出超过内联上限
   就会往临时文件里溢出，直接失败。现在 `TMPDIR`/`TEMP`/`TMP` 一律**设置**为授予的
   run 目录（`env.ts` 与 `worker.py` 两侧一致），父进程的值不再被继承
   （`scrubWorkerEnv` 已从 `ENV_KEEP` 移除这三个名字）。

## 5. 仍然没有做到的（不得据此声明通过）

- **内存/CPU 没有硬上限**。当前的实际预算是：单次调用墙钟（默认 300 s，可传更小值）、
  帧上限（1 MiB）、结果上限（2 MiB）、bash 输出尾部截断（2000 行 / 50 KB）以及
  超时后的进程树终止。`RLIMIT_AS` 在 macOS + CPython 下不可靠（虚拟地址预留会被误杀），
  本轮**没有**实现内存硬限制，A17 的“资源超限”只被上述维度覆盖。
- **SIGTERM 路径无法回传** `unreaped_groups`：信号处理函数没有回帧通道，该路径只做
  尽力清理；可回传的是 `cancel` / `shutdown` 两条请求-应答路径。
- **Windows 未做**：`resolveScriptSupport` 在 win32 返回 `unsupported_platform`，
  不提供降级路径，因此 A26/A27 在 Windows 上不得声明可用（见任务 4.5）。
- **签名未做**：安装包的**未签名**形态已验（见 §6），签名/公证后的等价探针未做（属 A33）。

## 6. 安装形态（随包 Python / 冻结 worker）：同一批断言换一个运行时

本轮把"**用户实际拿到的那种运行时**"补上了。在此之前上述全部结论都来自开发机
（源码树 + `.venv` 解释器），而这两种形态**不可互换**：

- 源码树跑的是 `python -m agent.desktop_local.worker`，其 `base_prefix` / `stdlib` /
  `site-packages` 都是**真实可读**目录；
- 安装形态只有**一个冻结可执行文件**，没有 `.venv`，而 `sysconfig` 仍会报出
  **构建机**的路径——所以 `interpreter.ts` 对 frozen 分支**只授予 `bundleRoot`**。
  于是"在源码树里成立的 profile 在冻结后可能起不来"（表现为 worker 在握手前就死，
  桌面只能报"worker 崩溃"），而这正是用户拿到的那一种。

### 6.1 可复现入口（已在发布流水线里当门槛）

`tests/test_desktop_local_execution.cjs` 现在支持把同一批断言指向一个**打包后端**：

```bash
COW_A26_BACKEND=/Applications/Cow.app/Contents/Resources/backend \
  node --test tests/test_desktop_local_execution.cjs
# 或指向 PyInstaller 输出（流水线用的就是这条）
COW_A26_BACKEND=$PWD/desktop/build/dist node --test tests/test_desktop_local_execution.cjs
```

运行时由**生产代码**自己解析（`interpreter.ts::findWorkerRuntime` 找
`<backendPath>/cowagent-backend/<exe>`，与安装形态的
`Contents/Resources/backend` 布局一致），read 授权由 `--probe` 实测得出——
不是为了测试另算一套路径（否则应用可以在测试全绿的情况下出货即坏）。

已接入 `release.yml` 与 `release-overlay.yml` 的 macOS leg（step：
*Verify the installed-shape sandbox boundary*），产物就是同一次构建的
`desktop/build/dist`。

### 6.2 实测结果

本轮对**真实 `.app` 内**的 bundle 与 PyInstaller 输出各跑一遍，均为
**44 passed, 0 failed, 15 skipped**（源码形态 **54 passed, 0 failed, 5 skipped**，
即两种形态各自跳过对方专属的用例，没有一条"两边都不跑"）。

新增 5 条安装形态断言（`tests/test_desktop_local_execution.cjs`）：

| 断言 | 结果 |
| --- | --- |
| runtime 取自 bundle；read 授权只有 bundle | 通过 |
| **构建机**的 `stdlib`/`purelib` 未被授予（两者在本机真实存在，故此断言非空洞） | 通过 |
| 冻结 worker 在沙箱内**完成握手**且工具集与 master 一致（六工具） | 通过 |
| 冻结 worker 能写/读/`bash` 自己的项目 | 通过 |
| 越界由**内核**拒绝（`Operation not permitted`），且 `read`/`write` 工具自身的守卫同样拒绝；项目内仍可用 | 通过 |
| 启动器秘密不到达 worker；`identity.db` 不可读 | 通过 |

最后一条值得单独说明：`interpreter.ts::interpreterReadPaths` 对 frozen **刻意只给
`bundleRoot`**，所以在安装形态下**不存在**任何可用于"逃逸探针"的通用解释器——
本机 macOS 26 的 `/usr/bin/python3` 其实是个 `xcrun` shim，其 dylib 位于
`/Library/Developer/CommandLineTools`，同样被 profile 拒绝：

```
xcrun: error: unable to load libxcrun (dlopen(...): file system sandbox blocked open())
```

这是**沙箱在正常工作**，不是缺陷；但结论必须如实说明：**解释器形态的逃逸探针
（`os.open` 动态路径、软链接、孙进程）只在源码树形态成立**。安装形态的内核级断言
改由 worker 自带的 `bash` 工具承担——shell 命令**不经过** worker 的路径守卫，
所以"内核允许什么就会发生什么"，该断言因此是内核级的而非守卫级的。
若某平台存在能在安装形态沙箱内启动的解释器，可用
`COW_A26_PROBE_PYTHON=/path/to/python` 把这些探针也挂上去。
