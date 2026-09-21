# encoding:utf-8
"""Small shared helpers for web.py route tests.

Keeps cookie extraction in one place: since Web login returns no token in the
JSON body (only the HttpOnly session Cookie), tests that need a session token to
send subsequent requests read it from the ``Set-Cookie`` response header.
"""

import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


def web_layer_source(include_upstream: bool = False) -> str:
    """Source of the fork's console web layer, as one string.

    The fork's handler bodies used to sit in ``channel/web/web_channel.py``; the
    web-split change (``openspec/changes/adopt-upstream-web-split``) moved the
    fork's implementation into ``channel/web/fork/`` and left the entry module
    with the URL table and the handler imports.

    Structural assertions about the fork's console — "this logic exists in the
    fork's web layer", "this retired symbol is nowhere in it" — must therefore
    read the layer, not one file. Scoping them to the entry module would leave
    them passing for the wrong reason: a resurrected helper inside
    ``channel/web/fork/`` would go unnoticed, which is exactly what the
    no-resurrection guardrails exist to catch.

    ``channel/web/api/**`` and ``channel/web/core/**`` are **excluded** by
    default because they are not the fork's code: the same change adopts them
    verbatim from upstream, which keeps its own handlers under the same names
    (64 collide with the fork's) and its own password login. A fork guardrail
    that read them would fail on upstream's legitimate code — "the fork retired
    shared-password login" is true of the fork's layer and false of upstream's.
    Pass ``include_upstream=True`` for the whole layer when that is what the
    assertion actually means.

    Order is deterministic (entry module first, then the rest by path), so
    assertions that slice a single function body out of the text stay stable.
    """
    web = Path(__file__).resolve().parents[1] / "channel" / "web"
    entry = web / "web_channel.py"
    upstream = ("api", "core")
    parts = [entry]
    for path in sorted(web.rglob("*.py")):
        if path == entry:
            continue
        rel = path.relative_to(web)
        if not include_upstream and rel.parts and rel.parts[0] in upstream:
            continue
        parts.append(path)
    return "\n\n".join(p.read_text(encoding="utf-8") for p in parts)


def upstream_web_layer_source() -> str:
    """Source of the upstream modules the change adopts (``api/``, ``core/``)."""
    web = Path(__file__).resolve().parents[1] / "channel" / "web"
    return "\n\n".join(p.read_text(encoding="utf-8")
                       for p in sorted(web.rglob("*.py"))
                       if p.relative_to(web).parts
                       and p.relative_to(web).parts[0] in ("api", "core"))


def fork_web_layer_files():
    """Every fork-owned file of the console web layer, the entry module first.

    The same set :func:`web_layer_source` concatenates, kept as paths so an
    assertion can parse one file at a time (per-handler structural checks) instead
    of regex-matching one enormous string.
    """
    web = Path(__file__).resolve().parents[1] / "channel" / "web"
    entry = web / "web_channel.py"
    files = [entry]
    for path in sorted(web.rglob("*.py")):
        if path == entry:
            continue
        rel = path.relative_to(web)
        if rel.parts and rel.parts[0] in ("api", "core"):
            continue
        files.append(path)
    return files


def cookie_value(response, name):
    """Return the value of the cookie ``name`` from a response's Set-Cookie.

    ``response`` is a ``web.storage`` as returned by ``app.request(...)``; it
    exposes ``header_items`` (list of ``(name, value)``). Returns "" when absent.
    """
    items = getattr(response, "header_items", None) or []
    for header_name, value in items:
        if header_name != "Set-Cookie":
            continue
        for part in value.split("; "):
            if part.startswith(name + "="):
                return part[len(name) + 1:]
    return ""


def has_cookie(response, name):
    """True when the response sets a cookie named ``name``."""
    return bool(cookie_value(response, name))


