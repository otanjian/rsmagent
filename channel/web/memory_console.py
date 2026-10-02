# encoding:utf-8
"""``GET /api/memory`` / ``GET /api/memory/content``: one compatibility read
surface, three explicit targets (change ``complete-database-capability-parity``,
task group 5).

Why a separate module
---------------------
The two endpoints above are upstream's *Agent workspace memory* read. After the
fork the same question has three different answers, and only one of them is
"the Agent's workspace":

``personal``       the caller's own ``/api/memory/personal`` domain — keyed by
                   *(tenant, user)*, never by an Agent. Delegated to the
                   delivered :class:`PersonalMemoryService`.
``private_agent``  a privately owned Agent's memory. Tenant-wide reach is not
                   enough: the owner check (``_require_private_owner``) runs
                   *before* anything is read, and an administrator is not an
                   exception.
``shared``         the tenant's shared Agent memory. This is a *tenant resource*,
                   so it takes the tenant-administration qualification: an
                   ordinary member holding ``chat.use`` on that Agent is refused
                   (``not_authorized``) rather than shown a filtered page — the
                   same management range ``auth.object_scope`` gives the roster
                   read (change ``unify-console-by-data-scope``, task 2.1).

The handler keeps its upstream shape (one ``try``, one ``except``), and this
module owns the two things that must not be spread across handlers: resolving a
request into exactly one target, and refusing when there is no single correct
target. It is a *delegation* seam — no second memory CRUD, no second index, no
second route table.

Deliberately refused, never guessed
-----------------------------------
* an unknown ``scope`` (``unknown_scope``) — including an empty tenant root:
  the personal scope never falls back to the tenant shared root (design D5), so
  a caller who asks for "my memory" and has none gets an empty *personal* list,
  not the tenant's files;
* ``scope=personal`` together with ``agent_id`` (``ambiguous_target``): the
  personal domain is not an Agent's, so accepting both would mean silently
  picking one;
* an unknown ``category`` (``unknown_category``), and any category other than
  ``memory`` for the personal domain;
* an address that does not resolve (``unknown_agent`` / ``unknown_entry``);
* a target owned by someone else (``not_owner``) — checked before the read, so
  a refusal cannot be a filtered read;
* a **shared** target asked for without the management qualification
  (``not_authorized``) — again before the read, so no shared entry, total or body
  is produced for a caller outside its range;
* a path that is not a plain file inside the root (``unsafe_path``).

Codes are stable strings in the JSON body (``{"status": "error", "code": ...,
"message": ...}``); the HTTP status is the class of refusal (400 malformed,
403 not-yours, 404 unknown, 503 unavailable), which is what the console
branches on and what the route gate already answers for a foreign tenant.

Cross-tenant note
-----------------
There is deliberately **no** ``cross_tenant`` code here. A target that belongs
to another tenant is answered as ``unknown_agent`` with 404 — the same
non-disclosing answer ``_require_tenant_agent_binding`` has always given (pinned
by ``tests/test_tenant_read_scoping.py``), because distinguishing "exists in
another tenant" from "does not exist" is exactly the existence leak the change
forbids. The 403 a foreign tenant's *member* receives comes from the HTTP gate,
which refuses the tenant selection itself (no membership), not from here.

Reading and writing
-------------------
The personal scope's payload carries ``"read_only": true`` because this surface
serves no write verb for it: writes go to ``POST /api/memory/personal`` (which
the response's ``id``/``revision`` address). The Agent scopes' payloads derive
``read_only`` from their category and ``actions`` from the caller's range, and
their write verbs are ``/api/memory/save|delete|clear``.

An Agent memory root is, in database mode, the **tenant shared root**: the same
bytes carry a private Agent's memory and the tenant's shared Agent memory.
Reads tolerate that; writes cannot, so :func:`_require_root_writable` demands
qualification for *every* scope reaching the root. See that function for the
measurement that forced it.

Client contract (for the console page, task 9.1)
------------------------------------------------
``personal`` rows are addressed by ``id`` (= ``filename`` = the entry id
relative to the *personal root*, which is ``<tenant>/users/<user_id>``, **not**
the workspace root). A page that edits must route those rows to
``/api/memory/personal``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import web

from auth.object_scope import MANAGE

from common.log import logger
from common import safe_fs

# --- scope vocabulary -------------------------------------------------------

SCOPE_PERSONAL = "personal"
SCOPE_PRIVATE_AGENT = "private_agent"
SCOPE_SHARED = "shared"

#: The picker value that means "my own user memory". A distinct non-empty token,
#: not ``""``: an empty value is how the console spells "nothing chosen yet", and
#: the two must not collapse or the personal domain becomes unreachable.
MEMORY_PERSONAL_VALUE = "personal"

#: The scope names a caller may pass. Anything else is ``unknown_scope``: a
#: typo must not silently read the caller's own memory, and an omitted scope is
#: *not* a synonym for "whatever is convenient".
SCOPES = (SCOPE_PERSONAL, SCOPE_PRIVATE_AGENT, SCOPE_SHARED)

#: Categories the Agent workspace memory reader knows (upstream's three tabs).
CATEGORY_MEMORY = "memory"
CATEGORY_DREAM = "dream"
CATEGORY_EVOLUTION = "evolution"
CATEGORIES = (CATEGORY_MEMORY, CATEGORY_DREAM, CATEGORY_EVOLUTION)

#: Categories addressable in the personal domain. Personal memory is one list;
#: dream diaries and evolution logs are Agent artefacts, not the member's own
#: memory, and answering the personal scope from an Agent's directory is the
#: scope widening this surface exists to prevent.
PERSONAL_CATEGORIES = (CATEGORY_MEMORY, CATEGORY_EVOLUTION, CATEGORY_DREAM)

MAIN_ENTRY = "MEMORY.md"

#: Upper bound on one compatibility page, so a client cannot ask the console to
#: render an unbounded domain in a single response.
MAX_PAGE_SIZE = 200

# --- stable machine codes ---------------------------------------------------

CODE_UNKNOWN_SCOPE = "unknown_scope"
CODE_AMBIGUOUS_TARGET = "ambiguous_target"
CODE_UNKNOWN_CATEGORY = "unknown_category"
CODE_UNKNOWN_AGENT = "unknown_agent"
CODE_UNKNOWN_ENTRY = "unknown_entry"
CODE_ENTRY_REQUIRED = "entry_required"
CODE_INVALID_ENTRY = "invalid_entry"
CODE_INVALID_PAGING = "invalid_paging"
CODE_NOT_OWNER = "not_owner"
CODE_NOT_AUTHORIZED = "not_authorized"
CODE_UNSAFE_PATH = "unsafe_path"
CODE_MEMORY_UNAVAILABLE = "memory_unavailable"
#: The write verbs here serve the *Agent* domain only. The member's own memory
#: already has a versioned write surface (``POST /api/memory/personal``); a
#: second path to the same files would be the "second memory CRUD" this seam's
#: docstring forbids, and the two would drift on revision semantics.
CODE_PERSONAL_WRITE_ENDPOINT = "personal_write_endpoint"
CODE_READ_ONLY_CATEGORY = "read_only_category"
CODE_UNKNOWN_ACTION = "unknown_action"
CODE_INDEX_PENDING = "index_pending"

_STATUS_LINES = {
    400: "400 Bad Request",
    401: "401 Unauthorized",
    403: "403 Forbidden",
    404: "404 Not Found",
    409: "409 Conflict",
    503: "503 Service Unavailable",
}

#: Directories the Agent memory reader lists per category. Guarded as a whole:
#: they live under the tenant shared root, so a symlinked one would redirect the
#: read into another member's personal directory.
_AGENT_LIST_DIRS = {
    CATEGORY_MEMORY: ("memory",),
    CATEGORY_DREAM: ("memory/dreams",),
    CATEGORY_EVOLUTION: ("memory/evolution", "memory/dreams"),
}

_ROW_SUBDIRS = {
    "daily": "memory",
    "dream": "memory/dreams",
    "evolution": "memory/evolution",
}


class MemoryScopeError(Exception):
    """A refused compatibility read, with the code the console branches on."""

    def __init__(self, code: str, message: str = "", *, status: int = 400):
        super().__init__(message or code)
        self.code = code
        self.status = int(status)


class MemoryTarget:
    """One resolved read target: scope, Agent, category and page."""

    __slots__ = ("scope", "agent_id", "category", "page", "page_size", "entry")

    def __init__(self, scope: str, agent_id: Optional[str], category: str,
                 page: int, page_size: int, entry: str = ""):
        self.scope = scope
        self.agent_id = agent_id
        self.category = category
        self.page = page
        self.page_size = page_size
        self.entry = entry

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return ("MemoryTarget(scope=%r, agent_id=%r, category=%r, page=%r, "
                "page_size=%r, entry=%r)" % (self.scope, self.agent_id,
                                             self.category, self.page,
                                             self.page_size, self.entry))


def http_error(error: MemoryScopeError) -> "web.HTTPError":
    """The ``web.HTTPError`` a refusal is answered with (status + JSON body)."""
    body = json.dumps({"status": "error", "code": error.code,
                       "message": str(error)}, ensure_ascii=False)
    return web.HTTPError(
        _STATUS_LINES.get(error.status, "%d Error" % error.status),
        {"Content-Type": "application/json; charset=utf-8"}, body)


def unavailable(_exc: BaseException = None) -> MemoryScopeError:
    """The one refusal for "the memory store could not answer at all"."""
    return MemoryScopeError(CODE_MEMORY_UNAVAILABLE, "记忆暂时不可用", status=503)


# --- parameter reading ------------------------------------------------------

def _web_channel():
    from channel.web import web_channel
    return web_channel


def _first(value):
    """``web.input`` merges query and body, so a repeated field arrives as a list."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _text_param(params, *names: str) -> str:
    for name in names:
        value = _first(getattr(params, name, None))
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _read_scope(params) -> Optional[str]:
    raw = _first(getattr(params, "scope", None))
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text not in SCOPES:
        raise MemoryScopeError(CODE_UNKNOWN_SCOPE,
                               "未知的记忆作用域: %s" % text, status=400)
    return text


