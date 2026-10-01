# 打包后端（冻结 worker）本机实测：两处 P0 缺陷与两道新增构建门槛

对应任务 **10.1 / 10.2 的「打包与冻结 worker」部分（未完成整包验收）**，并为
**10.7** 提供安装包结果的**第一段真实证据**。本文只记录**本机实跑**的内容与**明确未跑**
的内容，不含推测。

实测环境（精确版本）：

| 项 | 值 |
| --- | --- |
| 机器 | macOS 26.2，arm64 |
| 构建解释器 | CPython 3.11.15（uv 管理） |
| PyInstaller | 6.22.3 |
| setuptools / pip | 79.0.1 / 26.x |
| 构建方式 | `desktop/build/cowagent-backend.spec`（onedir），`bash desktop/build/build-backend.sh` 与 CI 的 `pyinstaller … --distpath/--workpath` 等价 |
| 产物 | `desktop/build/dist/cowagent-backend/`（onedir，413MB） |

> 为什么值得单独立文：**这三处缺陷都不会让 CI 变红**。PyInstaller 编译不了某个模块时只在
> warnfile 记一行 `invalid module named …` 并继续出包，于是故障形态不是"构建失败"，而是
> **"桌面能启动、但任何本地命令都跑不了"**。本机实跑冻结二进制才发现。

## 1. 缺陷 D1：`excludes=['wheel']` 让 PyInstaller 直接失败（构不出包）

现象（PyInstaller 6.22.3 + setuptools 79.0.1，全新 `pip install pyinstaller` 即复现）：

```
ValueError: Target module "wheel" already imported as "ExcludedModule('wheel',)"
```

根因：`wheel` 未安装时，setuptools 的 PyInstaller hook 会把**自带的** `setuptools._vendor.wheel`
别名到 `wheel` 名字上；而 `alias_module()` 遇到目标名字**已在图里**（此处是 `ExcludedModule`）
是**硬错误**，不是警告。也就是说被 exclude 掉的那个名字，正是别名要来满足的导入。

修复：`desktop/build/cowagent-backend.spec` 里**移除** `'wheel'`（保留 `'pip'`，setuptools 不
vendor 它），并在 spec 内写明原因，避免下次又被当成"可以瘦身的 exclude"加回去。

## 2. 缺陷 D2：`agent/tools/bash/bash.py:57` 在 3.11 上是语法错误（出包但不能跑工具）

冻结包**构建成功**，但第一帧 `hello` 就失败：

```
[DesktopWorker] unexpected failure: No module named 'agent.tools.bash.bash'
  File "agent/tools/__init__.py", line 9, in <module>
    from agent.tools.bash.bash import Bash
```

warnfile 里只有一行（`invalid module named agent.tools.bash.bash`），PyInstaller 不失败、照常出包。
`agent.tools` 在**启动时**就 import `Bash`，所以在随包的 3.11 上**整个应用 import 失败**，
不只是这一个工具。

根因（可直接复现，不依赖 PyInstaller）：

```
>>> compile(open('agent/tools/bash/bash.py').read(), 'bash.py', 'exec')
SyntaxError: f-string expression part cannot include a backslash (bash.py, line 57)
```

第 57 行把 `'\n'` 写进了 f-string 的**表达式**里。PEP 701 从 **3.12** 起才允许，而
`requirements-desktop.txt` / `build-backend.sh` / 两个主线 workflow 都指定 **3.11**。
**开发机的 venv 是 Python 3.14**（本机实测），因此本地测试、本地 import 全绿。

修复：把换行提到表达式之外（`_PLATFORM_LINE` 类属性），描述文本逐字不变（`{{cwd}}` 占位、
POSIX 下不带 Windows 提示均已断言）。

## 3. 缺陷 D3：`Scene/` 技能策略同类语法错误（随包发布且被执行）

`Scene/finance_report_audit/skills/financial-reprot-audit/strategies/related_party.py:295`
用了 f-string 内**重复引号**（`f'{'已' if … else '未'}…'`），同样是 PEP 701；该文件作为
**data 随包发布并由随包解释器执行**，在 3.11 上"到货即死"。修复：该片段外层改用双引号。

