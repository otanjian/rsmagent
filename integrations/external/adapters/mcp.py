# encoding:utf-8
"""The MCP connection adapter: configure, test, discover, invoke.

What this adapter owns, and what it deliberately does not
--------------------------------------------------------
It owns *how to talk to an MCP server*: the three transports, the handshake,
tool discovery, a tool call and a resource read, each bounded by the
deployment's network policy, deadline and cancellation flag. It does **not**
own authorization, the secret store, the result persistence or the audit trail —
:class:`~integrations.external.runtime.ConnectionRuntime` builds the context,
runs the probe through the pool and binds the stored summary, and
``ConnectionRuntime.invoke`` applies the open classes, the risk catalogue and the
approval binding before this module is reached.

Two invariants worth stating because they are easy to get wrong:

* **There is one transport implementation.** The protocol lives in
  :mod:`agent.tools.mcp.mcp_client` (the same client an ``mcp.json`` server
  uses). This module only translates a connection's configuration into that
  client and translates its staged failures back into probe stages. A second
  hand-rolled HTTP/JSON-RPC path would be a second place for the policy,
  redirect and redaction rules to drift.
* **Probe is handshake + discovery only** ("MCP 仅握手与发现"): a test must not
  call a business tool. ``invoke`` is the only place a tool actually runs, and
  it refuses anything the adapter did not declare.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from common.log import logger

from integrations.external import registry
from integrations.external.adapters.base import (
    AdapterError,
    CapabilityReport,
    ConnectionAdapter,
    ExecutionContext,
    InvokeResult,
    PolicyRefused,
    ProbeResult,
    STAGE_AUTH,
    STAGE_CONFIG,
    STAGE_POLICY,
    STAGE_PROTOCOL,
    STAGE_TIMEOUT,
    StageResult,
    invoke_failed,
    invoke_ok,
    invoke_unknown,
    register_adapter,
    stage_failed,
    stage_ok,
)
from integrations.external.adapters.netpolicy import NetworkPolicy, current_policy
from integrations.external.errors import invalid

# -- action vocabulary -------------------------------------------------------
#
# ``tools.call`` covers "call an MCP tool". The remote server's own
# ``readOnlyHint`` is data, not authority, so this action is a *write* — the
# risk catalogue classifies it high and requires an approval. ``tools.list``
# and ``resources.read`` are reads against the server.
#
# ``tools.read`` is the read counterpart of ``tools.call``: it calls a tool the
# remote server published, and it exists because the two need different answers.
# Folding them into one action would have to vary the risk level and the open
# class by the tool being called, which turns the stable ``(kind, action)`` key
# that authorization, audit, approval and quota are all built on into
# ``(kind, action, arguments)``.
#
# Everything a connection publishes is offered through ``tools.read``. There is
# no per-tool declaration on the connection: the remote catalogue is itself the
# answer to "what may be called", and a hand-maintained list next to it would be
# a second copy of that answer, written by someone who cannot see the first.
ACTION_TOOLS_LIST = "tools.list"
ACTION_TOOLS_READ = "tools.read"
ACTION_TOOLS_CALL = "tools.call"
ACTION_RESOURCES_READ = "resources.read"

ACTIONS: FrozenSet[str] = frozenset(
    {ACTION_TOOLS_LIST, ACTION_TOOLS_READ, ACTION_TOOLS_CALL,
     ACTION_RESOURCES_READ})
WRITE_ACTIONS: FrozenSet[str] = frozenset({ACTION_TOOLS_CALL})

#: Our transport names (registry's spelling) -> the MCP client's ``type``.
TRANSPORT_STDIO = "stdio"
TRANSPORT_SSE = "sse"
TRANSPORT_STREAMABLE_HTTP = "streamable_http"

_CLIENT_TRANSPORT: Dict[str, str] = {
    TRANSPORT_STDIO: "stdio",
    TRANSPORT_SSE: "sse",
    TRANSPORT_STREAMABLE_HTTP: "streamable-http",
}

#: Headers the transport itself manages. Letting a connection declare one
#: would let it break the session or the framing, so they are refused offline.
_RESERVED_HEADERS: FrozenSet[str] = frozenset({
    "host", "content-length", "content-type", "accept", "mcp-session-id",
    "connection", "transfer-encoding",
})

#: Bounds the console cannot express as a per-field rule.
MAX_ARGS = 64
MAX_ARG_LENGTH = 512
MAX_ENV_KEYS = 64
MAX_KEY_LENGTH = 256
MAX_TOOLS_IN_METADATA = 50
MAX_TOOL_NAME_LENGTH = 200

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


# -- helpers -----------------------------------------------------------------

def _config_of(ctx_or_config: Any) -> Mapping[str, Any]:
    if isinstance(ctx_or_config, ExecutionContext):
        return ctx_or_config.config or {}
    return ctx_or_config or {}


def _transport(config: Mapping[str, Any]) -> str:
    return str((config or {}).get("transport") or "").strip().lower()


def _auth(config: Mapping[str, Any]) -> str:
    return str((config or {}).get("auth") or "none").strip().lower()


def _remote_call(params: Mapping[str, Any], *,
                 action: str) -> Tuple[str, Dict[str, Any],
                                      Optional[InvokeResult]]:
    """Parse the ``{tool, arguments}`` shape both tool-call actions take.

    Returns ``(name, arguments, failure)`` with exactly one of the last two
    meaningful: the adapter answers a malformed request with a result rather
    than an exception (the invoke path's house style), so the failure comes back
    as the third element instead of being raised.

    The remote tool name may also arrive under ``name``: the connection-level
    form predates the reviewed one and named callers exist, so both spellings
    keep working.
    """
    name = str((params or {}).get("tool") or (params or {}).get("name")
               or "").strip()
    if not name or len(name) > MAX_TOOL_NAME_LENGTH:
        return "", {}, invoke_failed(
            "field_required", stage=STAGE_CONFIG,
            message="%s needs the remote tool name" % action)
    raw_arguments = (params or {}).get("arguments")
    if raw_arguments is None:
        return name, {}, None
    if isinstance(raw_arguments, Mapping):
        return name, dict(raw_arguments), None
    return "", {}, invoke_failed(
        "field_invalid", stage=STAGE_CONFIG,
        message="%s arguments must be an object" % action)


def _policy_of(ctx: ExecutionContext) -> NetworkPolicy:
    policy = ctx.limits.get("policy") if isinstance(ctx.limits, Mapping) else None
    return policy if isinstance(policy, NetworkPolicy) else current_policy()


def _redact_remote(value: Any, secrets: Optional[List[str]] = None,
                   limit: int = 200) -> str:
    """A bounded, single-line detail with no secret and no raw remote body.

    ``McpTransportError`` already carries a redacted message, but a probe must
    not rely on that: a detail is re-redacted here against the secrets this
    attempt actually resolved, so a server that echoes a bearer token in an
    error string still cannot put it in the stored test summary.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    for secret in (secrets or []):
        if secret:
            text = text.replace(secret, "***")
    # Long opaque tokens survive the exact-value pass; mask their shape too.
    text = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", "<redacted>", text)
    return text[:limit].rstrip()


def _result(ctx: ExecutionContext, stages: List[StageResult], *,
            metadata: Optional[Mapping[str, Any]] = None,
            cancelled: bool = False) -> ProbeResult:
    return ProbeResult(
        stages=tuple(stages), metadata=dict(metadata or {}),
        cancelled=cancelled, config_version=ctx.config_version,
        secret_versions=dict(ctx.secret_versions))


def _isolation_accepted(ctx: ExecutionContext) -> bool:
    """Whether this deployment accepted the local-process isolation story.

    Read from the resolved pool limits, and from the pool itself when the
    context did not carry them (a direct adapter call in a test). stdio spawns a
    local program, which the design gates on a second, independent acceptance
    ("stdio 另有 execution-isolation"); the default is closed.
    """
    limits = ctx.limits if isinstance(ctx.limits, Mapping) else {}
    if "uninterruptible_ok" in limits:
        return bool(limits.get("uninterruptible_ok"))
    from integrations.external.adapters import pool as pool_module
    return bool(pool_module.pool_for_deployment().limits.uninterruptible_ok)


def _stdio_command_allowlist() -> FrozenSet[str]:
    """The deployment's allowed stdio executables (empty means unrestricted).

    The same key :meth:`McpClient._command_allowed` reads, so the offline check
    here and the runtime gate cannot disagree.
    """
    try:
        from config import conf
        raw = (conf() or {}).get("mcp_stdio_command_allowlist") or []
    except Exception:  # noqa: BLE001 - unreadable config is "no extra rule"
        return frozenset()
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    out = set()
    for item in raw:
        name = os.path.basename(str(item).strip()).lower()
        if name.endswith(".exe"):
            name = name[:-4]
        if name:
            out.add(name)
    return frozenset(out)


def _command_basename(command: str) -> str:
    name = os.path.basename(str(command or "").strip()).lower()
    return name[:-4] if name.endswith(".exe") else name


def _stdio_env_secret(ctx: ExecutionContext) -> str:
    """The ``env`` secret slot for a stdio attempt, resolved before spawning.

    Only read when the configuration *declares* ``env_keys``: a stdio server
    with no declared environment variables needs no secret, and demanding one
    would turn "this server takes no configuration" into a configuration error.

    Returns the raw value (also used for redaction); an empty/missing value is
    refused rather than passed on. The refusal uses the same code and stage the
    client raises at spawn, so the console shows one wording whether the check
    happened here or there.
    """
    declared = list((ctx.config or {}).get("env_keys") or [])
    if not declared:
        return ""
    try:
        raw = ctx.secret("env")
    except AdapterError:
        raise
    if not raw:
        raise AdapterError(
            "the connection declares env_keys but no env secret is stored",
            code="secret_unavailable", stage=STAGE_CONFIG)
    return raw if isinstance(raw, str) else json.dumps(raw)


def _close_client(client: Any) -> None:
    """Release a probe's client without letting cleanup become the result.

    A probe is a bounded attempt, so the transport it opened has to be given
    back on every path. A failure *to* release is logged, not reported: it says
    nothing about whether the target is reachable, and turning it into a failed
    stage would hide a successful handshake behind a teardown problem.
    """
    if client is None:
        return
    try:
        client.shutdown()
    except Exception:  # noqa: BLE001 - teardown must not mask the probe
        logger.debug("failed to release a probe client", exc_info=True)


# -- building a policy-bound client -----------------------------------------

def build_client(ctx: ExecutionContext, *, name: str = ""):
    """An :class:`McpClient` bound to this attempt's context.

    The returned client carries ``_external_ctx``, so every request it makes is
    re-checked against the deployment policy, honours the attempt's deadline and
    fails on cancellation. It never starts an interactive OAuth flow and never
    resends a message: both would be unbound side effects of a bounded attempt.
    """
    from agent.tools.mcp.mcp_client import McpClient

    config = dict(ctx.config or {})
    transport = _transport(config)
    client_type = _CLIENT_TRANSPORT.get(transport)
    if client_type is None:
        raise AdapterError("unsupported MCP transport %r" % transport,
                           code="unsupported_transport", stage=STAGE_CONFIG)

    client_config: Dict[str, Any] = {
        "name": name or ("external:" + ctx.connection_id),
        "type": client_type,
        "_external_ctx": ctx,
    }

    timeout = 30
    if isinstance(ctx.limits, Mapping):
        try:
            timeout = int(float(ctx.limits.get("test_timeout") or 30))
        except (TypeError, ValueError):
            timeout = 30
    client_config["timeout"] = max(1, timeout)

    if transport == TRANSPORT_STDIO:
        client_config["command"] = str(config.get("command") or "")
        client_config["args"] = list(config.get("args") or [])
        # ``env_keys`` names the variables the child may receive; the values are
        # read from the ``env`` secret slot by the client itself, so no secret
        # value is ever copied into a config dict here.
        client_config["env_keys"] = list(config.get("env_keys") or [])
        return McpClient(client_config)

    client_config["url"] = str(config.get("url") or "")
    auth = _auth(config)
    if auth == "header":
        header_name = str(config.get("header_name") or "").strip()
        secret = ctx.secret("header")
        if secret:
            client_config.setdefault("headers", {})[header_name] = secret
    elif auth == "oauth":
        # The token record is keyed per connection, so a token minted for one
        # connection can never be presented to another. No token means no
        # Authorization header, and the server's 401 becomes an explicit
        # re-authorization requirement rather than a silent interactive flow.
        client_config["scope"] = str(config.get("oauth_provider") or "")
    return McpClient(client_config)


def _error_stage(exc: BaseException) -> Tuple[str, str]:
    """``(code, stage)`` for a transport/handshake/discovery failure."""
    if isinstance(exc, AdapterError):
        return exc.code or "adapter_error", exc.stage
    code = str(getattr(exc, "code", "") or "") or "protocol_error"
    stage = str(getattr(exc, "stage", "") or "") or STAGE_PROTOCOL
    return code, stage


# -- the adapter -------------------------------------------------------------

@register_adapter
class McpAdapter(ConnectionAdapter):
    """stdio / SSE / Streamable HTTP behind one action contract."""

    kind = registry.KIND_MCP
    actions = ACTIONS
    write_actions = WRITE_ACTIONS

    # -- offline ------------------------------------------------------------

    def validate_config(self, config: Mapping[str, Any]) -> Dict[str, Any]:
        """Checks the registry's partitioning cannot express, still offline.

        :func:`integrations.external.registry.validate_config` has already
        refused unknown keys, secret-shaped keys, the stdio/remote split and a
        bad transport or auth mode. What is left is what only the adapter knows:
        which executables this deployment permits, the shape and bounds of the
        header/args/env fields, and the one combination the client cannot serve
        (OAuth on a transport that has no token flow).
        """
        normalized = dict(config or {})
        transport = _transport(normalized)
        if transport == TRANSPORT_STDIO:
            return self._validate_stdio(normalized)
        if transport in (TRANSPORT_SSE, TRANSPORT_STREAMABLE_HTTP):
            return self._validate_remote(normalized, transport)
        raise invalid("transport must be stdio, sse or streamable_http",
                      code="field_invalid", fields={"transport": "invalid"})

    def _validate_stdio(self, config: Mapping[str, Any]) -> Dict[str, Any]:
        command = str(config.get("command") or "").strip()
        if not command:
            raise invalid("stdio requires a command", code="field_required",
                          fields={"command": "required"})
        if "\x00" in command or "\n" in command:
            raise invalid("command must be a single executable name",
                          code="field_invalid", fields={"command": "invalid"})
        allowlist = _stdio_command_allowlist()
        if allowlist and _command_basename(command) not in allowlist:
            # The offline half of the runtime gate: a connection with an
            # executable this deployment does not permit is refused at save
            # time, not discovered when someone presses Test.
            raise invalid(
                "command %r is not in this deployment's allowed executables"
                % command,
                code="command_not_allowed", fields={"command": "not_allowed"})
        args = config.get("args") or []
        if len(args) > MAX_ARGS:
            raise invalid("args has too many entries", code="field_invalid",
                          fields={"args": "too_many"})
        for arg in args:
            if len(str(arg)) > MAX_ARG_LENGTH:
                raise invalid("an argument is too long", code="field_invalid",
                              fields={"args": "too_long"})
        env_keys = list(config.get("env_keys") or [])
        if len(env_keys) > MAX_ENV_KEYS:
            raise invalid("env_keys has too many entries", code="field_invalid",
                          fields={"env_keys": "too_many"})
        seen = set()
        for key in env_keys:
            name = str(key)
            if not _ENV_NAME.match(name):
                raise invalid("env_keys must be environment variable names",
                              code="field_invalid",
                              fields={"env_keys": "invalid"})
            if name in seen:
                raise invalid("env_keys contains a duplicate name",
                              code="field_invalid",
                              fields={"env_keys": "duplicate"})
            seen.add(name)
        return dict(config)

    def _validate_remote(self, config: Mapping[str, Any],
                         transport: str) -> Dict[str, Any]:
        url = str(config.get("url") or "")
        if len(url) > 2048:
            raise invalid("url is too long", code="field_invalid",
                          fields={"url": "too_long"})
        if "#" in url:
            raise invalid("url must not carry a fragment", code="field_invalid",
                          fields={"url": "invalid"})
        auth = _auth(config)
        if auth == "oauth" and transport != TRANSPORT_STREAMABLE_HTTP:
            # The existing OAuth flow is the Streamable HTTP one; advertising it
            # for SSE would store a credential that is never presented.
            raise invalid("oauth authentication requires streamable_http",
                          code="field_invalid", fields={"auth": "not_allowed"})
        if auth == "header":
            header_name = str(config.get("header_name") or "").strip()
            if len(header_name) > MAX_KEY_LENGTH:
                raise invalid("header_name is too long", code="field_invalid",
                              fields={"header_name": "too_long"})
            if not _HEADER_NAME.match(header_name):
                raise invalid("header_name is not a valid HTTP header name",
                              code="field_invalid",
                              fields={"header_name": "invalid"})
            if header_name.lower() in _RESERVED_HEADERS:
                raise invalid("header_name is managed by the transport",
                              code="field_invalid",
                              fields={"header_name": "reserved"})
        return dict(config)

    # -- probe --------------------------------------------------------------

    def probe(self, ctx: ExecutionContext) -> ProbeResult:
        """Handshake + ``tools/list`` only. Never calls a business tool."""
        transport = _transport(ctx.config)
        if transport == TRANSPORT_STDIO:
            return self._probe_stdio(ctx)
        if transport in (TRANSPORT_SSE, TRANSPORT_STREAMABLE_HTTP):
            return self._probe_remote(ctx, transport)
        return _result(ctx, [stage_failed(
            "configuration", "unsupported_transport", stage=STAGE_CONFIG,
            detail="this connection has no supported MCP transport")])

    # -- probe: remote transports ------------------------------------------

    def _probe_remote(self, ctx: ExecutionContext,
                      transport: str) -> ProbeResult:
        config = ctx.config or {}
        auth = _auth(config)
        secrets: List[str] = []
        if auth == "header":
            try:
                secret = ctx.secret("header")
            except AdapterError as exc:
                return _result(ctx, [stage_failed(
                    "secret", exc.code or "secret_unavailable",
                    stage=exc.stage, detail=_redact_remote(exc, secrets))])
            if secret:
                secrets.append(secret)

        policy = _policy_of(ctx)
        url = str(config.get("url") or "")
        try:
            # The *offline* half of the policy check: is this endpoint permitted
            # by the deployment at all. Deliberately not ``policy.check()`` —
            # that resolves DNS, and resolution belongs at the connection, where
            # the address used and the address vetted are the same lookup (the
            # client enforces exactly that in ``_check_target``). Doing it here
            # too would both duplicate the check and make this stage depend on a
            # name resolver, which is not what it is asserting.
            policy.check_url_permission(url, secret_bearing=bool(secrets))
        except PolicyRefused as exc:
            return _result(ctx, [stage_failed(
                "target", "target_not_allowed", stage=STAGE_POLICY,
                detail=_redact_remote(exc, secrets))])
        stages: List[StageResult] = [stage_ok(
            "target", stage=STAGE_POLICY,
            detail="the endpoint is permitted by the deployment policy")]

        if auth == "oauth":
            # Not a refusal: a stored, connection-bound token may well be
            # present. The refusal (if any) arrives as a 401 and is reported as
            # a re-authorization requirement by the handshake stage.
            stages.append(stage_ok(
                "oauth", stage=STAGE_AUTH,
                detail="a connection-bound token is used when one is stored"))

        ctx.check_alive()
        client = None
        try:
            client = build_client(ctx)
        except AdapterError as exc:
            return _result(ctx, stages + [stage_failed(
                "configuration", exc.code or "adapter_error", stage=exc.stage,
                detail=_redact_remote(exc, secrets))])

        # Everything under this ``try`` releases the client on the way out,
        # including the early returns: a probe that opened a transport and did
        # not close it leaks a socket (or a subprocess) once per test, which is
        # how a "test connection" button takes a deployment down over a
        # morning.
        try:
            try:
                client.initialize_strict()
            except BaseException as exc:  # noqa: BLE001 - mapped, never leaked
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                code, stage = _error_stage(exc)
                return _result(ctx, stages + [stage_failed(
                    "handshake", code, stage=stage,
                    detail=_redact_remote(exc, secrets))])
            stages.append(stage_ok(
                "handshake", stage=STAGE_AUTH,
                detail="MCP initialize succeeded"))

            ctx.check_alive()
            try:
                tools = client.list_tools_strict()
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                code, stage = _error_stage(exc)
                return _result(ctx, stages + [stage_failed(
                    "discovery", code, stage=stage,
                    detail=_redact_remote(exc, secrets))])
            stages.append(stage_ok(
                "discovery", stage=STAGE_PROTOCOL,
                detail="%d tool(s) discovered" % len(tools)))

            return _result(ctx, stages, metadata=self._discovery_metadata(
                tools, transport=transport, auth=auth))
        finally:
            _close_client(client)

    # -- probe: stdio -------------------------------------------------------

    def _probe_stdio(self, ctx: ExecutionContext) -> ProbeResult:
        if not _isolation_accepted(ctx):
            # Reported before the process is spawned, with the reason: a
            # deployment that has not accepted the isolation story must not run
            # a local program, and must say so instead of showing a timeout.
            return _result(ctx, [stage_failed(
                "isolation", "stdio_requires_isolation",
                stage=STAGE_POLICY,
                detail="a stdio MCP server runs a local program and this "
                       "deployment has not accepted the process isolation "
                       "requirements for it")])

        config = ctx.config or {}
        command = str(config.get("command") or "")
        if not command:
            return _result(ctx, [stage_failed(
                "configuration", "field_required", stage=STAGE_CONFIG,
                detail="stdio requires a command")])

        allowlist = _stdio_command_allowlist()
        if allowlist and _command_basename(command) not in allowlist:
            return _result(ctx, [stage_failed(
                "policy", "command_not_allowed", stage=STAGE_POLICY,
                detail="the executable is not in this deployment's allowed "
                       "list")])

        secrets: List[str] = []
        try:
            # Resolved here, before anything is spawned: the child's ``env``
            # block comes from the ``env`` secret slot, and a probe that starts
            # a local program only to discover the secret is missing has
            # already run the program. The client repeats this check at spawn
            # (it must: it is the thing that builds the environment), so this is
            # the same refusal reported one step earlier, in the stage that can
            # still be called "configuration".
            env_secret = _stdio_env_secret(ctx)
        except AdapterError as exc:
            return _result(ctx, [stage_failed(
                "configuration", exc.code or "secret_unavailable",
                stage=exc.stage, detail=_redact_remote(exc))])
        if env_secret:
            secrets.append(env_secret)

        ctx.check_alive()
        client = None
        try:
            client = build_client(ctx)
        except AdapterError as exc:
            return _result(ctx, [stage_failed(
                "configuration", exc.code or "adapter_error", stage=exc.stage,
                detail=_redact_remote(exc))])

        started = [stage_ok(
            "isolation", stage=STAGE_POLICY,
            detail="this deployment accepts the local-process isolation story")]
        # The subprocess is released on every path, including the early
        # returns: a probe that leaves ``npx`` running leaks one child process
        # per click until the host runs out of them.
        try:
            try:
                client.initialize_strict()
            except BaseException as exc:  # noqa: BLE001 - mapped, never leaked
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                code, stage = _error_stage(exc)
                return _result(ctx, started + [stage_failed(
                    "handshake", code, stage=stage,
                    detail=_redact_remote(exc, secrets))],
                    metadata={"transport": TRANSPORT_STDIO})
            started.append(stage_ok(
                "handshake", stage=STAGE_PROTOCOL,
                detail="MCP initialize succeeded"))

            ctx.check_alive()
            try:
                tools = client.list_tools_strict()
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                code, stage = _error_stage(exc)
                return _result(ctx, started + [stage_failed(
                    "discovery", code, stage=stage,
                    detail=_redact_remote(exc, secrets))],
                    metadata={"transport": TRANSPORT_STDIO})
            started.append(stage_ok(
                "discovery", stage=STAGE_PROTOCOL,
                detail="%d tool(s) discovered" % len(tools)))
            return _result(ctx, started, metadata=self._discovery_metadata(
                tools, transport=TRANSPORT_STDIO, auth="none"))
        finally:
            _close_client(client)

    @staticmethod
    def _discovery_metadata(tools: List[Mapping[str, Any]], *,
                            transport: str, auth: str) -> Dict[str, Any]:
        """A bounded, non-secret summary of what discovery found.

        Names only (no schemas: a schema can be large and is not a test
        result), capped, and never a secret. The stable identity of each tool is
        derived from the connection at dispatch time, not stored here.
        """
        names: List[str] = []
        for tool in tools:
            name = str((tool or {}).get("name") or "")
            if not name:
                continue
            names.append(name[:MAX_TOOL_NAME_LENGTH])
            if len(names) >= MAX_TOOLS_IN_METADATA:
                break
        return {
            "transport": transport,
            "auth": auth,
            "tool_count": len(tools),
            "tools": names,
        }

    # -- capabilities -------------------------------------------------------

    def describe_capabilities(self, ctx: ExecutionContext) -> CapabilityReport:
        """What *this configuration* can do, with the reason for each gap.

        Deployment readiness is applied afterwards by the registry, so this
        reports only the configuration's own facts: the transport, whether it
        can run at all here, and how a remote connection authenticates.
        """
        config = ctx.config or {}
        transport = _transport(config)
        auth = _auth(config)
        reasons: Dict[str, str] = {}
        classes = {"configure"}
        metadata: Dict[str, Any] = {"transport": transport, "auth": auth}
        # The write class is not openable for MCP in this build (the risk
        # evidence is not accepted), so a tool call is never advertised as
        # available even though the code path exists.
        metadata["write_class_openable"] = False

        if transport == TRANSPORT_STDIO:
            if not _isolation_accepted(ctx):
                reasons["test"] = "stdio_requires_isolation"
                reasons["execute"] = "stdio_requires_isolation"
                metadata["isolation_accepted"] = False
                return CapabilityReport(classes=frozenset(classes),
                                        actions=self._action_states(False),
                                        reasons=reasons, metadata=metadata)
            metadata["isolation_accepted"] = True
        elif transport not in _CLIENT_TRANSPORT:
            reasons["transport"] = "unsupported_transport"
            return CapabilityReport(classes=frozenset(classes),
                                    actions=self._action_states(False),
                                    reasons=reasons, metadata=metadata)

        if auth == "header":
            try:
                has_header = bool(ctx.has_secret("header"))
            except Exception:  # noqa: BLE001 - unreadable secret is "missing"
                has_header = False
            if not has_header:
                reasons["header"] = "secret_missing"
        if transport == TRANSPORT_STDIO and config.get("env_keys"):
            try:
                has_env = bool(ctx.has_secret("env"))
            except Exception:  # noqa: BLE001
                has_env = False
            if not has_env:
                reasons["env"] = "secret_missing"
        if auth == "oauth":
            reasons["oauth"] = "oauth_authorization_required"

        classes.update({"test", "read_execute"})
        return CapabilityReport(classes=frozenset(classes),
                                actions=self._action_states(True),
                                reasons=reasons, metadata=metadata)

    @staticmethod
    def _action_states(configured: bool) -> Dict[str, bool]:
        return {
            ACTION_TOOLS_LIST: bool(configured),
            ACTION_RESOURCES_READ: bool(configured),
            # A read tool call needs nothing declared: what the connection may
            # reach is what its server publishes, and that is discovered rather
            # than configured. A connection that is not configured at all still
            # reports the read action as unavailable.
            ACTION_TOOLS_READ: bool(configured),
            # A tool call is a write; the runtime's open-class check decides it,
            # and for MCP that class is not openable in this build. Reporting it
            # as available here would advertise exactly what the runtime
            # refuses.
            ACTION_TOOLS_CALL: False,
        }

    # -- invoke -------------------------------------------------------------

    def invoke(self, ctx: ExecutionContext, action: str,
               params: Mapping[str, Any]) -> InvokeResult:
        """Run one declared action. Anything else is refused here.

        The runtime has already checked the action list, the open classes, the
        risk catalogue and the approval; repeating the action check means a
        direct adapter call cannot skip it, and the approval is re-checked for a
        write so a caller that reached this method without the runtime still
        cannot perform an unapproved tool call.
        """
        self.guard_action(action)
        if action in self.write_actions:
            approval = dict(ctx.extra.get("approval") or {})
            if not approval.get("approved"):
                return invoke_failed(
                    "approval_required", stage=STAGE_POLICY,
                    message="calling an MCP tool changes the remote system and "
                            "needs an approval bound to it")
        if action == ACTION_TOOLS_CALL:
            return self._invoke_tools_call(ctx, params)
        if action == ACTION_TOOLS_READ:
            return self._invoke_tools_read(ctx, params)
        if action == ACTION_TOOLS_LIST:
            return self._invoke_tools_list(ctx, params)
        return self._invoke_resources_read(ctx, params)

    # -- invoke: shared client handling ------------------------------------

    def _with_client(self, ctx: ExecutionContext, action: str,
                     work) -> InvokeResult:
        """Open a client, run ``work(client)``, and map every failure.

        One place decides how a transport failure becomes an ``InvokeResult``,
        so ``outcome_unknown`` cannot be granted to one action and forgotten for
        another.
        """
        import time as _time

        secrets: List[str] = []
        if _auth(ctx.config) == "header":
            try:
                secret = ctx.secret("header")
            except AdapterError as exc:
                return invoke_failed(exc.code or "secret_unavailable",
                                     stage=exc.stage,
                                     message=_redact_remote(exc, secrets))
            if secret:
                secrets.append(secret)

        ctx.check_alive()
        try:
            client = build_client(ctx)
        except AdapterError as exc:
            return invoke_failed(exc.code or "adapter_error", stage=exc.stage,
                                 message=_redact_remote(exc, secrets))

        started = _time.monotonic()
        try:
            client.initialize_strict()
            result = work(client)
            if result is not None and result.ok and result.duration_ms == 0:
                result = InvokeResult(
                    ok=True, data=result.data,
                    duration_ms=int((_time.monotonic() - started) * 1000))
            return result
        except BaseException as exc:  # noqa: BLE001 - mapped, never leaked
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            code, stage = _error_stage(exc)
            message = _redact_remote(exc, secrets)
            duration_ms = int((_time.monotonic() - started) * 1000)
            if stage == STAGE_TIMEOUT and action in self.write_actions:
                # The request was written to a transport and no answer came
                # back: the server may have received it. Recorded as unknown so
                # a caller reconciles instead of resending — an MCP tool can
                # have any effect, and the protocol offers no idempotency key.
                return invoke_unknown(
                    code or "timeout", stage=STAGE_TIMEOUT,
                    message=(message or "the tool call timed out") +
                            " (the request may have been received; do not "
                            "resend without checking the remote state)",
                    duration_ms=duration_ms)
            return invoke_failed(code, stage=stage, message=message,
                                 duration_ms=duration_ms)
        finally:
            self._close(client)

    @staticmethod
    def _close(client: Any) -> None:
        closer = getattr(client, "shutdown", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - a teardown failure is not a result
                pass

    # -- invoke: actions ----------------------------------------------------

    def _invoke_tools_list(self, ctx: ExecutionContext,
                           params: Mapping[str, Any]) -> InvokeResult:
        def work(client) -> InvokeResult:
            tools = client.list_tools_strict()
            names = [str(t.get("name") or "") for t in tools
                     if isinstance(t, Mapping)]
            return invoke_ok({"tools": names[:MAX_TOOLS_IN_METADATA],
                              "tool_count": len(names)})

        return self._with_client(ctx, ACTION_TOOLS_LIST, work)

    def _invoke_resources_read(self, ctx: ExecutionContext,
                               params: Mapping[str, Any]) -> InvokeResult:
        uri = str((params or {}).get("uri") or "").strip()
        if not uri:
            return invoke_failed("field_required", stage=STAGE_CONFIG,
                                 message="resources.read needs a uri")
        if len(uri) > 2048:
            return invoke_failed("field_invalid", stage=STAGE_CONFIG,
                                 message="the resource uri is too long")

        def work(client) -> InvokeResult:
            return invoke_ok(client.read_resource_strict(uri))

        return self._with_client(ctx, ACTION_RESOURCES_READ, work)

    def _invoke_tools_call(self, ctx: ExecutionContext,
                           params: Mapping[str, Any]) -> InvokeResult:
        name, arguments, failure = _remote_call(params, action=ACTION_TOOLS_CALL)
        if failure is not None:
            return failure

        def work(client) -> InvokeResult:
            text = client.call_tool_strict(name, arguments)
            return invoke_ok(text)

        return self._with_client(ctx, ACTION_TOOLS_CALL, work)

    def _invoke_tools_read(self, ctx: ExecutionContext,
                           params: Mapping[str, Any]) -> InvokeResult:
        """Call one remote tool this connection's server publishes.

        The bound is the *server's own catalogue*, not a list we maintain: the
        name must be among the tools discovery found for this exact connection
        version. It is checked before a client is built, so a name the server
        never published never produces a handshake, let alone a request —
        contacting the server to ask would both leak the name and make the
        server's answer the authority this check exists to keep out of the
        decision.

        A connection with no discovery result for its current version is
        refused, not waved through: "we do not know what it publishes" is not
        "it publishes nothing".

        The remote's own ``annotations.readOnlyHint`` is deliberately not
        consulted. It is data the far side controls, and it decides nothing here
        — neither granting nor withholding a call.
        """
        from agent.tools.mcp.external import remembered_tool_names

        name, arguments, failure = _remote_call(params, action=ACTION_TOOLS_READ)
        if failure is not None:
            return failure
        published = remembered_tool_names(
            tenant_id=str(ctx.tenant_id or ""), connection_id=ctx.connection_id,
            version=int(ctx.config_version or 0))
        if published is None:
            return invoke_failed(
                "tool_not_published", stage=STAGE_POLICY,
                message="this connection has no current discovery result, so "
                        "the tools it publishes are unknown; none is callable "
                        "until discovery answers")
        if name not in published:
            return invoke_failed(
                "tool_not_published", stage=STAGE_POLICY,
                message="%r is not a tool this connection published; only the "
                        "remote server's own catalogue is callable" % name)

        def work(client) -> InvokeResult:
            text = client.call_tool_strict(name, arguments)
            return invoke_ok(text)

        return self._with_client(ctx, ACTION_TOOLS_READ, work)


# -- tenant-scoped reads the tool provider needs -----------------------------
#
# Mirrors ``integrations.external.adapters.erp_scene``: the tool provider must
# answer "which MCP connections does this tenant actually run as" without the
# caller holding a connection-management permission, because a subject whose
# only qualification is tool execution still has to be offered the tools it may
# execute (design §6). Resolution is tenant-scoped and never keyed on an ambient
# value, and a connection id from another tenant answers exactly like a missing
# one.

def _mcp_service():
    from integrations.external.service import get_external_connection_service
    return get_external_connection_service()


def list_mcp_connections(tenant_id: str, *,
                         enabled_only: bool = True) -> List[Mapping[str, Any]]:
    """The MCP connections a tenant may use, stable by name then id.

    The tenant's own rows **plus** the platform templates it was granted. The
    template's own row is the effective connection when the tenant has not
    overridden it, and its secrets are the platform's — a tenant never sees the
    platform config copied into its own row, which is what the inheritance rule
    requires and what this reader must not quietly undo.

    A template the tenant has overridden is returned as the *override* row and
    the template is left out, matching ``effective_id`` in the console: one
    effective connection per logical connection, or the same capability would
    be offered twice under two names.

    A template that is disabled, un-granted or deleted is absent rather than
    listed-and-refused, so the tool list cannot become a platform catalogue
    reader for a tenant that holds no grant.
    """
    tenant = str(tenant_id or "").strip()
    if not tenant:
        return []
    try:
        service = _mcp_service()
    except Exception:  # noqa: BLE001 - an unreadable store offers nothing
        return []
    enabled_clause = " AND enabled=1" if enabled_only else ""
    rows = service._store.execute(  # noqa: SLF001 - same layer as the runtime
        "SELECT * FROM external_connections WHERE kind='mcp'"
        " AND scope=? AND tenant_id=? AND deleted_at IS NULL"
        + enabled_clause + " ORDER BY name, id",
        (registry.SCOPE_TENANT, tenant))
    overridden = {row["base_connection_id"] for row in rows
                  if row["base_connection_id"]}
    inherited = service._store.execute(  # noqa: SLF001
        "SELECT * FROM external_connections WHERE kind='mcp'"
        " AND scope='platform' AND deleted_at IS NULL"
        + enabled_clause +
        " AND id IN (SELECT platform_connection_id FROM"
        "            external_connection_tenant_access"
        "            WHERE tenant_id=? AND enabled=1)"
        + " ORDER BY name, id",
        (tenant,))
    # The tenant's own rows win: where both exist the override is the effective
    # one, and the template behind it is only the source of a version.
    out = list(rows)
    out.extend(row for row in inherited if row["id"] not in overridden)
    out.sort(key=lambda row: (str(row["name"]), str(row["id"])))
    return out


def mcp_connection(tenant_id: str,
                   connection_id: str) -> Optional[Mapping[str, Any]]:
    """One tenant-owned MCP connection, or ``None`` (cross-tenant answers 404)."""
    tenant = str(tenant_id or "").strip()
    connection_id = str(connection_id or "").strip()
    if not tenant or not connection_id:
        return None
    try:
        service = _mcp_service()
    except Exception:  # noqa: BLE001
        return None
    rows = service._store.execute(  # noqa: SLF001
        "SELECT * FROM external_connections WHERE id=? AND kind='mcp'"
        " AND scope=? AND deleted_at IS NULL",
        (connection_id, registry.SCOPE_TENANT))
    if not rows:
        return None
    row = rows[0]
    if str(row["tenant_id"] or "") != tenant:
        return None
    return row


def connection_config(row: Mapping[str, Any]) -> Dict[str, Any]:
    """The stored non-secret configuration of a connection row."""
    try:
        loaded = json.loads(row["config_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


# -- tool provider and dispatcher -------------------------------------------

def _offered_actions(opened: FrozenSet[str]) -> Tuple[str, ...]:
    """Declared actions this deployment would actually accept.

    ``tools.call`` is a write and MCP's write class is not openable in this
    build, so it is never offered — the same rule ERP applies to ``rfc.call``.
    ``tools.read`` is offered for every connection: what it may reach is the
    server's own catalogue, which is discovered rather than declared, so there
    is no per-connection condition left to test here.
    """
    offered = [ACTION_TOOLS_LIST, ACTION_RESOURCES_READ, ACTION_TOOLS_READ]
    if "write_execute" in opened:
        offered.append(ACTION_TOOLS_CALL)
    return tuple(offered)


def discovered_tools_offered(rows: Optional[List[Mapping[str, Any]]] = None) -> bool:
    """Whether per-tool (discovered) bindings could be offered right now.

    A discovered-tool binding exists to call one remote tool, and every
    published tool is offered through ``tools.read``, so this is exactly the
    question "is the read class open here". Discovery performs a handshake,
    which is why the question is asked before spending one at all.

    ``rows`` is accepted for callers that already hold them and is otherwise
    unused: it no longer narrows the answer. It is kept in the signature because
    the caller's question ("should I reconcile this batch") is still the same
    one, and dropping the argument would only move the branch to the call site.
    """
    del rows
    try:
        opened = registry.open_classes(registry.KIND_MCP)
    except Exception:  # noqa: BLE001 - an unreadable switch is closed
        return False
    return ACTION_TOOLS_READ in _offered_actions(opened) \
        and "read_execute" in opened


def _mcp_tool_provider(tenant_id: Optional[str],
                       actor_user_id: str) -> List[Any]:
    """Bind the declared MCP actions to each of the tenant's connections.

    Listing is not authorization: the binding carries the connection id so
    ``dispatch`` re-derives the exact connection, and every call re-checks the
    connection, the tenant and the deployment's open classes inside
    ``ConnectionRuntime.invoke``. The tool name carries the connection as well,
    so two MCP connections cannot collide.

    Two shapes are offered:

    * the *connection-wide* capabilities (``tools.list`` / ``resources.read`` /
      ``tools.read`` / ``tools.call``), which are the declared, grantable pair
      ``(kind, action)`` and exist even before discovery has answered; and
    * one binding per **discovered** remote tool, so the model can call a tool
      by the name and schema its own server published. Discovery is memoized
      against the connection row (see ``agent.tools.mcp.external``), so this
      stays a cheap read on the turn path and re-discovers by itself when the
      connection changes.

    A discovered binding is offered through ``tools.read``: every tool the
    server publishes is callable as a read, so there is no per-tool condition
    left to classify. ``tools.call`` remains the *connection-wide* write action
    (explicit ``{tool, arguments}``, approval-gated, and un-openable for MCP in
    this build); it never carries a discovered tool.
    """
    from agent.tools.mcp import external as mcp_external
    from integrations.external.tools import ExternalTool, ToolBinding

    del actor_user_id  # call-time re-authorization happens in the runtime
    tenant = str(tenant_id or "").strip()
    if not tenant:
        return []
    opened = registry.open_classes(registry.KIND_MCP)
    if "read_execute" not in opened:
        # The deployment has not opened MCP read execution; showing a tool that
        # always fails would be a placeholder, not a capability.
        return []
    rows = list_mcp_connections(tenant, enabled_only=True)
    discover = discovered_tools_offered(rows)
    out: List[Any] = []
    for row in rows:
        connection_id = str(row["id"])
        name = str(row["name"] or connection_id)
        config = connection_config(row)
        transport = _transport(config)
        offered = _offered_actions(opened)
        connection_bindings: List[Any] = []
        for action in offered:
            composed = mcp_external.tool_name(
                action=action, connection_id=connection_id)
            if not composed:
                # Nothing this connection can be named by, so nothing about it
                # can be offered: the memo refuses the same connection for the
                # same reason, and a tool name that fails the wire contract
                # would cost the whole turn rather than just this tool.
                logger.warning(
                    "[ExternalMcpTool] connection %s cannot compose an"
                    " admissible tool name; it is not offered", connection_id)
                connection_bindings = []
                break
            tool = ExternalTool(
                name=composed,
                kind=registry.KIND_MCP, action=action,
                write=action in WRITE_ACTIONS,
                description="Run MCP %s against %s (%s)"
                            % (action, name, transport or "unknown"),
                metadata={"connection_name": name, "transport": transport,
                          "connection_id": connection_id})
            connection_bindings.append(ToolBinding(
                tool=tool, connection_id=connection_id,
                connection_name=name, scope="tenant"))
        if not connection_bindings:
            continue
        out.extend(connection_bindings)
        if discover:
            out.extend(_discovered_bindings(
                tenant_id=tenant, row=row, connection_name=name,
                connection_id=connection_id, transport=transport,
                offered=offered))
    return out


def _discovered_bindings(*, tenant_id: str, row: Mapping[str, Any],
                         connection_name: str, connection_id: str,
                         transport: str, offered: Tuple[str, ...] = ()) -> List[Any]:
    """One binding per remote tool this connection has published.

    The remote name and its input schema travel in the binding's metadata, so
    the agent-side wrapper can advertise the real shape. Nothing is stored here:
    the memo in ``agent.tools.mcp.external`` is keyed on the connection row, and
    the connection id in the name is what dispatch resolves against.

    Every candidate is bound to ``tools.read``. What the model may call is what
    the server published, and the adapter's read action re-checks that same
    catalogue before sending anything — so the listing and the call agree
    without either of them consulting a per-tool declaration. A candidate is
    skipped rather than advertised when the read action is not offered here.
    """
    from agent.tools.mcp import external as mcp_external
    from integrations.external.tools import ExternalTool, ToolBinding

    out: List[Any] = []
    named: set = set()
    try:
        tools = mcp_external.remote_tools_for(tenant_id=tenant_id, row=row)
    except Exception:  # noqa: BLE001 - discovery must never break a listing
        return out
    for schema in tools:
        remote = str(schema.get("name") or "")
        if not remote or len(remote) > mcp_external.MAX_REMOTE_NAME:
            continue
        if ACTION_TOOLS_READ not in offered:
            # No read class here, so there is no action a discovered tool could
            # be reached through: the write action is connection-wide only.
            continue
        action, write = ACTION_TOOLS_READ, False
        name = mcp_external.tool_name(
            action=action, connection_id=connection_id, remote_name=remote)
        if not name:
            # No name within the wire contract could be composed for this one
            # (an absurdly long connection id). Skipping it keeps the other
            # tools usable; composing anyway would fail the whole request.
            logger.warning(
                "[ExternalMcpTool] no admissible tool name for remote tool %r"
                " on connection %s; it is not offered", remote, connection_id)
            continue
        if name in named:
            # Two remote tools never share a wire name (the segment carries a
            # digest whenever rewriting was needed), so reaching this is a
            # defect. Advertising both would hand the model one name for two
            # different remote tools, which is worse than offering one.
            logger.warning(
                "[ExternalMcpTool] remote tools on connection %s collapse onto"
                " the same name %r; %r is not offered", connection_id, name,
                remote)
            continue
        named.add(name)
        tool = ExternalTool(
            name=name,
            kind=registry.KIND_MCP, action=action, write=write,
            description=(str(schema.get("description") or "")
                         or "Call MCP tool %s on %s" % (remote, connection_name)),
            metadata={"connection_name": connection_name, "transport": transport,
                      "connection_id": connection_id,
                      mcp_external.METADATA_REMOTE_TOOL: remote,
                      mcp_external.METADATA_INPUT_SCHEMA: schema.get("inputSchema")},
        )
        out.append(ToolBinding(tool=tool, connection_id=connection_id,
                               connection_name=connection_name, scope="tenant"))
    return out


def _connection_service(service):
    """The connection-control service behind a dispatcher's ``service`` argument.

    ``integrations.external.tools.dispatch`` forwards whatever the calling entry
    point held. The agent-side wrapper passes the *identity* service — the same
    database, but without the connection-control layer — so a dispatcher that
    used it verbatim would fail every call with an ``AttributeError`` instead of
    a decision. When the argument cannot run an action, resolve the
    connection-control service over the same configured database; a caller that
    passes the real service is used verbatim. This is a compatibility path, not
    a second authority: everything below it (open classes, risk, approval,
    audit) runs exactly once, on the connection's own store.
    """
    if callable(getattr(service, "invoke_action", None)):
        return service
    from integrations.external.service import get_external_connection_service
    return get_external_connection_service()


def _mcp_dispatcher(service, binding, params: Mapping[str, Any], *,
                    tenant_id: Optional[str], actor_user_id: str,
                    agent_id: str = "", run_id: str = "",
                    approval: Optional[Mapping[str, Any]] = None):
    """Run one MCP action against the connection the binding named.

    The connection is resolved from the binding's id *now*, so a binding that
    outlived its connection, its grant or its deployment readiness refuses
    instead of running against whatever is left.

    A **discovered** binding names one remote tool, so the model's parameters
    are that tool's own arguments and the remote name is injected here, from the
    binding. Deciding it from the binding's metadata -- never from the shape or
    contents of the model's parameters -- is what keeps a remote tool whose
    schema happens to have a ``tool`` field from having that field read as the
    tool name. Connection-level bindings name no remote tool and are passed
    through untouched, so their explicit ``{tool, arguments}`` form is exactly
    what the caller wrote.
    """
    from agent.tools.mcp import external as mcp_external

    metadata = dict(getattr(binding.tool, "metadata", None) or {})
    remote = str(metadata.get(mcp_external.METADATA_REMOTE_TOOL) or "")
    call_params: Mapping[str, Any] = dict(params or {})
    if remote:
        call_params = {"tool": remote,
                       "arguments": dict(params or {})}
    return _connection_service(service).invoke_action(
        binding.connection_id, binding.tool.action, call_params,
        actor_user_id=actor_user_id, tenant_id=tenant_id,
        agent_id=agent_id, run_id=run_id, approval=approval)


def register_tools() -> None:
    from integrations.external.tools import register_dispatcher, register_tool_provider
    register_tool_provider(registry.KIND_MCP, _mcp_tool_provider)
    register_dispatcher(registry.KIND_MCP, _mcp_dispatcher)


register_tools()
