# encoding:utf-8
"""MCP tools discovered from an *external connection*, re-authorized per call.

Why this module exists
----------------------
An ``mcp.json`` server and an external MCP connection must both give the model
something it can call, but they are authorized in different places. ``mcp.json``
is a workspace file, so a tool found there is trusted as configuration. An
external connection is a control-plane row with a tenant, a version, a
credential and a grant, so **是"发现到的工具"不是权限**（spec: 工具发现不等于调用
授权）: the discovered name is a *hint* about what exists, and every call
re-derives the connection, the actor and the deployment's open classes through
:class:`~integrations.external.runtime.ConnectionRuntime`.

Three consequences shape this module:

1. **One transport, one authorization path.** The tool subclass here refines
   only the *advertised* shape (the remote tool's real name, description and
   input schema). Dispatch is inherited from
   :class:`~agent.tools.external.external_tool.ExternalConnectionTool`, which
   routes through ``integrations.external.tools.dispatch`` →
   ``ConnectionRuntime``. There is no second client, no second grant check and
   no second approval gate for MCP.

2. **Listing is cheap and self-invalidating.** Discovery is a handshake plus
   ``tools/list`` — network I/O that must not happen on every turn. It is
   memoized per ``(tenant, connection)`` and *validated against the connection
   row itself* (version, enabled flag, the secret markers the runtime records),
   so there is no independent cache lifetime to go stale: creating, updating,
   disabling, deleting or re-credentialing a connection changes the very value
   the memo is keyed on, and the next listing re-discovers. A connection the
   caller can no longer see is dropped from the memo on that pass.

3. **A tool holds no authority.** It carries a binding and the connection id —
   never a credential and never a client. A call after the connection is gone
   (or after its grant was revoked) refuses inside the runtime, which is also
   where the write's risk, approval and quota are enforced.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from agent.tools.base_tool import MAX_TOOL_NAME, is_wire_safe_name
from agent.tools.external.external_tool import TOOL_PREFIX, ExternalConnectionTool
from common.log import logger

#: Separator between the parts of a composed tool name.
#:
#: A readability device, not a parseable delimiter: nothing decodes a name back
#: into its parts (``find_binding`` compares whole names, and the binding's
#: metadata carries the real remote tool name). It is ``_`` rather than the
#: ``.`` this used to be because the model providers reject any character
#: outside ``[A-Za-z0-9_-]`` — with a dot, **every** external tool made the
#: whole request fail (``Invalid 'tools[18].function.name'``), which is how this
#: was found. Unambiguity is therefore provided by :func:`_wire_part` plus
#: :func:`_remote_segment`, not by the delimiter.
TOOL_NAME_SEPARATOR = "_"

#: Bound on how many tools one connection may contribute. A server advertising
#: thousands of tools must not be able to blow up the model's tool list.
MAX_TOOLS_PER_CONNECTION = 200

#: Bound on a remote tool's own name, so a hostile server cannot push an
#: unbounded string into a prompt, a log line or a memo. The *composed* name is
#: additionally bounded by :data:`agent.tools.base_tool.MAX_TOOL_NAME`, which is
#: what a name past this bound runs into.
MAX_REMOTE_NAME = 128

#: Length of the digest appended to a remote segment that had to be rewritten.
#: 48 bits: two names sharing one is far less likely than the same connection
#: publishing two tools whose names differ only in characters we cannot carry.
REMOTE_DIGEST_LEN = 12

#: Fallback discovery bound when the deployment's pool limit cannot be read.
DEFAULT_DISCOVERY_TIMEOUT = 30.0

#: Metadata key the adapter's provider uses to hand a discovered tool's real
#: schema to the tool wrapper.
METADATA_REMOTE_TOOL = "remote_tool"
METADATA_INPUT_SCHEMA = "input_schema"
METADATA_CONNECTION_ID = "connection_id"


def _wire_part(value: Any) -> str:
    """One name segment, rewritten to the characters a provider accepts.

    The remote name is third-party input: ``tools.read``, ``weird name/x`` and
    non-ASCII are all names a real server may publish, and none of them can be
    carried on the wire as-is. Replacing rather than rejecting keeps the tool
    addressable; :func:`_remote_segment` is what keeps it *distinct*.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value or ""))


