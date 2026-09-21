"""Coding agents: entry points into a shared OpenCode service.

An Agent of type ``coding`` does not run the platform's normal runtime. It is a
Web entry point: the platform keeps only the Agent's configuration, a light
link between a platform session and an OpenCode session, and a rebuildable list
cache. OpenCode stays the only authority for the session body, its title and
its execution state.

This module owns the shared vocabulary every other module imports — the stable
error codes callers switch on, and the resolved global service settings — so
that a status code means the same thing in the HTTP layer, the initializer and
the tests.

Deliberately absent: a provider abstraction, an event bus, a worker queue and a
second session store. The scope is one operator-configured service, reached
over plain HTTP, with the existing conversation database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional


# Stable error codes. Callers (console, tests, other modules) must branch on
# these rather than on the human-readable message.
CODING_DISABLED = "coding_disabled"
CODING_WEB_ONLY = "coding_web_only"
CODING_SERVICE_CHANGED = "coding_service_changed"
CODING_UPSTREAM_UNAVAILABLE = "coding_upstream_unavailable"
CODING_NOT_LINKED = "coding_not_linked"
#: The request itself is unusable: a missing field, or an endpoint addressed at
#: an Agent that is not a coding Agent at all.
CODING_INVALID_REQUEST = "coding_invalid_request"
#: ``attach`` found the remote session in a different project than its source.
CODING_PROJECT_MISMATCH = "coding_project_mismatch"

#: The codes that mean "the capability cannot do this right now, but the user's
#: own session is fine". None of these are 401/403, which the platform reserves
#: for the caller's own identity.
CODING_FAILURE_CODES = frozenset({
    CODING_DISABLED,
    CODING_SERVICE_CHANGED,
    CODING_UPSTREAM_UNAVAILABLE,
})


class CodingError(Exception):
    """A coding request that must fail with a stable code and status.

    ``status`` is the HTTP status the Web layer reports: 400 for a bad request
    (wrong agent type, unusable input), 404 when the target is not a linked
    coding session, 409 when the configured service instance changed, 503 when
    the capability is switched off, and 502/504 for upstream failures. None of
    these reuse 401/403, which the platform reserves for the caller's own
    session so a broken OpenCode service can never log a user out.
    """

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def coding_web_only(agent_id: str = "") -> CodingError:
    """A coding Agent was submitted to a path that only runs normal Agents."""
    where = f"'{agent_id}'" if agent_id else "this agent"
    return CodingError(
        CODING_WEB_ONLY,
        f"coding agent {where} can only be used from the Web coding entry",
        400,
    )


def coding_disabled() -> CodingError:
    return CodingError(
        CODING_DISABLED,
        "the shared coding service is not enabled on this instance",
        503,
    )


def coding_service_changed() -> CodingError:
    return CodingError(
        CODING_SERVICE_CHANGED,
        "the configured coding service is a different data instance",
        409,
    )


@dataclass(frozen=True)
class CodingSettings:
    """Resolved global OpenCode settings, with the password kept out of them.

    ``password`` is a live read of the environment variable named by
    ``password_env`` at the moment the settings are resolved; it is never
    written back into configuration, a URL, a session row or a log line.
    """

    enabled: bool = False
    service_id: str = "default"
    api_url: str = "http://127.0.0.1:4096"
    web_url: str = ""
    username: str = "opencode"
    password_env: str = "RSM_OPENCODE_PASSWORD"
    password: Optional[str] = None

    @property
    def configured(self) -> bool:
        """Whether a usable service address exists at all.

        Kept separate from ``enabled``: an instance may keep the capability off
        while still holding a valid address, and the console has to be able to
        say "configured but disabled" rather than "not configured".
        """
        return bool(self.api_url and self.web_url)

    def auth_headers(self) -> dict:
        if not self.password:
            return {}
        import base64

        raw = f"{self.username or 'opencode'}:{self.password}".encode("utf-8")
        return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "on")
    return bool(value)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def resolve_settings(raw: Optional[Mapping[str, Any]] = None) -> CodingSettings:
    """Resolve the global ``opencode`` block, defaulting to "off".

    A missing block, a missing key or an unparsable value all resolve to the
    closed default rather than raising: an older config file is a supported
    input, and a typo in this block must not take down an instance whose normal
    Agents do not depend on it at all.
    """
    if raw is None:
        try:
            from config import conf

            raw = conf().get("opencode")
        except Exception:  # pragma: no cover - defensive, config may not be loaded
            raw = None
    if not isinstance(raw, Mapping):
        raw = {}
    password_env = _as_str(raw.get("password_env"), "RSM_OPENCODE_PASSWORD")
    password = os.environ.get(password_env) or None
    return CodingSettings(
        enabled=_as_bool(raw.get("enabled"), False),
        service_id=_as_str(raw.get("service_id"), "default"),
        api_url=_as_str(raw.get("api_url"), "http://127.0.0.1:4096").rstrip("/"),
        web_url=_as_str(raw.get("web_url"), "").rstrip("/"),
        username=_as_str(raw.get("username"), "opencode"),
        password_env=password_env,
        password=password,
    )


def settings_for_console(raw: Optional[Mapping[str, Any]] = None) -> dict:
    """The read-only projection of the service shown to a *browser*.

    Deliberately absent, by construction rather than by filtering:

    * ``password`` and ``password_env`` — the secret, and the name of the
      variable that holds it;
    * ``username`` — the other half of the upstream credential. The console
      renders a status line, and a status line needs no login name;
    * ``api_url`` — the *server's* address. A browser must be given ``web_url``;
      handing out the backend address is how ``http://localhost:4096`` ends up
      as a production iframe ``src``.

    An operator still edits all of these on the configuration page, which is
    rendered from the editable schema and authenticated separately.
    """
    settings = resolve_settings(raw)
    return {
        "enabled": settings.enabled,
        "service_id": settings.service_id,
        "web_url": settings.web_url,
        "configured": settings.configured,
    }
