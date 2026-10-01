# P3 验收：本机产出、卡片与系统动作（任务 9.8，A21–A25 / A34 / A35 + A06/A07）

第 9 组把「本机产出怎么变成卡片、卡片上的动作怎么落到这台机器上」逐层做完，每一层都
有自己的用例。9.8 要做的**不是**把这些用例的名字抄一遍，而是按验收行**再走一遍**，
并且补上唯一一直缺的那一环：**在真浏览器里点那张卡片**。

补这一环的时候发现了本 change 最严重的一个缺陷：**四项系统动作在真机上全部点不动**
（§三）。所以本文的价值不在"9 条都绿"，而在"这条一直没人走过的路，第一次走就断了"。

## 一、验收行 → 证据

| 行 | 主张 | 运行的东西 | 结果 |
|---|---|---|---|
| A06 | 本地与远程各运行固定 Excel 技能；汇总单元格 350、报告真实位于所选项目、输入摘要不变 | 本地：`tests/test_desktop_skill_acceptance.py`（真 `sandbox-exec` 启动器 + 未改动的代表技能）；远程：`tests/test_desktop_remote_skill_acceptance.py` | 11 passed / 10 passed |
| A07 | 本地与远程各运行带模板和辅助资源的文档技能；模板齐全、字段正确、技能/记忆目录未搬进项目、项目外缓存未被写 | 同上（同一文件里按技能分组的用例） | 同上 |
| A21 | 文件树/搜索/预览/编辑/@ 之后执行技能，刷新并切项目：来源与当前项目一致、@ 不重复上传、刷新先重验、失效订阅被清理 | `tests/test_desktop_project_source.cjs`(37)、`tests/test_desktop_project_watch.cjs`(28)、`tests/test_desktop_project_refresh.cjs`(22) | 含在 `node --test tests/test_desktop_*.cjs` 741 通过内 |
| A22 | 从项目 A 生成后切到 B、同路径替换/删除，再打开历史卡片：定位生成时项目、变化/缺失明确、不对服务器同名路径 stat 或下载 | `tests/test_desktop_artifact_source.py::HistoryTests`、`tests/test_desktop_local_root.py`，以及本文 §三 的 `gone` / `other-project` 两条**浏览器内**检查 | 含在 1234 通过内 + 探针 2 项通过 |
| A23 | 设备离线、设备 B 看设备 A 历史、客户端重启恢复候选：保留元数据与原因、不自动上传/复制/恢复授权、重新获权后才可访问 | `tests/test_desktop_project_refresh.cjs`（三态恢复 `live`/`stale`/`none`，只恢复本会话）、`tests/test_desktop_artifact_source.py`、`tests/test_desktop_local_context.py` | 含在 741 / 1234 通过内 |
| A24 | 预览含脚本的 HTML 去调 preload/IPC、读认证或其他目录：无原生桥、无秘密、无法越界；受保护 URL 会失效 | `desktop/e2e/preview-isolation.probe.cjs`（**真 Electron + 真 Chromium + 真 `protocol.handle`**）、`tests/test_desktop_isolated_preview.cjs`(27) | 探针 **16 项 0 失败 PASS**；用例含在 741 内 |
| A25 | 系统打开 / 打开所在文件夹 / 复制路径 / 另存为 / 明确上传分享：原文件可用、变动或无应用真实提示、上传产生独立获权副本、未操作时不上传 | `desktop/e2e/cards-and-system-open.probe.cjs`（本文 §三）、`tests/test_desktop_project_native_actions.cjs`(40)、`tests/test_desktop_web_pages.py::test_w25_*`(2)、`tests/test_desktop_materialize.cjs`(17) | 探针 **16 项 0 失败 PASS**；用例含在 741 / 1234 内 |
| A34 | 工具输出含摘要/错误/用户请求的 `pwd`：文案准确说明结果送往服务器或模型、无整目录默认同步、路径文本不被当成授权依据 | 三份词典的 `ws_local_data_flow` 语句（`tests/test_desktop_project_native_actions.cjs::the three dictionaries carry the data-flow statement…`，并断言那句不能写的谎）、`tests/test_desktop_local_root.py`（`entry_for_path` 只认在册注册）、`evidence/scripts/mutate_resource_landing.py`（8.x：`resource_landing` 的同步边界） | 含在 741 / 1234 通过内 |
| A35 | 模型只声称已生成、或 worker 上报不存在/越界产出：**不生成可用卡片**，只有真实文件验证过的引用才可发布 | `tests/test_desktop_artifact_source.py`（30 项，含"只声称没写"与"写到了另一台机器"）、`evidence/scripts/mutate_desktop_artifact_source.py` | 含在 1234 通过内 |

