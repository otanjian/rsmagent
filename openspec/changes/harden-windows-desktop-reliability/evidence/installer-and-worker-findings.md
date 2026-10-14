# 打包链与 worker 协议：本机实测记录（6.4/7.1 前置）

日期：2026-10-14 00:30–01:20。目的：在**本机**把 Windows 安装包打出来（任务 6.4/7.1 的前置）。两条结论：①安装包卡在 GitHub 下载，已找到离线绕法；②**冻结包能起来，但 `--desktop-local-worker` 协议在本机不回复，仓库自带的 bundle 验证门与 worker 测试都因此失败/挂死**——这条我尚未根因化，如实记录。

## 1. `npm run dist:win` 的真实结果

- 任务报 **exit 0**，但**没有任何产物**（`desktop/release` 不存在）。
- 直接跑 `electron-builder --win` 得到真实原因：
  `Get https://github.com/electron/electron/releases/download/v33.4.11/electron-v33.4.11-win32-x64.zip … read tcp 192.168.0.112→20.205.243.166:443: wsa…`
  → **GitHub release 下载不通**（pypi.org / registry.npmjs.org / python.org / nuget.org 均正常）。
- 离线绕法是现成的：`%LOCALAPPDATA%\electron\Cache\…\electron-v33.4.11-win32-x64.zip`（109.7 MB，版本正好）与已解包的 `desktop/node_modules/electron/dist`（33.4.11，188 MB）→ 可用 `electronDist` 覆盖，**不需要 GitHub**。
- 另外 `extraResources` 还要求 `desktop/build/dist/cowagent-backend`（PyInstaller 后端 bundle）；`resources/app-config.json`、`resources/themes` 被 gitignore（flavor 产物，标准构建可缺）。

## 2. 后端 bundle：conda Python 造不出来，python.org 3.11 可以

| 构建解释器 | 前置检查 | PyInstaller | 结果 |
|---|---|---|---|
| micromamba **py312** | 技能依赖 ✓、语法 ✓ | 完成 | **bundle 起不来**：`ImportError: DLL load failed while importing _ssl`（micromamba 把 `libssl-3-x64.dll`/`libcrypto-3-x64.dll`/`sqlite3.dll`/`libexpat.dll` 放在 `DLLs\` 之外，PyInstaller 收不到） |
| python.org **3.11.9**（nuget 包解压到 `%TEMP%`，未动系统） | 技能依赖 ✓、语法下限 **3.11** ✓（789 个文件） | 完成（393 MB，`cowagent-backend.exe` 28.7 MB） | **能运行**：直接启动会打印 banner、绑定 **9899**、工作集 120 MB；`--desktop-local-probe` 报 `probe ok: frozen=True` ✓ |

即：**DLL 问题由 3.11 修好**（复核记录：micromamba py312 → `_ssl` 导入失败；python.org 3.11.9 → 正常）。

## 3. 更正（2026-10-14）：worker 本身正常——先前"死锁"结论已推翻

**先前结论**：worker 在 `_emit()` 的 `writer.flush()` 上死锁，属产品缺陷。
**更正后结论**：那是**父进程/本执行环境的交互假象**。两组决定性实验：

| 实验 | 做法 | 结果 |
|---|---|---|
| **A. 回复写文件** | 让 `serve()` 的 writer 落盘（日志同时改到 stderr） | **四帧全部正确**：hello / describe / `call read`（**真的读到** `1|bundle-check-marker-2f8a1c`）/ shutdown |
| **B. 一次性写完四帧、父进程只读** | `StdIn` 连写四帧后不再写，再用 `ReadToEndAsync` 只读 | **四帧全部经真实 stdout 管道返回**（8,225 B），worker 处理完 shutdown 正常退出 |

先前失败的形态**全部**是"父进程写一帧、随后阻塞在读上"：在这种交错下，本执行环境里的子进程收不到（或收不全）stdin，于是表现为"只答首帧"。同一形态也解释了仓库自带 `verify-backend-bundle.py` 在 `v3` 超时、以及 `tests/test_desktop_local_worker.py` 挂死（它们的父进程都是"写完就阻塞读"）。

## 3. 附：原"死锁"观察记录（保留原文，供对照）


**症状**：worker 答复**第一帧 `hello` 后就再无任何回复**，进程存活、stderr 全空。用 `faulthandler.dump_traceback_later` 打出线程栈，定位到：

```
Timeout (0:00:25)!
Thread 0x000068d0 (most recent call first):
  File "D:\AIPJ\rsmagent\agent\desktop_local\worker.py", line 472 in _emit
  File "D:\AIPJ\rsmagent\agent\desktop_local\worker.py", line 527 in serve
  File "…\worker-stall.py", line 30 in <module>
