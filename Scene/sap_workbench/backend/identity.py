"""Pure contracts for an opt-in, independently verified SAP browser identity.

Only a scene-owned collector of a known SAP System Status screen may supply
``system_status`` evidence. A source label received from a model or HTTP client
does not establish trust. The caller owns the browser target/origin and checks
its fresh revision/control epoch before invoking this module. No page title,
URL, configured account, or sap-connect registry value is identity evidence.

The optional HTTP probe reads the configured account's server report without
collecting browser credentials/cookies or changing the existing manual path.
MCP credentials remain scene_config. Binding references below are internal
handles, never passwords, browser tokens, or model-visible tool arguments.
"""
from dataclasses import dataclass, field
import json
import math
import re
from urllib.parse import parse_qsl, urlsplit

from .configuration import WorkbenchError
from .deadline import task_timeout
from .deployment import MCP_ENDPOINTS, verify_sap_tls

_EVIDENCE_KEYS = frozenset({
    "source", "sysid", "client", "username", "revision", "epoch", "login_generation",
})
_MAX_GENERATION = (1 << 63) - 1
ADT_IDENTITY_PATH = "/sap/bc/adt/core/http/systeminformation"
ADT_IDENTITY_ACCEPT = "application/vnd.sap.adt.core.http.systeminformation.v1+json"
ADT_IDENTITY_MAX_BYTES = 16384
ADT_IDENTITY_CLOSE_TIMEOUT = 1


def _counter(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= _MAX_GENERATION:
        raise WorkbenchError("sap_identity_invalid", 409)
    return value


def _identifier(value, pattern, maximum):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or not re.fullmatch(pattern, value)):
        raise WorkbenchError("sap_identity_invalid", 409)
    return value


def _account(sysid, client, username):
    # SID and SAP usernames are case-insensitive here; the Client is an exact
    # three-character value. Never coerce 1 to "001" or take a URL parameter.
    return (
        _identifier(sysid, r"[A-Za-z0-9]{3}", 3).upper(),
        _identifier(client, r"[0-9]{3}", 3),
        _identifier(username, r"[A-Za-z0-9_.@/-]{1,64}", 64).upper(),
    )


def _reference(value):
    # Handles are bounded but intentionally not interpreted as credentials.
    if (not isinstance(value, str) or not value or len(value) > 256
            or value != value.strip() or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise WorkbenchError("sap_identity_binding_invalid", 409)
    return value


def adt_identity_request(client):
    """Fixed read-only request metadata; the trusted caller supplies auth/TLS.

    Query selection is not proof of the response's actual Client. The caller
    must use its bound SAP origin, prevent redirects, bound request time, and
    pass the authenticated server response to ``parse_adt_identity_response``.
    This contract does not obtain or transfer browser session credentials.
    """
    selected = _identifier(client, r"[0-9]{3}", 3)
    return {"method": "GET", "path": ADT_IDENTITY_PATH,
            "query": {"sap-client": selected}, "headers": {"Accept": ADT_IDENTITY_ACCEPT}}


@dataclass(frozen=True)
class ServerSapIdentity:
    """A server report parsed by a trusted HTTP consumer, not registry data."""
    sysid: str
    client: str
    username: str
    source: str = "adt_systeminformation"

    def __post_init__(self):
        if (_account(self.sysid, self.client, self.username) != (self.sysid, self.client, self.username)
                or self.source != "adt_systeminformation"):
            raise WorkbenchError("sap_identity_invalid", 409)


def _http_origin(value, *, response=False):
    try:
        if not isinstance(value, str) or not value or len(value) > 2048 or "\\" in value:
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or any(ord(char) <= 32 for char in value)
                or parsed.port == 0):
            raise ValueError()
        if not response and (parsed.path not in {"", "/"} or parsed.query):
            raise ValueError()
        return parsed, (parsed.scheme, parsed.hostname.lower(), parsed.port or 443)
    except (ValueError, TypeError):
        raise WorkbenchError("sap_identity_unverified", 409) from None


