# 发布平台 / 最低版本 / Python 打包与依赖清单（P0 / A33 / A36，任务 1.4）

本文是任务 1.4 要求的**发布面盘点**：现行发布平台、最低系统版本、Python 打包方式与
库锁定清单，以及 cwd/落盘工具按平台的分类。所有条目都注明**来源**：CI 矩阵、构建脚本、
契约文档或上游工具链的**声明下限**。凡是只在开发机上验证过、未进安装包验证的内容，
在 §5 明确列为未完成，不当成已验收。

盘点方式：读 `.github/workflows/release*.yml`、`desktop/package.json`、
`desktop/build/*`、`desktop/electron-builder*.js`、`contracts/desktop/*.json`，
配合 `evidence/execution-boundary-map.md`（工具与 cwd 现状）。

## 1. 现行发布平台与目标架构

来源：`.github/workflows/release.yml` 的 `strategy.matrix`。

| 平台 | Runner | 目标 | electron-builder 参数 | 产物 |
| --- | --- | --- | --- | --- |
| macOS arm64 | `macos-14` | arm64 | `--mac --arm64` | dmg + zip |
| macOS x64 | `macos-15-intel` | x64 | `--mac --x64` | dmg + zip |
| Windows x64 | `windows-latest` | x64 | `--win --x64` | NSIS `Setup.exe` |

- **Linux 不是发布目标**：没有任何 workflow 构建 Linux 产物。`contracts/desktop/v2.json`
  的 `platforms.posix` 覆盖的是**隔离家族**（同一条 `sandbox-exec`/POSIX 路径），不是
  "已发布 Linux 安装包"。
- **架构正确性是构建期硬门槛**：`release.yml` 用 `lipo -archs` 断言后端二进制的架构与
  矩阵一致（arm64 job 里出现 x86_64 即失败）。Python 与 PyInstaller 跟随宿主解释器架构，
  在 Apple Silicon 上以 Rosetta 编译会产出错架构后端，所以这条不是形式检查。
- **叠加渠道**（`.github/workflows/release-overlay.yml`）：同一份 `desktop/` 树在构建期
  套用私有 overlay（产品码/品牌），产物直传 R2，feed 在**所有平台齐了之后**才发布到根。
  它不新增平台，只新增品牌变体；因此平台清单仍是上表三行。
- **Win7/8/8.1 传统渠道**（`.github/workflows/release-win7.yml`）：按需手工触发，把
  两半都钉到最后支持 Win7 的版本——Electron 22.3.27（Chromium 108）与 Python 3.8。
  该 workflow 自己写明：主线用的 Electron 33 + Python 3.11 **都不**支持 Win7。

## 2. 最低系统版本

| 平台 | 最低版本 | 来源与性质 |
| --- | --- | --- |
| macOS（主线） | Electron 33 所支持的下限（macOS 10.15+） | **上游工具链声明下限**，非本项目探针结论。`package.json` 未设 `minimumSystemVersion`，故取 Electron/Chromium 130 的声明下限。 |
| Windows（主线） | Windows 10+ | 同上：Electron 33 已不支持 Win7/8/8.1。 |
| Windows（传统渠道） | Windows 7 SP1 | Win7 需 SP1 + `KB2533623`（或累积更新 `KB4457144`），否则随包 Python 3.8 仍启动失败。见 `release-win7.yml` 头注释。 |

**这是本文最弱的一环**，必须如实标注：仓库里**没有任何一处**（README、package.json、
构建脚本、契约文档）写明最低系统版本，上表 macOS/Windows 两行是从所钉的 Electron 版本
**推导**出来的，不是实测结论。P0 的"平台方案可复核"因此在本条上只到"可复核到上游声明
下限"这一层，安装包级的实测在任务 10.1—10.3。

## 3. Python 打包

来源：`desktop/build/build-backend.sh`、`desktop/build/cowagent-backend.spec`、
`desktop/package.json`。

- **形态**：PyInstaller `onedir`（不是 onefile），产物
  `desktop/build/dist/cowagent-backend/`，由 `electron-builder` 的 `extraResources`
  放进安装包，落到 `resources/backend/cowagent-backend`。
- **解释器选择**：优先 Python 3.11（3.13+ 的 `web.py` 必须从 GitHub 源码装，易受网络影响），
  依次退到 `python3.12` / `python3.10` / `python3`；可用 `PYTHON=` 覆盖。实际打包解释器
  版本是构建期间接证据（构建日志），**未**写入包内元数据。
- **隔离构建环境**：`desktop/build/.venv-build`，依赖安装失败即删除 venv，避免半成品
  venv 被下次复用。
- **技能依赖门槛**：`check-skill-dependencies.py --verify-imports` 在 PyInstaller **之前**
  运行，用技能 frontmatter 声明的 `requires.python` / `install: pip` 与本文件比对并真实
  import。这是 `xlsxwriter` 那类"输出期才炸"的防回归点，已接进 `release.yml`。
