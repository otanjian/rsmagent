# encoding:utf-8
"""The MCP connection adapter and the MCP parts of task groups 9 and 11.

Subject under test: ``integrations/external/adapters/mcp.py``,
``agent/tools/mcp/external.py`` and the MCP wiring in
``agent/tools/tool_manager.py``.

What the specs make non-negotiable, and where each is checked below:

* **传输与探测** — stdio / SSE / Streamable HTTP are configured, validated
  offline and probed with a real handshake plus ``tools/list``; a probe never
  calls a business tool, and a handshake that succeeds while discovery fails is
  reported as a discovery failure (never as the previous tool count).
* **stdio 不得继承进程环境** — the child receives only the declared
  ``env_keys`` (resolved from the ``env`` secret slot) plus a safe baseline, and
  a deployment that has not accepted the isolation story refuses to start it at
  all.
* **工具发现不等于调用授权** — a discovered tool's identity is bound to the
  connection, every call re-resolves the connection, the actor and the
  deployment's open classes through ``ConnectionRuntime``, and a revoked
  connection / grant / open class fails the call rather than the listing.
* **外部提交超时 SHALL 标记结果未知** — a tool call that may have reached the
  server comes back ``outcome_unknown`` and is never resent.
* **风险、审批与配额** — an MCP tool call is a write: an unapproved one is
  refused on the service path, the tool-entry-point path and the agent path, and
  the call still consumes the ordinary tool-call quota.

No test needs a real MCP server or the network: the client seam
(``build_client``) is replaced with a scripted client, and the classification of
real transport errors is asserted directly.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from agent.tools.mcp import external as mcp_external
from agent.tools.mcp.mcp_client import McpTransportError, _as_transport_error
from common.runtime_identity import RuntimeIdentity, use_identity
from integrations.external import registry
from integrations.external.adapters import mcp as mcp_adapter
from integrations.external.adapters.base import (
    STAGE_AUTH,
    STAGE_CONFIG,
    STAGE_DEPENDENCY,
    STAGE_NETWORK,
    STAGE_POLICY,
    STAGE_PROTOCOL,
    STAGE_TIMEOUT,
    STAGE_TLS,
    AdapterError,
    ExecutionContext,
)
from integrations.external.adapters.netpolicy import NetworkPolicy
from tests._helpers import build_identity, legacy_connection_rule

MASTER_KEY = "unit-test-master-key"
HEADER_SECRET = "mcp-header-secret-9f2c41ab"
ALLOWED_HOST = "mcp.example.com"

STREAMABLE_CONFIG: Mapping[str, Any] = {
    "transport": "streamable_http",
    "url": "https://%s/mcp" % ALLOWED_HOST,
    "auth": "header",
    "header_name": "X-Api-Key",
}
SSE_CONFIG: Mapping[str, Any] = {
    "transport": "sse",
    "url": "https://%s/sse" % ALLOWED_HOST,
    "auth": "header",
    "header_name": "X-Api-Key",
}
STDIO_CONFIG: Mapping[str, Any] = {
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "demo-mcp"],
    "env_keys": ["DEMO_API_KEY"],
}
TOOL_SCHEMA: Mapping[str, Any] = {
    "name": "echo",
    "description": "Echo the input back",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
}


# -- fixtures and helpers ----------------------------------------------------

@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


@pytest.fixture(autouse=True)
def _clean_discovery_memo():
    """No discovered tool identity survives a test."""
    mcp_external._reset_for_tests()
    try:
        yield
    finally:
        mcp_external._reset_for_tests()

def _mcp_name(action, connection_id, remote=""):
    """The binding name the adapter composes for one action on one connection.

    Built through the composer rather than written out, because the wire format
    is a contract with the provider (``^[a-zA-Z0-9_-]+$``) and not a spelling
    these cases are about: they are about which capabilities are offered and
    which calls are authorized. The spelling itself is pinned in
    ``tests/test_tool_name_wire_contract.py``.
    """
    from agent.tools.mcp.external import tool_name
    return tool_name(action=action, connection_id=connection_id,
                     remote_name=remote)


def _model_name(action, connection_id, remote=""):
    """The name the model sees: the binding name carrying its origin prefix."""
    from agent.tools.external.external_tool import _external_tool_name
    return _external_tool_name(_mcp_name(action, connection_id, remote))


def _policy(hosts: Sequence[str] = (ALLOWED_HOST,)) -> NetworkPolicy:
    return NetworkPolicy(allow_hosts=frozenset(hosts))


def _ctx(config: Optional[Mapping[str, Any]] = None, *,
         secrets: Optional[Mapping[str, str]] = None,
         limits: Optional[Mapping[str, Any]] = None,
         extra: Optional[Mapping[str, Any]] = None,
         policy: Optional[NetworkPolicy] = None,
         actor_user_id: str = "u-alice",
         connection_id: str = "conn_mcp_1") -> ExecutionContext:
    values: Dict[str, str] = {"header": HEADER_SECRET,
                              "env": json.dumps({"DEMO_API_KEY": "env-secret-1"})}
    values.update({k: v for k, v in dict(secrets or {}).items()})
    merged: Dict[str, Any] = {"policy": policy or _policy()}
    merged.update(dict(limits or {}))

    def _resolver(slot: str) -> str:
        if slot not in values:
            raise AdapterError("secret %r is not configured" % slot,
                               code="secret_unavailable", stage=STAGE_CONFIG)
        return values[slot]

    return ExecutionContext(
        kind=registry.KIND_MCP, scope=registry.SCOPE_TENANT,
        tenant_id="tenant-1", owner_user_id=None,
        connection_id=connection_id,
        config=dict(config if config is not None else STREAMABLE_CONFIG),
        secret_resolver=_resolver, config_version=3,
        secret_versions={"header": 11},
        actor_user_id=actor_user_id, limits=merged,
        extra=dict(extra or {}))


class FakeClient:
    """A scripted ``McpClient`` at the adapter's own seam.

    The adapter is supposed to translate a client's staged failure into a probe
    stage and to route exactly one call per action, so what the client *did* is
    recorded: a resend after a timeout, or a second initialize, has to show up
    here for the "never retried" property to be testable.
    """

    def __init__(self, *, tools: Sequence[Mapping[str, Any]] = (TOOL_SCHEMA,),
                 init_error: Optional[BaseException] = None,
                 list_error: Optional[BaseException] = None,
                 call_result: Any = "echoed",
                 call_error: Optional[BaseException] = None,
                 read_result: Any = None,
                 read_error: Optional[BaseException] = None):
        self.tools = list(tools)
        self.init_error = init_error
        self.list_error = list_error
        self.call_result = call_result
        self.call_error = call_error
        self.read_result = read_result if read_result is not None else {"text": "ok"}
        self.read_error = read_error
        self.initialized = 0
        self.listed = 0
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.reads: List[str] = []
        self.shutdowns = 0

    def initialize_strict(self) -> None:
        self.initialized += 1
        if self.init_error is not None:
            raise self.init_error

    def list_tools_strict(self):
        self.listed += 1
        if self.list_error is not None:
            raise self.list_error
        return list(self.tools)

    def call_tool_strict(self, name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        if self.call_error is not None:
            raise self.call_error
        return self.call_result

    def read_resource_strict(self, uri: str) -> Any:
        self.reads.append(uri)
        if self.read_error is not None:
            raise self.read_error
        return self.read_result

    def shutdown(self) -> None:
        self.shutdowns += 1


@pytest.fixture
def client_seam(monkeypatch):
    """Replace ``build_client`` with a scripted client; record the contexts."""
    contexts: List[ExecutionContext] = []
    holder: Dict[str, FakeClient] = {}

    def _install(client: FakeClient) -> FakeClient:
        holder["client"] = client

        def _build(ctx, *, name=""):
            contexts.append(ctx)
            return holder["client"]

        monkeypatch.setattr(mcp_adapter, "build_client", _build)
        return client

    _install.holder = holder  # type: ignore[attr-defined]
    _install.contexts = contexts  # type: ignore[attr-defined]
    return _install


def _fail(code: str, stage: str, message: str = "boom") -> McpTransportError:
    return McpTransportError(message, code=code, stage=stage)


# =========================================================================== #
# 1. Declaration and offline validation
# =========================================================================== #

def test_adapter_registers_for_mcp_with_the_declared_actions():
    from integrations.external.adapters.base import adapter_for

    adapter = adapter_for(registry.KIND_MCP)
    assert isinstance(adapter, mcp_adapter.McpAdapter)
    assert adapter.kind == "mcp"
    assert adapter.actions == frozenset(
        {"tools.list", "tools.read", "tools.call", "resources.read"})
    # A tool call is a write: the remote server's own readOnlyHint is data, not
    # authority (risk catalogue entry for ``(mcp, tools.call)``). The read
    # action is the one the connection's own declaration unlocks, and it must
    # not be a way into the write class.
    assert "tools.call" in adapter.write_actions
    assert adapter.write_actions == frozenset({"tools.call"})


def test_validate_config_refuses_stdio_without_a_command():
    with pytest.raises(AdapterError) as error:
        mcp_adapter.McpAdapter().validate_config({"transport": "stdio"})
    assert error.value.code == "field_required"
    assert error.value.stage == STAGE_CONFIG


def test_validate_config_refuses_a_command_outside_the_deployment_allowlist(
        monkeypatch):
    from config import conf
    monkeypatch.setitem(conf(), "mcp_stdio_command_allowlist", ["node"])
    with pytest.raises(AdapterError) as error:
        mcp_adapter.McpAdapter().validate_config(
            {"transport": "stdio", "command": "/usr/bin/npx"})
    assert error.value.code == "command_not_allowed"
    # The runtime gate reads the same key, so "saved" cannot mean "startable".
    from agent.tools.mcp.mcp_client import McpClient
    assert McpClient({"type": "stdio", "command": "npx"})._command_allowed("npx") is False


def test_validate_config_bounds_stdio_args_and_env_keys():
    adapter = mcp_adapter.McpAdapter()
    with pytest.raises(AdapterError) as too_many_args:
        adapter.validate_config({
            "transport": "stdio", "command": "npx",
            "args": ["x"] * (mcp_adapter.MAX_ARGS + 1)})
    assert too_many_args.value.code == "field_invalid"

    with pytest.raises(AdapterError) as bad_env:
        adapter.validate_config({
            "transport": "stdio", "command": "npx", "env_keys": ["9BAD"]})
    assert bad_env.value.code == "field_invalid"

    with pytest.raises(AdapterError) as duplicate_env:
        adapter.validate_config({
            "transport": "stdio", "command": "npx",
            "env_keys": ["A", "A"]})
    assert duplicate_env.value.code == "field_invalid"

    assert adapter.validate_config(STDIO_CONFIG) == STDIO_CONFIG


def test_validate_config_refuses_remote_field_abuse():
    adapter = mcp_adapter.McpAdapter()
    with pytest.raises(AdapterError) as fragment:
        adapter.validate_config({"transport": "sse",
                                 "url": "https://%s/sse#frag" % ALLOWED_HOST})
    assert fragment.value.code == "field_invalid"

    with pytest.raises(AdapterError) as oauth_on_sse:
        adapter.validate_config({"transport": "sse",
                                 "url": "https://%s/sse" % ALLOWED_HOST,
                                 "auth": "oauth"})
    assert oauth_on_sse.value.code == "field_invalid"

    with pytest.raises(AdapterError) as reserved:
        adapter.validate_config({
            "transport": "streamable_http",
            "url": "https://%s/mcp" % ALLOWED_HOST,
            "auth": "header", "header_name": "Mcp-Session-Id"})
    assert reserved.value.code == "field_invalid"

    with pytest.raises(AdapterError) as bad_name:
        adapter.validate_config({
            "transport": "streamable_http",
            "url": "https://%s/mcp" % ALLOWED_HOST,
            "auth": "header", "header_name": "X Api Key"})
    assert bad_name.value.code == "field_invalid"


def test_validate_config_accepts_both_remote_transports():
    adapter = mcp_adapter.McpAdapter()
    assert adapter.validate_config(STREAMABLE_CONFIG) == STREAMABLE_CONFIG
    assert adapter.validate_config(SSE_CONFIG) == SSE_CONFIG
    assert adapter.validate_config({
        "transport": "streamable_http",
        "url": "https://%s/mcp" % ALLOWED_HOST, "auth": "oauth"})["auth"] == "oauth"


# =========================================================================== #
# 2. Probe: handshake and discovery only, one stage per failure kind
# =========================================================================== #

def test_probe_streamable_http_handshakes_and_discovers_only(client_seam):
    fake = client_seam(FakeClient())
    result = mcp_adapter.McpAdapter().probe(_ctx())

    assert result.outcome == "ok"
    assert result.ok is True
    assert fake.initialized == 1 and fake.listed == 1
    # 仅握手与发现: no business tool was called.
    assert fake.calls == []
    assert result.metadata["transport"] == "streamable_http"
    assert result.metadata["tool_count"] == 1
    assert result.metadata["tools"] == ["echo"]
    # The result is bound to the version and secrets it was produced from.
    assert result.config_version == 3
    assert dict(result.secret_versions) == {"header": 11}
    assert fake.shutdowns == 1


def test_probe_sse_connects_and_handshakes(client_seam):
    fake = client_seam(FakeClient())
    result = mcp_adapter.McpAdapter().probe(_ctx(SSE_CONFIG))
    assert result.outcome == "ok"
    assert fake.initialized == 1
    assert result.metadata["transport"] == "sse"


@pytest.mark.parametrize("code,stage,expected_stage", [
    ("authorization_failed", STAGE_AUTH, STAGE_AUTH),
    ("network_error", STAGE_NETWORK, STAGE_NETWORK),
    ("tls_error", STAGE_TLS, STAGE_TLS),
    ("timeout", STAGE_TIMEOUT, STAGE_TIMEOUT),
    ("protocol_error", STAGE_PROTOCOL, STAGE_PROTOCOL),
])
def test_probe_reports_the_failing_stage_and_code(client_seam, code, stage,
                                                 expected_stage):
    client_seam(FakeClient(init_error=_fail(code, stage)))
    result = mcp_adapter.McpAdapter().probe(_ctx())

    failure = result.failed_stage()
    assert failure is not None
    assert failure.code == code
    assert failure.stage == expected_stage
    assert result.outcome == "failed"


def test_probe_maps_a_missing_stdio_executable_to_the_dependency_stage(
        client_seam):
    client_seam(FakeClient(init_error=_fail(
        "dependency_missing", STAGE_DEPENDENCY,
        "the executable 'npx' was not found")))
    result = mcp_adapter.McpAdapter().probe(
        _ctx(STDIO_CONFIG, limits={"uninterruptible_ok": True}))

    failure = result.failed_stage()
    assert failure is not None
    assert (failure.stage, failure.code) == (STAGE_DEPENDENCY,
                                            "dependency_missing")
    assert "was not found" in failure.detail


def test_probe_refuses_a_stdio_process_when_isolation_is_not_accepted(
        client_seam):
    fake = client_seam(FakeClient())
    result = mcp_adapter.McpAdapter().probe(_ctx(STDIO_CONFIG))

    failure = result.failed_stage()
    assert failure is not None
    assert (failure.stage, failure.code) == (STAGE_POLICY,
                                            "stdio_requires_isolation")
    # Refused *before* the process was built: no spawn, no handshake.
    assert fake.initialized == 0


def test_probe_reports_a_discovery_failure_without_reusing_an_old_tool_count(
        client_seam):
    """握手成功而发现失败 → 显示发现失败，不把旧工具数量作为本次发现结果."""
    fake = client_seam(FakeClient(
        list_error=_fail("protocol_error", STAGE_PROTOCOL,
                         "the server sent an unreadable response")))
    result = mcp_adapter.McpAdapter().probe(_ctx())

    failure = result.failed_stage()
    assert failure is not None
    assert failure.code == "protocol_error"
    assert fake.listed == 1
    # No tool summary at all: not the count from a previous run, not an empty
    # "0 tools" that reads like a successful discovery.
    assert "tool_count" not in result.metadata
    assert "tools" not in result.metadata
    handshake = [s for s in result.stages if s.name == "handshake"]
    assert handshake and handshake[0].status == "ok"


def test_probe_refuses_a_target_outside_the_deployment_policy(client_seam):
    fake = client_seam(FakeClient())
    result = mcp_adapter.McpAdapter().probe(
        _ctx(policy=_policy(hosts=("other.example.com",))))

    failure = result.failed_stage()
    assert failure is not None
    assert (failure.stage, failure.code) == (STAGE_POLICY, "target_not_allowed")
    assert fake.initialized == 0  # refused before any request was made


def test_probe_never_puts_the_header_secret_in_the_payload(client_seam):
    """A server echoing the credential must not get it into the summary."""
    client_seam(FakeClient(init_error=_fail(
        "authorization_failed", STAGE_AUTH,
        "rejected credential %s" % HEADER_SECRET)))
    result = mcp_adapter.McpAdapter().probe(_ctx())

    payload = json.dumps(result.as_dict(), ensure_ascii=False)
    assert HEADER_SECRET not in payload
    assert "rejected credential" in payload


def test_probe_does_not_start_stdio_without_the_env_secret(client_seam):
    fake = client_seam(FakeClient())
    result = mcp_adapter.McpAdapter().probe(
        _ctx(STDIO_CONFIG, secrets={"env": ""},
             limits={"uninterruptible_ok": True}))

    failure = result.failed_stage()
    assert failure is not None
    assert failure.stage == STAGE_CONFIG
    assert failure.code == "secret_unavailable"
    assert fake.initialized == 0


def test_stdio_env_does_not_inherit_the_process_environment(monkeypatch):
    """The child gets the declared keys plus a safe baseline — nothing else."""
    from agent.tools.mcp.mcp_client import McpClient

    monkeypatch.setenv("COW_LEAKED_API_KEY", "must-not-reach-the-child")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-the-child")
    monkeypatch.setenv("DYNAMIC_MCP_VALUE", "must-not-reach-the-child")

    ctx = _ctx(STDIO_CONFIG, secrets={
        "env": json.dumps({"DEMO_API_KEY": "declared-value",
                           "NOT_DECLARED": "must-not-reach-the-child"})})
    client = McpClient({"name": "stdio", "type": "stdio", "command": "npx",
                        "args": [], "env_keys": ["DEMO_API_KEY"],
                        "_external_ctx": ctx})
    env = client._build_stdio_env(client._stdio_extra_env())

    assert env["DEMO_API_KEY"] == "declared-value"
    # A value the operator did not declare never reaches the child, even though
    # it is present in the connection's own env secret.
    assert "NOT_DECLARED" not in env
    # Nothing from the agent process's own environment leaks in...
    for leaked in ("COW_LEAKED_API_KEY", "AWS_SECRET_ACCESS_KEY",
                   "DYNAMIC_MCP_VALUE"):
        assert leaked not in env, leaked
    # ...while the toolchain variables a local server needs are kept.
    assert "PATH" in env
    # The values, not just the names, are what matters here.
    assert "must-not-reach-the-child" not in json.dumps(env)


def test_stdio_spawn_passes_only_the_declared_environment(monkeypatch):
    """The real spawn path builds the child's env, not the process's."""
    from agent.tools.mcp import mcp_client

    captured: Dict[str, Any] = {}

    class _Proc:
        pid = 4242
        stdin = None
        stdout = None
        stderr = None
        stdout_fd: Any = None
        stderr_fd: Any = None

        def poll(self):
            return 0

    def _popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = dict(kwargs.get("env") or {})
        raise OSError("no such toolchain in this test")

    monkeypatch.setenv("COW_LEAKED_API_KEY", "must-not-reach-the-child")
    monkeypatch.setattr(mcp_client.subprocess, "Popen", _popen)

    ctx = _ctx(STDIO_CONFIG, limits={"uninterruptible_ok": True})
    client = mcp_client.McpClient({
        "name": "stdio", "type": "stdio", "command": "npx", "args": ["-y", "x"],
        "env_keys": ["DEMO_API_KEY"], "_external_ctx": ctx})
    with pytest.raises(McpTransportError) as error:
        client.initialize_strict()

    assert error.value.stage == STAGE_DEPENDENCY
    assert error.value.code == "dependency_unavailable"
    assert captured["argv"] == ["npx", "-y", "x"]
    assert captured["env"]["DEMO_API_KEY"] == "env-secret-1"
    assert "COW_LEAKED_API_KEY" not in captured["env"]


def test_transport_errors_classify_socket_tls_and_timeout_failures():
    """The client's own classification, so the stage vocabulary is not assumed."""
    assert _as_transport_error(
        socket.gaierror(-2, "Name or service not known")).stage == STAGE_NETWORK
    assert _as_transport_error(
        ConnectionRefusedError("refused")).stage == STAGE_NETWORK
    assert _as_transport_error(ssl.SSLError("handshake failed")).stage == STAGE_TLS
    assert _as_transport_error(TimeoutError("timed out")).stage == STAGE_TIMEOUT
    assert _as_transport_error(
        json.JSONDecodeError("x", "", 0)).stage == STAGE_PROTOCOL
    # An AdapterError keeps its own staged vocabulary.
    staged = _as_transport_error(AdapterError("cancelled", code="cancelled",
                                              stage=STAGE_POLICY))
    assert (staged.stage, staged.code) == (STAGE_POLICY, "cancelled")


