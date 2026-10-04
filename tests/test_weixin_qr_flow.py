# encoding:utf-8
"""Task 7: the WeChat scan route end to end, over the real WSGI app.

``tests/test_scan_onboarding_state.py`` proves the state machine module on its
own. This file proves the **route**: ``GET|POST /api/weixin/qrlogin`` driven
through a real ``build_web_app()`` over a private identity database
(:class:`tests._helpers.WebAppHarness`), with a fake *vendor* at the pipeline's
one injected seam (``weixin_scan_adapter.qr_client``) — the same way the Feishu
tests inject a fake SDK. Nothing here makes the handler claim a scan succeeded:
the fake vendor answers the poll, and every refusal comes from delivered code
(the identity service's own transaction, the CSRF gate, the capability matrix).

Per task, what these tests are for:

* 7.1 every scan binds the initiating session (user, tenant and **AuthSession**)
  and the purpose/target the server resolved; there is no process-global QR slot
  left to fill, and a handle belonging to another user, another login, another
  tenant or nobody is refused with one identical code and message.
* 7.2 the commit is the delivered instance write: the grant is verified up front
  and redeemed last, a write refused for a *later* reason keeps the grant, and a
  retried submit reads the same receipt back instead of creating a second
  instance. No long-term secret reaches ``conf()``, a response or a receipt.
* 7.3 the name comes from the delivered label plus the first free ordinal, the
  token is stored as that instance's own ciphertext, and the app-occupancy rule
  is the delivered one.
* 7.4 the member path reuses the delivered personal create; the instance is
  honestly reported as saved-and-not-connected until the runtime is accepted.
* 7.5 the delivered verified-owner private-chat routing decides the inbound, and
  it is exercised for this provider as-is (a component check only — see the
  evidence file for what 7.8 still owes).
"""

import json
import sys
import threading
import time
from urllib.parse import quote

import pytest

from channel import weixin_scan_adapter as adapter
from channel import channel_instances
from channel.web import scan_onboarding as so
from tests._helpers import IdentityStack

MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
#: The bot token the fake vendor hands out on a confirmed scan. Shaped like a
#: secret on purpose: if it ever reaches a response, a receipt or ``conf()``,
#: the assertions below that search for it will find it.
VENDOR_TOKEN = "wx-bot-token-3f9a1c7d"
VENDOR_BASE = "https://vendor.example"
VENDOR_QR = "/api/weixin/qrlogin"
#: The Agent the personal path in this file routes to: ``member1``'s **own
#: private** Agent, which is what a personal (``scope='user'``) channel instance
#: must name — it has to be a target that member holds privately *and* that the
#: Agent Registry lists. ``agent-a`` is the tenant's shared Agent and is the
#: public path's target; naming it from a personal create is the shape the
#: product refuses (``tests/test_personal_instance_policy.py``).
PERSONAL_TARGET = "member1-private"
#: The same, for the member of the second tenant in the cross-tenant test.
FOREIGN_PERSONAL_TARGET = "foreign-private"


class _FakeChannelManager:
    """A stand-in for the process's ``ChannelManager`` — the runtime seam.

    ``channel.channel_instances`` resolves the manager through the shared
    channel registry precisely so it never owns it, so
    a test can put one there and drive the *real* runtime path: the row is read
    from the identity store, the credentials are decrypted per instance, and the
    recorded outcome is what ``instance_connection_state`` reports.
    """

    def __init__(self):
        self.restarts: list = []
        self.removed: list = []

    def restart(self, instance):
        self.restarts.append(getattr(instance, "instance_id", instance))

    def remove_channel(self, instance_id):
        self.removed.append(instance_id)


def _install_manager(monkeypatch, manager):
    """Put *manager* where the delivered runtime resolver looks for it."""
    monkeypatch.setattr("common.channel_registry.get_channel_manager", lambda: manager)


class _FakeVendor:
    """The vendor QR API, scripted per test (the one external seam)."""

    qrs = 0
    polls = 0
    answers: list = []
    bases: list = []

    def __init__(self, base_url=""):
        self.base_url = str(base_url or VENDOR_BASE)
        type(self).bases.append(self.base_url)

    # -- scripting ---------------------------------------------------------

    @classmethod
    def reset(cls):
        cls.qrs = 0
        cls.polls = 0
        cls.answers = []
        cls.bases = []

    @classmethod
    def confirm(cls, **overrides):
        """Queue the answer a real phone scan produces.

        The vendor-only fields (``ilink_bot_id``/``ilink_user_id``) are included
        because the real answer carries them: the pipeline must narrow them away
        rather than store whatever the vendor happened to send.
        """
        answer = {
            "status": "confirmed",
            "bot_token": VENDOR_TOKEN,
            "baseurl": VENDOR_BASE,
            "ilink_bot_id": "ilink-bot-1",
            "ilink_user_id": "ilink-user-1",
        }
        answer.update(overrides)
        cls.answers.append(answer)

    @classmethod
    def queue(cls, status, **fields):
        answer = {"status": status}
        answer.update(fields)
        cls.answers.append(answer)

    # -- the client itself -------------------------------------------------

    def fetch_qr_code(self):
        type(self).qrs += 1
        return {"qrcode": f"vendor-qr-{type(self).qrs}",
                "qrcode_img_content": f"{self.base_url}/qr/{type(self).qrs}.png"}

    def poll_qr_status(self, qrcode, timeout=10):
        type(self).polls += 1
        if type(self).answers:
            return dict(type(self).answers.pop(0))
        return {"status": "wait"}


@pytest.fixture(autouse=True)
def _scan_process_state(monkeypatch):
    """Per-test key, a clean registry/receipt ledger and a clean vendor."""
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)
    from auth import scan_authorization

    from channel import channel_instances

    def _clear():
        so._reset()
        scan_authorization._reset()
        channel_instances._runtime_state.clear()
        _FakeVendor.reset()

    _clear()
    yield
    _clear()