class IdentityStack:
    """A real identity database with the actors a scheduler test needs.

    Built from the same service calls the CLI makes rather than by hand-inserting
    rows: the scheduler's authorization reads memberships, roles, resource grants
    and bindings through the service, so a fixture that poked rows directly would
    let a broken query pass. See ``tests/test_scheduler_identity_revalidation.py``
    for the usage pattern this generalises.
    """

    ROOT_PASSWORD = "Str0ngAdminPass"
    MEMBER_PASSWORD = "MemberPass123!"
    TEMP_PASSWORD = "TempPass123!"

    def __init__(self, service, tenant_id, root_user_id, shared_root):
        self.service = service
        self.tenant_id = tenant_id
        self.root = root_user_id
        self.shared_root = shared_root
        self.members = {}

    # -- population --------------------------------------------------------

    def agent_role(self, code, agents, *, permissions=None):
        """A role carrying ``chat.use`` + ``agent.use`` on the listed Agents."""
        permissions = list(permissions or ("chat.use", "agent.use", "agent.read"))
        return self.service.create_role(
            actor_user_id=self.root, tenant_id=self.tenant_id,
            code=code, name=code.title(), permissions=permissions,
            resource_grants=[
                {"resource_kind": "agent", "resource_id": f"agent:{agent}",
                 "action": "use"} for agent in agents
            ],
        )

    def member(self, username, role_codes, *, password=None):
        """Create a member who already completed the first password change."""
        created = self.service.create_member(
            actor_user_id=self.root, tenant_id=self.tenant_id,
            operation="create-new", username=username,
            display_name=username.title(), temporary_password=self.TEMP_PASSWORD,
            roles=list(role_codes),
        )
        token = self.service.login(username, self.TEMP_PASSWORD).token
        self.service.change_password(token, self.TEMP_PASSWORD,
                                     password or self.MEMBER_PASSWORD)
        self.members[username] = created["user_id"]
        return created["user_id"]

    def tenant_admin(self, username="tenant-admin"):
        """A tenant administrator: manages public tasks, not private ones."""
        created = self.service.create_member(
            actor_user_id=self.root, tenant_id=self.tenant_id,
            operation="create-new", username=username,
            display_name=username.title(), temporary_password=self.TEMP_PASSWORD,
            roles=["tenant_admin"],
        )
        token = self.service.login(username, self.TEMP_PASSWORD).token
        self.service.change_password(token, self.TEMP_PASSWORD,
                                     self.MEMBER_PASSWORD)
        self.members[username] = created["user_id"]
        return created["user_id"]

    def other_tenant(self, code="other", *, agent="other-agent",
                     username="foreign"):
        """A second tenant with one ready member, for cross-tenant tests."""
        other = self.service.create_tenant(
            actor_user_id=self.root, code=code, name=code.title(),
            shared_root="", admin_username=f"{code}-root",
            admin_display=f"{code} Root", admin_password=self.ROOT_PASSWORD,
            recent_password=self.ROOT_PASSWORD,
        )["id"]
        self.service.bind_agent(tenant_id=other, agent_id=agent)
        role = self.service.create_role(
            actor_user_id=self.root, tenant_id=other, code=f"{code}-role",
            name=f"{code} role", permissions=["chat.use", "agent.use"],
            resource_grants=[{"resource_kind": "agent",
                              "resource_id": f"agent:{agent}", "action": "use"}],
        )
        user = self.service.create_member(
            actor_user_id=self.root, tenant_id=other, operation="create-new",
            username=username, display_name=username.title(),
            temporary_password=self.TEMP_PASSWORD, roles=[role["code"]],
        )["user_id"]
        token = self.service.login(username, self.TEMP_PASSWORD).token
        self.service.change_password(token, self.TEMP_PASSWORD,
                                     self.MEMBER_PASSWORD)
        self.members[username] = user
        return {"tenant_id": other, "user_id": user, "agent_id": agent}


@contextmanager
def personal_target_roster(*agent_ids):
    """A real Agent Registry over a private workspace, holding *agent_ids*.

    A personal channel instance may only route to a private Agent of its own
    owner, and that target is verified against the **Agent Registry** as well as
    the identity binding. A fixture that creates one therefore has to provide a
    roster; without it the create is refused for a reason unrelated to what the
    test is about, and with a *shared* target it would exercise a shape the
    product must reject.

    Pinned to a fresh temp workspace on purpose: resolving the registry against
    the developer's own workspace would make the outcome depend on their roster.
    """
    from unittest.mock import patch

    from config import Config, conf as _conf

    settings = Config(dict(_conf()))
    settings["agent_workspace"] = tempfile.mkdtemp(prefix="cow-roster-")
    settings["agents"] = [{"id": agent_id, "name": agent_id, "enabled": True}
                          for agent_id in agent_ids]
    settings["default_agent_id"] = agent_ids[0]
    with patch("config.conf", lambda: settings):
        # A registry may already be pinned from an earlier test; unpin it so the
        # roster above is the one every lookup resolves.
        from agent.registry import set_agent_registry

        set_agent_registry(None)
        yield settings


