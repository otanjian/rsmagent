# encoding:utf-8
"""Remote skill delivery, server half (task 8.9).

Change ``align-desktop-project-execution-with-master``. Task 8.8 ran the
representative skills on the *local* path and found the remote path could not run
them at all. Three pieces were missing; this file covers the first, which the
other two depend on:

**The server never recorded which skills a run was authorized with.** A command's
``params_digest`` covers the skill set, so it was *checked* -- but the set was
never *stored*. ``dispatch.enqueue`` passed ``resources=None``, and
``_enqueue_command`` had no column for it. Two consequences, both asserted below:

* the ``execute_tool`` frame carries ``skill_resources`` from the row, so with no
  stored set the device was never told which versions the run expected;
* because the stored digest had been computed over an *empty* set, a device
  declaring skills could only ever be told "conflict", while a device declaring
  **nothing** was accepted. That is the worst of the two: it meant a run could
  not be *required* to use a particular version, so a device with a stale
  snapshot would quietly run whatever it had.

The rule this file pins, in both directions:

* a run with a skill set must be driven from the frame, and the device must
  declare *that* set -- a different version is ``incompatible_skill``, and
  declaring nothing is refused too, not accepted as "no requirements";
* a run without a skill set stays exactly as it was: no set on the row, no set
  in the frame, and an empty declaration still accepted. Backward compatibility
  here is not politeness -- existing v1 commands and every skill-less v2 tool
  must keep working through the same writer.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import unittest
from unittest.mock import patch

from tests._helpers import WebAppHarness

CLIENT_ID = "cowagent-desktop"
REDIRECT_URI = "http://127.0.0.1:52345/callback"
VERIFIER = "M" * 43
ORIGIN = "https://console.test"
SESSION = "biz-skill-session"
NONCE = "nonce_" + ("s" * 22)
AGENT = "skill-agent"

PREPARE = "/api/desktop/execution/prepare"
START = "/api/desktop/execution/start"

#: The two deployment switches the endpoint gate composes (task 11.2).
_SWITCHES = ("desktop_project_execution_enabled",
             "desktop_project_scripts_enabled")


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


def digest_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


#: One skill version, as the server would pin it for a run. Listed in the
#: canonical order (by ``skill_id``) the digest uses, so an equality assertion
#: here does not accidentally depend on which end sorted the list.
SKILL_SET = [
    {"skill_id": "builtin:report-document", "digest": digest_of("report@1")},
    {"skill_id": "builtin:summary-workbook", "digest": digest_of("summary@1")},
]


class _EnabledSlice:
    """A slice that is implemented, accepted and switched on.

    All four fields matter: ``availability`` composes them into a reason, and a
    stub missing one raises rather than reporting "available".
    """

    enabled = True
    implemented = True
    accepted = True

    def is_open(self, action):
        return True


class _SkillDeliveryHarness(unittest.TestCase):
    """The real service, real store, real HTTP handlers -- no stubs of either end."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="remote-skill-")
        cls.app = WebAppHarness(__import__("os").path.join(cls._tmp.name, "instance"))
        cls.app.add_agent(AGENT)
        cls.app.role("skill-role",
                     ["chat.use", "agent.use", "agent.read", "skill.use"],
                     grants=[("agent", "agent:%s" % AGENT, "use"),
                             # The package endpoint re-checks ``skill.use`` live,
                             # so the role that may run these runs must be able to
                             # name the skill being pulled -- otherwise the test
                             # would be measuring the grant, not the transfer.
                             ("skill", REAL_SKILL, "use")])
        cls.u1 = cls.app.member("skill-u1", ["skill-role"])
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(
            lambda agent_id, session_id: cls.u1 if session_id == SESSION else None)

    @classmethod
    def tearDownClass(cls):
        from integrations.desktop import access as access_mod
        access_mod.set_business_session_lookup(None)
        cls.app.close()
        cls._tmp.cleanup()

    def setUp(self):
        # The declaration half: the real slices ship ``accepted=False`` until the
        # acceptance suites run, and these tests are *of* that acceptance path.
        self._patch = patch("auth.capability_matrix.slice_for",
                            side_effect=lambda name: _EnabledSlice())
        self._patch.start()
        self.addCleanup(self._patch.stop)

        # The deployment half: the switches are off by default (task 11.2) and
        # the endpoint gate reads them from the live settings, so they are
        # opened the way an operator would rather than patched at the call site.
        settings = self.app._settings
        before = {key: settings.get(key) for key in _SWITCHES}
        settings.update({key: True for key in _SWITCHES})

        def restore():
            for key, value in before.items():
                if value is None:
                    settings.pop(key, None)
                else:
                    settings[key] = value

        self.addCleanup(restore)
        self._fixture = None

    # -- harness ------------------------------------------------------------

    def native(self):
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        web_token = self.app.login("skill-u1")
        started = desktop.begin(
            session_token=web_token, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, code_challenge=_challenge(),
            code_challenge_method="S256")
        confirmed = desktop.confirm(
            request_id=started["request_id"], csrf=started["csrf"],
            session_token=web_token)
        return desktop.exchange(
            code=confirmed["code"], verifier=VERIFIER, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, origin=ORIGIN)["token"]

    def paired(self):
        from auth.desktop_web_session import service_for
        native = self.native()
        child = service_for(self.app.service).bootstrap(
            native_token=native, bootstrap_id=secrets.token_urlsafe(18),
            instance_id=secrets.token_urlsafe(18), web_protocol=1, origin=ORIGIN)
        return native, child["web_token"]

    def binding(self):
        if self._fixture is not None:
            return self._fixture
        from integrations.desktop.commands import service_for as commands_for
        from integrations.desktop.devices import service_for as devices_for

        native, web = self.paired()
        devices = devices_for(self.app.service)
        device = devices.register_device(
            token=native, installation_id="install_" + secrets.token_urlsafe(18),
            display_name="SkillBox", platform="macos", client_version="2.1.9")
        binding = devices.create_binding(
            token=web, tenant_id=self.app.tenant_id, device_id=device["id"],
            agent_id=AGENT, business_session_id=SESSION, context_nonce=NONCE)
        workspace = devices.register_workspace(
            token=native, tenant_id=self.app.tenant_id, device_id=device["id"],
            label="我的 项目", grant_version=1, project_mode="project-execution")
        devices.bind_workspace(
            token=native, tenant_id=self.app.tenant_id, binding_id=binding["id"],
            workspace_id=workspace["id"], grant_version=1)
        cmds = commands_for(self.app.service)
        lease = cmds.acquire_lease(
            token=native, device_id=device["id"],
            gateway_id="gw-skill-" + secrets.token_urlsafe(6), protocol_major=2)
        self._fixture = (native, web, device, binding, workspace, lease, cmds)
        return self._fixture

    def enqueue(self, **overrides):
        _native, web, _device, binding, workspace, _lease, cmds = self.binding()
        kwargs = dict(
            token=web, tenant_id=self.app.tenant_id, binding_id=binding["id"],
            workspace_id=workspace["id"], grant_version=1, tool="bash",
            arguments={"command": "python3 -c \"print(1)\"", "timeout": 120},
            run_id="run_" + secrets.token_hex(4),
            tool_call_id="call_" + secrets.token_hex(4),
            selection_generation=1, origin=ORIGIN,
            request_id="req_" + secrets.token_hex(6),
        )
        kwargs.update(overrides)
        return cmds.create_execution(**kwargs)

    def claimed(self, command):
        _native, _web, device, _binding, _ws, lease, cmds = self.binding()
        cmds.claim_outbox(device_id=device["id"], epoch=lease["epoch"])
        return cmds.acknowledge(command_id=command["id"], epoch=lease["epoch"])

    def body(self, command, **overrides):
        _native, _web, device, binding, workspace, lease, _cmds = self.binding()
        data = {
            "command_id": command["id"],
            "device_id": device["id"],
            "binding_id": binding["id"],
            "workspace_id": workspace["id"],
            "grant_version": 1,
            "params_digest": command["params_digest"],
            "connection_epoch": lease["epoch"],
        }
        data.update(overrides)
        return data

    def call(self, path, body, *, token=None):
        headers = {"Authorization": "Bearer " + (token or self.native())}
        return self.app.request(path, "POST", body=body, headers=headers)

    def ok(self, response):
        payload = json.loads(response.data.decode("utf-8"))
        self.assertTrue(str(response.status).startswith("200"),
                        "%s: %s" % (response.status, payload))
        self.assertEqual(payload.get("status"), "success", payload)
        return payload["data"]

    def refusal(self, response, *, status, code):
        payload = json.loads(response.data.decode("utf-8"))
        self.assertTrue(str(response.status).startswith(str(status)),
                        "%s: %s" % (response.status, payload))
        self.assertEqual(payload.get("code"), code, payload)
        return payload