**运行记录（本轮实测）**

```
node --test tests/test_desktop_*.cjs
  tests 741 | pass 741 | fail 0            (16.6s)

.venv/bin/python -m pytest tests/test_desktop_*.py tests/test_mutation_evidence_scripts.py -q -p no:randomly
  1234 passed, 822 subtests passed         (1:56)

desktop/node_modules/.bin/electron desktop/e2e/preview-isolation.probe.cjs
  checks 16 | failed 0 | PASS

desktop/node_modules/.bin/electron desktop/e2e/cards-and-system-open.probe.cjs
  checks 16 | failed 0 | PASS              (日志 evidence/cards-and-system-open-probe.log)

.venv/bin/python …/evidence/scripts/mutate_project_native_actions.py
  23 类走样 23/23 命中，退出码 0            (日志 evidence/project-native-actions-mutations.log)
```

## 二、「本地和远程」的卡片/系统打开证据为什么是同一份

远程模式下跑的**是同一个页面**：`channel/web/static/js/workspace.js` 与
`fork/project-source.js` 由远程容器里的控制台加载，`CowDesktopHost` 之下的 preload 换成
容器自己的 `remote-preload.ts`，四个方法名不变。因此"远程"这一半要证的是**两端对同一批
名字的理解一致**，而不是第二套页面：

- **容器那一端**：`tests/test_desktop_project_native_actions.cjs` 的前半段驱动**真实注册的
  IPC handler**（`desktop:bridge:call`），断言四项动作随 `localFiles` 门控出现/消失、
  参数形状、以及拒绝码原样返回；
- **两端之间**：`tests/test_desktop_web_pages.py::test_w25_the_two_ends_agree_on_the_four_action_names`
  （本轮新增）把页面里的动作表与主进程的 `NATIVE_METHOD_BY_ACTION`、与桥真正发布的
  `PHASE3_METHODS` **逐对比较**——两边各自都对、中间那层没人比过，正是 §三 的缺陷形状；
- **页面那一端**：本文 §三 的探针。

## 三、端到端：真 Chromium 里的卡片与系统打开（本轮新增）

`desktop/e2e/cards-and-system-open.probe.cjs`（新）按页面加载的顺序载入**出货的那几个
脚本**（`i18n/core.js`、`fork/desktop-host.js`、`fork/project-source.js`、`workspace.js`），
在真 Chromium 里渲染真卡片、`button.click()` 真按钮，唯一被替换的是最下面那层 preload
（它记录收到什么，答案可脚本化）。16 项检查：

1. 四个页面模块在同一个文档里加载成功；
2. 本机卡片画出四个系统动作、**没有**服务器下载按钮；
3. 服务器卡片保留下载、拿不到系统动作；
4. `local-open` / `local-reveal` / `local-copy-path` 各一次真实点击，**到达 preload 的
   方法名与参数准确**（`projectOpenFile` / `projectRevealFile` / `projectCopyPath`，
   `{workspace_id, path}`），并且 toast 是**词典里的真句子**（`已用系统应用打开` /
   `已在该文件所在文件夹中显示` / `已复制完整本机路径`）；
5. `local-save-as` 上"文件被改过"的整条路：第一次 `expected_mtime=1700000000` → `changed`
   → 用户确认 → 第二次带 `accept_current: true` → `已另存副本`；
6. `resolution:"missing"` 的重放卡片**根本不去问宿主**，直接说 `该文件在本机已不存在`；
7. 来自另一个项目的重放卡片带自己的 `workspace_id`，**同样不发出任何宿主调用**，说
   `该产出属于另一个项目，请先打开那个项目`；
8. 宿主自己的拒绝（`no_application`）**原话**传到用户眼前：
   `系统没有可打开该文件的应用，请先安装或改用其他方式打开 (There is no application set to open the document)`；
9. 预览走同一条缝（`projectPreviewFile`，回 `cow-preview://…`）；
10. 整个过程里被调用的宿主方法**只有这四个加预览**。

（`_wsToast`、`window.confirm`、`escapeHtml`、`t` 这几个 `console.js` 的全局在这里按
`console.js` 的做法重建；`t` 读的是**出货的词典**，所以上面每一句断言比的都是用户真正看到的
那句话。）

### 3.1 第一次走这条路，它是断的

在写探针之前，这条链路的每一层都是**各自单独测**的：`makeBridge` 那批驱动真的 IPC handler
但另一侧是桩，`loadAdapter` 那批驱动真的适配器但另一侧也是桩。于是：