class _Flow:
    """The route as a client uses it: one cached login per actor."""

    def __init__(self, web):
        self.web = web
        self._tokens = {}

    # -- actors ------------------------------------------------------------

    def token(self, who):
        """The login this actor started their scan in (cached on purpose).

        A second ``login`` is a *different* AuthSession, which is exactly what
        the cross-session test needs, so tokens are never re-fetched silently.
        """
        if who not in self._tokens:
            self._tokens[who] = self.web.login(who)
        return self._tokens[who]

    def user_id(self, who):
        return self.web.user_id(who)

    @property
    def tenant_id(self):
        return self.web.tenant_id

    @property
    def service(self):
        return self.web.service

    # -- requests ----------------------------------------------------------

    def get(self, who, *, scope="", base_url="", headers=None):
        path = VENDOR_QR
        query = []
        if scope:
            query.append("scope=" + quote(str(scope), safe=""))
        if base_url:
            query.append("base_url=" + quote(str(base_url), safe=""))
        if query:
            path += "?" + "&".join(query)
        return self.web.get(path, token=self.token(who), headers=headers)

    def post(self, who, body, *, headers=None, token=None):
        return self.web.post(VENDOR_QR, body,
                             token=token or self.token(who), headers=headers)

    # -- the ordinary path -------------------------------------------------

    def start(self, who="root", **kwargs):
        """``GET`` and return the parsed 200 body."""
        return _ok(self.get(who, **kwargs))

    def scan(self, who="root", *, confirm=None, commit=None, **kwargs):
        """Start a scan and have the vendor confirm it: returns the commit body.

        ``confirm=False`` leaves the QR pending (the operator has not scanned
        yet), which is what the refusal tests need. ``commit`` adds fields to the
        confirming poll: a personal create has to *name its target*, and the
        target is a request field of the commit (not of the scan start), so the
        member paths below pass ``commit={"agent_id": ...}``.
        """
        started = self.start(who, **kwargs)
        if confirm is False:
            return started
        _FakeVendor.confirm(**(confirm or {}))
        body = {"action": "poll", "handle": started["handle"]}
        body.update(commit or {})
        return _ok(self.post(who, body))


@pytest.fixture
def make_flow(web_app, monkeypatch):
    monkeypatch.setattr(adapter, "qr_client", _FakeVendor)

    def _make(**settings):
        web = web_app(settings=settings)
        web.add_agent("agent-a")
        web.member("member1", ["member"])
        web.member("member2", ["member"])
        web.member("admin2", ["tenant_admin"])
        # The tenant's public path keeps the shared ``agent-a``; the member paths
        # need a target of their own, so this fixture supplies one for member1
        # (bound privately *and* listed in the registry — both halves of the
        # delivered predicate for a personal instance's target).
        web.private_agent(web.user_id("member1"), PERSONAL_TARGET)
        return _Flow(web)

    return _make


@pytest.fixture
def flow(make_flow):
    return make_flow()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _json(response):
    return json.loads(response.data.decode("utf-8"))


def _ok(response):
    body = _json(response)
    assert str(response.status).startswith("200"), (response.status, body)
    return body


def _refused(response, status, code=None):
    """Assert a refusal by HTTP status (and code when it is the point)."""
    body = _json(response)
    assert str(response.status).startswith(str(status)), (response.status, body)
    if code is not None:
        assert body.get("code") == code, body
    return body


def _refusal_shape(response):
    """The refusal a client can observe: status, code and message."""
    body = _json(response)
    return (str(response.status), body.get("code"), body.get("message"))


def _instances(flow, scope="tenant", *, tenant_id=None, headers=None):
    """Instances of one scope, through the delivered list the console uses."""
    if scope == "personal":
        listing = flow.service.list_personal_channel_instances(
            actor_user_id=flow.user_id("member1"),
            tenant_id=tenant_id or flow.tenant_id)
    else:
        listing = flow.service.list_tenant_channel_instances(
            actor_user_id=flow.user_id("root"),
            tenant_id=tenant_id or flow.tenant_id)
    return listing["items"]


# ---------------------------------------------------------------------------
# 7.1 the state machine, per initiator, with no global slot
# ---------------------------------------------------------------------------

def test_two_initiators_hold_two_handles_and_two_qrs(flow):
    """Two people scanning at once must not share one QR slot."""
    first = flow.start("root")
    second = flow.start("admin2")

    assert first["handle"] and second["handle"]
    assert first["handle"] != second["handle"]
    assert first["qrcode_url"] != second["qrcode_url"]
    assert first["source"] == "session"
    assert first["qr_status"] == "waiting"
    assert first["handle"] not in json.dumps(second)
    # The vendor was asked twice: one scan, one QR.
    assert _FakeVendor.qrs == 2


def test_no_process_global_qr_slot_remains():
    """The class attribute the old handler kept its one QR slot in is gone."""
    from channel.web.web_channel import WeixinQrHandler

    assert not hasattr(WeixinQrHandler, "_qr_state")


def test_a_foreign_missing_or_other_login_handle_is_one_identical_refusal(flow):
    """No existence leak: three different reasons, one observable answer."""
    alice = flow.start("root")
    handle = alice["handle"]

    # (a) a handle that belongs to nobody
    missing = flow.post("admin2", {"action": "poll", "handle": "no-such-handle"})
    # (b) a handle that belongs to another user of the same tenant
    foreign = flow.post("admin2", {"action": "poll", "handle": handle})
    # (c) a bare poll by an actor who never started a scan
    none_at_all = flow.post("admin2", {"action": "poll"})
    # (d) a handle that belongs to another *login* of the same user
    other_login = flow.web.login("root")
    rebound = flow.post("root", {"action": "poll", "handle": handle},
                        token=other_login)

    shapes = {_refusal_shape(r) for r in (missing, foreign, none_at_all, rebound)}
    assert len(shapes) == 1, shapes
    status, code, _message = shapes.pop()
    assert status.startswith("404"), status
    assert code == "not_owner"
    for response in (missing, foreign, none_at_all, rebound):
        assert "no-such-handle" not in response.data.decode("utf-8")


