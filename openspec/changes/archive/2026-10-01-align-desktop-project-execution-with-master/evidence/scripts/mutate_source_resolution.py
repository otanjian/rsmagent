#!/usr/bin/env python3
"""变异检查：来源解析测试真的会因为实现被改坏而失败（任务 3.6）。

每一项都把源码改成一条**看起来更省事**的实现（回退服务器目录、把绝对路径
当相对路径、无条件落地成上传、忽略授权版本），跑同一批用例，要求出现预期
失败后立刻还原源文件。任何一项「改坏了却全绿」都会让脚本以非零码退出。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
RESOLVER = os.path.join(REPO, "agent", "desktop_local", "source_resolver.py")
RUN_CONTEXT = os.path.join(REPO, "agent", "desktop_local", "run_context.py")
REGISTRY = os.path.join(REPO, "agent", "desktop_local", "__init__.py")

SUITE = "tests/test_desktop_source_resolver.py"
EXTRA_SUITE = "tests/test_desktop_run_context.py"
PYTEST = [".venv/bin/python", "-m", "pytest", "-q", "-p", "no:randomly"]

MUTATIONS = [
    {
        "name": "M1 本机项目解析不出来时回退服务器目录",
        "file": RESOLVER,
        "old": (
            "    root, refusal = resolve_target_root(target, identity)\n"
            "    return Source(kind=DESKTOP, root=root, target=target, refusal=refusal)\n"
        ),
        "new": (
            "    root, refusal = resolve_target_root(target, identity)\n"
            "    if root is None:\n"
            "        root, refusal = server_root, None\n"
            "    return Source(kind=DESKTOP, root=root, target=target, refusal=refusal)\n"
        ),
        "expect": [
            "test_a_revoked_project_is_a_refusal_not_the_server_root",
            "test_a_session_whose_grant_was_revoked_is_refused",
            "test_a_revoked_local_session_refuses_instead_of_listing_the_server",
        ],
    },
    {
        "name": "M2 绝对路径当成项目内相对路径解析",
        "file": RESOLVER,
        "old": (
            "        if os.path.isabs(text) or text.startswith((\"\\\\\", \"~\")):\n"
        ),
        "new": (
            "        if os.path.isabs(text) or text.startswith((\"\\\\\", \"~\")):\n"
            "            text = text.lstrip(\"/\\\\\\\\~\").strip()\n"
            "        if False:\n"
        ),
        "expect": [
            "test_an_absolute_path_is_refused_and_never_parsed",
            "test_a_server_path_refuses_the_call_before_the_tool_runs",
        ],
    },
    {
        "name": "M3 服务器输入不清示直接落地成项目内文件（等于无条件上传）",
        "file": RESOLVER,
        "old": (
            "            land = _land(source, raw, transfer)\n"
        ),
        "new": (
            "            land = transfer(raw) if transfer else os.path.join(\n"
            "                root, os.path.basename(raw))\n"
            "            if False:\n"
            "                land = _land(source, raw, transfer)\n"
        ),
        "expect": [
            "test_a_server_path_refuses_the_call_before_the_tool_runs",
            "test_a_refused_source_refuses_the_call",
        ],
    },
    {
        "name": "M4 重验时忽略授权版本（旧目标继续可用）",
        "file": REGISTRY,
        "old": (
            "        if entry.grant_version != int(grant_version or 0):\n"
            "            return None\n"
        ),
        "new": (
            "        if False:\n"
            "            return None\n"
        ),
        "expect": ["test_a_repick_invalidates_the_run"],
    },
    {
        "name": "M5 运行中重选目录后静默跟随新目录",
        "file": RUN_CONTEXT,
        "old": (
            "    if frozen:\n"
            "        if os.path.realpath(entry.absolute_path) != os.path.realpath(frozen):\n"
            "            # The frozen cwd and the live authorization disagree: the session was\n"
            "            # re-pointed while this run streamed. Refuse rather than pick one.\n"
            "            return None, REFUSAL_UNAVAILABLE\n"
            "        root = frozen\n"
            "    else:\n"
            "        root = entry.absolute_path\n"
        ),
        "new": "    root = frozen or entry.absolute_path\n",
        "expect": ["test_a_moved_directory_does_not_silently_follow"],
    },
]


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def failed_tests(output: str) -> list:
    return sorted(set(re.findall(r"^FAILED (\S+)", output, flags=re.M)))


def run_suite() -> str:
    proc = subprocess.run(
        PYTEST + [SUITE, EXTRA_SUITE], cwd=REPO,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.stdout


def main() -> int:
    failures: list = []
    for mutation in MUTATIONS:
        path = mutation["file"]
        original = read(path)
        if mutation["old"] not in original:
            print(f"[skip] {mutation['name']}: 锚点未命中（源码已变）")
            failures.append(mutation["name"])
            continue
        mutated = original.replace(mutation["old"], mutation["new"], 1)
        write(path, mutated)
        try:
            output = run_suite()
        finally:
            write(path, original)
        assert read(path) == original, f"还原失败：{path}"
        caught = failed_tests(output)
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        summary = re.search(r"^\d+ (?:failed.*?passed|passed).*$", output, flags=re.M)
        print(f"\n### {mutation['name']}")
        print(f"  汇总：{summary.group(0) if summary else '（未解析到汇总行）'}")
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
    print("五类实现走样都被对应用例判为失败，且还原后源文件与原文一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