class TheAuthorizedSetIsRecordedTests(_SkillDeliveryHarness):
    """Gap ③: the set is *checked* by the digest but was never *stored*."""

    def test_a_command_records_the_skill_set_the_run_was_authorized_with(self):
        created = self.enqueue(skill_resources=SKILL_SET)

        self.assertEqual(created.get("skill_resources"), SKILL_SET)

    def test_the_skill_set_survives_a_row_reload_not_just_the_return_value(self):
        """Read back through the store, so "echoed" cannot pass for "persisted"."""
        created = self.enqueue(skill_resources=SKILL_SET)
        _native, _web, _device, _binding, _workspace, _lease, cmds = self.binding()

        reloaded = cmds.get_command(
            token=self.binding()[0], tenant_id=self.app.tenant_id,
            command_id=created["id"])

        self.assertEqual(reloaded.get("skill_resources"), SKILL_SET)

    def test_the_device_frame_carries_the_authorized_skill_set(self):
        """Without this the device is never told which versions the run expects."""
        from integrations.desktop import execution_payload

        created = self.enqueue(skill_resources=SKILL_SET)
        # Built from the *claimed* durable row through the real gateway builder,
        # so this asserts the frame the device is actually handed.
        claimed = self.claimed(created)

        frame = execution_payload.device_execution_frame(claimed)

        self.assertEqual(frame.get("skill_resources"), SKILL_SET)

    def test_the_digest_changes_once_the_set_is_recorded(self):
        """The stored digest must be *over* the set, or the check is vacuous."""
        with_skills = self.enqueue(skill_resources=SKILL_SET)
        without = self.enqueue(skill_resources=None, run_id="run_other",
                               tool_call_id="call_other")

        self.assertNotEqual(with_skills["params_digest"], without["params_digest"])
        self.assertEqual(without.get("skill_resources"), None)

    def test_a_command_with_no_skill_set_stays_exactly_as_it_was(self):
        """Every skill-less v2 tool and every v1 row keeps its old shape."""
        from integrations.desktop import execution_payload

        created = self.enqueue(skill_resources=None)

        self.assertIsNone(created.get("skill_resources"))
        claimed = self.claimed(created)
        self.assertNotIn("skill_resources",
                         execution_payload.device_execution_frame(claimed))