def _read_category(params, scope: str) -> str:
    raw = _first(getattr(params, "category", None))
    text = str(raw).strip() if raw is not None else ""
    if not text:
        text = CATEGORY_MEMORY
    if text not in CATEGORIES:
        raise MemoryScopeError(CODE_UNKNOWN_CATEGORY,
                               "未知的记忆分类: %s" % text, status=400)
    if scope == SCOPE_PERSONAL and text not in PERSONAL_CATEGORIES:
        raise MemoryScopeError(
            CODE_UNKNOWN_CATEGORY,
            "个人记忆不提供该分类: %s" % text, status=400)
    return text


def _read_paging(params) -> "tuple[int, int]":
    page = _int_param(params, "page", 1)
    page_size = _int_param(params, "page_size", 20)
    if page < 1 or page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise MemoryScopeError(
            CODE_INVALID_PAGING,
            "分页参数无效（page>=1，1<=page_size<=%d）" % MAX_PAGE_SIZE, status=400)
    return page, page_size


def _int_param(params, name: str, default: int) -> int:
    raw = _first(getattr(params, name, None))
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise MemoryScopeError(CODE_INVALID_PAGING,
                               "分页参数无效: %s" % name, status=400) from None


# --- target resolution ------------------------------------------------------

def resolve_target(ctx, params, *, entry: bool = False) -> MemoryTarget:
    """Turn one request into exactly one read target, or refuse.

    ``ctx`` is the verified :class:`auth.runtime.RequestContext`; every
    ownership fact comes from it, never from a parameter.
    """
    wc = _web_channel()
    requested = _read_scope(params)
    agent_id = wc._request_agent_id(params)
    entry_name = _text_param(params, "filename", "entry", "id") if entry else ""
    if entry and not entry_name:
        raise MemoryScopeError(CODE_ENTRY_REQUIRED, "缺少 filename", status=400)

    if requested == SCOPE_PERSONAL or (requested is None and not agent_id):
        if agent_id:
            raise MemoryScopeError(
                CODE_AMBIGUOUS_TARGET,
                "个人记忆不接受 agent_id：个人记忆属于成员，不属于某个智能体", status=400)
        category = _read_category(params, SCOPE_PERSONAL)
        page, page_size = _read_paging(params)
        return MemoryTarget(SCOPE_PERSONAL, None, category, page, page_size,
                            entry_name)

    if not agent_id:
        raise MemoryScopeError(CODE_AMBIGUOUS_TARGET,
                               "缺少目标：需要 agent_id 或 scope=personal", status=400)

    category = _read_category(params, requested or SCOPE_SHARED)
    page, page_size = _read_paging(params)
    scope, agent_id = _resolve_agent_target(ctx, agent_id, requested)
    return MemoryTarget(scope, agent_id, category, page, page_size, entry_name)