# =========================================================================== #
# 3. Capabilities
# =========================================================================== #

def test_capabilities_report_the_stdio_isolation_reason():
    report = mcp_adapter.McpAdapter().describe_capabilities(
        _ctx(STDIO_CONFIG, limits={"uninterruptible_ok": False}))

    assert report.reasons["test"] == "stdio_requires_isolation"
    assert report.reasons["execute"] == "stdio_requires_isolation"
    assert report.actions["tools.list"] is False
    # 连接本身没跑起来，读动作也不报告为可用。
    assert report.actions["tools.read"] is False
    # The write class is not openable for MCP in this build, so the report says
    # so rather than advertising a tool call the runtime refuses.
    assert report.actions["tools.call"] is False
    assert report.metadata["write_class_openable"] is False


def test_capabilities_offer_reads_for_a_remote_connection():
    report = mcp_adapter.McpAdapter().describe_capabilities(_ctx())
    assert set(report.classes) >= {"configure", "test", "read_execute"}
    assert report.actions["tools.list"] is True
    assert report.actions["resources.read"] is True
    # 读动作不再依赖任何逐工具声明：连接可用即可读。
    assert report.actions["tools.read"] is True
    assert report.actions["tools.call"] is False
    assert report.metadata["transport"] == "streamable_http"