class TheDeclaredSetMustBeTheAuthorizedSetTests(_SkillDeliveryHarness):
    """The broker half: a run cannot be told a set it was not authorized with."""

    def test_the_device_may_declare_the_authorized_set(self):
        created = self.enqueue(skill_resources=SKILL_SET)
        self.claimed(created)

        data = self.ok(self.call(PREPARE, self.body(
            created, skill_resources=SKILL_SET)))

        self.assertEqual(data["skill_resources"], SKILL_SET)

    def test_the_start_permit_path_checks_the_set_just_as_prepare_does(self):
        """Start is the second, independent validation -- it must not be weaker."""
        created = self.enqueue(skill_resources=SKILL_SET)
        self.claimed(created)

        data = self.ok(self.call(START, self.body(
            created, skill_resources=SKILL_SET)))

        self.assertEqual(data["skill_resources"], SKILL_SET)

    def test_a_device_declaring_another_version_is_refused_by_name(self):
        """A stale snapshot must not quietly run a different package."""
        created = self.enqueue(skill_resources=SKILL_SET)
        self.claimed(created)
        other = [{"skill_id": "builtin:summary-workbook",
                  "digest": digest_of("summary@OLDER")}]

        response = self.call(PREPARE, self.body(created, skill_resources=other))

        self.refusal(response, status=422, code="incompatible_skill")

    def test_declaring_nothing_is_refused_when_the_run_has_a_set(self):
        """The bug this task exists to fix.

        Previously the stored digest was computed over an empty set, so a device
        that declared *no* skills was accepted -- which made it impossible to
        require a run to use a particular version.
        """
        created = self.enqueue(skill_resources=SKILL_SET)
        self.claimed(created)

        response = self.call(PREPARE, self.body(created, skill_resources=[]))

        self.refusal(response, status=422, code="incompatible_skill")

    def test_declaring_a_subset_is_refused(self):
        """Dropping one skill is a different set, not a smaller requirement."""
        created = self.enqueue(skill_resources=SKILL_SET)
        self.claimed(created)

        response = self.call(PREPARE, self.body(
            created, skill_resources=SKILL_SET[:1]))

        self.refusal(response, status=422, code="incompatible_skill")

    def test_the_order_of_the_declared_set_does_not_matter(self):
        """Two devices listing the same versions must agree, or it is noise."""
        created = self.enqueue(skill_resources=SKILL_SET)
        self.claimed(created)

        data = self.ok(self.call(PREPARE, self.body(
            created, skill_resources=list(reversed(SKILL_SET)))))

        self.assertEqual(data["skill_resources"], SKILL_SET)

    def test_a_run_with_no_skill_set_still_accepts_an_empty_declaration(self):
        """The compatibility half: skill-less runs are untouched."""
        created = self.enqueue(skill_resources=None)
        self.claimed(created)

        data = self.ok(self.call(PREPARE, self.body(created, skill_resources=[])))

        self.assertEqual(data["skill_resources"], [])

    def test_a_run_with_no_skill_set_refuses_a_device_claiming_one(self):
        """The inverse: a device cannot invent requirements the run never had."""
        created = self.enqueue(skill_resources=None)
        self.claimed(created)

        response = self.call(PREPARE, self.body(
            created, skill_resources=SKILL_SET))

        self.refusal(response, status=422, code="incompatible_skill")


