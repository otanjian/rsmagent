# encoding:utf-8
"""The narrowed execution broker over the real app: prepare, start, heartbeat,
status.

Change ``align-desktop-project-execution-with-master``, tasks 6.4 / 6.5.

``contracts/desktop/v2.json`` declares exactly four paths under
``/api/desktop/execution/`` and this file drives all four through the real WSGI
app on a real identity database, because the promises they carry cannot be
checked at the service layer:

* **the authorization is re-established per request.** Not "the enqueue was
  lawful, so this is fine" -- membership, Agent use, device liveness, the live
  grant version, the tool policy, the action approval, the quota and the
  declared-parameters digest are all re-read on *this* request, which is the
  only way ``入队后权限被撤销`` is enforced instead of documented;
* **two independent re-validations, one permit.** ``prepare`` answers "may I
  still run this?", ``start`` asks the same question again and is the only place
  a permit is minted, and the permit is single use and short lived -- so
  "authorized at prepare" cannot be replayed as a start later;
* **a body may name identifiers and the digest, and nothing else.** No URL, no
  auth header, no module, no class, no cwd (``broker.forbidden``), at any depth
  and on every path, including the GET;
* **the credential must be native.** A page -- a Cookie-backed Web session --
  is refused before any check runs, so "a page cannot drive execution" is a
  property of the credential rather than of a URL;
* **status never invents a state.** A started command with no terminal result
  reads as running, and an ``outcome_unknown`` row keeps its ``effects=unknown``
  claim plus a reconcile instruction instead of becoming a success.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import urlencode

from tests._helpers import WebAppHarness

CLIENT_ID = "cowagent-desktop"
REDIRECT_URI = "http://127.0.0.1:52345/callback"
VERIFIER = "M" * 43
ORIGIN = "https://console.test"
SESSION = "biz-broker-session"
NONCE = "nonce_" + ("b" * 22)

PREPARE = "/api/desktop/execution/prepare"
START = "/api/desktop/execution/start"
HEARTBEAT = "/api/desktop/execution/heartbeat"
STATUS = "/api/desktop/execution/status"
SKILL_PACKAGE = "/api/desktop/execution/skill-package"

AGENT = "v2-agent"


def _challenge():
    from auth.desktop_auth import s256_challenge
    return s256_challenge(VERIFIER)


class _EnabledSlice:
    """The declaration once the acceptance suites have run.

    The two v2 slices ship ``accepted=False`` on purpose -- they are opened by
    the acceptance evidence in ``evidence/``, never by a test -- so every test
    here describes the deployment the endpoints exist for: declared, accepted
    and switched on.
    """

    enabled = True
    implemented = True
    accepted = True
    reason = ""

    def is_open(self, action):
        return True


class BrokerFixture(unittest.TestCase):
    """Harness shared by the broker's own suites: a real device and a real frame.

    Split out from ``BrokerEndpointTests`` so the rollback drill
    (``RollbackDrillTests``, task 11.4) can build the same rows through the same
    creation path without re-running every endpoint test a second time.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-broker-")
        cls.app = WebAppHarness(
            os.path.join(cls._tmp.name, "instance"),
            settings={
                "desktop_project_execution_enabled": True,
                "desktop_project_scripts_enabled": True,
            })
        cls.app.add_agent(AGENT)
        cls.app.role("v2-role", ["chat.use", "agent.use", "agent.read"],
                     grants=[("agent", "agent:%s" % AGENT, "use")])
        cls.u1 = cls.app.member("v2-u1", ["v2-role"])
        cls.u2 = cls.app.member("v2-u2", ["v2-role"])
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
        self._patch = patch("auth.capability_matrix.slice_for",
                            side_effect=lambda name: _EnabledSlice())
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._fixture = {}
        self._tokens = {}

    # -- harness ------------------------------------------------------------

    @contextmanager
    def _config(self, **values):
        """Change a live deployment setting the way an operator would.

        The harness's ``conf()`` *is* this dict, so a test can open a switch or
        declare an approval-required action mid-flight and have every reader see
        it -- no patched layer between the assertion and the code.
        """
        settings = self.app._settings
        before = {key: settings.get(key) for key in values}
        settings.update(values)
        try:
            yield
        finally:
            for key, value in before.items():
                if value is None:
                    settings.pop(key, None)
                else:
                    settings[key] = value

    def native(self, username="v2-u1"):
        """A native bearer for ``username`` (the credential a device holds)."""
        if username in self._tokens:
            return self._tokens[username]
        from auth.desktop_auth import service_for
        desktop = service_for(self.app.service)
        web_token = self.app.login(username)
        started = desktop.begin(
            session_token=web_token, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, code_challenge=_challenge(),
            code_challenge_method="S256")
        confirmed = desktop.confirm(
            request_id=started["request_id"], csrf=started["csrf"],
            session_token=web_token)
        self._tokens[username] = desktop.exchange(
            code=confirmed["code"], verifier=VERIFIER, client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI, origin=ORIGIN)["token"]
        return self._tokens[username]

    def paired(self):
        from auth.desktop_web_session import service_for
        native = self.native()
        child = service_for(self.app.service).bootstrap(
            native_token=native, bootstrap_id=secrets.token_urlsafe(18),
            instance_id=secrets.token_urlsafe(18), web_protocol=1,
            origin=ORIGIN)
        return native, child["web_token"]

    def commands(self):
        from integrations.desktop.commands import service_for
        return service_for(self.app.service)

    def devices(self):
        from integrations.desktop.devices import service_for
        return service_for(self.app.service)

    def fixture(self, *, platform="macos"):
        """A device, binding, project-execution workspace, grant and live lease.

        Built per platform and cached for the test; each test gets its own
        device and installation, so no test can be read through another's rows.
        """
        if platform in self._fixture:
            return self._fixture[platform]
        native, web = self.paired()
        devices = self.devices()
        device = devices.register_device(
            token=native, installation_id="install_" + secrets.token_urlsafe(18),
            display_name="BrokerBox %s" % platform, platform=platform,
            client_version="2.1.9")
        binding = devices.create_binding(
            token=web, tenant_id=self.app.tenant_id, device_id=device["id"],
            agent_id=AGENT, business_session_id=SESSION, context_nonce=NONCE)
        workspace = devices.register_workspace(
            token=native, tenant_id=self.app.tenant_id, device_id=device["id"],
            label="我的 项目", grant_version=1, project_mode="project-execution")
        devices.bind_workspace(
            token=native, tenant_id=self.app.tenant_id,
            binding_id=binding["id"], workspace_id=workspace["id"],
            grant_version=1)
        cmds = self.commands()
        lease = cmds.acquire_lease(
            token=native, device_id=device["id"],
            gateway_id="gw-broker-" + secrets.token_urlsafe(6),
            protocol_major=2)
        fixture = {
            "native": native, "web": web, "device": device,
            "binding": binding, "workspace": workspace, "lease": lease,
        }
        self._fixture[platform] = fixture
        return fixture

    def enqueue(self, *, platform="macos", **overrides):
        """One real v2 command through the real creation path."""
        fix = self.fixture(platform=platform)
        kwargs = dict(
            token=fix["web"], tenant_id=self.app.tenant_id,
            binding_id=fix["binding"]["id"], workspace_id=fix["workspace"]["id"],
            grant_version=1, tool="bash",
            arguments={"command": "python3 -c \"print(1)\"", "timeout": 120},
            run_id="run_" + secrets.token_hex(4),
            tool_call_id="call_" + secrets.token_hex(4),
            selection_generation=1, origin=ORIGIN,
            request_id="req_" + secrets.token_hex(6),
            permission_mode="full-access")
        kwargs.update(overrides)
        return self.commands().create_execution(**kwargs)

    def claimed(self, command, *, platform="macos"):
        """Claim + acknowledge: the two states a frame passes through first."""
        fix = self.fixture(platform=platform)
        cmds = self.commands()
        cmds.claim_outbox(device_id=fix["device"]["id"],
                          epoch=fix["lease"]["epoch"])
        return cmds.acknowledge(command_id=command["id"],
                                epoch=fix["lease"]["epoch"])

    def body(self, command, *, platform="macos", **overrides):
        """The identifiers a device holds for one command."""
        fix = self.fixture(platform=platform)
        data = {
            "command_id": command["id"],
            "device_id": fix["device"]["id"],
            "binding_id": fix["binding"]["id"],
            "workspace_id": fix["workspace"]["id"],
            "grant_version": 1,
            "params_digest": command["params_digest"],
            "connection_epoch": fix["lease"]["epoch"],
        }
        data.update(overrides)
        return data

    def call(self, path, body, *, token=None, method="POST"):
        headers = {"Authorization": "Bearer " + (token or self.native())}
        if method == "GET":
            return self.app.request("%s?%s" % (path, urlencode(body)), "GET",
                                    headers=headers)
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

    def row(self, command, *, platform="macos"):
        fix = self.fixture(platform=platform)
        return self.commands().get_command(
            token=fix["web"], tenant_id=self.app.tenant_id,
            command_id=command["id"])


