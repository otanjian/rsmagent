"""Short-lived browser grants for the operator's same-origin OpenCode proxy.

The grant is bound to an existing platform login, tenant, Agent and owned
conversation. It is not an upstream credential. The proxy rechecks that login
and its grants for every request; logout and permission revocation take effect
without waiting for this cookie to expire.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit

COOKIE_PREFIX = "rsm_coding_"
TTL = 3600
API_PATH = "/coding-api"


def _sign(key: str, value: str) -> str:
    return hmac.new(key.encode(), ("coding-browser-v1:" + value).encode(), hashlib.sha256).hexdigest()


def issue(key: str, token: str, *, tenant: str, agent: str, session: str,
          service: str, now=None) -> tuple[str, str]:
    now = int(time.time() if now is None else now)
    claims = {"tenant": tenant, "agent": agent, "session": session, "service": service,
              "login": hashlib.sha256(token.encode()).hexdigest(), "exp": now + TTL}
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
    name = COOKIE_PREFIX + hashlib.sha256((tenant + "\0" + agent).encode()).hexdigest()[:16]
    return name, payload + "." + _sign(key, payload)


def verify(key: str, token: str, value: str, *, service: str, now=None):
    if not key or not token or not isinstance(value, str) or len(value) > 4096:
        return None
    payload, sep, signature = value.partition(".")
    if not sep or not hmac.compare_digest(_sign(key, payload), signature):
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if (not isinstance(claims, dict) or claims.get("service") != service
                or not isinstance(claims.get("exp"), int)
                or claims["exp"] <= (time.time() if now is None else now)
                or claims["exp"] > (time.time() if now is None else now) + TTL
                or not hmac.compare_digest(claims.get("login", ""), hashlib.sha256(token.encode()).hexdigest())
                or any(not isinstance(claims.get(field), str) or not claims[field]
                       for field in ("tenant", "agent", "session"))):
            return None
        return claims
    except (ValueError, TypeError):
        return None


def same_origin(web_url: str, origin: str) -> bool:
    target = urlsplit(web_url)
    source = urlsplit(origin)
    return (target.scheme in {"http", "https"} and target.scheme == source.scheme
            and target.netloc == source.netloc and not target.username and not target.password
            and origin == source.scheme + "://" + source.netloc
            and not source.username and not source.password)
