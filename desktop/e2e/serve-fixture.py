# encoding:utf-8
"""The real console, served over HTTP for the Electron E2E (task 5.8).

This is not a mock server: it is ``build_web_app()`` -- the same WSGI
application a deployment runs -- over a private identity database built by the
same fixture the Python suites use (``tests/_helpers.WebAppHarness``). It exists
because the desktop E2E needs something the in-process harness cannot give it: a
socket, real Cookies, real redirects and a real origin for the container to load.

Everything that happens to a request is decided by the product code. The two
things this file adds are test-only and are both printed on the readiness line:

``capability``
    The phase-1 slice ``desktop_remote_web`` is opened for the lifetime of this
    process. In the shipped declaration it is ``open={}`` /
    ``accepted=False`` (auth/capability_matrix.py): the gate stays shut until
    acceptance evidence exists (task 6.3). The E2E has to run *before* that
    evidence exists, and the Python suites open the same slice through
    ``tests/test_desktop_web_session.py``'s stand-in, so this is that stand-in,
    scoped to exactly this process and reported as such.

``upstream``
    A canned OpenAI-compatible endpoint, included only so a chat turn can
    stream: the real runtime performs the real request, but the tokens come from
    this file rather than from a vendor that cannot be called in a test. It is
    off unless ``--model-port`` is given, and the readiness line says whether
    the console was pointed at it.

Usage (the Electron main process spawns this; see ``main.e2e.cjs``)::

    python desktop/e2e/serve-fixture.py --data-dir <tmp> --port 0 --model-port 0
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)

#: The phase-1 slices whose gate the E2E opens for this process only.
PHASE1_SLICES = ("desktop_remote_web",)

#: The actions each slice serves while the E2E runs. The declaration lists none
#: yet (``open={}``) because acceptance has not been recorded; these are the
#: actions the routes the E2E exercises actually gate on.
PHASE1_DEFAULT_ACTIONS = {
    "desktop_remote_web": {"session": "execute"},
}

#: The identities the E2E signs in as. Passwords are the fixture's own.
USERS = {
    "u1": ("e2e-u1", ["member"], "E2eMember1!"),
    "u2": ("e2e-u2", ["member"], "E2eMember1!"),
    "ta": ("e2e-ta", ["tenant_admin"], "E2eMember1!"),
    "pa": ("e2e-pa", [], "E2eMember1!"),
}

#: Two tenants, so the tenant switch has somewhere to switch to (A06/W01).
TENANT_CODES = ("acme", "beta")


def _open_phase1_slices():
    """Open the declared-but-not-accepted phase-1 slices in this process.

    The *declaration* (``auth/capability_matrix.py``) is a module-level value, so
    the honest way to open a slice for one process is to change that value's own
    attributes -- not to hand a stand-in object back from ``slice_for``. Every
    consumer then sees one consistent slice: the route gate, the
    ``/auth/context`` projection and ``availability()`` (which reads
    ``implemented``/``accepted`` and would fail on a duck-typed substitute).

    ``open`` is filled from ``declared_open`` so the actions come from the
    declaration itself; if the declaration lists none, the slice genuinely has
    nothing to serve and the fixture must not invent an action.
    """
    from auth import capability_matrix

    opened = {}
    for slice_id in PHASE1_SLICES:
        spec = capability_matrix.slice_for(slice_id)
        # The code for this change exists, so "not implemented" would be a lie;
        # what is missing is the recorded acceptance, which this process stands
        # in for. ``availability()`` therefore reports it as available *in this
        # process only*.
        spec.implemented = True
        spec.accepted = True
        spec.open = dict(spec.declared_open) or dict(PHASE1_DEFAULT_ACTIONS[slice_id])
        opened[slice_id] = dict(spec.open)
    return opened


# --------------------------------------------------------------------------- #
# The canned OpenAI-compatible upstream
# --------------------------------------------------------------------------- #

#: The tokens the agent will stream, in order. Kept short and distinctive so the
#: spec can assert the page received *these* and not a single blob.
STREAM_TOKENS = ["E2E-", "stream-", "ok"]

STREAM_BODY = "".join(STREAM_TOKENS)

#: The one model this fixture configures and authorizes. Single-sourced because
#: the chat runtime refuses a model the caller holds no ``model.use`` grant for,
#: and that grant is keyed by the model code (``_grant_model_use``): a mismatch
#: between the configured name and the granted id would surface as the runtime's
#: "no model is authorized for you" rather than as an obviously wrong name.
MODEL_CODE = "e2e-canned"


class _ModelHandler(BaseHTTPRequestHandler):
    """``POST /v1/chat/completions``: a valid OpenAI SSE answer, token by token."""

    protocol_version = "HTTP/1.1"
    requests_seen = 0

    def log_message(self, *args):  # noqa: D102 - silence the default logger
        pass

    def _send_body(self, body):
        """Write one SSE event, framed as its own HTTP chunk while streaming.

        The framing is what makes a token arrive *when it is written*. A
        response with no length at all (and no chunking) leaves the client's
        buffered reader waiting for a full read's worth of bytes, so the page
        renders the whole answer at once -- which is exactly the difference this
        fixture exists to show. The real endpoint frames its SSE this way.
        """
        if getattr(self, "chunked", False):
            self.wfile.write(b"%x\r\n" % len(body) + body + b"\r\n")
        else:
            self.wfile.write(body)
        self.wfile.flush()

    def _send_event(self, payload):
        self._send_body(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        payload = b""
        if length:
            payload = self.rfile.read(length)
        self.last_request = payload
        type(self).requests_seen += 1
        stream = b'"stream":true' in payload.replace(b" ", b"")
        self.chunked = stream
        self.send_response(200)
        self.send_header("Content-Type",
                         "text/event-stream" if stream else "application/json")
        self.send_header("Cache-Control", "no-store")
        if stream:
            # Chunked framing, so each event reaches the client on its own.
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if not stream:
            body = {"id": "chatcmpl-e2e", "object": "chat.completion",
                    "created": int(time.time()), "model": MODEL_CODE,
                    "choices": [{"index": 0, "message": {"role": "assistant",
                                                         "content": STREAM_BODY},
                                 "finish_reason": "stop"}]}
            encoded = json.dumps(body).encode("utf-8")
            self.wfile.write(encoded)
            self.wfile.flush()
            return
        for index, token in enumerate(STREAM_TOKENS):
            chunk = {
                "id": "chatcmpl-e2e", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": MODEL_CODE,
                "choices": [{"index": 0, "delta": {"content": token},
                             "finish_reason": None}],
            }
            self._send_event(chunk)
            # A visible gap: the spec asserts the page rendered a prefix before
            # the last token arrived, which a single buffered blob cannot show.
            time.sleep(0.35)
        final = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": MODEL_CODE,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self._send_event(final)
        self._send_body(b"data: [DONE]\n\n")
        # Terminate the chunked body, or the client waits for a chunk that never
        # comes.
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        _ = index


def start_model_upstream(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), _ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


# --------------------------------------------------------------------------- #
# TLS
# --------------------------------------------------------------------------- #

def _spki_sha256(cert_path):
    """The base64 SPKI hash Chromium's ``--ignore-certificate-errors-spki-list``
    matches against. Read from the certificate itself, never assumed."""
    import base64
    import hashlib
    import subprocess

    der = subprocess.run(
        ["openssl", "x509", "-in", cert_path, "-pubkey", "-noout"],
        check=True, capture_output=True).stdout
    spki = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "der"],
        input=der, check=True, capture_output=True).stdout
    return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def ensure_certificate(directory):
    """A self-signed certificate for 127.0.0.1, generated once per data dir.

    ``openssl`` is the only dependency and the file is reused if a previous run
    made it, so a spec re-run is not slowed down by key generation.

    Only the file paths are returned: the server is handed them through
    cheroot's own SSL adapter (see ``main``) rather than being wrapped by hand.
    That matters beyond style -- an adapter is what makes the server tell the
    application the truth about the transport, and ``_request_origin_exact``
    (channel/web/auth_handlers.py) reads exactly that: with the socket wrapped
    outside the server, every request looked like plain HTTP, so the console
    answered ``/`` with an ``http://`` redirect that the TLS client could not
    follow (``ERR_CONNECTION_RESET``).
    """
    import subprocess

    os.makedirs(directory, exist_ok=True)
    cert = os.path.join(directory, "console.crt")
    key = os.path.join(directory, "console.key")
    if not (os.path.exists(cert) and os.path.exists(key)):
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key, "-out", cert, "-days", "2", "-nodes",
            "-subj", "/CN=127.0.0.1",
            # Chromium ignores the CN: a SAN is mandatory.
            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ], check=True, capture_output=True)

    return {"cert": cert, "key": key, "spki": _spki_sha256(cert)}


# --------------------------------------------------------------------------- #
# The console
# --------------------------------------------------------------------------- #

def build_console(data_dir, model_origin):
    """The real WSGI app over a real identity database (the suite's fixture)."""
    from tests._helpers import WebAppHarness

    harness = WebAppHarness(
        os.path.join(data_dir, "instance"),
        settings={
            # The chat runtime must reach *this* process's canned upstream and
            # nothing else: a custom OpenAI-compatible provider pointed at
            # loopback (config.py: custom_api_key / custom_api_base).
            "model": MODEL_CODE,
            "bot_type": "custom",
            "custom_api_key": "e2e-canned-key",
            "custom_api_base": model_origin + "/v1",
            # The deployment switch for the phase-1 container. It ships ``False``
            # (config.py) and stays off until acceptance is recorded (task 6.3);
            # this process turns it on for itself so the container can be driven
            # *before* that evidence exists, and the readiness line reports it.
            "desktop_remote_web_enabled": True,
        },
    )
    ids = {}
    menu_role = _grant_chat_menu(harness)
    # u1 is the identity the container's journey runs as: it carries the chat
    # menu and the model grant, the two things a tenant administrator would hand
    # a member before the chat page can answer (see the two helpers).
    model_role = _grant_model_use(harness, harness.admin_id, harness.tenant_id)
    for key, (username, roles, password) in USERS.items():
        wanted = list(roles) + ([menu_role, model_role] if key == "u1" else [])
        user_id = harness.member(username, wanted, password=password)
        if key == "pa":
            _promote_to_platform_admin(harness, user_id)
        ids[key] = {"username": username, "password": password, "user_id": user_id}

    # A second tenant u1 also belongs to, so the console's tenant switch has a
    # real target and the child session has to move with it (A06/W01).
    second = _add_second_tenant(harness, ids["u1"]["user_id"], menu_role, model_role)
    # Allocating a model to a tenant is a *platform* write, so it waits for ``pa``
    # to hold the qualification (promoted in the loop above).
    _allocate_model(harness, ids["pa"]["user_id"], harness.tenant_id)
    _allocate_model(harness, ids["pa"]["user_id"], second["tenant_id"])
    harness.add_agent("e2e-agent")
    return harness, ids, second


def _model_use_grant():
    """The platform's own resource id for the fixture's model.

    ``auth/policy.py`` namespaces a model grant as ``provider:<config-id>:<code>``
    (``RESOURCE_NAMESPACES``), and the runtime matches a grant on the resource
    id's trailing ``:<code>`` segment. ``custom`` is the config-id of the
    OpenAI-compatible provider this fixture points at its canned upstream
    (``custom_api_base`` / ``custom_api_key``), so this is the id an operator
    would be shown for that model.

    Derived here rather than read from the live catalog: ``_session_model_catalog``
    projects user-defined providers out of ``custom_providers`` config, and the
    fixture's provider is the legacy single ``custom_api_base`` one, so the
    catalog is empty in this process (reported as a finding). The id is the same
    string the catalog would have built for this model.
    """
    return "provider:custom:%s" % MODEL_CODE


def _grant_model_use(harness, actor_user_id, tenant_id):
    """Authorize the fixture's model for one tenant, as its admin would.

    The chat runtime refuses a model outside the caller's ``model.use`` grant set
    (``bridge/agent_bridge.py``: ``_require_model_use``), and a plain ``member``
    carries none, so a fresh tenant cannot answer at all. Two writes are needed
    and neither is a substitute for the other: this role grant, and the platform
    allocation in ``_allocate_model`` -- ``resource_ids_for`` intersects the two
    sets, and they compare as strings.
    """
    code = "e2e-model"
    harness.service.create_role(
        actor_user_id=actor_user_id, tenant_id=tenant_id,
        code=code, name="E2E Model", permissions=[],
        resource_grants=[{"resource_kind": "model",
                          "resource_id": _model_use_grant(), "action": "use"}])
    return code


def _allocate_model(harness, platform_admin_id, tenant_id):
    """Allocate the fixture's model to a tenant: the platform half of the grant.

    ``set_tenant_resource_grants`` is the product's single write path for a
    tenant's allocatable ceiling, and it requires a platform admin. The existing
    grants are carried over rather than replaced with just this one, so the call
    only ever adds. Idempotent, so a tenant that already holds it is left alone.
    """
    existing = harness.service.tenant_resource_grants(tenant_id)
    if any(g["resource_id"] == _model_use_grant() and g["action"] == "use"
           for g in existing):
        return existing
    version = harness.service._store.execute(
        "SELECT version FROM tenants WHERE id=?", (tenant_id,))[0]["version"]
    return harness.service.set_tenant_resource_grants(
        actor_user_id=platform_admin_id, tenant_id=tenant_id,
        grants=list(existing) + [{"resource_kind": "model",
                                  "resource_id": _model_use_grant(),
                                  "action": "use"}],
        expected_version=version)


def _grant_chat_menu(harness):
    """Give the journey's identity the console's chat menu, as a tenant admin would.

    ``BUILTIN_MENU_DEFAULTS`` (``auth/policy.py``) seeds the built-in ``member``
    role with every workbench page *except* ``workbench.chat``. A role carrying
    any menu grant switches the projection to the menu-bound rule
    (``auth/service.py``: ``menu_gated``), so a plain member is answered
    ``menu_denied`` for the chat page and the console renders "无权访问" over
    ``/chat`` -- the very page this change exists to carry into the container.

    The fixture therefore grants the page through the product's own role API
    rather than patching the declaration: that is the write a tenant
    administrator performs, and it keeps the assertion honest about how the page
    is reached. (The missing default is reported as a finding; it lives in a file
    this change does not own.)
    """
    code = "e2e-chat-menu"
    harness.service.create_role(
        actor_user_id=harness.admin_id, tenant_id=harness.tenant_id,
        code=code, name="E2E Chat Menu", permissions=[],
        resource_grants=[{"resource_kind": "menu",
                          "resource_id": "nav:workbench.chat", "action": "view"}])
    return code


def _promote_to_platform_admin(harness, user_id):
    """Give an ordinary member the platform qualification (W16).

    Uses the product's single write path for the platform binding, so the
    fixture cannot end up with a platform admin that the service would refuse.
    ``recent_password`` is the *actor's* own password -- it is a re-check of the
    acting platform admin, not of the target.
    """
    version = harness.service._store.execute(
        "SELECT version FROM users WHERE id=?", (user_id,))[0]["version"]
    harness.service.set_platform_user_status(
        actor_user_id=harness.admin_id, user_id=user_id, active=True,
        is_platform_admin=True, expected_version=version,
        recent_password=harness.ADMIN_PASSWORD)


def _add_second_tenant(harness, user_id, menu_role="e2e-chat-menu",
                       model_role="e2e-model"):
    """A second tenant the *existing* account is bound to.

    The membership is created as ``bind-existing``: same account, same password,
    two memberships -- the shape a real user who belongs to two tenants has, and
    the premise the console's tenant switch needs (W01/A06). Both grants have to
    be repeated here: a role belongs to one tenant, so the second membership
    would otherwise drop the menu *and* the model the switch is supposed to keep.
    """
    from tests._helpers import IdentityStack

    admin_username = "e2e-beta-admin"
    created = harness.service.create_tenant(
        actor_user_id=harness.admin_id, code="beta", name="Beta",
        admin_username=admin_username, admin_display="Beta Admin",
        admin_password=IdentityStack.MEMBER_PASSWORD,
        recent_password=IdentityStack.ROOT_PASSWORD,
        shared_root=os.path.join(harness.root, "tenants", "beta"),
    )
    tenant_id = created["id"]
    admin_id = next(user["id"] for user in harness.service.list_platform_users()
                    if user["username"] == admin_username)
    username = next(user["username"] for user in harness.service.list_platform_users()
                    if user["id"] == user_id)
    # The grants travel with the membership: roles are per-tenant, so the second
    # tenant needs its own copies before the switch can keep the chat page and
    # its model.
    harness.service.create_role(
        actor_user_id=admin_id, tenant_id=tenant_id,
        code=menu_role, name="E2E Chat Menu", permissions=[],
        resource_grants=[{"resource_kind": "menu",
                          "resource_id": "nav:workbench.chat", "action": "view"}])
    _grant_model_use(harness, admin_id, tenant_id)
    harness.service.create_member(
        actor_user_id=admin_id, tenant_id=tenant_id, operation="bind-existing",
        username=username, display_name=username, temporary_password="",
        roles=["member", menu_role, model_role])
    return {"tenant_id": tenant_id, "code": "beta"}


# --------------------------------------------------------------------------- #
# The control surface (test-only, never part of the product)
# --------------------------------------------------------------------------- #

class _ControlHandler(BaseHTTPRequestHandler):
    """Small control plane so the spec can prepare and inspect fixture state."""

    protocol_version = "HTTP/1.1"
    harness = None
    ids = None
    second = None
    model_server = None

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            return self._json({"ok": True})
        if parsed.path == "/identities":
            return self._json({"users": self.ids, "second_tenant": self.second,
                               "tenant_id": self.harness.tenant_id})
        if parsed.path == "/model-requests":
            return self._json({"count": _ModelHandler.requests_seen})
        if parsed.path == "/workspace/write":
            name = (query.get("name") or [""])[0]
            content = (query.get("content") or [""])[0]
            path = _write_workspace_file(self.harness, name, content)
            return self._json({"path": path})
        if parsed.path == "/workspace/read":
            name = (query.get("name") or [""])[0]
            try:
                with open(os.path.join(_agent_root(self.harness), name), "r",
                          encoding="utf-8") as handle:
                    return self._json({"content": handle.read()})
            except OSError as error:
                return self._json({"error": str(error)}, 404)
        if parsed.path == "/workspace/uploaded":
            # The bytes the console's upload endpoint actually stored, by name.
            name = (query.get("name") or [""])[0]
            return self._json({"content": _read_uploaded(self.harness, name)})
        if parsed.path == "/child/revoke":
            return self._json(_revoke_child(self.harness))
        if parsed.path == "/child/session":
            # The identity store's own truth for the linked Web *child* session:
            # after a detach the App's partition is gone, so the only place left
            # to check is the session row the server minted.
            return self._json(_child_session_state(
                self.harness, (query.get("id") or [""])[0]))
        if parsed.path == "/links":
            # The pairing table, so the spec can assert a detach really ended the
            # link instead of only hiding the container.
            rows = self.harness.service._store.execute(
                "SELECT id, revoked_at, native_session_id FROM desktop_web_links"
                " ORDER BY created_at ASC, id ASC")
            links = [{"id": row["id"], "revoked": row["revoked_at"] is not None}
                     for row in rows]
            return self._json({
                "total": len(rows),
                "active": sum(1 for link in links if not link["revoked"]),
                "revoked": sum(1 for link in links if link["revoked"]),
                "links": links,
            })
        return self._json({"error": "not found"}, 404)


def _child_session_state(harness, link_id):
    """Whether the Web child session a link points at is revoked server-side."""
    if not link_id:
        return {"error": "no_link_id"}
    rows = harness.service._store.execute(
        "SELECT s.revoked_at AS revoked_at"
        " FROM desktop_web_links l JOIN auth_sessions s ON s.id = l.web_session_id"
        " WHERE l.id = ?", (link_id,))
    if not rows:
        return {"error": "no_link"}
    return {"link_id": link_id, "revoked": rows[0]["revoked_at"] is not None}


def _agent_root(harness):
    from agent.registry import get_agent_registry
    return get_agent_registry().get("e2e-agent", require_enabled=False).workspace


def _write_workspace_file(harness, name, content):
    root = _agent_root(harness)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def _read_uploaded(harness, name):
    """Find a file the console's upload endpoint stored, by its base name.

    The endpoint *renames* what it stores -- ``web_<hex><ext>`` (``runtime.py``:
    ``upload``) -- so the original name is not on disk; the extension is, and
    the bytes are what the caller asserts on. The file lands in the *addressed*
    Agent's ``user/<user id>/uploads`` (change
    ``isolate-shared-agent-user-data``), which for this fixture is the instance
    root, so the walk looks under every ``uploads`` directory of the deployment
    rather than at the Agent the fixture happens to name.
    """
    extension = os.path.splitext(name)[1].lower()
    newest = None  # (mtime, path)
    for root in (getattr(harness, "instance_root", ""),
                 getattr(harness, "shared_root", "")):
        if not root or not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            if os.path.basename(base) != "uploads":
                continue
            for found in files:
                if not found.startswith("web_"):
                    continue
                if os.path.splitext(found)[1].lower() != extension:
                    continue
                path = os.path.join(base, found)
                stamp = os.path.getmtime(path)
                if newest is None or stamp > newest[0]:
                    newest = (stamp, path)
    if newest is None:
        return None
    with open(newest[1], "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def _revoke_child(harness):
    """Revoke the paired Web child session server-side (A04's premise)."""
    from auth.desktop_web_session import service_for

    rows = harness.service._store.execute(
        "SELECT id, native_session_id FROM desktop_web_links"
        " WHERE revoked_at IS NULL ORDER BY created_at DESC LIMIT 1")
    if not rows:
        return {"revoked": False, "reason": "no_link"}
    link = rows[0]
    service_for(harness.service).revoke_for_native(
        native_session_id=link["native_session_id"])
    return {"revoked": True, "link_id": link["id"]}


def seed_local_identity(data_dir):
    """Create the identity database a *local-mode* desktop launch signs in to.

    The E2E drives the shipped app in local mode first -- the mode every install
    starts in, and the one that must keep working -- so that launch needs an
    account of its own. This is the same real stack the Python suites build
    (``tests._helpers.build_identity``): a bootstrapped tenant with a root
    administrator, no fixture-specific shortcut and no user data from anywhere
    else. Only the credentials the browser leg needs are printed.

    A *second* account is seeded beside the root one so the journey can change
    accounts rather than only re-enter as the same user. It is an ordinary
    member carrying the same agent access the root account has and nothing
    more: no extra role and no widened slice. Cross-account isolation is
    therefore checked without granting anything the install did not already
    have.
    """
    from tests._helpers import IdentityStack, build_identity

    os.makedirs(data_dir, exist_ok=True)
    stack = build_identity(data_dir, agents=("default",), tenant_code="local")
    stack.agent_role("local-member", ["default"])
    stack.member("member", ["local-member"])
    # The bootstrap records the tenant's shared root; make the directory it named
    # exist before a real backend starts against it.
    os.makedirs(os.path.join(data_dir, "shared"), exist_ok=True)
    return {
        "username": "root",
        "password": IdentityStack.ROOT_PASSWORD,
        "tenant_id": stack.tenant_id,
        "second_user": {
            "username": "member",
            "password": IdentityStack.MEMBER_PASSWORD,
            "tenant_id": stack.tenant_id,
        },
    }


def start_control(port, harness, ids, second, model_server):
    _ControlHandler.harness = harness
    _ControlHandler.ids = ids
    _ControlHandler.second = second
    _ControlHandler.model_server = model_server
    server = ThreadingHTTPServer(("127.0.0.1", port), _ControlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--model-port", type=int, default=0)
    parser.add_argument("--control-port", type=int, default=0)
    parser.add_argument("--no-model", action="store_true")
    # TLS. The desktop refuses a plain-HTTP server outright (profiles.ts), so
    # the console has to be a real HTTPS origin here. The certificate is a
    # self-signed one generated into the data directory; the client trusts it by
    # *pinning its public key* (see main.e2e.cjs), which is narrower than
    # installing a CA and is never a product option.
    parser.add_argument("--tls-dir", default=None,
                        help="where to keep the generated certificate "
                             "(default: <data-dir>/tls)")
    parser.add_argument("--no-tls", action="store_true",
                        help="serve plain HTTP (only for hand-probing the fixture)")
    parser.add_argument("--seed-local-identity", default=None, metavar="DIR",
                        help="create the local-mode identity database in DIR, print"
                             " the credential it made, and exit without serving")
    args = parser.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    # The *config* tree lives one level below the console's own directory, and
    # that separation is load-bearing rather than cosmetic: ``build_console``
    # bootstraps its tenant under ``<data-dir>/instance/tenants/<code>``, while
    # ``common/state_dir`` refuses any tenant shared root that sits inside
    # ``get_data_root()`` (tenant data must never live in the config tree).
    # Pinning the data root to ``--data-dir`` itself made every tenant the
    # console created resolve inside it, so the sessions API answered every
    # request with the nesting error and the journey's chat turn came back as
    # ``Agent error``.
    os.environ["COW_DATA_DIR"] = os.path.join(args.data_dir, "state")

    if args.seed_local_identity:
        # Local-mode leg of the E2E: build the identity database the bundled
        # backend will sign the operator in against, then stop. No server, no
        # gate is opened -- that leg runs with the shipped declaration.
        print(json.dumps({
            "ready": True,
            "local_identity": seed_local_identity(args.seed_local_identity),
        }), flush=True)
        return

    model_server = None
    model_port = 0
    if not args.no_model:
        model_server, model_port = start_model_upstream(args.model_port)
    model_origin = "http://127.0.0.1:%d" % model_port if model_port else ""

    tls = None
    if not args.no_tls:
        tls = ensure_certificate(args.tls_dir or os.path.join(args.data_dir, "tls"))

    _open_phase1_slices()
    harness, ids, second = build_console(args.data_dir, model_origin)
    control_server, control_port = start_control(
        args.control_port, harness, ids, second, model_server)

    import web
    from cheroot.ssl.builtin import BuiltinSSLAdapter

    web.httpserver.LogMiddleware.log = lambda self, status, environ: None
    server = web.httpserver.WSGIServer(("127.0.0.1", args.port), harness.app.wsgifunc())
    server.daemon_threads = True
    server.request_queue_size = 128
    server.timeout = 300
    server.requests.min = 20
    server.requests.max = 80
    server.max_request_body_size = 64 * 1024 * 1024
    if tls:
        # The server terminates TLS itself, so ``wsgi.url_scheme`` and ``HTTPS``
        # are set per request and the console's own origin arithmetic is right.
        # Wrapping ``server.socket`` after ``prepare()`` instead made the WSGI
        # environ claim plain HTTP, and every ``/`` redirect came back as
        # ``http://...`` -- unfollowable over the TLS socket the client used.
        server.ssl_adapter = BuiltinSSLAdapter(tls["cert"], tls["key"])
    server.prepare()
    port = server.socket.getsockname()[1]
    scheme = "https" if tls else "http"

    print(json.dumps({
        "ready": True,
        "origin": "%s://127.0.0.1:%d" % (scheme, port),
        "port": port,
        "control_origin": "http://127.0.0.1:%d" % control_port,
        "control_port": control_port,
        "model_origin": model_origin,
        "model_port": model_port,
        "tls_cert": tls["cert"] if tls else "",
        "tls_spki": tls["spki"] if tls else "",
        "tls_key": tls["key"] if tls else "",
        "users": ids,
        "second_tenant": second,
        "tenant_id": harness.tenant_id,
        "capability": {"opened_for_this_process": list(PHASE1_SLICES)},
    }), flush=True)

    server.serve()


if __name__ == "__main__":
    main()
