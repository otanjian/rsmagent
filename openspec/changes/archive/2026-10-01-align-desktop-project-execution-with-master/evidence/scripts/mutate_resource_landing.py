#!/usr/bin/env python3
"""变异检查：第 8.6 组（服务器资源落地与落盘工具分类）用例真的能发现实现走样。

每一项把源码改成一条**看起来更省事**的实现（不查版本、不查来源自报摘要、不查调用
方摘要、摘要无法解析就当没写、缓存不看摘要、资源 id 允许上跳、缺目录就现建、把服务
器资源当项目路径、未分类的落盘工具照样执行、跨用户/跨租户也能取、取字节时用库里存
的摘要而不是现算、run 临时目录在"问路径"时就创建），跑同一批用例，要求出现预期失败
后立刻还原源文件。

任何一项「改坏了却全绿」都会让脚本以非零码退出。

    .venv/bin/python \\
        openspec/changes/align-desktop-project-execution-with-master/evidence/scripts/mutate_resource_landing.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHANGE = os.path.abspath(os.path.join(HERE, "..", ".."))
REPO = os.path.abspath(os.path.join(CHANGE, "..", "..", ".."))

LANDING = os.path.join(REPO, "agent", "desktop_local", "resource_landing.py")
STAGING = os.path.join(REPO, "agent", "desktop_local", "source_resolver.py")
REFS = os.path.join(REPO, "agent", "desktop_local", "resource_refs.py")
RUNINPUTS = os.path.join(REPO, "agent", "desktop_local", "run_inputs.py")
RUNTIME = os.path.join(REPO, "agent", "desktop_local", "run_context.py")
DISPOSITION = os.path.join(REPO, "agent", "desktop_local", "tool_disposition.py")
STREAM = os.path.join(REPO, "agent", "protocol", "agent_stream.py")

SUITES = [
    "tests/test_desktop_resource_landing.py",
    "tests/test_desktop_resource_staging.py",
    "tests/test_desktop_tool_disposition.py",
    "tests/test_desktop_run_inputs.py",
    "tests/test_desktop_local_input_staging.py",
    "tests/test_desktop_resource_refs.py",
]

MUTATIONS = [
    {
        "name": "M1 8.6 不查版本，回来的就是对的",
        "file": LANDING,
        "old": (
            "        if request.version and str(fetched.version or \"\") != request.version:\n"
            "            return _refusal(\n"
            "                UNAVAILABLE,\n"
            "                f\"resource {request.resource_id!r} came back as version \"\n"
            "                f\"{fetched.version!r}, but version {request.version!r} was pinned; \"\n"
            "                f\"a drifted version is not used\")\n"
        ),
        "new": "        pass\n",
        "expect": ["test_a_fetched_version_that_drifts_from_the_pin_is_refused"],
    },
    {
        "name": "M2 8.6 不查来源自报摘要（调用方没钉就完全不校验）",
        "file": LANDING,
        "old": (
            "        claimed = normalize_digest(fetched.digest)\n"
            "        raw_claim = str(fetched.digest or \"\").strip()\n"
        ),
        "new": (
            "        claimed = actual\n"
            "        raw_claim = actual\n"
        ),
        "expect": ["test_a_payload_whose_bytes_do_not_match_its_own_digest_is_refused"],
    },
    {
        "name": "M3 8.6 不查调用方钉的摘要",
        "file": LANDING,
        "old": (
            "        if not _digests_agree(pinned, actual):\n"
            "            return _refusal(\n"
            "                DIGEST_MISMATCH,\n"
            "                f\"resource {request.resource_id!r} does not match the pinned digest \"\n"
            "                f\"(expected {pinned}, computed {actual}); nothing was landed\")\n"
        ),
        "new": "        pass\n",
        "expect": ["test_a_payload_that_fails_the_callers_pin_is_refused"],
    },
    {
        "name": "M4 8.6 钉住的摘要解析不了就当没钉（把笔误降级成不校验）",
        "file": LANDING,
        "old": (
            "        declared_pin = str(request.digest or \"\").strip()\n"
            "        if declared_pin and not pinned:\n"
        ),
        "new": (
            "        declared_pin = \"\"\n"
            "        if declared_pin and not pinned:\n"
        ),
        "expect": ["test_an_unparsable_pin_is_refused_rather_than_ignored"],
    },
    {
        "name": "M5 8.6 两边都没有摘要也照样落地（不校验就发布）",
        "file": LANDING,
        "old": (
            "        if not claimed and not pinned:\n"
        ),
        "new": (
            "        if False:\n"
        ),
        "expect": ["test_a_source_reporting_no_digest_is_refused_without_a_caller_pin"],
    },
    {
        "name": "M6 8.6 缓存命中不看调用方的钉",
        "file": LANDING,
        "old": (
            "            if pinned and cached_digest != pinned:\n"
            "                continue\n"
        ),
        "new": "            pass\n",
        "expect": ["test_a_cached_landing_does_not_answer_a_different_pin"],
    },
    {
        "name": "M7 8.6 资源 id 允许上跳（服务器给的名字直接当路径）",
        "file": LANDING,
        "old": (
            "        if not parts or \"..\" in parts:\n"
            "            return None\n"
        ),
        "new": (
            "        if not parts:\n"
            "            return None\n"
        ),
        "expect": ["test_a_resource_id_cannot_escape_the_landing_root"],
    },
    {
        "name": "M8 8.6 落地目录不存在就现建一个",
        "file": LANDING,
        "old": (
            "        real = os.path.realpath(self._root)\n"
            "        return real if os.path.isdir(real) else None\n"
        ),
        "new": (
            "        real = os.path.realpath(self._root)\n"
            "        os.makedirs(real, exist_ok=True)\n"
            "        return real\n"
        ),
        "expect": ["test_a_missing_root_is_a_refusal_not_a_created_directory"],
    },
    {
        "name": "M9 8.6 服务器资源当成项目内相对路径来读",
        "file": REFS,
        "old": (
            "    if ref.kind == RESOURCE:\n"
            "        # A server resource has no local path until an explicit landing puts one\n"
            "        # there (``resource_landing``). Refusing here -- rather than resolving to\n"
            "        # a directory literally named ``resource:att_1`` -- is what keeps \"not\n"
            "        # landed\" from turning into a confusing \"file not found\".\n"
            "        raise ResourceRefError(\n"
            "            \"resource_not_landed\",\n"
            "            f\"the server resource {ref.relative!r} is not on this machine; \"\n"
            "            f\"it must be landed explicitly before a local tool can read it\")\n"
        ),
        "new": (
            "    if ref.kind == RESOURCE:\n"
            "        return ResolvedRef(kind=RESOURCE, logical=ref.relative)\n"
        ),
        "expect": ["test_a_resource_reference_has_no_local_path_until_it_is_landed"],
    },
    {
        "name": "M10 8.6 没有落地传输时退回服务器路径",
        "file": STAGING,
        "old": (
            "            if landing is None:\n"
            "                refusals.append(REFUSAL_RESOURCE_NOT_LANDED.format(raw=original))\n"
            "                continue\n"
        ),
        "new": (
            "            if landing is None:\n"
            "                continue\n"
        ),
        "expect": ["test_a_resource_without_a_landing_transport_is_refused_by_name"],
    },
    {
        "name": "M11 8.6 跨用户也能取到别人的运行输入",
        "file": RUNINPUTS,
        "old": (
            "        if row.get(\"user_id\") != getattr(ident, \"user_id\", \"\"):\n"
            "            return None\n"
        ),
        "new": "        pass\n",
        "expect": ["test_another_users_input_is_not_found"],
    },
    {
        "name": "M12 8.6 跨租户也能取到别人的运行输入",
        "file": RUNINPUTS,
        "old": (
            "        if tenant_id and row.get(\"tenant_id\") and row[\"tenant_id\"] != tenant_id:\n"
            "            return None\n"
        ),
        "new": "        pass\n",
        "expect": ["test_another_tenants_input_is_not_found"],
    },
    {
        "name": "M13 8.6 取字节时用库里存的摘要，而不是现算",
        "file": RUNINPUTS,
        "old": (
            "        return FetchedResource(\n"
            "            version=ref.version,\n"
            "            digest=digest_of(data),\n"
        ),
        "new": (
            "        _ = digest_of(data)\n"
            "        return FetchedResource(\n"
            "            version=ref.version,\n"
            "            digest=ref.version,\n"
        ),
        "expect": ["test_the_fetched_digest_is_of_the_bytes_on_disk"],
    },
    {
        "name": "M14 8.6 「问落地目录在哪」就把目录建出来",
        "file": RUNTIME,
        "old": (
            "    return os.path.join(root, INPUT_DIR_NAME, INPUT_SUBDIR, scope), None\n"
        ),
        "new": (
            "    directory = os.path.join(root, INPUT_DIR_NAME, INPUT_SUBDIR, scope)\n"
            "    os.makedirs(directory, mode=0o700, exist_ok=True)\n"
            "    return directory, None\n"
        ),
        "expect": ["test_resolving_the_directory_does_not_create_it"],
    },
    {
        "name": "M15 8.6 未分类的落盘工具照样执行（A36 不设闸门）",
        "file": DISPOSITION,
        "old": (
            "    if not name or name not in inventory() or name in DISPOSITIONS:\n"
            "        return \"\"\n"
        ),
        "new": (
            "    return \"\"\n"
            "    if not name or name not in inventory() or name in DISPOSITIONS:\n"
            "        return \"\"\n"
        ),
        "expect": ["test_an_unclassified_writer_is_refused",
                   "test_a_new_writing_tool_with_no_disposition_is_not_dispatched"],
    },
    {
        "name": "M16 8.6 派发路径不再调用分类闸门（规则留着但没人用）",
        "file": STREAM,
        "old": (
            "        disposition_refusal = unclassified_writer_refusal(tool_name)\n"
            "        if disposition_refusal:\n"
            "            return None, disposition_refusal, \"capability\"\n"
        ),
        "new": "        pass\n",
        "expect": ["test_a_new_writing_tool_with_no_disposition_is_not_dispatched"],
    },
    {
        "name": "M17 8.6 资源 id 里的分隔符不再过滤（run 目录可被 id 左右）",
        "file": RUNTIME,
        "old": (
            "            return \"\".join(c for c in value if c.isalnum() or c in \"-_\")[:64]\n"
        ),
        "new": (
            "            return value[:64]\n"
        ),
        "expect": ["test_the_scope_identifier_cannot_carry_a_path_separator"],
    },
    {
        "name": "M18 8.6 运行输入的上跳不再拦（库里存的名字可越出工作目录）",
        "file": RUNINPUTS,
        "old": (
            "        if candidate != root and root not in candidate.parents:\n"
            "            logger.warning(\n"
            "                f\"[RunInputs] run input {ref.resource_id!r} escapes the work dir\")\n"
            "            return None\n"
        ),
        "new": "        pass\n",
        "expect": ["test_a_run_input_that_escapes_the_work_dir_is_refused"],
    },
    {
        "name": "M19 8.6 artifact_rel 改按发布暂存根解析（用错基准目录）",
        "file": RUNINPUTS,
        "old": (
            "        from common.state_dir import agent_user_work_dir\n"
            "\n"
            "        work = agent_user_work_dir(self._ident(), ensure=False)\n"
            "        return None if work is None else Path(work)\n"
        ),
        "new": (
            "        return Path(self._publisher()._root())\n"
        ),
        "expect": ["test_production_resolution_uses_the_work_dir_base"],
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