def test_capabilities_report_a_missing_header_secret():
    report = mcp_adapter.McpAdapter().describe_capabilities(
        _ctx(secrets={"header": ""}))
    assert report.reasons["header"] == "secret_missing"


# =========================================================================== #
# 4. Invoke
# =========================================================================== #

def test_invoke_refuses_an_undeclared_action():
    with pytest.raises(AdapterError) as error:
        mcp_adapter.McpAdapter().invoke(_ctx(), "shell.run", {})
    assert error.value.code == "unsupported_action"
    assert error.value.stage == STAGE_CONFIG


def test_invoke_tools_call_refuses_without_an_approved_binding(client_seam):
    fake = client_seam(FakeClient())
    result = mcp_adapter.McpAdapter().invoke(
        _ctx(), "tools.call", {"tool": "echo", "arguments": {}})

    assert result.ok is False
    assert result.code == "approval_required"
    assert result.stage == STAGE_POLICY
    # Nothing was sent: the refusal is before the transport.
    assert fake.calls == []
    assert fake.initialized == 0


def test_invoke_tools_call_returns_the_remote_result(client_seam):
    fake = client_seam(FakeClient(call_result={"content": [{"text": "hi"}]}))
    result = mcp_adapter.McpAdapter().invoke(
        _ctx(extra={"approval": {"approved": True}}), "tools.call",
        {"tool": "echo", "arguments": {"text": "hi"}})

    assert result.ok is True
    assert result.data == {"content": [{"text": "hi"}]}
    assert fake.calls == [("echo", {"text": "hi"})]


