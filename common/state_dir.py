"""Single point of truth for where an Agent's state lives on disk.

Two rules keep later work cheap, and both matter:

1. This is the only module that reads ``agent_workspace`` from config.
2. This is the only module that knows the directory layout. Callers ask for
   ``memory_dir()``; they never write ``os.path.join(root, "memory")``. Without
   this second rule, inserting the per-user layer means editing every call site
   a second time.

State falls into three kinds, and which kind a path is decided here:

- **Shared** (``shared_root``): skills, knowledge, MCP servers, credentials,
  products. Copying these per Agent costs an update in N places and a version
  drift; what actually differs between Agents is which ones are switched on,
  not which ones exist. An Agent that genuinely needs its own copy creates the
  directory and wins by presence.
- **Per Agent** (``state_root``): persona, sessions, scratch.
  What makes this Agent a different one from that Agent. Scheduled tasks are
  shared (each task carries its own ``agent_id``) so re-binding a channel
  instance never has to move a file.
- **Per end user** (``user_root``): profile, preferences, memory, task records.
  "Wang likes email over phone calls" is a fact about Wang, not about one
  Agent, so it sits beside the Agents rather than under one of them.

Everything collapses onto the workspace root on a single-Agent install with no
end users, so every path resolves exactly where it does today and the layout
change needs no migration.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Tuple

from common.runtime_identity import RuntimeIdentity, current_identity


class StateDirError(RuntimeError):
    """Raised when an identity names an Agent that does not exist."""


#: The one directory, directly under an Agent's workspace, that holds the
#: per-user platform files (uploads, generated outputs, scratch work). The
#: file surface treats it as protected: every entry admission rule resolves a
#: path through :func:`classify_agent_user_path` before the ordinary
#: Agent/tenant/shared rules may open it (change
#: ``isolate-shared-agent-user-data``). Distinct from ``users/`` under the
#: shared root, which is the per-end-user *memory* store, not platform files.
USER_FILES_CONTAINER = "user"

#: A user id becomes a single path segment, so it must not be able to address a
#: parent, a separator, or anything the filesystem would reinterpret. Real ids
#: are ``usr_<opaque>``; the pattern is the safe superset the store issues.
_USER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _validated_user_id(user_id: Optional[str]) -> Optional[str]:
    """The user id as a safe path segment, or ``None`` when there is none.

    A present-but-unsafe id raises rather than silently resolving to no user:
    falling through to the shared upload directory would turn a malformed
    identity into a cross-user write, which is the exact failure this layout
    exists to prevent.
    """
    if not user_id:
        return None
    candidate = str(user_id)
    if not _USER_ID_RE.fullmatch(candidate):
        raise StateDirError(f"invalid user id for user file directory: {user_id!r}")
    return candidate


def _resolve(identity: Optional[RuntimeIdentity]) -> RuntimeIdentity:
    return identity if identity is not None else current_identity()


def state_root(identity: Optional[RuntimeIdentity] = None) -> Path:
    """Workspace root of the Agent this work belongs to.

    An absent ``agent_id`` resolves to the default Agent: startup tasks such as
    skill sync and scheduler boot legitimately run before routing. An
    ``agent_id`` that does not resolve is a bug and raises rather than quietly
    falling back, which would leak one Agent's files into another's workspace.
    """
    from agent.registry import get_agent_registry

    agent_id = _resolve(identity).agent_id
    registry = get_agent_registry()
    if not agent_id:
        return Path(registry.get(require_enabled=False).workspace)
    try:
        profile = registry.get(agent_id, require_enabled=False)
    except KeyError:
        raise StateDirError(f"unknown agent id: {agent_id!r}") from None
    return Path(profile.workspace)


def _real(path) -> str:
    """Symlink-resolved absolute path for containment comparisons."""
    return os.path.realpath(str(path))


def _contains(a: str, b: str) -> bool:
    """True when path ``a`` equals or is an ancestor of ``b``."""
    try:
        return os.path.commonpath([a, b]) == a
    except ValueError:
        # different drives (Windows) -> cannot be ancestors
        return False


def _instance_root() -> Optional[str]:
    """The deployment's instance root (``agent_workspace``), realpath'd.

    This is where a default tenant's shared root legitimately is (e.g.
    ``~/cow``). It is read from config rather than from the default Agent's
    workspace on purpose: once the default Agent has been given a private
    workspace of its own (``agents/<id>``), deriving the exemption from that
    Agent would shrink it to the subdirectory and falsely reject the instance
    root the default tenant actually uses.
    """
    try:
        from common.utils import expand_path
        from config import conf
        return _real(expand_path(str(conf().get("agent_workspace") or "~/cow")))
    except Exception:
        return None


def _default_agent_workspace() -> Optional[str]:
    """The default Agent's own workspace, realpath'd.

    Equal to the instance root on the usual single-Agent layout, so this is a
    second, independent way to recognise a legitimate root -- and the only one
    available when config was never loaded (as in tests).
    """
    try:
        from agent.registry import get_agent_registry
        return _real(get_agent_registry().get(require_enabled=False).workspace)
    except Exception:
        return None


def _tenant_base_real() -> Optional[str]:
    """The operator-configured base for new tenant shared roots, realpath'd.

    A shared root under this base is as trusted as the engineering root: the
    operator opted into it explicitly (config ``tenant_shared_base`` or env
    ``COW_TENANT_BASE``), so the home/global-escape guard must not reject the
    tenants that legitimately live there. Returns None when unset.
    """
    try:
        from config import get_tenant_shared_base
        return get_tenant_shared_base()
    except Exception:
        return None


def _trusted_roots() -> list:
    """Roots exempt from the home/global escape check, realpath'd.

    Three verified sources, none of them user-supplied: the deployment instance
    root, the default Agent's workspace, and the operator-configured tenant data
    base. They overlap on a plain single-Agent install; the instance root is the
    one that keeps a default tenant resolving after the default Agent moves into
    a private ``agents/<id>`` workspace.
    """
    roots: list = []
    for candidate in (_instance_root(), _default_agent_workspace(),
                      _tenant_base_real()):
        if candidate and candidate not in roots:
            roots.append(candidate)
    return roots


def _is_home_or_global_escape(path: str, home: str, trusted: list) -> bool:
    """True when ``path`` escapes into home/config/global areas.

    Rejects a path that is (or sits inside) the user's home or a global data
    root, unless it is (or sits inside) one of the caller's verified roots (see
    ``_trusted_roots``) -- a default tenant legitimately lives in the instance
    root and new tenants derive under the configured tenant base, and both are
    verified rather than user-controlled.
    """
    if _contains(home, path) and not any(_contains(t, path) for t in trusted):
        return True
    # Global data/config root: tenant data must never be stored in the config/
    # source tree or the shared data directory itself.
    try:
        from config import get_data_root
        data_root = _real(get_data_root())
        if _contains(data_root, path):
            return True
    except Exception:
        pass
    return False


def validate_tenant_shared_root(root: str, *, tenant_id: Optional[str] = None,
                                svc=None) -> None:
    """Reject a shared root that equals/contains/is contained by another tenant's.

    Cross-tenant containment (3.9): two tenants must never share or overlap
    roots, or one tenant could read the other's shared assets. ``tenant_id``
    (when given) is skipped so its own historical nesting stays legal; pass
    None to compare a brand-new tenant against every existing root. Uses
    ``os.path.realpath`` so symlinks cannot smuggle a path out. Throws
    ``StateDirError`` on violation.

    Shared by read-time resolution (``shared_root()``) and by tenant creation
    (``auth.service``), so a root that can never resolve is refused at write
    time instead of poisoning the other tenant's resolution.
    """
    from auth.service import get_identity_service
    svc = svc if svc is not None else get_identity_service()
    target = _real(root)
    for other in svc.tenant_shared_roots():
        # Skip the tenant being validated: its own nested paths are legal.
        if tenant_id is not None and other["id"] == tenant_id:
            continue
        other_root = _real(other["shared_root"])
        # Equal, or one contains the other -> cross-tenant containment.
        if _contains(target, other_root) or _contains(other_root, target):
            who = f"tenant {tenant_id!r} " if tenant_id else ""
            raise StateDirError(
                f"{who}shared root {target!r} overlaps tenant {other['id']!r} "
                f"root {other_root!r}"
            )


def _assert_tenant_roots_do_not_contain(ident, root) -> None:
    """Reject a tenant root that escapes into home/config or another tenant (3.9).

    ``shared_root()`` and the tenant base resolvers must never return a path
    that (a) is/falls inside the user's home or a global data/config root
    (unless it is/falls inside one of ``_trusted_roots()``: the instance root,
    the default Agent's workspace, or the configured tenant base), or (b) equals
    or contains / is contained by another tenant's
    shared root. Both checks use ``os.path.realpath`` so symlinks cannot smuggle
    a path out. Throws ``StateDirError`` on violation. Same-tenant historical
    nesting is allowed -- only *other* tenants' roots are compared.
    """
    from common.runtime_identity import current_identity
    if not ident.tenant_id:
        return
    home = _real(Path.home())

    target = _real(root)
    if _is_home_or_global_escape(target, home, _trusted_roots()):
        raise StateDirError(
            f"tenant {ident.tenant_id!r} shared root {target!r} resolves inside "
            f"the home/global workspace root; refusing to fall back"
        )

    from auth.service import get_identity_service
    try:
        svc = get_identity_service()
        validate_tenant_shared_root(root, tenant_id=ident.tenant_id, svc=svc)
    except StateDirError:
        raise
    except Exception as e:
        # Tie-breaking failure must not silently open a path we cannot verify.
        raise StateDirError(
            f"cannot verify tenant {ident.tenant_id!r} shared root containment: {e}"
        ) from e


def _resolve_tenant_shared_root(ident) -> Path:
    """Resolve the trusted tenant shared root and validate containment (3.9)."""
    from auth.service import get_identity_service
    root = get_identity_service().tenant_shared_root(ident.tenant_id)
    if root:
        _assert_tenant_roots_do_not_contain(ident, root)
        return Path(root)
    raise StateDirError(
        f"tenant {ident.tenant_id!r} has no configured shared root"
    )


def shared_root(identity: Optional[RuntimeIdentity] = None) -> Path:
    """Root of the assets every Agent draws on.

    Implicitly the default Agent's workspace rather than a configured path of
    its own: on a single-Agent install that is the workspace root, so nothing
    moves, and a second Agent reads the skills and credentials that are already
    there instead of needing its own copies. Add a setting if someone ever
    wants the shared area somewhere else.

    In database mode the identity's tenant resolves its trusted shared root
    (task 3.9): a tenant never falls back to another tenant's (or the default
    Agent's) shared assets, and the root is rejected when it escapes into
    home/global or overlaps another tenant.

    ``identity`` must be passed whenever the caller already holds one. Resolving
    it from the ambient identity instead is how "validate against this tenant,
    read from that tenant" becomes possible: the two agree while a handler
    installs the same identity and diverge the moment anything else does.
    """
    ident = _resolve(identity)
    if ident.tenant_id:
        return _resolve_tenant_shared_root(ident)
    from agent.registry import get_agent_registry

    return Path(get_agent_registry().get(require_enabled=False).workspace)


def tenant_app_data_root(tenant_id: str, *, identity_service=None) -> Path:
    """Private application data for a verified tenant, outside its workspace.

    Resolves only; the business store creates its directory after authorization.
    Never use the tenant's shared_root here: it is exposed by file previews.
    """
    import re
    from auth.service import get_identity_service
    from agent.registry import get_agent_registry
    from config import get_data_root

    if not isinstance(tenant_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", tenant_id):
        raise StateDirError("invalid tenant id for private application data")
    svc = identity_service if identity_service is not None else get_identity_service()
    tenant = svc.get_tenant(tenant_id)
    if not tenant or not tenant["active"]:
        raise StateDirError("private application data requires an active tenant")

    data_root = Path(get_data_root()).resolve()
    namespace = data_root / "tenants"
    root = namespace / tenant_id
    # A symlink must not turn a private tenant directory into a workspace or
    # another tenant's directory, including when the target does not exist yet.
    if namespace.is_symlink() or root.is_symlink():
        raise StateDirError("private tenant data directory cannot be a symlink")
    workspaces = [p.workspace for p in get_agent_registry().list(include_disabled=True)]
    workspaces.extend(t["shared_root"] for t in svc.tenant_shared_roots())
    for workspace in filter(None, workspaces):
        workspace = _real(workspace)
        if _contains(workspace, str(root)) or _contains(str(root), workspace):
            raise StateDirError("private tenant data directory overlaps a workspace")
    return root


def user_root(identity: Optional[RuntimeIdentity] = None) -> Path:
    """Root of the data owned by one end user.

    Beside the Agents, not under one of them: a user's preferences are the same
    fact whichever Agent is talking to them, and one copy per Agent would drift.

    Collapses onto the Agent's own root while ``user_id`` is unset, which is
    what makes the tenancy migration a change to this function rather than to
    every caller.
    """
    ident = _resolve(identity)
    if ident.user_id:
        return shared_root(ident) / "users" / ident.user_id
    return state_root(ident)


def state_path(*parts: str, identity: Optional[RuntimeIdentity] = None) -> Path:
    """Escape hatch for paths with no named helper. Prefer adding a helper."""
    return state_root(identity).joinpath(*parts)


# --- Per end user, inside the Agent: platform files ---------------------------
#
# Layered *under* one Agent's workspace rather than beside the Agents, because
# these files belong to the work the Agent did for one user and must travel
# with it: ``<agent workspace>/user/<user_id>/``. The shape mirrors the Agent
# root (one container per Agent) and the personal memory store is a different
# domain entirely (``<shared root>/users/<user_id>``) -- do not confuse them.


def agent_user_container(identity: Optional[RuntimeIdentity] = None, *,
                         base=None) -> Path:
    """``<agent workspace>/user`` -- the protected per-user container."""
    return _agent_base(identity, base) / USER_FILES_CONTAINER


def _assert_user_container_safe(container: Path) -> None:
    """Refuse a container that a path could be redirected through.

    A symlinked or non-directory ``user`` entry cannot be trusted to bound the
    subtree: the classifier compares *real* paths, so a link would make files
    outside the Agent's workspace look as if they lived under a user they do
    not belong to. The product never creates either shape, so this refuses
    rather than trying to interpret it.
    """
    if container.is_symlink():
        raise StateDirError(f"user file container is a symlink: {container}")
    if container.exists() and not container.is_dir():
        raise StateDirError(f"user file container is not a directory: {container}")


def agent_user_root(identity: Optional[RuntimeIdentity] = None, *,
                    ensure: bool = False, base=None) -> Optional[Path]:
    """``<agent workspace>/user/<user_id>``, or ``None`` without a user.

    The single place the per-user file layout is spelled out. ``base`` names
    the Agent workspace explicitly, so value objects that already resolved one
    do not re-resolve an identity they do not hold; without it the Agent's
    ``state_root`` is used.
    """
    ident = _resolve(identity)
    user_id = _validated_user_id(ident.user_id)
    if user_id is None:
        return None
    container = _agent_base(ident, base) / USER_FILES_CONTAINER
    _assert_user_container_safe(container)
    root = container / user_id
    if root.is_symlink():
        raise StateDirError(f"user file directory is a symlink: {root}")
    return _ensure(root, ensure)


def agent_user_uploads_dir(identity: Optional[RuntimeIdentity] = None, *,
                           ensure: bool = False, base=None) -> Optional[Path]:
    """This user's uploads for the addressed Agent (``user/<id>/uploads``)."""
    root = agent_user_root(identity, ensure=ensure, base=base)
    return None if root is None else _ensure(root / "uploads", ensure)


def agent_user_outputs_dir(identity: Optional[RuntimeIdentity] = None, *,
                           ensure: bool = False, base=None) -> Optional[Path]:
    """This user's delivered results (``user/<id>/outputs``).

    Where the platform archives a generated file it is *returning to* one user,
    so the link it mints resolves inside an owner-checked subtree instead of the
    shared workspace root.
    """
    root = agent_user_root(identity, ensure=ensure, base=base)
    return None if root is None else _ensure(root / "outputs", ensure)


def agent_user_work_dir(identity: Optional[RuntimeIdentity] = None, *,
                        ensure: bool = False, base=None) -> Optional[Path]:
    """This user's scratch space for one Agent (``user/<id>/work``)."""
    root = agent_user_root(identity, ensure=ensure, base=base)
    return None if root is None else _ensure(root / "work", ensure)


def classify_agent_user_path(real_path, workspace) -> Tuple[str, Optional[str]]:
    """Locate ``real_path`` relative to one Agent's ``user/`` container.

    Returns ``(state, owner_user_id)``:

    * ``("none", None)`` -- not inside this Agent's user container at all, so
      the ordinary Agent/tenant/shared rules apply unchanged;
    * ``("container", None)`` -- exactly ``<workspace>/user``, which may be
      listed (its entries are filtered one by one);
    * ``("user", uid)`` -- inside ``<workspace>/user/<uid>/...``;
    * ``("unowned", None)`` -- inside ``user/`` but not under a well-formed user
      directory, which callers fail closed on rather than expose.

    Both sides are ``os.path.realpath``-resolved first, so a symlink alias
    (``<workspace>/alias -> <workspace>/user/<uid>``) is classified by where it
    really points. Resolution of a *linked* ``user`` container moves the target
    outside the workspace prefix, so such a path is ``none`` and falls back to
    the ordinary rule for wherever it actually lives -- never a witness that it
    belonged to some user.
    """
    if not real_path or not workspace:
        return "none", None
    real = _real(real_path)
    ws = _real(workspace)
    if not _contains(ws, real):
        return "none", None
    try:
        rel = os.path.relpath(real, ws)
    except ValueError:
        return "none", None
    if rel == os.curdir:
        return "none", None
    parts = rel.split(os.sep)
    if parts[0] != USER_FILES_CONTAINER:
        return "none", None
    if len(parts) == 1:
        return "container", None
    owner = parts[1]
    if not _USER_ID_RE.fullmatch(owner):
        return "unowned", None
    return "user", owner


def _ensure(path: Path, ensure: bool) -> Path:
    if ensure:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _agent_base(identity, base) -> Path:
    return Path(base) if base is not None else state_root(identity)


def _user_base(identity, base) -> Path:
    return Path(base) if base is not None else user_root(identity)


def _shared_or_own(identity, base, *parts: str) -> Path:
    """Shared copy, unless this Agent has one of its own.

    Opting out is by presence rather than by configuration: an Agent that needs
    a private skill set or its own MCP servers creates the directory, and every
    other Agent goes on reading the single shared copy. Nothing to configure in
    the common case, and on a single-Agent install both branches name the same
    path anyway.

    Writes land on whichever of the two this returns, so an Agent that has not
    opted out contributes to the shared copy rather than quietly forking it.
    """
    own = _agent_base(identity, base).joinpath(*parts)
    if own.exists():
        return own
    return shared_root(identity).joinpath(*parts)


# ``base`` lets value objects that already carry a resolved root (MemoryConfig,
# KnowledgeService) reuse the layout without re-resolving an identity they do
# not have. It exists so the layout stays defined exactly once. It names the
# Agent's own root, not a self-contained one: shared assets still fall back to
# the shared copy when that root has none, which is what the callers passing it
# actually want.


# --- Shared: one copy every Agent draws on, unless it opts out ---------------


def skills_dir(identity=None, ensure: bool = False, base=None) -> Path:
    return _ensure(_shared_or_own(identity, base, "skills"), ensure)


def knowledge_dir(identity=None, ensure: bool = False, base=None) -> Path:
    return _ensure(_shared_or_own(identity, base, "knowledge"), ensure)


def websites_dir(identity=None, ensure: bool = False, base=None) -> Path:
    return _ensure(_shared_or_own(identity, base, "websites"), ensure)


def subagents_dir(identity=None, ensure: bool = False, base=None) -> Path:
    """Sub agent templates. Shared like skills: sub agents have no identity of
    their own, they are a way work gets done."""
    return _ensure(_shared_or_own(identity, base, "subagents"), ensure)


def mcp_config_file(identity=None, base=None) -> Path:
    return _shared_or_own(identity, base, "mcp.json")


def env_file(identity=None, base=None) -> Path:
    """Credentials and model keys. Shared, because they belong to the person who
    runs the instance rather than to one of their Agents."""
    return _shared_or_own(identity, base, ".env")


def system_dir(base=None, ensure: bool = False) -> Path:
    """Instance-wide system state that isn't user config.

    Shared, not per Agent: things like the per-provider model catalog are a
    property of the instance (same as the global model keys), not of whichever
    Agent happens to be talking. Kept out of ``config.json`` so derived/managed
    documents don't bloat the hand-editable config.
    """
    root = Path(base) if base is not None else shared_root()
    return _ensure(root / "system", ensure)


def models_catalog_file(base=None) -> Path:
    """The per-provider model catalog overlay (overrides + hidden)."""
    return system_dir(base) / "models.json"


def scheduler_recipients_file(base=None) -> Path:
    """The one directory of people/groups observed on inbound IM channels.

    Shared, not per Agent: a contact is a fact about the person, not about which
    Agent happened to talk to them, so one copy lets any Agent's console build a
    scheduled task for anyone the instance has ever met. It lives beside the
    global ``tasks.json`` under ``scheduler`` — the recipients a scheduled push
    can target — rather than in a separate directory.
    """
    if base is not None:
        return Path(base) / "scheduler" / "recipients.json"
    return shared_root() / "scheduler" / "recipients.json"


def scheduler_file_global(base=None) -> Path:
    """The one task store every Agent's scheduled tasks live in.

    Shared, not per Agent, and each task carries its own ``agent_id`` (the Agent
    it runs as) plus ``instance_id`` (the channel login it delivers through).
    Keeping ownership *on the task* rather than *in the file path* is what lets a
    channel instance be re-bound to a different Agent without moving any task
    data: delivery resolves by ``instance_id`` and execution sets identity from
    the task's ``agent_id``, neither of which depends on where the file sits.

    Superseded the per-Agent ``scheduler_file`` (``<agent>/scheduler/tasks.json``);
    a one-time boot migration folds those into this single store.
    """
    if base is not None:
        return Path(base) / "scheduler" / "tasks.json"
    return shared_root() / "scheduler" / "tasks.json"


# --- Per Agent: what makes this Agent a different one -----------------------


def scheduler_file(identity=None, base=None) -> Path:
    return _agent_base(identity, base) / "scheduler" / "tasks.json"


def tmp_dir(identity=None, ensure: bool = True, base=None) -> Path:
    """Transient downloads and synthesized media.

    Agent-scoped for now. It holds inbound attachments, so it arguably belongs
    to the user; revisit when tenancy lands rather than guessing now.
    """
    return _ensure(_agent_base(identity, base) / "tmp", ensure)


# --- User-scoped: isolated per end user once tenancy lands -------------------


def memory_dir(identity=None, ensure: bool = False, base=None) -> Path:
    return _ensure(_user_base(identity, base) / "memory", ensure)


def memory_index_db(identity=None, ensure: bool = True, base=None) -> Path:
    """The index, not the content: one database per Agent, isolated per user by
    the ``user_id`` and ``scope`` columns rather than by path.

    Deliberately not under ``user_root``, unlike the memory files it indexes.
    It also holds the sessions and runs tables, which are per Agent, and a
    database per user would make "what do I know about Wang" a fan-out over N
    files. Filtering has to be applied at the retrieval entry point instead:
    miss one query and it is a cross-user leak, so there is exactly one place
    that may build these WHERE clauses.
    """
    index_dir = _ensure(_agent_base(identity, base) / "memory" / "long-term", ensure)
    return index_dir / "index.db"


def memory_file(identity=None, base=None) -> Path:
    return _user_base(identity, base) / "MEMORY.md"


def output_dir(identity=None, ensure: bool = False, base=None) -> Path:
    return _ensure(_user_base(identity, base) / "output", ensure)


def runs_dir(identity=None, ensure: bool = False, base=None) -> Path:
    """Task execution records. User-scoped because a trace holds full tool
    output: one user's runs must not be readable by another."""
    return _ensure(_user_base(identity, base) / "runs", ensure)


def persona_file(identity=None, base=None) -> Path:
    """The end user's personal persona, layered on top of an Agent's own.

    A file rather than a column, so it is editable in place and versionable.
    Never created implicitly here: a user without one simply gets no personal
    segment, which keeps "has not set a persona" distinguishable from "set an
    empty one". Collapses onto the Agent root while ``user_id`` is unset.
    """
    return _user_base(identity, base) / "PERSONA.md"


# --- Compatibility -----------------------------------------------------------


def state_root_str(identity: Optional[RuntimeIdentity] = None) -> str:
    """``state_root`` for the many call sites that still pass str paths around."""
    return str(state_root(identity))


def real_state_root(identity: Optional[RuntimeIdentity] = None) -> str:
    """Symlink-resolved root, for containment checks that compare prefixes."""
    return os.path.realpath(str(state_root(identity)))