def _remote_segment(remote_name: str, budget: int) -> str:
    """The remote-tool segment of a name, within ``budget`` characters.

    Rewriting destroys information — ``tools.read`` and ``tools-read`` both
    become ``tools_read`` — so whenever anything had to be rewritten (or the
    name is too long), the segment is bound back to the original by appending a
    digest of the *whole* original name. Two remote tools therefore never share
    a wire name, and the mapping does not depend on the order the server
    happened to list them in, which matters because dispatch runs against the
    remote name the binding carries: a name that could resolve to the other
    tool would call the other tool.

    A name that needs no rewriting is returned verbatim, so the ordinary case
    reads as the server wrote it.
    """
    if budget <= 0:
        return ""
    safe = _wire_part(remote_name)
    if safe == remote_name and len(safe) <= budget:
        return safe
    digest = hashlib.sha1(
        remote_name.encode("utf-8", "replace")).hexdigest()[:REMOTE_DIGEST_LEN]
    if budget <= REMOTE_DIGEST_LEN:
        return digest[:budget]
    head = safe[:budget - REMOTE_DIGEST_LEN - 1].strip("_")
    return "%s%s%s" % (head, TOOL_NAME_SEPARATOR, digest) if head else digest


def tool_name(*, action: str, connection_id: str,
              remote_name: str = "") -> str:
    """The binding name of an MCP capability on one connection.

    ``mcp_<action>_<connection_id>`` for the connection-wide capability and
    ``mcp_<action>_<connection_id>_<remote tool>`` for one discovered tool, so
    the name answers "which connection" before it answers "which tool". Two
    connections can never collide on it.

    The result satisfies :func:`agent.tools.base_tool.is_wire_safe_name` **after**
    the model-visible prefix is added, which is why the budget is computed
    against that prefix rather than against the bare name — the length that
    matters is the one the provider sees. Returns ``""`` when no name within the
    contract can be composed; callers skip such a candidate rather than
    advertise a name that would fail the request.
    """
    base = "%s%s%s%s%s" % ("mcp", TOOL_NAME_SEPARATOR, _wire_part(action),
                           TOOL_NAME_SEPARATOR, _wire_part(connection_id))
    if not _wire_part(action) or not _wire_part(connection_id):
        # An unnamed action or connection would compose a name that reads like a
        # real binding (``mcp_tools_read_``) while resolving to nothing.
        return ""
    if not remote_name:
        return base if is_wire_safe_name(TOOL_PREFIX + base) else ""
    budget = (MAX_TOOL_NAME - len(TOOL_PREFIX) - len(base)
              - len(TOOL_NAME_SEPARATOR))
    segment = _remote_segment(remote_name, budget)
    if not segment:
        return ""
    composed = "%s%s%s" % (base, TOOL_NAME_SEPARATOR, segment)
    return composed if is_wire_safe_name(TOOL_PREFIX + composed) else ""


def _connection_nameable(connection_id: str) -> bool:
    """Whether a connection id may take part in a composed tool name."""
    return bool(tool_name(action="tools.read", connection_id=connection_id))


# --------------------------------------------------------------------------- #
# The tool: an external connection tool whose shape is the remote tool's
# --------------------------------------------------------------------------- #

class ExternalMcpTool(ExternalConnectionTool):
    """One discovered MCP tool, named and shaped by its own server.

    It **is** an :class:`ExternalConnectionTool`: the agent's per-turn
    reconciliation recognizes it, and dispatch, authorization, refusal rendering
    and the ``outcome_unknown`` handling are all inherited rather than
    re-implemented. What it refines is what the model reads:

    * the name carries the connection, so the model cannot call "the same" tool
      on a different connection by accident;
    * the input schema is the remote ``inputSchema`` when discovery returned
      one, instead of the generic permissive object. A model that cannot see
      the arguments invents them, and the invented call is the one that reaches
      someone's production system.
    """

    #: Sourced from a discovered connection rather than from a static
    #: declaration. Used by the manager's reconciliation to keep mcp.json tools
    #: and connection tools in separate buckets.
    external_connection = True

    def __init__(self, binding) -> None:
        super().__init__(binding)
        metadata = dict(getattr(binding.tool, "metadata", None) or {})
        self.connection_id = str(binding.connection_id)
        self.connection_name = str(binding.connection_name or self.connection_id)
        self.remote_name = str(metadata.get(METADATA_REMOTE_TOOL) or "")
        schema = metadata.get(METADATA_INPUT_SCHEMA)
        if isinstance(schema, Mapping):
            self.params = dict(schema)
        # The remote description is carried in the binding's tool description
        # already; make the target explicit so a refusal can be attributed.
        self.description = "%s [连接 %s]" % (
            self.description, self.connection_name)