def test_invoke_resources_read_needs_a_uri(client_seam):
    fake = client_seam(FakeClient())
    result = mcp_adapter.McpAdapter().invoke(_ctx(), "resources.read", {})
    assert result.ok is False and result.code == "field_required"
    assert fake.reads == []

    ok = mcp_adapter.McpAdapter().invoke(
        _ctx(), "resources.read", {"uri": "file:///notes.md"})
    assert ok.ok is True
    assert fake.reads == ["file:///notes.md"]


def test_invoke_reports_outcome_unknown_when_a_write_times_out_and_never_resends(
        client_seam):
    """Task 9.7: the request may have been received — say so, do not resend."""
    fake = client_seam(FakeClient(call_error=_fail(
        "timeout", STAGE_TIMEOUT, "the request timed out")))
    result = mcp_adapter.McpAdapter().invoke(
        _ctx(extra={"approval": {"approved": True}}), "tools.call",
        {"tool": "echo", "arguments": {}})

    assert result.ok is False
    assert result.outcome_unknown is True
    assert result.stage == STAGE_TIMEOUT
    assert "may have been received" in result.message
    # Exactly one attempt: no retry, and no second handshake to retry with.
    assert len(fake.calls) == 1
    assert fake.initialized == 1


def test_invoke_read_timeout_is_a_plain_failure_not_unknown(client_seam):
    client_seam(FakeClient(read_error=_fail("timeout", STAGE_TIMEOUT,
                                            "the request timed out")))
    result = mcp_adapter.McpAdapter().invoke(
        _ctx(), "resources.read", {"uri": "file:///notes.md"})

    assert result.ok is False
    assert result.outcome_unknown is False
    assert result.code == "timeout"


def test_a_tool_call_that_failed_before_it_was_sent_is_a_plain_failure(
        client_seam):
    client_seam(FakeClient(call_error=_fail(
        "authorization_failed", STAGE_AUTH, "the server refused the credential")))
    result = mcp_adapter.McpAdapter().invoke(
        _ctx(extra={"approval": {"approved": True}}), "tools.call",
        {"tool": "echo", "arguments": {}})

    assert result.ok is False
    assert result.outcome_unknown is False
    assert (result.stage, result.code) == (STAGE_AUTH, "authorization_failed")


# =========================================================================== #
# 5. Discovery identity, per-call authorization and invalidation
# =========================================================================== #

@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)
    return build_identity(tmp_path)


@pytest.fixture
def svc(stack, monkeypatch):
    from integrations.external.service import ExternalConnectionService

    service = ExternalConnectionService(stack.service)
    # ``list_mcp_connections`` and the discovery memo resolve the service by the
    # process-wide accessor; point it at this test's database.
    monkeypatch.setattr(
        "integrations.external.service.get_external_connection_service",
        lambda: service)
    return service


def _open_classes(monkeypatch, *, read: bool = True, write: bool = False,
                  test: bool = True) -> frozenset:
    """Open MCP execution classes for this deployment.

    ``write_execute`` is in no deployment's openable set for MCP in this build
    (``OPENABLE_CLASSES``), so only the runtime switch can be exercised — which
    is exactly the fail-closed property being asserted elsewhere.
    """
    opened = {"configure"}
    if test:
        opened.add("test")
    if read:
        opened.add("read_execute")
    if write:
        opened.add("write_execute")
    frozen = frozenset(opened)
    real = registry.open_classes

    def _patched(kind: str):
        return frozen if kind == registry.KIND_MCP else real(kind)

    monkeypatch.setattr(registry, "open_classes", _patched)
    return frozen


def _make_connection(svc, stack, *, name: str = "团队 MCP", config=None):
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="mcp", name=name,
        config=dict(config or STREAMABLE_CONFIG),
        secrets={"header": HEADER_SECRET})
    # These tests are about discovery, the grant gate, approvals and the runtime
    # switches, and they call with no trusted Agent context: keep the connection
    # on the pre-assignment 沿用原权限 rule so the refusal under test is the one
    # the test names. The assigned path has its own tests.
    legacy_connection_rule(svc, stack.tenant_id, connection["id"])
    return connection


def _remember(svc, connection: Mapping[str, Any], *,
              tools: Sequence[Mapping[str, Any]] = (TOOL_SCHEMA,)) -> None:
    mcp_external.remember_tools(
        tenant_id=connection["tenant_id"], connection_id=connection["id"],
        version=int(connection["version"]), tools=tools)


def _tool_names(bindings) -> set:
    return {binding.tool.name for binding in bindings}


def test_discovered_tools_are_bound_to_their_connection(stack, svc, monkeypatch):
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    first = _make_connection(svc, stack, name="MCP A")
    second = _make_connection(svc, stack, name="MCP B")
    _remember(svc, first)
    _remember(svc, second, tools=[dict(TOOL_SCHEMA, name="echo")])
    monkeypatch.setattr(mcp_external, "_discover_in_background", lambda **_: None)

    bindings = external_tools.available_tools(tenant_id=stack.tenant_id,
                                              actor_user_id=stack.root)
    mine = [b for b in bindings if b.tool.kind == "mcp"]
    names = _tool_names(mine)

    assert _mcp_name("tools.list", first["id"]) in names
    assert _mcp_name("tools.read", first["id"], "echo") in names
    assert _mcp_name("resources.read", first["id"]) in names
    # Namespaced by connection: the same remote tool on two connections is two
    # distinct identities, never one name that could resolve to the other.
    assert _mcp_name("tools.read", second["id"], "echo") in names
    assert len(names) == len(set(names))
    for binding in mine:
        assert binding.connection_id in {first["id"], second["id"]}
        assert binding.connection_id in binding.tool.name


def test_discovered_tool_keeps_the_remote_schema_and_read_flag(
        stack, svc, monkeypatch):
    from agent.tools.external.external_tool import ExternalConnectionTool
    from agent.tools.mcp.external import ExternalMcpTool, upgrade_external_mcp_tools
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)

    bindings = [b for b in external_tools.available_tools(
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
        if b.tool.kind == "mcp" and b.tool.action == "tools.read"]
    assert bindings

    upgraded = upgrade_external_mcp_tools(
        {"external_" + b.tool.name: ExternalConnectionTool(b) for b in bindings})
    tool = upgraded[_model_name("tools.read", connection["id"], "echo")]

    assert isinstance(tool, ExternalMcpTool)
    assert tool.remote_name == "echo"
    # The advertised arguments are the remote server's own schema, not a
    # fabricated or permissive one: a model that cannot see them invents them.
    assert tool.params == TOOL_SCHEMA["inputSchema"]
    # 每个发布的工具都以读动作投放：本 build 里没有写动作可以承载它。
    assert tool.binding.tool.write is False
    # It is still an ordinary external connection tool, so the per-turn
    # reconcile and the runtime dispatch treat it identically.
    assert isinstance(tool, ExternalConnectionTool)
    assert tool.self_authorized is True


def test_the_management_path_refuses_an_unapproved_mcp_write(stack, svc,
                                                             monkeypatch):
    """Task 9.6: the service path cannot run an unapproved tool call."""
    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)

    result = svc.invoke_action(
        connection["id"], "tools.call", {"tool": "echo", "arguments": {}},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)

    assert result.ok is False
    assert result.code == "approval_required"
    assert result.outcome_unknown is False


def test_the_tool_entry_point_refuses_an_unapproved_mcp_write(stack, svc,
                                                             monkeypatch):
    """The same refusal through the declared connection-level write action."""
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    name = _mcp_name("tools.call", connection["id"])
    assert external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                      actor_user_id=stack.root) is not None

    result = external_tools.dispatch(
        svc, name, {"tool": "echo", "arguments": {}},
        tenant_id=stack.tenant_id, actor_user_id=stack.root)

    assert result.ok is False
    assert result.code == "approval_required"