def _resolve_agent_target(ctx, agent_id: str, requested: Optional[str]):
    """``(scope, agent_id)`` for an Agent-addressed read, or a refusal."""
    wc = _web_channel()
    try:
        wc._require_tenant_agent_binding(ctx, agent_id)
    except web.HTTPError as error:
        if _status_of(error) == 404:
            raise MemoryScopeError(CODE_UNKNOWN_AGENT, "该智能体不属于当前租户",
                                   status=404) from error
        raise
    try:
        wc._require_private_owner(ctx, agent_id)
    except web.HTTPError as error:
        raise MemoryScopeError(CODE_NOT_OWNER, "该记忆属于其他成员", status=403) from error

    binding = _agent_binding(agent_id)
    owner = binding.get("private_owner_user_id") if binding else None
    kind = SCOPE_PRIVATE_AGENT if owner else SCOPE_SHARED
    if kind == SCOPE_PRIVATE_AGENT and owner != getattr(ctx, "user_id", None):
        # ``_require_private_owner`` refuses this already; repeated here so the
        # scope decision can never depend on a check someone reorders later.
        raise MemoryScopeError(CODE_NOT_OWNER, "该记忆属于其他成员", status=403)

    # The target set is filtered on the server, by the same authority the roster
    # read uses (task 2.1/2.2): an ordinary member has their own personal memory
    # and their own private Agents' memory; a shared Agent's memory is a tenant
    # resource, so reading *it* — and not merely writing it — needs the
    # tenant-administration qualification. Holding ``chat.use`` on that Agent is
    # not management access, so a member asking for it is refused outright rather
    # than served a filtered page (spec ``database-memory-console``: no shared
    # entries, statistics or body).
    from auth.object_scope import ObjectScope

    if kind == SCOPE_SHARED and not ObjectScope.from_context(ctx).allows_agent_memory(binding):
        raise MemoryScopeError(
            CODE_NOT_AUTHORIZED,
            "共享智能体记忆需要管理资格", status=403)

    if requested and requested != kind:
        declared = "私有智能体" if requested == SCOPE_PRIVATE_AGENT else "共享智能体"
        raise MemoryScopeError(CODE_AMBIGUOUS_TARGET,
                               "该智能体不是%s" % declared, status=400)
    return kind, agent_id


