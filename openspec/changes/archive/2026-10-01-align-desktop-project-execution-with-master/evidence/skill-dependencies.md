# 技能依赖锁定与交付证据（任务 8.5）

对应任务：**8.5 按现有桌面打包流程锁定并交付 Python 与代表技能必需库；声明式额外依赖走
受控准备，缺失/不兼容提供明确错误。**

结论先说：**发现并修掉了一个真实缺口** —— 代表技能 `rfq-quote` 的交付物全部经由
`xlsxwriter` 写出，而桌面打包的锁定清单里**没有它**。因为导入是延迟的（在
`Report.__init__` 里），失败会发生在**产出阶段、沙箱子进程内**，报成一句
`ModuleNotFoundError`，而此时模型往往已经汇报过成功。本次把"声明 → 交付 → 校验"这条链
补齐，并让校验进入现有打包流程，使这类缺口在**构建期**失败而不是在客户现场失败。

## 1. 缺口是怎么来的（两个机制都在，但都没人校验）

| 已有机制 | 解析在哪 | 谁在用 | 问题 |
| --- | --- | --- | --- |
| `metadata.requires`（`bins`/`anyBins`/`env`/`anyEnv`，以及任何自定义 kind） | `agent/skills/frontmatter.py` | `agent/skills/config.py` 的 `should_include_skill`（**启用判定**） | 对"可选能力"是对的：密钥没配就对该技能隐身。但对"脚本无条件 import 的库"是错的——用户明确点名要它时，隐身不解决问题 |
| `metadata.install`（`SkillInstallSpec`：`brew`/`pip`/`npm`/`download`） | `agent/skills/frontmatter.py` | **没有任何代码消费** | 技能可以精确声明"怎么装",然后没人装 |

与此同时 `desktop/build/requirements-desktop.txt`（桌面包真正会带的库）是**手工维护、
且不与任何东西比对**的。三者相加，就是"声明了但没打包、也没有人报错"。

事实核对（用例钉住，避免结论漂移）：

```
skills/rfq-quote/scripts/quote.py:183:        import xlsxwriter      # Report.__init__ 内，延迟导入
agent/presets/workbuddy/shared/scenario_io.py:103:    import xlsxwriter  # render_workbook
desktop/build/requirements-desktop.txt            # 修复前：无 xlsxwriter
```
两个引用方都不是可选项：前者是 `rfq-quote` 每一个交付 Excel 的写出路径，后者是 workbuddy
预设的表格渲染路径。

## 2. 落地物

| 文件 | 作用 |
| --- | --- |
| `agent/skills/dependencies.py` | 声明模型 + 锁定清单解析 + 模块→发行名映射 + 校验（三种稳定错误码） |
| `desktop/build/check-skill-dependencies.py` | 打包守卫：不满足就 **非零退出**，附可执行的修复说明 |
| `desktop/build/build-backend.sh` | 在 PyInstaller **之前**调用守卫（`--verify-imports`） |
| `.github/workflows/release.yml` | 同上（现代线，Python 3.11） |
| `.github/workflows/release-overlay.yml` | 同上（overlay 线，Python 3.11） |
| `.github/workflows/release-win7.yml` | 同上，`--lock /tmp/requirements-win7.txt`（该线会改写锁定清单以放宽 playwright） |
| `desktop/build/requirements-desktop.txt` | 补入 `xlsxwriter`，并注明引用方与"谁在守着这份清单" |
| `.gitignore` | `desktop/build/*` 是整目录忽略、逐个源码文件白名单的；新守卫必须加 `!` 行，否则**永远不会进 CI** |
| `skills/rfq-quote/SKILL.md` | 补上声明：`requires.python: [xlsxwriter]` + `install: [pip xlsxwriter]` |
| `tests/test_skill_dependencies_lock.py` | 34 项：解析 / 映射 / 声明 / 校验 / 解释器窗口 / 仓库级不变量 |
| `tests/test_skill_dependency_guard.py` | 22 项：守卫退出码语义 + **接线**断言（含 gitignore 陷阱） |