class BrokerEndpointTests(BrokerFixture):
    """One endpoint per behaviour: what each broker path refuses and returns."""

    # -- the contract's own routing ----------------------------------------

    def test_the_broker_paths_are_the_contract_paths_and_carry_a_tenant_policy(self):
        """Task 6.4: the surface exists exactly where the contract says.

        The registry keeps these reachable (a ``closed`` policy would refuse
        before any handler ran, turning a documented refusal code into a generic
        gate): what closes a deployment is the capability state, and the handler
        is where that is answered.
        """
        from auth import desktop_contracts_v2 as v2
        from channel.web import route_registry

        entries = {entry.pattern: entry for entry in route_registry.ROUTES}
        # Task 8.9 added ``skill_package``, which is the same surface for the same
        # reason: a device asking the server it is connected to for the pinned
        # skill bytes is asking a question the command channel cannot answer, and
        # it must be answered under the caller's own tenant.
        self.assertEqual(v2.BROKER["paths"], {
            "prepare": PREPARE, "start": START,
            "heartbeat": HEARTBEAT, "status": STATUS,
            "skill_package": SKILL_PACKAGE})
        for name, path in v2.BROKER["paths"].items():
            self.assertIn(path, entries, name)
            method = v2.BROKER["methods"][name]
            policy = entries[path].methods[method]["policy"]
            self.assertNotEqual(policy, "closed", name)
            self.assertEqual(policy, "tenant", name)
        self.assertEqual(sorted(entries[PREPARE].methods), ["POST"])
        self.assertEqual(sorted(entries[STATUS].methods), ["GET"])
        self.assertEqual(sorted(entries[SKILL_PACKAGE].methods), ["GET"])

    # -- native only --------------------------------------------------------

    def test_a_page_session_cannot_prepare_an_execution(self):
        """A Cookie-backed Web session is refused before any check runs."""
        command = self.enqueue()
        page = self.app.login("v2-u1")
        response = self.call(PREPARE, self.body(command), token=page)
        self.refusal(response, status=401, code="auth_required")
        self.assertEqual(self.row(command)["state"], "queued")

    def test_a_page_session_cannot_read_the_status(self):
        command = self.enqueue()
        page = self.app.login("v2-u1")
        response = self.call(STATUS, self.body(command), token=page,
                             method="GET")
        self.refusal(response, status=401, code="auth_required")

    # -- request shaping ----------------------------------------------------

    def test_a_forbidden_field_is_refused_at_any_depth(self):
        """No URL, no module, no cwd -- the broker is not a general runner."""
        command = self.enqueue()
        for override in ({"cwd": "/etc"}, {"module_path": "x.y"},
                         {"server_url": "https://evil.test"},
                         {"authorization_header": "Bearer x"},
                         {"extra": {"nested": {"class_name": "Evil"}}}):
            response = self.call(PREPARE, self.body(command, **override))
            self.refusal(response, status=400, code="invalid_request")
        self.assertEqual(self.row(command)["state"], "queued")

    def test_every_identifier_is_required_and_grant_version_is_a_number(self):
        command = self.enqueue()
        for missing in ("command_id", "device_id", "binding_id",
                        "workspace_id", "params_digest"):
            body = self.body(command)
            body.pop(missing)
            response = self.call(PREPARE, body)
            self.refusal(response, status=400, code="invalid_request")
        for bad in ("two", 0, -1, None):
            response = self.call(PREPARE, self.body(command, grant_version=bad))
            self.refusal(response, status=400, code="invalid_request")

    def test_prepare_refuses_a_non_object_body(self):
        response = self.app.request(
            PREPARE, "POST", body=json.dumps(["not", "an", "object"]),
            headers={"Authorization": "Bearer " + self.native()})
        self.refusal(response, status=400, code="invalid_request")

    # -- prepare: the happy path -------------------------------------------

    def test_prepare_returns_the_digest_and_changes_nothing(self):
        command = self.enqueue()
        before = self.row(command)
        data = self.ok(self.call(PREPARE, self.body(command)))

        self.assertEqual(data["command_id"], command["id"])
        self.assertEqual(data["tool"], "bash")
        self.assertEqual(data["params_digest"], command["params_digest"])
        self.assertEqual(data["run_id"], command["run_id"])
        self.assertEqual(data["tool_call_id"], command["tool_call_id"])
        self.assertEqual(data["platform"], "posix")
        self.assertEqual(data["skill_resources"], [])
        self.assertEqual(data["checks"]["quota"]["within_limit"], True)
        self.assertFalse(data["terminal"])

        after = self.row(command)
        self.assertEqual(after["state"], "queued")
        self.assertEqual(after["phase"], "queued")
        self.assertIsNone(after["started_at"])
        self.assertIsNone(after["journal_id"])
        self.assertIsNone(after["permit_id"])
        self.assertEqual(after["updated_at"], before["updated_at"])
        self.assertTrue(before)
        self.assertFalse(data["expires_in_seconds"] < 0)

    def test_prepare_tolerates_a_terminal_command_and_says_so(self):
        """A redelivered frame is answered, not turned into an error."""
        command = self.enqueue()
        self.claimed(command)
        self.commands().complete_execution(
            command_id=command["id"], epoch=self.fixture()["lease"]["epoch"],
            payload={
                "type": "execution_result", "protocol_major": 2,
                "command_id": command["id"], "run_id": command["run_id"],
                "tool_call_id": command["tool_call_id"],
                "workspace_id": command["workspace_id"],
                "state": "succeeded", "execution_phase": "succeeded",
                "effects": "completed", "started_at": 1, "finished_at": 2})
        data = self.ok(self.call(PREPARE, self.body(command)))
        self.assertTrue(data["terminal"])
        self.assertEqual(data["state"], "succeeded")

    # -- prepare: the checks that can have changed since enqueue -----------

    def test_prepare_refuses_a_digest_that_is_not_the_commands(self):
        command = self.enqueue()
        response = self.call(PREPARE, self.body(
            command, params_digest="sha256:" + "0" * 64))
        self.refusal(response, status=409, code="command_conflict")
        self.assertEqual(self.row(command)["state"], "queued")

    def test_prepare_refuses_a_workspace_the_command_does_not_belong_to(self):
        """Same device, same binding, same grant version -- other project."""
        fix = self.fixture()
        other = self.devices().register_workspace(
            token=fix["native"], tenant_id=self.app.tenant_id,
            device_id=fix["device"]["id"], label="另一个项目",
            grant_version=1, project_mode="project-execution")
        self.devices().bind_workspace(
            token=fix["native"], tenant_id=self.app.tenant_id,
            binding_id=fix["binding"]["id"], workspace_id=other["id"],
            grant_version=1)
        command = self.enqueue()
        response = self.call(PREPARE, self.body(command, workspace_id=other["id"]))
        self.refusal(response, status=409, code="stale_context")

    def test_prepare_refuses_another_users_command(self):
        command = self.enqueue()
        response = self.call(PREPARE, self.body(command), token=self.native("v2-u2"))
        self.refusal(response, status=404, code="resource_not_found")
        self.assertEqual(self.row(command)["state"], "queued")

    def test_prepare_refuses_a_v1_command_by_name(self):
        """A v1 row has no v2 execution, and that is what it is told."""
        fix = self.fixture()
        v1 = self.commands().create_command(
            token=fix["web"], tenant_id=self.app.tenant_id,
            binding_id=fix["binding"]["id"], workspace_id=fix["workspace"]["id"],
            grant_version=1, op="stat", params={"relative_path": "a.txt"},
            request_id="req_" + secrets.token_hex(6))
        response = self.call(PREPARE, self.body(v1, params_digest=""))
        self.refusal(response, status=400, code="protocol_incompatible")

    def test_prepare_refuses_a_platform_with_no_accepted_launcher(self):
        command = self.enqueue(platform="windows")
        response = self.call(PREPARE, self.body(command, platform="windows"))
        self.refusal(response, status=422, code="unsupported_platform")

    def test_a_supported_platform_still_refuses_a_tool_it_cannot_run(self):
        """The second refusal step, which no shipped platform reaches today.

        win32 is refused as a *platform* first (above), so the per-tool branch is
        defensive -- but it is the promise the contract makes about the day a
        Windows launcher is accepted: a Windows device must never be handed a
        POSIX command string, so `bash` has to keep being refused by name. Pinned
        by forcing win32 into the supported table rather than trusting that the
        branch stays reachable.
        """
        from auth import desktop_contracts_v2 as v2

        command = self.enqueue(platform="windows")
        forced = dict(v2.PLATFORMS)
        forced["win32"] = {"supported": True, "launcher": "forced-for-this-test"}
        with patch.dict(v2.PLATFORMS, forced, clear=True):
            self.assertTrue(v2.platform_supported("win32"))
            self.assertFalse(v2.tool_supported_on("bash", "win32"))
            response = self.call(PREPARE, self.body(command, platform="windows"))
        self.refusal(response, status=503, code="feature_unavailable")
        self.assertEqual(self.row(command)["state"], "queued")

    def test_prepare_refuses_a_revoked_workspace_grant(self):
        fix = self.fixture()
        command = self.enqueue()
        self.devices().revoke_workspace(
            token=fix["native"], tenant_id=self.app.tenant_id,
            workspace_id=fix["workspace"]["id"])
        response = self.call(PREPARE, self.body(command))
        self.refusal(response, status=403, code="grant_revoked")

    def test_prepare_refuses_a_revoked_binding(self):
        fix = self.fixture()
        command = self.enqueue()
        self.devices().revoke_binding(
            token=fix["native"], tenant_id=self.app.tenant_id,
            binding_id=fix["binding"]["id"])
        response = self.call(PREPARE, self.body(command))
        self.refusal(response, status=403, code="grant_revoked")

    def test_prepare_refuses_a_disabled_device(self):
        """Disabling a device cascades to its bindings, and *that* fires first.

        Disabling is not a flag the broker can ignore: the same transaction
        revokes the bindings and grants, so the refusal a device sees is the
        revoked grant (`grant_revoked`). Whichever of the two checks a future
        refactor orders first, the answer must be a refusal -- never a prepare.
        """
        fix = self.fixture()
        command = self.enqueue()
        self.devices().disable_device(
            token=fix["native"], device_id=fix["device"]["id"])
        response = self.call(PREPARE, self.body(command))
        self.refusal(response, status=403, code="grant_revoked")
        self.assertEqual(self.row(command)["state"], "queued")

    def test_prepare_re_runs_the_tool_permission_policy(self):
        """A row written in a mode that cannot write must not start writing."""
        command = self.enqueue(
            permission_mode="read-only",
            arguments={"command": "mkdir -p 输出", "timeout": 60})
        response = self.call(PREPARE, self.body(command))
        self.refusal(response, status=403, code="permission_denied")
        # The same command in a mode that allows it is prepared normally: the
        # refusal is the policy, not the tool.
        allowed = self.enqueue(permission_mode="full-access",
                               arguments={"command": "mkdir -p 输出",
                                          "timeout": 60})
        self.ok(self.call(PREPARE, self.body(allowed)))

    def test_prepare_refuses_when_the_tool_call_quota_is_exhausted(self):
        command = self.enqueue()
        self.app.service.set_quota(
            actor_user_id=self.app.admin_id, tenant_id=self.app.tenant_id,
            metric="tool_calls", hard_limit=1)
        self.addCleanup(self._clear_quota)
        self.assertTrue(self.app.service.consume_quota(
            user_id=self.u1, tenant_id=self.app.tenant_id, metric="tool_calls"))
        response = self.call(PREPARE, self.body(command))
        self.refusal(response, status=413, code="limit_exceeded")


    def _clear_quota(self):
        self.app.service.set_quota(
            actor_user_id=self.app.admin_id, tenant_id=self.app.tenant_id,
            metric="tool_calls", hard_limit=0)

    def _approved_action(self, action="tool:bash"):
        """An approval for one action, requested by u1 and decided by root."""
        requested = self.app.service.request_approval(
            actor_user_id=self.u1, tenant_id=self.app.tenant_id,
            agent_id=AGENT, action=action)
        self.app.service.decide_approval(
            actor_user_id=self.app.admin_id, tenant_id=self.app.tenant_id,
            approval_id=requested["id"], approve=True)
        return requested["id"]

    def test_prepare_refuses_a_declared_action_without_an_approval(self):
        command = self.enqueue()
        with self._config(approval_required_actions="tool:bash"):
            response = self.call(PREPARE, self.body(command))
            self.refusal(response, status=403, code="approval_required")

    def test_prepare_accepts_the_approved_action_and_refuses_the_revoked_one(self):
        approval_id = self._approved_action()
        command = self.enqueue(approval_id=approval_id)
        with self._config(approval_required_actions="tool:bash"):
            prepared = self.ok(self.call(PREPARE, self.body(command)))
            self.assertEqual(prepared["checks"]["approval"], "verified")
            # A controller supersedes the approval *after* prepare: the start
            # must not run on the strength of the earlier answer.
            self.app.service.revoke_approval(
                actor_user_id=self.app.admin_id, tenant_id=self.app.tenant_id,
                approval_id=approval_id)
            self.claimed(command)
            response = self.call(START, self.body(command))
            self.refusal(response, status=403, code="approval_required")

    # -- start: the single-use permit --------------------------------------

    def test_start_refuses_while_the_device_holds_no_live_connection(self):
        """An offline device is refused by name so it retries on reconnect."""
        command = self.enqueue()
        self.claimed(command)
        self.commands().revoke_lease(epoch=self.fixture()["lease"]["epoch"])
        response = self.call(START, self.body(command))
        self.refusal(response, status=503, code="device_offline")
        self.assertIsNone(self.row(command)["permit_id"])

    def test_start_refuses_a_superseded_connection_epoch(self):
        """Task 7.x's fence, seen from the broker: only the live epoch starts."""
        fix = self.fixture()
        command = self.enqueue()
        self.claimed(command)
        stale = fix["lease"]["epoch"]
        fresh = self.commands().acquire_lease(
            token=fix["native"], device_id=fix["device"]["id"],
            gateway_id="gw-broker-second", protocol_major=2)
        self.assertNotEqual(fresh["epoch"], stale)
        response = self.call(START, self.body(command, connection_epoch=stale))
        self.refusal(response, status=409, code="stale_context")
        self.assertIsNone(self.row(command)["permit_id"])

    def test_the_permit_is_short_lived_and_single_use(self):
        command = self.enqueue()
        self.claimed(command)
        data = self.ok(self.call(START, self.body(command)))
        permit = data["permit"]
        self.assertEqual(permit["command_id"], command["id"])
        self.assertEqual(permit["params_digest"], command["params_digest"])
        self.assertGreater(permit["expires_at"], permit["server_time"])

        from auth import desktop_contracts_v2 as v2
        ttl = int(v2.LIMITS["start_permit_ttl_seconds"])
        self.assertLessEqual(permit["expires_at"] - permit["server_time"], ttl)
        self.assertEqual(v2.validate_start_permit(permit, now=permit["server_time"]), [])

        # The permit is spent by recording the start, so a second start cannot
        # mint a second one, and the frame cannot be restarted with the first.
        self.ok(self.call(HEARTBEAT, self.body(
            command, journal_id="journal_1", permit_id=permit["permit_id"])))
        response = self.call(START, self.body(command))
        self.refusal(response, status=409, code="already_started")

    def test_start_re_validates_the_authorization_prepare_answered(self):
        """Task 6.5: the same checks, asked again, right before the side effect."""
        fix = self.fixture()
        command = self.enqueue()
        self.claimed(command)
        self.ok(self.call(PREPARE, self.body(command)))
        self.devices().revoke_workspace(
            token=fix["native"], tenant_id=self.app.tenant_id,
            workspace_id=fix["workspace"]["id"])
        response = self.call(START, self.body(command))
        self.refusal(response, status=403, code="grant_revoked")
        self.assertIsNone(self.row(command)["permit_id"])

    def test_start_refuses_a_quota_exhausted_after_prepare(self):
        command = self.enqueue()
        self.claimed(command)
        self.ok(self.call(PREPARE, self.body(command)))
        self.app.service.set_quota(
            actor_user_id=self.app.admin_id, tenant_id=self.app.tenant_id,
            metric="tool_calls", hard_limit=1)
        self.addCleanup(self._clear_quota)
        self.app.service.consume_quota(
            user_id=self.u1, tenant_id=self.app.tenant_id, metric="tool_calls")
        response = self.call(START, self.body(command))
        self.refusal(response, status=413, code="limit_exceeded")

    def test_a_start_on_a_command_that_already_finished_is_refused(self):
        fix = self.fixture()
        command = self.enqueue()
        self.commands().request_cancel(
            token=fix["web"], tenant_id=self.app.tenant_id,
            command_id=command["id"])
        self.assertEqual(self.row(command)["state"], "cancelled")
        response = self.call(START, self.body(command))
        self.refusal(response, status=409, code="stale_context")

    def test_a_start_on_a_terminal_execution_is_refused_by_the_row(self):
        """A completion that already happened cannot be restarted by a permit."""
        fix = self.fixture()
        command = self.enqueue()
        self.claimed(command)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        self.ok(self.call(HEARTBEAT, self.body(
            command, journal_id="journal_1", permit_id=permit["permit_id"])))
        self.commands().complete_execution(
            command_id=command["id"], epoch=fix["lease"]["epoch"],
            payload={
                "type": "execution_result", "protocol_major": 2,
                "command_id": command["id"], "run_id": command["run_id"],
                "tool_call_id": command["tool_call_id"],
                "state": "cancelled", "execution_phase": "cancelled",
                "effects": "unknown", "started_at": 1, "finished_at": 2})
        response = self.call(START, self.body(command))
        self.refusal(response, status=409, code="stale_context")

    # -- heartbeat: the start, then liveness -------------------------------

    def test_a_first_heartbeat_needs_both_journal_and_permit(self):
        command = self.enqueue()
        self.claimed(command)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        for partial in ({}, {"journal_id": "journal_1"},
                        {"permit_id": permit["permit_id"]}):
            response = self.call(HEARTBEAT, self.body(command, **partial))
            self.refusal(response, status=400, code="invalid_request")
        still = self.row(command)
        self.assertEqual(still["state"], "acknowledged")
        self.assertIsNone(still["started_at"])
        self.assertIsNone(still["journal_id"])

    def test_the_start_is_recorded_once_and_later_beats_only_advance_the_clock(self):
        command = self.enqueue()
        self.claimed(command)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        started = self.ok(self.call(HEARTBEAT, self.body(
            command, journal_id="journal_1", permit_id=permit["permit_id"])))
        self.assertEqual(started["recorded"], "start")
        self.assertEqual(started["state"], "running")
        self.assertEqual(started["phase"], "running")
        row = self.row(command)
        self.assertEqual(row["journal_id"], "journal_1")
        self.assertEqual(row["permit_id"], permit["permit_id"])
        self.assertTrue(row["started_at"])

        beat = self.ok(self.call(HEARTBEAT, self.body(
            command, output_bytes=4096)))
        self.assertEqual(beat["recorded"], "heartbeat")
        self.assertEqual(beat["state"], "running")
        self.assertTrue(self.row(command)["heartbeat_at"])

    def test_a_heartbeat_from_a_superseded_epoch_is_refused(self):
        fix = self.fixture()
        command = self.enqueue()
        self.claimed(command)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        self.ok(self.call(HEARTBEAT, self.body(
            command, journal_id="journal_1", permit_id=permit["permit_id"])))
        stale = fix["lease"]["epoch"]
        self.commands().acquire_lease(
            token=fix["native"], device_id=fix["device"]["id"],
            gateway_id="gw-broker-third", protocol_major=2)
        response = self.call(HEARTBEAT, self.body(
            command, connection_epoch=stale, output_bytes=1))
        self.refusal(response, status=409, code="stale_context")

    def test_a_permit_that_expired_cannot_start_anything(self):
        command = self.enqueue()
        self.claimed(command)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        self.app.service._store.execute(
            "UPDATE desktop_execution_permits SET expires_at=issued_at WHERE id=?",
            (permit["permit_id"],))
        response = self.call(HEARTBEAT, self.body(
            command, journal_id="journal_late", permit_id=permit["permit_id"]))
        self.refusal(response, status=409, code="permit_expired")
        still = self.row(command)
        self.assertEqual(still["state"], "acknowledged")
        self.assertIsNone(still["started_at"])

    def test_a_permit_cannot_start_a_command_it_does_not_belong_to(self):
        command = self.enqueue()
        other = self.enqueue()
        self.claimed(command)
        self.claimed(other)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        response = self.call(HEARTBEAT, self.body(
            other, journal_id="journal_x", permit_id=permit["permit_id"]))
        self.refusal(response, status=400, code="invalid_request")
        self.assertIsNone(self.row(other)["started_at"])

    # -- status: the truth about one command -------------------------------

    def test_status_reports_an_unstarted_command_as_never_started(self):
        command = self.enqueue()
        data = self.ok(self.call(STATUS, self.body(command), method="GET"))
        self.assertEqual(data["command_id"], command["id"])
        self.assertEqual(data["state"], "queued")
        self.assertFalse(data["started"])
        self.assertFalse(data["terminal"])
        self.assertIsNone(data["started_at"])
        self.assertIsNone(data["reconcile"])

    def test_status_reads_a_started_command_as_running_never_as_not_run(self):
        command = self.enqueue()
        self.claimed(command)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        self.ok(self.call(HEARTBEAT, self.body(
            command, journal_id="journal_1", permit_id=permit["permit_id"])))
        data = self.ok(self.call(STATUS, self.body(command), method="GET"))
        self.assertTrue(data["started"])
        self.assertEqual(data["state"], "running")
        self.assertEqual(data["phase"], "running")
        self.assertFalse(data["terminal"])
        self.assertFalse(data["outcome_unknown"])
        self.assertIsNotNone(data["reconcile"])
        self.assertIn("journal", data["reconcile"])

    def test_status_keeps_an_unknown_outcome_unknown(self):
        fix = self.fixture()
        command = self.enqueue()
        self.claimed(command)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        self.ok(self.call(HEARTBEAT, self.body(
            command, journal_id="journal_1", permit_id=permit["permit_id"])))
        self.commands().complete_execution(
            command_id=command["id"], epoch=fix["lease"]["epoch"],
            payload={
                "type": "execution_result", "protocol_major": 2,
                "command_id": command["id"], "run_id": command["run_id"],
                "tool_call_id": command["tool_call_id"],
                "state": "failed", "execution_phase": "outcome_unknown",
                "effects": "unknown", "error_code": "outcome_unknown",
                "error_message": "device lost", "started_at": 1,
                "finished_at": 2})
        data = self.ok(self.call(STATUS, self.body(command), method="GET"))
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["phase"], "outcome_unknown")
        self.assertEqual(data["effects"], "unknown")
        self.assertTrue(data["terminal"])
        self.assertTrue(data["outcome_unknown"])
        self.assertIsNotNone(data["reconcile"])
        self.assertNotEqual(data["state"], "succeeded")

    def test_status_refuses_a_command_the_caller_cannot_continue(self):
        """Cross-scope polling is a refusal, not a quiet read."""
        fix = self.fixture()
        other = self.devices().register_workspace(
            token=fix["native"], tenant_id=self.app.tenant_id,
            device_id=fix["device"]["id"], label="另一个项目",
            grant_version=1, project_mode="project-execution")
        self.devices().bind_workspace(
            token=fix["native"], tenant_id=self.app.tenant_id,
            binding_id=fix["binding"]["id"], workspace_id=other["id"],
            grant_version=1)
        command = self.enqueue()
        response = self.call(STATUS, self.body(command, workspace_id=other["id"]),
                             method="GET")
        self.refusal(response, status=409, code="stale_context")

        response = self.call(STATUS, self.body(command), token=self.native("v2-u2"),
                             method="GET")
        self.refusal(response, status=404, code="resource_not_found")

    def test_status_refuses_a_forbidden_field_in_the_query(self):
        """The GET binds the same rules as the POSTs: no cwd, no module."""
        command = self.enqueue()
        response = self.call(STATUS, self.body(command, cwd="/etc"), method="GET")
        self.refusal(response, status=400, code="invalid_request")
        response = self.call(STATUS, self.body(command, params_digest=""),
                             method="GET")
        self.refusal(response, status=409, code="command_conflict")


