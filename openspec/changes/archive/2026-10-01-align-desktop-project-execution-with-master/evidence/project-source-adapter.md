# 本机 source adapter：文件树、搜索、分页预览、编辑与 `@`（任务 9.2）

对应主规范 `desktop-project-execution` 的「本机项目来源」与
`desktop-project-artifacts` 的「文件面板显示本轮项目内容」。本文记录**实际改了什么、
怎么验证的、哪里没做**。

要求原文：「增加本机 source adapter，接通文件树、搜索、分页预览、编辑和 `@`；后端来源
继续走原 API，不对本机引用服务器 stat。」

## 一、交付内容

面板过去一律问服务器 `/api/workspace/*`。项目在本机时，那些请求要么 404，要么更糟——
**拿服务器上同名文件回答**。做法是**三层一致的来源**，而不是在面板里加一个分支：

| 层 | 实现位置 | 语义 |
|---|---|---|
| 本机浏览器（主进程） | `desktop/src/main/project-browser/browser.ts`（新） | `describe` / `tree` / `search` / `resolve` / `read` / `write` 全部直接落在 `DeviceCommandRunner` 上：读走 v1 `fs-guard` 的 `list`/`stat`/`search`/`read_text`，写走 v2 `write` 执行帧。**没有一条路径是绝对路径**：helper 只回名字，相对路径在这里拼一次（`joinRelative`），条目自带的 `path` 恒为项目内相对形式 |
| 桌面桥与 preload | `remote/host-bridge.ts`、`remote-host-ipc.ts`、`remote-container-ipc.ts`、`remote-preload.ts` | 六个方法组成 `PHASE3_METHODS`，随 `local-files` 一起开（`local-files-bridge.ts::bridgeCapabilitiesPayload`），关着时只在 `allMethods` 里出现；每个方法**逐项校验**：`workspace_id` 必填、路径必须是项目内相对形状（绝对路径/盘符/NUL/`..`/超长一律 `invalid_path`，**到不了文件 API**）、写正文按**字节**限 `PROJECT_WRITE_MAX_BYTES`（1 MiB）。`projects` 端口可选：没有它时 `feature_unavailable`，而不是回退去读服务器 |
| 面板适配器（页面） | `channel/web/static/js/fork/project-source.js`（新）+ `workspace.js`/`console.js`/`chat.html` | 在 `wsApi()` 与 `savePreviewEdit()` 两个既有出口拦截 `/api/workspace/{tree,resolve,read,search}` 与 `/api/workspace/write`，把本机回答**映射成面板原本就在读的形状**（`entries`/`file`/`content`/`results`），条目标 `source: 'desktop'`、`local: true`，并**剥掉** `abs_path`/`raw_url`/`preview_url` 与列表的 `root`——留着会让面板转头去请求服务器上不存在的路径。非本机请求（`meta`、`user-dir`、`projects`、`/api/file`…）原样返回 `null`，走原 API |

几个语义上的取舍，都是**故障时看得出来**的那一侧：

* **授权每次调用重验**：`binding()` 活在每次调用里，从不缓存。用户关了项目、撤了 grant、
  重选了目录，下一次调用就拒绝，而不是继续服务一个缓存下来的目录。
* **编辑是一次真实的 v2 `write` 帧**，不是面板私有的文件写：`run_id`/`tool_call_id`
  一次一换（不借用模型轮次），`binding_id`/`grant_version`/`selection_generation`/
  `connection_epoch` 取自**当前**授权，`params_digest` 用契约自己的
  `paramsDigest()`（`canonicalEnvelope` 的 sha256），因此它过的是**服务器同一套**
  `validateExecuteFrame`；只读授权/离线/无执行通道各自有名字（`source_read_only`/
  `device_offline`/`feature_unavailable`），不是一句「保存失败」。