def upgrade_external_mcp_tools(candidates: Mapping[str, Any]) -> Dict[str, Any]:
    """Rebuild MCP candidates as :class:`ExternalMcpTool`, in place by value.

    ``integrations.external.tools`` builds every binding into the base
    ``ExternalConnectionTool``; a discovered MCP tool advertises a richer,
    server-provided schema, so the agent-side wrapper is upgraded here — in the
    one place that already owns the actor's external tool set. A candidate that
    is not MCP, or one this build cannot upgrade, is passed through untouched:
    an upgraded wrapper is a convenience, never a gate.
    """
    out: Dict[str, Any] = {}
    for name, tool in dict(candidates or {}).items():
        binding = getattr(tool, "binding", None)
        kind = str(getattr(getattr(binding, "tool", None), "kind", "") or "")
        if kind != "mcp" or isinstance(tool, ExternalMcpTool):
            out[name] = tool
            continue
        try:
            upgraded = ExternalMcpTool(binding)
        except Exception as error:  # noqa: BLE001 - keep the working base tool
            logger.warning(
                "[ExternalMcpTool] %s kept as the generic external tool: %s",
                name, error)
            out[name] = tool
            continue
        out[upgraded.name] = upgraded
    return out


# --------------------------------------------------------------------------- #
# Discovery: bounded, redacted, cached against the connection row
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DiscoveryResult:
    """One connection's discovered tools, or the staged reason it failed."""

    tools: Tuple[Mapping[str, Any], ...] = ()
    code: str = ""
    stage: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.code


@dataclass
class _Entry:
    """The memo for one ``(tenant, connection)`` pair.

    Every field is *derived from the connection row*, so a change to the row is
    itself the invalidation: nothing here has to be told that the configuration
    moved.
    """

    version: int = 0
    enabled: bool = True
    secret_marker: Tuple[Tuple[str, int], ...] = ()
    tools: Tuple[Mapping[str, Any], ...] = ()
    code: str = ""
    stage: str = ""
    detail: str = ""
    discovered_at: float = 0.0
    pending: bool = False


_MEMO: Dict[Tuple[str, str], _Entry] = {}
_MEMO_LOCK = threading.RLock()


def _memo_key(tenant_id: str, connection_id: str) -> Tuple[str, str]:
    return (str(tenant_id or ""), str(connection_id or ""))


def _pool_timeout() -> float:
    try:
        from integrations.external.adapters import pool_for_deployment
        return float(pool_for_deployment().limits.timeout)
    except Exception:  # noqa: BLE001 - an unreadable limit uses the default
        return DEFAULT_DISCOVERY_TIMEOUT


def _isolation_accepted() -> bool:
    """Whether this deployment accepted the local-process isolation story.

    Read from the resolved pool limits — the same switch the runtime gates a
    stdio probe on — so a discovery handshake and an administrator's Test
    cannot disagree about whether spawning a local program is permitted.
    """
    try:
        from integrations.external.adapters import pool_for_deployment
        return bool(pool_for_deployment().limits.uninterruptible_ok)
    except Exception:  # noqa: BLE001 - an unreadable switch is closed
        return False