def _agent_binding(agent_id: str) -> Dict[str, Any]:
    """The Agent's binding row, or ``{}`` when the store cannot answer.

    A missing row is read as "not privately owned", the same rule
    ``web_channel._db_path_owner_forbidden`` already applies to this binding:
    ``_require_tenant_agent_binding`` refused every Agent that is not bound to
    the caller's tenant just above, so an absent row here can only be a store
    failure — and reading it as *not* privately owned never grants a read the
    owner check would have refused.
    """
    try:
        from auth.service import get_identity_service
        return get_identity_service().get_agent_binding(agent_id) or {}
    except MemoryScopeError:
        raise
    except Exception as error:  # noqa: BLE001 - unavailable is not "shared"
        logger.warning("[MemoryConsole] agent binding read failed: %s", error)
        raise unavailable(error) from error


def _status_of(error: "web.HTTPError") -> int:
    try:
        return int(str(error.args[0]).split()[0])
    except (IndexError, TypeError, ValueError):
        return 0


# --- delegation: list -------------------------------------------------------

def list_response(ctx, params) -> str:
    """The JSON body of ``GET /api/memory`` for the resolved target."""
    target = resolve_target(ctx, params)
    if target.scope == SCOPE_PERSONAL:
        service = _personal_service(ctx)
        payload = _personal_list(service, target.page, target.page_size, target.category)
        payload["read_only"] = not service.write_enabled()
    else:
        payload = _agent_list(ctx, target)
    # Sent with every list so the page can build its target picker from the same
    # answer the read is authorised by (task 5.1, mirroring the channel
    # candidates of task 6.2). Built from the resolved target's own caller, so a
    # picker can never offer a target this very request would be refused.
    payload["targets"] = target_list(ctx)
    return _envelope(target, payload)