* **编辑的基线检查在适配器**：`write` 用 `expected_mtime` 与当下的 `projectResolve`
  比一次，变了就回 `{status:'error', code:'conflict'}`，让面板弹既有的覆盖确认；
  `expected_mtime: null` 是用户明确的「照写」。
* **拒绝不回退**：只有 `feature_unavailable`（这个构建/部署根本没有本机面）才回退到
  原 API；其余代码（撤权、只读、离线、读不了）一律**上报**——回退会在同名文件上
  静默换成另一份内容。
* **面板要的数不是面板给的数**：`?limit`/`?offset`/`?bytes` 是字符串，桥接层要求非负
  整数，所以适配器做**严格**解析：读不出来就是 `invalid_request` 拒绝，**绝不静默丢掉**
  ——丢掉会让一次「要 12 条」变成「给你 200 条」。
* **`@` 引用走同一条路**：`workspace.js` 的 mention 搜索（`/api/workspace/search?q=…&limit=…`）
  本就是 `wsApi()` 出口，因此 `@` 在本机项目里看到的就是本机文件，**不经过服务器、
  不重复上传**。
* **服务器侧的文档查看器故意不拦**：`console.js::docReadFile/docWriteFile` 用裸 `fetch`
  读**记忆文件与技能定义**，它们锚在 agent 的服务器状态根上；把它们也接到本机来源会
  把面板指向用户的项目目录——那是另一个目录，不是同一份资产。
* 四种拒绝在三种语言里都有可读文案（`ws_local_read_only`/`ws_local_offline`/
  `ws_local_stale`/`ws_local_unavailable`，zh-CN/zh-TW/en-US）。

## 二、验证

命令固定为 `node --test <文件>`（主进程侧）与
`.venv/bin/python -m pytest <子集> -q -p no:randomly`（Python 侧）。原始输出见
`evidence/project-source-tests.log`。

| 套件 | 结果 | 覆盖 |
|---|---|---|
| `tests/test_desktop_project_source.cjs`（新，37 项） | **37 项通过** | 三层各一组：**browser** 在真实 helper + 真实目录树上列目录/搜索/分页读/stat/写，含只读授权、离线设备、绝对路径与穿越路径、符号链接出项目、超一页的大文件、冲突（mtime 变了）、显式覆盖；编辑帧过 `validateExecuteFrame` 且 `paramsDigest(frame) === frame.params_digest`，并断言帧里**不出现**目录字符串；**bridge** 校验六个方法的存在与门控（`local-files` 关着不宣称）、参数校验（`/etc/passwd`、`../secret`、`C:\x`、`a/../../b` 都 `invalid_path` 且**端口一次都没被调用**）、写正文超限、无端口 `feature_unavailable`、拒绝码原样透传；**adapter** 树/解析/读/搜/写映射、来源标记、无 `abs_path`/`raw_url`/`preview_url`/`root`、冲突、只读拒绝、分页参数、非本机请求原样放过、无绑定/无端口回退 |
| `tests/test_desktop_remote_host.cjs`、`test_desktop_execution_contract.cjs`、`test_desktop_execution_journal.cjs`、`test_desktop_project_execution_grant.cjs`、`test_desktop_process_handles.cjs` | **共 114 项通过** | 与 9.2 的同批：新增的六个方法没有改变 preload 声明、桥接调用形状、契约与去重日志、授权表、进程句柄的既有行为 |
| `tests/test_console_workspace_frontend.cjs` | **22 项通过** | 页面侧：`chat.html` 的脚本顺序与 i18n 文案 |
| `tests/test_desktop_source_resolver.py`、`test_desktop_web_pages.py`、`test_desktop_local_root.py`、`test_desktop_run_context.py`、`test_desktop_run_scope_cancel.py`、`test_web_console_assets.py`、`test_workspace_edit.py`、`test_mutation_evidence_scripts.py` | **226 项通过，12 项跳过，292 个子测试通过** | 3.6 的来源解析/面板来源、9.1 的工件、运行上下文与撤权取消，以及静态资源守卫、工作区编辑与变异脚本结构守卫 |

