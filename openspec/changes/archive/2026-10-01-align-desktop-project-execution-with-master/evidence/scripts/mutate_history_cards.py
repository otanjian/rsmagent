#!/usr/bin/env python3
"""变异检查：历史卡片归位（任务 9.6）的断言真的会因为实现被改坏而失败。

第 9.6 条要回答一个只有两端合起来才成立的问题：**一张从历史里重建出来的本机
产出卡片，属于哪个项目？**服务器端知道那条记录写了哪个绝对路径，但"这个路径
现在归哪个授权"只有设备/注册表知道；前端知道卡片里带着哪个项目，但"路径该按
哪个项目解析"是本地执行的判断。

于是每一处走样都是一条**看起来更省事**的实现：

* 从"现在打开的项目"推来源（切过目录就错，或把同名文件当同一个）；
* 文件不在就丢掉卡片（历史里那次产出就此消失）；
* 只认会话目录，不查实时注册（授权撤销后又从"最近使用"里复活）；
* 前缀比对不加分隔符、不做 realpath、不按用户/租户隔离；
* 最外层项目赢（嵌套项目归错）；
* 前端把卡片所属项目丢掉，或对已标记"不存在"的卡片照样去解析。

跑同一批用例，要求出现预期失败后立刻还原源文件；任何一项「改坏了却全绿」都会
让脚本以非零码退出。注意：等长改动的 `mtime+size` 可能与旧 `.pyc` 校验相符，
从而读到旧字节码并伪造「未被发现」的结论，所以每次跑之前清掉被改模块的
`__pycache__`，并让子进程不写字节码。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
RUNTIME = os.path.join(REPO, "channel", "web", "fork", "runtime.py")
LOCAL = os.path.join(REPO, "agent", "desktop_local", "__init__.py")
PROJECT_SOURCE = os.path.join(
    REPO, "channel", "web", "static", "js", "fork", "project-source.js")
WORKSPACE = os.path.join(REPO, "channel", "web", "static", "js", "workspace.js")

ARTIFACT_SUITE = ("py", "tests/test_desktop_artifact_source.py")
ROOT_SUITE = ("py", "tests/test_desktop_local_root.py")
PANEL_SUITE = ("node", "tests/test_desktop_project_native_actions.cjs")
PYTEST = [".venv/bin/python", "-m", "pytest", "-q", "-p", "no:randomly"]

MUTATIONS = [
    # -- 服务器端：卡片属于哪次授权 --------------------------------------
    {
        "name": "H1 只认会话当前目录，不查实时注册（切过目录的老卡片直接消失）",
        "file": RUNTIME,
        "suites": [ARTIFACT_SUITE],
        "old": (
            "            entry = _project_holding(path, identity) if os.path.isabs(path) else None\n"
        ),
        "new": "            entry = None\n",
        "expect": [
            "test_a_local_history_card_has_no_server_url",
            "test_a_card_names_the_project_that_produced_it_not_the_one_open_now",
        ],
    },
    {
        "name": "H2 卡片身份取自「现在打开的项目」，而不是真正持有该文件的注册",
        "file": RUNTIME,
        "suites": [ARTIFACT_SUITE],
        "old": (
            "    try:\n"
            "        target = desktop_target(\n"
            "            device_id=entry.device_id, workspace_id=entry.workspace_id,\n"
            "            binding_id=entry.binding_id, grant_version=entry.grant_version,\n"
            "            project_mode=entry.project_mode,\n"
            "        )\n"
        ),
        "new": (
            "    try:\n"
            "        from agent.workspace.project_store import get_execution_target\n"
            "\n"
            "        target = get_execution_target()\n"
        ),
        "expect": [
            "test_a_card_names_the_project_that_produced_it_not_the_one_open_now",
        ],
    },
    {
        "name": "H3 文件不在就丢掉卡片（历史里那次产出被抹掉）",
        "file": RUNTIME,
        "suites": [ARTIFACT_SUITE],
        "old": (
            "                if origin is not None and not os.path.isfile(path):\n"
            "                    missing = _missing_local_card(path, step_root, origin)\n"
            "                    if missing is not None and missing[\"relative_path\"] not in seen:\n"
            "                        seen.add(missing[\"relative_path\"])\n"
            "                        out.append(missing)\n"
            "                    continue\n"
        ),
        "new": (
            "                if origin is not None and not os.path.isfile(path):\n"
            "                    continue\n"
        ),
        "expect": ["test_a_deleted_file_keeps_its_card_and_says_it_is_gone"],
    },
    {
        "name": "H4 卡片不再带上「能不能在这里解析」（消失的卡片看起来还能打开）",
        "file": RUNTIME,
        "suites": [ARTIFACT_SUITE],
        "old": '        "resolution": origin.get("resolution") or "ok",\n',
        "new": '        "resolution": "ok",\n',
        "expect": ["test_a_deleted_file_keeps_its_card_and_says_it_is_gone"],
    },
    # -- 设备端：哪个实时注册持有这个路径 ---------------------------------
    {
        "name": "H5 外层项目赢（嵌套项目里的文件归给外层）",
        "file": LOCAL,
        "suites": [ROOT_SUITE],
        "old": (
            "            if best is None or len(real_root) > len(os.path.realpath(best.absolute_path)):\n"
            "                best = entry\n"
        ),
        "new": (
            "            if best is None:\n"
            "                best = entry\n"
        ),
        "expect": ["test_the_innermost_project_wins"],
    },
    {
        "name": "H6 前缀比对不加分隔符（/p/project-ab 被当成 /p/project 里的文件）",
        "file": LOCAL,
        "suites": [ROOT_SUITE],
        "old": (
            "            if real_path != real_root and not real_path.startswith(real_root + os.sep):\n"
            "                continue\n"
        ),
        "new": (
            "            if real_path != real_root and not real_path.startswith(real_root):\n"
            "                continue\n"
        ),
        "expect": ["test_a_sibling_with_a_shared_prefix_is_not_inside"],
    },
    {
        "name": "H7 不按用户/租户过滤（同机另一个账号的项目也算我的）",
        "file": LOCAL,
        "suites": [ROOT_SUITE],
        "old": (
            "            if entry.user_id != user_id or entry.tenant_id != tenant_id:\n"
            "                continue\n"
        ),
        "new": (
            "            if entry.user_id != user_id and entry.tenant_id != tenant_id:\n"
            "                continue\n"
        ),
        "expect": [
            "test_another_users_registration_is_not_usable",
            "test_another_tenants_registration_is_not_usable",
        ],
    },
    {
        "name": "H8 只做字符串比对，不解析符号链接（链接把项目外文件伪装成项目内）",
        "file": LOCAL,
        "suites": [ROOT_SUITE],
        "old": (
            "            if not real_root:\n"
            "                continue\n"
            "            if real_path != real_root and not real_path.startswith(real_root + os.sep):\n"
            "                continue\n"
        ),
        "new": (
            "            real_root = os.path.normpath(entry.absolute_path)\n"
            "            real_path = os.path.normpath(os.path.expanduser(str(absolute_path or \"\")))\n"
            "            if real_path != real_root and not real_path.startswith(real_root + os.sep):\n"
            "                continue\n"
        ),
        "expect": ["test_a_link_out_of_the_project_does_not_hold_its_target"],
    },
    # -- 面板：卡片自己带着项目与可解析性 ---------------------------------
    {
        "name": "H9 面板不给老卡片带上所属项目（老路径按当前项目解析）",
        "file": WORKSPACE,
        "suites": [PANEL_SUITE],
        "old": (
            "function wsLocalCardScope(meta) {\n"
            "    return (meta && meta.workspace_id) ? { expectedWorkspaceId: String(meta.workspace_id) } : {};\n"
            "}\n"
        ),
        "new": (
            "function wsLocalCardScope(meta) {\n"
            "    return {};\n"
            "}\n"
        ),
        "expect": [
            "the panel hands the card's project to the adapter, and a live card nothing",
        ],
    },
    {
        "name": "H10 面板对服务器已记为「不存在」的卡片照样去解析（执行）",
        "file": WORKSPACE,
        "suites": [PANEL_SUITE],
        "old": (
            "    // a more confusing way to say the same thing.\n"
            "    if (wsLocalCardIsGone(meta)) return { ok: false, code: 'not_found', message: '' };\n"
        ),
        "new": (
            "    // a more confusing way to say the same thing.\n"
            "    if (wsLocalCardIsGone(meta) === 'never') return { ok: false, code: 'not_found', message: '' };\n"
        ),
        "expect": ["the panel refuses a card the server already reported as gone"],
    },
    {
        "name": "H10b 面板对服务器已记为「不存在」的卡片照样去预览",
        "file": WORKSPACE,
        "suites": [PANEL_SUITE],
        "old": (
            "    if (wsLocalCardIsGone(meta)) return { ok: false, code: 'not_found', message: '' };\n"
            "    return CowProjectSource.preview(wsEditTargetPath(meta), wsLocalCardScope(meta));\n"
        ),
        "new": (
            "    if (wsLocalCardIsGone(meta) === 'never') return { ok: false, code: 'not_found', message: '' };\n"
            "    return CowProjectSource.preview(wsEditTargetPath(meta), wsLocalCardScope(meta));\n"
        ),
        "expect": ["the panel refuses to preview a card the server reported as gone"],
    },
    {
        "name": "H11 卡片不再携带所属项目与可解析性（历史卡片和实时卡片长得一样）",
        "file": WORKSPACE,
        "suites": [PANEL_SUITE],
        "old": (
            "        workspace_id: meta.workspace_id || '',\n"
            "        resolution: meta.resolution || 'ok',\n"
        ),
        "new": (
            "        workspace_id: '',\n"
            "        resolution: 'ok',\n"
        ),
        "expect": ["a card carries the project and the resolution it was rebuilt with"],
    },
    {
        "name": "H12 「属于另一个项目」不再有独立说法（和普通失败混为一谈）",
        "file": WORKSPACE,
        "suites": [PANEL_SUITE],
        "old": (
            "    if (code === 'not_found') return t('ws_local_file_gone');\n"
            "    if (code === 'wrong_project') return t('ws_local_other_project');\n"
            "    if (code === 'changed') return `${t('ws_local_file_changed')}${suffix}`;\n"
        ),
        "new": (
            "    if (code === 'not_found') return t('ws_local_file_gone');\n"
            "    if (code === 'wrong_project') return t('ws_local_action_failed');\n"
            "    if (code === 'changed') return `${t('ws_local_file_changed')}${suffix}`;\n"
        ),
        "expect": ['"another project" is its own sentence, not a generic failure'],
    },
    {
        "name": "H12b 预览也不再说「属于另一个项目」",
        "file": WORKSPACE,
        "suites": [PANEL_SUITE],
        "old": (
            "    if (code === 'not_found') return t('ws_local_file_gone');\n"
            "    if (code === 'wrong_project') return t('ws_local_other_project');\n"
            "    if (code === 'stale_context' || code === 'grant_revoked') return t('ws_local_stale');\n"
        ),
        "new": (
            "    if (code === 'not_found') return t('ws_local_file_gone');\n"
            "    if (code === 'wrong_project') return t('ws_local_preview_failed');\n"
            "    if (code === 'stale_context' || code === 'grant_revoked') return t('ws_local_stale');\n"
        ),
        "expect": ['"another project" is its own sentence, not a generic failure'],
    },
    {
        "name": "H13 适配层丢掉卡片所属项目（新路径直接按当前项目解析）",
        "file": PROJECT_SOURCE,
        "suites": [PANEL_SUITE],
        "old": (
            "    var other = wrongProject(opts.expectedWorkspaceId);\n"
            "    if (other) return Promise.resolve(other);\n"
        ),
        "new": (
            "    var other = null;\n"
            "    if (other) return Promise.resolve(other);\n"
        ),
        "expect": ["an action on a file from another project is refused, not re-resolved"],
    },
    {
        "name": "H14 预览也丢掉卡片所属项目（预览会读到另一个项目的同名文件）",
        "file": PROJECT_SOURCE,
        "suites": [PANEL_SUITE],
        "old": (
            "    var other = wrongProject((options || {}).expectedWorkspaceId);\n"
            "    if (other) return Promise.resolve(other);\n"
        ),
        "new": (
            "    var other = null;\n"
            "    if (other) return Promise.resolve(other);\n"
        ),
        "expect": ["a preview of a file from another project is refused the same way"],
    },
]


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def clear_pycache(path: str) -> None:
    cache = os.path.join(os.path.dirname(path), "__pycache__")
    if os.path.isdir(cache):
        shutil.rmtree(cache, ignore_errors=True)


def failed_py_tests(output: str) -> list:
    """``FAILED``/``SUBFAILED`` lines, reduced to the bare test name.

    ``SUBFAILED(name=...)`` is followed by ``(`` and not whitespace, so an
    anchored ``FAILED\\s+`` would miss the one format that already bit this
    change; the class and module are stripped so a name matches either way.
    """
    names = re.findall(
        r"^(?:FAILED|SUBFAILED\([^)]*\))\s+\S*::(?:[\w.]+\.)?(\w+)",
        output, flags=re.M)
    return sorted(set(names))


def failed_node_tests(output: str) -> list:
    """``node --test`` marks a failing test with ``✖ <name> (<ms>)``.

    The marker is printed twice for a failure (once inline, once in the trailing
    "failing tests" list), so the set is what matters -- counting lines would
    double every hit.
    """
    return sorted(set(re.findall(r"^\u2716 (.+?) \(\d", output, flags=re.M)))


def run_suite(kind: str, suite: str) -> str:
    if kind == "node":
        proc = subprocess.run(
            ["node", "--test", suite], cwd=REPO,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return proc.stdout
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(PYTEST + [suite], cwd=REPO, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    return proc.stdout


def run_suites(suites) -> tuple:
    output = ""
    caught: list = []
    for kind, suite in suites:
        run = run_suite(kind, suite)
        output += run
        caught.extend(
            failed_node_tests(run) if kind == "node" else failed_py_tests(run))
    return output, sorted(set(caught))


def summarize(output: str) -> str:
    lines = re.findall(r"^\d+ (?:failed.*?passed|passed).*$", output, flags=re.M)
    if lines:
        return "；".join(lines)
    counts = re.search(r"^\u2139 fail (\d+)$", output, flags=re.M)
    passing = re.search(r"^\u2139 pass (\d+)$", output, flags=re.M)
    if counts and passing:
        return "%s failed, %s passed" % (counts.group(1), passing.group(1))
    return "（未解析到汇总行）"


def main() -> int:
    failures: list = []
    for mutation in MUTATIONS:
        path = mutation["file"]
        py = any(kind == "py" for kind, _suite in mutation["suites"])
        original = read(path)
        if mutation["old"] not in original:
            print(f"[skip] {mutation['name']}: 锚点未命中（源码已变）")
            failures.append(mutation["name"])
            continue
        write(path, original.replace(mutation["old"], mutation["new"], 1))
        if py:
            clear_pycache(path)
        try:
            output, caught = run_suites(mutation["suites"])
        finally:
            write(path, original)
            if py:
                clear_pycache(path)
        assert read(path) == original, f"还原失败：{path}"
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        print(f"\n### {mutation['name']}")
        print(f"  汇总：{summarize(output)}")
        print(f"  预期失败命中：{hits}")
        print(f"  实际失败：{caught}")
        if not hits:
            failures.append(mutation["name"])

    print("\n== 结论 ==")
    if failures:
        print("变异未被用例发现（用例太弱或锚点失效）：")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("两端共 %d 类实现走样都被对应用例判为失败，且还原后源文件与原文一致。"
          % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
