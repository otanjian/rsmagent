#!/usr/bin/env python3
"""变异检查：迁移原子性、开关交集与 A32 回退（任务 11.1—11.4）的断言真的有效。

第 11 组要证明的是「升级与回退不会造成损害」，而这类结论最容易写成**恰好都
通过**的空断言。每一处走样都是一种**更省事、看起来更合理**的写法：

* 让迁移体继续用原生 `executescript`（DDL 落到自己的事务里，于是进程在
  「体已提交、标记未写」之间被杀之后，库再也打不开）；
* 去掉 `ADD COLUMN` 的存在性判断（被旧版本弄成半应用的库无法自愈）；
* 把 `availability()` 的原因判序调换（把「还没验收」说成「被开关关掉」）；
* 让 `execution_state` 直接看开关（开关一开就宣称能力可用）；
* 拆掉 broker 的合成闸门（回退时新调用照样执行）；
* 让 `status()` 不认 `outcome_unknown`（未知被洗成普通失败，下一位读者就会重跑）；
* 让委派不再看开关（关闭后仍然派发）；
* 让「解析不到本机目录」退回服务器目录（这正是需求禁止的改投服务端）；
* 注册表不比对 grant 版本（重选目录后旧授权复活）；
* 用朴素 `split(';')` 切分脚本（`CREATE TRIGGER ... BEGIN ... END;` 被切两半）。

任何一项「改坏了却全绿」都会让脚本以非零码退出。等长改动会命中旧 `.pyc`，
所以每次跑之前清掉被改模块的 `__pycache__`，并让子进程不写字节码。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
STORE = os.path.join(REPO, "auth", "store.py")
MATRIX = os.path.join(REPO, "auth", "capability_matrix.py")
CAPABILITY = os.path.join(REPO, "integrations", "desktop", "execution_capability.py")
HANDLERS = os.path.join(REPO, "channel", "web", "fork", "handlers", "desktop.py")
BROKER = os.path.join(REPO, "integrations", "desktop", "execution_broker.py")
MODE = os.path.join(REPO, "agent", "desktop_remote", "mode.py")
RUN_CONTEXT = os.path.join(REPO, "agent", "desktop_local", "run_context.py")
LOCAL = os.path.join(REPO, "agent", "desktop_local", "__init__.py")

DRILL_SUITE = ("py", "tests/test_desktop_project_migration_drill.py")
GATES_SUITE = ("py", "tests/test_desktop_release_gates.py")
CONTRACT_SUITE = ("py", "tests/test_desktop_execution_v2_contract.py")
BROKER_SUITE = ("py", "tests/test_desktop_execution_broker.py")
ROOT_SUITE = ("py", "tests/test_desktop_local_root.py")
PYTEST = [".venv/bin/python", "-m", "pytest", "-q", "-p", "no:randomly"]

MUTATIONS = [
    # -- 11.1 迁移体与标记必须同一个事务 --------------------------------
    {
        "name": "R1 迁移体退回原生 executescript（隐式提交，标记与 DDL 分属两个事务）",
        "note": ("迁移 1—40 都是以 executescript 写的，所以走样后每条都会留下"
                 "「体已提交、标记未写」的半状态；迁移 41—44 用 con.execute 写，"
                 "本身就在框架事务里，故不受这条影响（R3 覆盖它们）。"),
        "file": STORE,
        "suites": [DRILL_SUITE],
        "old": (
            "    def executescript(self, script: str) -> None:\n"
            "        for statement in _split_sql_script(str(script)):\n"
            "            self._con.execute(statement)\n"
        ),
        "new": (
            "    def executescript(self, script: str) -> None:\n"
            "        self._con.executescript(str(script))\n"
        ),
        "expect": [
            "test_a_body_that_fails_leaves_the_store_at_the_previous_head",
        ],
    },
    {
        "name": "R2 脚本用朴素 split(';') 切分（触发器 BEGIN…END 被切断）",
        "file": STORE,
        "suites": [DRILL_SUITE],
        "old": (
            "    statements: List[str] = []\n"
            "    buffer = \"\"\n"
            "    for line in script.splitlines(True):\n"
            "        buffer += line\n"
            "        if sqlite3.complete_statement(buffer):\n"
        ),
        "new": (
            "    statements: List[str] = []\n"
            "    buffer = \"\"\n"
            "    for line in script.split(';'):\n"
            "        buffer = line\n"
            "        if True:\n"
        ),
        "expect": ["test_fresh_store_applies_every_migration_once"],
    },
    {
        "name": "R3 ADD COLUMN 不再判断存在（被旧版本弄成半应用的库无法自愈）",
        "file": STORE,
        "suites": [DRILL_SUITE],
        "old": (
            "    existing = {row[1] for row in con.execute(\"PRAGMA table_info(%s)\" % table)}\n"
            "    if column not in existing:\n"
            "        con.execute(\"ALTER TABLE %s ADD COLUMN %s %s\" % (table, column, declaration))\n"
        ),
        "new": (
            "    existing = set()\n"
            "    if True:\n"
            "        con.execute(\"ALTER TABLE %s ADD COLUMN %s %s\" % (table, column, declaration))\n"
        ),
        "expect": ["test_a_store_left_half_applied_by_an_older_build_still_opens"],
    },
    # -- 11.2 报告只能是「已实现 × 已验收 × 部署开关」的交集 -------------
    {
        "name": "R4 原因判序调换（还没验收被说成被开关关掉）",
        "file": MATRIX,
        "suites": [GATES_SUITE, CONTRACT_SUITE],
        "old": (
            "    if not spec.implemented:\n"
            "        reason = \"not_implemented\"\n"
            "    elif not spec.accepted:\n"
            "        reason = \"not_accepted\"\n"
            "    elif not configured:\n"
            "        reason = \"disabled_by_deployment\"\n"
        ),
        "new": (
            "    if not spec.implemented:\n"
            "        reason = \"not_implemented\"\n"
            "    elif not configured:\n"
            "        reason = \"disabled_by_deployment\"\n"
            "    elif not spec.accepted:\n"
            "        reason = \"not_accepted\"\n"
        ),
        "expect": ["test_the_reason_names_the_first_missing_condition"],
    },
    {
        "name": "R5 合成块只看开关（开关一开就宣称能力可用，绕过验收）",
        "file": CAPABILITY,
        "suites": [GATES_SUITE, CONTRACT_SUITE],
        "old": "    available = bool(files.get(\"available\")) and bool(runnable)\n",
        "new": "    available = bool(files.get(\"configured\")) and bool(runnable)\n",
        "expect": [
            "test_a_switch_flip_alone_never_opens_the_composed_block",
            "test_the_default_deployment_reports_not_accepted",
        ],
    },
    # -- 11.4 A32：关闭开关后的行为 --------------------------------------
    {
        "name": "R6 拆掉 broker 的合成闸门（回退时新调用照样进授权层）",
        "file": HANDLERS,
        "suites": [BROKER_SUITE],
        "old": (
            "    if not state.get(\"available\"):\n"
            "        return _error(\n"
            "            \"project execution is not available (%s)\" % state.get(\"reason\"),\n"
            "            503, \"feature_unavailable\")\n"
        ),
        "new": (
            "    if False:\n"
            "        return _error(\n"
            "            \"project execution is not available (%s)\" % state.get(\"reason\"),\n"
            "            503, \"feature_unavailable\")\n"
        ),
        "expect": ["test_closing_the_switch_stops_new_calls"],
    },
    {
        "name": "R7 status 不认 outcome_unknown（未知被洗成普通失败）",
        "file": BROKER,
        "suites": [BROKER_SUITE],
        "old": "        unknown = command.get(\"phase\") == \"outcome_unknown\"\n",
        "new": "        unknown = False\n",
        "expect": [
            "test_status_keeps_an_unknown_outcome_unknown",
            "test_the_unknown_row_is_never_auto_rerun",
        ],
    },
    {
        "name": "R8 委派不再看开关（关闭后仍然派发到设备）",
        "file": MODE,
        "suites": [BROKER_SUITE],
        "old": "    return delegation_enabled()\n",
        "new": "    return True\n",
        "expect": ["test_the_rollback_never_routes_the_work_to_the_server"],
    },
    {
        "name": "R9 解析不到本机目录就退回服务器目录（需求禁止的改投服务端）",
        "file": RUN_CONTEXT,
        "suites": [BROKER_SUITE, GATES_SUITE],
        "old": (
            "    frozen = getattr(identity, \"execution_cwd\", None)\n"
            "    if not frozen:\n"
            "        # Resolved at entry and it did not resolve: the trusted registry had no\n"
            "        # entry for this authorization, or the directory was already gone.\n"
            "        return None, REFUSAL_UNAVAILABLE\n"
        ),
        "new": (
            "    frozen = getattr(identity, \"execution_cwd\", None)\n"
            "    if not frozen:\n"
            "        return \".\", None\n"
        ),
        "expect": [
            "test_the_rollback_never_routes_the_work_to_the_server",
            "test_a_legacy_target_gets_no_delegation_and_no_local_directory",
        ],
    },
    # -- 11.3 旧授权不静默扩权 -------------------------------------------
    {
        "name": "R10 注册表不比对 grant 版本（重选目录后旧授权复活）",
        "file": LOCAL,
        "suites": [ROOT_SUITE],
        "old": (
            "        if entry.grant_version != int(grant_version or 0):\n"
            "            return None\n"
        ),
        "new": (
            "        if False:\n"
            "            return None\n"
        ),
        "expect": ["test_version_bump_invalidates_the_old_root"],
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

    ``SUBFAILED(version=1) path::Class::name`` is how a ``subTest`` failure is
    reported, so the class separator is ``::`` and not ``.`` -- a pattern that
    only allowed ``module.name`` silently dropped every subtest failure, which
    looked exactly like "the mutation was not caught".
    """
    names = re.findall(
        r"^(?:FAILED|SUBFAILED\([^)]*\))\s+\S*::(?:[\w.]+::)?(\w+)",
        output, flags=re.M)
    return sorted(set(names))


def run_suite(suite: str) -> str:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(PYTEST + [suite], cwd=REPO, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    return proc.stdout


def run_suites(suites) -> tuple:
    output = ""
    caught: list = []
    for _kind, suite in suites:
        run = run_suite(suite)
        output += run
        caught.extend(failed_py_tests(run))
    return output, sorted(set(caught))


def summarize(output: str) -> str:
    lines = re.findall(r"^\d+ (?:failed.*?passed|passed).*$", output, flags=re.M)
    if lines:
        return "；".join(lines)
    return "（未解析到汇总行）"


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
        clear_pycache(path)
        try:
            output, caught = run_suites(mutation["suites"])
        finally:
            write(path, original)
            clear_pycache(path)
        assert read(path) == original, f"还原失败：{path}"
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        print(f"\n### {mutation['name']}")
        if mutation.get("note"):
            print(f"  说明：{mutation['note']}")
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
    print("共 %d 类实现走样都被对应用例判为失败，且还原后源文件与原文一致。"
          % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