def parse_adt_identity_response(body, *, status, content_type, response_url, expected_origin,
                                expected_sysid, expected_client, expected_username):
    """Parse only the fixed endpoint's bounded JSON response from trusted I/O.

    The caller must establish that this is the authenticated response of its
    configured MCP consumer. A model/HTTP client cannot supply this evidence.
    Browser and ADT authentication can be independent even on the same origin;
    this function therefore never returns a VerifiedSapIdentity for Web GUI.
    ``sap_whoami`` metadata and discovery/health responses are not accepted.
    """
    if (type(status) is not int or status != 200 or not isinstance(content_type, str)
            or len(content_type) > 160 or content_type.split(";", 1)[0].strip().lower()
            not in {"application/json", ADT_IDENTITY_ACCEPT}):
        raise WorkbenchError("sap_identity_unverified", 409)
    parsed, origin = _http_origin(response_url, response=True)
    _, expected = _http_origin(expected_origin)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if (origin != expected or parsed.path != ADT_IDENTITY_PATH
            or query not in ([], [("sap-client", expected_client)])):
        raise WorkbenchError("sap_identity_unverified", 409)
    if not isinstance(body, (str, bytes)) or len(body) > ADT_IDENTITY_MAX_BYTES:
        raise WorkbenchError("sap_identity_unverified", 409)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError()

    try:
        payload = json.loads(body, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WorkbenchError("sap_identity_unverified", 409) from None
    if not isinstance(payload, dict) or not {"systemID", "client", "userName"} <= payload.keys():
        raise WorkbenchError("sap_identity_unverified", 409)
    actual = _account(payload["systemID"], payload["client"], payload["userName"])
    if actual != _account(expected_sysid, expected_client, expected_username):
        raise WorkbenchError("sap_identity_mismatch", 409)
    return ServerSapIdentity(*actual)


async def verify_configured_adt_identity(sap_url, *, sysid, client, username, password,
                                         timeout=10, client_factory=None):
    """Private read-only probe of the configured SAP account, not Web GUI login.

    Only trusted scene code may call this with resolved configuration secrets.
    Basic auth is sent to one configured HTTPS origin and the fixed ADT path;
    redirects/proxies are disabled. TLS uses the existing explicit test-only
    exception. Network work has an absolute same-task deadline <= 10 seconds,
    raw response size <= 16 KiB, and two bounded one-second cleanup budgets.
    No credentials, bodies, HTTP diagnostics or response headers are returned
    or included in errors. ``client_factory`` is an offline test seam.
    """
    _account(sysid, client, username)
    if (not isinstance(password, str) or not password or len(password) > 4096
            or type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 10):
        raise WorkbenchError("sap_identity_probe_invalid", 400)
    target, _ = _http_origin(sap_url, response=True)
    origin = "https://" + target.netloc
    request = adt_identity_request(client)
    # Optional scene dependency: importing ordinary scene contracts does not
    # import HTTP clients, discover accounts, or create config/log directories.
    try:
        import httpx
    except ImportError:
        raise WorkbenchError("sap_identity_probe_unavailable", 503) from None
    http, response, successful = None, None, False
    try:
        factory = client_factory or httpx.AsyncClient
        http = factory(timeout=httpx.Timeout(timeout), trust_env=False, follow_redirects=False,
                       verify=verify_sap_tls(sap_url), auth=httpx.BasicAuth(username, password))
        async with task_timeout(timeout):
            fixed = http.build_request("GET", origin + request["path"], params=request["query"],
                                       headers={**request["headers"], "Accept-Encoding": "identity"})
            response = await http.send(fixed, stream=True, follow_redirects=False)
            if response.status_code != 200 or response.headers.get("content-encoding", "identity").lower() != "identity":
                raise WorkbenchError("sap_identity_unverified", 409)
            body = bytearray()
            async for chunk in response.aiter_raw(chunk_size=4096):
                if len(body) + len(chunk) > ADT_IDENTITY_MAX_BYTES:
                    raise WorkbenchError("sap_identity_unverified", 409)
                body.extend(chunk)
            identity = parse_adt_identity_response(bytes(body), status=response.status_code,
                content_type=response.headers.get("content-type", ""), response_url=str(response.url),
                expected_origin=origin, expected_sysid=sysid, expected_client=client, expected_username=username)
        successful = True
        return identity
    except WorkbenchError:
        raise
    except Exception as error:
        import asyncio
        if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)):
            raise WorkbenchError("sap_identity_probe_timeout", 503) from None
        raise WorkbenchError("sap_identity_probe_failed", 503) from None
    finally:
        cleanup_failed = False
        try:
            if response is not None:
                try:
                    async with task_timeout(ADT_IDENTITY_CLOSE_TIMEOUT):
                        await response.aclose()
                except Exception:
                    cleanup_failed = True
        finally:
            if http is not None:
                try:
                    async with task_timeout(ADT_IDENTITY_CLOSE_TIMEOUT):
                        await http.aclose()
                except Exception:
                    cleanup_failed = True
        if successful and cleanup_failed:
            raise WorkbenchError("sap_identity_probe_cleanup_failed", 503)


