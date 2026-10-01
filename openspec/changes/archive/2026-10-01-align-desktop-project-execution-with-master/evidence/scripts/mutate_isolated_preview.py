#!/usr/bin/env python3
"""变异检查：隔离预览（任务 9.5，验收 A25）的断言真的会因为实现被改坏而失败。

9.5 给「本机文件没有 URL」这件事一个新答案：一个**为这一个文件**签发、**在钟上
到期**、**页面读不出字节**的受保护地址。这条链上有四层，每一层都有一个「看起来
更省事」的写法会悄悄把隔离拿掉：

* **决策层**（`desktop/src/main/project-browser/preview.ts` 的 `planLocalPreview`
  / `previewKindOf`）：拿目录当文档、拿超限的文件当可预览、拿不认识的扩展名当图
  片、按小写化之前的名字判类型、把 SVG 当纯图片（它是能带脚本的标记）、读取时截断
  而不是拒绝、不把上限交给读取器。
* **票据层**（同文件的 `createPreviewTickets` / `previewResourceFor` /
  `answerPreviewRequest`）：令牌能从文件名推出来、过期不生效、没有上限、一个票
  据能换成另一个文件名、别的主机也认、未知票据不再是 404。
* **策略层**（同文件的 `previewContentSecurityPolicy` / `previewHeaders`）：把
  `allow-same-origin` 加回去（沙箱立刻失效）、给图片和录音也放脚本、放开
  `connect-src`、允许缓存（过期的副本比票据活得久）、把来源漏出去。
* **桥与容器层**（`remote/host-bridge.ts`、`remote/remote-host-ipc.ts`、
  `remote/remote-container-ipc.ts`）：不查路径形状、不提 workspace_id、本机文件
  关着也放行、把 `stream/secure/standard` 之外的特权加上（页面就能 `fetch` 本地
  字节）、装到别的分区上、卸载时不收票据。
* **页面层**（`fork/project-source.js`、`workspace.js`、`i18n/core.js`）：不问宿主
  就报可用、拒绝压成异常、本机 HTML 指向服务器地址、本机图片指向服务器地址、四
  种失败共用一个说法、把所有拒绝都当成"这台机器不支持"、一种语言漏掉一句话。

每一处跑同一批用例（`tests/test_desktop_isolated_preview.cjs`），要求出现预期失败
后立刻还原源文件；任何一项「改坏了却全绿」都会让脚本以非零码退出。

注意：node 套件用源文件 mtime 决定是否重新 `tsc`（`needsBuild`），改过 `.ts` 就会
重编，所以它读到的一定是变异后的字节 —— 不需要手动清构建缓存。但这也意味着**一条
编不过的变异等于什么都没测**：脚本会检查输出里的编译错误并把那种变异判为失败。

用法：`.venv/bin/python openspec/changes/.../evidence/scripts/mutate_isolated_preview.py`
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))

PREVIEW_TS = os.path.join(REPO, "desktop", "src", "main", "project-browser", "preview.ts")
HOST_BRIDGE_TS = os.path.join(REPO, "desktop", "src", "main", "remote", "host-bridge.ts")
HOST_IPC_TS = os.path.join(REPO, "desktop", "src", "main", "remote", "remote-host-ipc.ts")
CONTAINER_TS = os.path.join(REPO, "desktop", "src", "main", "remote", "remote-container-ipc.ts")
LOCAL_FILES_TS = os.path.join(REPO, "desktop", "src", "main", "remote", "local-files-bridge.ts")
PROJECT_SOURCE_JS = os.path.join(REPO, "channel", "web", "static", "js", "fork", "project-source.js")
WORKSPACE_JS = os.path.join(REPO, "channel", "web", "static", "js", "workspace.js")
CORE_I18N_JS = os.path.join(REPO, "channel", "web", "static", "js", "i18n", "core.js")

SUITE = "tests/test_desktop_isolated_preview.cjs"

#: The test names a mutation is expected to break, quoted from the suite so a
#: rename in the suite shows up here as a failure rather than as silence.
T_DIRECTORY = "a directory is not a document, and neither is a link out of the project"
T_TOO_LARGE = "a file too large for the isolated surface is refused, not truncated"
T_KIND = "the kind follows the file, not the page: svg is markup, xlsx is not"
T_UNSUPPORTED = "a kind with no isolated preview is refused by name, not rendered blank"
T_READ = "a preview reads at most its bound, and refuses a byte more"
T_ONE_FILE = "a ticket names one file, and the URL carries no path"
T_GUESSABLE = "the token is not derivable from the name, and two previews of one file differ"
T_EXPIRES = "a ticket stops answering when it expires, and can be revoked sooner"
T_BOUNDED = "the store is bounded, and the oldest preview is the one that goes"
T_HEADERS = "a live ticket answers with exactly its own file, under the kind's policy"
T_NO_SIBLING = "one ticket cannot be spent on another name, another scheme or another host"
T_404 = "an unknown, expired or revoked ticket is a 404 with none of the bytes"
T_POLICY = "every preview policy is restrictive: no same-origin, no network, no scripts for data"
T_GATE = "the preview is published with local files, and not with them closed"
T_MALFORMED = "a malformed preview never reaches the port, and a refusal keeps its code"
T_UNAVAILABLE = "a host that cannot preview says so by name, never with a silent success"
T_PRIVILEGES = "the scheme privileges are exactly the three the probe measured"
T_INSTALLED = "the scheme is registered before ready and installed on the container session"
T_PAGE_EMBEDS = "a local preview asks the host and embeds the answer, and never a server URL"
T_PAGE_REFUSALS = "a refusal the page can act on is shown with the action that works"
T_PAGE_SANDBOX = "the shell never frames a local file without a sandbox, and never with same-origin"

MUTATIONS = [
    # -- 决策层：哪些文件有资格被预览 ------------------------------------
    {
        "name": "M1 目录也当文档预览（把一次列出目录当成一次渲染）",
        "file": PREVIEW_TS,
        # 目录不是「一」份文档：`planNativeAction` 为「打开」放行目录是对的，
        # 预览要自己再问一次。
        "old": "  if (planned.isDirectory) {\n",
        "new": "  if (planned.isDirectory && false) {\n",
        "expect": [T_DIRECTORY],
    },
    {
        "name": "M2 超过上限也照样预览（把 2 GiB 拉进主进程）",
        "file": PREVIEW_TS,
        "old": "  if (planned.size > max) {\n",
        "new": "  if (planned.size > max && false) {\n",
        "expect": [T_TOO_LARGE],
    },
    {
        "name": "M3 不认识的扩展名当图片（给一个没有预览的格式一个空框）",
        "file": PREVIEW_TS,
        # 不能写成「去掉 `if (!kind)` 判断」：那样 `kind` 保持 `PreviewKind | ''`
        # 而返回类型要求 `PreviewKind`，变异后编不过 —— 那是编译失败，不是
        # 用例发现走样。让 `previewKindOf` 自己兜一个值才是能编译的走样。
        "old": "  const entry = PREVIEW_TYPES[extensionOf(name)]\n  return entry ? entry.kind : ''\n",
        "new": "  const entry = PREVIEW_TYPES[extensionOf(name)]\n  return entry ? entry.kind : 'image'\n",
        "expect": [T_KIND, T_UNSUPPORTED],
    },
    {
        "name": "M4 扩展名不小写（大写命名的文件全部没有预览）",
        "file": PREVIEW_TS,
        "old": "  return dot > 0 ? clean.slice(dot + 1).toLowerCase() : ''\n",
        "new": "  return dot > 0 ? clean.slice(dot + 1) : ''\n",
        "expect": [T_KIND],
    },
    {
        "name": "M5 SVG 当纯图片（能带脚本的标记被当成数据）",
        "file": PREVIEW_TS,
        "old": "  svg: { kind: 'html', contentType: 'image/svg+xml' },\n",
        "new": "  svg: { kind: 'image', contentType: 'image/svg+xml' },\n",
        "expect": [T_KIND],
    },
    {
        "name": "M6 读取时截断而不是拒绝（半份报告被当成整份渲染）",
        "file": PREVIEW_TS,
        # 多读的那一个字节**就是**调用方知道「没装下」的方式。
        "old": (
            "    const buffer = Buffer.allocUnsafe(limit + 1)\n"
            "    const { bytesRead } = await handle.read(buffer, 0, limit + 1, 0)\n"
        ),
        "new": (
            "    const buffer = Buffer.allocUnsafe(limit)\n"
            "    const { bytesRead } = await handle.read(buffer, 0, limit, 0)\n"
        ),
        "expect": [T_READ],
    },
    {
        "name": "M7 读回来超界不拦（记录里的大小与实际不符时放行）",
        "file": PREVIEW_TS,
        "old": "  if (content.byteLength > max) {\n",
        "new": "  if (content.byteLength > max && false) {\n",
        "expect": [T_READ],
    },
    {
        "name": "M8 不把上限交给读取器（约束只在读完以后才生效）",
        "file": PREVIEW_TS,
        "old": "  const content = await read(input.absolutePath, max)\n",
        "new": "  const content = await read(input.absolutePath, max + 1)\n",
        "expect": [T_READ],
    },
    # -- 票据层：一个票据授权什么 ----------------------------------------
    {
        "name": "M9 令牌就是文件名（一个猜得出来的地址就是永久地址）",
        "file": PREVIEW_TS,
        "old": "      const token = random(32)\n",
        "new": "      const token = name\n",
        "expect": [T_GUESSABLE],
    },
    {
        "name": "M10 过期不生效（地址在票据死后继续回答）",
        "file": PREVIEW_TS,
        "old": "      if (ticket.expiresAt <= at) tickets.delete(token)\n",
        "new": "      if (ticket.expiresAt <= at && false) tickets.delete(token)\n",
        "expect": [T_EXPIRES, T_404],
    },
    {
        "name": "M11 没有上限（预览多了就无限留着）",
        "file": PREVIEW_TS,
        "old": "      while (tickets.size >= max) {\n",
        "new": "      while (tickets.size >= max * 100) {\n",
        "expect": [T_BOUNDED],
    },
    {
        "name": "M12 一个票据能换成另一个文件名（令牌变成一份读权限）",
        "file": PREVIEW_TS,
        "old": "  if (asked.name !== ticket.name) {\n",
        "new": "  if (false) {\n",
        "expect": [T_NO_SIBLING],
    },
    {
        "name": "M13 别的主机也认（把票据当全局命名空间）",
        "file": PREVIEW_TS,
        "old": "  if (parsed.host !== PREVIEW_HOST) return null\n",
        "new": "  if (false) return null\n",
        "expect": [T_NO_SIBLING],
    },
    {
        "name": "M14 未知票据不再 404（用状态码代替授权）",
        "file": PREVIEW_TS,
        # `status: 404` 在 `answerPreviewRequest` 里只出现一次：成功那条是 200。
        "old": "  return {\n    status: 404,\n",
        "new": "  return {\n    status: 200,\n",
        "expect": [T_404],
    },
    # -- 策略层：答案带着什么走 ------------------------------------------
    {
        "name": "M15 把 allow-same-origin 加回去（沙箱当场失效）",
        "file": PREVIEW_TS,
        # 这一个 flag 的缺席就是整条隔离：有了它，生成页和宿主同源。
        "old": "      'sandbox allow-scripts allow-forms allow-modals',\n",
        "new": "      'sandbox allow-scripts allow-forms allow-modals allow-same-origin',\n",
        "expect": [T_POLICY],
    },
    {
        "name": "M16 图片和录音也放脚本（数据被当代码）",
        "file": PREVIEW_TS,
        "old": "  return common.join('; ')\n",
        "new": "  return common.join('; ') + ' allow-scripts'\n",
        "expect": [T_POLICY],
    },
    {
        "name": "M17 生成页可以连网（报告能把预览到的东西送出去）",
        "file": PREVIEW_TS,
        # 需要 6 空格缩进的那一处：4 空格缩进的 `connect-src 'none'` 在 common
        # 里，改到那一处会让两个分支一起松，而用例只针对 html。
        "old": "      \"connect-src 'none'\",\n      \"form-action 'none'\",\n",
        "new": "      \"connect-src *\",\n      \"form-action 'none'\",\n",
        "expect": [T_POLICY],
    },
    {
        "name": "M18 允许缓存（过期的副本比票据活得久）",
        "file": PREVIEW_TS,
        # 只有 `previewHeaders` 里那一处带上面两行注释；`answerPreviewRequest`
        # 的 404 分支里也有同样的 Cache-Control，锚点必须越过它。
        "old": (
            "    // outlive the ticket that authorized it.\n"
            "    'Cache-Control': 'no-store, max-age=0',\n"
        ),
        "new": (
            "    // outlive the ticket that authorized it.\n"
            "    'Cache-Control': 'public, max-age=3600',\n"
        ),
        "expect": [T_HEADERS],
    },
    {
        "name": "M19 引用来源漏出去（预览把宿主地址交给被预览的文档）",
        "file": PREVIEW_TS,
        "old": "    'Referrer-Policy': 'no-referrer',\n",
        "new": "    'Referrer-Policy': 'unsafe-url',\n",
        "expect": [T_HEADERS],
    },
    {
        "name": "M20 URL 里把文件名放在令牌前面（令牌不再是路径的第一段）",
        "file": PREVIEW_TS,
        "old": (
            "  return `${PREVIEW_SCHEME}://${PREVIEW_HOST}/${encodeURIComponent(token)}/"
            "${encodeURIComponent(name)}`\n"
        ),
        "new": (
            "  return `${PREVIEW_SCHEME}://${PREVIEW_HOST}/${encodeURIComponent(name)}/"
            "${encodeURIComponent(token)}`\n"
        ),
        "expect": [T_ONE_FILE],
    },
    # -- 桥层：页面能指名什么，桥就放行什么 --------------------------------
    {
        "name": "M21 路径形状不查就递给宿主（畸形路径直达本机）",
        "file": HOST_BRIDGE_TS,
        # 锚点必须唯一：`checkProjectPath(params.path, false)` 在 projectResolve /
        # projectRead / projectWrite 里也出现，只写这一行会改到别的 case 上 ——
        # 那会静默地"通过"，因为套件根本不跑那三个方法。带上后面那一行模板字符串
        # （里面写着 `projectPreviewFile.path`）才落在这一个 case 上。
        "old": (
            "      if (!checkProjectPath(params.path, false)) {\n"
            "        return refuse('invalid_path', 'projectPreviewFile.path must be a project-relative path')\n"
            "      }\n"
        ),
        "new": (
            "      if (!checkProjectPath(params.path, false) && false) {\n"
            "        return refuse('invalid_path', 'projectPreviewFile.path must be a project-relative path')\n"
            "      }\n"
        ),
        "expect": [T_MALFORMED],
    },
    {
        "name": "M22 预览不提 workspace_id 也收（宿主不知道该问哪个项目）",
        "file": HOST_BRIDGE_TS,
        "old": (
            "      if (!requireWorkspace(params)) {\n"
            "        return refuse('invalid_request', 'projectPreviewFile requires a workspace_id')\n"
            "      }\n"
        ),
        "new": (
            "      if (false) {\n"
            "        return refuse('invalid_request', 'projectPreviewFile requires a workspace_id')\n"
            "      }\n"
        ),
        "expect": [T_MALFORMED],
    },
    {
        "name": "M23 宿主抛错被压成「这台机器不支持预览」（该重试的被说成该升级）",
        "file": HOST_IPC_TS,
        # 「这台机器不支持」和「这台机器刚才坏了」是两个答案：前者让用户去升级，
        # 后者让用户重试。把它们合并，就等于把一次设备抖动变成一次功能缺失。
        # 注：同一个 case 里 `!isRemoteLocalFilesEnabled()` 那道闸门不在这里变异 ——
        # 它在桥层已经被 `methods` 挡住（能力关着时调用根本不会走到这里），改它
        # 在这批用例里**不可观测**，硬写成"变异"只会是自欺。
        "old": (
            "      try {\n"
            "        return projectReply(await port.previewFile(params))\n"
            "      } catch (err) {\n"
            "        return refusal('device_error', String((err as Error)?.message || err))\n"
            "      }\n"
            "    }\n"
            "    case 'writeBack':\n"
        ),
        "new": (
            "      try {\n"
            "        return projectReply(await port.previewFile(params))\n"
            "      } catch (err) {\n"
            "        return refusal('feature_unavailable', 'this host cannot preview a local project file')\n"
            "      }\n"
            "    }\n"
            "    case 'writeBack':\n"
        ),
        "expect": [T_UNAVAILABLE],
    },
    {
        "name": "M24 本机文件关着也把预览报成可用（页面点了才失败）",
        "file": LOCAL_FILES_TS,
        "old": "    methods: localFiles\n      ? [...ALL_BRIDGE_METHODS]\n      : [...PHASE1_METHODS],\n",
        "new": "    methods: [...ALL_BRIDGE_METHODS],\n",
        "expect": [T_GATE],
    },
    # -- 容器层：scheme 建在哪里、拆在哪里 ---------------------------------
    {
        "name": "M25 给 scheme 加上 supportFetchAPI（页面能 fetch 到本地字节）",
        "file": PREVIEW_TS,
        # 这一条是探针测出来的**值**：注册了 fetch 权限，容器文档就能把本机文件的
        # 字节读进脚本 —— 本任务存在的意义正好相反。单测里钉住这个对象，探针才算
        # 有回归网。
        "old": "export const PREVIEW_SCHEME_PRIVILEGES = {\n  standard: true,\n  secure: true,\n  stream: true,\n}\n",
        "new": (
            "export const PREVIEW_SCHEME_PRIVILEGES = {\n  standard: true,\n  secure: true,\n"
            "  stream: true,\n  supportFetchAPI: true,\n}\n"
        ),
        "expect": [T_PRIVILEGES],
    },
    {
        "name": "M26 装到别的会话上（预览 URL 在页面所在的文档里没人回答）",
        "file": CONTAINER_TS,
        "old": "  installPreviewProtocol(child.partition)\n",
        "new": "  installPreviewProtocol('persist:cow-preview')\n",
        "expect": [T_INSTALLED],
    },
    {
        "name": "M27 卸载时不收票据（上一次会话的地址活到下一次）",
        "file": CONTAINER_TS,
        "old": "  previewTickets.revokeAll()\n  previewTickets = createPreviewTickets()\n",
        "new": "  previewTickets = createPreviewTickets()\n",
        "expect": [T_INSTALLED],
    },
    # -- 页面层：面板说什么、指向哪里 --------------------------------------
    {
        "name": "M28 本机 HTML 指向服务器地址（预览到的是同名的另一个文件）",
        "file": WORKSPACE_JS,
        "old": "                frame.src = ticket.url;\n",
        "new": "                frame.src = rawUrl;\n",
        "expect": [T_PAGE_EMBEDS],
    },
    {
        "name": "M29 本机图片指向服务器地址（永远打不开的图）",
        "file": WORKSPACE_JS,
        "old": "    media.src = ticket.url;\n",
        "new": "    media.src = rawUrl;\n",
        "expect": [T_PAGE_EMBEDS],
    },
    {
        "name": "M30 面板不再问宿主（本机文件退回没有 URL 的旧行为）",
        "file": WORKSPACE_JS,
        "old": "    return CowProjectSource.preview(wsEditTargetPath(meta));\n",
        "new": "    return null;\n",
        "expect": [T_PAGE_EMBEDS],
    },
    {
        "name": "M31 帧不再带沙箱标记（生成页能导航、能开窗）",
        "file": WORKSPACE_JS,
        "old": "            frame.setAttribute('sandbox', 'allow-scripts allow-forms allow-modals');\n",
        "new": "            frame.setAttribute('sandbox', 'allow-scripts allow-forms allow-modals allow-same-origin');\n",
        "expect": [T_PAGE_SANDBOX],
    },
    {
        "name": "M32 问宿主能不能预览之前就报可用（点了才知道不行）",
        "file": PROJECT_SOURCE_JS,
        "old": "    return host().canUseProjectPreview().catch(function () { return false; });\n",
        "new": "    return Promise.resolve(true);\n",
        "expect": [T_PAGE_REFUSALS],
    },
    {
        "name": "M33 任何拒绝都当成「这台机器不支持」（真实原因被吞掉）",
        "file": WORKSPACE_JS,
        "old": "    return code === 'feature_unavailable' || code === 'no_host';\n",
        "new": "    return true;\n",
        "expect": [T_PAGE_REFUSALS],
    },
    {
        "name": "M34 太大和类型不支持共用一个说法（用户不知道该做什么）",
        "file": WORKSPACE_JS,
        "old": "    if (code === 'limit_exceeded') return t('ws_local_preview_too_large');\n",
        "new": "    if (false) return t('ws_local_preview_too_large');\n",
        "expect": [T_PAGE_REFUSALS],
    },
    {
        "name": "M35 一种语言漏掉一句话（那种语言的用户只看到一片空白）",
        "file": CORE_I18N_JS,
        # 只改英文那一处：三份字典各一次，少一份就是 2 次。
        "old": (
            '            "ws_local_preview_too_large": "This local file is too large to preview'
            ' in isolation - open it with a system application",\n'
        ),
        "new": (
            '            "ws_local_preview_too_large_": "This local file is too large to preview'
            ' in isolation - open it with a system application",\n'
        ),
        "expect": [T_PAGE_REFUSALS],
    },
]


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def failed_tests(output: str) -> list:
    """The names of the tests that failed, whatever runner produced the output.

    ``node --test`` marks a failure with ``✖ <name> (<ms>)``; pytest with
    ``FAILED <path>::[<class>::]<name>``. Both are read, because a mutation may
    be aimed at either layer -- and a parser that only knew one format would
    report a caught mutation as uncaught, which is the one thing this script
    must not do.
    """
    names = set(re.findall(r"^\u2716 (.+?) \(\d", output, flags=re.M))
    for target in re.findall(r"^(?:FAILED|SUBFAILED\([^)]*\)) (tests/\S+)::(\S+)", output,
                             flags=re.M):
        names.add(target[1].split("::")[-1])
    return sorted(names)


def run_suite(suite: str) -> str:
    if suite.endswith(".py"):
        command = [sys.executable, "-m", "pytest", suite, "-q", "-p", "no:randomly"]
    else:
        command = ["node", "--test", suite]
    proc = subprocess.run(
        command, cwd=REPO,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.stdout


def summarize(output: str) -> str:
    counts = re.search(r"^\u2139 fail (\d+)$", output, flags=re.M)
    passing = re.search(r"^\u2139 pass (\d+)$", output, flags=re.M)
    if counts and passing:
        return "%s failed, %s passed" % (counts.group(1), passing.group(1))
    tail = [line for line in output.strip().splitlines() if line.strip()]
    return tail[-1] if tail else "（无输出）"


def broken_run(output: str) -> str:
    """A mutation that does not even build proves nothing about the tests.

    ``node --test`` reports every test as failed when the module (or the
    suite's ``before``) throws, which *looks* like a catch. It is not: the
    suite never got to assert anything. TypeScript is the case that matters
    here -- a mutation that breaks the types fails the compile, not the
    assertion -- so such a mutation must be rewritten rather than counted.

    The compile error itself is *not* in the captured output: the suite shells
    out to ``tsc`` with ``stdio: 'pipe'``, so all that surfaces is node's own
    ``Command failed: npx tsc ...``. Checking only for ``error TS`` would miss
    exactly the case this function exists for.
    """
    for pattern in (r"error TS\d+", r"SyntaxError", r"Cannot find module",
                    r"Command failed: npx tsc", r"Command failed:",
                    r"IndentationError", r"ERROR tests/.*collect"):
        if re.search(pattern, output):
            return pattern
    return ""


def main() -> int:
    failures: list = []
    for mutation in MUTATIONS:
        path = mutation["file"]
        original = read(path)
        found = original.count(mutation["old"])
        if found != 1:
            # 锚点出现 0 次是源码变了；出现多次更危险 —— ``replace(..., 1)`` 会
            # 改到**第一处**，而那多半不是想改的那一处，于是用例照样全绿、脚本
            # 却报告"变异没被发现"，或者更糟：报告成功而实际测的是别的分支。
            print(f"[skip] {mutation['name']}: 锚点出现 {found} 次（需恰好 1 次）")
            failures.append(mutation["name"])
            continue
        write(path, original.replace(mutation["old"], mutation["new"], 1))
        try:
            output = run_suite(SUITE)
        finally:
            write(path, original)
        assert read(path) == original, f"还原失败：{path}"
        caught = failed_tests(output)
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        print(f"\n### {mutation['name']}")
        print(f"  文件：{os.path.relpath(path, REPO)}")
        print(f"  汇总：{summarize(output)}")
        print(f"  预期失败命中：{hits}")
        print(f"  实际失败：{caught}")
        broken = broken_run(output)
        if broken:
            print(f"  ** 变异后根本编不过/装不起来（{broken}）：这不是用例发现的，"
                  "必须改写这条变异")
        if not hits or broken:
            failures.append(mutation["name"])

    print("\n== 结论 ==")
    if failures:
        print("变异未被用例发现（用例太弱或锚点失效）：")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("隔离预览五层（决策 / 票据 / 策略 / 桥与容器 / 页面）共 %d 类实现走样都被"
          "同一批用例判为失败，且还原后源文件与原文一致。" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
