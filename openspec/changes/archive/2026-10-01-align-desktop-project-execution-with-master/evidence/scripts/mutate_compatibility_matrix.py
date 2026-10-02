#!/usr/bin/env python3
"""变异检查：A29 混合版本矩阵的断言真的有效（任务 10.4、11.3）。

A29 要证明的是「四种组合下，v1 一如既往地工作，且没有任何一种组合把
『新通道不可用』悄悄变成『那就走老路』」。这类结论最容易写成**恰好都通过**
的空断言：断言与实现同源、或者断言只检查自己刚设进去的值。

于是每一处走样都是一种**更省事、看起来更合理**的写法：

* 旧服务器（payload 里没有 `project_execution` 块）被当成"可用"——
  这正是 A29 存在的理由：absent ≠ refused，而两者都不是改走 v1 的理由；
* 协议主版本不匹配时不再拒绝（"版本不同但先试试"）；
* 服务端把这块声明成 `required: True`（于是老客户端被迫面对它读不懂的协议）；
* 判定原因时把 `not_accepted` 排在 `not_implemented` 之前（把"代码还没有"说成"还没验收"，
  运维会去动自己的开关）；
* 把写工具加进 **v1** 的 `commands.ops`——"新通道不可用就退回老通道"之所以被禁止，
  正是因为老通道只承诺过**读**；一旦它也能写，"不可用"就变成了"用老方式执行"；
* 只读目标（`readonly-input`）也照常委派——开关决定**能不能**委派，
  绝不决定**授了什么权**。

任何一项「改坏了却全绿」都会让脚本以非零码退出。等长改动会命中旧 `.pyc`，
所以每次跑之前清掉被改模块的 `__pycache__`，并让子进程不写字节码。
`contracts/desktop/v1.json` 是数据不是模块（没有 `__pycache__`），但同样先读原文、
跑完还原，且还原后逐字节比对。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
V2 = os.path.join(REPO, "auth", "desktop_contracts_v2.py")
MATRIX = os.path.join(REPO, "auth", "capability_matrix.py")
HANDLER = os.path.join(REPO, "channel", "web", "fork", "handlers", "desktop.py")
MODE = os.path.join(REPO, "agent", "desktop_remote", "mode.py")
V1_JSON = os.path.join(REPO, "contracts", "desktop", "v1.json")

COMPAT_SUITE = ("py", "tests/test_desktop_compatibility_matrix.py")
CONTRACT_SUITE = ("py", "tests/test_desktop_execution_v2_contract.py")
GATES_SUITE = ("py", "tests/test_desktop_release_gates.py")
PYTEST = [".venv/bin/python", "-m", "pytest", "-q", "-p", "no:randomly"]

MUTATIONS = [
    # -- 第 1 行：旧服务器 ------------------------------------------------
    {
        "name": "C1 旧服务器（没有 project_execution 块）被判为可用",
        "note": ("payload 里没有这一块，说明对面根本没有 v2；报 available 会让新客户端"
                 "拿着一个不存在的入口去执行，而不是如实说 not_implemented。"),
        "file": V2,
        "suites": [COMPAT_SUITE, CONTRACT_SUITE],
        "old": (
            "    offered = server_protocols.get(PROTOCOL_NAME)\n"
            "    if not isinstance(offered, dict):\n"
            "        return False, \"not_implemented\"\n"
        ),
        "new": (
            "    offered = server_protocols.get(PROTOCOL_NAME)\n"
            "    if not isinstance(offered, dict):\n"
            "        return True, \"available\"\n"
        ),
        "expect": ["test_row_1_an_old_server_leaves_the_entry_point_unavailable"],
    },
    # -- 第 1/2 行之间：主版本不匹配 --------------------------------------
    {
        "name": "C2 协议主版本不匹配时不再拒绝（版本不同但先试试）",
        "note": ("分开一个主版本的全部意义就是拒绝：v2 的帧形状在 v1 上是未定义的，"
                 "放过去等于用旧解析器读新帧。"),
        "file": V2,
        "suites": [COMPAT_SUITE, CONTRACT_SUITE],
        "old": (
            "    if offered.get(\"major\") != PROTOCOL_MAJOR:\n"
            "        return False, \"protocol_incompatible\"\n"
        ),
        "new": (
            "    if offered.get(\"major\") != PROTOCOL_MAJOR:\n"
            "        return True, \"available\"\n"
        ),
        "expect": ["test_a_mismatched_major_is_a_protocol_refusal_not_a_fallback"],
    },
    # -- 第 2 行：老客户端 -------------------------------------------------
    {
        "name": "C3 服务端把新协议声明成 required（老客户端被迫面对读不懂的协议）",
        "note": ("声明成必选会让 v1 的协商器把它当成一个它不满足的前置条件："
                 "老客户端要么报错要么被迫解析——两者都是「升级服务端弄坏了老客户端」。"),
        "file": HANDLER,
        "suites": [COMPAT_SUITE],
        "old": (
            "            {\"major\": desktop_contracts_v2.PROTOCOL_MAJOR,\n"
            "             \"minor\": desktop_contracts_v2.PROTOCOL[\"minor\"], \"required\": False})\n"
        ),
        "new": (
            "            {\"major\": desktop_contracts_v2.PROTOCOL_MAJOR,\n"
            "             \"minor\": desktop_contracts_v2.PROTOCOL[\"minor\"], \"required\": True})\n"
        ),
        "expect": [
            "test_row_2_an_old_client_keeps_reading_v1_and_is_not_offered_v2",
        ],
    },
    # -- 第 4 行：原因判序 -------------------------------------------------
    {
        "name": "C4 把「还没验收」排在「还没实现」之前（运维会去动自己的开关）",
        "note": ("`not_implemented` 必须最先判：代码不存在时，改开关没有任何用，"
                 "而报成 not_accepted/disabled_by_deployment 会把人引向错误的自救方向。"),
        "file": MATRIX,
        "suites": [COMPAT_SUITE, GATES_SUITE],
        "old": (
            "    if not spec.implemented:\n"
            "        reason = \"not_implemented\"\n"
            "    elif not spec.accepted:\n"
            "        reason = \"not_accepted\"\n"
        ),
        "new": (
            "    if not spec.accepted:\n"
            "        reason = \"not_accepted\"\n"
            "    elif not spec.implemented:\n"
            "        reason = \"not_implemented\"\n"
        ),
        "expect": [
            "test_row_4_a_missing_implementation_is_blamed_first",
            "test_the_reason_names_the_first_missing_condition",
        ],
    },
    # -- 跨四行的硬性质：老通道只承诺过读 --------------------------------
    {
        "name": "C5 把写工具加进 v1 的 commands.ops（老通道也成了写通道）",
        "note": ("这是最危险的一种走样：一旦 v1 也能写，「新通道不可用」就有了退路，"
                 "而退回去执行的是一次**写**——正是一条只承诺过读的通道不该做的事。"),
        "file": V1_JSON,
        "suites": [COMPAT_SUITE, CONTRACT_SUITE],
        "old": (
            "      \"materialize\": { \"params\": [\"relative_path\", \"expected_version\"] },\n"
            "      \"inspect\": { \"params\": [\"processor_id\", \"source_ref\", \"options\"] }\n"
        ),
        "new": (
            "      \"materialize\": { \"params\": [\"relative_path\", \"expected_version\"] },\n"
            "      \"bash\": { \"params\": [\"command\", \"timeout_ms\"] },\n"
            "      \"inspect\": { \"params\": [\"processor_id\", \"source_ref\", \"options\"] }\n"
        ),
        "expect": ["test_a_client_never_falls_back_from_v2_to_v1"],
    },
    # -- 11.3：旧只读授权不得静默扩权 -------------------------------------
    {
        "name": "C6 只读目标照常委派（开关决定授了什么权）",
        "note": ("`readonly-input` 是文件引用，不是执行许可。绕过这一判，"
                 "开关一开就把用户从未授予的**写**递给设备。"),
        "file": MODE,
        "suites": [COMPAT_SUITE],
        "old": (
            "    if not getattr(target, \"allows_project_execution\", False):\n"
            "        # ``readonly-input`` is a file *reference*, not permission to execute:\n"
            "        # delegating it would hand the device a write the user never granted.\n"
            "        return False\n"
        ),
        "new": (
            "    if False:\n"
            "        # ``readonly-input`` is a file *reference*, not permission to execute:\n"
            "        # delegating it would hand the device a write the user never granted.\n"
            "        return False\n"
        ),
        "expect": ["test_a_read_only_target_is_never_delegated_even_with_the_switch_open"],
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

    ``SUBFAILED(version=1) path::Class::name`` reports a ``subTest`` failure with
    ``::`` as the class separator, so a pattern that only allowed
    ``module.name`` would silently drop every subtest failure -- which looks
    exactly like "the mutation was not caught".
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
