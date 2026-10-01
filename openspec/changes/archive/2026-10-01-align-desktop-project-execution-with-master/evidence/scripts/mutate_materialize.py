#!/usr/bin/env python3
"""变异检查：显式上传/分享（任务 9.7）的断言真的会因为实现被改坏而失败。

第 9.7 条要同时成立两件事，而它们很容易被"更省事"的写法同时做坏：
一边是「没要求上传时一个字节都不许走」，另一边是「要求了就走既有传输并把来源
带过去」。每一处走样都是这样一条看似无害的实现：

* 不再比对调用方批准的版本（文件在用户点头后又被改过，照样发上去）；
* 目录也当作可发送的东西（把一次失败变成一次空上传）；
* 大小上限不查（服务器必然拒绝，但字节已经白跑一趟）；
* 服务器提交后没给引用也算成功（模型于是拿着一份不存在的服务器副本继续）；
* 来源标记直接放相对路径（绝对路径借 source_ref 溜出设备）；
* 一次读满整窗（越过 helper 的单次读上限，窗就短了——"读到的就是磁盘上的"
  这条性质随之失真，而它正是"独立副本"的全部意义）；
* 路由层不再校验相对路径、或不再把批准版本传下去；
* 传输层把服务器的错误码压成通用失败、不校验 origin、没有会话也照发；
* 页面不再说明数据流，或把"本机"的承诺贴到服务器路径上；
* 少一份词典（三语言只剩两语言，用户看到的语言正好缺那句）。

跑同一批用例，要求出现预期失败后立刻还原源文件；任何一项「改坏了却全绿」都会
让脚本以非零码退出。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
MATERIALIZE = os.path.join(REPO, "desktop", "src", "main", "local-files", "materialize.ts")
TRANSFER = os.path.join(REPO, "desktop", "src", "main", "local-files", "transfer.ts")
DEVICE_OPS = os.path.join(REPO, "desktop", "src", "main", "remote", "device-ops.ts")
TRANSPORT = os.path.join(
    REPO, "desktop", "src", "main", "remote", "materialize-transport.ts")
WORKSPACE = os.path.join(REPO, "channel", "web", "static", "js", "workspace.js")
I18N = os.path.join(REPO, "channel", "web", "static", "js", "i18n", "core.js")

MAT_SUITE = ("node", "tests/test_desktop_materialize.cjs")
READ_SUITE = ("node", "tests/test_desktop_local_read.cjs")
PANEL_SUITE = ("node", "tests/test_desktop_project_native_actions.cjs")

MUTATIONS = [
    # -- 编排：发送的是不是用户批准的那份文件 -----------------------------
    {
        "name": "M1 不比对批准版本（文件改过也照发）",
        "file": MATERIALIZE,
        "suites": [MAT_SUITE],
        "old": (
            "  if (expected && expected !== current) {\n"
            "    throw new TransferError(\n"
            "      'file_changed',\n"
            "      'the local file changed since it was approved',\n"
            "    )\n"
            "  }\n"
        ),
        "new": "  // no version check\n",
        "expect": ["a file that is not the one approved is refused before any transfer opens"],
    },
    {
        "name": "M2 目录也当可发送（把拒绝变成一次空上传）",
        "file": MATERIALIZE,
        "suites": [MAT_SUITE],
        "old": (
            "  if (info.kind !== 'file') {\n"
            "    throw new TransferError('not_a_file', 'only a regular file can be delivered')\n"
            "  }\n"
        ),
        "new": "  // no kind check\n",
        "expect": ["a directory is refused, and a path escape never reaches the transfer"],
    },
    {
        "name": "M3 不查大小上限（字节白跑一趟才被服务器拒绝）",
        "file": MATERIALIZE,
        "suites": [MAT_SUITE],
        "old": (
            "  if (size > MATERIALIZE_MAX_BYTES) {\n"
            "    throw new TransferError(\n"
            "      'limit_exceeded',\n"
            "      `the file exceeds the ${MATERIALIZE_MAX_BYTES} byte limit`,\n"
            "    )\n"
            "  }\n"
        ),
        "new": "  // no size bound\n",
        "expect": ["a file past the server bound is refused here, before anything moves"],
    },
    {
        "name": "M4 服务器没给引用也算成功（模型拿着不存在的副本继续）",
        "file": MATERIALIZE,
        "suites": [MAT_SUITE],
        "old": (
            "  if (!artifactRef) {\n"
            "    throw new TransferError(\n"
            "      'publish_failed',\n"
            "      'the server committed the transfer without an artifact reference',\n"
            "    )\n"
            "  }\n"
        ),
        "new": "  // a missing reference is accepted\n",
        "expect": ["a commit with no artifact reference is a failure, not a silent success"],
    },
    {
        "name": "M5 来源标记直接放相对路径（绝对路径借 source_ref 溜出设备）",
        "file": MATERIALIZE,
        "suites": [MAT_SUITE],
        "old": (
            "  const relative = relativePath.replace(/\\\\/g, '/').replace(/^\\/+/, '')\n"
            "  return `desktop-file:${workspaceId || 'unbound'}:${relative}`\n"
        ),
        "new": "  return String(relativePath || '')\n",
        "expect": ["the provenance token names the project and the file, never a directory"],
    },
    # -- 读字节的窗口：越过 helper 的单次读上限就短了 ----------------------
    {
        "name": (
            "M6 一次读满整窗（越过 helper 的单次请求上限，helper 直接拒绝）"
        ),
        "file": MATERIALIZE,
        "suites": [MAT_SUITE],
        "old": "        const want = Math.min(chunk, length - got)\n",
        "new": "        const want = length - got\n",
        # Only a file past the helper's own 1 MiB ceiling can observe this: below
        # it the helper silently clamps to 32 KiB and the loop reassembles the
        # window anyway, so the change is *equivalent* there. It was declared
        # against the small-file test at first and reported "not caught" -- the
        # mutation was fine, the expectation was aimed at a case that cannot see
        # it. The 1.2 MiB case is the one that can.
        "expect": [
            "a file larger than the helper's per-request ceiling still travels in one window",
        ],
    },
    # -- 一块窗口必须属于它自己的那次传输 ---------------------------------
    {
        "name": "M7 分块不再带传输 id（两块并发上传可以互相追加字节）",
        "file": TRANSFER,
        "suites": [MAT_SUITE],
        "old": "        const ack = await args.putChunk(created.id, offset, body, digest)\n",
        "new": "        const ack = await args.putChunk('', offset, body, digest)\n",
        "expect": ["a file larger than one helper read is published whole, hashed as one file"],
    },
    # -- 路由：相对路径与批准版本都得传下去 -------------------------------
    {
        "name": "M8 路由不校验相对路径（把空路径交给上传层）",
        "file": DEVICE_OPS,
        "suites": [READ_SUITE],
        "old": "        if (!relativePath) {\n          return { state: 'failed', errorCode: 'invalid_request', errorMessage: 'materialize needs a relative path' }\n        }\n",
        "new": "        if (relativePath === '__never__') {\n          return { state: 'failed', errorCode: 'invalid_request', errorMessage: 'materialize needs a relative path' }\n        }\n",
        "expect": ["materialize without a relative path is a caller mistake, not a build gap"],
    },
    {
        "name": "M9 路由不再把批准版本传下去（版本守门形同不存在）",
        "file": DEVICE_OPS,
        "suites": [READ_SUITE],
        "old": "          expectedVersion: asString(params.expected_version),\n",
        "new": "          expectedVersion: '',\n",
        "expect": ["materialize hands the command id and the bound workspace to the uploader"],
    },
    {
        "name": "M10 没有上传口也算成功（把「做不到」说成「做好了」）",
        "file": DEVICE_OPS,
        "suites": [READ_SUITE],
        "old": (
            "        if (!deliver) {\n"
            "          return {\n"
            "            state: 'failed',\n"
            "            errorCode: 'feature_unavailable',\n"
            "            errorMessage: 'this build cannot deliver a local file to the server',\n"
            "          }\n"
            "        }\n"
        ),
        "new": (
            "        if (!deliver) {\n"
            "          return { state: 'succeeded', result: { op: 'materialize' } }\n"
            "        }\n"
        ),
        "expect": [
            "materialize without an upload port says the build cannot deliver, not that it delivered",
        ],
    },
    # -- 传输层：错误码、origin、会话 -------------------------------------
    {
        "name": "M11 把服务器的错误码压成通用失败（用户看不出为什么没传上去）",
        "file": TRANSPORT,
        "suites": [MAT_SUITE],
        "old": (
            "      const failure = codeFromBody(parsed)\n"
            "      throw new TransferError(\n"
            "        failure?.code || 'transport_error',\n"
            "        failure?.message || `the transfer request was refused (${res.status})`,\n"
            "      )\n"
        ),
        "new": (
            "      throw new TransferError(\n"
            "        'transport_error',\n"
            "        `the transfer request was refused (${res.status})`,\n"
            "      )\n"
        ),
        "expect": ["a server refusal keeps the server code instead of a generic failure"],
    },
    {
        "name": "M12 不校验 origin（配置里塞什么就往哪送）",
        "file": TRANSPORT,
        "suites": [MAT_SUITE],
        "old": (
            "  if (!allowed) {\n"
            "    throw new TransferError('invalid_request', 'the configured origin must be https')\n"
            "  }\n"
        ),
        "new": "  // every origin is accepted\n",
        "expect": ["an origin this build cannot trust is refused before a token is read"],
    },
    {
        "name": "M13 没有会话也照发（未认证的上传被当成一次失败）",
        "file": TRANSPORT,
        "suites": [MAT_SUITE],
        "old": (
            "    if (!token) {\n"
            "      throw new TransferError('auth_required', 'this device has no session for the server')\n"
            "    }\n"
        ),
        "new": "    // send without a session\n",
        "expect": ["a device with no session refuses to send rather than sending unauthenticated"],
    },
    # -- 页面：数据流说明说不说、跟谁说话 ---------------------------------
    {
        "name": "M14 本机项目不再说明数据流（用户只能猜）",
        "file": WORKSPACE,
        "suites": [PANEL_SUITE],
        "old": (
            "    const rootTooltip = wsCurrentSource === 'desktop'\n"
            "        ? `${rootText}${rootText ? ' — ' : ''}${t('ws_local_data_flow')}`\n"
            "        : rootText;\n"
        ),
        "new": "    const rootTooltip = rootText;\n",
        "expect": ["a local project crumb states the data flow, and a server one does not"],
    },
    {
        "name": "M15 把本机承诺贴到服务器路径上（服务器文件不需要这句话）",
        "file": WORKSPACE,
        "suites": [PANEL_SUITE],
        "old": (
            "    const rootTooltip = wsCurrentSource === 'desktop'\n"
            "        ? `${rootText}${rootText ? ' — ' : ''}${t('ws_local_data_flow')}`\n"
            "        : rootText;\n"
        ),
        "new": (
            "    const rootTooltip = `${rootText}${rootText ? ' — ' : ''}${t('ws_local_data_flow')}`;\n"
        ),
        "expect": ["a local project crumb states the data flow, and a server one does not"],
    },
    {
        "name": "M16 少一份词典（用户看到的语言正好缺那句）",
        "file": I18N,
        "suites": [PANEL_SUITE],
        "old": (
            "            \"ws_local_data_flow\": \"文件保存在本机；工具的输出、摘录、错误以及引用产出所需的元数据会返回服务器并可能进入模型上下文。整份文件只有在你明确要求上传或分享时才会传上去。\",\n"
        ),
        "new": "            // the zh statement is gone\n",
        "expect": [
            "the three dictionaries carry the data-flow statement and agree on its shape",
        ],
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

    Reduced to a set: a failure is printed twice (inline and in the trailing
    list), so counting lines would double every hit.
    """
    return sorted(set(re.findall(r"^\u2716 (.+?) \(\d", output, flags=re.M)))


