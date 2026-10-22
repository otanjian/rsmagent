# 决策记录：Windows 脚本运行时方案（任务 7.1）

**状态**：已决策。
**对应**：变更 `add-windows-local-project-access` 任务 7.1、design D7/D8。
**决策范围**：*用哪个 Python 运行时执行用户项目里的脚本*。不含隔离实现（7.2–7.4），不含 PowerShell（7.6）。

## 决策

**采用「最小随包 CPython（embeddable）+ 显式声明的依赖子集」**，不复用扩展冻结 worker。

同时确定三条配套规则：

1. **依赖版本复用而不新增第二份清单**：脚本运行时所需的第三方库**取自现有 `desktop/build/requirements-desktop.txt` 中已锁定的版本**，只在其上**选子集**（`requirements-script-runtime.txt`）。已由构建守卫 `check-skill-dependencies.py` 校验过的版本号**不重新挑一遍**——第二份清单就是第二个会漂移的地方。
2. **解释器路径由受信方解析**，模型不得指定 `executable`。`script-launcher` 只接受绝对路径，7.1 的职责是保证那个路径**来自随包目录**，而不是 PATH。
3. **不降级**：运行时或依赖缺失 → 明确拒绝该语言，不回落系统 Python。

## 实测数据（本机，Windows 11 x64）

### 方案 A：复用扩展冻结 worker

| 指标 | 实测 |
|---|---|
| 已随包的后端总量 | **399.5 MB**（`desktop/build/dist/cowagent-backend`） |
| 主可执行 | 30.69 MB |
| 体积构成（最大项） | `googleapiclient` 106.8、`playwright` 92.0、`webhelp` 52.7、`numpy.libs` 20.0、`PIL` 12.8、`grpc` 11.4、`cryptography` 9.4、`Scene` 7.4、`channel` 6.9、`lxml` 6.6 |

**改造面**（按 design 要求计入，不重复验证它能否执行任意脚本）：

- 需要一个**新增的通用脚本入口**。现有 worker 是 `agent/desktop_local/worker.py`，它按 `proto.ALLOWED_TOOLS` 精确实例化**六个项目工具**，不是通用解释器入口。
- 需要一轮**后端资源收敛**：为一个脚本入口而让 `googleapiclient`/`playwright`/`grpc`/各聊天 SDK 全部出现在可执行目录里，等于把这些库放进脚本进程的 reach。
- 冻结进程内**无法动态增删包**（PyInstaller 打包），脚本侧做"按需装库"要么不可能，要么要另一套机制。

### 方案 B：最小随包 CPython

| 指标 | 实测 |
|---|---|
| 官方 embeddable 包（`python-3.11.9-embed-amd64.zip`） | 下载 10.73 MB |
| 解包后 | **20.66 MB** |
| 最大文件 | `python311.dll` 5.53、`libcrypto-3.dll` 4.95、`python311.zip` 4.09、`sqlite3.dll` 1.47、`unicodedata.pyd` 1.09 |
| 能否执行脚本 | **能**（`print` 正常） |
| 标准库 | **齐**（实测 import `json/csv/math/re/datetime/pathlib/collections/itertools/functools/statistics/decimal/fractions/hashlib/base64/textwrap/typing/dataclasses/enum/abc/io/os/sys` 全通过） |
| 第三方库 | **不含**——`openpyxl`/`pandas`/`numpy`/`docx`/`pptx` 实测均 `ModuleNotFoundError` |

**首批技能所需的第三方库**（从现有冻结环境量取，作为规模估算）：

| 库 | 体积 |
|---|---|
| `lxml` | 6.6 MB |
| `numpy` | 5.9 MB |
| `docx`（python-docx） | 0.9 MB |
| `pptx`（python-pptx） | 0.3 MB |
| `openpyxl` / `xlsxwriter` / `et_xmlfile` | 各 <1 MB（未单独成目录） |

**量级结论**：运行时约 21 MB + 首批依赖约 15 MB ≈ **35–50 MB**，对比方案 A 的 **399.5 MB**。

## 择优理由

| 维度 | A 复用冻结 worker | B 最小随包 CPython | 采用 |
|---|---|---|---|
| **隔离资源范围** | 整个后端（含浏览器自动化、云 SDK、聊天 SDK）在可执行目录内 | 只有显式列出的库 | **B** |
| **依赖适配** | 已就绪，但**多到无法逐项审计** | 需自带，但**清单即审计面** | **B** |
| **包体** | 399.5 MB（已有，增量 0） | 约 35–50 MB（新增） | 视分发目标；**B** 的增量可接受 |
| **维护成本** | 需新增通用入口 + 后端资源收敛 | 需维护运行时与子集清单 | 相当 |

