# encoding:utf-8
"""A06/A07 in *remote* mode: what the channel carries, and where it stops.

The acceptance line says "本地与远程各运行固定 Excel 技能" and "本地与远程各运行带
模板和辅助资源的文档技能". This file records, with tests rather than prose, exactly
how far the remote half gets today:

**What holds.** The device's execution request carries a *digest set* of the skills
the run expects (``skill_resources``), the digest is a function of that set (so a set
cannot be swapped after the fact), a malformed entry is refused instead of dropped,
and the device journals the digests it ran against. The answer the server reports is
the device's own, never a local re-run.

**What the server now also does (task 8.9).** The set the run was authorized with is
*persisted* on the command row, carried in the ``execute_tool`` frame, and required
back from the device in both directions: a different version, a subset, or no
declaration at all is refused ``incompatible_skill``. Before this, every stored
digest had been computed over an empty set, so a device declaring nothing was
accepted and a run could not be required to use a particular version.

**What the device now also does (task 8.9).** It holds a digest-keyed cache of
verified versions, obtains a version it is missing over the native channel
(``GET /api/desktop/execution/skill-package``, bound to the command by its
``params_digest`` and to the set that command was authorized with), verifies the
package against the declared digest *before* writing anything, and hands the
resulting directories to its sandboxed worker as read-only roots -- the same
mechanism the local path uses, so both ends grant the same thing the same way.

The remote A06/A07 run is therefore no longer blocked by a missing mechanism. What
remains for those two lines is *installer-level* evidence (tasks 10.1-10.3): the
packaged desktop build, on a real device, running the fixture skills end to end.
That is a different gap and is tracked there, not here.
"""

from __future__ import annotations

import hashlib
import os
import unittest

from tests import test_desktop_local_e2e as _local

REPO_ROOT = _local.REPO_ROOT

#: A syntactically valid digest set, as a device would declare for a 350-sum run.
FIXTURE_DIGEST = "sha256:" + hashlib.sha256(b"summary-workbook@pinned").hexdigest()

#: The fields `envelope_for_command` needs for a well-formed v2 command.
COMMAND_FIELDS = dict(
    tool="bash", tool_schema_version=1, arguments={"command": "echo hi"},
    run_id="r1", tool_call_id="t1", session_id=None, agent_id=None,
    origin="https://master.test", binding_id="b1", workspace_id="w1",
    device_id="d1", grant_version=1, selection_generation=0)


def envelope(**overrides):
    from integrations.desktop import execution_payload

    fields = dict(COMMAND_FIELDS)
    fields.update(overrides)
    return execution_payload.envelope_for_command(**fields)


class RemoteSkillResourceChannelTests(unittest.TestCase):
    """The half of remote skill delivery that is really wired."""

    def test_a06_the_digest_is_a_function_of_the_declared_skill_set(self):
        """A set cannot be swapped after authorization: the digest would differ.

        This is what makes "the device declares a set the server never authorized"
        a detectable conflict instead of an unchecked claim.
        """
        from integrations.desktop import execution_payload

        empty = execution_payload.params_digest(envelope(resources=[]))
        declared = execution_payload.params_digest(envelope(resources=[
            {"skill_id": "builtin:summary-workbook", "digest": FIXTURE_DIGEST}]))

        self.assertNotEqual(empty, declared,
                            "声明了技能集却得到同一个摘要，设备换了集也看不出来")
        # Two different *versions* of the same skill must also differ: otherwise
        # a device could run an older package and still reproduce the digest.
        other = execution_payload.params_digest(envelope(resources=[
            {"skill_id": "builtin:summary-workbook",
             "digest": "sha256:" + "2" * 64}]))
        self.assertNotEqual(declared, other)

    def test_a06_two_devices_listing_the_same_versions_agree(self):
        """Order must not matter, or the digest becomes a source of false conflicts."""
        from integrations.desktop import execution_payload

        entries = [{"skill_id": "builtin:a", "digest": FIXTURE_DIGEST},
                   {"skill_id": "builtin:b", "digest": FIXTURE_DIGEST}]
        forward = execution_payload.params_digest(envelope(resources=entries))
        backward = execution_payload.params_digest(
            envelope(resources=list(reversed(entries))))

        self.assertEqual(forward, backward)

    def test_a06_a_malformed_skill_resource_is_refused_not_ignored(self):
        """A resource with no digest is refused, not dropped into an empty set."""
        from integrations.desktop import execution_payload

        for bad in ([{"skill_id": "builtin:x"}], [FIXTURE_DIGEST], ["x"]):
            with self.assertRaises(ValueError, msg=str(bad)):
                execution_payload.skill_digests(bad)

    def test_a06_the_device_journals_the_declared_digests_it_ran_against(self):
        """The device records *which* versions it ran against, not just "it ran".

        Read from the device's own source, because the journal is what a later
        reconciliation reads when a run's result is disputed.
        """
        source = os.path.join(REPO_ROOT, "desktop", "src", "main",
                              "project-execution", "command-runner.ts")
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("skill_resources", text, "设备不再读取声明的技能集")
        self.assertIn("skill_digests", text, "设备没有把技能摘要写进日志")