def target_list(ctx) -> List[Dict[str, Any]]:
    """The memory targets this caller may address, ownership included.

    The page's choose-a-target component must not offer a domain the read/edit
    would refuse, and only the server knows which those are — so the set is
    derived from the *same* predicate the request path runs
    (:meth:`auth.object_scope.ObjectScope.allows_agent_memory`), never from the
    caller's role name nor from the console's own Agent catalogue. The console's
    catalogue is a **use** range: for a member it contains the tenant's shared
    Agents, whose memory is a tenant resource a member may not manage. Offering
    those is exactly the defect this list removes (spec
    ``database-memory-console``: 共享智能体记忆需要管理资格).

    The member's own user memory is always present and is listed first: it is the
    one domain that needs no Agent and no grant, and it is what a member who owns
    no private Agent has to reach.
    """
    wc = _web_channel()
    targets: List[Dict[str, Any]] = [{
        "kind": "personal",
        "value": MEMORY_PERSONAL_VALUE,
        "scope": SCOPE_PERSONAL,
    }]
    for profile, _tenant_default, _can_chat, unavailable_reason in (
            wc._iter_tenant_agents(ctx, action=MANAGE, include_disabled=True)):
        binding = wc._agent_binding_for(ctx, profile.id) or {}
        owns = binding.get("private_owner_user_id") == getattr(ctx, "user_id", None)
        targets.append({
            "kind": "agent",
            "value": profile.id,
            "agent_id": profile.id,
            "name": profile.name,
            "scope": SCOPE_PRIVATE_AGENT if owns else SCOPE_SHARED,
            # A stopped Agent still holds memory worth reading or clearing —
            # stopping refuses new *traffic*, not access to what is stored — so
            # it stays offered and says what it is rather than disappearing.
            "enabled": unavailable_reason != "agent_disabled",
        })
    return targets


def _personal_list(service, page: int, page_size: int, category="memory") -> Dict[str, Any]:
    from agent.memory.personal import scope_transaction
    with scope_transaction(service.user_root()):
        entries = service.list_entries(category)
        all_entries = service.list_entries('all')
        collection_revision = service._collection_revision(all_entries)
        state = service.scope_status()
    start = (page - 1) * page_size
    return {
        "page": page,
        "page_size": page_size,
        "total": len(entries),
        "list": [_personal_row(entry) for entry in entries[start:start + page_size]],
        "collection_revision": collection_revision,
        "counts": {kind: sum(e['type'] == kind for e in all_entries)
                   for kind in ('global', 'daily', 'dream', 'evolution')},
        "actions": {"edit": service.write_enabled(), "delete": True, "clear": True},
        "index_state": 'pending' if state['pending'] else 'ok',
        "owner": {"user_id": service._require_scope().user_id,
                  "tenant_id": service._require_scope().tenant_id},
    }


def _personal_row(entry: Dict[str, Any]) -> Dict[str, Any]:
    """One personal entry in the legacy row shape.

    ``filename`` is the entry id (``MEMORY.md`` / ``memory/<name>.md``) — the
    value the legacy viewer sends straight back as ``filename`` — and ``id``
    repeats it because that is the parameter ``/api/memory/personal`` takes.
    ``actions`` is reported empty: this surface serves no write verb, so it
    must not advertise one.
    """
    entry_id = str(entry.get("id") or "")
    return {
        "filename": entry_id,
        "type": entry.get('type', "global" if entry_id == MAIN_ENTRY else "daily"),
        "size": entry.get("size", 0),
        "updated_at": entry.get("updated_at", ""),
        "id": entry_id,
        "revision": entry.get("revision"),
        "actions": entry.get('actions', {}),
    }


def _agent_list(ctx, target: MemoryTarget) -> Dict[str, Any]:
    from agent.memory.service import MemoryService

    root = _agent_root(ctx, target.agent_id)
    for relative in _AGENT_LIST_DIRS.get(target.category, ()):
        _assert_plain_dir(root, relative)

    service = MemoryService(root)
    try:
        result = service.list_files(page=target.page, page_size=target.page_size,
                                    category=target.category)
    except (ValueError, FileNotFoundError) as error:
        raise MemoryScopeError(CODE_UNKNOWN_ENTRY, str(error), status=404) from error

    rows, dropped = _plain_rows(root, list(result.get("list") or []))
    result["list"] = rows
    if dropped:
        # A symlinked entry is invisible everywhere it is observable, so it is
        # removed from the page *and* from the total it was counted in.
        result["total"] = max(0, int(result.get("total", 0)) - dropped)
    # The revision and the offered verbs are what make an edit possible at all:
    # the write path is version-conditioned, so a page that was never handed a
    # revision could only ever be refused ("revision_required"), and a verb
    # offered for a read-only category would be clickable-but-refused. Both are
    # derived here, once, from the same rules the write enforces.
    for row in result["list"]:
        row["revision"] = _entry_revision(root, _row_relative(row))
        row["actions"] = _entry_actions(ctx, target)
    return result