```

即 `_emit()` 里的 **`writer.flush()` 永久阻塞**（`worker.py:472`），主循环因此再也回不到读帧处。

**四处对照，排除"冻结特有"与"缺依赖"**（均由 .NET 父进程重定向 stdio、stdin 保持打开，避免外部工具 stdio 的干扰）：

| 被测 | 解释器 / 依赖 | hello | describe / call / shutdown |
|---|---|---|---|
| 冻结包 `cowagent-backend.exe --desktop-local-worker` | 包内 3.11 | **0.8s 回复 ✓** | 各 90s **无回复** |
| 源码 `-m agent.desktop_local.worker` | `.venv-build`（python.org **3.11.9** + 完整依赖） | **3.3s 回复 ✓** | 各 60s **无回复** |
| 源码 `-m …` | 裸 python.org 3.11（无依赖） | 立即报 `No module named 'requests'` 并打印 traceback **到 stdout** | 进程退出 |
| 仓库自带 `tests/test_desktop_local_worker.py` | 同上 | — | **挂死 >12 分钟**（卡在第一个真正拉起 worker 进程的用例 `WorkerProcessTests::test_a_bad_frame_does_not_kill_the_worker`） |

**关键旁证**：worker 的**日志与 traceback 都写在 stdout 上**（上表第三行可见 `[ERROR][…][worker.py:523]` 与 traceback 直接出现在协议流里）。协议本身就是"stdout 上一行一个 JSON"，因此**日志与协议争用同一个流**；`_emit()` 持 `write_lock` 调 `writer.flush()` 时的阻塞与这一点一致。

**判定（更正后）**：worker 协议与工具执行在本机**可用**（实验 A/B 双重证明）；`verify-backend-bundle.py` 的 `v3` 超时与 `test_desktop_local_worker.py` 的挂死**在本环境里**由"父进程写完即阻塞读"的交错方式触发，**需在受支持平台/CI 上复核**后才能判定是否为产品问题——不再当作已确认的产品缺陷。唯一被本轮**证实**的真实隐患是 `common/log.py:72-74` 的双重包裹（触发时协议流报 `ValueError: I/O operation on closed file`）。

**后续更正（同日）："本执行环境干预管道"这个解释已被推翻。** 用**任务计划程序**把 `verify-backend-bundle.py` 放到我的进程树之外运行，它**仍在 `v3` 超时**（`::error::no reply to 'v3' within 180.0s`），说明失败与我的执行环境无关。目前**确定**的是：①worker 逻辑与工具执行正常（实验 A/B 双重证明）；②失败只出现在"**写一帧 → 等回复 → 再写一帧**"的交错形态，而**一次性入队四帧**或**回复写文件**都不失败；③精确机制**尚未证明**——`common/log.py:72-74` 的双胞胎 stdout wrapper 是首要嫌疑（它导致共享缓冲被 GC 关闭的危害已单独复现），但把日志改到 stderr 的尝试**没有**修好交错形态。要定论需在**受支持平台/CI** 上复跑该门。

## 3.2 机制确认（本轮）：日志与协议共用 stdout，且被包成两个独立缓冲

`common/log.py:72-82`（复核原文）：

```python
stdout = sys.stdout
if hasattr(stdout, "buffer"):
    stdout = io.TextIOWrapper(stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
console_handle = logging.StreamHandler(stdout)
log.addHandler(console_handle)
```

即在 `sys.stdout.buffer`（与协议同一个 fd）上**再包一层新的 `TextIOWrapper`** 交给日志处理器——**同一 fd 两个独立缓冲的写者**，而 worker 协议用的仍是原来的 `sys.stdout`。

支持该机制的观察：

1. worker 的**日志行与 traceback 直接出现在协议流里**：裸解释器那次运行，父进程在 **stdout** 上读到 `[ERROR][2026-10-14 10:07:18][worker.py:523] - [DesktopWorker] unexpected failure: No module named 'requests'` 与随后整段 traceback；
2. 同一份日志（`run.log`）显示那次 `describe` **确实被处理了**（`worker.py:434` 记录 `unexpected failure: 'read'`），但它的**协议帧从未到达父进程**——与"输出被两个缓冲互相吞掉"一致；
3. 该失败**与父进程实现无关**：
   - `.NET Process`（重定向 stdio、stdin 保持打开）→ 冻结包与源码都只答复首帧；
   - **pytest 的 stdout/stderr 直接重定向到文件（全程无管道）**→ 进度点停在 `...........sssss.....`，即**第 21 个用例（第一个真正拉起 worker 的用例）挂住 10 分钟以上**；
   - 我早前用 Python 管道的探针甚至连首帧都收不到——那部分是**外层 stdio 转发的假象**，已在本轮更正。

**仍未证明**：`flush()` 在 OS 层究竟等在哪把锁上（有线程栈与共享 fd 的结构证据，但没有锁级证明）。**修法候选**（尚未实施，属本 change 范围之外）：① worker 模式下日志写 stderr；② 不重新包裹 `sys.stdout.buffer`（复用同一个对象）；③ 在 `worker.main()` 里把根日志器的控制台处理器改到 stderr。

## 3.0 早先的观察（保留）

`desktop/build/verify-backend-bundle.py`（仓库自己的 bundle 门）在冻结包上失败：

```
==> Verifying the bundle can actually run a tool
::error::no reply to 'v3' within 180.0s; stderr:
==> ok: no uncompilable modules in the PyInstaller graph
==> probe ok: frozen=True bundleRoot=…\cowagent-backend\_internal
bundle verification refused the bundle
```

我做了三组对照，观察如下（**均为本机实测**）：

| 场景 | 结果 |
|---|---|
| 把 `hello` 帧**管道进去后立即关闭 stdin**（frozen 与源码 worker 各一次） | **正常回复**：`{"id":"v1","status":"success","protocol_version":1,…,"tools":["read","write","edit","bash","ls","search_files"]}` |
| 保持 stdin **打开**、用线程读回复（frozen 与源码、各 25–300 秒/步、含 `PYTHONUNBUFFERED=1` 与产品自带 `build_worker_env()`） | **始终零回复**，进程存活、stderr 全空（frozen 300 秒/步×3 步 = 900 秒无输出） |
| 仓库自己的 `tests/test_desktop_local_worker.py`（它的 harness 与我的等价：`-m` + `build_worker_env()` + 保持 stdin、`readline()`） | **挂死 >12 分钟**，被我终止 |

**已知与未知**：worker 的读入是 `for line in reader:`（流式），`_emit()` 里确有 `writer.flush()`，桌面端 `worker-session.ts` 也保持 stdin 打开并按 id 匹配回复——所以"保持 stdin 打开却收不到回复"与代码意图相反，我**尚未根因化**（未排除：导入阶段的极慢/阻塞、stdio 被包装后 flush 落不到真实管道、或本机环境相关的其它因素）。这不是"缺 DLL"那类问题（那已由 3.11 修好），也不是我的改动引入的（本 change 不触碰 worker/fs-guard；上述对照在**源码** worker 上同样复现）。

## 3.1 出包最终成功（三个绕法都用上了）

| 环节 | 问题 | 绕法 |
|---|---|---|
| Electron 二进制 | `github.com` release 下载超时 | `--config.electronDist=<repo>\desktop\node_modules\electron\dist`（已解包 33.4.11）+ `ELECTRON_OVERRIDE_DIST_PATH` |
| NSIS 二进制 | `nsis-3.0.4.1.7z` 从 GitHub 超时 | `ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/`（**862 ms / 687 ms** 下完） |
| 后端 bundle | conda Python 造出的包缺 `libssl` 等 DLL | 用 python.org **3.11.9**（nuget 包解压到 `%TEMP%`，未动系统）重建 |

产物（2026-10-14 09:21）：

| 文件 | 大小 | 备注 |
|---|---|---|
| `desktop/release/容大AI-Setup-2.2.0-x64.exe` | **234.5 MB** | NSIS、`oneClick=false`、`perMachine=false`（用户级安装，可卸载） |
| `…exe.blockmap` | 0.2 MB | 差分更新用 |
| `desktop/release/latest.yml` | — | 更新元数据 |
| `desktop/release/win-unpacked/容大AI.exe` | 715 MB 目录 | 未安装即可直接运行，供 7.1 冒烟 |
| SHA-256（安装包） | `091A6B53DF5315434AC70262A0A42CF9B22A2CFBF64A6917BD9C4C41B5C686C0` | |

包内自带后端：`resources/backend/cowagent-backend/cowagent-backend.exe`（31 MB）+ `_internal`（2,764 个文件）；签名被跳过（无证书信息，本机构建预期如此）。

**仍未通过的门**：`desktop/build/verify-backend-bundle.py` 的 `v3` 工具调用（见 §3），因此 6.4/7.1 **尚不能判定通过**——安装包已可产出，但"bundle 能跑工具"这一条尚未成立。

## 4. 影响与判定

- **6.4 / 7.1 不能声称可交付**：安装包的前置之一（后端 bundle 通过仓库自己的门）**未通过**；即便绕开 GitHub 下载打出安装包，装出来的应用在本机项目执行路径上会撞上同一个 worker 行为。
- 这条与 A04/A05 一类"真机执行工具"的验收**同源**，建议作为独立缺陷排查（本 change 的范围是加固与验收，不在未定位前改动 worker）。
- **未做**：用 `electronDist` 跑通 electron-builder 出包（下一步）；flavor 产物生成（标准构建可缺）。

## 5. 复现命令

```powershell
# 1) 观察 electron-builder 的真实失败（GitHub 下载）
cd desktop; .\node_modules\.bin\electron-builder.cmd --win

# 2) 用 python.org 3.11 重建后端 bundle（脚本已放到 %TEMP%\cow-pack\build-backend.ps1）
powershell -File %TEMP%\cow-pack\build-backend.ps1 -Python %TEMP%\cow-py311\python\tools\python.exe

# 3) 仓库自己的 bundle 门
.\.venv-build\Scripts\python.exe desktop\build\verify-backend-bundle.py

# 4) worker 协议（保持 stdin 打开 → 本机收不到回复；关闭 stdin → 立刻回复）
'{"id":"v1","op":"hello","root":"<目录>","temp":"<目录>\.cow-tmp"}' | .\desktop\build\dist\cowagent-backend\cowagent-backend.exe --desktop-local-worker
```

（构建脚本、python.org 3.11.9 与探针脚本都放在 `%TEMP%` 下，未写入仓库、未改系统；`desktop/build/.venv-build` 的 py312 版本已改名为 `venv-build.py312.*` 保留，未删除。）