def test_the_agent_tool_path_refuses_an_unapproved_mcp_write(stack, svc,
                                                             monkeypatch):
    from agent.tools.mcp.external import upgrade_external_mcp_tools
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    monkeypatch.setattr("auth.service.get_identity_service",
                        lambda: stack.service)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)

    binding = external_tools.find_binding(
        _mcp_name("tools.call", connection["id"]),
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
    tool = upgrade_external_mcp_tools(
        {"external_" + binding.tool.name: _wrap(binding)})[
            _model_name("tools.call", connection["id"])]

    with use_identity(RuntimeIdentity(
            agent_id="agent-a", user_id=stack.root, tenant_id=stack.tenant_id)):
        result = tool.execute({"text": "hi"})

    assert result.status == "error"
    assert "approval" in json.dumps(result.result, ensure_ascii=False)


def test_the_agent_tool_path_refuses_when_the_connection_is_disabled(
        stack, svc, monkeypatch):
    from agent.tools.mcp.external import upgrade_external_mcp_tools
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    monkeypatch.setattr("auth.service.get_identity_service",
                        lambda: stack.service)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)

    binding = external_tools.find_binding(
        _mcp_name("tools.call", connection["id"]),
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
    tool = upgrade_external_mcp_tools(
        {"external_" + binding.tool.name: _wrap(binding)})[
            _model_name("tools.call", connection["id"])]

    svc.update_connection(
        actor_user_id=stack.root, scope="tenant",
        connection_id=connection["id"],
        expected_version=int(connection["version"]), tenant_id=stack.tenant_id,
        enabled=False)

    with use_identity(RuntimeIdentity(
            agent_id="agent-a", user_id=stack.root, tenant_id=stack.tenant_id)):
        result = tool.execute({"text": "hi"})

    assert result.status == "error"
    payload = json.dumps(result.result, ensure_ascii=False)
    assert "not available" in payload or "disabled" in payload


def test_the_agent_tool_path_refuses_without_a_trusted_identity(
        stack, svc, monkeypatch):
    from agent.tools.mcp.external import upgrade_external_mcp_tools
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    binding = external_tools.find_binding(
        _mcp_name("tools.call", connection["id"]),
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
    tool = upgrade_external_mcp_tools(
        {"external_" + binding.tool.name: _wrap(binding)})[
            _model_name("tools.call", connection["id"])]

    # No identity at all: nothing to authorize, so nothing runs.
    result = tool.execute({"text": "hi"})
    assert result.status == "error"


def _wrap(binding):
    from agent.tools.external.external_tool import ExternalConnectionTool
    return ExternalConnectionTool(binding)


def test_the_agent_tool_path_uses_the_real_connection_service(stack, svc,
                                                             monkeypatch):
    """The wrapper passes the identity service; the dispatcher resolves the
    connection-control service over the same store rather than failing."""
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    binding = external_tools.find_binding(
        _mcp_name("tools.call", connection["id"]),
        tenant_id=stack.tenant_id, actor_user_id=stack.root)

    identity_only = stack.service
    assert not hasattr(identity_only, "invoke_action")

    result = external_tools.dispatch(
        identity_only, binding.tool.name, {"tool": "echo", "arguments": {}},
        tenant_id=stack.tenant_id, actor_user_id=stack.root)

    # A decision, not an AttributeError: the approval refusal (or the
    # execution-class refusal) — never a crash.
    assert result.ok is False
    assert result.code in {"approval_required", "execution_not_available"}
    assert "invoke_action" not in result.message


def test_a_disabled_connection_is_not_offered_and_cannot_be_dispatched(
        stack, svc, monkeypatch):
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    name = _mcp_name("tools.read", connection["id"], "echo")
    assert external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                      actor_user_id=stack.root) is not None

    disabled = svc.update_connection(
        actor_user_id=stack.root, scope="tenant", connection_id=connection["id"],
        expected_version=int(connection["version"]), tenant_id=stack.tenant_id,
        enabled=False)
    assert disabled["enabled"] is False

    # Gone from the listing...
    assert external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                       actor_user_id=stack.root) is None
    # ...and a name held from before the change is refused, not run.
    with pytest.raises(Exception) as error:
        external_tools.dispatch(svc, name, {"tool": "echo", "arguments": {}},
                                tenant_id=stack.tenant_id,
                                actor_user_id=stack.root)
    assert getattr(error.value, "code", "") in {
        "not_found", "connection_disabled", "tool_not_available"}


def test_a_deleted_connection_cannot_be_dispatched_from_a_held_name(
        stack, svc, monkeypatch):
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    name = _mcp_name("tools.read", connection["id"], "echo")
    binding = external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                         actor_user_id=stack.root)
    assert binding is not None

    svc.delete_connection(actor_user_id=stack.root, scope="tenant",
                          connection_id=connection["id"],
                          expected_version=int(connection["version"]),
                          tenant_id=stack.tenant_id)

    # The binding object the caller already held cannot be run either: the
    # runtime resolves the connection by id at call time.
    with pytest.raises(Exception) as error:
        external_tools.dispatch(svc, name, {"tool": "echo", "arguments": {}},
                                tenant_id=stack.tenant_id,
                                actor_user_id=stack.root)
    assert getattr(error.value, "code", "") in {"not_found", "connection_disabled"}
    # The runtime resolves the connection by id at call time, so the service
    # path refuses the same way — a 404, not a silent success.
    with pytest.raises(Exception) as direct:
        svc.invoke_action(
            connection["id"], "tools.call", {"tool": "echo"},
            actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert getattr(direct.value, "code", "") == "not_found"


def test_revoking_the_open_class_refuses_a_tool_that_was_just_listed(
        stack, svc, monkeypatch):
    """工具已在会话中展示但其授权或连接随后被撤销 → 实际调用被拒绝."""
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    name = _mcp_name("tools.read", connection["id"], "echo")
    assert external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                      actor_user_id=stack.root) is not None

    # The deployment closes MCP execution after the tool was listed. The old
    # verdict cannot stand in for the current one: the name is no longer
    # offered, so dispatch refuses instead of running anything.
    _open_classes(monkeypatch, read=False, write=False)

    with pytest.raises(Exception) as refusal:
        external_tools.dispatch(
            svc, name, {"tool": "echo", "arguments": {}},
            tenant_id=stack.tenant_id, actor_user_id=stack.root)
    assert getattr(refusal.value, "code", "") in {
        "not_found", "execution_not_available", "connection_disabled"}
    assert external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                       actor_user_id=stack.root) is None