def _root_is_writable(ctx, target: MemoryTarget) -> bool:
    """Whether a *write* to this target's memory root is allowed at all.

    In database mode an Agent's memory root is the **tenant shared root**
    (``_get_workspace_root`` → ``resolve_tenant_workspace_root``), so a private
    Agent's memory and the tenant's shared Agent memory are literally the same
    bytes. Reads can afford that: being served those bytes through your own
    private Agent grants nothing you could not already see. A write cannot.

    Measured before this guard existed, with the per-target range rule alone:
    a member's ``save`` through their private Agent's scope was visible on the
    shared scope, and a member's ``delete`` removed the very file the shared
    Agent reads — i.e. a member could rewrite the tenant's shared memory and
    inject text the shared Agent retrieves. So a write here requires
    qualification for *every* scope that reaches this root, which today is the
    tenant-administration qualification the shared scope already demands.

    This is deliberately the *stronger* rule than the read's. A per-Agent memory
    root would make the narrower owner rule correct; until then the narrower
    rule is an escalation, and the member-facing half of task 5.1 waits on it.
    """
    from auth.object_scope import ObjectScope

    scope = ObjectScope.from_context(ctx)
    return bool(scope.has_tenant and scope.is_admin)


def _require_root_writable(ctx, target: MemoryTarget) -> None:
    if not _root_is_writable(ctx, target):
        raise MemoryScopeError(
            CODE_NOT_AUTHORIZED,
            "该记忆根目录同时是租户共享记忆，写入需要管理资格", status=403)


def _entry_actions(ctx, target: MemoryTarget) -> Dict[str, bool]:
    """The verbs the page may offer for an entry of this category and target.

    Two independent reasons refuse an edit, and both are reported here so the
    page never offers a control the write would refuse: the category (the Agent
    writes its own dream and evolution diaries) and the caller's range on the
    *root* (:func:`_root_is_writable` — an Agent memory root is the tenant's
    shared root today).
    """
    writable = (target.category == CATEGORY_MEMORY
                and _root_is_writable(ctx, target))
    return {"edit": writable, "delete": writable}


def _entry_revision(root: str, relative: str):
    """The content revision of one entry, or ``None`` when it is absent.

    Deliberately the *same* hash the write path compares against
    (``_revision_of``): a locally invented revision format would make every edit
    conflict, which reads as "someone else changed it" and is indistinguishable
    from a real lost update.
    """
    if not relative:
        return None
    from agent.memory.personal import _revision_of
    try:
        text = safe_fs.read_text(root, relative)
    except (safe_fs.UnsafePathError, FileNotFoundError, NotADirectoryError,
            OSError):
        return None
    if text is None:
        return None
    return _revision_of(text)


def _agent_root(ctx, agent_id: str) -> str:
    """The Agent memory root, as the delivered fork resolves it per request."""
    wc = _web_channel()
    return wc._get_workspace_root(agent_id=agent_id)


def _plain_rows(root: str, rows: List[Dict[str, Any]]):
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for row in rows:
        relative = _row_relative(row)
        if relative and _is_symlink(root, relative):
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


def _row_relative(row: Dict[str, Any]) -> str:
    name = str(row.get("filename") or "")
    if not name:
        return ""
    if row.get("type") == "global" or name == MAIN_ENTRY:
        return name
    sub = _ROW_SUBDIRS.get(str(row.get("type") or ""))
    return "%s/%s" % (sub, name) if sub else ""


# --- delegation: content ----------------------------------------------------

def content_response(ctx, params) -> str:
    """The JSON body of ``GET /api/memory/content`` for the resolved target."""
    target = resolve_target(ctx, params, entry=True)
    if target.scope == SCOPE_PERSONAL:
        service = _personal_service(ctx)
        read = service.read(target.entry)
        payload = {
            "filename": target.entry,
            # Relative to the *personal* root; the write address for this entry
            # is /api/memory/personal, which is why ``read_only`` travels with
            # the payload.
            "rel_path": target.entry,
            "content": read.get("content") or "",
            "id": target.entry,
            "revision": read.get("revision"),
            "read_only": not service.write_enabled(),
            "actions": {"edit": service.write_enabled(), "delete": read.get('revision') is not None},
        }
    else:
        payload = _agent_content(ctx, target)
    return _envelope(target, payload)


