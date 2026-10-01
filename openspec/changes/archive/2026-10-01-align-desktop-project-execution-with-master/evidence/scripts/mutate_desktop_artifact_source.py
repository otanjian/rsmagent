#!/usr/bin/env python3
"""变异检查：本机产出源（任务 9.1）的断言真的会因为实现被改坏而失败。

第 9.1 条有两端：服务器端（`agent/protocol/artifact.py` 等，负责把来源挂到事件
上、把本机卡片投影成"没有服务器 URL"的形状）和设备端（`desktop/src/main/
project-execution/artifact.ts` 等，负责在真机上核验文件后给出引用）。只测一端
是不够的：本机文件的真实性只有设备知道，而卡片该长什么样只有服务器知道。

每一处走样都是一条**看起来更省事**的实现：不给产出记来源、给本机文件也发服务器
URL、把文件名当版本、给服务器产出也盖本机来源、设备不核验文件就把引用发出去。
跑同一批用例，要求出现预期失败后立刻还原源文件；任何一项「改坏了却全绿」都会让
脚本以非零码退出。

注意：等长改动的 `mtime+size` 可能与旧 `.pyc` 校验相符，从而读到旧字节码并伪造
「变异未被发现」的结论。所以每次跑之前都清掉被改模块的 `__pycache__`，并让子进程
不写字节码。设备端同理：`node --test` 那个套件自己会用源文件 mtime 决定是否重编
（`needsBuild`），改过 `.ts` 就会重新 `tsc`，所以它读到的一定是变异后的字节。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
ARTIFACT = os.path.join(REPO, "agent", "protocol", "artifact.py")
STREAM = os.path.join(REPO, "agent", "protocol", "agent_stream.py")
RUNTIME = os.path.join(REPO, "channel", "web", "fork", "runtime.py")
DESKTOP = os.path.join(REPO, "desktop")
ARTIFACT_TS = os.path.join(DESKTOP, "src", "main", "project-execution", "artifact.ts")
DEVICE_TS = os.path.join(DESKTOP, "src", "main", "project-execution", "device-execution.ts")

PY_SUITE = "tests/test_desktop_artifact_source.py"
NODE_SUITE = "tests/test_desktop_device_artifacts.cjs"
PYTEST = [".venv/bin/python", "-m", "pytest", "-q", "-p", "no:randomly"]

MUTATIONS = [
    {
        "name": "M1 本机产出不记录来源（只有文件，没有设备/项目/运行）",
        "runner": "py",
        "file": ARTIFACT,
        "old": (
            "    local = _local_origin(abs_path, rel_path, origin)\n"
            "    if local is not None:\n"
            '        artifact["origin"] = local\n'
        ),
        "new": "    local = None\n",
        "expect": [
            "test_a_local_artifact_records_the_contract_fields",
            "test_a_local_write_carries_device_run_and_tool_call",
        ],
    },
    {
        "name": "M2 本机文件也发服务器 raw_url/preview_url/abs_path",
        "runner": "py",
        "file": RUNTIME,
        "old": (
            '    origin = data.get("origin")\n'
            '    if isinstance(origin, dict) and origin.get("source") == "desktop":\n'
        ),
        "new": (
            '    origin = data.get("origin")\n'
            "    if False:\n"
        ),
        "expect": ["test_a_local_artifact_gets_no_server_url_or_absolute_path"],
    },
    {
        "name": "M3 版本取显示器上的文件名（而不是文件内容）",
        "runner": "py",
        "file": ARTIFACT,
        "old": '    return "sha256:" + digest.hexdigest()\n',
        "new": '    return "sha256:" + os.path.basename(path)\n',
        "expect": ["test_the_name_is_not_part_of_the_version"],
    },
    {
        "name": "M4 服务器产出也盖上本机来源（无目标也发设备标识）",
        "runner": "py",
        "file": ARTIFACT,
        "old": (
            "    if target is None or not getattr(target, \"is_desktop\", False):\n"
            "        return None\n"
        ),
        "new": (
            "    if target is None:\n"
            "        target = type(\"T\", (), {\"is_desktop\": True, \"device_id\": \"\",\n"
            "                           \"workspace_id\": \"\", \"binding_id\": \"\",\n"
            "                           \"project_mode\": \"\", \"grant_version\": 0})()\n"
        ),
        "expect": ["test_a_backend_target_gets_no_origin"],
    },
    {
        "name": "M5 撤权/不可解析的运行也盖本机来源",
        "runner": "py",
        "file": STREAM,
        "old": (
            "            cwd, _refusal = run_local_cwd(identity)\n"
            "            if not cwd:\n"
            "                return None\n"
        ),
        "new": '            cwd = getattr(identity, "execution_cwd", "") or ""\n',
        "expect": ["test_a_revoked_grant_stamps_no_origin"],
    },
    # -- 设备端（引用是在有字节的那台机器上造的） -------------------------
    {
        "name": "M6 设备生成了文件却在结果帧里不带引用",
        "runner": "node",
        "file": DEVICE_TS,
        "old": "            ...(artifacts.length ? { artifacts } : {}),\n",
        "new": "            ...(false ? { artifacts } : {}),\n",
        "expect": ["a successful write emits one desktop artifact naming the real file"],
    },
    {
        "name": "M7 设备版本取文件名（而不是内容摘要）",
        "runner": "node",
        "file": ARTIFACT_TS,
        "old": "    return 'sha256:' + hash.digest('hex')\n",
        "new": "    return 'sha256:' + hash.update(path.basename(absolutePath)).digest('hex')\n",
        "expect": ["the source version is the content digest, not the file name"],
    },
    {
        "name": "M8 设备不判越界（符号链接与项目外路径也当成产出）",
        "runner": "node",
        "file": ARTIFACT_TS,
        "old": (
            "    if (!isInside(realRoot, real)) return null\n"
            "\n"
            "    const relativePath = path.relative(realRoot, real).split(path.sep).join('/')\n"
            "    if (!relativePath || relativePath.startsWith('..') || path.isAbsolute(relativePath)) {\n"
            "        return null\n"
            "    }\n"
        ),
        "new": (
            "    const relativePath = path.relative(realRoot, real).split(path.sep).join('/')\n"
            "    if (!relativePath) {\n"
            "        return null\n"
            "    }\n"
        ),
        "expect": [
            "a file reached through a symlinked directory is not in the project",
            "an absolute path outside the project is not a reference",
        ],
    },
    {
        "name": "M10 设备把失败运行留下的半成品也当成本机产出",
        "runner": "node",
        "file": DEVICE_TS,
        "old": "        if (phase !== 'succeeded' && phase !== 'cancelled') return []\n",
        "new": "        if (phase === 'impossible') return []\n",
        "expect": ["a write run that failed after writing the file emits no artifact"],
    },
    {
        "name": "M9 设备只信工具的一面之词（不核验存在、不是普通文件、不判越界）",
        "runner": "node",
        "file": ARTIFACT_TS,
        # 这条覆盖整段"先核验再发布"的实现，包括目录：`sourceVersion` 在目录上
        # 本来也会抛（外层 catch 兜住），所以"目录不算产出"单独改一处锚点是不可
        # 观测的，只有把这整段捷径写出来才暴露出用例的价值。
        "old": (
            "    let stat: fs.Stats\n"
            "    try {\n"
            "        stat = fs.lstatSync(named)\n"
            "    } catch {\n"
            "        return null\n"
            "    }\n"
            "    if (!stat.isFile()) return null\n"
            "\n"
            "    // ...and the *real* path must still be inside the project: a symlinked\n"
            "    // directory component above the file would otherwise escape it.\n"
            "    let real: string\n"
            "    try {\n"
            "        real = fs.realpathSync(named)\n"
            "    } catch {\n"
            "        return null\n"
            "    }\n"
            "    if (!isInside(realRoot, real)) return null\n"
            "\n"
            "    const relativePath = path.relative(realRoot, real).split(path.sep).join('/')\n"
            "    if (!relativePath || relativePath.startsWith('..') || path.isAbsolute(relativePath)) {\n"
            "        return null\n"
            "    }\n"
            "    try {\n"
            "        return {\n"
            "            source: ARTIFACT_SOURCE,\n"
            "            artifact_id: artifactId(input.runId, input.toolCallId, relativePath),\n"
            "            device_id: input.deviceId,\n"
            "            workspace_id: input.workspaceId,\n"
            "            run_id: input.runId,\n"
            "            tool_call_id: input.toolCallId,\n"
            "            relative_path: relativePath,\n"
            "            file_name: path.basename(real),\n"
            "            kind: artifactKind(real),\n"
            "            size: stat.size,\n"
            "            source_version: sourceVersion(real),\n"
            "        }\n"
            "    } catch {\n"
            "        return null\n"
            "    }\n"
        ),
        "new": (
            "    try {\n"
            "        return {\n"
            "            source: ARTIFACT_SOURCE,\n"
            "            artifact_id: artifactId(input.runId, input.toolCallId, candidate),\n"
            "            device_id: input.deviceId,\n"
            "            workspace_id: input.workspaceId,\n"
            "            run_id: input.runId,\n"
            "            tool_call_id: input.toolCallId,\n"
            "            relative_path: candidate,\n"
            "            file_name: path.basename(candidate),\n"
            "            kind: artifactKind(candidate),\n"
            "            size: 0,\n"
            "            source_version: 'v0',\n"
            "        }\n"
            "    } catch {\n"
            "        return null\n"
            "    }\n"
        ),
        "expect": [
            "a frame whose file never reached disk emits no artifact",
            "a symlink pointing outside the project is not a local reference",
            "a directory is not an artifact",
        ],
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
    return sorted(set(re.findall(r"^FAILED (\S+)", output, flags=re.M)))


def failed_node_tests(output: str) -> list:
    """``node --test`` marks a failing test with ``✖ <name> (<ms>)``.

    The marker is printed twice for a failure (once inline, once in the trailing
    "failing tests" list), so the set is what matters -- counting lines would
    double every hit.
    """
    return sorted(set(re.findall(r"^\u2716 (.+?) \(\d", output, flags=re.M)))


def run_suite(runner: str) -> str:
    if runner == "node":
        proc = subprocess.run(
            ["node", "--test", NODE_SUITE], cwd=REPO,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return proc.stdout
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(PYTEST + [PY_SUITE], cwd=REPO, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    return proc.stdout


def summarize(output: str) -> str:
    line = re.search(r"^\d+ (?:failed.*?passed|passed).*$", output, flags=re.M)
    if line:
        return line.group(0)
    counts = re.search(r"^\u2139 fail (\d+)$", output, flags=re.M)
    passing = re.search(r"^\u2139 pass (\d+)$", output, flags=re.M)
    if counts and passing:
        return "%s failed, %s passed" % (counts.group(1), passing.group(1))
    return "（未解析到汇总行）"


def main() -> int:
    failures: list = []
    for mutation in MUTATIONS:
        path = mutation["file"]
        runner = mutation["runner"]
        original = read(path)
        if mutation["old"] not in original:
            print(f"[skip] {mutation['name']}: 锚点未命中（源码已变）")
            failures.append(mutation["name"])
            continue
        mutated = original.replace(mutation["old"], mutation["new"], 1)
        write(path, mutated)
        if runner == "py":
            clear_pycache(path)
        try:
            output = run_suite(runner)
        finally:
            write(path, original)
            if runner == "py":
                clear_pycache(path)
        assert read(path) == original, f"还原失败：{path}"
        caught = failed_node_tests(output) if runner == "node" else failed_py_tests(output)
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        # 编译/启动就失败会让整套用例一起变红，看起来"抓到了"，其实一句断言都没跑。
        # 套件用 `stdio: 'pipe'` 调用 tsc，冲出来的只有 node 自己的
        # `Command failed: npx tsc ...`，所以这里按它判"根本没起来"。
        broken = "Command failed:" in output
        if broken:
            print("  ** 变异后根本编不过/起不来：不算用例发现的，必须改写这条变异")
        print(f"\n### {mutation['name']}")
        print(f"  汇总：{summarize(output)}")
        print(f"  预期失败命中：{hits}")
        print(f"  实际失败：{caught}")
        if not hits or broken:
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