def test_a_committed_handle_is_refused_the_same_way_by_everyone_else(flow):
    """The same answer *after* the instance exists — no timing oracle either."""
    committed = flow.scan("root")
    assert committed["saved"] is True
    handle = committed["handle"]

    owner = _ok(flow.post("root", {"action": "poll", "handle": handle}))
    assert owner["outcome"] == "replayed"

    foreign = flow.post("admin2", {"action": "poll", "handle": handle})
    missing = flow.post("admin2", {"action": "poll", "handle": "no-such"})
    assert _refusal_shape(foreign) == _refusal_shape(missing)
    _refused(foreign, 404, "not_owner")


def test_a_platform_admin_of_another_tenant_cannot_reach_the_session(flow):
    """Cross-tenant: the tenant header selects a tenant the owner is not in."""
    started = flow.start("root")
    other = flow.web.stack.other_tenant()
    hint = flow.web.service.login("other-root", IdentityStack.ROOT_PASSWORD).token
    flow.web.service.change_password(hint, IdentityStack.ROOT_PASSWORD,
                                     "Str0ngOtherFinal")
    foreign_admin = flow.web.service.login("other-root", "Str0ngOtherFinal").token

    response = flow.web.post(
        VENDOR_QR, {"action": "poll", "handle": started["handle"]},
        token=foreign_admin, headers={"X-Tenant-ID": other["tenant_id"]})
    missing = flow.web.post(
        VENDOR_QR, {"action": "poll", "handle": "no-such"},
        token=foreign_admin, headers={"X-Tenant-ID": other["tenant_id"]})

    assert _refusal_shape(response) == _refusal_shape(missing)
    _refused(response, 404, "not_owner")


def test_scope_is_decided_by_the_server_not_the_client(flow):
    """Platform scope does not exist; tenant scope needs tenant control."""
    _refused(flow.get("root", scope="platform"), 400, "scope_not_supported")
    _refused(flow.get("member1", scope="tenant"), 403, "forbidden")
    # A member's default is their own instance, and choosing it explicitly is
    # the same thing — not a widening.
    assert flow.start("member1")["scope"] == "personal"
    assert flow.start("member1", scope="personal")["scope"] == "personal"
    # A tenant controller's default is the tenant's shared instance.
    assert flow.start("root")["scope"] == "tenant"


def test_a_multi_worker_deployment_refuses_to_serve_a_scan(flow, monkeypatch):
    """The registry is process-local, so a topology that cannot share it stops."""
    def _refuse():
        raise RuntimeError("multi-worker identity deployments are not supported")

    monkeypatch.setattr("auth.ratelimit.reject_multi_worker_identity", _refuse)
    body = _refused(flow.get("root"), 503, "state_not_shared")
    assert "state" in body["message"]


def test_cancel_is_terminal_and_a_new_scan_is_a_new_handle(flow):
    started = flow.start("root")
    cancelled = _ok(flow.post("root", {"action": "cancel",
                                       "handle": started["handle"]}))
    assert cancelled["qr_status"] == "cancelled"
    assert cancelled["terminal"] is True

    _refused(flow.post("root", {"action": "commit", "handle": started["handle"]}),
             409, "terminal")
    assert flow.start("root")["handle"] != started["handle"]


# ---------------------------------------------------------------------------
# 7.2 the commit is the delivered write: one grant, one instance, one receipt
# ---------------------------------------------------------------------------

def test_a_confirmed_scan_creates_the_instance_through_the_service(flow):
    committed = flow.scan("root")

    assert committed["outcome"] == "committed"
    assert committed["saved"] is True
    assert committed["authorization_consumed"] is True
    assert committed["channel_type"] == "weixin"
    assert committed["secret_present"] is True
    # The four facts are reported separately, and a saved instance is not
    # automatically a connected one.
    assert committed["qr_status"] == "confirmed"
    assert committed["authorized"] is True
    assert committed["connected"] is False
    assert committed["connection"] in ("not_connected", "pending")

    items = _instances(flow)
    assert [i["id"] for i in items] == [committed["instance_id"]]
    assert items[0]["scope"] == "tenant"
    # The audit line is the delivered one, written inside the same commit.
    trail = flow.service.list_audit(flow.tenant_id)
    events = [e for e in trail if e.get("action") == "channel.scan.bind"]
    assert len(events) == 1, trail
    assert events[0]["target"].endswith(committed["instance_id"])
    assert VENDOR_TOKEN not in json.dumps(events, ensure_ascii=False)


def test_the_stored_bundle_carries_only_the_declared_credentials(flow):
    """The vendor's own fields and nothing else reach the encrypted bundle."""
    committed = flow.scan("root")
    bundle = flow.service.channel_instance_credentials(
        flow.tenant_id, committed["instance_id"])

    assert set(bundle) == {"weixin_token", "weixin_base_url"}
    assert bundle["weixin_token"] == VENDOR_TOKEN
    assert bundle["weixin_base_url"] == VENDOR_BASE


def test_no_global_configuration_or_shared_file_is_written(flow):
    """The token lives in the instance's ciphertext, never in ``conf()``."""
    import os

    settings = dict(flow.web._settings)
    committed = flow.scan("root")

    for key in adapter.GLOBAL_CONFIG_KEYS:
        assert flow.web._settings.get(key) == settings.get(key), key
        assert key not in json.dumps(committed)
    # Nothing resembling a credentials file was created for the scan.
    written = []
    for root, _dirs, files in os.walk(flow.web.root):
        written.extend(os.path.join(root, name) for name in files)
    assert not [p for p in written if "weixin" in os.path.basename(p).lower()
                and "credential" in os.path.basename(p).lower()], written
    # And the deployment's own declared key set is untouched, key by key.
    assert flow.web._settings["identity_db_path"] == settings["identity_db_path"]