def install_personal_target_roster(case, *agent_ids):
    """``personal_target_roster`` for a ``unittest.TestCase``.

    ``setUp`` cannot use a ``with`` block for something that has to outlive the
    method, so the context is entered here and unwound by ``addCleanup``.

    The pinned settings are left on ``case.roster_settings``: a test that needs
    to *change* the roster (stop an Agent, say) has to edit the same object the
    registry reads, and reading it back off the case is how it does so without
    re-deriving the fixture.
    """
    context = personal_target_roster(*agent_ids)
    case.roster_settings = context.__enter__()
    case.addCleanup(lambda: context.__exit__(None, None, None))
    return context


def personal_channel_target(service, *, tenant_id, user_id, agent_id,
                            origin="user_created"):
    """Bind *agent_id* as *user_id*'s own private Agent and return it.

    The order is the product's: the target has to exist and belong to the member
    before a channel instance may name it. Pair it with
    :func:`personal_target_roster`, which supplies the registry half of the same
    predicate.
    """
    service.bind_agent(tenant_id=tenant_id, agent_id=agent_id,
                       private_owner_user_id=user_id, origin=origin)
    return agent_id


def build_identity(path, *, agents=("agent-a",), tenant_code="acme"):
    """Bootstrap a tenant with an identity database at ``path``.

    Split from :func:`bootstrap_identity` so a ``unittest.TestCase`` (which has
    no ``monkeypatch`` fixture) can build the same real stack and patch
    ``get_identity_service`` with :func:`unittest.mock.patch` itself.
    """
    from auth.service import IdentityService

    root_dir = Path(path)
    shared = root_dir / "shared"
    service = IdentityService(str(root_dir / "identity.db"))
    tenant = service.bootstrap(
        tenant_code=tenant_code, tenant_name=tenant_code.title(),
        admin_username="root", admin_display="Root",
        admin_password=IdentityStack.ROOT_PASSWORD,
        shared_root=str(shared), allow_weak=True,
    )["id"]
    root = service.list_platform_users()[0]["id"]
    for agent in agents:
        service.bind_agent(tenant_id=tenant, agent_id=agent)
    return IdentityStack(service, tenant, root, str(shared))


def bootstrap_identity(tmp_path, monkeypatch, *, agents=("agent-a",),
                       tenant_code="acme"):
    """Bootstrap a tenant with a ready member; return an :class:`IdentityStack`.

    ``get_identity_service`` is patched to this database for the duration of the
    test, which is what makes the ambient-identity tool path transparently use
    it — the same wiring the application has in database identity mode.
    """
    stack = build_identity(tmp_path, agents=agents, tenant_code=tenant_code)
    monkeypatch.setattr("auth.service.get_identity_service", lambda: stack.service)
    return stack