# --- delegation: write (task 5.1 second half) -------------------------------

def write_response(ctx, params, action: str) -> str:
    """The JSON body of one write verb on the Agent-domain memory surface.

    Authorization happens *before* any mutation: :func:`resolve_target` applies
    the same target-range rule the reads use (owner for a private Agent,
    tenant-administration qualification for a shared one), so a caller who may
    not read a target cannot write it either. The mutation itself is the
    delivered ``PersonalMemoryService`` flow — version condition, atomic write,
    publish intent, tombstone masking, retry — inherited by ``MemoryService``
    for the Agent root; nothing here re-implements it.
    """
    target = resolve_target(ctx, params, entry=(action not in ("clear", "retry_index")))
    if target.scope == SCOPE_PERSONAL:
        service = _personal_service(ctx)
        entry = target.entry
    else:
        service = _agent_service(ctx, target)
        entry = _entry_relative(target.category, target.entry) if action != 'clear' else ''
    if target.scope != SCOPE_PERSONAL and target.category != CATEGORY_MEMORY:
        # Dream diaries and evolution logs are written by the Agent itself; a
        # manual edit is overwritten by the next consolidation run. Both roles
        # are refused, so the read-only state does not depend on who asks.
        raise MemoryScopeError(
            CODE_READ_ONLY_CATEGORY,
            "该分类由智能体自己写入，不支持手工修改", status=403)
    if target.scope != SCOPE_PERSONAL:
        _require_root_writable(ctx, target)

    # The read surface's own convention is a bare ``filename`` plus
    # ``category``; ``_entry_relative`` is the one place that converts the two
    # into the workspace-relative path the write flow addresses, so a row the
    # page has just listed can be edited without re-deriving its layout.
    revision = _raw_param(params, "revision")
    if action in ('save', 'delete') and target.scope == SCOPE_PERSONAL and not revision:
        raise MemoryScopeError('revision_required', '修改需要当前版本', status=409)
    try:
        if action == "save":
            result = service.save(entry, _raw_param(params, "content"),
                                  expected_revision=revision)
        elif action == "delete":
            result = service.delete(entry, expected_revision=revision)
        elif action == "clear":
            clear_scope = _text_param(params, 'clear_scope') or 'memory'
            if target.scope == SCOPE_PERSONAL:
                if clear_scope == 'all_personal' and not revision:
                    raise MemoryScopeError('revision_required', '清空需要当前集合版本', status=409)
                result = service.clear(expected_revision=revision, clear_scope=clear_scope)
            else:
                result = service.clear(expected_revision=revision)
        elif action == 'retry_index' and target.scope == SCOPE_PERSONAL:
            result = service.retry_pending_index()
        else:
            raise MemoryScopeError(CODE_UNKNOWN_ACTION,
                                   "未知的记忆操作: %s" % action, status=400)
    except MemoryScopeError:
        raise
    except Exception as error:  # noqa: BLE001 - delegate refusals are typed
        raise refusal_for(error) from error

    index_state = str(result.get("index_state") or "")
    payload: Dict[str, Any] = {"action": action, "result": result}
    if index_state and index_state != "ok":
        # The content operation succeeded but the index is behind (or the
        # operation was overtaken). Reporting "success" would claim a
        # consistency the store does not have — the same rule the personal
        # endpoint applies (task 5.3).
        payload.update({"status": "pending", "code": CODE_INDEX_PENDING,
                        "message": "内容已更新，索引待重试",
                        "index_state": index_state})
        body: Dict[str, Any] = {"scope": target.scope}
        if target.agent_id:
            body["agent_id"] = target.agent_id
        body.update(payload)
        return json.dumps(body, ensure_ascii=False)

    payload.update({"status": "success", "index_state": index_state or "ok"})
    return _envelope(target, payload)


def _raw_param(params, name: str):
    """A parameter *without* the whitespace-trimming the text getter applies.

    Memory content is text: a body of only newlines is a legitimate value the
    caller may want to store, and ``_text_param`` would report it as absent.
    """
    value = _first(getattr(params, name, None))
    return value


def _agent_service(ctx, target: MemoryTarget):
    """The Agent-domain memory service bound to this target's workspace."""
    from agent.memory.service import MemoryService

    return MemoryService(_agent_root(ctx, target.agent_id))