- 页面把**面板动作名**（`open` / `reveal` / `copyPath` / `saveAs`，即卡片按钮上那四个
  `data-action`）交给 `CowDesktopHost.projectAction()`；
- 而 `CowDesktopHost.projectAction()` 当时拿**预载方法名**（`projectOpenFile`…）去比这个名字。

结果：`canAct()` 为真、四个按钮照常画出来、适配器也确实发出去了，但**每一次点击**都在碰到
预载之前被拒（`invalid_request: unknown project action`），页面把它归到兜底文案，用户看到
`本机文件操作失败 (unknown project action)`——一句既没有原因也没有出路的英文片段。

**证据（把探针跑在修之前的实现上）**：`evidence/cards-and-system-open-probe-before-fix.log`，
`checks 16 | failed 8 | FAIL`，其中两条：

```
{"probe":"a click on local-open reaches the shell as projectOpenFile","ok":false,"detail":"[]"}
{"probe":"local-open tells the user what happened, in their language","ok":false,
 "detail":"{…\"toasts\":[\"本机文件操作失败 (unknown project action)\"]}"}
```

### 3.2 修法与它现在的守卫

`PROJECT_ACTIONS` 一张表（动作名 → 预载方法），`PROJECT_ACTION_METHODS` 由它**派生**：
`canUseProjectActions()` 认的能力清单和 `projectAction()` 认的动作名从此不可能各说各话；
查表用 `hasOwnProperty`，所以 `constructor` / `toString` 不是动作名。

- `tests/test_desktop_project_native_actions.cjs` 新增 4 项（40 项全绿）：把两个**真模块**
  关进同一个沙箱、下面垫一个按容器行为作答的预载桩，逐项断言四个动作以**预载方法名**到达、
  预览同一条缝的接线、"宿主没有的动作按名字拒绝且预载根本不被调用"、以及原型键不是动作名；
- `tests/test_desktop_web_pages.py::test_w25_the_two_ends_agree_on_the_four_action_names`
  新增：页面动作表 = 主进程动作表 = 桥发布的方法集合；
- 变异脚本新增 **M21**（退回修之前的写法）、**M22**（表当字典用）、**M23**（页面把两个动作
  对到同一个方法上），三条都被对上，见 §一 的 23/23 记录。

## 四、P3 门槛检查（design.md D10）

| 门槛原文 | 判定 | 依据 |
|---|---|---|
| 「未改动内容的代表技能完整处理 Excel/文档」 | 本地 **满足** | `tests/test_desktop_skill_acceptance.py` 用**未改动**的代表技能，经平台启动器真执行：汇总单元格 = 350、报告确实落在**所选项目**里、输入摘要不变；文档技能模板资源齐全、输出字段正确、技能/记忆目录没有被搬进项目 |
| 同上（远程） | 机制 **满足**，**真机执行未完成（属 10.x）** | `tests/test_desktop_remote_skill_acceptance.py` 证明通道侧完整（摘要集合随命令持久化并被双向要求、包在写盘前按摘要校验、worker 只拿到只读根）；该文件自己写明：剩下的是**安装件**（10.1–10.3）在真设备上端到端跑一遍 |
| 「结果可打开」 | **满足** | A25 的探针在真 Chromium 里点卡片→到达宿主；宿主拒绝时给真实原因；`preview-isolation.probe.cjs` 16 项证明预览隔离。**尚未做的是安装件内**（10.1–10.3） |
| 不以 mock 或开关关闭当作完成 | 遵守 | 本轮结论对远程真机、Windows、安装件一律留空，不勾选 10.x |

**结论**：P3 门槛在**本 change 能证明的范围内成立**（本地代表技能真执行 + 产出可打开 +
卡片动作端到端）；远程的**安装件内真机执行**不是本组能补的，随 10.1–10.3 一起进 P4 验收。

## 五、边界与未做

- **安装件内真机**（`10.1–10.3`）：打包后的桌面端在真设备上跑 A06/A07 与卡片动作，本轮
  未做；本文所有端到端结论都明确限定在"开发件 + 真 Electron/Chromium"。
- **Windows 未验证**：平台启动、路径形状与 `shell`/`clipboard` 行为均只在 macOS 上测过。
- **没有真实模型驱动**：A06/A07 由测试构造运行并驱动真脚本，不经过在线模型；"模型只声称
  已生成文件"（A35）由 `tests/test_desktop_artifact_source.py` 用构造的台账覆盖。
- **`open`/`reveal` 的真实平台副作用**：单测用假效果（`shell.openPath` 等被替换），探针也
  只到 preload 为止——真机上真的弹出应用需要安装件（10.2 的探针）。