## 3. 三种错误码，第三种才是容易做错的那个

- `dependency_missing` —— 声明了、能定位到发行名、锁定清单里没有。消息里带**技能名、
  模块名、发行名、该往哪个文件加**。
- `dependency_undeclared` —— 声明了，但**无法判断**哪个发行名提供它。这里**必须报**：
  把"我判断不了"静默当成"没问题"，就是本仓库此前踩过的"校验通过是因为它根本没查"。
- `python_incompatible` —— 解释器不在已声明窗口内。

**不兼容/缺失都不抛异常，而是收集成列表**：调用方要的是完整清单，不是第一个问题。

## 4. 两处设计上的自我纠正（都是用例先红）

### 4.1 "模块名就是发行名"的兜底会把校验整体废掉

第一版在映射表之外加了兜底：查不到就按模块名当作发行名。语义上"无害"，实际上让**每一个
未知 import 都解析到某个发行名**——于是没打包的库会被判为已交付。用例
`test_an_unknown_module_is_none_not_a_guess` 直接把它照出来。

修法：删掉兜底，把同名映射**显式列进表里**（`requests`/`openpyxl`/`numpy`…）。
"在表里"从此等于"本项目知道谁提供它"，未知仍然是未知。对照：`MODULE_DISTRIBUTIONS` 的
注释与 `module_distribution` 的 docstring 都记了这一条，避免以后有人"顺手简化"回去。

### 4.2 解释器窗口不能是一个凭感觉的数字

第一版写死 `>=3.10,<3.14`。守卫第一次跑起来就否掉了本机（3.14），于是去核对发布线：

```
.github/workflows/release.yml          python-version: "3.11"
.github/workflows/release-overlay.yml  python-version: "3.11"
.github/workflows/release-win7.yml     python-version: "3.8"    # Win7 最后一个 CPython
desktop/build/build-backend.sh         探测 3.11 / 3.12 / 3.10 / python3
```

也就是**两条发布会用两个不同的解释器**：现代线 3.11，传统 Win7 线 3.8（并把 playwright
放宽到最后一个 cp38 轮子）。单一 `>=3.10` 下限会让守卫**拒绝一条真实存在的发布线**。

修法：窗口取两条线的**并集** `>=3.8,<3.14`，并单列 `MODERN_MIN_PYTHON = (3, 10)` 表示
"现代线的下限"——这两个是不同的问题，不该挤在一个常量里。上界取 3.14 是因为**没有任何
东西验证过 3.14 的包**：清单里有明确的 `python_version >= "3.13"` 处理（`legacy-cgi` +
git 源 `web.py`），说明 3.13 是有意的，3.14 则未被验证。

## 5. 守卫本身：能说"不"，且区分"没查"

退出码分开是刻意的，"没查成"绝不能被读成"查过且没问题"：

| 码 | 含义 |
| --- | --- |
| `0` | 每个被声明的依赖都在包里 |
| `1` | 有一个或多个问题 |
| `2` | **检查本身跑不了**（锁定清单缺失 / 没有技能目录）——与 `0` 不同 |

另外三处刻意的"宁可报不可猜"：

- **读不出的 SKILL.md 记为失败**，不是跳过。声明**未知**不等于**没有**声明。
- **`bins` 只在传了探测函数时才判**。没探测 = 未知，既不伪造通过也不伪造失败
  （二进制属于"机器的事"，不是"包的事"）。
- **`--verify-imports` 是唯一不含映射假设的检查**：映射表说"谁**应该**提供它"，
  只有真实构建 venv 里的一次 import 说"它**确实**在"。所以打包脚本与三条发布线都在
  `pip install` 之后、PyInstaller 之前跑它。

## 6. 接线也是断言（"存在但没人调用"是这个仓库真实发生过的失败）