def test_closing_the_write_class_after_listing_refuses_the_call(
        stack, svc, monkeypatch):
    """读仍开放、写被关闭：调用仍被拒绝，且必须是同一个执行闸门给出的理由.

    ``dispatch`` refuses a name it no longer offers; a call that still reaches
    the runtime is refused by the runtime itself with ``execution_not_available``
    — the two paths agree because both re-derive the verdict from the current
    deployment switches rather than trusting the listing.
    """
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)

    # Reads stay open; only the write class closes.
    _open_classes(monkeypatch, read=True, write=False)
    result = svc.invoke_action(
        connection["id"], "tools.call", {"tool": "echo"},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert result.ok is False
    assert result.code == "execution_not_available"


def test_a_connection_of_another_tenant_is_invisible(stack, svc, monkeypatch):
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    theirs = _make_connection(svc, stack, name="Other tenant MCP")
    other = stack.other_tenant()
    # The connection belongs to the first tenant; the second tenant must not see
    # its tool, and a name it guesses must not resolve.
    with pytest.raises(Exception) as invisible:
        svc.get_connection(actor_user_id=stack.root, scope="tenant",
                           connection_id=theirs["id"],
                           tenant_id=other["tenant_id"])
    assert getattr(invisible.value, "code", "") == "not_found"

    theirs_row = svc._store.execute(
        "SELECT * FROM external_connections WHERE id=?", (theirs["id"],))[0]
    mcp_external.remember_tools(
        tenant_id=theirs_row["tenant_id"], connection_id=theirs["id"],
        version=int(theirs_row["version"]), tools=(TOOL_SCHEMA,))
    monkeypatch.setattr(mcp_external, "_discover_in_background", lambda **_: None)

    names = _tool_names(external_tools.available_tools(
        tenant_id=other["tenant_id"], actor_user_id=other["user_id"]))
    assert theirs["id"] not in " ".join(names)
    with pytest.raises(Exception):
        external_tools.dispatch(svc, _mcp_name("tools.read", theirs["id"], "echo"),
                                {"tool": "echo", "arguments": {}},
                                tenant_id=other["tenant_id"],
                                actor_user_id=other["user_id"])


def test_discovery_memo_follows_the_connection_row(stack, svc, monkeypatch):
    """连接变更 SHALL 刷新发现: an edited or disabled connection re-discovers."""
    started: List[Tuple[str, str]] = []
    monkeypatch.setattr(
        mcp_external, "_discover_in_background",
        lambda **kwargs: started.append(
            (kwargs["connection_id"], str(kwargs["row"]["version"]))))

    connection = _make_connection(svc, stack)
    row = svc._store.execute(
        "SELECT * FROM external_connections WHERE id=?", (connection["id"],))[0]
    _remember(svc, connection)
    assert mcp_external.remote_tools_for(
        tenant_id=stack.tenant_id, row=row) == (TOOL_SCHEMA,)

    # An edit bumps the version, so the memo no longer describes the connection
    # and the next listing re-discovers instead of reusing it.
    edited = svc.update_connection(
        actor_user_id=stack.root, scope="tenant",
        connection_id=connection["id"],
        expected_version=int(connection["version"]),
        tenant_id=stack.tenant_id, name="团队 MCP (renamed)")
    new_row = svc._store.execute(
        "SELECT * FROM external_connections WHERE id=?",
        (connection["id"],))[0]
    assert int(new_row["version"]) != int(row["version"])
    assert mcp_external.remote_tools_for(
        tenant_id=stack.tenant_id, row=new_row) == ()
    assert started == [(connection["id"], str(edited["version"]))]

    # A disabled connection loses its memo outright, so a re-created connection
    # cannot inherit the names of a deleted one.
    svc.update_connection(
        actor_user_id=stack.root, scope="tenant",
        connection_id=connection["id"],
        expected_version=int(edited["version"]), tenant_id=stack.tenant_id,
        enabled=False)
    mcp_external.refresh_tenant_tools(tenant_id=stack.tenant_id)
    assert mcp_external.remote_tools_for(
        tenant_id=stack.tenant_id, row=new_row) == ()


def test_tool_manager_refresh_and_invalidate_drop_stale_tool_names(
        stack, svc, monkeypatch):
    from agent.tools.tool_manager import ToolManager

    monkeypatch.setattr(
        "agent.tools.external.external_tool.external_tools_for",
        lambda **_: {})

    _open_classes(monkeypatch, read=True, write=True)

    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    manager = ToolManager()

    assert manager.refresh_external_mcp_tools(
        tenant_id=stack.tenant_id, actor_user_id=stack.root) == 1

    manager.invalidate_external_mcp_tools(tenant_id=stack.tenant_id,
                                          connection_id=connection["id"])
    row = svc._store.execute(
        "SELECT * FROM external_connections WHERE id=?", (connection["id"],))[0]
    monkeypatch.setattr(mcp_external, "_discover_in_background", lambda **_: None)
    assert mcp_external.remote_tools_for(tenant_id=stack.tenant_id,
                                         row=row) == ()


def test_sync_external_into_agent_removes_a_tool_whose_connection_is_gone(
        stack, svc, monkeypatch):
    """A stale tool must not be dispatchable, and must not stay in the list."""
    from agent.tools.tool_manager import ToolManager
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)

    manager = ToolManager()
    agent = SimpleNamespace(tools={})
    identity = RuntimeIdentity(agent_id="agent-a", user_id=stack.root,
                               tenant_id=stack.tenant_id)
    with use_identity(identity):
        added, removed = manager.sync_external_into_agent(agent)
        assert _model_name("tools.read", connection["id"], "echo") in added

        svc.update_connection(
            actor_user_id=stack.root, scope="tenant",
            connection_id=connection["id"],
            expected_version=int(connection["version"]),
            tenant_id=stack.tenant_id, enabled=False)
        added, removed = manager.sync_external_into_agent(agent)
    assert removed and agent.tools == {}


def test_an_mcp_tool_call_still_consumes_the_tool_call_quota(stack, svc,
                                                            monkeypatch):
    """The MCP tool is an ordinary tool: the agent seam meters it."""
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.tools.mcp.external import upgrade_external_mcp_tools
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    binding = external_tools.find_binding(
        _mcp_name("tools.read", connection["id"], "echo"),
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
    tool = upgrade_external_mcp_tools(
        {"external_" + binding.tool.name: _wrap(binding)})[
            _model_name("tools.read", connection["id"], "echo")]

    monkeypatch.setattr("auth.service.get_identity_service", lambda: stack.service)

    class _Stream(AgentStreamExecutor):
        """Only the seams ``_quota_tool_denial`` reads are needed here."""

        def __init__(self):
            self.tools = {tool.name: tool}
            self.agent = SimpleNamespace(
                tools=self.tools, effective_cwd=lambda: "/tmp")

    stack.service.set_quota(actor_user_id=stack.root, tenant_id=stack.tenant_id,
                            metric="tool_calls", hard_limit=1)
    stream = _Stream()
    with use_identity(RuntimeIdentity(agent_id="agent-a", user_id=stack.root,
                                      tenant_id=stack.tenant_id)):
        assert stream._quota_tool_denial(tool.name) is None
        denial = stream._quota_tool_denial(tool.name)

    assert denial is not None and "quota" in denial


# =========================================================================== #
# 12. Published tools: offered through a read action, bounded by discovery
#     (change add-external-mcp-readonly-tool-execution)
# =========================================================================== #
#
# The reported symptom: an Agent assigned to an MCP connection could not see or
# call the connection's tools. Two of the three layers were structural rather
# than a bug — ``tools.call`` is a write by design, and MCP's write class is not
# openable in this build — so the *code path* for "call a tool as a read" did
# not exist, and no deployment switch could have opened it.
#
# These cases pin that path: the read action is its own (kind, action) pair,
# every tool a connection publishes is offered through it without any per-tool
# declaration, the remote server's ``readOnlyHint`` decides nothing, the names
# that may be called are the ones this connection actually published, and a
# discovered tool's call carries the remote name from the binding so the model
# only supplies that tool's own arguments.


def test_read_only_tool_calls_are_a_read_action_separate_from_the_write_one():
    from integrations.external import risk
    from integrations.external.adapters.base import adapter_for

    adapter = adapter_for(registry.KIND_MCP)
    assert mcp_adapter.ACTION_TOOLS_READ in adapter.actions
    # 读与写分别归档：新增读动作不得把写动作的判定放宽。
    assert mcp_adapter.ACTION_TOOLS_READ not in adapter.write_actions
    assert adapter.write_actions == frozenset({"tools.call"})

    entry = risk.RISK_CATALOGUE[(registry.KIND_MCP, mcp_adapter.ACTION_TOOLS_READ)]
    assert entry.write is False
    assert entry.level == risk.RISK_LOW


def test_a_per_tool_declaration_is_not_a_configuration_field():
    """没有逐工具名单：MCP 的字段集合里不存在这样的键。"""
    assert "read_only_tools" not in registry.spec_for(
        registry.KIND_MCP).config_keys

    with pytest.raises(AdapterError) as unknown:
        registry.validate_config(
            registry.KIND_MCP, dict(STREAMABLE_CONFIG, read_only_tools=["echo"]))
    assert unknown.value.code == "unknown_field"
    assert unknown.value.fields == {"read_only_tools": "unknown"}


def test_a_stored_row_that_still_carries_the_removed_key_keeps_working(
        stack, svc, monkeypatch, client_seam):
    """遗留行不被字段移除打断：读取、调用、再次保存三条路径都不报错。"""
    fake = client_seam(FakeClient(call_result="echoed"))
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)

    # What an in-place upgrade looks like: the key is in the stored JSON and the
    # field no longer exists. Written directly so the row is exactly that.
    legacy = json.dumps(dict(STREAMABLE_CONFIG, read_only_tools=["echo"]))
    svc._store.execute(
        "UPDATE external_connections SET config_json=? WHERE id=?",
        (legacy, connection["id"]))
    row = svc._store.execute(
        "SELECT * FROM external_connections WHERE id=?", (connection["id"],))[0]
    assert mcp_adapter.connection_config(row)["read_only_tools"] == ["echo"]

    # Reading it is unaffected, and it decides nothing: the call runs because
    # the tool is published, not because the leftover names it.
    _remember(svc, row)
    result = svc.invoke_action(
        connection["id"], mcp_adapter.ACTION_TOOLS_READ,
        {"tool": "echo", "arguments": {}},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert result.ok is True
    assert fake.calls == [("echo", {})]

    # Re-saving with the shape the console now sends drops the leftover key:
    # the row migrates itself the first time somebody touches it.
    svc.update_connection(
        actor_user_id=stack.root, scope="tenant", connection_id=connection["id"],
        expected_version=int(connection["version"]), tenant_id=stack.tenant_id,
        config=dict(STREAMABLE_CONFIG))
    saved = svc._store.execute(
        "SELECT * FROM external_connections WHERE id=?", (connection["id"],))[0]
    assert "read_only_tools" not in mcp_adapter.connection_config(saved)


def test_a_published_tool_runs_through_the_read_action(
        stack, svc, monkeypatch, client_seam):
    fake = client_seam(FakeClient(call_result={"content": [{"text": "hit"}]}))
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)

    result = svc.invoke_action(
        connection["id"], mcp_adapter.ACTION_TOOLS_READ,
        {"tool": "echo", "arguments": {"text": "hi"}},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)

    assert result.ok is True
    # 读动作不需要审批，且参数原样到达远端。
    assert fake.calls == [("echo", {"text": "hi"})]


