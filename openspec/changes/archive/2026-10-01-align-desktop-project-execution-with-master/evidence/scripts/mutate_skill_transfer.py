#!/usr/bin/env python3
"""变异检查：8.9 技能包到设备与摘要校验，用例真的能发现实现走样。

任务 8.9 有两端，各自都有一个"看起来更省事"的写法，而这个任务存在的理由正是
它们被拒绝了：

* **服务端**按命令**已授权的集合**发字节 —— 不是"服务端有的版本都能拿"，
  也不是"摘要对得上就发"；
* **设备端**按声明的摘要校验后才挂载 —— 不是"服务端说了是这个版本就是"，
  也不是"声明的技能版本只记日志不校验"。

每一项都把实现改成上面那种更省事的写法，跑同一个套件，要求出现预期失败后立刻
还原源文件。任何一项「改坏了却全绿」都会让脚本以非零码退出。

锚点落在 TypeScript 源码上时，用例的 `before()` 会自己 `tsc` 重建（源码 mtime
比 dist 新即触发），所以不需要本脚本额外构建；脚本开头与结尾各跑一次未变异的
用例，作为「基线是绿的」与「还原后 dist 由正确源码重建」两件事的证据。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHANGE = os.path.abspath(os.path.join(HERE, "..", ".."))
REPO = os.path.abspath(os.path.join(CHANGE, "..", "..", ".."))

BROKER = os.path.join(REPO, "integrations", "desktop", "execution_broker.py")
TRANSFER = os.path.join(REPO, "desktop", "src", "main", "project-execution",
                        "skill-transfer.ts")
EXECUTION = os.path.join(REPO, "desktop", "src", "main", "project-execution",
                         "device-execution.ts")
CACHE = os.path.join(REPO, "desktop", "src", "main", "project-execution",
                     "skill-cache.ts")

PY_DELIVERY = "tests/test_desktop_remote_skill_delivery.py"
NODE_TRANSFER = "tests/test_desktop_skill_transfer.cjs"
NODE_EXECUTION = "tests/test_desktop_device_execution.cjs"

#: 每个套件跑一次需要的参数：pytest 走 `.venv/bin/python -m pytest`，其余走
#: `node --test`。
SUITES = {
    PY_DELIVERY: {"runner": "py"},
    NODE_TRANSFER: {"runner": "node"},
    NODE_EXECUTION: {"runner": "node"},
}

MUTATIONS = [
    {
        "name": "P1 服务端不比对授权集合（服务端有的版本都能拿）",
        "runner": "py", "file": BROKER, "suite": PY_DELIVERY,
        "old": '        if {"skill_id": skill_id, "digest": digest} not in authorized:\n',
        "new": "        if False:\n",
        # 命中这两条，而不是名称相近的那条"请求的摘要服务器造不出来"：只有
        # **真实的当前版本**、但不在命令授权集合里的请求能穿过下面的摘要检查，
        # 所以这一层只有它们能证伪。
        "expect": [
            "test_a_package_this_command_was_never_authorized_with_cannot_be_pulled",
            "test_a_package_outside_a_non_empty_set_is_refused_too",
        ],
    },
    {
        "name": "P2 服务端不重算摘要（改动过的技能按旧版本名发出去）",
        "runner": "py", "file": BROKER, "suite": PY_DELIVERY,
        "old": "            if manifest.digest != digest:\n",
        "new": "            if False:\n",
        "expect": ["test_a_version_the_server_would_not_ship_is_refused_by_name"],
    },
    {
        "name": "P3 服务端重新接受空声明（运行无法被要求使用某个版本）",
        "runner": "py", "file": BROKER, "suite": PY_DELIVERY,
        "old": "        if resources != authorized:\n",
        "new": "        if resources and resources != authorized:\n",
        "expect": ["test_declaring_nothing_is_refused_when_the_run_has_a_set"],
    },
    {
        "name": "N1 设备端接受对不上声明的回答（装成运行要的版本）",
        "runner": "node", "file": TRANSFER, "suite": NODE_TRANSFER,
        "old": "  if (String(payload.digest || '') !== request.digest) {\n",
        "new": "  if (false) {\n",
        "expect": ["an answer for a different digest is refused, not installed"],
    },
    {
        "name": "N2 设备端不重算包摘要就发布（服务器说谎也照装）",
        "runner": "node", "file": CACHE, "suite": NODE_TRANSFER,
        "old": "    if (actual !== expected) {\n",
        "new": "    if (false) {\n",
        "expect": ["a package whose bytes do not match the declared digest is refused"],
    },
    {
        "name": "N3 设备端不校验声明的技能版本就执行",
        "runner": "node", "file": EXECUTION, "suite": NODE_EXECUTION,
        "old": "        const skillRefusal = this.verifySkills(frame)\n",
        "new": "        const skillRefusal = undefined\n",
        "expect": ["a frame whose skills are not here is refused before anything runs"],
    },
]


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def clear_pycache(path: str) -> None:
    """A mutated ``.pyc`` must not outlive the mutated source.

    ``PYTHONDONTWRITEBYTECODE`` below stops new ones being written; this removes
    anything a previous run left behind, so a later suite cannot import a
    compiled copy of code that is no longer on disk.
    """
    cache = os.path.join(os.path.dirname(path), "__pycache__")
    if os.path.isdir(cache):
        shutil.rmtree(cache, ignore_errors=True)


def failed_tests(output: str) -> list:
    """Name every test the runner marked as failing, in either runner's format.

    pytest (``-q``) names failures as ``FAILED path::Class::test``; a failure
    inside ``assertRaises`` + ``subTest`` surfaces as
    ``SUBFAILED(name='...') path::Class::test``, which an anchored ``FAILED\\s+``
    misses. ``node --test`` prints ``✖ <name> (<ms>)`` and repeats the names
    under ``failing tests:``. A crash (a build error inside ``before()``) yields
    nothing on purpose: the caller treats "no expected name" as a failure, so a
    mutation that breaks the build cannot be mistaken for a caught one.
    """
    names = []
    for raw in re.findall(r"^(?:SUB)?FAILED\b[^\n]*", output, flags=re.M):
        names.append(raw.split("::")[-1].strip())
    for raw in re.findall(
            r"^\s*[\u2716\u00d7]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?ms\))?\s*$",
            output, flags=re.M):
        name = raw.strip()
        # The reporter's own heading for the list it is about to repeat; it is
        # not a test, and leaving it in would read as an unnamed failure.
        if name.rstrip(":").lower() == "failing tests":
            continue
        names.append(name)
    return sorted({name for name in names if name})


def summary(output: str) -> str:
    found = re.search(r"^\u2139 fail \d+.*$", output, flags=re.M)
    if found:
        return found.group(0).strip()
    found = re.search(r"^\d+ (?:failed|passed).*$", output, flags=re.M)
    if found:
        return found.group(0).strip()
    return "(未解析到汇总行)"


def run_suite(suite: str) -> str:
    if SUITES[suite]["runner"] == "node":
        # The suites rebuild ``dist`` from source mtime, so a mutated ``.ts`` is
        # compiled before it is measured -- the mutation is of the real artefact.
        proc = subprocess.run(
            ["node", "--test", suite], cwd=REPO, env=dict(os.environ),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return proc.stdout
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [os.path.join(REPO, ".venv", "bin", "python"), "-m", "pytest",
         suite, "-q", "-p", "no:randomly"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    return proc.stdout


def main() -> int:
    baselines = {}
    for suite in SUITES:
        output = run_suite(suite)
        baselines[suite] = failed_tests(output)
        print(f"基线 {suite}：{summary(output)}")
    dirty = {suite: names for suite, names in baselines.items() if names}
    if dirty:
        print("基线不绿，先修基线再谈变异：")
        for suite, names in dirty.items():
            print(f"  {suite}: {names}")
        return 1

    missed: list = []
    for mutation in MUTATIONS:
        path = mutation["file"]
        original = read(path)
        if mutation["old"] not in original:
            print(f"[skip] {mutation['name']}: 锚点未命中（源码已变）")
            missed.append(mutation["name"])
            continue
        write(path, original.replace(mutation["old"], mutation["new"], 1))
        if mutation["runner"] == "py":
            clear_pycache(path)
        try:
            output = run_suite(mutation["suite"])
        finally:
            write(path, original)
            if mutation["runner"] == "py":
                clear_pycache(path)
        assert read(path) == original, f"还原失败：{path}"

        caught = failed_tests(output)
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        print(f"\n### {mutation['name']}")
        print(f"  汇总：{summary(output)}")
        print(f"  预期失败命中：{hits}")
        print(f"  实际失败：{caught}")
        if not hits:
            missed.append(mutation["name"])

    restored: list = []
    for suite in SUITES:
        output = run_suite(suite)
        names = failed_tests(output)
        print(f"还原后 {suite}：{summary(output)}")
        restored.extend(f"{suite}: {name}" for name in names)

    print("\n== 结论 ==")
    if missed:
        print("变异未被用例发现（用例太弱或锚点失效）：")
        for name in missed:
            print(f"  - {name}")
        return 1
    if restored:
        print("还原后的源码仍然失败，脚本没有把仓库留在可用状态：")
        for name in restored:
            print(f"  - {name}")
        return 1
    print("%d 类实现走样都被对应用例判为失败，且还原后源文件与用例都回到基线。"
          % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