def _secret_marker(service, connection_id: str
                   ) -> Tuple[Tuple[str, int], ...]:
    """The connection's current secret markers, as the runtime records them.

    Used only as a cache key: rotating a credential changes the marker, which
    drops the memo, which re-discovers. The values are already hashed by the
    runtime, so this holds nothing sensitive.
    """
    try:
        versions = service.runtime().secret_versions(connection_id)
    except Exception:  # noqa: BLE001 - an unreadable marker only costs a refresh
        return ()
    return tuple(sorted((str(k), int(v)) for k, v in dict(versions).items()))


def discover_connection_tools(service, row: Mapping[str, Any], *,
                              tenant_id: str,
                              actor_user_id: str = "",
                              timeout: Optional[float] = None
                              ) -> DiscoveryResult:
    """Handshake + ``tools/list`` for one MCP connection.

    Runs on a background thread: it performs network (or local-process) I/O and
    must never be reached from the path that assembles an agent's tools. The
    result is redacted — a failure reports a code, a stage and a sanitized
    detail, never a remote body and never a credential.
    """
    from integrations.external.adapters.mcp import (_error_stage, _redact_remote,
                                                    build_client)

    connection_id = str(row["id"])
    bound = float(timeout if timeout is not None else _pool_timeout())
    client = None
    cancel = threading.Event()
    try:
        runtime = service.runtime()
        snapshot = runtime.snapshot(connection_id, tenant_id=tenant_id)
        if snapshot.kind != "mcp":
            return DiscoveryResult(code="wrong_kind", stage="config",
                                   detail="the connection is not an MCP one")
        if not snapshot.enabled:
            return DiscoveryResult(code="connection_disabled", stage="config",
                                   detail="the connection is disabled")
        ctx = runtime.build_context(
            snapshot, actor_user_id=actor_user_id, draft=True,
            limits={"test_timeout": bound})
        ctx.cancel = cancel
        ctx.deadline = time.monotonic() + bound
        try:
            client = build_client(ctx)
            client.initialize_strict()
            tools = client.list_tools_strict()
        finally:
            # Drop the bound before shutting down: the shutdown is a local
            # cleanup step and must not be cut off by the discovery deadline.
            ctx.deadline = None
            cancel.set()
    except BaseException as exc:  # noqa: BLE001 - one connection's failure
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        code, stage = _error_stage(exc)
        return DiscoveryResult(code=code, stage=stage,
                               detail=_redact_remote(exc))
    finally:
        if client is not None:
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001 - cleanup never masks the result
                pass

    cleaned: List[Mapping[str, Any]] = []
    for tool in tools or ():
        if not isinstance(tool, Mapping):
            continue
        name = str(tool.get("name") or "").strip()
        if not name or len(name) > MAX_REMOTE_NAME:
            continue
        schema = tool.get("inputSchema")
        cleaned.append({
            "name": name,
            "description": str(tool.get("description") or "")[:4000],
            "inputSchema": dict(schema) if isinstance(schema, Mapping)
            else {"type": "object", "properties": {}},
        })
        if len(cleaned) >= MAX_TOOLS_PER_CONNECTION:
            break
    return DiscoveryResult(tools=tuple(cleaned))


def _store_result(key: Tuple[str, str], entry: _Entry,
                  result: DiscoveryResult) -> None:
    with _MEMO_LOCK:
        current = _MEMO.get(key)
        if current is not None and current is not entry:
            # A newer pass already replaced this entry (the connection was
            # edited while discovery ran). Keeping the newer one matters: the
            # older result was produced from a configuration that no longer
            # exists.
            return
        entry.tools = result.tools
        entry.code = result.code
        entry.stage = result.stage
        entry.detail = result.detail
        entry.discovered_at = time.monotonic()
        entry.pending = False


