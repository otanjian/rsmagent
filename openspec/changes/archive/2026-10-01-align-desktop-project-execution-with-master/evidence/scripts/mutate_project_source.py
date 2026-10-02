#!/usr/bin/env python3
"""变异检查：本机 source adapter 的用例真的会因为实现被改坏而失败（任务 9.2）。

每一项都把实现改成一条**看起来更省事**的写法（digest 自造、编辑不看失败帧、
只读授权也放行、桥接层不管绝对路径、坏的分页参数静默丢弃、本机拒绝回退服务器
API、分页读也标成可编辑、列表带上目录与服务器 URL、本地文件关着也宣称有
project 面），跑同一批用例（`node --test tests/test_desktop_project_source.cjs`），
要求出现预期失败后立刻还原源文件。任何一项「改坏了却全绿」都会让脚本以非零码
退出。

锚点落在 TypeScript 源码上时，用例的 `before()` 会自己 `tsc` 重建，所以不需要
本脚本额外构建；脚本开头与结尾各跑一次未变异的用例，作为「基线是绿的」与
「还原后 dist 由正确源码重建」两件事的证据。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
BROWSER = os.path.join(REPO, "desktop", "src", "main", "project-browser", "browser.ts")
HOST_BRIDGE = os.path.join(REPO, "desktop", "src", "main", "remote", "host-bridge.ts")
LOCAL_FILES = os.path.join(REPO, "desktop", "src", "main", "remote", "local-files-bridge.ts")
ADAPTER = os.path.join(REPO, "channel", "web", "static", "js", "fork", "project-source.js")

SUITE = "tests/test_desktop_project_source.cjs"
FS_GUARD = os.path.join(
    REPO, "desktop", "native", "fs-guard", "target", "release",
    "fs-guard.exe" if os.name == "nt" else "fs-guard")

MUTATIONS = [
    {
        "name": "M1 编辑帧自造一个 digest（收件方算出来的不是这个）",
        "file": BROWSER,
        "old": "    frame.params_digest = paramsDigest(frame)\n",
        "new": "    frame.params_digest = 'sha256:' + '0'.repeat(64)\n",
        "expect": ["an edit travels as a v2 write frame the contract accepts"],
    },
    {
        "name": "M2 执行帧报了失败，面板仍当成保存成功",
        "file": BROWSER,
        "old": "    if (reply.state !== 'succeeded' || phase !== 'succeeded') {\n",
        "new": "    if (false && (reply.state !== 'succeeded' || phase !== 'succeeded')) {\n",
        "expect": ["a frame whose run failed is reported as a failure, not a save"],
    },
    {
        "name": "M3 只读授权 / 离线设备也允许写盘",
        "file": BROWSER,
        "old": (
            "    const refusal = this.editRefusal(binding)\n"
            "    if (refusal) return refuse(refusal, this.editRefusalMessage(refusal))\n"
        ),
        "new": (
            "    const refusal = ''\n"
            "    if (refusal) return refuse(refusal, this.editRefusalMessage(refusal))\n"
        ),
        "expect": [
            "a read-only grant offers reading and refuses editing, by name",
            "an offline device refuses the edit rather than journaling one",
        ],
    },
    {
        "name": "M4 桥接层不再管路径形状（绝对路径、穿越都放过去）",
        "file": HOST_BRIDGE,
        "old": "  if (value === undefined || value === null) return allowEmpty\n",
        "new": (
            "  if (value === undefined || value === null) return allowEmpty\n"
            "  if (typeof value === 'string' && value.includes('/')) return true\n"
        ),
        "expect": ["a page parameter is validated before the port sees it"],
    },
    {
        "name": "M5 分页参数读不出来就静默丢掉（读得比要的更多）",
        "file": ADAPTER,
        "old": (
            "    if (!/^(0|[1-9][0-9]*)$/.test(raw)) {\n"
            "      return { refusal: fail('invalid_request', 'the requested ' + key + ' is not a count') };\n"
            "    }\n"
        ),
        "new": (
            "    if (!/^(0|[1-9][0-9]*)$/.test(raw)) {\n"
            "      return { value: undefined };\n"
            "    }\n"
        ),
        "expect": ["a page range reaches the bridge as numbers, and a broken one is refused"],
    },
    {
        "name": "M6 本机拒绝时回退服务器 API（同名文件会换一份）",
        "file": ADAPTER,
        "old": "    if (((reply && reply.refusal) || {}).code === 'feature_unavailable') return null;\n",
        "new": "    if (reply) return null;\n",
        "expect": ["a refusal about this project is reported, not papered over"],
    },
    {
        "name": "M7 截断的分页读也标成可编辑（保存会截掉尾巴）",
        "file": ADAPTER,
        "old": "        editable: classify.editable(kind) && !truncated,\n",
        "new": "        editable: classify.editable(kind),\n",
        "expect": ["a truncated read is not offered as editable"],
    },
    {
        "name": "M8 本机列表带上服务器 URL（面板会去请求后端）",
        "file": ADAPTER,
        "old": (
            "      name: name,\n"
            "      path: String((row && row.path) || ''),\n"
        ),
        "new": (
            "      name: name,\n"
            "      raw_url: '/api/file?path=' + String((row && row.path) || ''),\n"
            "      path: String((row && row.path) || ''),\n"
        ),
        "expect": ["a tree request is served locally, in the shape the panel already reads"],
    },
    {
        "name": "M9 本机列表带上目录（面包屑会露出真实路径）",
        "file": ADAPTER,
        # 锚点必须唯一：`path: String(data.path || '')` 在 tree / resolve / read
        # 三处都有，只写这一行会改到 resolve 上。带上紧跟的第二行，锚点才落在
        # 列表答复上（那才是"面包屑会露出真实路径"要测的地方）。
        "old": (
            "        path: String(data.path || ''),\n"
            "        // No `root`: the backend's listing names an absolute directory, and a\n"
        ),
        "new": (
            "        root: String(data.root || '/'),\n"
            "        path: String(data.path || ''),\n"
            "        // No `root`: the backend's listing names an absolute directory, and a\n"
        ),
        "expect": ["a tree request is served locally, in the shape the panel already reads"],
    },
    {
        "name": "M10 本地文件关着也宣称有 project 面",
        "file": LOCAL_FILES,
        "old": "      : [...PHASE1_METHODS],\n",
        "new": "      : [...ALL_BRIDGE_METHODS],\n",
        "expect": ["the six project methods are declared, and only with local files open"],
    },
]


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def failed_tests(output: str) -> list:
    """Name every test node's reporter marked as failing.

    ``node --test`` prints one ``✖ <name> (<ms>)`` line per failing test and
    repeats them under ``failing tests:``; both forms are parsed and deduped. A
    crash (a build error inside ``before()``) leaves this empty on purpose: the
    caller treats "no expected name" as a failure, so a mutation that breaks the
    build cannot be mistaken for a caught one.
    """
    names = re.findall(r"^\s*[✖×]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?ms\))?\s*$", output, flags=re.M)
    return sorted({name.strip() for name in names if name.strip()})


def summary(output: str) -> str:
    found = re.search(r"^\u2139 fail \d+.*$", output, flags=re.M)
    if found:
        return found.group(0).strip()
    return "(未解析到汇总行)"


def run_suite() -> str:
    env = dict(os.environ, COW_SKIP_RUST_BUILD="1")
    proc = subprocess.run(
        ["node", "--test", SUITE], cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.stdout


def main() -> int:
    if not os.path.exists(FS_GUARD):
        print(f"缺少本机 helper: {FS_GUARD}（用例会整批跳过，变异检查没有意义）")
        return 1

    baseline = run_suite()
    if failed_tests(baseline):
        print("基线不绿，先修基线再谈变异：")
        print(baseline)
        return 1
    print(f"基线：{summary(baseline)}（无失败）")

    missed: list = []
    for mutation in MUTATIONS:
        path = mutation["file"]
        original = read(path)
        if mutation["old"] not in original:
            print(f"[skip] {mutation['name']}: 锚点未命中（源码已变）")
            missed.append(mutation["name"])
            continue
        write(path, original.replace(mutation["old"], mutation["new"], 1))
        try:
            output = run_suite()
        finally:
            write(path, original)
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

    restored = run_suite()
    restored_ok = not failed_tests(restored)
    print(f"\n还原后：{summary(restored)}")

    print("\n== 结论 ==")
    if missed:
        print("变异未被用例发现（用例太弱或锚点失效）：")
        for name in missed:
            print(f"  - {name}")
        return 1
    if not restored_ok:
        print("还原后的源码仍然失败，脚本没有把仓库留在可用状态")
        return 1
    print(f"{len(MUTATIONS)} 类实现走样都被对应用例判为失败，且还原后用例重新全绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