class WebAppHarness:
    """A real ``build_web_app()`` over a private identity database.

    Handler-level tests with a patched ``web`` module prove the handler's own
    logic but say nothing about who is allowed to reach it; once a surface is
    guarded by the route policy and by an owner check, that question can only be
    answered by driving the actual WSGI app. This harness provides the legal
    identity fixture — a bootstrapped tenant, members with roles and grants, and
    a session cookie from a real login — so a test can assert both the happy path
    and the refusals on the wire.

    Usage (pytest)::

        web = WebAppHarness(tmp_path)
        web.add_agent("shared-agent")
        web.member("alice", ["member"], grants=...)
        response = web.post("/api/scheduler/delete", {"task_id": "t1"},
                            token=web.login("alice"))
    """

    BASE = "http://localhost:9899"
    ADMIN_PASSWORD = IdentityStack.ROOT_PASSWORD
    ADMIN_FINAL = "Str0ngRootFinal"
    HOST = "localhost:9899"

    def __init__(self, root, *, tenant_code="acme", settings=None,
                 stack_factory=None):
        from unittest.mock import patch

        from auth.service import IdentityService
        import config as config_module

        self.root = str(root)
        os.makedirs(self.root, exist_ok=True)
        self.db_path = os.path.join(self.root, "identity.db")
        # The deployment layout the console expects: tenants are siblings under
        # ``<instance root>/tenants/<code>``, so a second tenant can be created
        # without its root overlapping the first one's.
        self.shared_root = os.path.join(self.root, "tenants", tenant_code)
        self.service = IdentityService(self.db_path)
        self.stack = (stack_factory or _bootstrap_tenant)(
            self.service, self.shared_root, tenant_code=tenant_code)
        self.tenant_id = self.stack.tenant_id
        self.admin_id = self.stack.root
        self._agents = []
        self._passwords = {}
        self._ids = {}
        self._patchers = []

        token = self.service.login("root", self.ADMIN_PASSWORD).token
        self._passwords["root"] = self.ADMIN_PASSWORD
        self._ids["root"] = self.admin_id

        # Start from the settings the suite already resolved (the session fixture
        # redirects ``agent_workspace`` out of the developer's real workspace) and
        # override only what this tenant owns, so the app is built from a complete
        # configuration rather than the three keys a scheduler test happens to
        # care about.
        base = dict(config_module.conf())
        base.update({
            "identity_mode": "database",
            "identity_db_path": self.db_path,
            "agent_workspace": self.root,
            "tenant_shared_base": os.path.join(self.root, "tenants"),
        })
        base.update(settings or {})
        self._settings = base

        # The scheduler caches one global task store (and service) resolved from
        # whatever root was current when it was first touched. Drop them before
        # building this harness's app, or a store pinned to an earlier test's
        # temp root answers every task query here -- an empty list that looks
        # like a policy failure rather than a stale cache.
        self._reset_scheduler_globals()
        for target in (config_module, __import__("channel.web.web_channel",
                                                 fromlist=["conf"])):
            patcher = patch.object(target, "conf", self._conf)
            patcher.start()
            self._patchers.append(patcher)
        self.app = __import__("channel.web.web_channel",
                              fromlist=["build_web_app"]).build_web_app()

    def _conf(self):
        return self._settings

    def close(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._patchers = []
        # The Agent Registry is a process-global built lazily from ``conf()``, and
        # this harness just replaced ``conf`` with its own root. Unpin it on the
        # way out, or the *next* test's first lookup answers from this harness's
        # roster (and its temp directory), which shows up as an unrelated test
        # failing on "the Agent is not enabled" depending on file order.
        try:
            from agent.registry import set_agent_registry
            set_agent_registry(None)
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        # ``AgentBridge`` is a process-global too, and it caches the registry it
        # was constructed with while the ``Bridge`` singleton outlives one
        # harness's ``conf`` patch. A test that drives an ordinary session
        # through the app builds it, so re-point it at the registry the *real*
        # configuration resolves and drop the instances it created here -- a
        # plain channel-routing test in another file would otherwise route its
        # message through this harness's roster and answer with the wrong
        # Agent. Same leak as ``test_session_context_scope._roster``.
        try:
            from agent.registry import get_agent_registry
            from bridge.bridge import Bridge

            bridge = Bridge().get_agent_bridge()
            with bridge._agents_lock:
                bridge._agent_instances.clear()
            bridge.agent_registry = get_agent_registry()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        self._reset_scheduler_globals()

    @staticmethod
    def _reset_scheduler_globals():
        """Unpin the process-global scheduler store/service (see ``close``)."""
        try:
            from agent.tools.scheduler.integration import reset_scheduler_services
            reset_scheduler_services(stop=False)
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass

    # -- population --------------------------------------------------------

    def write_roster(self, agent_ids, *, default=None):
        """Write the Agent roster (``<instance>/agents/team.json``).

        The registry reads the roster, and its cache is keyed on the file's
        stamped mtime, so writing here is what makes a second Agent visible to
        the app that was already built.

        An entry may be a bare id or a full profile mapping, because a coding
        Agent cannot be described by an id alone: it needs ``agent_type`` and
        ``coding_project_dir`` to be readable at all, and the registry refuses a
        coding Agent without a project rather than guessing one.
        """
        from agent import team

        entries = list(agent_ids)
        first_id = (entries[0] if isinstance(entries[0], str) else entries[0]["id"]) if entries else None
        default = default or first_id
        profiles = []
        for index, entry in enumerate(entries):
            if isinstance(entry, str):
                entry = {"id": entry, "name": entry}
            profile = dict(entry)
            profile.setdefault("name", profile["id"])
            if index != 0 and not profile.get("agent_type"):
                profile.setdefault(
                    "workspace",
                    os.path.join(self.shared_root, "agents", profile["id"]),
                )
            profiles.append(profile)
        path = team.team_file(self._settings)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        path.write_text(json.dumps({
            "agents": profiles, "default_agent_id": default,
        }), encoding="utf-8")
        self._roster = entries
        self._roster_default = default
        return path

    def _roster_ids(self):
        return [e if isinstance(e, str) else e["id"] for e in getattr(self, "_roster", [])]

    def add_coding_agent(self, agent_id, project_dir, *, name=None, tenant_id=None):
        """Put a coding Agent in the roster, enabled and bound to the tenant.

        Coding Agents are ordinary roster entries for every read path, so the
        same binding step applies; what makes them special is that only the Web
        coding entry may run them.
        """
        entries = [e for e in getattr(self, "_roster", []) if not (
            (e if isinstance(e, str) else e["id"]) == agent_id)]
        entries.append({
            "id": agent_id,
            "name": name or agent_id,
            "agent_type": "coding",
            "coding_project_dir": project_dir,
        })
        self.service.bind_agent(
            tenant_id=tenant_id or self.tenant_id, agent_id=agent_id)
        if agent_id not in self._agents:
            self._agents.append(agent_id)
        return self.write_roster(
            entries, default=getattr(self, "_roster_default", None))

    def add_agent(self, *agent_ids, tenant_id=None):
        """Bind Agents to the tenant and put them in the roster.

        ``tenant_id`` defaults to this harness's own tenant; a test that made a
        member in a second tenant names that tenant explicitly, because an Agent
        is bound to exactly one tenant and a first bind to the wrong one is not
        repairable (``bind_agent`` refuses a cross-tenant re-point).
        """
        existing = [e for e in getattr(self, "_roster", [])]
        seen = [e if isinstance(e, str) else e["id"] for e in existing]
        for agent_id in agent_ids:
            if agent_id not in seen:
                existing.append(agent_id)
                seen.append(agent_id)
            self.service.bind_agent(tenant_id=tenant_id or self.tenant_id,
                                    agent_id=agent_id)
            if agent_id not in self._agents:
                self._agents.append(agent_id)
        self.write_roster(existing)
        return list(agent_ids)

    def bind_agent_to_tenant(self, agent_id, tenant_id):
        self.service.bind_agent(tenant_id=tenant_id, agent_id=agent_id)

    def private_agent(self, user_id, agent_id, *, tenant_id=None):
        """Give *user_id* their own private Agent, present in the roster.

        A personal channel instance may only route to a target its own owner
        holds **privately**, and the target is verified against the Agent
        Registry as well as the identity binding — so a fixture that creates one
        has to supply both halves, and this is the one call that supplies them
        together: the Agent is added to the roster (enabled) and bound privately
        to *user_id*. Passing a *shared* Agent here would exercise a shape the
        product must refuse.

        ``tenant_id`` defaults to this harness's own tenant; a test that made a
        member in a second tenant names that tenant explicitly.
        """
        self.add_agent(agent_id, tenant_id=tenant_id)
        self.service.bind_agent(
            tenant_id=tenant_id or self.tenant_id, agent_id=agent_id,
            private_owner_user_id=user_id, origin="user_created")
        return agent_id

    @property
    def agents(self):
        return list(self._agents)

    def role(self, code, permissions, grants=()):
        return self.service.create_role(
            actor_user_id=self.admin_id, tenant_id=self.tenant_id,
            code=code, name=code.title(), permissions=list(permissions),
            resource_grants=[
                {"resource_kind": g[0], "resource_id": g[1], "action": g[2]}
                for g in grants])

    def revoke_grants(self, role):
        """Take the role's resource grants away, keeping its permissions.

        This is the "grant revoked after the task exists" case: the member is
        still a member, so their own tasks remain theirs to pause or delete, but
        they can no longer use the Agent the task runs on.
        """
        current = self.service.list_roles(self.tenant_id)
        row = [r for r in current if r["code"] == role["code"]][0]
        return self.service.update_role(
            actor_user_id=self.admin_id, tenant_id=self.tenant_id,
            role_id=row["id"], name=row["name"],
            permissions=row["permissions"], expected_version=row["version"],
            resource_grants=[])

    def member(self, username, roles, *, password=None):
        """Create a member who already completed the first password change."""
        created = self.service.create_member(
            actor_user_id=self.admin_id, tenant_id=self.tenant_id,
            operation="create-new", username=username,
            display_name=username.title(),
            temporary_password=IdentityStack.TEMP_PASSWORD, roles=list(roles))
        self.service.change_password(
            self.service.login(username, IdentityStack.TEMP_PASSWORD).token,
            IdentityStack.TEMP_PASSWORD, password or IdentityStack.MEMBER_PASSWORD)
        self._passwords[username] = password or IdentityStack.MEMBER_PASSWORD
        self._ids[username] = created["user_id"]
        return created["user_id"]

    def user_id(self, username):
        return self._ids[username]

    def login(self, username, password=None):
        """A real session cookie value for ``username``."""
        token = self.service.login(
            username, password or self._passwords[username]).token
        return token

    # -- requests ----------------------------------------------------------

    def headers(self, token=None, *, tenant=True, json_body=False):
        headers = {"Host": self.HOST, "Origin": self.BASE}
        if token:
            headers["Cookie"] = "cow_session=" + token
        if tenant:
            headers["X-Tenant-ID"] = self.tenant_id
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def request(self, path, method="GET", body=None, token=None, *,
                tenant=True, headers=None):
        payload = None
        if body is not None:
            payload = body if isinstance(body, (bytes, str)) else json.dumps(body)
        merged = self.headers(token, tenant=tenant, json_body=body is not None)
        merged.update(headers or {})
        return self.app.request(path, method=method, headers=merged, data=payload)

    def get(self, path, token=None, **kwargs):
        return self.request(path, "GET", token=token, **kwargs)

    def post(self, path, body, token=None, **kwargs):
        return self.request(path, "POST", body=body, token=token, **kwargs)

    def put(self, path, body, token=None, **kwargs):
        return self.request(path, "PUT", body=body, token=token, **kwargs)

    def delete(self, path, token=None, **kwargs):
        return self.request(path, "DELETE", token=token, **kwargs)

    @staticmethod
    def json(response):
        return json.loads(response.data.decode("utf-8"))

    # -- workspace ---------------------------------------------------------

    def agent_workspace(self, agent_id):
        """The Agent's workspace root as the web layer resolves it."""
        from agent.registry import get_agent_registry
        return get_agent_registry().get(agent_id, require_enabled=False).workspace

    def scheduler_store(self, agent_id):
        """An Agent-scoped view of the one global ``TaskStore``.

        Scheduled tasks no longer live in per-Agent files: one store holds every
        Agent's tasks and the owning Agent is stamped on each task, so a
        per-Agent view is a filter (``list_tasks(agent_id=...)``) rather than a
        separate path. Returning a bound view keeps the tests' per-Agent
        assertions meaningful without every call site repeating the filter.
        Use ``legacy_scheduler_store`` when the *pre-migration* per-Agent layout
        is what is under test.
        """
        return self.global_scheduler_store(agent_id)

    def legacy_scheduler_store(self, agent_id):
        """The pre-migration per-Agent ``TaskStore`` (``<agent>/scheduler/tasks.json``).

        The input side of the tenancy migration and of the boot-time fold into
        the global store; runtime scheduling does not read this path any more.
        """
        from agent.tools.scheduler.task_store import TaskStore
        from common import state_dir
        from common.runtime_identity import RuntimeIdentity

        identity = RuntimeIdentity(agent_id=agent_id, tenant_id=self.tenant_id)
        return TaskStore(str(state_dir.scheduler_file(identity)))

    def global_scheduler_store(self, agent_id=None):
        """The one global ``TaskStore``, optionally scoped to one Agent.

        With ``agent_id`` the returned view filters ``list_tasks`` to that
        Agent's tasks (ownership lives on the task now, not in the file path);
        without it, the raw store sees every Agent's tasks. Built fresh from
        ``state_dir`` rather than through ``get_task_store()`` so a store cached
        for an earlier test's root can never leak into this one.
        """
        from agent.tools.scheduler.task_store import TaskStore
        from common import state_dir
        from common.runtime_identity import RuntimeIdentity

        # Pin the tenant: in database mode ``shared_root()`` resolves through the
        # *ambient* identity, and a test helper runs outside a request (no tenant
        # in scope) where it would resolve the instance root instead -- a
        # different file from the one the request-side handler reads.
        identity = RuntimeIdentity(agent_id=agent_id, tenant_id=self.tenant_id)
        store = TaskStore(
            str(state_dir.shared_root(identity) / "scheduler" / "tasks.json")
        )
        if agent_id is None:
            return store
        from agent.tools.scheduler.integration import AgentScopedTaskStore

        return AgentScopedTaskStore(store, agent_id)

    def seed_task(self, agent_id, **overrides):
        """Put a task in an Agent's schedule, with owner and scope explicit."""
        from datetime import datetime, timedelta

        now = datetime.now()
        task = {
            "id": "task-1",
            "name": "task-1",
            "enabled": True,
            # The owning Agent is stamped on the task itself now that one store
            # holds every Agent's schedule; ownership-by-path is gone, so a seed
            # without this would be bucketed under the default Agent.
            "agent_id": agent_id,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "next_run_at": (now + timedelta(hours=1)).isoformat(),
            "schedule": {"type": "interval", "seconds": 3600},
            "action": {"type": "agent_task", "task_description": "x",
                       "receiver": "user-1", "channel_type": "web"},
            "scope": "public",
        }
        task.update(overrides)
        store = self.scheduler_store(agent_id)
        store.add_task(task)
        return store

    def personal_task(self, agent_id, user_id, **overrides):
        """A task owned by ``user_id`` (created by them, for them)."""
        task = {
            "scope": "personal",
            "owner": {"user_id": user_id, "tenant_id": self.tenant_id,
                      "agent_id": agent_id},
        }
        task.update(overrides)
        return self.seed_task(agent_id, **task)


@contextmanager
def open_capability_actions(mapping, routes=(), namespace=None, disabled=()):
    """Open capability-matrix actions for the duration of one test.

    Test-only. Production opens an action by editing ``auth/capability_matrix.py``
    once the batch it belongs to has real acceptance evidence
    (``design.md`` D1/D2); this helper exists because a handler can only be
    driven over the real WSGI app once its route is *not* ``closed``.

    ``mapping`` is ``{slice_id: {action: access_class}}``. The declared ``open``
    maps are mutated, ``channel.web.route_registry`` is re-executed (its
    ``ROUTES`` entries are built from ``S(...)`` at import, so a closed action is
    materialised as ``{"policy": "closed"}`` otherwise), and the two derived
    tables (``web_channel._WEB_URLS``, ``auth.http_policy.ROUTE_POLICY``) are
    republished before the app is built. Everything is restored on exit.

    ``disabled`` is the *deployment* half of the same state: public action keys
    applied through :func:`capability_matrix.finalize`, which is exactly what a
    restart with ``RDAI_DISABLED_ACTIONS`` set computes. It can only narrow what
    an accepted declaration serves, so passing keys here reproduces the operator
    shutdown switch without editing the declaration.

    ``routes`` and ``namespace`` keep a phase's HTTP tests self-contained while
    its production route registration is still being wired: pass
    ``RouteEntry`` objects to register for the duration of the test and a
    ``{class name: handler class}`` map to publish on ``web_channel`` (which is
    what ``build_web_app`` resolves patterns against).
    """
    import importlib

    from auth import capability_matrix, http_policy
    from channel.web import route_registry, web_channel

    saved_open = {}
    for slice_id, actions in mapping.items():
        spec = capability_matrix.slice_for(slice_id)
        # Both maps: ``open`` is what the gate reads now, ``declared_open`` is
        # the baseline ``finalize()`` recomputes from -- setting only ``open``
        # would be undone by any ``finalize()`` call inside the test.
        saved_open[slice_id] = (dict(spec.open), dict(spec.declared_open))
        spec.declared_open.update(actions)
        spec.open.update(actions)

    saved_disabled = capability_matrix.disabled_actions()
    if disabled:
        capability_matrix.finalize(capability_matrix.parse_disabled_actions(
            ",".join(disabled)))

    saved_urls = web_channel._WEB_URLS
    saved_policy = http_policy.ROUTE_POLICY
    saved_attrs = {}
    for name in (namespace or {}):
        saved_attrs[name] = getattr(web_channel, name, _MISSING)
    # ``importlib.reload`` re-executes the module body, so it rebinds
    # ``CoverageViolation`` and ``RouteEntry`` to *new* objects. Tests that
    # imported them at collection time (``test_upstream_drift_guards``,
    # ``test_route_registry``, ``test_scheduler_create_scope``) keep the
    # pre-reload identity, so ``assertRaises(CoverageViolation)`` would not
    # recognise the class the validator actually raised -- a failure that has
    # nothing to do with the test's subject. Pinning the two classes back makes
    # the reload invisible to those callers while ``ROUTES`` and the derived
    # tables are still genuinely rebuilt from the opened actions.
    pinned = {name: getattr(route_registry, name)
              for name in ("CoverageViolation", "RouteEntry")}

    def _rebuild_route_registry():
        importlib.reload(route_registry)
        for name, value in pinned.items():
            setattr(route_registry, name, value)

    try:
        _rebuild_route_registry()
        if routes:
            route_registry.register_fork_routes(*routes)
        for name, value in (namespace or {}).items():
            setattr(web_channel, name, value)
        web_channel._WEB_URLS = route_registry.derive_web_urls()
        http_policy.ROUTE_POLICY = route_registry.derive_route_policy()
        yield
    finally:
        # The declaration is restored *before* the tables are rebuilt, so the
        # reloaded registry describes the state the process is actually left in.
        capability_matrix.finalize(saved_disabled)
        for slice_id, (open_map, declared) in saved_open.items():
            spec = capability_matrix.slice_for(slice_id)
            spec.open = dict(open_map)
            spec.declared_open = dict(declared)
        for name, value in saved_attrs.items():
            if value is _MISSING:
                delattr(web_channel, name)
            else:
                setattr(web_channel, name, value)
        _rebuild_route_registry()
        web_channel._WEB_URLS = saved_urls
        http_policy.ROUTE_POLICY = saved_policy


@contextmanager
def close_capability_actions(*keys, routes=(), namespace=None):
    """Close *accepted* actions from the deployment side, for one test.

    The mirror of :func:`open_capability_actions` and the state a restart with
    ``RDAI_DISABLED_ACTIONS=<keys>`` produces. ``keys`` are the public action
    keys (``scheduler.runs.list``) -- the same vocabulary the operator's variable
    and the client both use -- and ``implemented``/``accepted`` are deliberately
    left alone, so the projection reports ``disabled_by_deployment`` instead of
    claiming the batch was never accepted.
    """
    with open_capability_actions({}, routes=routes, namespace=namespace,
                                 disabled=keys):
        yield


class _Missing:
    """Sentinel for "the attribute did not exist before the test"."""


_MISSING = _Missing()


def _bootstrap_tenant(service, shared_root, *, tenant_code="acme"):
    root_dir = os.path.dirname(shared_root.rstrip("/")) or shared_root
    tenant = service.bootstrap(
        tenant_code=tenant_code, tenant_name=tenant_code.title(),
        admin_username="root", admin_display="Root",
        admin_password=IdentityStack.ROOT_PASSWORD,
        shared_root=shared_root, allow_weak=True,
    )["id"]
    root = service.list_platform_users()[0]["id"]
    return IdentityStack(service, tenant, root, shared_root)