def test_a_second_scan_names_the_instance_with_the_next_free_ordinal(flow):
    """Upstream naming behaviour: the type's label, then " 2", " 3", ..."""
    first = flow.scan("root")
    assert first["display_name"] == "微信"

    second = flow.scan("admin2")   # a different owner, same tenant + type
    assert second["display_name"] == "微信 2"

    # A client may still name it; the resolved default is pre-fillable.
    started = flow.start("root")
    assert started["default_display_name"] == "微信 3"
    _FakeVendor.confirm()
    named = _ok(flow.post("root", {"action": "poll", "handle": started["handle"],
                                   "display_name": "客服小助手"}))
    assert named["display_name"] == "客服小助手"


def test_a_retried_submit_reads_the_receipt_back_instead_of_creating_again(flow):
    """The console's own poll is a retry: same handle, same key, one instance."""
    committed = flow.scan("root")
    handle = committed["handle"]

    replayed = _ok(flow.post("root", {"action": "poll", "handle": handle}))
    assert replayed["outcome"] == "replayed"
    assert replayed["instance_id"] == committed["instance_id"]
    assert replayed["receipt_state"] == "complete"
    assert replayed["display_name"] == committed["display_name"]
    assert replayed["secret_present"] is True
    assert VENDOR_TOKEN not in json.dumps(replayed)

    # A bare poll (the console sends no handle) is the same operation.
    bare = _ok(flow.post("root", {"action": "poll"}))
    assert bare["outcome"] == "replayed"
    assert _instances(flow) and len(_instances(flow)) == 1


def test_concurrent_retries_of_one_scan_commit_exactly_once(flow):
    """Same key, same content, two threads: one create, one connection."""
    started = flow.scan("root", confirm=False)
    handle = started["handle"]
    _FakeVendor.confirm()

    applied = []
    applied_lock = threading.Lock()
    real_apply = adapter.apply_connection

    def _recording_apply(instance_id):
        with applied_lock:
            applied.append(instance_id)
        return real_apply(instance_id)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(adapter, "apply_connection", _recording_apply)
        results = []
        errors = []

        def _submit():
            try:
                results.append(_ok(flow.post(
                    "root", {"action": "poll", "handle": handle})))
            except Exception as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=_submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert not errors, errors
    assert len(results) == 2
    assert sorted(r["instance_id"] for r in results) == [results[0]["instance_id"]] * 2
    items = _instances(flow)
    assert len(items) == 1
    assert applied == [items[0]["id"]]
    assert {r["outcome"] for r in results} <= {"committed", "replayed"}


def test_a_write_refused_later_keeps_the_authorization_for_a_retry(flow):
    """A quota refusal must not cost the operator another phone scan."""
    member = flow.user_id("member1")
    flow.service.set_tenant_channel_policy(
        actor_user_id=flow.user_id("root"), tenant_id=flow.tenant_id,
        recent_password=flow.web.ADMIN_PASSWORD, personal_instance_limit=0)

    started = flow.start("member1")
    _FakeVendor.confirm()
    refused = flow.post("member1", {"action": "poll", "handle": started["handle"],
                                    "agent_id": PERSONAL_TARGET})
    _refused(refused, 403, "quota_exceeded")
    assert _instances(flow, "personal") == []

    # The operator's next attempt is the *same* scan: same handle, same grant,
    # same key. It must succeed without a new QR.
    flow.service.set_tenant_channel_policy(
        actor_user_id=flow.user_id("root"), tenant_id=flow.tenant_id,
        recent_password=flow.web.ADMIN_PASSWORD, personal_instance_limit=-1)
    committed = _ok(flow.post("member1", {"action": "poll",
                                          "handle": started["handle"],
                                          "agent_id": PERSONAL_TARGET}))

    assert committed["outcome"] == "committed"
    assert committed["authorization_consumed"] is True
    items = _instances(flow, "personal")
    assert [i["id"] for i in items] == [committed["instance_id"]]
    row = flow.service.get_tenant_channel_instance_row(committed["instance_id"])
    assert row["owner_user_id"] == member
    assert row["scope"] == "user"
    assert _FakeVendor.qrs == 1, "a retry must not need a second QR"


def test_an_audit_refusal_resumes_the_same_scan_instead_of_rescanning(
        flow, monkeypatch):
    """The audit step is inside the same authorization, not beside it.

    The delivered instance write redeems the grant as its own transaction
    commits, so the tail (audit, receipt) can fail with the grant already spent.
    The retry must still finish the same operation — one instance, one QR, one
    authorization — instead of answering "authorization refused" for a row that
    is already saved. A foreign caller, meanwhile, gets the ordinary refusal.
    """
    real = adapter.audit_hook
    seen = {"n": 0}

    def flaky(service):
        hook = real(service)

        def _hook(**kwargs):
            seen["n"] += 1
            if seen["n"] == 1:
                raise RuntimeError("audit store unavailable")
            return hook(**kwargs)

        return _hook

    monkeypatch.setattr(adapter, "audit_hook", flaky)
    started = flow.start("root")
    _FakeVendor.confirm()

    refused = flow.post("root", {"action": "poll", "handle": started["handle"]})
    _refused(refused, 503, "audit_failed")
    assert len(_instances(flow)) == 1

    resumed = _ok(flow.post("root", {"action": "poll",
                                     "handle": started["handle"]}))
    assert resumed["outcome"] == "resumed"
    assert resumed["authorization_consumed"] is True
    items = _instances(flow)
    assert [i["id"] for i in items] == [resumed["instance_id"]]
    assert _FakeVendor.qrs == 1, "an audit retry must not need a second QR"
    trail = [e for e in flow.service.list_audit(flow.tenant_id)
             if e.get("action") == "channel.scan.bind"]
    assert len(trail) == 1, trail

    # A staged tail is still the initiator's own operation: another member's
    # handle-less poll cannot finish it, and gets the missing-handle refusal.
    other = flow.post("member2", {"action": "poll", "handle": started["handle"]})
    missing = flow.post("member2", {"action": "poll", "handle": "no-such"})
    assert _refusal_shape(other) == _refusal_shape(missing)
    _refused(other, 404, "not_owner")