def _discover_in_background(*, tenant_id: str, connection_id: str,
                            row: Mapping[str, Any], entry: _Entry,
                            actor_user_id: str, timeout: float) -> None:
    """Start one discovery, deduplicated by the memo entry's ``pending`` flag."""

    def _run() -> None:
        try:
            service = _service()
        except Exception as exc:  # noqa: BLE001 - an unreadable store is a result
            _store_result(_memo_key(tenant_id, connection_id), entry,
                          DiscoveryResult(code="store_unavailable",
                                          stage="internal", detail=str(exc)[:200]))
            return
        try:
            result = discover_connection_tools(
                service, row, tenant_id=tenant_id,
                actor_user_id=actor_user_id, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - never raise into the thread
            result = DiscoveryResult(code="discovery_failed", stage="internal",
                                     detail=str(exc)[:200])
        if result.code:
            logger.info(
                "[ExternalMcpTool] discovery of %s failed (%s/%s)",
                connection_id, result.stage, result.code)
        _store_result(_memo_key(tenant_id, connection_id), entry, result)

    threading.Thread(target=_run, daemon=True,
                     name="mcp-conn-discovery").start()


def _service():
    from integrations.external.service import get_external_connection_service
    return get_external_connection_service()


#: How long a *failed* or empty discovery is left alone before another attempt.
#: This is a failure backoff, not a cache lifetime: a discovery that succeeded
#: is invalidated only by the connection row itself (version, enabled flag or
#: credential markers), so nothing here can outlive the configuration it
#: describes.
DISCOVERY_RETRY_SECONDS = 60.0


def refresh_connection(*, tenant_id: str, row: Mapping[str, Any],
                       actor_user_id: str = "",
                       timeout: Optional[float] = None,
                       background: bool = True) -> Optional[_Entry]:
    """Bring the memo for one connection up to date with its row.

    Cheap in the steady state: a comparison of the row's version, its enabled
    flag and its credential markers. A new, edited, re-credentialed or
    previously failed connection starts one bounded discovery. Returns the
    current entry when its tools are usable, else ``None``.
    """
    connection_id = str(row["id"])
    key = _memo_key(tenant_id, connection_id)
    version = int(row["version"] or 0)
    enabled = bool(row["enabled"])
    if not _connection_nameable(connection_id):
        # A connection id that could make a composed name ambiguous (or an
        # empty one) contributes nothing: a name that might resolve to another
        # connection is worse than a missing tool.
        return None
    try:
        marker = _secret_marker(_service(), connection_id)
    except Exception:  # noqa: BLE001 - an unreadable marker only costs a refresh
        marker = ()
    stamp = time.monotonic()
    with _MEMO_LOCK:
        entry = _MEMO.get(key)
        same_config = (entry is not None and entry.version == version
                       and entry.enabled == enabled
                       and entry.secret_marker == marker)
        if same_config:
            if entry.tools:
                return entry
            if entry.pending:
                return None
            if (entry.discovered_at
                    and stamp - entry.discovered_at < DISCOVERY_RETRY_SECONDS):
                return None
            entry.pending = True
            target = entry
        else:
            target = _Entry(version=version, enabled=enabled,
                            secret_marker=marker)
            _MEMO[key] = target
            if background:
                target.pending = True
    if not background:
        return None
    _discover_in_background(
        tenant_id=tenant_id, connection_id=connection_id, row=row,
        entry=target, actor_user_id=actor_user_id,
        timeout=float(timeout if timeout is not None else _pool_timeout()))
    return None


def remote_tools_for(*, tenant_id: str, row: Mapping[str, Any],
                     actor_user_id: str = "") -> Tuple[Mapping[str, Any], ...]:
    """The discovered tool list for this connection version, or ``()``.

    ``()`` means "not known right now": discovery is in flight, failed, or the
    connection changed under it. It is deliberately not an error — the
    connection-wide capability bindings still exist, so the connection remains
    usable while its tool catalogue is being (re)read.
    """
    entry = refresh_connection(tenant_id=tenant_id, row=row,
                               actor_user_id=actor_user_id)
    return tuple(entry.tools) if entry is not None else ()


def remembered_tool_names(*, tenant_id: str, connection_id: str,
                          version: int) -> Optional[FrozenSet[str]]:
    """The remote names memoized for exactly this connection version.

    Read-only, and deliberately not a discovery: the caller is asking "what did
    this connection publish, as of the version I am acting on". A connection
    whose row moved on has a memo that no longer describes it, and a connection
    never discovered has none at all — both answer ``None``, which is "not
    known right now" rather than "published nothing".

    Callers that authorize *by* this answer must refuse on ``None``: the whole
    point of asking is that the set of callable names is the server's actual
    catalogue, and an unknown catalogue is not an empty one that may be widened.
    """
    key = _memo_key(tenant_id, connection_id)
    with _MEMO_LOCK:
        entry = _MEMO.get(key)
        if entry is None or not entry.enabled:
            return None
        if int(entry.version) != int(version):
            return None
        return frozenset(
            str(tool.get("name") or "") for tool in entry.tools
            if str(tool.get("name") or ""))


def remember_tools(*, tenant_id: str, connection_id: str, version: int,
                   tools: Sequence[Mapping[str, Any]],
                   enabled: bool = True,
                   secret_marker: Optional[Sequence[Tuple[str, int]]] = None,
                   ) -> None:
    """Record a discovery result as the memo for this connection version.

    The discovery worker uses it for its own result; a warm-up pass and the
    tests use it to describe "this connection was discovered, and here is what
    it published" without performing I/O. The stored markers are the ones the
    runtime would report, so the entry matches exactly the configuration it is
    claimed to describe and is dropped by the very next comparison if the
    connection moves.
    """
    marker = tuple(secret_marker) if secret_marker is not None else None
    if marker is None:
        try:
            marker = _secret_marker(_service(), connection_id)
        except Exception:  # noqa: BLE001
            marker = ()
    with _MEMO_LOCK:
        _MEMO[_memo_key(tenant_id, connection_id)] = _Entry(
            version=int(version), enabled=bool(enabled), secret_marker=marker,
            tools=tuple(dict(tool) for tool in tools),
            discovered_at=time.monotonic(), pending=False)


def forget(*, tenant_id: str, connection_id: str) -> None:
    """Drop one connection's memo — called when the connection goes away."""
    with _MEMO_LOCK:
        _MEMO.pop(_memo_key(tenant_id, connection_id), None)


def forget_tenant(tenant_id: str) -> None:
    """Drop every memo entry a tenant owns."""
    tenant = str(tenant_id or "")
    with _MEMO_LOCK:
        for key in [k for k in _MEMO if k[0] == tenant]:
            _MEMO.pop(key, None)


def reconcile_memo(*, tenant_id: str,
                   rows: Sequence[Mapping[str, Any]]) -> None:
    """Drop memo entries for connections this tenant no longer has enabled.

    The listing already leaves them out of the tool set; dropping the memo as
    well means a re-created connection cannot inherit a deleted one's tool
    catalogue, and a disabled connection's names stop being remembered at all.
    """
    tenant = str(tenant_id or "")
    keep = {str(row["id"]) for row in rows}
    with _MEMO_LOCK:
        for key in [k for k in _MEMO if k[0] == tenant and k[1] not in keep]:
            _MEMO.pop(key, None)


def refresh_tenant_tools(*, tenant_id: str, actor_user_id: str = "") -> int:
    """Reconcile discovery with the tenant's live MCP connections.

    The MCP counterpart of ``ToolManager.refresh_mcp_if_changed``: an mcp.json
    edit is detected by hashing the file, an external connection's edit by the
    version and credential markers on its own row. Called per turn; a memo that
    already matches its row costs one small read per connection and nothing
    else. Returns the number of connections considered.
    """
    from integrations.external.adapters.mcp import (discovered_tools_offered,
                                                    list_mcp_connections)

    tenant = str(tenant_id or "").strip()
    if not tenant:
        return 0
    rows = list_mcp_connections(tenant, enabled_only=True)
    if not discovered_tools_offered(rows):
        # Nothing in this deployment would offer a discovered tool, so a
        # handshake would be I/O spent on a name the runtime always refuses.
        # The rows are consulted so "this deployment declared nothing" costs no
        # handshake at all, rather than one per connection.
        forget_tenant(tenant)
        return 0
    reconcile_memo(tenant_id=tenant, rows=rows)
    for row in rows:
        refresh_connection(tenant_id=tenant, row=row,
                           actor_user_id=actor_user_id)
    return len(rows)


def _reset_for_tests() -> None:
    """Drop all memo state. For tests only."""
    with _MEMO_LOCK:
        _MEMO.clear()