PACKAGE = "/api/desktop/execution/skill-package"

#: A builtin skill that really exists on disk, so the package path is exercised
#: against real bytes rather than a fixture that could hide a broken read.
REAL_SKILL = "builtin:rfq-quote"


def real_manifest(skill_id: str = REAL_SKILL):
    """The manifest this server would build for ``skill_id`` right now."""
    import shutil

    from agent.skills.manager import build_skill_manager
    from agent.skills.manifest import build_skill_manifest

    manager = build_skill_manager()
    entry = manager.get_skill_by_resource_id(skill_id)
    if entry is None:
        raise unittest.SkipTest("本机没有内置技能 %s" % skill_id)
    return build_skill_manifest(
        entry, platform="posix", is_authorized=manager.is_authorized,
        which=shutil.which)


class ThePinnedPackageCanBePulledTests(_SkillDeliveryHarness):
    """Gap ①: the device had no way to obtain the bytes it was required to run.

    The whole exchange runs against the real skill directory: the digest the run
    is authorized with is the digest of the files on disk, and the bytes that
    come back are those files. A fixture digest would prove the endpoint echoes
    a number; this proves it ships a package.
    """

    def authorized_set(self):
        manifest = real_manifest()
        return manifest, [{"skill_id": manifest.skill_id, "digest": manifest.digest}]

    def pull(self, command, **extra):
        _native, _web, device, binding, workspace, _lease, _cmds = self.binding()
        query = {
            "command_id": command["id"],
            "device_id": device["id"],
            "binding_id": binding["id"],
            "workspace_id": workspace["id"],
            "grant_version": 1,
            "params_digest": command["params_digest"],
        }
        query.update(extra)
        encoded = "&".join("%s=%s" % (key, value) for key, value in query.items())
        return self.app.request(
            PACKAGE + "?" + encoded, "GET",
            headers={"Authorization": "Bearer " + self.native()})

    def test_the_device_receives_the_exact_bytes_the_digest_covers(self):
        """The payloads are the ones on disk, and they hash to the declared digest."""
        import base64

        from agent.desktop_local.package_rules import digest_of
        from agent.skills.manifest import read_skill_payloads

        manifest, authorized = self.authorized_set()
        created = self.enqueue(skill_resources=authorized)
        self.claimed(created)

        data = self.ok(self.pull(created, skill_id=manifest.skill_id,
                                 digest=manifest.digest))

        self.assertEqual(data["digest"], manifest.digest)
        self.assertEqual(data["skill_id"], manifest.skill_id)
        # Every declared resource is present, at its declared size, and the bytes
        # are the ones the server reads back from its own directory.
        on_disk = read_skill_payloads(manifest)
        shipped = {entry["relative_path"]: base64.b64decode(entry["body_base64"])
                   for entry in data["entries"]}
        self.assertEqual(sorted(shipped), sorted(on_disk))
        for relative, body in on_disk.items():
            self.assertEqual(shipped[relative], body, relative)
            self.assertEqual(digest_of(shipped[relative]), digest_of(body))
        declared = {resource.relative_path: resource
                    for resource in manifest.resources}
        self.assertEqual(sorted(declared), sorted(shipped))
        for relative, resource in declared.items():
            self.assertEqual(len(shipped[relative]), resource.size, relative)

    def test_a_version_the_command_was_not_authorized_with_cannot_be_pulled(self):
        """A device cannot pull a package by asking for it directly.

        The version that decides is the command's own recorded set, so a device
        that names a different digest -- one the server really could produce --
        is refused before any bytes are read.
        """
        manifest, authorized = self.authorized_set()
        created = self.enqueue(skill_resources=authorized)
        self.claimed(created)

        response = self.pull(created, skill_id=manifest.skill_id,
                             digest=digest_of("not-the-authorized-version"))

        self.refusal(response, status=422, code="incompatible_skill")

    def test_a_package_this_command_was_never_authorized_with_cannot_be_pulled(self):
        """The recorded set decides, not "a version this server happens to have".

        The requested version is real and current, so the server's own rebuild
        agrees with the request and the digest check below cannot be what
        refuses it. Only the command's recorded set can say no -- which is the
        difference between "this device may fetch any package" and "this device
        may fetch the versions this run was authorized with".
        """
        manifest = real_manifest()
        created = self.enqueue(skill_resources=[])
        self.claimed(created)

        response = self.pull(created, skill_id=manifest.skill_id,
                             digest=manifest.digest)

        self.refusal(response, status=422, code="incompatible_skill")

    def test_a_package_outside_a_non_empty_set_is_refused_too(self):
        """Being authorized for *some* skills does not authorize their neighbours."""
        manifest = real_manifest()
        created = self.enqueue(skill_resources=SKILL_SET)
        self.claimed(created)

        response = self.pull(created, skill_id=manifest.skill_id,
                             digest=manifest.digest)

        self.refusal(response, status=422, code="incompatible_skill")

    def test_a_version_the_server_would_not_ship_is_refused_by_name(self):
        """A digest in the set that this server no longer produces is refused.

        This is the integrity half: the set is *stored*, so between enqueue and
        pull the server's copy could move on. Shipping the new bytes under the
        old name is the one substitution the device cannot detect by itself, so
        it is refused here instead.
        """
        manifest, _authorized = self.authorized_set()
        stale = [{"skill_id": manifest.skill_id,
                  "digest": digest_of("a-version-this-server-no-longer-has")}]
        created = self.enqueue(skill_resources=stale)
        self.claimed(created)

        response = self.pull(created, skill_id=manifest.skill_id,
                             digest=stale[0]["digest"])

        self.refusal(response, status=422, code="incompatible_skill")

    def test_a_skill_this_server_does_not_ship_is_refused_by_name(self):
        """Not a bytes-less success: the caller is told which name is unknown."""
        missing = [{"skill_id": "builtin:no-such-skill",
                    "digest": digest_of("no-such-skill@1")}]
        created = self.enqueue(skill_resources=missing)
        self.claimed(created)

        response = self.pull(created, skill_id="builtin:no-such-skill",
                             digest=missing[0]["digest"])

        self.refusal(response, status=404, code="resource_not_found")

    def test_the_package_endpoint_is_native_only(self):
        """A page holds no native credential, so a page cannot pull a package."""
        manifest, authorized = self.authorized_set()
        created = self.enqueue(skill_resources=authorized)
        self.claimed(created)
        _native, _web, device, binding, workspace, _lease, _cmds = self.binding()
        query = ("command_id=%s&device_id=%s&binding_id=%s&workspace_id=%s"
                 "&grant_version=1&params_digest=%s&skill_id=%s&digest=%s"
                 % (created["id"], device["id"], binding["id"], workspace["id"],
                    created["params_digest"], manifest.skill_id, manifest.digest))

        response = self.app.request(
            PACKAGE + "?" + query, "GET",
            headers={"Authorization": "Bearer " + self.app.login("skill-u1")})

        # The refusal is by credential: "a native bearer session is required".
        self.refusal(response, status=401, code="auth_required")

    def test_a_run_that_pins_nothing_is_not_asked_to_pull_anything(self):
        """The compatibility half: a skill-less run has no package to fetch."""
        created = self.enqueue(skill_resources=None)
        self.claimed(created)

        # The set is empty, so no pair can be a member of it -- refused for the
        # same reason, and no file read is attempted.
        response = self.pull(created, skill_id=REAL_SKILL,
                             digest=digest_of("anything"))

        self.refusal(response, status=422, code="incompatible_skill")


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