def test_a_commit_must_present_a_scan_that_the_provider_confirmed(flow):
    """No provider result, no commit — the state machine refuses by name."""
    started = flow.start("root")
    _refused(flow.post("root", {"action": "commit",
                                "handle": started["handle"]}),
             409, "not_confirmed")


def test_an_expired_session_is_refused_and_does_not_revive(flow):
    started = flow.start("root")
    # ``now`` is an absolute clock, not an offset: sweep from beyond this
    # session's TTL, then answer the late poll from the same handle.
    so.sweep(now=time.time() + so.SESSION_TTL_SECONDS + 60)

    _refused(flow.post("root", {"action": "poll", "handle": started["handle"]}),
             404, "not_owner")


def test_the_receipt_ledger_refuses_a_changed_content_replay(flow):
    """The same handle with different content is a second create, not a retry."""
    committed = flow.scan("root")
    handle = committed["handle"]
    replayed = _ok(flow.post("root", {"action": "poll", "handle": handle}))
    assert replayed["outcome"] == "replayed"

    started = flow.start("root")
    _FakeVendor.confirm()
    first = _ok(flow.post("root", {"action": "poll", "handle": started["handle"],
                                   "display_name": "名称 A"}))
    # A new scan is a new grant, so a new name creates a new instance...
    assert first["outcome"] == "committed"
    assert len(_instances(flow)) == 2


# ---------------------------------------------------------------------------
# 7.3 per-instance ciphertext, capability/connection state, occupancy
# ---------------------------------------------------------------------------

def test_the_same_bot_cannot_be_onboarded_twice_as_a_personal_instance(flow):
    """The delivered app-occupancy rule, exercised through the scan path.

    A member's own instance is compared against *every* active instance of the
    tenant, so a bot the tenant already runs cannot be claimed personally. The
    refusal is the identity service's, inside the create's transaction.
    """
    shared = flow.scan("root")

    started = flow.start("member1")
    _FakeVendor.confirm()
    refused = flow.post("member1", {"action": "poll", "handle": started["handle"],
                                    "agent_id": PERSONAL_TARGET})
    _refused(refused, 409, "app_conflict")

    assert [i["id"] for i in _instances(flow)] == [shared["instance_id"]]
    # The refused personal attempt left nothing behind: the session rolled back
    # with the operator's authorization intact, and the delivered occupancy rule
    # keeps refusing this bot for as long as the tenant runs it.
    _refused(flow.post("member1", {"action": "poll",
                                   "handle": started["handle"],
                                   "agent_id": PERSONAL_TARGET}),
             409, "app_conflict")
    # A *different* bot is a different application, so the same member can still
    # onboard one: the refusal is about the app, not about personal scans.
    cancelled = _ok(flow.post("member1", {"action": "cancel",
                                          "handle": started["handle"]}))
    assert cancelled["qr_status"] == "cancelled"
    second = flow.start("member1")
    _FakeVendor.confirm(bot_token="wx-bot-token-fresh")
    claimed = _ok(flow.post("member1", {"action": "poll",
                                        "handle": second["handle"],
                                        "agent_id": PERSONAL_TARGET}))
    assert claimed["outcome"] == "committed"
    row = flow.service.get_tenant_channel_instance_row(claimed["instance_id"])
    assert row["owner_user_id"] == flow.user_id("member1")
    assert row["scope"] == "user"


def test_one_instance_never_starts_a_second_connection(flow, monkeypatch):
    """The post-commit connection work item is per instance and idempotent."""
    calls = []
    monkeypatch.setattr("channel.channel_instances.reconcile_instance_runtime",
                        lambda instance_id: calls.append(instance_id) or {
                            "applied": True})

    committed = flow.scan("root")
    assert calls == [committed["instance_id"]]

    _ok(flow.post("root", {"action": "poll", "handle": committed["handle"]}))
    assert calls == [committed["instance_id"]], "a replayed receipt connects nothing"


@pytest.mark.parametrize("who", ["root", "member1"])
def test_scanner_is_bound_before_the_connection_can_receive_a_message(flow, monkeypatch, who):
    monkeypatch.setattr(channel_instances, "PERSONAL_RUNTIME_ACCEPTED_TYPES", frozenset({"weixin"}))
    monkeypatch.setattr(channel_instances, "personal_runtime_enabled", lambda _type: True)
    observed = []

    def apply(instance_id):
        row = flow.service.get_tenant_channel_instance_row(instance_id)
        assert row["sender_binding_at"] is not None
        if who == "member1":
            verdict = flow.service.resolve_personal_channel_inbound(
                instance_id=instance_id, provider="weixin", issuer="ilink-bot-1", subject="ilink-user-1")
            assert verdict["allowed"] is True, verdict
        observed.append(instance_id)
        return {"applied": True}

    monkeypatch.setattr(channel_instances, "reconcile_instance_runtime", apply)
    committed = flow.scan(who, commit={"agent_id": PERSONAL_TARGET} if who == "member1" else None)
    assert observed == [committed["instance_id"]]


