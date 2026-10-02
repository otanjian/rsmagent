#!/usr/bin/env python3
"""变异检查：有界刷新/订阅与失效清理（任务 9.3）的断言真的会因为实现被改坏而失败。

第 9.3 条有两端，而它们防的是两类不同的坏结果：

* **设备端**（`desktop/src/main/project-browser/watch.ts`）：把"没看全"说成"看全
  了"。首次扫描当变更、截断的扫描报删除、条目/深度上限不算截断、一次要得比设备
  肯给的页还大、授权搬走了还接着看——每一条都会让面板显示一个**不存在的事实**
  （整个项目都变了 / 半个项目被删了 / 这个目录还是你的）。
* **页面端**（`channel/web/static/js/console.js`、`workspace.js`、
  `fork/desktop-host.js` 与 `desktop/src/main/project-browser/restore.ts`）：把
  "授权已经没了"说成"还有"。刷新后凭记录（而不是凭实时授权）恢复、把别的会话的
  记录当成自己的、把 `stale` 说成 `none`、服务器列表覆盖本机 chip、失效时一声不
  吭——每一条都会让用户以为自己的文件还在本机打开着。

每一处走样都是一条**看起来更省事**的实现，跑同一批用例，要求出现预期失败后立刻还
原源文件；任何一项「改坏了却全绿」都会让脚本以非零码退出。

注意：两个套件都用源文件 mtime 决定是否重新 `tsc`（`needsBuild`），改过 `.ts` 就
会重编，所以它们读到的一定是变异后的字节——不需要手动清构建缓存。

用法：`.venv/bin/python openspec/changes/.../evidence/scripts/mutate_project_watching.py`
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))

WATCH_TS = os.path.join(REPO, "desktop", "src", "main", "project-browser", "watch.ts")
RESTORE_TS = os.path.join(REPO, "desktop", "src", "main", "project-browser", "restore.ts")
CONSOLE_JS = os.path.join(REPO, "channel", "web", "static", "js", "console.js")
WORKSPACE_JS = os.path.join(REPO, "channel", "web", "static", "js", "workspace.js")
DESKTOP_HOST_JS = os.path.join(REPO, "channel", "web", "static", "js", "fork", "desktop-host.js")

WATCH_SUITE = "tests/test_desktop_project_watch.cjs"
REFRESH_SUITE = "tests/test_desktop_project_refresh.cjs"

MUTATIONS = [
    # -- 设备端：监视器 --------------------------------------------------
    {
        "name": "M1 首次扫描不是基线（一开面板就说整个项目都变了）",
        "suite": WATCH_SUITE,
        "file": WATCH_TS,
        # 这一条**不能**写成 `if (false)`:TS 对不可达块不做控制流收窄,
        # `scan.seen` 会变成类型错误,套件整体构建失败 —— 那是"编不过",
        # 不是"用例发现了走样"。所以改成一条会真的放过首次扫描的条件。
        "old": (
            "    if (this.seen.size === 0) {\n"
            "      this.seen = scan.seen\n"
            "      return null\n"
            "    }\n"
        ),
        "new": (
            "    if (this.seen.size === 0 && this.scans > 1) {\n"
            "      this.seen = scan.seen\n"
            "      return null\n"
            "    }\n"
        ),
        "expect": ["the first scan is a baseline"],
    },
    {
        "name": "M2 截断的扫描照样报删除（把没走到当成已删除）",
        "suite": WATCH_SUITE,
        "file": WATCH_TS,
        "old": (
            "    const removed: string[] = []\n"
            "    if (!scan.truncated) {\n"
        ),
        "new": (
            "    const removed: string[] = []\n"
            "    if (true) {\n"
        ),
        "expect": [
            "a directory the truncated scan did not reach is not reported as removed",
            "a directory bigger than one page is a partial view",
        ],
    },
    {
        "name": "M3 授权搬走了还接着看（只看工作区 id，不看 grant 版本/重选）",
        "suite": WATCH_SUITE,
        "file": WATCH_TS,
        "old": "    if (this.scope && scopeKey(current) !== scopeKey(this.scope)) return null\n",
        "new": "    if (false) return null\n",
        "expect": [
            "a re-picked project stops the watch instead of following it",
            "a new grant version is a different authorization",
            "a re-attached device epoch stops the watch",
            "verifyScope stops a watch whose authorization moved",
        ],
    },
    {
        "name": "M4 授权被撤了也不停（拿旧授权当现在还在）",
        "suite": WATCH_SUITE,
        "file": WATCH_TS,
        "old": "    if (!current || current.workspaceId !== this.workspaceId) return null\n",
        "new": (
            "    if (current === null && this.scope !== null) return this.scope\n"
            "    if (!current || current.workspaceId !== this.workspaceId) return null\n"
        ),
        "expect": ["a revoked project stops the watch and forgets what it had seen"],
    },
    {
        "name": "M5 一次要点得比设备肯给的页还大（一整页当成看全了）",
        "suite": WATCH_SUITE,
        "file": WATCH_TS,
        "old": "    const pageMax = Math.min(maxEntries, WATCH_PAGE_MAX)\n",
        "new": "    const pageMax = maxEntries\n",
        "expect": ["a directory bigger than one page is a partial view"],
    },
    {
        "name": "M6 队列里还有没看的目录却不说这是一次不完整的扫描",
        "suite": WATCH_SUITE,
        "file": WATCH_TS,
        "old": "    if (queue.length > 0) truncated = true\n",
        "new": "    if (false) truncated = true\n",
        "expect": [
            "a scan that hits its entry bound says so and never reports removals",
            "a directory the truncated scan did not reach is not reported as removed",
            "a project too deep to finish is honestly partial",
        ],
    },
    {
        "name": "M7 深度上限不算截断（下面还有目录却说看全了）",
        "suite": WATCH_SUITE,
        "file": WATCH_TS,
        "old": (
            "      if (dir.depth >= maxDepth) {\n"
            "        if (rows.some((row) => String(row.kind || '') === 'dir')) truncated = true\n"
            "        continue\n"
            "      }\n"
        ),
        "new": (
            "      if (dir.depth >= maxDepth) {\n"
            "        continue\n"
            "      }\n"
        ),
        "expect": ["a project too deep to finish is honestly partial"],
    },
    # -- 设备端：刷新后的恢复决定 ----------------------------------------
    {
        "name": "M8 凭记录就恢复（不复验授权是否还在）",
        "suite": REFRESH_SUITE,
        "file": RESTORE_TS,
        "old": "  const current = live(recorded.workspaceId)\n",
        "new": (
            "  const current = live(recorded.workspaceId) ?? {\n"
            "    workspaceId: recorded.workspaceId,\n"
            "    bindingId: recorded.bindingId,\n"
            "    grantId: recorded.grantId,\n"
            "    grantVersion: 0,\n"
            "    selectionGeneration: 0,\n"
            "    label: recorded.workspaceId,\n"
            "    executable: false,\n"
            "    connected: true,\n"
            "  }\n"
        ),
        "expect": ["a record whose grant is gone is stale, not an open project"],
    },
    {
        "name": "M9 别的会话（或别的 Agent）的记录也当成自己的",
        "suite": REFRESH_SUITE,
        "file": RESTORE_TS,
        "old": (
            "  const mine = (records || []).filter((entry) => (\n"
            "    entry.agentId === request.agentId\n"
            "    && entry.businessSessionId === request.businessSessionId))\n"
        ),
        "new": "  const mine = (records || [])\n",
        "expect": ["confirmation is not resumed"],
    },
    {
        "name": "M10 授权没了说成「本来就没有」（用户被告知的项目没了却不被告知）",
        "suite": REFRESH_SUITE,
        "file": RESTORE_TS,
        "old": "  if (!current) return { state: 'stale', reason: 'grant_revoked' }\n",
        "new": "  if (!current) return { state: 'none' }\n",
        "expect": ["a record whose grant is gone is stale, not an open project"],
    },
    # -- 页面端：刷新恢复 ------------------------------------------------
    {
        "name": "M11 服务器项目列表覆盖本机 chip（本机项目当场消失）",
        "suite": REFRESH_SUITE,
        "file": CONSOLE_JS,
        "old": "        const local = _desktopContextForRequest() ? _wsSelState.current : null;\n",
        "new": "        const local = null;\n",
        "expect": ["the server project list does not wipe the local chip while it is in effect"],
    },
    {
        "name": "M12 失效清理变成空操作（引用、面板、提示都不动）",
        "suite": REFRESH_SUITE,
        "file": CONSOLE_JS,
        "old": (
            "function _desktopLocalLost(reason) {\n"
            "    const had = !!_desktopContext;\n"
            "    _desktopContextClear();\n"
        ),
        "new": (
            "function _desktopLocalLost(reason) {\n"
            "    const had = !!_desktopContext;\n"
            "    if (had) return;\n"
            "    _desktopContextClear();\n"
        ),
        "expect": ["losing the local project drops the reference and re-reads the selector"],
    },
    {
        "name": "M13 恢复时 stale 不告诉用户（本机项目静默变成服务器工作区）",
        "suite": REFRESH_SUITE,
        "file": CONSOLE_JS,
        "old": "        if (reply && reply.state === 'stale') _wsToast(t('ws_sel_local_lost'));\n",
        "new": "        if (false) _wsToast(t('ws_sel_local_lost'));\n",
        "expect": ["an expired local project is not resumed, and the user is told why"],
    },
    {
        "name": "M14 迟到的答复照样采纳（用户已经切走了）",
        "suite": REFRESH_SUITE,
        "file": CONSOLE_JS,
        # 锚点必须唯一：同一行在"挑选目录"和"绑定确认"两处也有。带上紧跟的
        # 那一行，锚点才落在恢复（resume）这条路径上。
        "old": (
            "    if (requestKey !== _desktopSelectionKey()) return;\n"
            "    if (!reply || reply.state !== 'live') {\n"
        ),
        "new": (
            "    if (false) return;\n"
            "    if (!reply || reply.state !== 'live') {\n"
        ),
        "expect": ["an answer that arrives after the user moved on is dropped"],
    },
    # -- 页面端：面板与适配器 --------------------------------------------
    {
        "name": "M15 本机读被拒为 stale_context 却不断开引用",
        "suite": REFRESH_SUITE,
        "file": WORKSPACE_JS,
        "old": (
            "            if (local.code === 'stale_context' && typeof _desktopLocalLost === 'function') {\n"
            "                _desktopLocalLost(local.code);\n"
            "            }\n"
        ),
        "new": (
            "            if (false) {\n"
            "                _desktopLocalLost(local.code);\n"
            "            }\n"
        ),
        "expect": ["a local read refused as stale_context drops the reference too"],
    },
    {
        "name": "M16 宿主拒绝就抛给页面（拒绝变成异常）",
        "suite": REFRESH_SUITE,
        "file": DESKTOP_HOST_JS,
        "old": (
            "        return Promise.resolve(bridge.localContext(params || {})).catch(function (err) {\n"
            "          return { state: 'none', reason: (err && err.code) || 'refused' };\n"
            "        });\n"
        ),
        "new": "        return Promise.resolve(bridge.localContext(params || {}));\n",
        "expect": ['answers "nothing to resume", never a throw'],
    },
]


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def failed_node_tests(output: str) -> list:
    """``node --test`` marks a failing test with ``✖ <name> (<ms>)``.

    The marker is printed twice for a failure (once inline, once in the trailing
    "failing tests" list), so the set is what matters -- counting lines would
    double every hit.
    """
    return sorted(set(re.findall(r"^\u2716 (.+?) \(\d", output, flags=re.M)))


def run_suite(suite: str) -> str:
    proc = subprocess.run(
        ["node", "--test", suite], cwd=REPO,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.stdout


def summarize(output: str) -> str:
    counts = re.search(r"^\u2139 fail (\d+)$", output, flags=re.M)
    passing = re.search(r"^\u2139 pass (\d+)$", output, flags=re.M)
    if counts and passing:
        return "%s failed, %s passed" % (counts.group(1), passing.group(1))
    return "（未解析到汇总行）"


def broken_build(output: str) -> str:
    """A mutation that does not even build proves nothing about the tests.

    ``node --test`` reports every test as failed when the module (or the suite's
    ``before``) throws, which *looks* like a catch. It is not: the suite never
    got to assert anything. TypeScript is the case that matters here -- a
    mutation written as ``if (false)`` disables control-flow narrowing and turns
    a working line into a type error, so such a mutation must be rewritten rather
    than counted.

    The compile error itself is *not* in the captured output: the suite shells
    out to ``tsc`` with ``stdio: 'pipe'``, so all that surfaces is node's own
    ``Command failed: npx tsc ...``. Checking only for ``error TS`` therefore
    misses exactly the case this function exists for -- it silently counts a
    broken build as a caught mutation (this happened: task 9.4, M1/M8).
    """
    for pattern in (r"error TS\d+", r"SyntaxError", r"Cannot find module",
                    r"Command failed: npx tsc", r"Command failed:"):
        if re.search(pattern, output):
            return pattern
    return ""


def main() -> int:
    failures: list = []
    for mutation in MUTATIONS:
        path = mutation["file"]
        original = read(path)
        if mutation["old"] not in original:
            print(f"[skip] {mutation['name']}: 锚点未命中（源码已变）")
            failures.append(mutation["name"])
            continue
        write(path, original.replace(mutation["old"], mutation["new"], 1))
        try:
            output = run_suite(mutation["suite"])
        finally:
            write(path, original)
        assert read(path) == original, f"还原失败：{path}"
        caught = failed_node_tests(output)
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        print(f"\n### {mutation['name']}")
        print(f"  套件：{mutation['suite']}")
        print(f"  汇总：{summarize(output)}")
        print(f"  预期失败命中：{hits}")
        print(f"  实际失败：{caught}")
        broken = broken_build(output)
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
    print("监视器与刷新恢复两端共 %d 类实现走样都被对应用例判为失败，且还原后源文件与"
          "原文一致。" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