def test_a_tool_the_connection_did_not_publish_is_never_reached(
        stack, svc, monkeypatch, client_seam):
    """边界是「服务器发布过这个名字」，不是一份人工维护的名单。"""
    fake = client_seam(FakeClient())
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)

    result = svc.invoke_action(
        connection["id"], mcp_adapter.ACTION_TOOLS_READ,
        {"tool": "delete_everything", "arguments": {}},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)

    assert result.ok is False
    assert result.code == "tool_not_published"
    # 拒绝发生在建立连接之前：远端没有被触达。
    assert fake.calls == []
    assert fake.initialized == 0


def test_a_read_before_the_connection_was_discovered_is_refused(
        stack, svc, monkeypatch, client_seam):
    """发现结果未知时 fail-closed：不猜测、不握手、不放行。"""
    fake = client_seam(FakeClient())
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)

    result = svc.invoke_action(
        connection["id"], mcp_adapter.ACTION_TOOLS_READ,
        {"tool": "echo", "arguments": {}},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)

    assert result.ok is False
    assert result.code == "tool_not_published"
    assert fake.calls == []
    assert fake.initialized == 0


def test_the_remote_read_only_hint_changes_nothing(
        stack, svc, monkeypatch, client_seam):
    """远端自称只读是数据，不是判定依据：既不因此放行，也不因此拒绝。"""
    fake = client_seam(FakeClient())
    _open_classes(monkeypatch, read=True)
    for hint in (True, False, None):
        annotations = {} if hint is None else {"readOnlyHint": hint}
        connection = _make_connection(svc, stack, name="hint %s" % hint)
        _remember(svc, connection,
                  tools=[dict(TOOL_SCHEMA, annotations=annotations)])

        result = svc.invoke_action(
            connection["id"], mcp_adapter.ACTION_TOOLS_READ,
            {"tool": "echo", "arguments": {}},
            actor_user_id=stack.root, tenant_id=stack.tenant_id)
        assert result.ok is True, (hint, result.code)

    assert fake.calls == [("echo", {}), ("echo", {}), ("echo", {})]


def test_published_candidates_are_offered_when_only_the_read_class_is_open(
        stack, svc, monkeypatch):
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True, write=False)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    monkeypatch.setattr(mcp_external, "_discover_in_background", lambda **_: None)

    names = _tool_names(external_tools.available_tools(
        tenant_id=stack.tenant_id, actor_user_id=stack.root))

    assert _mcp_name("tools.read", connection["id"], "echo") in names
    # 写入仍不开放：写动作不被投放。
    assert _mcp_name("tools.call", connection["id"], "echo") not in names


def test_a_connection_needs_no_declaration_to_offer_its_tools(
        stack, svc, monkeypatch):
    """本 change 的反转：没有任何声明的连接照样投放它发布的工具。"""
    from integrations.external import tools as external_tools

    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack, config=STREAMABLE_CONFIG)
    _remember(svc, connection)
    monkeypatch.setattr(mcp_external, "_discover_in_background", lambda **_: None)

    names = _tool_names(external_tools.available_tools(
        tenant_id=stack.tenant_id, actor_user_id=stack.root))

    assert _mcp_name("tools.read", connection["id"], "echo") in names
    # 读切片开放不等于写切片开放。
    assert _mcp_name("tools.call", connection["id"], "echo") not in names


def test_a_discovered_tool_call_carries_the_remote_name_and_the_model_arguments(
        stack, svc, monkeypatch, client_seam):
    """发现工具的远端名由绑定注入，模型只给该工具自己的参数。"""
    from agent.tools.mcp.external import upgrade_external_mcp_tools
    from integrations.external import tools as external_tools

    fake = client_seam(FakeClient(call_result="echoed"))
    _open_classes(monkeypatch, read=True)
    monkeypatch.setattr("auth.service.get_identity_service",
                        lambda: stack.service)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)

    binding = external_tools.find_binding(
        _mcp_name("tools.read", connection["id"], "echo"),
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
    tool = upgrade_external_mcp_tools(
        {"external_" + binding.tool.name: _wrap(binding)})[
            _model_name("tools.read", connection["id"], "echo")]

    with use_identity(RuntimeIdentity(
            agent_id="agent-a", user_id=stack.root, tenant_id=stack.tenant_id)):
        result = tool.execute({"text": "hi"})

    assert result.status != "error", result.result
    # 关键断言：远端收到工具名与 arguments，而不是缺工具名的 field_required。
    assert fake.calls == [("echo", {"text": "hi"})]


def test_a_remote_parameter_named_tool_is_not_read_as_the_tool_name(
        stack, svc, monkeypatch, client_seam):
    """封装按绑定判定，不按参数内容猜：远端工具自己的 ``tool`` 参数原样保留。"""
    from agent.tools.mcp.external import upgrade_external_mcp_tools
    from integrations.external import tools as external_tools

    fake = client_seam(FakeClient())
    _open_classes(monkeypatch, read=True)
    monkeypatch.setattr("auth.service.get_identity_service",
                        lambda: stack.service)
    schema = {"name": "echo", "inputSchema": {
        "type": "object", "properties": {"tool": {"type": "string"}}}}
    connection = _make_connection(svc, stack)
    _remember(svc, connection, tools=[schema])

    binding = external_tools.find_binding(
        _mcp_name("tools.read", connection["id"], "echo"),
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
    tool = upgrade_external_mcp_tools(
        {"external_" + binding.tool.name: _wrap(binding)})[
            _model_name("tools.read", connection["id"], "echo")]

    with use_identity(RuntimeIdentity(
            agent_id="agent-a", user_id=stack.root, tenant_id=stack.tenant_id)):
        tool.execute({"tool": "search"})

    assert fake.calls == [("echo", {"tool": "search"})]


def test_a_connection_level_read_keeps_the_explicit_form(
        stack, svc, monkeypatch, client_seam):
    """连接级动作显式给出工具名与参数，语义不因本次封装而改变。

    封装只按绑定的 metadata 判定，所以没有 ``remote_tool`` 的绑定（连接级
    动作）原样透传：这里的参数与连接级读取语义都不经过二次封装。
    """
    from integrations.external import tools as external_tools

    fake = client_seam(FakeClient())
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)

    result = external_tools.dispatch(
        svc, _mcp_name("tools.read", connection["id"]),
        {"tool": "echo", "arguments": {"text": "hi"}},
        tenant_id=stack.tenant_id, actor_user_id=stack.root)

    assert result.ok is True
    assert fake.calls == [("echo", {"text": "hi"})]


def test_closing_read_execution_refuses_a_read_tool_that_was_just_listed(
        stack, svc, monkeypatch, client_seam):
    """读切片关闭后，刚展示过的只读工具在下一次派发前被拒。"""
    from integrations.external import tools as external_tools

    fake = client_seam(FakeClient())
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    name = _mcp_name("tools.read", connection["id"], "echo")
    assert external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                       actor_user_id=stack.root) is not None

    _open_classes(monkeypatch, read=False)

    assert external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                       actor_user_id=stack.root) is None
    result = svc.invoke_action(
        connection["id"], mcp_adapter.ACTION_TOOLS_READ,
        {"tool": "echo", "arguments": {}},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert result.ok is False
    assert result.code == "execution_not_available"
    assert fake.calls == []


def test_a_disabled_connection_refuses_the_read_action(
        stack, svc, monkeypatch, client_seam):
    fake = client_seam(FakeClient())
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)
    svc.update_connection(
        actor_user_id=stack.root, scope="tenant", connection_id=connection["id"],
        expected_version=int(connection["version"]), tenant_id=stack.tenant_id,
        enabled=False)

    result = svc.invoke_action(
        connection["id"], mcp_adapter.ACTION_TOOLS_READ,
        {"tool": "echo", "arguments": {}},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)

    assert result.ok is False
    assert result.code in {"connection_disabled", "not_found"}
    assert fake.calls == []