def require_same_sap_account(browser, server):
    """Compare two separately trusted observations, without credential reuse."""
    if type(browser) is not VerifiedSapIdentity or type(server) is not ServerSapIdentity:
        raise WorkbenchError("sap_identity_unverified", 409)
    if (browser.sysid, browser.client, browser.username) != (server.sysid, server.client, server.username):
        raise WorkbenchError("sap_identity_mismatch", 409)
    return True


@dataclass(frozen=True)
class VerifiedSapIdentity:
    sysid: str
    client: str
    username: str
    revision: str
    epoch: int
    login_generation: int
    source: str = "system_status"

    def __post_init__(self):
        canonical = _account(self.sysid, self.client, self.username)
        if canonical != (self.sysid, self.client, self.username) or self.source != "system_status":
            raise WorkbenchError("sap_identity_invalid", 409)
        _identifier(self.revision, r"[A-Za-z0-9_.:-]{1,160}", 160)
        _counter(self.epoch)
        _counter(self.login_generation, positive=True)


def verify_system_status(payload, *, expected_sysid, expected_client,
                         revision, epoch, login_generation, expected_username=None):
    """Verify an exact fresh collector result, not arbitrary page/model data.

    Expectations come from the trusted scene binding. ``expected_username``
    can be the configured MCP account to require matching browser login, but
    that comparison still does not turn gateway metadata into a SAP-verified
    MCP identity. Unknown screen layouts must result in no evidence, not a
    guessed payload assembled from unrelated fields.
    """
    _identifier(revision, r"[A-Za-z0-9_.:-]{1,160}", 160)
    _counter(epoch)
    _counter(login_generation, positive=True)
    if payload is None:
        raise WorkbenchError("sap_identity_unverified", 409)
    if (not isinstance(payload, dict) or set(payload) != _EVIDENCE_KEYS
            or payload.get("source") != "system_status"):
        raise WorkbenchError("sap_identity_unverified", 409)
    # A late response must be discarded before binding a new identity.
    if (payload.get("revision") != revision or type(payload.get("epoch")) is not int
            or payload["epoch"] != epoch or type(payload.get("login_generation")) is not int
            or payload["login_generation"] != login_generation):
        raise WorkbenchError("sap_identity_stale", 409)
    actual = _account(payload["sysid"], payload["client"], payload["username"])
    expected = (
        _identifier(expected_sysid, r"[A-Za-z0-9]{3}", 3).upper(),
        _identifier(expected_client, r"[0-9]{3}", 3),
    )
    if actual[:2] != expected:
        raise WorkbenchError("sap_identity_mismatch", 409)
    if expected_username is not None and actual[2] != _identifier(
            expected_username, r"[A-Za-z0-9_.@/-]{1,64}", 64).upper():
        raise WorkbenchError("sap_identity_mismatch", 409)
    return VerifiedSapIdentity(*actual, revision, epoch, login_generation)