**决定性理由是第一条**：D8 把 LPAC 定为访问边界，而访问边界的价值取决于**可执行目录里有什么**。为一个脚本入口把 `googleapiclient`、`playwright`、`grpc` 和各聊天 SDK 放进脚本进程的 reach，等于**用一个 400 MB 的库堆去换 15 MB 的显式清单**。方案 B 让"脚本能 import 什么"成为一个**可读、可审、可拒绝**的清单——这正是"不被信任的代码"所需要的性质。

方案 A 的"依赖已就绪"是真实优势，但 design 明确：**复用是偏好，不要求穷尽不可行证明**。这条偏好在此被隔离面的可审计性压过。

## 落地物与一个**真实缺口**

| 文件 | 作用 |
|---|---|
| `desktop/build/requirements-script-runtime.txt` | 脚本运行时的依赖清单（9 个包） |
| `desktop/build/check-script-runtime-deps.py` | 构建守卫：子集规则、范围、首批技能可满足、豁免诚实 |

**守卫第一次运行就查出一个真实缺口**：`pandas` **既不在** `requirements-desktop.txt`、**也不在**冻结后端里（实测 `_internal/pandas` 不存在），而 `fin-report-analysis` 与 `finance-ledger-generator` **都 import 它**。

也就是说：**这两个技能在现有后端路径下同样会 `ModuleNotFoundError`**——这不是我引入的问题，是一个此前没人比对的缺口（与 `xlsxwriter` 那次同型）。已补进 desktop 清单，并因此让脚本运行时的子集规则重新自洽。

**守卫的四条检查**（每条堵一种复发方式）：

1. **子集**：脚本清单里的包必须在 desktop 清单里，或声明为豁免
2. **范围**：只允许首批（Excel/文档）需要的库——安全属性，不是整洁性
3. **首批技能可满足**：**读技能脚本的 import** 来判定，而不是信任手写表——这正是抓住 `xlsxwriter` 与 `pandas` 的那条
4. **豁免诚实**：声明为「自动包含」的包一旦出现在 desktop 清单，必须撤掉豁免

**豁免分两类**，因为理由不同：

| 类别 | 含义 |
|---|---|
| `AUTO_INCLUDED` | PyInstaller **自动**带传递依赖，所以 `lxml`/`et_xmlfile` 在 399.5 MB 包里但不在其清单里；embeddable **不自动带**，故必须显式列出 |
| `GAP_FILLED` | 曾被**两个清单都漏掉**的真实缺口（`pandas`）。补齐后此表已清空 |

**首批实际的第三方 import**：`numpy`、`openpyxl`、`pandas`、`xlsxwriter`。**被排除的 10 个技能**各有原因（PDF 解析、charting、网络客户端、或技能内部模块），由规则推导而非手写名单。

## 后果与已知缺口（不构成阻塞，记录以免被当成已完成）

- **技能依赖要显式列全**。现有 `check-skill-dependencies.py` 守卫的是**后端**清单；脚本运行时要新增一份子集，并让它**同样进构建守卫**，否则就会重演"声明了但没打包、也没人报错"（那是 `xlsxwriter` 曾经的真实缺口）。
- **embeddable 无 `pip`**：依赖必须**预置**在包内，不能运行时安装。这与"不联网"一致，但也意味着新增库要走发版。
- **`libcrypto-3.dll`（4.95 MB）** 与 `sqlite3.dll`（1.47 MB）：若首批技能不用，可从最小包中剔除，进一步缩小体积与攻击面。**本轮未剔除**，列为待办。
- **本决策只覆盖 Python**。PowerShell 7（7.6）需另行决策。
- **未验证**：本决策只做了比较与测量，**没有**在 Win10/Win11 上完成 7.1 要求的 LPAC/Job 最小原型。按 D8，原型与决策记录**齐备后**才可接业务。

## 下一步（顺序固定）

1. 建 `desktop/build/requirements-script-runtime.txt`（现有清单的子集，**版本沿用**）。
2. 让构建守卫覆盖该子集。
3. 7.2 的 ACL 生命周期原型（**接业务的硬前置**）。
4. 把 `Access::AppContainer` 在本机已可创建这一事实落成实现（见 `script-launcher/docs/isolation-status.md` §2.1）。
5. Win10 22H2 / Win11 上跑 LPAC/Job 最小原型。