守卫写得再好，没人调用就等于没有——本 change 里 manifest 与版本缓存都经历过"正确但未被
调用"，仓库里也出现过"清理逻辑自测全绿却从不执行"。所以
`tests/test_skill_dependency_guard.py::PackagingWiringTests` 把接线当**文本断言**：

- `build-backend.sh` 与三条 release 工作流都出现 `check-skill-dependencies.py`，
- 且都出现在 `pyinstaller … cowagent-backend.spec` **之前**，
- 且都带 `--verify-imports`，
- Win7 线必须校验它**真正安装的那份**清单（`--lock /tmp/requirements-win7.txt`），
  而不是仓库里那份被放宽过的原件。
- 守卫文件本身**没有被 gitignore**。`desktop/build/*` 是整目录忽略、逐个源码文件用 `!`
  白名单的（`.gitignore` 里有明确注释），新加文件若忘了加白名单，就会**只在本地存在**：
  本机构建照过，而每条发布线在守卫那一步因为"文件不存在"失败——这跟"存在但没人调用"
  是同一类失败的另一种形态。`test_the_guard_is_not_gitignored_so_it_actually_reaches_ci`
  用 `git check-ignore` 钉住（已实测：去掉白名单行该用例转红）。

## 7. 验证

```
# 用例：56 项（34 + 22）
.venv/bin/python -m pytest tests/test_skill_dependencies_lock.py tests/test_skill_dependency_guard.py -q -p no:randomly
# 56 passed in 0.15s
```

守卫在真实仓库上的行为：

```
$ .venv/bin/python desktop/build/check-skill-dependencies.py --skip-interpreter-check --verify-imports
checking 4 skill(s) against desktop/build/requirements-desktop.txt
build interpreter: Python 3.14 (supported >=3.8,<3.14)
ok: every declared skill dependency is shipped by this bundle
EXIT=0
```

**把缺口放回去**（用临时清单模拟修复前的状态），守卫必须拦下：

```
$ grep -v '^xlsxwriter$' desktop/build/requirements-desktop.txt > /tmp/lock-no-xlsxwriter.txt
$ .venv/bin/python desktop/build/check-skill-dependencies.py \
      --lock /tmp/lock-no-xlsxwriter.txt --skip-interpreter-check
!! 1 skill dependency problem(s):
   - dependency_missing [rfq-quote] rfq-quote needs 'xlsxwriter' (distribution 'xlsxwriter')
     and the desktop bundle does not ship it. Add it to desktop/build/requirements-desktop.txt,
     or stop declaring it in the skill's frontmatter.
EXIT=1
```

同一条缺口也会被仓库级不变量用例抓住：
`test_every_declared_dependency_of_every_shipped_skill_is_shipped` 对**全部**内置技能跑一遍
"声明 ⊆ 交付"，`rfq-quote` 的声明一旦落空就红。

### 变异验证（22 项全中，其中 4 项为 8.5 新增）

脚本：`evidence/scripts/mutate_skill_package.py`；完整输出：`evidence/skill-dependency-mutations.log`。

| 变异 | 内容 | 预期命中的用例 |
| --- | --- | --- |
| M19 | 缺失依赖不再报错（守卫放行未打包的库） | `test_a_declared_but_unshipped_dependency_is_reported_by_name`、`test_a_declared_but_unshipped_library_fails_the_build` |
| M20 | 无法判定的模块静默通过 | `test_an_unmappable_module_is_reported_rather_than_assumed_shipped` |
| M21 | 读不出的技能被静默跳过 | `test_an_unreadable_skill_fails_rather_than_being_skipped` |
| M22 | "无法检查"被当成"检查通过" | `test_a_missing_lock_cannot_check_and_says_so` |

```
22 类实现走样都被对应用例判为失败，且还原后源文件与原文一致。
```

变异后逐文件 `diff` 校验，全部与备份一致（8 个被变异文件 + 守卫）。

