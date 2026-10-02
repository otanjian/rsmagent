#!/usr/bin/env python3
"""变异检查：第 8 组（技能 manifest / 版本缓存）用例真的能发现实现走样（任务 8.8）。

每一项把源码改成一条**看起来更省事**的实现（摘要只覆盖说明文件、摘要随遍历顺序
变化、跳过摘要校验、允许包外文件、不查软链接、回收忽略引用计数、缓存存在即视为
已授权、作用域丢掉服务器维度、不拒绝凭据、manifest 不看软链接、授权检查变成可选、
失败后不清理暂存），跑同一批用例，要求出现预期失败后立刻还原源文件。

任何一项「改坏了却全绿」都会让脚本以非零码退出。

    .venv/bin/python \
        openspec/changes/align-desktop-project-execution-with-master/evidence/scripts/mutate_skill_package.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHANGE = os.path.abspath(os.path.join(HERE, "..", ".."))
REPO = os.path.abspath(os.path.join(CHANGE, "..", "..", ".."))

CACHE = os.path.join(REPO, "agent", "desktop_local", "skill_cache.py")
RULES = os.path.join(REPO, "agent", "desktop_local", "package_rules.py")
MANIFEST = os.path.join(REPO, "agent", "skills", "manifest.py")
REFS = os.path.join(REPO, "agent", "desktop_local", "resource_refs.py")
RUNTIME = os.path.join(REPO, "agent", "desktop_local", "skill_runtime.py")
DEPS = os.path.join(REPO, "agent", "skills", "dependencies.py")
GUARD = os.path.join(REPO, "desktop", "build", "check-skill-dependencies.py")

SUITES = [
    "tests/test_desktop_skill_cache.py",
    "tests/test_skill_manifest.py",
    "tests/test_desktop_resource_refs.py",
    "tests/test_desktop_skill_runtime.py",
    "tests/test_skill_dependencies_lock.py",
    "tests/test_skill_dependency_guard.py",
]

MUTATIONS = [
    {
        "name": "M1 8.1 摘要只覆盖说明文件，换掉脚本也不变",
        "file": MANIFEST,
        "old": (
            "    material = \"\".join(\n"
            "        f\"{r.relative_path}\\u0000{r.digest}\\u0000{r.size}\\u0001\"\n"
            "        for r in sorted(resources, key=lambda r: r.relative_path))\n"
        ),
        "new": (
            "    _ = resources\n"
            "    material = \"constant\"\n"
        ),
        "expect": ["test_changing_a_script_changes_the_digest"],
    },
    {
        "name": "M2 8.1 摘要随遍历顺序变化",
        "file": MANIFEST,
        "old": "        for r in sorted(resources, key=lambda r: r.relative_path))\n",
        "new": "        for r in list(resources))\n",
        "expect": ["test_the_digest_does_not_depend_on_directory_listing_order"],
    },
    {
        "name": "M3 8.2 跳过载荷摘要校验",
        "file": CACHE,
        "old": (
            "            if _digest_of(bytes(body)) != expected[\"digest\"]:\n"
            "                raise SkillCacheError(\n"
            "                    \"digest_mismatch\",\n"
            "                    f\"{normalized!r} does not match the manifest digest\")\n"
        ),
        "new": "            pass\n",
        "expect": ["test_a_tampered_payload_is_refused_by_digest"],
    },
    {
        "name": "M4 8.2 允许包外文件一起发布",
        "file": CACHE,
        "old": (
            "            if normalized not in declared:\n"
            "                raise SkillCacheError(\n"
            "                    \"undeclared_file\",\n"
            "                    f\"{normalized!r} is not declared by the manifest\")\n"
        ),
        "new": "            declared.setdefault(normalized, {\"digest\": _digest_of(bytes(body)), \"size\": len(body)})\n",
        "expect": ["test_an_undeclared_file_cannot_ride_along"],
    },
    {
        "name": "M5 8.2 不检查已发布版本里的软链接",
        "file": CACHE,
        "old": (
            "                    if os.path.islink(os.path.join(root, name)):\n"
            "                        raise SkillCacheError(\"link_refused\", f\"{name!r} is a link\")\n"
        ),
        "new": "                    pass\n",
        "expect": ["test_a_symlink_inside_a_published_version_is_refused_on_verify"],
    },
    {
        "name": "M6 8.3 回收忽略引用计数（在用版本也删）",
        "file": CACHE,
        "old": (
            "            if int(entry.get(\"refcount\", 0) or 0) > 0:\n"
            "                continue\n"
            "            if entry.get(\"digest\") in protected:\n"
            "                continue\n"
        ),
        "new": "            pass\n",
        "expect": ["test_gc_never_removes_a_pinned_version"],
    },
    {
        "name": "M7 8.3 解析不查授权（缓存存在即视为已授权）",
        "file": CACHE,
        "old": (
            "        check = authorized if authorized is not None else self._authorized\n"
            "        if check is not None and not check(skill_id):\n"
            "            return None\n"
        ),
        "new": "        check = None\n",
        "expect": ["test_resolve_refuses_a_cached_skill_once_authorization_is_gone"],
    },
    {
        "name": "M8 8.2 作用域丢掉服务器维度（两台服务器串缓存）",
        "file": CACHE,
        "old": (
            "            _component(scope.origin, what=\"origin\"),\n"
            "            _component(scope.tenant_id, what=\"tenant id\"),\n"
        ),
        "new": (
            "            _component(\"shared\", what=\"origin\"),\n"
            "            _component(scope.tenant_id, what=\"tenant id\"),\n"
        ),
        "expect": ["test_a_different_server_never_lets_a_tenant_name_collide"],
    },
    {
        "name": "M9 8.2 不拒绝凭据文件",
        "file": RULES,
        "old": (
            "    if is_secret_name(relative):\n"
            "        raise PackageRuleError(\n"
            "            \"secret_refused\", f\"{relative!r} is a credential, not skill content\")\n"
        ),
        "new": "    return None\n",
        "expect": [
            "test_a_credential_shaped_file_is_refused_as_skill_content",
            "test_a_credential_file_is_not_packaged_as_skill_content",
        ],
    },
    {
        "name": "M10 8.1 manifest 打包时无视软链接（把指向的内容也算进来）",
        "file": MANIFEST,
        "old": (
            "            if os.path.islink(full):\n"
            "                raise SkillManifestError(\"link_refused\", f\"{relative!r} is a link\")\n"
        ),
        "new": "            pass\n",
        "expect": ["test_a_resource_reaching_outside_the_skill_is_refused"],
    },
    {
        "name": "M11 8.1 授权检查变成可选（默认放行）",
        "file": MANIFEST,
        "old": "                         is_authorized: Callable[[str], bool],\n",
        "new": "                         is_authorized: Callable[[str], bool] = lambda _sid: True,\n",
        "expect": ["test_the_authorization_check_is_required_not_optional"],
    },
    {
        "name": "M12 8.2 发布失败后不清理暂存",
        "file": CACHE,
        "old": (
            "        except BaseException:\n"
            "            shutil.rmtree(staging, ignore_errors=True)\n"
            "            raise\n"
        ),
        "new": (
            "        except BaseException:\n"
            "            raise\n"
        ),
        "expect": ["test_a_crash_at_the_atomic_step_leaves_no_version_and_no_staging"],
    },
    {
        "name": "M13 8.4 工具参数把 skill: 引用当项目相对路径",
        "file": os.path.join(REPO, "agent", "desktop_local", "source_resolver.py"),
        "old": (
            "        if parsed is not None and parsed.kind in (\n"
            "                resource_refs.SKILL, resource_refs.BACKEND):\n"
        ),
        "new": (
            "        if False and parsed is not None and parsed.kind in (\n"
            "                resource_refs.SKILL, resource_refs.BACKEND):\n"
        ),
        "expect": ["test_a_skill_reference_without_deployment_is_refused_not_reinterpreted"],
    },
    {
        "name": "M14 8.4 重复 pin 同一版本（引用计数只增不减，泄漏）",
        "file": REFS,
        "old": (
            "        current = self._pinned.get(skill_id)\n"
            "        if current == digest:\n"
            "            return self._cache.version_dir(self._scope, skill_id, digest)\n"
        ),
        "new": (
            "        current = self._pinned.get(skill_id)\n"
        ),
        "expect": [
            "test_pinning_the_same_version_does_not_release_it_first",
            "test_prepare_twice_does_not_leak_a_reference_per_call",
        ],
    },
    {
        "name": "M15 8.4 prepare 重复部署（丢弃旧的 pin 集合）",
        "file": RUNTIME,
        "old": (
            "        if self._skills is not None:\n"
            "            if skill_filter is None:\n"
            "                return self._skills\n"
            "            # A different selection needs a different set; let go of the old pins\n"
            "            # first so no version is held by two sets at once.\n"
            "            self.release()\n"
        ),
        "new": "        pass\n",
        "expect": ["test_prepare_twice_does_not_leak_a_reference_per_call"],
    },
    {
        "name": "M16 8.4 不拒绝写入技能缓存（缓存变成可写）",
        "file": REFS,
        "old": (
            "    if resolved.read_only:\n"
            "        raise ResourceRefError(\n"
            '            "skill_cache_read_only",\n'
        ),
        "new": (
            "    if False:\n"
            "        raise ResourceRefError(\n"
            '            "skill_cache_read_only",\n'
        ),
        "expect": ["test_a_skill_resource_may_not_be_written"],
    },
    {
        "name": "M17 8.4 技能缺少资源时用同名项目文件顶上",
        "file": REFS,
        "old": (
            "    if not os.path.exists(absolute):\n"
            "        # Reported by name. Substituting a same-named project file (or the\n"
            "        # server's copy) is the failure the spec calls out explicitly.\n"
            "        raise ResourceRefError(\n"
            '            "incompatible_skill",\n'
        ),
        "new": (
            "    if False:\n"
            "        raise ResourceRefError(\n"
            '            "incompatible_skill",\n'
        ),
        "expect": ["test_a_missing_skill_resource_is_reported_and_not_substituted"],
    },
    {
        "name": "M18 8.4 backend: 引用被当成可用输入",
        "file": REFS,
        "old": (
            "    if ref.kind == BACKEND:\n"
            "        raise ResourceRefError(\n"
            '            "server_path_not_local",\n'
        ),
        "new": (
            "    if False:\n"
            "        raise ResourceRefError(\n"
            '            "server_path_not_local",\n'
        ),
        "expect": ["test_a_backend_reference_is_refused_for_a_local_run"],
    },
    {
        "name": "M19 8.5 缺失依赖不再报错（守卫放行未打包的库）",
        "file": DEPS,
        "old": (
            "        if distribution not in lock:\n"
            "            problem = Problem(\n"
        ),
        "new": (
            "        if False:\n"
            "            problem = Problem(\n"
        ),
        "expect": [
            "test_a_declared_but_unshipped_dependency_is_reported_by_name",
            "test_a_declared_but_unshipped_library_fails_the_build",
        ],
    },
    {
        "name": "M20 8.5 无法判定的模块静默通过（把“不知道”当成“没问题”）",
        "file": DEPS,
        "old": (
            "            if distribution is None:\n"
        ),
        "new": (
            "            if distribution is not None and False:\n"
        ),
        "expect": ["test_an_unmappable_module_is_reported_rather_than_assumed_shipped"],
    },
    {
        "name": "M21 8.5 读不出的技能被静默跳过（声明未知 ≠ 无声明）",
        "file": GUARD,
        "old": (
            "    for path in unreadable:\n"
            "        problems.append(f\"unreadable skill (declarations unknown): {path}\")\n"
        ),
        "new": (
            "    for path in unreadable:\n"
            "        pass\n"
        ),
        "expect": ["test_an_unreadable_skill_fails_rather_than_being_skipped"],
    },
    {
        "name": "M22 8.5 “无法检查”被当成“检查通过”",
        "file": GUARD,
        # 锚点必须唯一：同一个 `return EXIT_CANNOT_CHECK` 在"没有技能目录"那一
        # 支也有。带上它自己的那行消息，锚点才落在"锁文件不存在"上 —— 也正是
        # 用例 `test_a_missing_lock_cannot_check_and_says_so` 断言的那一支。
        "old": (
            "        print(f\"!! cannot check: lock file not found at {args.lock}\", file=sys.stderr)\n"
            "        return EXIT_CANNOT_CHECK\n"
        ),
        "new": (
            "        print(f\"!! cannot check: lock file not found at {args.lock}\", file=sys.stderr)\n"
            "        return EXIT_OK\n"
        ),
        "expect": ["test_a_missing_lock_cannot_check_and_says_so"],
    },
]


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def failed_tests(output: str) -> list:
    """pytest failure names, including ``unittest`` subtest failures.

    Two things this has to survive, both learned the hard way:

    * ``SUBFAILED`` is how a failure inside ``assertRaises`` + ``subTest``
      surfaces. A regex that only matched ``FAILED`` reported a mutation as "not
      caught" when the suite had in fact failed -- a green mutation report built
      on a parsing bug.
    * ``SUBFAILED`` is followed directly by ``(name='...')``, *not* by
      whitespace, so an anchored ``FAILED\\s+`` never matches it.
    """
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