@dataclass(frozen=True)
class VerifiedLoginBinding:
    identity: VerifiedSapIdentity
    config_version: int
    credential_ref: str = field(repr=False)
    mcp_connection_ids: tuple = field(repr=False)

    def __post_init__(self):
        if type(self.identity) is not VerifiedSapIdentity:
            raise WorkbenchError("sap_identity_binding_invalid", 409)
        _counter(self.config_version, positive=True)
        _reference(self.credential_ref)
        if (type(self.mcp_connection_ids) is not tuple or not self.mcp_connection_ids
                or len(self.mcp_connection_ids) > len(MCP_ENDPOINTS)):
            raise WorkbenchError("sap_identity_binding_invalid", 409)
        names = set()
        for item in self.mcp_connection_ids:
            if (type(item) is not tuple or len(item) != 2 or not isinstance(item[0], str)
                    or item[0] not in MCP_ENDPOINTS or item[0] in names):
                raise WorkbenchError("sap_identity_binding_invalid", 409)
            names.add(item[0])
            _reference(item[1])

    def status(self):
        """Small state projection; no principal, credential or connection handle."""
        return {"browser_identity_verified": True,
                "login_generation": self.identity.login_generation,
                "mcp_credential_source": "scene_config",
                "mcp_identity_verified": False}


def bind_verified_login(identity, *, config_version, credential_ref, mcp_connection_ids):
    """Bind trusted internal handles; no network authentication is performed."""
    if not isinstance(mcp_connection_ids, dict):
        raise WorkbenchError("sap_identity_binding_invalid", 409)
    # The immutable copy prevents later caller mutation changing the binding.
    pairs = tuple(mcp_connection_ids.items())
    return VerifiedLoginBinding(identity, config_version, credential_ref, pairs)


def require_verified_login(binding, observed, *, config_version, credential_ref,
                           mcp_connection_ids):
    """Check the complete private binding before use and after an awaited call.

    Failure requires the caller to pause automatic control, discard the old
    result, and revoke old gateway handles; this pure function performs no
    cleanup. Revisions may advance during the same verified login, but epoch
    or login generation changes invalidate a previously issued control lease.
    """
    if type(binding) is not VerifiedLoginBinding:
        raise WorkbenchError("sap_identity_unverified", 409)
    if observed is None:
        raise WorkbenchError("sap_identity_unverified", 409)
    current = bind_verified_login(observed, config_version=config_version,
                                  credential_ref=credential_ref, mcp_connection_ids=mcp_connection_ids)
    if (current.config_version != binding.config_version
            or current.credential_ref != binding.credential_ref
            or dict(current.mcp_connection_ids) != dict(binding.mcp_connection_ids)):
        raise WorkbenchError("sap_identity_binding_changed", 409)
    old, new = binding.identity, current.identity
    if (old.sysid, old.client, old.username, old.epoch, old.login_generation) != (
            new.sysid, new.client, new.username, new.epoch, new.login_generation):
        raise WorkbenchError("sap_login_changed", 409)
    return current


def relogin_plan(login_generation):
    """Internal plan for explicit manual re-login; it does not log the user in.

    The consumer must atomically advance the generation, pause/revoke old
    browser control and MCP handles, and open only its bound SAP login target.
    Afterwards it must freshly collect and verify status before re-enabling
    the optional guard. Browser passwords never become MCP configuration.
    """
    _counter(login_generation)
    if login_generation == _MAX_GENERATION:
        raise WorkbenchError("sap_identity_generation_exhausted", 409)
    return {"login_generation": login_generation + 1, "control": "manual",
            "browser_identity_verified": False, "revoke_mcp_connections": True,
            "mcp_credential_source": "scene_config", "credential_transfer": False}
