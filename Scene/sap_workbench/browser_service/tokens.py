"""Short-lived screen-view tokens for the SAP workbench browser pane.

The pane talks to a loopback WebSocket gateway, not the WSGI app, so the normal
session cookie never reaches that socket. Instead the authenticated scene
endpoint mints a single token bound to the real ``(tenant, user)`` that asked
for the pane, and the gateway only accepts that token. Tokens are deliberately
short-lived and revocable so a leaked URL is useless after the view closes.
"""
from __future__ import annotations

import secrets
import threading
import time


class ViewTokens:
    """Thread-safe store of unguessable, expiring, revocable view tokens."""

    def __init__(self, ttl: float = 60.0, clock=time.monotonic):
        self._ttl = float(ttl)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}

    def issue(self, tenant_id: str, user_id: str, *, generation: int = 1, **extra) -> str:
        token = secrets.token_urlsafe(32)
        claims = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "generation": int(generation),
            "expires_at": self._clock() + self._ttl,
        }
        claims.update(extra)
        with self._lock:
            self._purge_locked()
            self._entries[token] = claims
        return token

    def verify(self, token: str, *, tenant_id=None, user_id=None):
        """Return the token's claims, or ``None`` if unusable.

        ``tenant_id``/``user_id``, when given, must match the token exactly: a
        token minted for one user must never open another user's pane.
        """
        if not token:
            return None
        with self._lock:
            self._purge_locked()
            claims = self._entries.get(token)
            if claims is None:
                return None
            if tenant_id is not None and claims["tenant_id"] != tenant_id:
                return None
            if user_id is not None and claims["user_id"] != user_id:
                return None
            return dict(claims)

    def revoke(self, token: str) -> None:
        with self._lock:
            self._entries.pop(token, None)

    def _purge_locked(self) -> None:
        now = self._clock()
        for token in [t for t, c in self._entries.items() if c["expires_at"] <= now]:
            self._entries.pop(token, None)