全仓 3.11 扫描结果：**1234 个 .py 中只有上述 2 个**编译失败，其余全部干净。

## 4. 新增的两道构建期门槛

两道门槛都接进了 `desktop/build/build-backend.sh` 与四个发布 workflow
（`release.yml`、`release-overlay.yml`、`release-win7.yml`、`release-overlay-win7.yml`），
并在 `.gitignore` 单独放行脚本本身——**门槛脚本没进 runner 等于没门槛**，
而 workflow 会以"文件不存在"失败，掩盖它本该抓的问题。

### 4.1 `desktop/build/check-python-syntax.py`

以**构建解释器**逐文件 `compile()` 全部**随包** Python（跳过 `.git/.github/node_modules/
site-packages/dist/build-*`，以及 `tests/`——spec 已 exclude `tests`，桌面也不跑仓库套件），
命中即列出 `文件:行:原因` 并失败。

- 扫的是随包内容，**包括 `Scene/` 这类 data**，因为它们同样会被执行。
- `--floor X.Y` 断言下限：主线 3.11，Win7 线 **3.8**（`python-version: "3.8"`，是两者中更严的）。
  运行解释器**低于**下限时拒绝运行（`ast.parse(feature_version=…)` 不覆盖 PEP 701 这类
  tokenizer 级差异，实测无效），**高于**下限时明确打印"绿不等于覆盖 3.11"。

实测：3.11 → 685 文件全绿；3.8.20 → 685 文件全绿（本机用 `uv python install 3.8` 实跑）。
`python -m pytest tests/test_evolution_safety.py` 所在的测试文件是 3.8 唯一不兼容项，
它不随包发布，故按"所售即所检"排除。

**一次自身的缺陷（已修）**：本门槛最初只跳过 `dist/build/tests/…`，等到真的产出 `.app` 之后，
它开始扫描包内 `_internal/` 里的**第二份源码副本**（`agent/`、`channel/`、`Scene/`…），
文件数从 685 虚增到 892——即"对产物做检查并重复计数"。已补上 `release` 与 `*.app` 跳过规则，
并断言回到 685（且 `.app` 内文件计入数为 0）。

### 4.2 `desktop/build/verify-backend-bundle.py`

出包之后、打包之前跑，三段：

1. **warnfile 检查**：出现 `invalid module named` 即失败并逐行打印。
   （`missing module named` 不算错——可选依赖缺席是预期，代码已降级。）
2. **`--desktop-local-worker --probe`**：断言 `frozen=true`、`realPath` 就是包内二进制、
   `bundleRoot` 存在。**注意一个真实差异**：PyInstaller 下 `sysconfig` 报的
   `stdlib`/`purelib` 指向**构建机布局**、在包里并不存在，这是正常的；冻结态真正要 grant 的
   是 `bundleRoot` 与可执行文件所在目录（`interpreterReadPaths` 的冻结分支），故只断言后者。
   这条正是"本地能跑、装机后 worker 起不来"的现场检查。
3. **真实 stdio 会话**：起冻结进程，跑 `hello` → `describe` → 一次真实 `read`（读一个临时目录里
   的哨兵文件）→ `shutdown`，断言六个主工具全部就位且读回内容。这一步是 D2 的**回归测试**。

### 4.3 出包后对**构建目录**的即时校验（`.app` 之前）

```
$ pyinstaller desktop/build/cowagent-backend.spec --noconfirm --distpath desktop/build/dist --workpath desktop/build/build-work
pyinstaller exit=0
no invalid modules

$ python desktop/build/verify-backend-bundle.py
==> ok: no uncompilable modules in the PyInstaller graph
==> probe ok: frozen=True bundleRoot=…/cowagent-backend/_internal
==> bundle ok: read/write/edit/bash/ls/search_files all answered over stdio
```

回归（`.venv/bin/python -m pytest … -q -p no:randomly`）：