def test_a_tool_that_stops_being_published_is_refused(
        stack, svc, monkeypatch, client_seam):
    """发现结果按新值生效：不再发布的名字既不被投放，也不被放行。"""
    from integrations.external import tools as external_tools

    fake = client_seam(FakeClient())
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection)
    name = _mcp_name("tools.read", connection["id"], "echo")
    assert external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                       actor_user_id=stack.root) is not None

    # The remote stops publishing ``echo``. The connection row is untouched, so
    # the discovery result is the only thing that moved — which is exactly the
    # invalidation path this case exists to pin.
    _remember(svc, connection, tools=[dict(TOOL_SCHEMA, name="search")])

    assert external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                       actor_user_id=stack.root) is None
    result = svc.invoke_action(
        connection["id"], mcp_adapter.ACTION_TOOLS_READ,
        {"tool": "echo", "arguments": {}},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert result.ok is False
    assert result.code == "tool_not_published"
    assert fake.calls == []


# -- 工具名的线路契约 --------------------------------------------------------
#
# 现场报告：在智能体里问「你可以使用的mcp工具？」得到
#
#   Invalid 'tools[18].function.name': string does not match pattern
#   '^[a-zA-Z0-9_-]+$'
#
# 名字里的 `.` 来自动作 id（``tools.read``）与组合分隔符。DeepSeek 的工具名契约
# 只接受 ``[A-Za-z0-9_-]`` 且不超过 128 个字符 —— 违反时被拒绝的是**整次请求**，
# 不是那一个工具：模型连一句话都答不出来。所以这不是「某个名字不好看」，而是
# 「这个名字根本发不出去」，契约在这里被断言成一条不变量。
#
# 两个边界都要覆盖：字符集（远端名可以含任意字符，它来自第三方服务器）与长度
# （``MAX_REMOTE_NAME`` 曾允许 128，加上前缀与连接标识后必然超过 128）。

#: The contract the provider enforces, spelled out literally rather than imported,
#: so a change to it has to be a deliberate edit here as well.
WIRE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _offered_names(stack, monkeypatch):
    """Every name this tenant's MCP bindings carry, plus the model-visible one."""
    from integrations.external import tools as external_tools

    monkeypatch.setattr(mcp_external, "_discover_in_background", lambda **_: None)
    bindings = [b for b in external_tools.available_tools(
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
        if b.tool.kind == "mcp"]
    return bindings, _tool_names(bindings), {_wrap(b).name for b in bindings}


def _discovered_names(bindings) -> set:
    """Only the names that came from a remote tool, not the connection-level ones."""
    return {b.tool.name for b in bindings
            if b.tool.metadata.get(mcp_external.METADATA_REMOTE_TOOL)}


def test_every_name_sent_to_the_model_satisfies_the_wire_contract(
        stack, svc, monkeypatch):
    """字符集：远端名可以含任意字符，模型看到的名字不能。"""
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection, tools=[
        dict(TOOL_SCHEMA, name="echo"),
        dict(TOOL_SCHEMA, name="tools.read"),         # a dot, like the action ids
        dict(TOOL_SCHEMA, name="weird name/with, junk"),
        dict(TOOL_SCHEMA, name="知识库检索"),           # non-ASCII
        dict(TOOL_SCHEMA, name="a" * 200),            # past MAX_REMOTE_NAME
    ])

    bindings, binding_names, model_names = _offered_names(stack, monkeypatch)

    assert len(bindings) >= 5, "every published tool must still be offered"
    for name in binding_names | model_names:
        assert WIRE_NAME_RE.match(name), name
    # The model-visible name is the one that was actually rejected in the field,
    # so it is asserted on its own rather than left implied by the binding name.
    assert model_names and all(WIRE_NAME_RE.match(n) for n in model_names)


def test_a_remote_name_with_illegal_characters_stays_addressable(
        stack, svc, monkeypatch):
    """规范化丢掉了信息，所以名字要绑回原始身份，而不是只把非法字符抹平。

    ``tools.read`` 与 ``tools-read`` 抹平后是同一个串。只抹平会让两个不同的远端
    工具共用一个名字：模型无法分别寻址，而派发按绑定的远端名执行，谁被调用就取决于
    列表顺序。加上原始名的摘要后两者可区分，且与顺序无关。
    """
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection, tools=[
        dict(TOOL_SCHEMA, name="tools.read"),
        dict(TOOL_SCHEMA, name="tools-read"),
    ])

    bindings, binding_names, _ = _offered_names(stack, monkeypatch)

    discovered = _discovered_names(bindings)
    assert len(discovered) == 2, binding_names
    assert "tools.read" not in " ".join(binding_names)


def test_two_distant_remote_names_do_not_share_one_name(
        stack, svc, monkeypatch):
    """同名不等于同工具：远端名不同，模型看到的名字必须不同。"""
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)
    # Long enough that the composed name would pass the provider's 128-character
    # bound, still inside what discovery accepts from the server.
    long_a = "search_" + "x" * 113
    long_b = "search_" + "y" * 113
    assert len(long_a) <= mcp_external.MAX_REMOTE_NAME
    _remember(svc, connection, tools=[
        dict(TOOL_SCHEMA, name=long_a),
        dict(TOOL_SCHEMA, name=long_b),
    ])

    bindings, binding_names, model_names = _offered_names(stack, monkeypatch)

    discovered = _discovered_names(bindings)
    assert len(discovered) == 2, binding_names
    assert all(WIRE_NAME_RE.match(n) for n in model_names)


def test_a_long_connection_id_cannot_push_a_name_over_the_limit(
        stack, svc, monkeypatch):
    """长度预算按实际连接标识算，不是按一个假定值。"""
    _open_classes(monkeypatch, read=True)
    connection = _make_connection(svc, stack)
    _remember(svc, connection, tools=[dict(TOOL_SCHEMA, name="e" * 128)])

    # A connection id at the top of what the store can produce, plus the longest
    # remote name discovery accepts, is the worst case the budget must survive.
    assert mcp_external.MAX_REMOTE_NAME == 128
    _, binding_names, model_names = _offered_names(stack, monkeypatch)
    assert all(WIRE_NAME_RE.match(n) for n in model_names)

    from agent.tools.mcp.external import tool_name
    worst = tool_name(action="resources.read", connection_id="conn_" + "z" * 22,
                      remote_name="e" * mcp_external.MAX_REMOTE_NAME)
    from agent.tools.external.external_tool import _external_tool_name
    assert WIRE_NAME_RE.match(_external_tool_name(worst)), len(
        _external_tool_name(worst))