### 变异检查（证明用例真的能失败，而不是碰巧通过）

脚本 `evidence/scripts/mutate_project_source.py`（可重跑，日志
`evidence/project-source-mutations.log`）逐条把实现改成一条「看起来更省事」的写法，
跑同一批用例，出现预期失败后立刻还原，并在首尾各跑一次未变异基线：

**10 类走样 10/10 命中**：自造编辑帧 digest；执行帧报了失败仍当保存成功；只读授权/离线
设备也放行写盘；桥接层不再管路径形状；分页参数读不出来就静默丢掉；本机拒绝时回退服务器
API；截断的分页读也标成可编辑；本机列表带上服务器 URL；本机列表带上目录；本地文件关着
也宣称有 project 面。还原后用例重新全绿（`ℹ fail 0`）。

## 三、这一轮自己抓到并修掉的缺陷

变异与用例各抓到一半，都是「功能看着在、其实不在」的那类：

1. **编辑帧的 `params_digest` 是自造的**（`sha256(path \0 content)`）。收件方按契约的
   `canonicalEnvelope` 重算，两者**不可能相等**：帧会在 `prepare`/去重日志处以 digest
   不一致被拒，或者同一次编辑在两端各有一个身份。改为 `paramsDigest(frame)`，由
   `an edit travels as a v2 write frame the contract accepts` 钉住；变异 M1 可复现。
2. **分页参数没转数字**：`offset`/`bytes` 原样以字符串进桥接层，而 `checkCount` 要求
   `number`——`?offset=100` 会被判 `invalid_request`，也就是「分页预览」这条要求在真实
   参数下走不通（面板目前一次性读全文，所以没人碰到）。改为严格解析，并补
   `a page range reaches the bridge as numbers, and a broken one is refused`。
3. **适配器把 `undefined` 当参数值下发**（`limit: undefined`）：语义上被桥接层容忍，
   但让一次调用的形状变得模棱两可（「没要」和「要了个读不出来的值」看起来一样）。
   现在缺席就不发这个键。
4. **截断读的用例是空转的**：它用 `big.txt`，而测试桩的 kind 表把 `.txt` 判成不可编辑，
   于是「截断不可编辑」在**删掉截断判断之后仍然成立**——变异 M7 之前 0 失败。改成
   `.md` 并补一条「完整读仍然可编辑」，让断言只能由截断守卫满足。
5. **`needsBuild()` 漏了 `local-files-bridge.ts`**：能力清单与整个授权/选目录流程都在这个
   文件里，而它不在重建名单里——改它只跑旧构建。变异 M10（本地文件关着也宣称有 project
   面）因此一开始 0 失败，补进名单后才被抓住。

## 四、未完成（交给后续任务，不在这里假装完成）

* **本机目录变更的有界刷新/订阅与失效作用域清理**（9.3）。A21 的「刷新页面并切换项目 /
  文件变动能刷新」目前只做到「刷新页面即重新走一次来源与授权」：`applies()`/`landing()`
  每次重跑，`wsSourceChanged()` 在选目录/清上下文时揭示面板——**没有**轮询与外⾯变更
  检测，也没有订阅清理。
* **系统打开 / 打开所在文件夹 / 复制路径 / 另存为**（9.4）：本任务只做到面板内预览与编辑。
* **无原生桥的隔离预览与限制性 CSP**（9.5）。
* **A21–A25/A34/A35 的完整验收**（9.8）：本任务只覆盖 A21/A22 中「来源一致、`@` 不重复
  上传、不对本机引用服务器 stat」的部分；完整验收（含真 Chromium 里点卡片的探针）见
  `evidence/p3-artifacts-and-actions.md`。
* 安装件内真机验收属 10.1–10.3；Windows 未验证。