def broken_build(output: str) -> bool:
    """Whether the suite never ran at all.

    A mutation that does not compile makes every test in the file "fail" with a
    loader error, which reads exactly like a caught mutation while nothing was
    actually asserted. Task 9.4 was misled by this once, so it is reported as
    *not caught* rather than as a hit.
    """
    return "SyntaxError" in output or "TSError" in output or "Command failed:" in output


def run_suite(kind: str, suite: str) -> str:
    proc = subprocess.run(
        ["node", "--test", suite], cwd=REPO,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.stdout


def run_suites(suites) -> tuple:
    output = ""
    caught: list = []
    for _kind, suite in suites:
        run = run_suite(_kind, suite)
        output += run
        caught.extend(failed_node_tests(run))
    return output, sorted(set(caught))


def summarize(output: str) -> str:
    counts = re.search(r"^\u2139 fail (\d+)$", output, flags=re.M)
    passing = re.search(r"^\u2139 pass (\d+)$", output, flags=re.M)
    if counts and passing:
        return "%s failed, %s passed" % (counts.group(1), passing.group(1))
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
        try:
            output, caught = run_suites(mutation["suites"])
            broke = broken_build(output)
        finally:
            write(path, original)
        assert read(path) == original, f"还原失败：{path}"
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        print(f"\n### {mutation['name']}")
        print(f"  汇总：{summarize(output)}")
        print(f"  预期失败命中：{hits}")
        print(f"  实际失败：{caught}")
        if broke:
            print("  !! 编译/加载失败：这不算被抓到（没有断言真正跑过）")
            hits = []
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