- **签名/公证**（macOS）：`electron-builder.js` 注入 `mac.binaries`（约 180 个
  PyInstaller 的 `.so/.dylib`，逐个人工 `deep-sign` 不可行），CI **不公证**
  （`notarize: false`）：Apple 公证服务对这个大包常"In Progress"数小时，CI 等不起，
  公证拆成 CI 产出签名 dmg 之后的**人工本地步骤**。
- **签名**（Windows）：`electron-builder.win.js` 接签名 CLI（EV 私钥在硬件内，2023 后
  不可导出）；`SIGNTOOL_CERT_CODE` 缺失即整体跳过签名，`COW_SIGN_DRY_RUN=1` 走
  `--dry-run`，使整条管线可在 CI 用自签证书验证。

## 4. 库锁定清单

| 层 | 文件 | 锁定强度 |
| --- | --- | --- |
| Python（桌面后端） | `desktop/build/requirements-desktop.txt` | **区间约束，无哈希锁**。含按 Python 版本分支的条件依赖（`aiohttp<3.10; python_version<"3.13"`、`web.py @ git+...; python_version>="3.13"`），逐构建期解析。 |
| Python（Win7 渠道） | 同文件在 workflow 内改写成 `/tmp/requirements-win7.txt` 的**放宽副本** | 更弱：为 Python 3.8 去掉不可用版本，仅该 job 可见。 |
| Node/Electron | `desktop/package-lock.json` + `package.json` 的 `overrides`（`js-yaml 4.3.1`、`linkify-it 5.0.2`） | npm lockfile（哈希完整），加两处安全覆盖。 |
| 技能脚本库 | 由技能 frontmatter 声明，`check-skill-dependencies.py` 与上面的 Python 清单互校 | 构建期强制一致；不独立锁版本。 |

**明确的未完成项**：Python 侧没有哈希锁（无 `pip-compile` 产出的 `--generate-hashes` 文件）。
三条平台的 wheel 哈希不同，跨平台单一锁文件需要按平台各出一份或引入平台标记，属构建流程
变更，不在本 change 范围。因此"A36 依赖可复核"当前只做到：**清单文件 + 构建期真实 import
校验收**，而不是"可复现的字节级锁定"。这条必须留在 §5，不能靠"有清单"结项。

## 5. cwd / 落盘工具分类（按平台）

分类事实来源：`evidence/execution-boundary-map.md` §1（`device-ops.ts::SUPPORTED_OPS`、
`fs-guard` 的穷举 `match` 与 `UnknownOp` 断言）、`contracts/desktop/v2.json` 的 `tools`。

| 面 | 工具/op | 平台范围 | 性质 |
| --- | --- | --- | --- |
| v1 只读 helper | `list` `stat` `search` `read_text` | 全部平台（node fs-guard） | 只读；`exec` 与 `write/unlink/rename/shell/chmod/spawn` 显式 `UnknownOp` |
| v2 只读 | `read` `ls` `search_files` | `posix` 已接受；`win32` **不支持** | 可并行，`readonly_parallel_per_project` |
| v2 落盘 | `write` `edit` | `posix` 已接受；`win32` 不支持 | 结果效应 `files_written`；工作区相对路径 |
| v2 脚本 | `bash` | **仅 `posix`**；`win32` 明确拒绝 | 隔离脚本；`win32` 走 `unsupported_platform`(422)，未来若接受 Windows 启动器仍以 `feature_unavailable`(503) 拒绝 `bash`，绝不翻译成 POSIX 语义（本次已把契约文本与 broker 两段拒绝对齐并加断言） |
| cwd 敏感工具（主进程装配面） | `read` `write` `edit` `bash` `search_files` `ls` `web_fetch` `send` `browser`（`agent/protocol/agent.py::_CWD_TOOLS`） | 全部平台 | cwd 在**装配期**写入、取 Agent 时**原地改写**，正是本次要用逐轮不可变执行目标替换的对象 |

结论与本 change 的接口关系：`win32` 在 v2 契约里是**不可用平台**，不是"落盘工具少一个"。
平台支持表与 `tool_supported_on` 是本 change 唯一的能力闸门，`execution_isolation` 配置值
不能替代它。

## 6. 未完成项（不勾选 1.4 的部分，均已归入对应任务）

- 最低系统版本的**实测**（安装包在旧系统上真实启动）——任务 10.1—10.3。
- Python 依赖的哈希锁定——见 §4，属构建流程变更，本 change 不做，需单开条目。
- 安装包内（签名后）启动器与随包 Python 的探针——任务 1.5 / 4.9 / 10.2。
- Windows 平台启动器与探针——任务 4.5，当前 `resolveScriptSupport` 在 win32 直接拒绝。
