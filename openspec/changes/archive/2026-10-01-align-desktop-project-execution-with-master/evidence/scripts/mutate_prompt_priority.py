#!/usr/bin/env python3
"""变异检查：第 8.7 组（产出归属 / 维护权限 / 客户端平台）用例真的能发现实现走样。

每一项把源码改成一条**看起来更省事**的实现（本地提示不再要求成果落进项目、提示暗示
项目授权覆盖技能与记忆维护、客户端平台取不到就退回服务器平台、平台提示干脆不读、
服务器会话也回答一个客户端平台、取不到平台就报 posix、平台提示报错就把提示词构建带崩），
跑同一批用例，要求出现预期失败后立刻还原源文件。

任何一项「改坏了却全绿」都会让脚本以非零码退出。

    .venv/bin/python \\
        openspec/changes/align-desktop-project-execution-with-master/evidence/scripts/mutate_prompt_priority.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHANGE = os.path.abspath(os.path.join(HERE, "..", ".."))
REPO = os.path.abspath(os.path.join(CHANGE, "..", "..", ".."))

BUILDER = os.path.join(REPO, "agent", "prompt", "builder.py")
AGENT = os.path.join(REPO, "agent", "protocol", "agent.py")
EXECUTOR = os.path.join(REPO, "agent", "desktop_local", "script_executor.py")
SCRIPT_TOOL = os.path.join(REPO, "agent", "desktop_local", "script_tool.py")

SUITES = [
    "tests/test_desktop_local_prompt_priority.py",
]

MUTATIONS = [
    {
        "name": "M1 8.7 本地提示不再要求成果落进项目（产出可以留在临时目录）",
        "file": BUILDER,
        "old": (
            "        \"- 用户要的成果要落进**这个项目**：输出、报告与生成的文件都写在项目目录下\",\n"
            "        \"  （相对路径本来就是这个意思）。不要把成果留在临时目录里，也不要让用户自己去找。\",\n"
        ),
        "new": "        \"- 成果放在哪里都可以，不必特意放进某个目录。\",\n",
        "expect": ["test_the_local_notes_say_the_deliverable_belongs_in_the_project"],
    },
    {
        "name": "M2 8.7 暗示项目授权覆盖技能与记忆维护（越权口径）",
        "file": BUILDER,
        "old": (
            "        \"- 该项目授予的是**这个目录**上的权限，不延伸到技能与记忆维护：二者仍遵循原有权限，\",\n"
            "        \"  仍在上面那个系统目录中。不要经由项目去写它们；也不要把客户端路径交给\",\n"
            "        \"  在服务端解析路径的工具（记忆、知识、MCP、远程业务 API）——工具无法接受该路径时\",\n"
            "        \"  应如实说明，而不是自己编一个。\",\n"
        ),
        "new": (
            "        \"- 该项目授予的权限同样适用于技能与记忆维护。\",\n"
        ),
        "expect": [
            "test_the_notes_say_the_project_grant_does_not_cover_skills_or_memory",
            "test_the_notes_name_the_server_side_tools_that_cannot_take_a_client_path",
        ],
    },
    {
        "name": "M3 8.7 客户端平台取不到就退回服务器平台（自信地答错）",
        "file": BUILDER,
        "old": (
            "    if language == \"en\":\n"
            "        lines = [\n"
            "            \"\",\n"
            "            \"**This project is on the user's own computer.**\",\n"
        ),
        "new": (
            "    if not client_platform:\n"
            "        import sys as _sys\n"
            "        client_platform = _sys.platform\n"
            "    if language == \"en\":\n"
            "        lines = [\n"
            "            \"\",\n"
            "            \"**This project is on the user's own computer.**\",\n"
        ),
        "expect": ["test_an_unknown_platform_is_never_replaced_by_the_servers_own"],
    },
    {
        "name": "M4 8.7 客户端平台未知时不再说明「未知」（该说的警示没了）",
        "file": BUILDER,
        "old": (
            "            \"服务器的操作系统，也不要用仅 Unix 可用的命令。\"\n"
            "        )\n"
            "    else:\n"
            "        lines.append(\n"
            "            \"- 这里的命令运行在**客户端**平台上，而本轮未能确定该平台；不要假设它就是\"\n"
        ),
        "new": (
            "            \"服务器的操作系统，也不要用仅 Unix 可用的命令。\"\n"
            "        )\n"
            "    elif False:\n"
            "        lines.append(\n"
            "            \"- 这里的命令运行在**客户端**平台上，而本轮未能确定该平台；不要假设它就是\"\n"
        ),
        "expect": ["test_an_unknown_platform_is_said_to_be_unknown"],
    },
    {
        "name": "M5 8.7 运行时的脚本工具提示干脆不读（平台恒为未知）",
        "file": BUILDER,
        "old": (
            "    for tool in tools or []:\n"
            "        hint = getattr(tool, \"platform_hint\", None)\n"
        ),
        "new": (
            "    return \"\"\n"
            "    for tool in tools or []:\n"
            "        hint = getattr(tool, \"platform_hint\", None)\n"
        ),
        "expect": ["test_a_tool_that_reports_a_platform_is_read"],
    },
    {
        "name": "M6 8.7 服务器会话也回答一个客户端平台（服务器平台冒充客户端）",
        "file": AGENT,
        "old": (
            "        target = getattr(self, \"execution_target\", None)\n"
            "        if not getattr(target, \"is_desktop\", False):\n"
            "            return \"\"\n"
            "        try:\n"
            "            from agent.desktop_remote.mode import remote_mode_for\n"
        ),
        "new": (
            "        import sys as _sys\n"
            "        target = getattr(self, \"execution_target\", None)\n"
            "        if not getattr(target, \"is_desktop\", False):\n"
            "            return _sys.platform\n"
            "        try:\n"
            "            from agent.desktop_remote.mode import remote_mode_for\n"
        ),
        "expect": ["test_a_server_session_has_no_client_and_says_so",
                   "test_a_server_session_never_calls_the_launcher"],
    },
    {
        "name": "M7 8.7 取不到客户端平台就报 posix（把未知当成 POSIX）",
        "file": EXECUTOR,
        "old": (
            "    payload, refusal = _capability_payload(scope)\n"
            "    if refusal or payload is None:\n"
            "        return \"\"\n"
            "    return str(payload.get(\"platform\") or \"\").strip()\n"
        ),
        "new": (
            "    payload, refusal = _capability_payload(scope)\n"
            "    if refusal or payload is None:\n"
            "        return \"posix\"\n"
            "    return str(payload.get(\"platform\") or \"posix\").strip()\n"
        ),
        "expect": ["test_an_unsupported_launcher_reports_no_platform",
                   "test_a_refusal_reports_no_platform",
                   "test_a_payload_without_a_platform_reports_no_platform"],
    },
    {
        "name": "M8 8.7 平台提示报错就把整个提示词构建带崩",
        "file": BUILDER,
        "old": (
            "        except Exception as e:  # noqa: BLE001 - a failed hint is simply no hint\n"
            "            logger.debug(f\"Client platform hint skipped: {e}\")\n"
            "            continue\n"
        ),
        "new": "        except Exception:\n            raise\n",
        "expect": ["test_a_raising_hint_is_an_unknown_platform_not_a_crash"],
    },
    {
        "name": "M9 8.7 服务器端项目也声称有客户端平台（把服务器项目当本机项目）",
        "file": BUILDER,
        "old": (
            "        if workspace_scope == \"local\":\n"
        ),
        "new": (
            "        if workspace_scope in (\"local\", \"project\"):\n"
        ),
        "expect": ["test_a_server_project_run_does_not_claim_a_client_platform"],
    },
    {
        "name": "M10 8.7 本地脚本工具沿用服务器平台说明（平台描述照搬）",
        "file": SCRIPT_TOOL,
        "old": (
            "        self._base_description = _without_platform_note(delegate)\n"
        ),
        "new": (
            "        self._base_description = str(getattr(delegate, \"description\", \"\") or \"\")\n"
        ),
        "expect": [
            "test_the_isolated_tool_drops_the_server_platform_from_its_fallback",
        ],
    },
    {
        "name": "M11 8.7 平台常量不拆出来（平台说明无法被撤下）",
        "file": SCRIPT_TOOL,
        "old": (
            "    text = str(getattr(delegate, \"description\", \"\") or \"\")\n"
            "    note = str(getattr(delegate, \"platform_note\", \"\") or \"\")\n"
            "    if not note or note not in text:\n"
            "        return text\n"
        ),
        "new": (
            "    text = str(getattr(delegate, \"description\", \"\") or \"\")\n"
            "    note = \"\"\n"
            "    if not note or note not in text:\n"
            "        return text\n"
        ),
        "expect": [
            "test_stripping_reproduces_the_posix_description_exactly",
            "test_the_isolated_tool_drops_the_server_platform_from_its_fallback",
        ],
    },
    {
        "name": "M12 8.7 撤下平台说明时顺手重排文本（把描述改坏）",
        "file": SCRIPT_TOOL,
        "old": (
            "    trimmed = text.replace(\"\\n\" + note + \"\\n\", \"\", 1)\n"
            "    return trimmed.replace(\"\\n\\n\\n\", \"\\n\\n\")\n"
        ),
        "new": (
            "    return text.replace(note, \"\", 1).replace(\"\\n\\n\", \"\\n\")\n"
        ),
        "expect": [
            "test_stripping_reproduces_the_posix_description_exactly",
        ],
    },
]


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def failed_tests(output: str) -> list:
    """pytest failure names, including ``unittest`` subtest failures."""
    names = re.findall(r"^(?:SUB)?FAILED(?:\([^)]*\))?\s+\S+::(\S+?)\s*$",
                       output, flags=re.M)
    names += re.findall(r"^(?:SUB)?FAILED(?:\([^)]*\))?\s+\S+::(\S+?)(?:::|\s|$)",
                        output, flags=re.M)
    return sorted(set(names))


def run_suites() -> str:
    proc = subprocess.run(
        [os.path.join(REPO, ".venv", "bin", "python"), "-m", "pytest"]
        + SUITES + ["-q", "-p", "no:randomly"],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
        write(path, original.replace(mutation["old"], mutation["new"], 1))
        try:
            output = run_suites()
        finally:
            write(path, original)
        assert read(path) == original, f"还原失败：{path}"

        caught = failed_tests(output)
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        # 编译/启动就失败会让整套用例一起变红，看起来"抓到了"，其实一句断言都没跑。
        # 套件用 `stdio: 'pipe'` 调用 tsc，冲出来的只有 node 自己的
        # `Command failed: npx tsc ...`，所以这里按它判"根本没起来"。
        broken = "Command failed:" in output
        if broken:
            print("  ** 变异后根本编不过/起不来：不算用例发现的，必须改写这条变异")
        status = "命中" if hits and not broken else "未命中（用例没有发现走样）"
        print(f"### {mutation['name']}")
        print(f"  预期失败命中：{mutation['expect']}")
        print(f"  实际失败：{caught}")
        print(f"  → {status}")
        if not hits or broken:
            failures.append(mutation["name"])

    if failures:
        print("\n以下变异未被发现：")
        for name in failures:
            print(f"  - {name}")
        return 1
    print(f"\n{len(MUTATIONS)} 类实现走样都被对应用例判为失败，且还原后源文件与原文一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