- bash 与 worker 相关子集 **165 passed, 3 skipped**；
- 桌面本地/远程子集（`test_desktop_local_context/root/script_tool/input_staging/e2e` +
  `test_desktop_remote_execution_acceptance/dispatch`）**299 passed, 5 subtests passed**
  （交替复跑各 2/2 一致；期间观察到的偶发失败见 §5）。

### 4.4 真正打包出 `.app` 后发现的两件事

用 `npm run build && electron-builder --mac --dir --arm64` 产出真实安装件
`desktop/release/mac-arm64/容大AI.app`（735MB，**未签名**：本机证书全部过期/不受信，
electron-builder 打印 `0 valid identities found` 后跳过签名；也**不是 dmg/zip**，`--dir` 只出目录）。
把 `verify-backend-bundle.py --distpath …/Contents/Resources/backend` 指向**包内**的 bundle
（而不是构建目录）后，先暴露出下面这个缺陷。

#### 缺陷 D4：`contracts/desktop/*.json` 没随包发布 → `/api/desktop/meta` 在装机后 500

前置验证：包内 worker 本身是好的（`--desktop-local-worker --probe` 报 `frozen=true`、
`realPath` 指向包内二进制、arm64 与宿主一致），所以这不是 D2 那一类。接着把包内后端**当服务器起起来**
（`COW_DESKTOP=1`）——它**正常启动**：Web 控制台、各通道、scheduler、evolution 全部就绪，
`GET /chat` 返回 **200 / 297,106 字节**（`chat.html` 与 `assets/js/*` 模板/静态资源都在），
说明模板与静态资源这条线是好的。但：

```
$ curl http://127.0.0.1:9899/api/desktop/meta
<class 'FileNotFoundError'> at /api/desktop/meta
  File "auth/desktop_contracts_v2.py", line 37, in <module>
  File "auth/desktop_contracts.py", line 49, in <module>
  File "auth/desktop_contracts.py", line 45, in load_contract
FileNotFoundError: [Errno 2] No such file or directory:
  .../容大AI.app/Contents/Resources/backend/cowagent-backend/_internal/contracts/desktop/v1.json
```

根因：`contracts/` 是**数据**，spec 的 `datas` 里**没有它**；而 `load_contract()` 在
`auth/desktop_contracts.py` 的**模块作用域**执行、`auth/desktop_contracts_v2.py` 又 import 它，
所以 `/api/desktop/meta`——**桌面做 v2 协商的那个入口**——在装好的应用里直接 500。

修复：spec 的 `datas` 增加 `(rp('contracts'), 'contracts')`（64KB，`v2.json` 与 `samples/` 一并随包，
使"客户端被检查所依据的契约"与"服务端实际执行的契约"是同一份）。

**同类排查**：全仓搜 `dirname(dirname(abspath(__file__)))` 形式的**随包**仓库相对读取，
只有这两个契约文件（其余命中都在 `scripts/`（不随包）、`Scene/`（读自身目录）、`cli/`（读自身目录，且已随包）），
故这一处即为该类的全部。

#### 4.5 两道门槛因此各加了一环

- `check_shipped_data()`：断言**代码用仓库相对路径打开的**数据文件确实在包内
  （`contracts/desktop/v1.json`、`v2.json`、`channel/web/chat.html`、`templates`、`static`、`skills`），
  每条都注明消费方。这类缺口 PyInstaller 不会失败，只会在装机后第一次请求时炸。
- `check_meta_endpoint()`：把**包内后端当服务器起起来**（`COW_DATA_DIR`/`HOME` 指向临时目录，
  `web_port` 写进临时 `config.json`，因此不碰操作者自己的数据与配置），
  轮询 `GET /api/desktop/meta` 直到 200，并断言信封里有 `protocols`/`features`/`entry_path`
  且 `project_execution` 协议**被声明**。这是构建期能做到的、最接近"装好的应用能用"的检查——
  D4 正是它抓到的。（`COW_SKIP_APP_CHECK=1` 可跳过，供无网络的构建环境使用。）

#### 4.6 最终实测输出（对**包内** bundle，非构建目录）