## 8. 任务 8.5 的三句话逐条对应

| 要求 | 落点 |
| --- | --- |
| 按现有桌面打包流程锁定并交付 Python | 解释器窗口显式声明为两条发布线的并集（`dependencies.py`），守卫在打包流程内校验；窗口与 `build-backend.sh` 的探测顺序、三条 release 工作流的 `python-version` 对齐 |
| 按现有桌面打包流程锁定并交付代表技能必需库 | `xlsxwriter` 进锁定清单；`rfq-quote` 补声明；`build-backend.sh` + 三条工作流在 PyInstaller 前跑守卫 |
| 声明式额外依赖走受控准备 | 声明面用**既有**的 `requires` / `install` 两个机制（不新造）；准备面仍是既有流程——隔离 build venv 装锁定清单、PyInstaller 打包，不额外 pip、不写入运行目录 |
| 缺失/不兼容提供明确错误 | `dependency_missing` / `dependency_undeclared` / `python_incompatible`；构建期非零退出并给出"加哪个文件"的说明 |

## 9. 未完成部分（不作完成性主张）

- **本条只在开发机完成，未在安装包内验证。** 守卫本身在真实构建 venv 里跑过（本机
  `.venv`，`--verify-imports` 通过），但"打包后的 `.app` / NSIS 安装包内技能脚本真的能
  import 到 `xlsxwriter`"属于 **10.1–10.3**（构建实际安装包、签名后能力核对）。任务 4.9
  已就同一类边界写过"需安装包与真机，本机无法完成"，此处口径一致。
- **`tests/test_desktop_local_worker.py` 未覆盖从打包产物内导入技能库的路径。** 沙箱内技能
  脚本的导入行为仍只在源码运行下验证过（任务 8.8 的 A06/A07/A11–A13 两种模式验收）。
- **没有通用的按需安装实现。** 对"声明了但刻意不打包"的依赖（如 `lark-oapi`，由
  `channel/feishu/lark_install.py` 按需下载一个裁剪包），本次只做到"能判断出它不在锁定
  清单里并明确报错"，**没有**把它与 `install` 声明自动接起来。现状是该类依赖要么进锁定
  清单，要么在打包期失败——这是一个**刻意的**保守选择：静默走网络下载来补齐依赖，比构建
  期失败更糟。
- `MODULE_DISTRIBUTIONS` 是人工维护的表。未知模块会报 `dependency_undeclared`（不会静默
  通过），但也就意味着**新增一个第三方库时需要有人加一行**，否则打包会因为
  "undeclared" 而失败。这是有意的摩擦：宁可让构建停下问一句，也不要放行一个没人确认过的
  依赖。

## 10. 本次回归中**非本次改动引入**的失败

对 72 个含技能/预设关键字的用例文件跑了全量（1446 项，184 秒）：

- 含本次改动：**21 项失败**
- 仅把 `skills/rfq-quote/SKILL.md` 还原到改动前（其余改动保留）：**21 项失败，且逐条相同**

逐条 `diff` 的结论是：20 项为既有失败，**与本次改动无关**；第 21 项是我自己的
`test_rfq_quote_declares_the_library_its_report_writer_needs`——它在 SKILL.md 还原后按设计
转红（这正是"声明"那一半的 RED 状态）。也就是说本次改动**没有引入任何新失败**。

既有失败清单留档：`evidence/skill-dependencies-baseline-failures.txt`。集中在
`test_memory_global_config.py`(7)、`test_knowledge_sources_web.py`(5)、
`test_channel_startup_open.py`(3)、`test_compat_surface_closure.py`(2) 等——共同点仍是
**依赖本机真实部署数据或受同批次其它用例的全局状态影响**（单独跑这 7 个文件时
`test_memory_global_config.py` 与 `test_knowledge_sources_web.py` 全绿，合跑才红），
属仓库规则已记录的"换台机器可能红"类型，按规则**不顺手修**。
