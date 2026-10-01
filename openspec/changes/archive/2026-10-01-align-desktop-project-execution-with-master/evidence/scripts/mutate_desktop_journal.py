#!/usr/bin/env python3
"""变异检查：第 7 组（去重/journal/并发/取消/存活期）用例真的能发现实现走样（任务 7.7）。

每一项把设备端源码改成一条**看起来更省事**的实现（开始意图不落盘、去重键
少字段、崩溃后当作没跑过、同 id 不同摘要照跑、墓碑不留、有副作用命令不串行、
外部修改不报、超存活期不终止、取消不等进程结束），跑同一批真实进程用例，
要求出现预期失败后立刻还原源文件。

任何一项「改坏了却全绿」都会让脚本以非零码退出。改动前先清掉
``desktop/dist`` 的字节码，避免等长改动被 mtime+size 校验放过而读到旧产物
（这是 3.8 已经踩过的坑）。

    .venv/bin/python \
        openspec/changes/align-desktop-project-execution-with-master/evidence/scripts/mutate_desktop_journal.py
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
DESKTOP = os.path.join(REPO, "desktop")
SRC = os.path.join(DESKTOP, "src", "main", "project-execution")
DIST = os.path.join(DESKTOP, "dist")

JOURNAL = os.path.join(SRC, "journal.ts")
RUNNER = os.path.join(SRC, "command-runner.ts")
VERSIONS = os.path.join(SRC, "file-versions.ts")
DEVICE = os.path.join(SRC, "device-execution.ts")
# The production seam: the object the main process owns and drives. A capability
# that is implemented and tested but never *invoked* from here is dead code on a
# real install, and only a mutation anchored in this file can show that.
ASSEMBLY = os.path.join(
    DESKTOP, "src", "main", "remote", "local-read-assembly.ts")

SUITES = [
    "tests/test_desktop_execution_journal.cjs",
    "tests/test_desktop_command_runner.cjs",
    "tests/test_desktop_device_execution.cjs",
    # The seam suite: it is the only one that notices a capability that exists
    # in the library but is never *called* by the app (see M12).
    "tests/test_desktop_execution_wiring.cjs",
]
NODE_TEST = ["node", "--test", "--test-reporter=tap"]

MUTATIONS = [
    {
        "name": "M1 A14 开始意图不落盘，直接执行",
        "file": JOURNAL,
        "old": (
            "    this.entries.set(key, entry)\n"
            "    this.persist()\n"
            "    return { entry, created: true }\n"
        ),
        "new": (
            "    this.entries.set(key, entry)\n"
            "    return { entry, created: true }\n"
        ),
        "expect": ["a start intent reaches disk before the effect can run"],
    },
    {
        "name": "M2 A14 去重键丢掉租户与设备",
        "file": JOURNAL,
        "old": (
            "  return [scope.origin, scope.user_id, scope.tenant_id, scope.device_id,\n"
            "          scope.command_id].join('\\u0000')\n"
        ),
        "new": "  return [scope.origin, scope.user_id, scope.command_id].join('\\u0000')\n",
        "expect": ["the dedup key is the contract's own field list"],
    },
    {
        "name": "M3 A15 崩溃恢复把未完成当作没跑过",
        "file": JOURNAL,
        "old": "      if (entry.state !== 'started') continue\n",
        "new": "      if (true) continue\n",
        "expect": [
            "a crash between start and receipt becomes outcome_unknown, and is not retried",
            "A15: a crash after the start intent leaves outcome_unknown, never a silent re-run",
        ],
    },
    {
        "name": "M4 A16 同 id 不同摘要不判冲突，按新载荷执行",
        "file": JOURNAL,
        "old": (
            "      if (paramsDigest && existing.params_digest !== paramsDigest) {\n"
            "        return { kind: 'conflict', entry: existing }\n"
            "      }\n"
        ),
        "new": (
            "      if (paramsDigest && existing.params_digest.length < 0) {\n"
            "        return { kind: 'conflict', entry: existing }\n"
            "      }\n"
        ),
        "expect": [
            "A16: the same command id with different arguments is refused, and the first payload is untouched",
            "a redelivery with a different payload is refused, never executed as the new payload",
        ],
    },
    {
        "name": "M5 A16 清理回执不留墓碑，已花掉的命令又可执行",
        "file": JOURNAL,
        "old": (
            "      if (finishedAt === null || finishedAt >= retainBefore) continue\n"
            "      removed.push(entry.journal_id)\n"
            "      this.entries.delete(key)\n"
            "      survivors.push({ key, pruned_at: this.now() })\n"
        ),
        "new": (
            "      if (finishedAt === null || finishedAt >= retainBefore) continue\n"
            "      removed.push(entry.journal_id)\n"
            "      this.entries.delete(key)\n"
        ),
        "expect": [
            "A16: a command whose receipt was cleaned up cannot become executable again",
            "a pruned receipt leaves a tombstone, so a spent command cannot look fresh again",
        ],
    },
    {
        "name": "M6 A20 有副作用命令不再独占项目根",
        "file": RUNNER,
        "old": (
            "      const allowed = head.effectful\n"
            "        // An effectful command owns the root: no other side effect, and no read\n"
            "        // racing it, so a script never sees a file mid-write from a peer.\n"
            "        ? gate.running === 0\n"
            "        : !gate.effectful && gate.running < limit\n"
        ),
        "new": "      const allowed = gate.running < limit\n",
        "expect": ["A20: two aliases of one real root serialise effectful commands"],
    },
    {
        "name": "M7 A20 外部修改/删除一律不报",
        "file": VERSIONS,
        "old": (
            "  staleReason(path: string): string | null {\n"
            "    const seen = this.seen.get(path)\n"
        ),
        "new": (
            "  staleReason(path: string): string | null {\n"
            "    if (path) return null\n"
            "    const seen = this.seen.get(path)\n"
        ),
        "expect": [
            "A20: an edit reports an outside change from the version this device served",
            "A20: a file deleted after the read is reported as deleted",
        ],
    },
    {
        "name": "M8 A19 存活期判据反转，短断线也被当作超期回收",
        "file": RUNNER,
        "old": "    if (options.disconnectedForMs <= window) return []\n",
        "new": "    if (options.disconnectedForMs <= -window) return []\n",
        "expect": ["A19: an outage inside the liveness window terminates nothing"],
    },
    {
        "name": "M9 A18 取消不等进程真正结束",
        "file": RUNNER,
        "old": (
            "    this.cancelRequested.add(commandId)\n"
            "    await this.options.terminate(live.handle)\n"
            "    await live.settled\n"
            "    return true\n"
        ),
        "new": (
            "    this.cancelRequested.add(commandId)\n"
            "    return true\n"
        ),
        "expect": ["A18: a cancel resolves only after the process ended, and keeps the files already written"],
    },
    {
        "name": "M10 A15 结果帧把 outcome_unknown 报成成功",
        "file": DEVICE,
        "old": "    if (phase === 'outcome_unknown') return 'failed'\n",
        "new": "    if (phase === 'outcome_unknown') return 'succeeded'\n",
        "expect": [
            "A15: a crash after the start intent leaves outcome_unknown, never a re-run",
        ],
    },
    {
        "name": "M11 帧里的设备标识不核对（别的安装也能落进本机 journal）",
        "file": DEVICE,
        "old": "        if (String(frame.device_id) !== identity.device_id) {\n",
        "new": "        if (String(frame.device_id) === '__never__') {\n",
        "expect": ["a frame naming another device is refused without touching the journal"],
    },
    {
        "name": "M12 7.6 保留期清理从未被调用（能力实现了但没人调）",
        "file": ASSEMBLY,
        "old": (
            "    endpoint.sweep()\n"
            "    this.execution = endpoint\n"
        ),
        "new": (
            "    this.execution = endpoint\n"
        ),
        "expect": [
            "7.6: reaching the execution endpoint reclaims receipts past the retention window",
        ],
    },
]


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def failed_tests(output: str) -> list:
    """TAP failures: ``not ok <n> - <name>``."""
    return sorted(set(re.findall(r"^not ok \d+ - (.+?)\s*$", output, flags=re.M)))


def clear_bytecode() -> None:
    """Rebuild from a clean slate.

    Both suites run in parallel under ``node --test`` and each one builds the
    main process itself if ``dist`` is missing -- so deleting the output and
    leaving the build to the test hooks races them against each other, and the
    suite then fails for a reason that has nothing to do with the mutation.
    Building here, once and synchronously, also removes the stale-bytecode
    hazard: an equal-length edit would otherwise pass the mtime+size check.

    A mutation that does not type-check is a *broken* mutation, not a passing
    one: it is reported loudly instead of being mistaken for "the tests did not
    notice".
    """
    shutil.rmtree(DIST, ignore_errors=True)
    proc = subprocess.run(
        ["npm", "run", "build:main"], cwd=DESKTOP,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"变异体无法通过类型检查（锚点需要调整）：\n{proc.stdout}")


def run_suite() -> str:
    proc = subprocess.run(
        NODE_TEST + SUITES, cwd=REPO,
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
            clear_bytecode()
            output = run_suite()
        finally:
            # The restore must happen even if the build or the run raises --
            # a mutation left in place would corrupt every later check.
            write(path, original)
            clear_bytecode()
        assert read(path) == original, f"还原失败：{path}"
        caught = failed_tests(output)
        hits = [name for name in mutation["expect"]
                if any(name in test for test in caught)]
        summary = re.search(r"^# (?:tests|pass|fail).*$", output, flags=re.M)
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
    print(f"{len(MUTATIONS)} 类实现走样都被对应用例判为失败，且还原后源文件与原文一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