class ExecutionGateTests(unittest.TestCase):
    """A deployment that has not accepted the capability serves none of it."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-broker-off-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"), settings={
            "desktop_project_execution_enabled": True,
            "desktop_project_scripts_enabled": True,
        })

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls._tmp.cleanup()

    def test_the_broker_refuses_while_the_capability_is_not_accepted(self):
        """The switch is on, the code is present, the batch is not accepted yet.

        ``desktop_project_execution`` ships ``accepted=False`` deliberately, so
        this is the *real* declaration rather than a fixture: the gate has to
        refuse by name before any authorization is consulted, because a
        deployment that reported ``not_accepted`` on meta and still answered
        here would be advertising an enforcement it has not accepted.
        """
        token = self.app.login("root")
        response = self.app.request(
            PREPARE, "POST", body=json.dumps({
                "command_id": "c", "device_id": "d", "binding_id": "b",
                "workspace_id": "w", "grant_version": 1,
                "params_digest": "sha256:x"}),
            token=token)
        self.assertTrue(str(response.status).startswith("503"), response.status)
        self.assertEqual(json.loads(response.data.decode("utf-8"))["code"],
                         "feature_unavailable")

    def test_the_declaration_is_what_closes_it(self):
        """The same path opens once the declaration is accepted -- nothing else."""
        token = self.app.login("root")
        with patch("auth.capability_matrix.slice_for",
                   side_effect=lambda name: _EnabledSlice()):
            response = self.app.request(
                PREPARE, "POST", body=json.dumps({
                    "command_id": "c", "device_id": "d", "binding_id": "b",
                    "workspace_id": "w", "grant_version": 1,
                    "params_digest": "sha256:x"}),
                token=token)
        # Past the gate, and refused by the *authorization* layer: the caller is
        # a Cookie-backed Web session, so no broker endpoint answers it.
        self.assertTrue(str(response.status).startswith("401"), response.status)
        self.assertEqual(json.loads(response.data.decode("utf-8"))["code"],
                         "auth_required")


class RollbackDrillTests(BrokerFixture):
    """A32 (task 11.4): turn the switch off with work in flight, finished and unknown.

    The drill, not the mechanism. Nothing in the rollback path is *supposed* to
    delete anything, and that is exactly the kind of claim that is worth running:
    the failure modes are a cleanup that "tidies up" an in-flight command, a
    projection that rewrites ``outcome_unknown`` into a plain failure so a client
    retries it, and a closed switch causing the work to be re-routed to the
    server directory. Each of those is one assertion below.

    What the switch-off *should* do is stop new calls -- the composed gate
    (declaration x switch x platform) refuses every broker path by name -- and
    leave the rows exactly as they were, so that re-enabling restores the same
    answers. The device's own journal and the original files are on the device
    and are not reachable from here at all; the server-side claim is only that
    it deletes nothing and invents nothing.
    """

    def _drill_rows(self):
        """One running, one finished and one ``outcome_unknown`` command.

        Built through the real creation path, with the device claiming,
        acknowledging, starting and heartbeating each one, so the rows carry a
        real journal id, permit id and heartbeat -- the fields a cleanup would
        have to remove to make the in-flight work disappear.
        """
        fix = self.fixture()
        cases = {}

        command = self.enqueue()
        self.claimed(command)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        self.ok(self.call(HEARTBEAT, self.body(
            command, journal_id="journal_in_flight",
            permit_id=permit["permit_id"])))
        cases["running"] = command

        command = self.enqueue()
        self.claimed(command)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        self.ok(self.call(HEARTBEAT, self.body(
            command, journal_id="journal_done", permit_id=permit["permit_id"])))
        self.commands().complete_execution(
            command_id=command["id"], epoch=fix["lease"]["epoch"],
            payload={
                "type": "execution_result", "protocol_major": 2,
                "command_id": command["id"], "run_id": command["run_id"],
                "tool_call_id": command["tool_call_id"],
                "state": "succeeded", "execution_phase": "succeeded",
                "effects": "completed", "exit_code": 0,
                "started_at": 1, "finished_at": 2})
        cases["finished"] = command

        command = self.enqueue()
        self.claimed(command)
        permit = self.ok(self.call(START, self.body(command)))["permit"]
        self.ok(self.call(HEARTBEAT, self.body(
            command, journal_id="journal_unknown", permit_id=permit["permit_id"])))
        self.commands().complete_execution(
            command_id=command["id"], epoch=fix["lease"]["epoch"],
            payload={
                "type": "execution_result", "protocol_major": 2,
                "command_id": command["id"], "run_id": command["run_id"],
                "tool_call_id": command["tool_call_id"],
                "state": "failed", "execution_phase": "outcome_unknown",
                "effects": "unknown", "error_code": "outcome_unknown",
                "error_message": "device lost", "started_at": 1, "finished_at": 2})
        cases["unknown"] = command
        return cases

    def _snapshot(self, command_ids):
        """Every stored fact about these commands, plus what points at them.

        The permits and handles are included because they are the rows a
        "rollback" could plausibly prune: they reference the commands, and
        dropping them would break the device's ability to reconcile later.
        """
        store = self.app.service._store
        rows = {}
        for table, key in (("desktop_commands", "id"),
                           ("desktop_execution_permits", "command_id"),
                           ("desktop_process_handles", "command_id")):
            placeholders = ",".join("?" for _ in command_ids)
            rows[table] = sorted(
                tuple(sorted(dict(row).items()))
                for row in store.execute(
                    "SELECT * FROM %s WHERE %s IN (%s)" % (table, key, placeholders),
                    tuple(command_ids)))
        return rows

    # -- what the switch-off does ------------------------------------------

    def test_closing_the_switch_stops_new_calls(self):
        """Every broker path refuses, by name, before any authorization runs."""
        self._drill_rows()
        with self._config(desktop_project_execution_enabled=False,
                          desktop_project_scripts_enabled=False):
            token = self.native()
            for path, body, method in (
                (PREPARE, {"command_id": "c"}, "POST"),
                (START, {"command_id": "c"}, "POST"),
                (HEARTBEAT, {"command_id": "c"}, "POST"),
                (STATUS, {"command_id": "c"}, "GET"),
            ):
                response = self.call(path, body, token=token, method=method)
                self.refusal(response, status=503, code="feature_unavailable")

    def test_closing_the_switch_deletes_and_rewrites_nothing(self):
        """The rows are byte-for-byte the same with the switch off."""
        cases = self._drill_rows()
        ids = [command["id"] for command in cases.values()]
        before = self._snapshot(ids)

        with self._config(desktop_project_execution_enabled=False):
            # A refused call must not "helpfully" terminate the in-flight row.
            self.call(PREPARE, {"command_id": ids[0]}, token=self.native())
        self.assertEqual(self._snapshot(ids), before)

    def test_reopening_restores_the_same_answers(self):
        """After the switch comes back, each row still means what it meant."""
        cases = self._drill_rows()
        with self._config(desktop_project_execution_enabled=False):
            pass
        with self._config(desktop_project_execution_enabled=True):
            running = self.ok(self.call(STATUS, self.body(cases["running"]),
                                        method="GET"))
            finished = self.ok(self.call(STATUS, self.body(cases["finished"]),
                                         method="GET"))
            unknown = self.ok(self.call(STATUS, self.body(cases["unknown"]),
                                        method="GET"))

        self.assertEqual(running["state"], "running")
        self.assertTrue(running["started"])
        self.assertFalse(running["terminal"])
        self.assertIsNotNone(running["reconcile"])
        self.assertIn("journal", running["reconcile"])

        self.assertEqual(finished["state"], "succeeded")
        self.assertTrue(finished["terminal"])
        self.assertFalse(finished["outcome_unknown"])
        self.assertIsNone(finished["reconcile"])

        self.assertEqual(unknown["state"], "failed")
        self.assertEqual(unknown["phase"], "outcome_unknown")
        self.assertTrue(unknown["outcome_unknown"])
        self.assertEqual(unknown["effects"], "unknown")

    def test_the_in_flight_row_keeps_the_journal_and_permit_it_started_with(self):
        """The receipts survive the rollback, so reconciliation stays possible."""
        cases = self._drill_rows()
        before = self.row(cases["running"])
        with self._config(desktop_project_execution_enabled=False):
            pass
        after = self.row(cases["running"])
        self.assertEqual(after["journal_id"], "journal_in_flight")
        self.assertEqual(after["permit_id"], before["permit_id"])
        self.assertEqual(after["started_at"], before["started_at"])

    def test_the_unknown_row_is_never_auto_rerun(self):
        """An unknown outcome is preserved as unknown, and nothing re-dispatches.

        The one outcome that must never be "cleaned up": turning it into a plain
        failure would make the next redelivery look like a fresh attempt, and the
        user's original files are the only place the truth is stored.
        """
        cases = self._drill_rows()
        ids = [command["id"] for command in cases.values()]
        store = self.app.service._store
        outbox_before = len(store.execute("SELECT id FROM desktop_command_outbox"))
        before = self._snapshot(ids)

        with self._config(desktop_project_execution_enabled=False):
            pass
        with self._config(desktop_project_execution_enabled=True):
            # A reconnecting device asks twice; asking must not change anything.
            for _ in range(2):
                data = self.ok(self.call(STATUS, self.body(cases["unknown"]),
                                         method="GET"))
                self.assertTrue(data["outcome_unknown"])
                self.assertEqual(data["effects"], "unknown")
                self.assertIn("do not run this command again automatically",
                              data["reconcile"])

        self.assertEqual(self._snapshot(ids), before)
        self.assertEqual(
            len(store.execute("SELECT id FROM desktop_command_outbox")),
            outbox_before,
            "a rollback must not enqueue a replacement attempt")

    def test_a_degraded_client_is_told_which_reason_applies(self):
        """The meta projection answers why, so an old client can explain it."""
        from integrations.desktop import execution_capability as ec

        settings = self.app._settings
        state = ec.execution_state(
            settings=dict(settings, desktop_project_execution_enabled=False),
            platform="posix", runtime="cpython-test")
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "disabled_by_deployment")
        self.assertEqual(state["surfaces"]["files"]["reason"],
                         "disabled_by_deployment")

        reopened = ec.execution_state(
            settings=dict(settings, desktop_project_execution_enabled=True),
            platform="posix", runtime="cpython-test")
        self.assertTrue(reopened["available"])

    def test_the_rollback_never_routes_the_work_to_the_server(self):
        """The dangerous "fallback": refuse, do not run in a server directory.

        With the switch closed a delegation is not planned, and the tool layer
        reads the desktop target with no resolvable directory as a refusal. That
        pair is the requirement: the work does not move to the server, it stops.
        """
        from agent.desktop_local.run_context import REFUSAL_UNAVAILABLE, run_local_cwd
        from agent.desktop_remote.mode import remote_mode_for
        from agent.workspace.execution_target import desktop_target
        from common.runtime_identity import RuntimeIdentity

        target = desktop_target(device_id="d1", workspace_id="w1", binding_id="b1",
                                grant_version=1, project_mode="project-execution")
        identity = RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=target)

        with self._config(desktop_project_execution_enabled=False,
                          desktop_project_scripts_enabled=False):
            self.assertFalse(remote_mode_for(identity))
        # This process is the server in this scenario: no registry entry, so the
        # frozen directory is empty and the run is refused rather than retargeted.
        cwd, refusal = run_local_cwd(identity)
        self.assertIsNone(cwd)
        self.assertEqual(refusal, REFUSAL_UNAVAILABLE)
        self.assertIn("没有改用服务器目录", refusal)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