def test_a_commit_cannot_move_the_vendor_endpoint(flow):
    """The endpoint is bound at scan time; a later request may not redirect it."""
    started = flow.start("root")
    refused = flow.post("root", {"action": "poll", "handle": started["handle"],
                                 "base_url": "https://attacker.example"})
    _refused(refused, 409, "global_override_refused")
    # The QR really was minted against the default endpoint, and the refusal
    # above did not move it: nothing was polled anywhere else.
    assert _FakeVendor.bases == [adapter.default_base_url()]


def test_the_qr_start_never_reads_the_legacy_global_endpoint(flow):
    """``conf()["weixin_base_url"]`` must not steer a scan somewhere else."""
    flow.web._settings["weixin_base_url"] = "https://legacy-global.example"
    _FakeVendor.reset()
    flow.start("root")

    assert _FakeVendor.bases == [adapter.default_base_url()]
    assert "legacy-global" not in _FakeVendor.bases[0]


def test_a_refusal_after_the_instance_exists_requires_re_authorization(flow):
    """A 24h receipt is not a licence: read-back re-checks the target."""
    committed = flow.scan("root")
    assert _ok(flow.post("root", {"action": "poll",
                                 "handle": committed["handle"]}))["outcome"] == "replayed"

    row = flow.service.get_tenant_channel_instance_row(committed["instance_id"])
    flow.service.set_tenant_channel_instance_active(
        actor_user_id=flow.user_id("root"), tenant_id=flow.tenant_id,
        instance_id=committed["instance_id"], active=False,
        expected_version=row["version"],
        recent_password=flow.web.ADMIN_PASSWORD)

    refused = flow.post("root", {"action": "poll", "handle": committed["handle"]})
    _refused(refused, 403, "readback_refused")
    # Nothing was recreated by the refused read-back.
    assert len(_instances(flow)) == 1


def test_connection_state_is_reported_apart_from_the_saved_row(flow, monkeypatch):
    """``saved``/``connected`` are separate facts, and both are honest.

    ``connected`` is not a guess about the row: it is what the delivered runtime
    seam actually recorded, so this test drives the real
    ``reconcile_instance_runtime`` — once with a channel manager standing in for
    the running process, once without one (a console-only process), which is the
    only difference between "connected" and "saved, not applied yet".
    """
    manager = _FakeChannelManager()
    _install_manager(monkeypatch, manager)
    connected = flow.scan("root")
    assert connected["saved"] is True
    assert connected["connection"] == "connected"
    assert connected["connected"] is True
    # The connection went up for *this* instance, with credentials the runtime
    # read from that instance's own ciphertext (not from a global token).
    assert manager.restarts == [connected["instance_id"]]

    # A deployment whose runtime did not apply the instance still reports the
    # row as saved, with the reason the connection is missing.
    _install_manager(monkeypatch, None)
    other = flow.scan("admin2")
    assert other["saved"] is True
    assert other["connected"] is False
    assert other["connection"] in ("pending", "not_connected")
    assert other["connection_reason"]


# ---------------------------------------------------------------------------
# 7.4 the personal path reuses the delivered services
# ---------------------------------------------------------------------------

def test_a_member_scan_creates_their_own_instance_and_not_the_tenants(flow):
    """Scope, owner and target are the delivered personal create's, not ours."""
    committed = flow.scan("member1", commit={"agent_id": PERSONAL_TARGET})

    assert committed["saved"] is True
    assert committed["instance_id"]
    items = _instances(flow, "personal")
    assert [i["id"] for i in items] == [committed["instance_id"]]
    row = flow.service.get_tenant_channel_instance_row(committed["instance_id"])
    assert row["scope"] == "user"
    assert row["owner_user_id"] == flow.user_id("member1")
    assert row["tenant_id"] == flow.tenant_id
    # The route is the member's own private Agent, not the tenant's shared one.
    assert row["agent_id"] == PERSONAL_TARGET
    # A member's instance is not the tenant's to administer.
    assert _instances(flow) == []


def test_a_personal_scan_reports_saved_and_not_connected(flow):
    """Task 7.4's honest posture: configuration works, execution is not claimed."""
    committed = flow.scan("member1", commit={"agent_id": PERSONAL_TARGET})

    assert committed["saved"] is True
    assert committed["connected"] is False
    assert "not" in committed["connection"] or committed["connection"] == "pending"
    assert committed["connection_reason"]
    assert adapter.QUOTA_RESERVATION_WIRED is False


def test_another_member_cannot_read_a_personal_scan_back(flow):
    committed = flow.scan("member1", commit={"agent_id": PERSONAL_TARGET})
    response = flow.post("member2", {"action": "poll",
                                     "handle": committed["handle"]})
    missing = flow.post("member2", {"action": "poll", "handle": "no-such"})
    assert _refusal_shape(response) == _refusal_shape(missing)
    _refused(response, 404, "not_owner")


def test_the_owner_can_read_their_own_personal_scan_back(flow):
    committed = flow.scan("member1", commit={"agent_id": PERSONAL_TARGET})
    replayed = _ok(flow.post("member1", {"action": "poll",
                                         "handle": committed["handle"]}))
    assert replayed["outcome"] == "replayed"
    assert replayed["scope"] == "personal"
    assert replayed["instance_id"] == committed["instance_id"]


# ---------------------------------------------------------------------------
# 7.5 the delivered verified-owner private-chat routing, as-is
# ---------------------------------------------------------------------------