```
$ python desktop/build/verify-backend-bundle.py --distpath "…/容大AI.app/Contents/Resources/backend"
==> ok: no uncompilable modules in the PyInstaller graph
==> probe ok: frozen=True bundleRoot=…/容大AI.app/Contents/Resources/backend/cowagent-backend/_internal
==> bundle ok: read/write/edit/bash/ls/search_files all answered over stdio
==> ok: 6 shipped data paths present
==> ok: packaged backend served /api/desktop/meta
         (protocols=['bridge','files','project_execution','web_session'],
          features=['local_files','local_processing','notifications'])
in-app verifier exit=0
```

架构核对：包内后端可执行文件与 `.app` 主可执行文件均为 **arm64**（`lipo -archs`），
无 Rosetta 混装。

### 4.7 真正的安装件：dmg 与 zip（仍未签名）

`--dir` 只产出目录，不是"安装包"。补跑真正的目标：

```bash
cd desktop && ./node_modules/.bin/electron-builder --mac dmg zip --arm64 --publish never
```

产出（`artifactName` = `${productName}-${version}-${arch}.${ext}`，与 `package.json` 一致）：

| 产物 | 大小 | SHA-256 |
| --- | --- | --- |
| `容大AI-2.1.9-arm64.dmg` | 270 MB | `15c84151749e6ed1a9e9d92720be46afc7f047926470bf5f8fccf6433cac9f99` |
| `容大AI-2.1.9-arm64.zip` | 264 MB | `8de4c5ca8f1114d6ebc5c44c9d5949b4ff252291d5d1498203c96bf80e6b1234` |

**签名**：本机 `security find-identity` 只有 4 个**已过期/不受信**的身份，
electron-builder 打印 `0 valid identities found` 并**跳过签名**（`hardenedRuntime` 已配、
entitlements 已配，但没有可用证书）。因此这两个产物是**未签名**的：
它们不是可发布件（Gatekeeper 会拦），但**包内容与启动行为**可被完整验证。

**在产物内部验证**（不是构建目录，也不是 `.app` 目录）：

- **dmg**：`hdiutil attach -readonly -nobrowse` 挂载 → 卷内为
  `容大AI.app` + `Applications -> /Applications`（拖拽安装布局），
  `CFBundleShortVersionString=2.1.9`、`CFBundleIdentifier=com.cowagent.desktop`、`arm64`；
- **zip**：解出 `容大AI.app`（694 MB），版本与架构相同。

对**只读挂载卷内的** bundle 跑全部校验，三种产物形态结果一致：

```
$ python desktop/build/verify-backend-bundle.py --distpath "<dmg|zip|.app>/…/Resources/backend"
==> ok: no uncompilable modules in the PyInstaller graph
==> probe ok: frozen=True bundleRoot=…/backend/cowagent-backend/_internal
==> bundle ok: read/write/edit/bash/ls/search_files all answered over stdio
==> ok: 6 shipped data paths present
==> ok: packaged backend served /api/desktop/meta
         (protocols=['bridge','files','project_execution','web_session'], …)
verifier exit=0

$ COW_A26_BACKEND="<…>/Resources/backend" node --test tests/test_desktop_local_execution.cjs
ℹ pass 44   ℹ fail 0   ℹ skipped 15
```

即：**安装件里的**冻结 worker 能在**只读**卷上完成握手、跑通六个工具、答出 v2 协商，
并且越界仍由**内核**拒绝（`Operation not permitted`）。这比在构建目录上验证更强：
它经过 electron-builder 的拷贝、`extraResources` 重排与只读挂载。

**仍未做的**（不得据此声明 10.1/10.2 通过）：**签名与公证**（无可用身份）、
**安装到 `/Applications` 后的 Gatekeeper/quarantine 行为**、**macOS x64**、
以及 Electron 主进程按包内路径发现 worker 的**主链路**。

## 5. 回归口径：与变更前基线逐条比对

