# encoding:utf-8
"""8.8: run A06/A07/A11-A13/A27 against a representative, unmodified skill.

The acceptance lines these tests execute, from ``acceptance.md``:

    A06  本地与远程各运行固定 Excel 技能 | 汇总单元格为 350；报告真实位于所选项目；
         可用系统应用打开；输入摘要不变
    A07  本地与远程各运行带模板和辅助资源的文档技能 | 模板资源齐全、输出字段正确；
         技能/记忆目录未搬进项目；项目以外缓存未被写入
    A11  技能包传输中断、摘要错误、路径穿越、symlink、超预算；并发执行时升级包
    A12  缺 Python/处理库、技能硬编码服务器路径，或 Linux 服务器驱动 Windows 设备
    A13  技能消费服务器附件、知识/MCP 结果或 web_fetch 下载文件
    A27  脚本尝试读身份库、主进程配置、其他用户缓存或写技能目录；请求未授权网络/凭据

What "unmodified skill" means here
---------------------------------

``tests/fixtures/skills/summary-workbook`` and ``.../report-document`` are written
in the existing skill format (``SKILL.md`` + ``scripts/`` + assets), declare their
dependencies in frontmatter, and run **exactly as shipped**. They locate their own
resources relative to their ``SKILL.md``, so they need no adaptation at all -- the
acceptance allows resource-location adaptation, and these do not even need that.

Neither mode is faked for the benefit of the assertion:

* **local** -- the real executor endpoint (a real node process, a real loopback
  listener, a real launch token, a real ``sandbox-exec`` worker) runs the script
  inside the selected project;
* **remote** -- the real v2 command channel carries the call, and the device side
  really runs the script in *its own* root before completing the command
  (``test_desktop_remote_skill_acceptance.py``).

Both modes are run because the invariant they share is only visible from both
sides: the output lands in the execution target's directory, and the *other*
side's directory is byte-for-byte unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile

from tests import test_desktop_local_e2e as _local

REPO_ROOT = _local.REPO_ROOT
FIXTURE_SKILLS = os.path.join(REPO_ROOT, "tests", "fixtures", "skills")

WORKBOOK_SKILL = "summary-workbook"
DOCUMENT_SKILL = "report-document"

#: The three fixed synthetic records the workbook fixture encodes.
RECORDS = (100.0, 200.0, 50.0)
EXPECTED_SUMMARY = 350.0

#: The document fixture's input, chosen to exercise every placeholder.
DOCUMENT_INPUT = {
    "title": "季度验收报告",
    "customer": "示例客户",
    "project_code": "PRJ-2026-001",
    "summary": "三条记录全部通过核对。",
    "notes": "由固定夹具生成。",
}


def sha256_of(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def digest_tree(root: str) -> dict:
    """``relative path -> sha256`` for every file under ``root``."""
    found = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            found[os.path.relpath(path, root)] = sha256_of(path)
    return found


def quoted(value: str) -> str:
    """A shell word for a path that may hold a space or Chinese characters."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def xlsx_numbers(path: str):
    """The numeric cells of a workbook, read without a spreadsheet library.

    Read by the *test* rather than trusting the skill's own JSON: the assertion
    that matters is about the cell in the produced file, and a script that wrote
    the right JSON beside a wrong workbook would otherwise pass.
    """
    row = re.compile(r"<row[^>]*>(.*?)</row>", re.S)
    cell = re.compile(r"<c\s([^>]*)>(.*?)</c>", re.S)
    reference = re.compile(r'r="[A-Z]+\d+"')
    value = re.compile(r"<v>(.*?)</v>", re.S)
    not_a_number = re.compile(r't="(?:s|str|inlineStr|b|e)"')
    with zipfile.ZipFile(path) as archive:
        sheet = next(name for name in archive.namelist()
                     if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        xml = archive.read(sheet).decode("utf-8")
    found = []
    for row_body in row.findall(xml):
        for attributes, body in cell.findall(row_body):
            if not reference.search(attributes) or not_a_number.search(attributes):
                continue
            hit = value.search(body)
            if not hit:
                continue
            try:
                found.append(float(hit.group(1)))
            except ValueError:
                continue
    return found


def environment_record() -> dict:
    """The interpreter and dependency versions this evidence was produced under.

    Recorded rather than asserted: the point is that the evidence says *which*
    versions produced the outputs, so a later re-run that disagrees can be told
    apart from a re-run that merely changed.
    """
    record = {"python": sys.version.split()[0], "executable": sys.executable}
    for name in ("xlsxwriter",):
        try:
            module = __import__(name)
            record[name] = str(getattr(module, "__version__", "unknown"))
        except Exception as e:  # noqa: BLE001 - a missing dep is part of the record
            record[name] = f"MISSING ({e.__class__.__name__})"
    return record


def sandbox_available() -> bool:
    return bool(sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec")
                and shutil.which("node"))


class _SkillAcceptanceCase(_local._LocalProjectCase):
    """A real local project, real grants, and the real skill deploy path."""

    #: Every skill the acceptance identity is really granted ``skill.use`` for.
    GRANTED_SKILLS = (WORKBOOK_SKILL, DOCUMENT_SKILL)

    def setUp(self):
        # The grant comes first: the base class registers the project under this
        # account, and the launcher matches its own grant table by the same ids.
        self._auth_tmp = tempfile.TemporaryDirectory(prefix="skill-acceptance-")
        self.addCleanup(self._auth_tmp.cleanup)
        self._grant_identity()
        super().setUp()
        self.cache_root = os.path.realpath(os.path.join(self._tmp.name, "skill cache"))
        os.makedirs(self.cache_root, exist_ok=True)

    # -- a real grant -------------------------------------------------------

    def _grant_identity(self):
        """A real tenant, role and member, with real ``skill.use`` grants.

        Not a mock and not "legacy unrestricted": the deployment path asks the
        identity service whether this account may use each skill, so an acceptance
        run that bypassed that answer would be testing a deployment the product
        never performs. The ids are kept so the launcher's own grant table can name
        the same account, and so a second, narrower member can be created against
        the same tenant for the negative cases.
        """
        from unittest import mock

        import auth.service as auth_service
        from auth.service import IdentityService

        svc = IdentityService(os.path.join(self._auth_tmp.name, "identity.db"))
        svc.bootstrap(
            tenant_code="acme", tenant_name="Acme Corp",
            admin_username="root", admin_display="Root",
            admin_password="Str0ngAdminPass", shared_root="/s/acme", allow_weak=True)
        root = [u for u in svc.list_platform_users() if u["username"] == "root"][0]
        tenant = svc.list_tenants()[0]
        self.root_user_id = root["id"]
        self.tenant_id = tenant["id"]
        self.identity_service = svc

        member = self.member_with_grants(self.GRANTED_SKILLS)
        self.user_id = member["id"]
        self._auth_patch = mock.patch.object(
            auth_service, "get_identity_service", return_value=svc)
        self._auth_patch.start()
        self.addCleanup(self._auth_patch.stop)

    def member_with_grants(self, names, *, username="acceptance"):
        """A real member holding ``skill.use`` for exactly ``names``."""
        svc = self.identity_service
        grants = [
            {"resource_kind": "skill", "resource_id": f"builtin:{name}",
             "action": action}
            for name in names
            for action in ("read", "use", "edit", "enable")
        ]
        role = svc.create_role(
            self.root_user_id, self.tenant_id, f"skiller-{username}", "Skiller",
            ["skill.read", "skill.use", "skill.edit", "skill.enable"],
            resource_grants=grants)
        created = svc.create_member(
            actor_user_id=self.root_user_id, tenant_id=self.tenant_id,
            operation="create-new", username=username,
            display_name=f"Acceptance {username}",
            temporary_password="TmpPass123!", roles=[role["code"]])
        return svc._find_user_by_id(created["user_id"])

    def register(self, **overrides):
        fields = dict(user_id=self.user_id, tenant_id=self.tenant_id)
        fields.update(overrides)
        return super().register(**fields)

    def identity(self, target=None, frozen="__resolved__", user_id=None):
        from common.runtime_identity import RuntimeIdentity

        target = target or self.target()
        user_id = user_id or self.user_id
        if frozen == "__resolved__":
            entry = self.registry.lookup(
                user_id=user_id, tenant_id=self.tenant_id,
                device_id=target.device_id, workspace_id=target.workspace_id,
                binding_id=target.binding_id, grant_version=target.grant_version,
                require_mode=target.project_mode)
            frozen = entry.absolute_path if entry else ""
        return RuntimeIdentity(user_id=user_id,
                               tenant_id=self.tenant_id).derive(
            execution_target=target, execution_cwd=frozen)

    # -- the real deploy path -----------------------------------------------

    def manager(self, **overrides):
        """A real ``SkillManager`` over the fixture skills, not a stand-in."""
        from agent.skills.manager import SkillManager

        custom = os.path.join(self._tmp.name, "custom-skills")
        os.makedirs(custom, exist_ok=True)
        return SkillManager(builtin_dir=FIXTURE_SKILLS, custom_dir=custom,
                            **overrides)

    @contextlib.contextmanager
    def deployed(self, names=None, *, platform="posix", manager=None):
        """Select, deploy and pin the fixture skills through the runtime."""
        from agent.desktop_local.skill_runtime import runtime_for_identity

        manager = manager or self.manager()
        identity = self.identity()
        runtime = runtime_for_identity(
            manager, identity=identity, platform=platform,
            cache_root=self.cache_root)
        try:
            with self.signed_in(identity):
                runtime.prepare(skill_filter=names)
                yield runtime
        finally:
            runtime.release()

    def pinned(self, runtime, skill_id):
        """This run's pinned copy of ``skill_id``, or a failure naming why not.

        Matched on the trailing name because a deployed skill is identified by its
        ``source:name`` resource id (``builtin:summary-workbook``), and a caller
        that asked for a skill by name should not also have to know its source.
        """
        for deployment in runtime.deployments:
            if deployment.skill_id.split(":", 1)[-1] == skill_id:
                return deployment.path
        self.fail(f"{skill_id} 未部署：{[(p.skill_id, p.code) for p in runtime.problems]}")

    def pinned_file(self, runtime, skill_id, relative):
        path = os.path.join(self.pinned(runtime, skill_id), relative)
        self.assertTrue(os.path.exists(path), f"{relative} 不在固定版本里")
        return path

    # -- a real launcher ----------------------------------------------------

    @contextlib.contextmanager
    def launcher(self, *, skill_cache_root="__default__"):
        """The real executor endpoint, wired the way the shell wires it."""
        if not sandbox_available():
            self.skipTest("需要 macOS seatbelt 与 node 才能起真实启动器")
        from agent.desktop_local.script_executor import reset_executor_cache

        root = self.cache_root if skill_cache_root == "__default__" else skill_cache_root
        saved = {name: os.environ.pop(name, None) for name in (
            "COW_DESKTOP_EXECUTOR_URL", "COW_DESKTOP_EXECUTOR_TOKEN")}

        def restore():
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value
                else:
                    os.environ.pop(name, None)
            reset_executor_cache()

        self.addCleanup(restore)
        reset_executor_cache()
        with _local._running_executor(
                self.project, skill_cache_root=root,
                user=self.user_id, tenant=self.tenant_id) as endpoint:
            os.environ["COW_DESKTOP_EXECUTOR_URL"] = endpoint["origin"]
            os.environ["COW_DESKTOP_EXECUTOR_TOKEN"] = endpoint["token"]
            reset_executor_cache()
            yield endpoint
            reset_executor_cache()

    def run_shell(self, command, *, skill_roots=(), timeout=120):
        """Run one real command through the isolated script tool."""
        tools, view = self.run_view(skill_roots=skill_roots)
        result = view["bash"].execute({"command": command, "timeout": timeout})
        self.assertEqual(result.status, "success", result.result)
        return result

    def command(self, script, *arguments):
        return " ".join([sys.executable, quoted(script)]
                        + [quoted(value) for value in arguments])

    # -- shared assertions --------------------------------------------------

    def project_path(self, *parts):
        return os.path.join(self.project, *parts)

    def assert_server_untouched(self):
        """No local run may change the Agent's own directory."""
        self.assertEqual(sorted(os.listdir(self.server_root)), ["个人工作区.md"])
        with open(os.path.join(self.server_root, "个人工作区.md"), "rb") as handle:
            self.assertEqual(handle.read(), self.sentinel_bytes)

    def assert_produced_in_project(self, produced):
        """Every produced path is the project's, and none is in the cache.

        The skill cache is read-only by contract (task 8.4), so a run that
        "produced" a file inside it would be writing where the next run reads.
        """
        for path in produced:
            real = os.path.realpath(path)
            self.assertTrue(real.startswith(self.project + os.sep),
                            f"{real} 不在项目里")
            self.assertFalse(real.startswith(self.cache_root + os.sep),
                             f"{real} 落在技能缓存里")

    def write_document_input(self):
        path = self.project_path("验收 输入", "文档输入.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(DOCUMENT_INPUT, handle, ensure_ascii=False)
        return path


class LocalModeAcceptanceTests(_SkillAcceptanceCase):
    """A06 / A07 with the script really executed by the platform launcher."""

    def test_a06_the_fixed_workbook_skill_summarises_to_350_in_the_project(self):
        """A06 (local): 汇总单元格为 350、报告在项目里、输入摘要不变。"""
        with self.deployed([WORKBOOK_SKILL]) as runtime:
            roots = runtime.roots()
            make_input = self.pinned_file(
                runtime, WORKBOOK_SKILL, "scripts/make_input.py")
            summarize = self.pinned_file(
                runtime, WORKBOOK_SKILL, "scripts/summarize.py")
            workbook = self.project_path("验收 输入", "输入工作簿.xlsx")
            output_dir = self.project_path("验收 产出", "工作簿")

            with self.launcher():
                self.run_shell(self.command(make_input, "--output", workbook),
                               skill_roots=roots)
                self.assertTrue(os.path.exists(workbook), "输入工作簿没有落在项目里")
                digest_before = sha256_of(workbook)
                self.run_shell(self.command(summarize, "--input", workbook,
                                            "--output", output_dir),
                               skill_roots=roots)

            # The report really is in the selected project, not in the cache and
            # not in the Agent's own directory.
            report = os.path.join(output_dir, "汇总报告.xlsx")
            result_json = os.path.join(output_dir, "汇总结果.json")
            self.assertTrue(os.path.exists(report), "报告不在所选项目里")
            self.assertTrue(os.path.exists(result_json))
            self.assert_produced_in_project([report, result_json])
            self.assert_server_untouched()

            # The summary cell, read from the produced workbook itself.
            with open(result_json, encoding="utf-8") as handle:
                result = json.load(handle)
            self.assertEqual(result["records"], list(RECORDS))
            self.assertEqual(result["summary"], EXPECTED_SUMMARY)
            numbers = xlsx_numbers(report)
            self.assertIn(EXPECTED_SUMMARY, numbers, f"汇总单元格不是 350：{numbers}")

            # The input's bytes are unchanged: the script read it, never rewrote it.
            self.assertEqual(sha256_of(workbook), digest_before, "输入工作簿被改写了")

    def test_a07_the_document_skill_renders_from_its_own_template(self):
        """A07 (local): 模板资源齐全、输出字段正确、技能/记忆目录未搬进项目。"""
        with self.deployed([DOCUMENT_SKILL]) as runtime:
            roots = runtime.roots()
            template = self.pinned_file(runtime, DOCUMENT_SKILL, "assets/template.md")
            auxiliary = self.pinned_file(runtime, DOCUMENT_SKILL, "references/fields.md")
            render = self.pinned_file(runtime, DOCUMENT_SKILL, "scripts/render.py")

            # "模板资源齐全" is about the *deployed* package, so it is checked
            # there rather than assumed from the source tree.
            for path in (template, auxiliary):
                self.assertTrue(os.path.getsize(path) > 0, f"{path} 是空的")
            with open(template, encoding="utf-8") as handle:
                self.assertIn("{{title}}", handle.read())
            with open(auxiliary, encoding="utf-8") as handle:
                # The auxiliary resource is what documents the fields, so it is
                # asserted to actually carry them rather than merely exist.
                self.assertIn("{{project_code}}", handle.read())

            input_json = self.write_document_input()
            output_dir = self.project_path("验收 产出", "文档")
            with self.launcher():
                self.run_shell(self.command(render, "--input", input_json,
                                            "--output", output_dir),
                               skill_roots=roots)

            document = os.path.join(output_dir, "报告.md")
            rendered_json = os.path.join(output_dir, "渲染结果.json")
            self.assertTrue(os.path.exists(document), "报告文档不在所选项目里")
            with open(document, encoding="utf-8") as handle:
                text = handle.read()
            self.assert_produced_in_project([document, rendered_json])
            self.assert_server_untouched()

            # Every field came from the input, and no placeholder survived.
            for field, value in DOCUMENT_INPUT.items():
                self.assertIn(value, text, f"字段 {field} 的值没有进文档")
            self.assertNotIn("{{", text, "模板占位符没有被替换")

            # The skill and memory directories were not copied into the project:
            # running a skill reads it where it lives, it does not relocate it.
            entries = os.listdir(self.project)
            for forbidden in ("SKILL.md", "assets", "references", "scripts",
                              "memory", "记忆", ".skills"):
                self.assertNotIn(forbidden, entries, f"{forbidden} 被搬进了项目")

    def test_the_pinned_version_is_unchanged_by_running_it(self):
        """A skill runs *from* its pinned version; those bytes never move.

        Asserted in both modes because it is the precondition of the cache being
        safe to share: a script that rewrote itself would make every later run's
        digest assertion a statement about a different package.
        """
        with self.deployed([WORKBOOK_SKILL, DOCUMENT_SKILL]) as runtime:
            root = self.pinned(runtime, DOCUMENT_SKILL)
            before = digest_tree(root)
            self.assertTrue(before, "固定版本里没有资源")

            with self.launcher():
                self.run_shell(self.command(
                    self.pinned_file(runtime, DOCUMENT_SKILL, "scripts/render.py"),
                    "--input", self.write_document_input(), "--output",
                    self.project_path("验收 产出", "只读核对")),
                    skill_roots=runtime.roots())

            self.assertEqual(before, digest_tree(root), "运行改动了固定版本的字节")

    def test_a12_a_script_reading_the_skill_cache_is_allowed_but_never_writes_it(self):
        """A12/A27 (local): 技能目录可读、绝不可写，且只在被授权时。

        The read-only rule is enforced by the OS rather than by bookkeeping, so it
        is checked by trying: a write into the pinned version must fail.
        """
        with self.deployed([DOCUMENT_SKILL]) as runtime:
            roots = runtime.roots()
            target = os.path.join(self.pinned(runtime, DOCUMENT_SKILL), "被脚本写入.txt")
            tools, view = self.run_view(skill_roots=roots)
            sentinel = "不该出现的内容"

            with self.launcher():
                result = view["bash"].execute({
                    "command": f"printf %s {quoted(sentinel)} > {quoted(target)}",
                    "timeout": 60,
                })
                # The sandbox refuses the write; whether the launcher reports it as
                # an error or a non-zero exit, the file must not exist.
                self.assertFalse(os.path.exists(target),
                                 "脚本写入了只读的技能目录")
                self.assertNotIn(sentinel, str(result.result or ""))

                # Reading the same directory is allowed, so the refusal above is a
                # refusal and not "the cache is unreachable at all".
                read = self.run_shell(
                    f"cat {quoted(os.path.join(roots[0], 'SKILL.md'))}",
                    skill_roots=roots)
                self.assertIn("report-document", str(read.result))


class SkillGrantBoundaryTests(_SkillAcceptanceCase):
    """A11 / A12 / A13 / A27 at the seam where a run asks for a directory."""

    def test_a27_a_script_may_not_read_the_agent_workspace(self):
        """A27 (local): 脚本读不到服务器工作区与身份库。"""
        if not sandbox_available():
            self.skipTest("需要真实沙箱才能验证读取被拒绝")
        workspace_file = os.path.join(self.server_root, "个人工作区.md")
        self.assertTrue(os.path.exists(workspace_file))
        tools, view = self.run_view(skill_roots=())

        with self.launcher():
            result = view["bash"].execute({
                "command": f"cat {quoted(workspace_file)}", "timeout": 60})
            self.assertNotIn("个人默认工作区内容", str(result.result or ""),
                             "脚本读到了服务器工作区")

    def test_a12_a_missing_dependency_is_named_by_skill_module_and_distribution(self):
        """A12 (local): 缺处理库时报出技能名、模块名与发行名。"""
        from agent.skills.dependencies import LOCK_PATH_HINT, check_skills

        manager = self.manager()
        entry = manager.filter_skills([WORKBOOK_SKILL])[0]
        with open(os.path.join(REPO_ROOT, LOCK_PATH_HINT), encoding="utf-8") as handle:
            shipped = handle.read()

        # The shipped lock satisfies the declaration: without this half, the
        # failing half would only prove the check can say "missing".
        self.assertEqual(check_skills([entry], shipped), {})

        # A lock that does not ship it names the skill, the module and the file
        # the author has to edit.
        problems = check_skills([entry], "")
        self.assertIn(WORKBOOK_SKILL, problems)
        missing = [p for p in problems[WORKBOOK_SKILL] if p.code == "dependency_missing"]
        self.assertTrue(missing, problems)
        self.assertIn("xlsxwriter", missing[0].message, missing[0].message)
        self.assertIn(LOCK_PATH_HINT, missing[0].message, missing[0].message)

    def test_a12_a_skill_the_identity_may_not_use_is_not_deployed(self):
        """A12/A27 (local): 没授权的技能既不入包，也不落到设备上。

        The real service answers, so this is the product's own decision rather
        than a stand-in's. The acceptance account is granted both fixture skills
        and deploys both; a second real member granted only the workbook gets the
        workbook and *not* the document -- and the document's bytes are not in
        that account's cache either.
        """
        from agent.desktop_local.skill_runtime import runtime_for_identity

        with self.deployed() as runtime:
            deployed = {d.skill_id.split(":", 1)[-1] for d in runtime.deployments}
            self.assertEqual(deployed, set(self.GRANTED_SKILLS))

        narrow = self.member_with_grants(
            [WORKBOOK_SKILL], username="workbook-only")
        narrow_cache = os.path.realpath(os.path.join(self._tmp.name, "narrow cache"))
        identity = self.identity(user_id=narrow["id"])
        runtime = runtime_for_identity(
            self.manager(), identity=identity, cache_root=narrow_cache)
        try:
            with self.signed_in(identity):
                runtime.prepare()
            deployed = {d.skill_id.split(":", 1)[-1] for d in runtime.deployments}
            self.assertEqual(deployed, {WORKBOOK_SKILL},
                             "未授权的技能被部署了")
            self.assertFalse(self.cache_has(DOCUMENT_SKILL, narrow_cache),
                             "未授权的技能写进了缓存")
        finally:
            runtime.release()

    def cache_has(self, skill_name: str, root=None) -> bool:
        """Whether a skill's *bytes* reached the cache, by content not by path.

        The version directory is named after the package digest and the scope, so
        looking for a directory named after the skill would be looking for
        something the cache never creates. What the acceptance is about is whether
        the skill's own files landed, so that is what is searched for.
        """
        marker = os.path.join(FIXTURE_SKILLS, skill_name, "SKILL.md")
        with open(marker, "rb") as handle:
            expected = hashlib.sha256(handle.read()).hexdigest()
        for base, _dirs, files in os.walk(root or self.cache_root):
            for name in files:
                candidate = os.path.join(base, name)
                try:
                    if sha256_of(candidate) == expected:
                        return True
                except OSError:  # pragma: no cover - a vanished file is not a hit
                    continue
        return False

    def test_a12_an_unsupported_platform_is_reported_not_silently_skipped(self):
        """A12 (local): 平台不兼容时既不静默跳过，也不假装装上了。"""
        from agent.desktop_local.skill_runtime import runtime_for_identity

        manager = self.manager()
        identity = self.identity()
        runtime = runtime_for_identity(
            manager, identity=identity, platform="win32", cache_root=self.cache_root)
        try:
            with self.signed_in(identity):
                skills = runtime.prepare(skill_filter=[DOCUMENT_SKILL])
            # Either it deployed (the skill supports win32) or it reported a
            # problem naming itself: what is forbidden is the silent middle.
            self.assertTrue(runtime.deployments or runtime.problems,
                            "既不部署也不报告，是静默跳过")
            for problem in runtime.problems:
                self.assertTrue(problem.skill_id, problem)
                self.assertTrue(problem.code, problem)
                self.assertTrue(problem.message, problem)
            if runtime.problems:
                self.assertTrue(runtime.failure_message(), "问题没有面向模型的说明")
            else:
                self.assertTrue(skills.roots())
        finally:
            runtime.release()

    def test_a13_a_server_absolute_path_is_refused_not_translated(self):
        """A13 (local): 服务器路径不会被翻译成客户端路径。"""
        from agent.desktop_local.resource_refs import ResourceRefError, resolve_ref

        server_path = os.path.join(self.server_root, "个人工作区.md")
        self.assertTrue(os.path.exists(server_path), "服务端文件应当存在")

        for raw in (server_path, f"backend:{server_path}"):
            with self.assertRaises(ResourceRefError) as caught:
                resolve_ref(raw, project_root=self.project)
            self.assertEqual(caught.exception.code, "server_path_not_local", raw)
            # The refusal names the path: an unexplained "not found" would send
            # the model looking for a same-named local file.
            self.assertIn(os.path.basename(server_path), caught.exception.message)

    def test_a13_a_landed_server_input_is_read_from_the_project(self):
        """A13 (local): 落到项目里的服务器输入按项目引用读得到。

        The landing half is 8.6's evidence; what this pins is that the *resolution*
        a skill uses afterwards is an ordinary project reference -- so a landed
        attachment is consumed as ``project:<relative>`` and not through a server
        path that happens to exist on this machine.
        """
        from agent.desktop_local.resource_refs import resolve_ref

        landed = self.project_path("运行时 输入", "最新对账表.xlsx")
        os.makedirs(os.path.dirname(landed), exist_ok=True)
        with open(landed, "wb") as handle:
            handle.write(b"landed-bytes")

        resolved = resolve_ref("project:运行时 输入/最新对账表.xlsx",
                               project_root=self.project)
        self.assertEqual(os.path.realpath(resolved.absolute), os.path.realpath(landed))
        self.assertTrue(resolved.exists)

    def test_a11_a_skill_root_outside_the_cache_is_refused_by_the_launcher(self):
        """A11 (local): 指向缓存之外的技能目录被拒绝，不是照单全收。

        The backend has to name its pinned directories, so the interesting case is
        a request naming one *outside* the shell's cache root. It is refused with
        its own code -- refused rather than dropped, because a silently partial
        grant produces a sandboxed run that fails for a reason nobody can see.
        """
        if not sandbox_available():
            self.skipTest("需要真实启动器才能验证目录被拒绝")
        from agent.desktop_local.script_executor import (
            reset_executor_cache, run_script, script_scope,
        )

        def restore():
            for name in ("COW_DESKTOP_EXECUTOR_URL", "COW_DESKTOP_EXECUTOR_TOKEN"):
                os.environ.pop(name, None)
            reset_executor_cache()

        self.addCleanup(restore)
        reset_executor_cache()
        with _local._running_executor(
                self.project, skill_cache_root=self.cache_root,
                user=self.user_id, tenant=self.tenant_id) as endpoint:
            os.environ["COW_DESKTOP_EXECUTOR_URL"] = endpoint["origin"]
            os.environ["COW_DESKTOP_EXECUTOR_TOKEN"] = endpoint["token"]
            reset_executor_cache()
            identity = self.identity()
            target = identity.execution_target

            with self.signed_in(identity):
                # Outside the anchor: the Agent's own directory.
                escaped = script_scope(identity, target,
                                       skill_roots=[self.server_root])
                payload, refusal = run_script(
                    tool_name="bash", arguments={"command": "echo hi"},
                    scope=escaped, cwd=self.project, timeout_ms=20000)
                self.assertIsNone(payload, "越界目录被接受了")
                self.assertTrue(refusal, "越界目录没有给出拒绝理由")

                # No anchor at all: the same refusal, because a shell that has not
                # said where its cache lives cannot authorize any directory.
                no_anchor = script_scope(identity, target,
                                         skill_roots=[self.cache_root])
                with _local._running_executor(
                        self.project, user=self.user_id,
                        tenant=self.tenant_id) as bare:
                    os.environ["COW_DESKTOP_EXECUTOR_URL"] = bare["origin"]
                    os.environ["COW_DESKTOP_EXECUTOR_TOKEN"] = bare["token"]
                    reset_executor_cache()
                    payload2, refusal2 = run_script(
                        tool_name="bash", arguments={"command": "echo hi"},
                        scope=no_anchor, cwd=self.project, timeout_ms=20000)
                self.assertIsNone(payload2, "没有锚点时目录仍被授予")
                self.assertTrue(refusal2)
            reset_executor_cache()


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