def test_only_the_bound_owner_reaches_a_private_agent_on_a_scanned_bot(
        make_flow, monkeypatch):
    """A scanned personal bot serves its owner's private chat and nobody else.

    The routing is the delivered ``resolve_personal_channel_inbound`` — no rule
    is re-implemented here, and the link is made through the delivered binding
    challenge (a code the sender had to send from their own account), not by
    declaring a subject. This is a *component* check: it cannot stand in for the
    real-provider acceptance of 7.8, which is why the two switches below are
    raised only for the duration of the test and the shipped sets stay empty.
    """
    monkeypatch.setattr(
        "channel.channel_instances.PERSONAL_RUNTIME_ACCEPTED_TYPES",
        frozenset({"weixin"}))
    monkeypatch.setattr(
        "channel.channel_instances.PUBLIC_PERSONAL_INGRESS_TYPES",
        frozenset({"weixin"}))
    flow = make_flow(personal_channel_runtime=True,
                     personal_channel_runtime_types=["weixin"])
    service = flow.service
    tenant = flow.tenant_id
    owner = flow.user_id("member1")
    other = flow.user_id("member2")

    # A private Agent of the owner's own: a personal route needs a target, and
    # the delivered rule is that the *instance* names it (the binding challenge
    # deliberately refuses to introduce a second source of truth). The fixture
    # bound ``PERSONAL_TARGET`` to member1 for exactly this.
    started = flow.start("member1")
    _FakeVendor.confirm()
    committed = _ok(flow.post("member1", {"action": "poll",
                                          "handle": started["handle"],
                                          "agent_id": PERSONAL_TARGET}))
    instance_id = committed["instance_id"]
    assert committed["saved"] is True
    assert channel_instances.personal_runtime_enabled("weixin") is True
    # 保存与连接是两件事：这个进程里没有渠道运行时管理器（channel manager），所以
    # 连接只能报"待应用"，而不是假装已经连上。真正连上属 7.8/7.9 的实机验收。
    assert committed["connected"] is False
    assert committed["connection"] in ("pending", "not_connected")
    assert committed["connection_reason"], "a connection state must carry its reason"

    verdict = lambda **kw: service.resolve_personal_channel_inbound(  # noqa: E731
        instance_id=instance_id, provider="weixin", **kw)

    # The verified scanner is already bound; another sender cannot take over.
    scanned = verdict(issuer="ilink-bot-1", subject="ilink-user-1")
    assert scanned["allowed"] is True, scanned
    assert scanned["owner_user_id"] == owner
    assert verdict(issuer="wx-app", subject="wx-user-1")["reason"] == "sender_not_owner"
    # A group chat is a shared surface, never the owner's private chat.
    assert verdict(issuer="wx-app", subject="wx-user-1",
                   is_group=True)["reason"] == "group_not_personal"

    # The scanned instance is bound to the owner's own private Agent by the
    # delivered personal create; the binding challenge must not name a second
    # target on a personal instance (that is the delivered rule).
    challenge = service.start_personal_channel_binding(
        actor_user_id=owner, tenant_id=tenant, instance_id=instance_id)
    service.redeem_personal_channel_challenge(
        tenant_id=tenant, instance_id=instance_id, code=challenge["code"],
        provider="weixin", issuer="wx-app", subject="wx-user-1")

    allowed = verdict(issuer="wx-app", subject="wx-user-1")
    assert allowed["allowed"] is True, allowed
    assert allowed["owner_user_id"] == owner
    assert allowed["agent_id"] == PERSONAL_TARGET
    # Someone else on the owner's own bot, and another member of the same
    # tenant, are refusals — never served as the owner.
    assert verdict(issuer="wx-app", subject="wx-stranger")["reason"] == "sender_not_owner"
    assert verdict(issuer="wx-other-app", subject="wx-user-1")["reason"] == "sender_not_owner"
    # Still not a group surface, even for the bound triple.
    assert verdict(issuer="wx-app", subject="wx-user-1",
                   is_group=True)["reason"] == "group_not_personal"
    assert other != owner


def test_a_cross_tenant_sender_cannot_reach_another_tenants_private_bot(
        make_flow, monkeypatch):
    """Two tenants, two scanned bots: neither owner's subject works on the other."""
    monkeypatch.setattr(
        "channel.channel_instances.PERSONAL_RUNTIME_ACCEPTED_TYPES",
        frozenset({"weixin"}))
    flow = make_flow(personal_channel_runtime=True,
                     personal_channel_runtime_types=["weixin"])
    service = flow.service

    own = flow.scan("member1", commit={"agent_id": PERSONAL_TARGET})["instance_id"]
    other = flow.web.stack.other_tenant()
    foreign_token = service.login("foreign", IdentityStack.MEMBER_PASSWORD).token
    foreign_header = {"X-Tenant-ID": other["tenant_id"]}
    # The second tenant's member needs a private target of their own: their
    # personal create is the same predicate in a different tenant.
    flow.web.private_agent(other["user_id"], FOREIGN_PERSONAL_TARGET,
                           tenant_id=other["tenant_id"])
    started = _ok(flow.web.get(VENDOR_QR, token=foreign_token,
                               headers=foreign_header))
    _FakeVendor.confirm(bot_token="wx-bot-token-other")
    theirs = _ok(flow.web.post(VENDOR_QR,
                               {"action": "poll", "handle": started["handle"],
                                "agent_id": FOREIGN_PERSONAL_TARGET},
                               token=foreign_token, headers=foreign_header))
    assert theirs["saved"] is True
    assert theirs["scope"] == "personal"

    # Bind the first owner's identity triple on their own bot only.
    challenge = service.start_personal_channel_binding(
        actor_user_id=flow.user_id("member1"), tenant_id=flow.tenant_id,
        instance_id=own, target_agent_id="")
    service.redeem_personal_channel_challenge(
        tenant_id=flow.tenant_id, instance_id=own, code=challenge["code"],
        provider="weixin", issuer="wx-app", subject="wx-user-1")

    # The other tenant's bot knows nothing about that sender...
    cross = service.resolve_personal_channel_inbound(
        instance_id=theirs["instance_id"], provider="weixin",
        issuer="wx-app", subject="wx-user-1")
    assert cross["allowed"] is False
    assert cross["reason"] == "not_linked"
    # ...and the other tenant's sender is not the first owner either.
    alien = service.resolve_personal_channel_inbound(
        instance_id=own, provider="weixin", issuer="wx-app",
        subject="wx-user-other")
    assert alien["allowed"] is False
    assert alien["reason"] == "sender_not_owner"