**基线**：`/tmp/rsm-base`，`git log -1` = `7d4cf3db`，与工作区 `HEAD` **同一 commit**（即变更前）。
同一选择 `-k "desktop or workspace"` 在两棵树各跑一次（`-q -p no:randomly`）：

| 树 | 结果 |
| --- | --- |
| 基线 `7d4cf3db` | **9 failed, 450 passed, 3 skipped, 6737 deselected** |
| 工作区（含本 change） | **8 failed, 1506 passed, 3 skipped, 6820 deselected, 124 subtests passed** |

工作区的 8 个失败是基线 9 个的**真子集**，且全部属于"读本机真实部署数据"那一类（仓库既有约定）：

- `tests/test_memory_global_config.py`（6 项，工作区/记忆全局配置）；
- `tests/test_session_idor_closure.py::test_workspace_root_refuses_empty_tenant`；
- `tests/test_tenant_create_containment.py::…::test_create_without_base_rejected_when_workspace_is_default_root`。

**基线独有、工作区已不失败的那 1 项**是
`tests/test_desktop_web_pages.py::test_the_console_asks_the_adapter_and_nothing_else`
—— 该文件正是本 change 修改的（`M tests/test_desktop_web_pages.py`），"不带改动时红、带改动时绿"
是预期方向。

结论：**本 change 未引入任何失败**。8 项为既有环境依赖（在变更前、同一环境下同样失败），
按仓库约定**不顺手"修"**、也不计入通过。工作区通过数 450 → 1506 是本 change 新增用例所致。

### 5.1 先前记录的偶发失败已收口

早前在打包窗口内观察到 `test_desktop_remote_execution_acceptance.py` /
`test_desktop_remote_dispatch.py` 偶发失败（每次用例不同）。已按同一方式收口：

- 那批桌面子集 **299 passed, 5 subtests passed**，与 HEAD 版 `bash.py` **交替**跑各 2/2 一致；
- `Bash` 的对外面（`name`/`description`/`params`/`get_json_schema()`）在改动前后**逐字节相同**
  （`diff` 为空），且代码中不存在对工具类做属性枚举之处，与本轮改动无因果；
- 上述全量选择下这两条**不再失败**。

它们属打包/索引压力下的时序抖动，**不在本 change 的失败集合里**。**根因未定位**，
如实留作独立跟进项，**不声明为已修**。

## 6. 明确未完成 / 不支持（不计入通过）

- **10.1 仍未勾选**：本机做到的是 **后端 bundle（PyInstaller onedir）** + **真实 `.app` 目录件**
  （`--dir`，735MB，arm64），并证明**包内的** bundle 能当 worker 跑通六个工具、能当服务器起来并答出
  `/api/desktop/meta`（v2 `project_execution` 已声明）。**仍缺**：**签名与公证**
  （本机 0 个有效签名身份，electron-builder 跳过签名）、**dmg/zip 产物**（只出了 `--dir`）、
  **macOS x64**、以及 Electron 主进程按包内路径发现 worker 的端到端主链路。
- **10.2 / 10.3 未完成**：未在签名安装包上跑本地/远程主链路；**A33 仍未测**（`release.md` §1 已登记）；
  Windows（x64 NSIS 与 Win7 3.8 线）**无真机**，只做了同一 spec 的**解释器下限静态检查**。
- **平台覆盖**：本机仅 macOS arm64；**macOS x64 未跑**。平台矩阵其余格保持未测
  （见 `platform-inventory.md` §5）。
- **证明范围的三层**（务必区分，本文只主张到第 2 层）：
  1. **后端 worker 级**：冻结二进制按装机方式成为 worker，跑通 master 六个工具 —— **已证**；
  2. **后端应用级**：包内后端起得来，模板/静态资源/契约都在，`/api/desktop/meta` 答出 v2 协商 —— **已证**；
  3. **整机主链路级**：Electron 主进程的沙箱授权、grant 生命周期、本地/远程对话与 UI —— **未证**
     （属 10.2/10.3，且需要真实模型与会话）。
  第 4 节修掉的四个缺陷全部落在第 1—2 层；第 3 层的问题本文**不能**排除。
