#!/usr/bin/env python3
"""变异检查：本机文件动作（任务 9.4，验收 A25）的断言真的会因为实现被改坏而失败。

第 9.4 条把「打开 / 在文件管理器里定位 / 复制真实路径 / 另存一份」交给客户端的
原生层，而原生动作用户没法撤销：系统会跟着交给它的**任何**路径走，穿链接、出项目。
所以四处走样都必须被用例抓住：

* **决策层**（`desktop/src/main/project-browser/native-actions.ts`）：信 helper 的
  分类而不看磁盘（链接被当普通文件）、只在拼接字符串上判包含（目录里的链接把
  动作引到项目外）、把绝对路径当项目相对路径收下、文件已经被改过还照样复制、
  复制到文件自身（截断）、系统明明打不开却回成功、把「没有关联应用」压成
  「打不开」，以及把真实路径写进回复（用户的目录结构漏到页面上）。
* **桥层**（`desktop/src/main/remote/host-bridge.ts`、`remote/local-files-bridge.ts`）：
  畸形路径直接递给宿主、版本号负数也放行、能力还没开就把四个动作报成可用。
* **适配层**（`channel/web/static/js/fork/project-source.js`）：动作不带当前工作区、
  不认「这个会话里没有本机项目」、桥抛错就变成页面的异常。
* **宿主适配层**（`channel/web/static/js/fork/desktop-host.js`）：只认预载方法名而
  不认面板说的动作名（四个按钮全被按名字拒绝，桥两端各自都好、点下去什么也不发生）、
  把动作表当字典用（`constructor` 也算动作名）。
* **页面层**（`channel/web/static/js/workspace.js`、`channel/web/chat.html`）：
  改过的文件不问用户就复制、四种失败共用一个说法、本机文件的按钮不改语义、
  卡片给本机文件一个不存在的服务器下载地址、定位按钮对服务器文件也显示。

每一处都是一条**看起来更省事**的写法，跑同一批用例，要求出现预期失败后立刻还原
源文件；任何一项「改坏了却全绿」都会让脚本以非零码退出。

注意：node 套件用源文件 mtime 决定是否重新 `tsc`（`needsBuild`），改过 `.ts` 就会
重编，所以它读到的一定是变异后的字节 —— 不需要手动清构建缓存。

用法：`.venv/bin/python openspec/changes/.../evidence/scripts/mutate_project_native_actions.py`
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))

NATIVE_TS = os.path.join(REPO, "desktop", "src", "main", "project-browser", "native-actions.ts")
HOST_BRIDGE_TS = os.path.join(REPO, "desktop", "src", "main", "remote", "host-bridge.ts")
LOCAL_FILES_TS = os.path.join(REPO, "desktop", "src", "main", "remote", "local-files-bridge.ts")
PROJECT_SOURCE_JS = os.path.join(REPO, "channel", "web", "static", "js", "fork", "project-source.js")
DESKTOP_HOST_JS = os.path.join(REPO, "channel", "web", "static", "js", "fork", "desktop-host.js")
WORKSPACE_JS = os.path.join(REPO, "channel", "web", "static", "js", "workspace.js")
CHAT_HTML = os.path.join(REPO, "channel", "web", "chat.html")

NODE_SUITE = "tests/test_desktop_project_native_actions.cjs"
PAGE_SUITE = "tests/test_desktop_web_pages.py"
PAGE_TEST = "test_w25_the_local_file_actions_are_reachable_and_speak_the_hosts_language"

MUTATIONS = [
    # -- 决策层：谁有资格被系统动作碰 --------------------------------
    {
        "name": "M1 用 statSync 代替 lstatSync（跟随链接，把链接当普通文件）",
        "suite": NODE_SUITE,
        "file": NATIVE_TS,
        # 不能写成 `if (stat.isSymbolicLink() && entryKind === 'link')`：helper 的
        # 取值为 'dir' | 'file'，比较 'link' 是 TS2367，变异后根本编不过 —— 那是
        # "编译失败"，不是"用例发现走样"。用跟随链接的 statSync 来走样，才有意义。
        "old": "    stat = fs.lstatSync(candidate)\n",
        "new": "    stat = fs.statSync(candidate)\n",
        "expect": [
            "an entry that is a link, or a directory asked to be saved as a file, is refused",
            "an entry that resolves outside the project is refused even if the helper allowed it",
        ],
    },
    {
        "name": "M2 只在拼接的字符串上判包含（目录里的链接把动作引到项目外）",
        "suite": NODE_SUITE,
        "file": NATIVE_TS,
        "old": "  if (!isInside(realRoot, real)) {\n",
        "new": "  if (!isInside(realRoot, real) && false) {\n",
        "expect": ["an entry that resolves outside the project is refused even if the helper allowed it"],
    },
    {
        "name": "M3 绝对路径也收下（页面能指名项目外的文件）",
        "suite": NODE_SUITE,
        "file": NATIVE_TS,
        "old": (
            "  if (raw.startsWith('/') || /^[a-zA-Z]:/.test(raw)) return ''\n"
            "  const parts = raw.split(/[\\\\/]+/)\n"
            "  if (parts.some((part) => part === '..' || part === '' || part === '.')) return ''\n"
            "  return parts.join('/')\n"
        ),
        "new": "  return raw.replace(/[\\\\/]+/g, '/')\n",
        "expect": ["a path that could never be project-relative is refused before any check"],
    },
    {
        "name": "M4 条目类型和 helper 说的不一致也不管（打开用户没看见的东西）",
        "suite": NODE_SUITE,
        "file": NATIVE_TS,
        "old": "  if (stat.isDirectory() !== (entryKind === 'dir')) {\n",
        "new": "  if (stat.isDirectory() !== (entryKind === 'dir') && false) {\n",
        "expect": ["an entry whose kind changed on disk is refused rather than acted on"],
    },
    # -- 决策层：动作本身 ------------------------------------------------
    {
        "name": "M5 系统打不开也回成功（用户以为开了）",
        "suite": NODE_SUITE,
        "file": NATIVE_TS,
        "old": "      if (failure) return describeOpenFailure(failure)\n",
        "new": "      if (failure && false) return describeOpenFailure(failure)\n",
        "expect": ["an operating system failure is reported as the system described it"],
    },
    {
        "name": "M6 把「没有关联应用」压成「打不开」（用户不知道该装什么）",
        "suite": NODE_SUITE,
        "file": NATIVE_TS,
        "old": (
            "  if (/no application|application cannot be opened|application is associated|"
            "no handler|no.*registered|not associated|no default application|missing application/i.test(text)) {\n"
            "    return refuse('no_application', text)\n"
            "  }\n"
        ),
        # 注意：不能写成空串 —— 脚本的自检会拒绝「替换成空」的变异（那是删除
        # 而不是走样，也不该让锚点检查通过）。留一行注释，保持替换前后都是源
        # 文件里合法的 TS。
        "new": "  // 没有关联应用也只是「打不开」：见下行\n",
        "expect": ["an operating system failure is reported as the system described it"],
    },
    {
        "name": "M7 复制路径时顺手把真实路径交给页面（用户目录结构漏出去）",
        "suite": NODE_SUITE,
        "file": NATIVE_TS,
        "old": (
            "      return { ok: true, kind: 'copyPath', name: plan.name, "
            "is_directory: plan.isDirectory, copied: true }\n"
        ),
        "new": (
            "      return { ok: true, kind: 'copyPath', name: plan.name, "
            "is_directory: plan.isDirectory, copied: true,\n"
            "        absolute_path: plan.absolutePath } as NativeActionResult\n"
        ),
        "expect": ["reveal and copy-path act on the machine and keep the path out of the reply"],
    },
    {
        "name": "M8 版本比较写反（拿相同当冲突，改过的文件照样复制）",
        "suite": NODE_SUITE,
        "file": NATIVE_TS,
        # 不能写成 `... && false`：`expected !== plan.modified && false` 会让 TS 认为
        # 这个条件恒为假而报 2367，变异后编不过。把比较写成 `===` 才是能编译的
        # 走样：既不再拦"文件已改"，又把"版本一致"当成冲突。
        "old": (
            "      if (typeof expected === 'number' && expected > 0 && !options.acceptCurrent\n"
            "        && expected !== plan.modified) {\n"
        ),
        "new": (
            "      if (typeof expected === 'number' && expected > 0 && !options.acceptCurrent\n"
            "        && expected === plan.modified) {\n"
        ),
        "expect": ["a copy is refused while the file has moved on, unless the user says so"],
    },
    {
        "name": "M9 允许复制到文件自身（把原件截断成一份副本）",
        "suite": NODE_SUITE,
        "file": NATIVE_TS,
        "old": "      if (path.resolve(String(destination)) === path.resolve(plan.absolutePath)) {\n",
        "new": "      if (path.resolve(String(destination)) === path.resolve(plan.absolutePath) && false) {\n",
        "expect": ["a copy onto the file itself is refused rather than truncated"],
    },
    # -- 桥层：页面能指名什么，桥就放行什么 ------------------------------
    {
        "name": "M10 路径形状不查就递给宿主（畸形路径直达本机）",
        "suite": NODE_SUITE,
        "file": HOST_BRIDGE_TS,
        # 注意锚点必须**唯一**：同一个 `checkProjectPath(params.path, false)`
        # 在 projectResolve / projectRead / projectWrite 里也出现，只写这一行会
        # 改到别的 case 上 —— 那会静默地"通过"，因为套件根本不跑那三个方法。
        # 带上后面的模板字符串行，锚点才落在四个原生动作这一个 case 上。
        "old": (
            "      if (!checkProjectPath(params.path, false)) {\n"
            "        return refuse('invalid_path', `${method}.path must be a project-relative path`)\n"
            "      }\n"
        ),
        "new": (
            "      if (!checkProjectPath(params.path, false) && false) {\n"
            "        return refuse('invalid_path', `${method}.path must be a project-relative path`)\n"
            "      }\n"
        ),
        "expect": ["a malformed action never reaches the port"],
    },
    {
        "name": "M11 版本号只查类型不查符号（负版本放行到宿主）",
        "suite": NODE_SUITE,
        "file": HOST_BRIDGE_TS,
        "old": (
            "        if (typeof params.expected_mtime !== 'number' || !Number.isFinite(params.expected_mtime)\n"
            "          || params.expected_mtime < 0) {\n"
        ),
        "new": "        if (typeof params.expected_mtime !== 'number') {\n",
        "expect": ["a malformed action never reaches the port"],
    },
    {
        "name": "M12 本机文件能力还没开就报四个动作可用（页面点了才失败）",
        "suite": NODE_SUITE,
        "file": LOCAL_FILES_TS,
        "old": "    methods: localFiles\n      ? [...ALL_BRIDGE_METHODS]\n      : [...PHASE1_METHODS],\n",
        "new": "    methods: [...ALL_BRIDGE_METHODS],\n",
        "expect": ["the four actions are published with local files, and not without them"],
    },
    # -- 适配层：面板问什么 ----------------------------------------------
    {
        "name": "M13 动作不带当前工作区（宿主不知道该问哪个项目）",
        "suite": NODE_SUITE,
        "file": PROJECT_SOURCE_JS,
        "old": "    var params = { workspace_id: String(ctx.workspace_id), path: target };\n",
        "new": "    var params = { path: target };\n",
        "expect": ["an action carries the live workspace, and a refusal comes back as an answer"],
    },
    {
        "name": "M14 只看宿主不看绑定（没有本机项目也照样问）",
        "suite": NODE_SUITE,
        "file": PROJECT_SOURCE_JS,
        # 锚点必须唯一：`if (!ctx || !bridge)` 在 `preview()` 里也出现，只写这一行
        # 会改到那个函数上（任务 9.5 之后才有的一处），于是套件全绿而脚本报告
        # "变异没被发现"。带上 `var bridge = actionHost();` 才落在 `act()` 上。
        "old": (
            "    var bridge = actionHost();\n"
            "    var opts = options || {};\n"
            "    if (!ctx || !bridge) {\n"
        ),
        "new": (
            "    var bridge = actionHost();\n"
            "    var opts = options || {};\n"
            "    if (!bridge) {\n"
        ),
        "expect": ["an action without a project, or without a path, is refused by name"],
    },
    {
        "name": "M15 桥抛错就抛给页面（拒绝变成异常）",
        "suite": NODE_SUITE,
        "file": PROJECT_SOURCE_JS,
        "old": (
            "    return bridge.projectAction(action, params).catch(function (err) {\n"
            "      return actionRefusal((err && err.code) || 'device_error',\n"
            "        (err && err.message) || 'the local project action was refused');\n"
            "    });\n"
        ),
        "new": "    return bridge.projectAction(action, params);\n",
        "expect": ["an action without a project, or without a path, is refused by name"],
    },
    # -- 页面层：按钮说的是什么 ------------------------------------------
    {
        "name": "M16 本机文件的按钮不换语义（点了去开一个不存在的服务器地址）",
        "suite": NODE_SUITE,
        "file": WORKSPACE_JS,
        "old": "    const local = onFile && wsIsLocalFile(wsCurrentFile);\n",
        "new": "    const local = false;\n",
        "expect": [
            "a local file's buttons say what they will do, and a server file's keep saying theirs",
        ],
    },
    {
        "name": "M17 本机文件的卡片给一个服务器下载地址（点了必然 404）",
        "suite": NODE_SUITE,
        "file": WORKSPACE_JS,
        "old": "    const actions = wsIsLocalFile(meta)\n",
        "new": "    const actions = false\n",
        "expect": ["a local file card carries the system actions instead of a server download"],
    },
    {
        "name": "M18 改过的文件不问用户就复制（默默复制用户没看过的版本）",
        "suite": NODE_SUITE,
        "file": WORKSPACE_JS,
        "old": "    if (code === 'changed') {\n",
        "new": "    if (false) {\n",
        "expect": ["saving a copy asks before it copies a version the user has not seen"],
    },
    {
        "name": "M19 四种失败共用一个说法（「文件没了」和「没装应用」分不开）",
        "suite": NODE_SUITE,
        "file": WORKSPACE_JS,
        # 锚点必须唯一：`if (code === 'not_found') return t('ws_local_file_gone');`
        # 在 `wsLocalPreviewMessage`（任务 9.5）里也出现，只写这一行会改到预览那一
        # 处，于是本套件全绿而脚本报告"变异没被发现"。带上上一行的 `suffix` 才落在
        # `wsLocalActionMessage` 上。
        "old": (
            "    const suffix = detail && detail !== code ? ` (${detail})` : '';\n"
            "    if (code === 'not_found') return t('ws_local_file_gone');\n"
        ),
        "new": (
            "    const suffix = detail && detail !== code ? ` (${detail})` : '';\n"
            "    if (false) return t('ws_local_file_gone');\n"
        ),
        "expect": ["the four reasons a system action did not happen are told apart"],
    },
    # -- 页面层：页面本身 --------------------------------------------------
    {
        "name": "M20 定位按钮默认就显示（服务器文件也被提供一个做不到的动作）",
        "suite": PAGE_SUITE,
        "file": CHAT_HTML,
        "old": '<button id="ws-btn-reveal" class="workspace-icon-btn hidden"',
        "new": '<button id="ws-btn-reveal" class="workspace-icon-btn"',
        "expect": [PAGE_TEST],
    },
    # -- 宿主适配层：面板说的话宿主听不听得懂 ----------------------------
    {
        "name": "M21 宿主只认预载方法名（面板的四个动作全被按名字拒绝）",
        "suite": NODE_SUITE,
        "file": DESKTOP_HOST_JS,
        # 这正是修之前的实现：`projectAction()` 拿 `PROJECT_ACTION_METHODS`
        # （预载方法名）去比面板说的动作名。两端各自都"好"——适配器问得没错、
        # 预载也答得没错——只有点下去什么也不发生，所以这条走样必须被用例抓住。
        "old": (
            "      var preloadMethod = Object.prototype.hasOwnProperty.call(PROJECT_ACTIONS, action)\n"
            "        ? PROJECT_ACTIONS[action] : '';\n"
            "      if (!preloadMethod) {\n"
        ),
        "new": (
            "      var preloadMethod = action;\n"
            "      if (PROJECT_ACTION_METHODS.indexOf(preloadMethod) < 0) {\n"
        ),
        "expect": ["every action the panel names reaches the preload method the host declares"],
    },
    {
        "name": "M22 动作表当字典用（`constructor` 也算一个动作名）",
        "suite": NODE_SUITE,
        "file": DESKTOP_HOST_JS,
        "old": (
            "      var preloadMethod = Object.prototype.hasOwnProperty.call(PROJECT_ACTIONS, action)\n"
            "        ? PROJECT_ACTIONS[action] : '';\n"
        ),
        "new": "      var preloadMethod = PROJECT_ACTIONS[action] || '';\n",
        "expect": ["an action name that is not one of the four is refused, table hooks included"],
    },
    {
        "name": "M23 页面把两个动作对到同一个方法上（两端各说一词，点下去全错）",
        "suite": PAGE_SUITE,
        "file": DESKTOP_HOST_JS,
        # 这条走样在**页面**里看是"表填错了"，在**主进程**里完全看不出来：
        # 主进程的表（`native-actions.ts` 的 `NATIVE_METHOD_BY_ACTION`）是对的，
        # 页面这份是它的镜像。镜像漂了，只有跨端比对才抓得到。
        "old": "    copyPath: 'projectCopyPath',\n",
        "new": "    copyPath: 'projectSaveFileAs',\n",
        "expect": ["test_w25_the_two_ends_agree_on_the_four_action_names"],
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
    ``Command failed: npx tsc ...``. Checking only for ``error TS`` therefore
    misses exactly the case this function exists for, and counts a broken
    build as a caught mutation -- which is what happened here with M1 and M8
    before the pattern was added.
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
            output = run_suite(mutation["suite"])
        finally:
            write(path, original)
        assert read(path) == original, f"还原失败：{path}"
        caught = failed_tests(output)
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        print(f"\n### {mutation['name']}")
        print(f"  套件：{mutation['suite']}")
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
    print("本机文件动作五处（决策 / 桥 / 适配 / 宿主适配 / 页面）共 %d 类实现走样都被对应用例判为失败，"
          "且还原后源文件与原文一致。" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