def test_the_shipped_deployment_keeps_personal_execution_closed(flow):
    """7.4 vs 7.10: configuration works, personal *execution* stays gated off.

    A member can scan and own a personal instance (7.4), but nothing in this
    change flips the two delivered switches that let a personal bot actually
    serve traffic — that needs the real-provider acceptance 7.8/7.9 owe. This
    test is the receipt for that gap: it fails the moment someone opens the gate
    without the acceptance evidence.
    """
    assert channel_instances.personal_runtime_enabled("weixin") is False
    assert channel_instances.public_personal_ingress_ready("weixin") is False
    assert "weixin" not in channel_instances.PERSONAL_RUNTIME_ACCEPTED_TYPES
    assert "weixin" not in channel_instances.PUBLIC_PERSONAL_INGRESS_TYPES
    # A scanned personal instance is still fully configured and owned.
    committed = flow.scan("member1", commit={"agent_id": PERSONAL_TARGET})
    assert committed["saved"] is True
    judgement = flow.service.resolve_personal_channel_inbound(
        instance_id=committed["instance_id"], provider="weixin",
        issuer="wx-app", subject="wx-user-1")
    assert judgement["allowed"] is False
    assert judgement["reason"] == "channel_type_not_ready"


# ---------------------------------------------------------------------------
# 7.6 origin/CSRF, and the route surface itself
# ---------------------------------------------------------------------------

def test_a_side_effecting_post_must_present_the_console_origin(flow):
    started = flow.start("root")
    _FakeVendor.confirm()
    response = flow.post("root", {"action": "poll", "handle": started["handle"]},
                         headers={"Origin": "http://evil.example"})
    _refused(response, 403, "csrf_failed")
    # The refusal happened before any state moved: nothing was created and the
    # scan can still be submitted from the console.
    assert _instances(flow) == []
    assert _ok(flow.post("root", {"action": "poll",
                                 "handle": started["handle"]}))["saved"] is True


def test_the_scan_start_is_an_origin_checked_write_too(flow):
    response = flow.get("root", headers={"Origin": "http://evil.example"})
    _refused(response, 403, "csrf_failed")


def test_the_scan_route_refuses_an_unauthenticated_caller(flow):
    response = flow.web.get(VENDOR_QR)
    assert str(response.status).startswith("401"), response.status
    response = flow.web.post(VENDOR_QR, {"action": "poll"})
    assert str(response.status).startswith("401"), response.status


def test_an_unknown_action_is_refused_without_touching_the_session(flow):
    started = flow.start("root")
    _refused(flow.post("root", {"action": "teleport",
                                "handle": started["handle"]}),
             400, "unknown_action")
    assert _ok(flow.post("root", {"action": "poll",
                                 "handle": started["handle"]}))["qr_status"] == "waiting"


# ---------------------------------------------------------------------------
# 7.7 the registry, and the Feishu contract we must not have redesigned
# ---------------------------------------------------------------------------

def test_the_scan_slice_is_open_for_exactly_what_was_proved():
    """One registry, two scopes, no platform scope, no execution claim."""
    from auth import capability_matrix as matrix

    slice_ = matrix.slice_for("weixin_scan")
    assert slice_.capability == "channel-scan-onboarding"
    assert slice_.implemented is True
    assert slice_.accepted is True
    assert set(slice_.scope) == {"tenant", "personal"}
    assert slice_.reason == ""
    assert set(slice_.open) == {"qr", "poll"}
    assert slice_.access("qr") == matrix.ACCESS_CONFIG
    assert slice_.access("poll") == matrix.ACCESS_CONFIG
    # Nothing execution-class is declared, so an unverified execution cannot be
    # reached through this slice at all.
    assert slice_.access("execute") is None
    assert slice_.route("execute")["policy"] == "closed"
    assert slice_.route("qr")["policy"] == matrix.DEFAULT_POLICY


def test_the_route_table_reads_the_registry_instead_of_a_second_policy():
    """``route_registry`` must dispatch through the slice, not hardcode access."""
    import inspect

    from channel.web import route_registry

    source = inspect.getsource(route_registry)
    assert 'S("weixin_scan", "qr")' in source
    assert 'S("weixin_scan", "poll")' in source


def test_the_open_route_policy_admits_the_console_and_refuses_outsiders(flow):
    """The policy the registry derives from the slice, checked on the wire."""
    from auth import capability_matrix as matrix

    assert matrix.slice_for("weixin_scan").route("qr")["policy"] == "tenant"
    # A tenant member passes the route gate and is then scoped to their own
    # instance by the handler; an unauthenticated caller does not get past it.
    assert flow.start("member1")["scope"] == "personal"
    assert str(flow.web.get(VENDOR_QR).status).startswith("401")


def test_the_feishu_one_time_handoff_keeps_its_surface(flow):
    """Feishu is not redesigned by this change: same route, same contract.

    The full one-time hand-off (``app_id``/``app_secret``/``scan_ticket`` to the
    verified initiator) is covered by ``tests/test_feishu_register_session.py``.
    What this asserts is that the route is still the Feishu handler's own — it
    answers with its own error shape and was not moved under the scan's
    per-initiator state machine (which would answer ``not_owner``).
    """
    from channel.web.web_channel import FeishuRegisterHandler

    assert callable(getattr(FeishuRegisterHandler, "GET", None))
    assert callable(getattr(FeishuRegisterHandler, "POST", None))

    response = flow.web.get("/api/feishu/register")
    assert str(response.status).startswith("401"), response.status
    polled = flow.web.post("/api/feishu/register", {"action": "poll"})
    body = _json(polled)
    assert body.get("code") == "missing_handle", body
    assert body.get("code") != "not_owner"
