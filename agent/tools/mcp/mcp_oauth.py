"""
MCP OAuth 2.1 client (authorization code + PKCE) with zero external deps.

Implements the subset of the MCP authorization spec needed to connect to
remote MCP servers that guard their endpoint behind OAuth (e.g. Xmind):

  1. Metadata discovery via RFC 9728 (protected-resource) + RFC 8414
     (authorization-server) .well-known documents.
  2. Dynamic Client Registration (RFC 7591) to obtain a client_id.
  3. PKCE (RFC 7636, S256) authorization-code flow.
  4. Token exchange + refresh, persisted to ~/.cow/mcp_oauth.json.

The actual browser round-trip is completed out-of-band: McpClient generates
an authorization URL, the user opens it, and the web console callback
(/mcp/oauth/callback) feeds the returned code back into finish_authorization().
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Callable, Optional

from common.log import logger


# ------------------------------------------------------------------
# Token store: ~/.cow/mcp_oauth.json  {server_name: {...credentials...}}
# ------------------------------------------------------------------

_STORE_LOCK = threading.Lock()


def _store_path() -> str:
    base = os.path.expanduser("~/.cow")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    return os.path.join(base, "mcp_oauth.json")


def _load_store() -> dict:
    path = _store_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"[MCP-OAuth] Failed to read token store: {e}")
        return {}


def _save_store(store: dict) -> None:
    path = _store_path()
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        # Credentials file: restrict to owner read/write when possible.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as e:
        logger.warning(f"[MCP-OAuth] Failed to persist token store: {e}")


def load_server_record(server_name: str) -> dict:
    with _STORE_LOCK:
        record = _load_store().get(server_name, {})
        return dict(record) if isinstance(record, dict) else {}


def save_server_record(server_name: str, record: dict) -> None:
    with _STORE_LOCK:
        store = _load_store()
        store[server_name] = record
        _save_store(store)


def clear_server_record(server_name: str) -> None:
    with _STORE_LOCK:
        store = _load_store()
        if server_name in store:
            store.pop(server_name, None)
            _save_store(store)


# ------------------------------------------------------------------
# Pending authorizations, keyed by the OAuth `state` param.
# Populated when an authorization URL is generated; consumed by the
# web callback when the browser redirects back with ?code&state.
# ------------------------------------------------------------------

_PENDING_LOCK = threading.Lock()
#: state -> {"handler": OAuthHandler, "created": ts, "binding": {...}}.
#: ``binding`` is the record ``integrations.external.oauth_binding`` builds: who
#: started the flow, for which connection, at which configuration version. It is
#: kept here rather than only on the handler so a callback can be *refused*
#: before the handler is reached, and the state is consumed either way.
_PENDING: dict = {}
_PENDING_TTL = 600  # seconds


def _register_pending(state: str, handler: "OAuthHandler",
                      binding: Optional[dict] = None) -> None:
    with _PENDING_LOCK:
        _prune_pending_locked()
        _PENDING[state] = {"handler": handler, "created": time.time(),
                           "binding": dict(binding or {})}


def _prune_pending_locked() -> None:
    now = time.time()
    stale = [s for s, v in _PENDING.items() if now - v["created"] > _PENDING_TTL]
    for s in stale:
        _PENDING.pop(s, None)


def pop_pending(state: str) -> Optional["OAuthHandler"]:
    """Pop the pending handler for ``state``, without verifying its binding.

    Kept for callers that have already established the binding themselves, and
    for the existing tests. Prefer :func:`take_pending`: a callback that pops
    without verifying is exactly the gap this was built to close.
    """
    return take_pending(state)


def pending_binding(state: str) -> Optional[dict]:
    """The binding recorded when ``state`` was issued, if it is still live."""
    with _PENDING_LOCK:
        _prune_pending_locked()
        entry = _PENDING.get(state)
    return dict(entry["binding"]) if entry else None


def take_pending(state: str, verify: Optional[Callable[[dict], tuple]] = None
                 ) -> Optional["OAuthHandler"]:
    """Pop a pending authorization, optionally refusing it if it no longer fits.

    ``verify`` receives the recorded binding and returns ``(ok, reason)``. The
    pop happens **before** the verification and unconditionally, which is
    deliberate: a callback that fails verification has consumed its state, so a
    refused authorization cannot be retried against the same one-time value.

    Returns ``None`` both for an unknown/expired state and for a refused one. The
    distinction is logged and not propagated: a callback that distinguished them
    in its response would tell an attacker whether a state value ever existed.
    """
    with _PENDING_LOCK:
        _prune_pending_locked()
        entry = _PENDING.pop(state, None)
    if not entry:
        return None
    if verify is not None:
        try:
            ok, reason = verify(dict(entry.get("binding") or {}))
        except Exception as exc:  # noqa: BLE001 - a broken check is a refusal
            logger.warning("[MCP-OAuth] callback verification failed: %s", exc)
            return None
        if not ok:
            logger.warning("[MCP-OAuth] callback refused: %s", reason)
            return None
    return entry["handler"]


def has_pending() -> bool:
    with _PENDING_LOCK:
        _prune_pending_locked()
        return bool(_PENDING)


# ------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ------------------------------------------------------------------

_UA = "RongAI-MCP-OAuth/1.0"


def _http_get_json(url: str, timeout: int = 15) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        logger.debug(f"[MCP-OAuth] GET {url} -> HTTP {e.code}")
        return None
    except Exception as e:
        logger.debug(f"[MCP-OAuth] GET {url} failed: {e}")
        return None


def _http_post_form(url: str, fields: dict, timeout: int = 20) -> dict:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _http_post_json(url: str, payload: dict, timeout: int = 20) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


# ------------------------------------------------------------------
# Discovery (RFC 9728 + RFC 8414)
# ------------------------------------------------------------------

def _origin(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def discover_metadata(resource_url: str, www_authenticate: str = "") -> Optional[dict]:
    """
    Resolve the authorization server metadata for a protected MCP resource.

    Returns a dict with at least authorization_endpoint + token_endpoint,
    plus registration_endpoint when the server supports DCR. Returns None
    when discovery fails.
    """
    as_metadata_url = _parse_resource_metadata_url(www_authenticate)

    # 1) Protected-resource metadata (RFC 9728) to locate the auth server.
    auth_server = None
    prm = None
    if as_metadata_url:
        prm = _http_get_json(as_metadata_url)
    if prm is None:
        origin = _origin(resource_url)
        prm = _http_get_json(f"{origin}/.well-known/oauth-protected-resource")
    if prm and isinstance(prm.get("authorization_servers"), list) and prm["authorization_servers"]:
        auth_server = prm["authorization_servers"][0]

    # 2) Authorization-server metadata (RFC 8414). Fall back to the resource
    #    origin when the resource did not advertise a separate auth server.
    base = auth_server or _origin(resource_url)
    asm = _fetch_as_metadata(base)
    if not asm:
        return None

    if not asm.get("authorization_endpoint") or not asm.get("token_endpoint"):
        logger.warning("[MCP-OAuth] Authorization server metadata missing required endpoints")
        return None

    # Derive the scope to request. Prefer the resource's required_scopes
    # (RFC 9728), then its scopes_supported, then the auth server's
    # scopes_supported. Stored so callers don't have to configure it.
    discovered_scope = ""
    if prm:
        scopes = prm.get("required_scopes") or prm.get("scopes_supported")
        if isinstance(scopes, list) and scopes:
            discovered_scope = " ".join(str(s) for s in scopes)
    if not discovered_scope and isinstance(asm.get("scopes_supported"), list) and asm["scopes_supported"]:
        discovered_scope = " ".join(str(s) for s in asm["scopes_supported"])
    if discovered_scope:
        asm["_discovered_scope"] = discovered_scope
    return asm


def _parse_resource_metadata_url(www_authenticate: str) -> Optional[str]:
    """Extract resource_metadata="..." from a WWW-Authenticate: Bearer header."""
    if not www_authenticate:
        return None
    # naive but sufficient parse for `resource_metadata="URL"`
    marker = "resource_metadata="
    idx = www_authenticate.find(marker)
    if idx < 0:
        return None
    rest = www_authenticate[idx + len(marker):].strip()
    if rest.startswith('"'):
        end = rest.find('"', 1)
        return rest[1:end] if end > 0 else None
    # unquoted, up to comma/space
    for sep in (",", " "):
        if sep in rest:
            rest = rest.split(sep, 1)[0]
    return rest or None


def _fetch_as_metadata(base: str) -> Optional[dict]:
    """Try both RFC 8414 and OIDC well-known locations."""
    base = base.rstrip("/")
    candidates = [
        f"{base}/.well-known/oauth-authorization-server",
        f"{base}/.well-known/openid-configuration",
    ]
    for url in candidates:
        data = _http_get_json(url)
        if data and data.get("authorization_endpoint"):
            return data
    return None


# ------------------------------------------------------------------
# PKCE
# ------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_pkce() -> tuple:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ------------------------------------------------------------------
# OAuthHandler: per-server OAuth state machine
# ------------------------------------------------------------------

class OAuthHandler:
    """Drives the OAuth flow and token lifecycle for a single MCP server."""

    def __init__(self, server_name: str, resource_url: str, redirect_uri: str,
                 scope: str = "", client_name: str = "容大AI",
                 binding: Optional[dict] = None):
        self.server_name = server_name
        self.resource_url = resource_url
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.client_name = client_name
        #: What this authorization is bound to, from
        #: ``integrations.external.oauth_binding``. Empty for a plain
        #: ``mcp.json`` server, which has no connection row to bind to; an
        #: external connection always supplies one.
        self.binding: dict = dict(binding or {})

        rec = load_server_record(server_name)
        self.metadata: dict = rec.get("metadata", {})
        self.client_id: Optional[str] = rec.get("client_id")
        self.client_secret: Optional[str] = rec.get("client_secret")
        self.access_token: Optional[str] = rec.get("access_token")
        self.refresh_token: Optional[str] = rec.get("refresh_token")
        self.expires_at: float = float(rec.get("expires_at", 0) or 0)
        # A record written before bindings existed has none, so the stored one
        # is only adopted when the caller did not bring a fresher one.
        if not self.binding:
            self.binding = dict(rec.get("binding") or {})
        self._verifier: Optional[str] = None

    @property
    def bound_config_version(self) -> int:
        """The connection version this authorization was started against, or 0."""
        try:
            return int(self.binding.get("config_version", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def is_current(self, config_version: int) -> bool:
        """Whether a stored authorization still belongs to this connection version.

        A token minted against version *n* must not be presented for version
        *n+1*: the operator may have changed the endpoint, the client id or the
        scope, and the old grant says nothing about the new configuration. An
        unbound handler (a plain ``mcp.json`` server) is always current, because
        it has no version to disagree with.
        """
        bound = self.bound_config_version
        if not bound:
            return True
        return bound == int(config_version or 0)

    # --- persistence -------------------------------------------------

    def _persist(self) -> None:
        save_server_record(self.server_name, {
            "resource_url": self.resource_url,
            "metadata": self.metadata,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "binding": dict(self.binding),
        })

    # --- token access ------------------------------------------------

    def get_valid_access_token(self, leeway: int = 60) -> Optional[str]:
        """Return a usable access token, refreshing proactively when near expiry."""
        if not self.access_token:
            return None
        if self.expires_at and time.time() >= self.expires_at - leeway:
            if not self.refresh():
                return None
        return self.access_token

    def refresh(self) -> bool:
        """Refresh the access token using the stored refresh token."""
        if not self.refresh_token or not self.metadata.get("token_endpoint"):
            return False
        fields = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id or "",
        }
        if self.client_secret:
            fields["client_secret"] = self.client_secret
        try:
            resp = _http_post_form(self.metadata["token_endpoint"], fields)
        except Exception as e:
            logger.warning(f"[MCP-OAuth:{self.server_name}] refresh failed: {e}")
            return False
        return self._absorb_token_response(resp)

    # --- authorization-code flow ------------------------------------

    def ensure_registered(self, www_authenticate: str = "") -> bool:
        """Discover metadata + register a client if not already done."""
        if not self.metadata.get("authorization_endpoint"):
            meta = discover_metadata(self.resource_url, www_authenticate)
            if not meta:
                return False
            self.metadata = meta
        # Adopt the scope discovered from metadata when the user didn't set one.
        if not self.scope and self.metadata.get("_discovered_scope"):
            self.scope = self.metadata["_discovered_scope"]
            logger.info(f"[MCP-OAuth:{self.server_name}] Using discovered scope: {self.scope}")
        if not self.client_id:
            if not self._register_client():
                return False
        self._persist()
        return True

    def _register_client(self) -> bool:
        reg_endpoint = self.metadata.get("registration_endpoint")
        if not reg_endpoint:
            logger.warning(
                f"[MCP-OAuth:{self.server_name}] No registration_endpoint; "
                f"DCR unavailable. Provide client_id manually."
            )
            return False
        payload = {
            "client_name": self.client_name,
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        if self.scope:
            payload["scope"] = self.scope
        try:
            resp = _http_post_json(reg_endpoint, payload)
        except Exception as e:
            logger.warning(f"[MCP-OAuth:{self.server_name}] DCR failed: {e}")
            return False
        client_id = resp.get("client_id")
        if not client_id:
            logger.warning(f"[MCP-OAuth:{self.server_name}] DCR returned no client_id")
            return False
        self.client_id = client_id
        self.client_secret = resp.get("client_secret")
        logger.info(f"[MCP-OAuth:{self.server_name}] Registered client_id={client_id}")
        return True

    def build_authorization_url(self) -> Optional[str]:
        """Create an authorization URL and register this handler as pending.

        The pending record carries this handler's :attr:`binding`, so the
        callback can refuse a completion that no longer matches the request that
        started it (see ``integrations.external.oauth_binding``).
        """
        if not self.metadata.get("authorization_endpoint") or not self.client_id:
            return None
        self._verifier, challenge = _make_pkce()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if self.scope:
            params["scope"] = self.scope
        # Advertise the resource we intend to access (RFC 8707).
        params["resource"] = self.resource_url
        _register_pending(state, self, binding=self.binding)
        return f"{self.metadata['authorization_endpoint']}?{urllib.parse.urlencode(params)}"

    def revoke(self) -> bool:
        """Forget this server's stored authorization.

        ``clear_server_record`` already existed but had no caller, so there was
        no way to revoke: a token stayed usable after the reason for it went
        away. Clearing the record is what makes the next call re-authorize
        (``authorization_required``) instead of reusing the old grant, which is
        认证失败 SHALL 要求重新授权, 不返回令牌或沿用失效会话.

        Returns whether anything was actually cleared, so a caller can report
        "already revoked" rather than claiming a change it did not make.
        """
        existed = bool(self.access_token or self.refresh_token or self.client_id)
        self.access_token = None
        self.refresh_token = None
        self.expires_at = 0.0
        self._verifier = None
        try:
            clear_server_record(self.server_name)
        except Exception as exc:  # noqa: BLE001 - report, do not raise
            logger.warning(f"[MCP-OAuth:{self.server_name}] revoke failed: {exc}")
            return False
        logger.info(f"[MCP-OAuth:{self.server_name}] authorization revoked")
        return existed

    def finish_authorization(self, code: str) -> bool:
        """Exchange an authorization code for tokens."""
        if not self.metadata.get("token_endpoint") or not self._verifier:
            return False
        fields = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id or "",
            "code_verifier": self._verifier,
            "resource": self.resource_url,
        }
        if self.client_secret:
            fields["client_secret"] = self.client_secret
        try:
            resp = _http_post_form(self.metadata["token_endpoint"], fields)
        except Exception as e:
            logger.warning(f"[MCP-OAuth:{self.server_name}] token exchange failed: {e}")
            return False
        ok = self._absorb_token_response(resp)
        self._verifier = None
        return ok

    def _absorb_token_response(self, resp: dict) -> bool:
        access = resp.get("access_token")
        if not access:
            logger.warning(f"[MCP-OAuth:{self.server_name}] token response missing access_token: {resp}")
            return False
        self.access_token = access
        if resp.get("refresh_token"):
            self.refresh_token = resp["refresh_token"]
        expires_in = resp.get("expires_in")
        self.expires_at = time.time() + int(expires_in) if expires_in else 0
        self._persist()
        logger.info(f"[MCP-OAuth:{self.server_name}] Access token stored")
        return True