class RemoteSkillDeliveryIsWiredTests(unittest.TestCase):
    """The device half, pinned so it cannot quietly go missing again.

    This class used to assert the *absence* of the device-side mechanism. Both of
    the facts it pinned have flipped, so it pins the positive form instead: a
    future change that removes the cache, the transfer or the read-only grant
    fails here rather than silently reopening the gap.

    Behaviour is pinned where behaviour lives: the server exchange is covered by
    ``tests/test_desktop_remote_skill_delivery.py`` (the bytes come back and hash
    to the declared digest), and the cache, the package digest and the install
    path by the Node suites ``tests/test_desktop_skill_cache.cjs`` and
    ``tests/test_desktop_skill_package_digest.cjs``. What this file adds is that
    the device *sources* are wired to each other.
    """

    def source(self, *parts):
        path = os.path.join(REPO_ROOT, *parts)
        self.assertTrue(os.path.exists(path), f"缺少 {os.path.join(*parts)}")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_a06_the_device_has_a_digest_keyed_skill_cache(self):
        """Declared digests are checked against a real cache, not journalled."""
        cache = self.source("desktop", "src", "main", "project-execution",
                            "skill-cache.ts")
        self.assertIn("resolveDeclared", cache,
                      "设备侧缓存不再有“按声明摘要解析”的闸门")
        self.assertIn("install", cache, "设备侧缓存不再有按摘要校验的安装入口")
        execution = self.source("desktop", "src", "main", "project-execution",
                                "device-execution.ts")
        self.assertIn("verifySkills", execution, "执行帧不再校验声明的技能版本")
        self.assertIn("incompatible_skill", execution,
                      "技能版本不符时不再以 incompatible_skill 拒绝")

    def test_a06_the_device_can_obtain_a_version_it_does_not_hold(self):
        """Without this the requirement is unenforceable: nothing would arrive."""
        transfer = self.source("desktop", "src", "main", "project-execution",
                               "skill-transfer.ts")
        self.assertIn("/api/desktop/execution/skill-package", transfer,
                      "设备不再从这个协议端点取技能包")
        self.assertIn("ensureSkillVersions", transfer,
                      "设备不再有“补齐声明版本”的入口")
        assembly = self.source("desktop", "src", "main", "remote",
                               "local-read-assembly.ts")
        self.assertIn("fetchSkills", assembly, "设备端点不再接线技能包获取")
        self.assertIn("skillCacheRoot", assembly,
                      "设备不再为自己的技能缓存配置根目录")

    def test_a06_the_device_gives_the_pinned_versions_to_the_worker_read_only(self):
        """The same mechanism the local path uses (8.8), applied to the device."""
        assembly = self.source("desktop", "src", "main", "remote",
                               "local-read-assembly.ts")
        # The roots come from the cache, and they reach the launcher's grant.
        self.assertIn("skillRootsFor", assembly, "设备不再解析本轮的只读技能根")
        self.assertIn("skillRoots", assembly, "设备不再把技能根交给启动器")
        # And a worker is keyed by its root set: the sandbox profile is built
        # once per worker, so a different pin set has to be a different worker.
        self.assertIn("skillSetKey", assembly,
                      "worker 不再按根集区分，会继承上一轮的授权面")

    def test_a06_the_server_serves_only_the_version_the_command_authorized(self):
        """The server half of the transfer, in the source that decides it.

        The behavioural proof is in ``test_desktop_remote_skill_delivery.py``;
        what is pinned here is that the decision is made against the *command's
        recorded set* rather than against "a version this server has".
        """
        source = " ".join(self.source("integrations", "desktop",
                                      "execution_broker.py").split())
        self.assertIn("def skill_package", source,
                      "服务端不再提供技能包端点")
        self.assertIn("canonical_skill_resources", source,
                      "技能包端点不再以命令行的授权集合为准")
        self.assertIn("manifest.digest != digest", source,
                      "服务端不再重算摘要，可能把另一个版本按旧名发出")

    def test_a06_the_server_now_stores_and_requires_the_authorized_set(self):
        """The server half of the gap is **closed** (task 8.9).

        What used to be here asserted that the set was validated but never
        persisted, and that the comment said so. Both are now false, and this
        test says which way round they are instead of being deleted: the row
        stores the canonical set, and the broker compares the device's
        declaration against it in *both* directions.
        """
        from integrations.desktop import execution_payload

        source = os.path.join(REPO_ROOT, "integrations", "desktop",
                              "execution_broker.py")
        with open(source, encoding="utf-8") as handle:
            text = " ".join(handle.read().split())
        self.assertNotIn("until then every command's set is empty", text,
                         "服务端的技能集仍被当成空集处理")
        self.assertIn("canonical_skill_resources", text,
                      "服务端没有把声明的技能集规范化后与行上的集合比对")

        commands_text = " ".join(self.source("integrations", "desktop",
                                             "commands.py").split())
        self.assertIn("skill_resources", commands_text,
                      "命令行不再保存技能集")
        # The canonical form is one function, used for the digest *and* the
        # stored value, or "the set the digest covers" and "the set the device is
        # handed" could drift into two different lists.
        self.assertIsNotNone(execution_payload.canonical_skill_resources)

    def test_a06_the_remote_run_is_no_longer_blocked_by_a_missing_mechanism(self):
        """The blocker is gone, and the skip that named it is gone with it.

        This test used to ``skipTest`` with the missing pieces as its reason. The
        mechanism now exists -- the set is recorded and required, the device can
        pull a version, verifies it against the declared digest and mounts it
        read-only -- so the skip would be a claim about the code that is no longer
        true. What the remote A06/A07 line still needs is the packaged,
        installer-level run, and that is tracked under tasks 10.1-10.3 rather than
        as an unwired path here.
        """
        self.assertTrue(
            os.path.exists(os.path.join(REPO_ROOT, "desktop", "src", "main",
                                        "project-execution", "skill-cache.ts")),
            "设备侧技能缓存不存在，远程技能交付仍不通")


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