def _envelope(target: MemoryTarget, payload: Dict[str, Any]) -> str:
    """The response envelope: ``status`` first, then the legacy fields.

    ``agent_id`` is omitted for the personal scope rather than sent as ``null``:
    the personal domain is not an Agent's, and echoing an empty target may read
    as "the caller's default Agent", which is a scope this surface does not have.
    """
    body: Dict[str, Any] = {"status": "success", "scope": target.scope}
    if target.agent_id:
        body["agent_id"] = target.agent_id
    body.update(payload)
    return json.dumps(body, ensure_ascii=False)


def _agent_content(ctx, target: MemoryTarget) -> Dict[str, Any]:
    from agent.memory.service import MemoryService

    root = _agent_root(ctx, target.agent_id)
    relative = _entry_relative(target.category, target.entry)
    if not _assert_plain_entry(root, relative):
        raise MemoryScopeError(CODE_UNKNOWN_ENTRY, "记忆条目不存在", status=404)

    service = MemoryService(root)
    from agent.memory.personal import _revision_of
    try:
        payload = service.get_content(target.entry, category=target.category)
    except FileNotFoundError as error:
        raise MemoryScopeError(CODE_UNKNOWN_ENTRY, "记忆条目不存在",
                               status=404) from error
    except ValueError as error:
        raise MemoryScopeError(CODE_INVALID_ENTRY, str(error), status=400) from error
    # The same two facts the list rows carry, for the same reasons: a client that
    # opened the entry directly (deep link, refresh) still needs the revision to
    # save it and must not be offered an edit the write would refuse.
    payload["revision"] = _revision_of(payload.get("content") or "")
    payload["actions"] = _entry_actions(ctx, target)
    payload["read_only"] = target.category != CATEGORY_MEMORY
    return payload


def _entry_relative(category: str, filename: str) -> str:
    """Where ``MemoryService._resolve_path`` will look for ``filename``.

    Kept in step with that method on purpose: the guard has to check the *same*
    path the delegate is about to read, or it guards nothing.
    """
    if filename == MAIN_ENTRY:
        return MAIN_ENTRY
    if category == CATEGORY_DREAM:
        return "memory/dreams/%s" % filename
    if category == CATEGORY_EVOLUTION:
        return "memory/evolution/%s" % filename
    return "memory/%s" % filename


# --- path guards ------------------------------------------------------------

def _assert_plain_dir(root: str, relative: str) -> None:
    """Refuse a directory whose path is (or crosses) a symlink.

    ``MemoryService`` enumerates with ``os.listdir``/``os.path.isfile``, which
    follow links: a ``memory`` symlink inside the tenant shared root would make
    the shared scope list — and, through ``get_content``, serve — another
    member's personal directory. Absence is not an error (a fresh scope has no
    ``memory/`` yet); a link is.
    """
    try:
        safe_fs.resolve_within(root, relative)
    except safe_fs.UnsafePathError as error:
        raise _unsafe(error) from error
    except (FileNotFoundError, NotADirectoryError, OSError):
        return


def _assert_plain_entry(root: str, relative: str) -> bool:
    """``False`` when the entry is absent; refuse a symlinked entry or parent."""
    try:
        if safe_fs.is_symlink(root, relative):
            raise _unsafe("symbolic link entry %r" % relative)
        return safe_fs.is_file(root, relative)
    except safe_fs.UnsafePathError as error:
        raise _unsafe(error) from error


def _is_symlink(root: str, relative: str) -> bool:
    try:
        return safe_fs.is_symlink(root, relative)
    except safe_fs.UnsafePathError as error:
        raise _unsafe(error) from error


def _unsafe(error) -> MemoryScopeError:
    return MemoryScopeError(CODE_UNSAFE_PATH, "记忆路径被替换，已拒绝访问", status=403)


# --- the personal service, bound to the verified context ---------------------

def _personal_service(ctx):
    """The *delivered* personal memory service for this request's identity.

    Same binding the ``/api/memory/personal`` handlers use: ownership comes
    from ``ctx``, so there is no parameter a caller could set to name another
    user.
    """
    return _web_channel()._personal_memory_service(ctx)


def refusal_for(exc: BaseException) -> MemoryScopeError:
    """Translate a delegate failure into a :class:`MemoryScopeError`."""
    from agent.memory.personal import PersonalMemoryError
    if isinstance(exc, PersonalMemoryError):
        return MemoryScopeError(exc.code, str(exc), status=exc.status)
    return unavailable(exc)
