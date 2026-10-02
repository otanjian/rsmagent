"""Where a session's tools run, and under what authorization.

Change ``align-desktop-project-execution-with-master``. A session has always had
exactly one implicit execution location: the server (the Agent's workspace, or a
server-side project directory). "打开本机项目" adds a second one, so the choice
has to become an explicit, validated value instead of being inferred from
whichever path happens to be set.

Two properties this module exists to guarantee:

1. **The location is data, never a client-supplied path.** The desktop target
   carries identifiers (device / workspace / binding / grant version) and a
   purpose. The absolute root stays on the client; the local backend resolves it
   from its own registry (see ``agent/desktop_local``). A model or a page can
   never put a path here.
2. **A read-only reference is not an execution grant.** ``project_mode``
   distinguishes ``readonly-input`` (the phase-2 local file reference) from
   ``project-execution`` (the explicit "open my project here" authorization).
   Only the latter permits project tools and Skill scripts locally.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Optional

LOCATION_BACKEND = "backend"
LOCATION_DESKTOP = "desktop"

MODE_READONLY_INPUT = "readonly-input"
MODE_PROJECT_EXECUTION = "project-execution"

_LOCATIONS = (LOCATION_BACKEND, LOCATION_DESKTOP)
_MODES = (MODE_READONLY_INPUT, MODE_PROJECT_EXECUTION)

# Identifier shape: opaque, non-secret, and bounded. The server mints these; a
# value that does not look like one is a bug or a forgery, not a path.
_MAX_ID = 128


class ExecutionTargetError(ValueError):
    """A target that cannot be trusted, with a stable code for the caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _clean_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > _MAX_ID:
        raise ExecutionTargetError("invalid_request", "identifier is too long")
    return text


def _clean_int(value: Any, *, minimum: int = 0) -> int:
    if value is None or value == "":
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ExecutionTargetError("invalid_request", "version is not an integer")
    if number < minimum:
        raise ExecutionTargetError("invalid_request", "version is out of range")
    return number


@dataclass(frozen=True)
class ExecutionTarget:
    """The immutable execution location for one session (and one run).

    ``backend`` is the pre-existing behaviour and is the default, so every
    session that predates this change keeps working unchanged.
    """

    location: str = LOCATION_BACKEND
    project_mode: str = MODE_READONLY_INPUT
    # Desktop-only, opaque identifiers. Never a path.
    device_id: str = ""
    workspace_id: str = ""
    binding_id: str = ""
    grant_version: int = 0
    selection_generation: int = 0

    def __post_init__(self) -> None:
        if self.location not in _LOCATIONS:
            raise ExecutionTargetError("invalid_request", f"unknown location: {self.location!r}")
        if self.project_mode not in _MODES:
            raise ExecutionTargetError("invalid_request", f"unknown project mode: {self.project_mode!r}")
        if self.location == LOCATION_DESKTOP:
            missing = [name for name, value in (
                ("device_id", self.device_id),
                ("workspace_id", self.workspace_id),
                ("binding_id", self.binding_id),
            ) if not value]
            if missing:
                raise ExecutionTargetError(
                    "invalid_request",
                    "a desktop target needs " + ", ".join(missing),
                )
        else:
            # A backend target is a statement about location only. Carrying
            # desktop identifiers on it would be a half-migrated record that
            # could be read as a grant; refuse it instead.
            if self.device_id or self.workspace_id or self.binding_id:
                raise ExecutionTargetError(
                    "invalid_request",
                    "a backend target must not carry desktop identifiers",
                )

    # -- predicates the gates read -------------------------------------------

    @property
    def is_desktop(self) -> bool:
        return self.location == LOCATION_DESKTOP

    @property
    def allows_project_execution(self) -> bool:
        """True only for an explicit local project-execution authorization."""
        return self.location == LOCATION_DESKTOP and self.project_mode == MODE_PROJECT_EXECUTION

    @property
    def is_readonly_input(self) -> bool:
        return self.location == LOCATION_DESKTOP and self.project_mode == MODE_READONLY_INPUT

    def with_grant_version(self, grant_version: int) -> "ExecutionTarget":
        return replace(self, grant_version=_clean_int(grant_version))

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "location": self.location,
            "project_mode": self.project_mode,
            "device_id": self.device_id,
            "workspace_id": self.workspace_id,
            "binding_id": self.binding_id,
            "grant_version": self.grant_version,
            "selection_generation": self.selection_generation,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["ExecutionTarget"]:
        """Parse a stored/requests value, or None when there is nothing to parse.

        ``None`` means "no desktop target": the session falls back to whatever
        the pre-existing rules resolve, which is how every legacy record (and
        every non-desktop deployment) is interpreted.
        """
        if raw is None:
            return None
        if isinstance(raw, ExecutionTarget):
            return raw
        if not isinstance(raw, dict):
            raise ExecutionTargetError("invalid_request", "execution target must be an object")
        location = str(raw.get("location") or LOCATION_BACKEND)
        if location == LOCATION_BACKEND:
            # A stored backend target is a valid, ordinary value, but it is the
            # same thing as having none; normalise so callers have one shape.
            return None
        return cls(
            location=location,
            project_mode=str(raw.get("project_mode") or MODE_READONLY_INPUT),
            device_id=_clean_id(raw.get("device_id")),
            workspace_id=_clean_id(raw.get("workspace_id")),
            binding_id=_clean_id(raw.get("binding_id")),
            grant_version=_clean_int(raw.get("grant_version")),
            selection_generation=_clean_int(raw.get("selection_generation")),
        )


# What "no desktop target" looks like as a value (never stored, only compared).
BACKEND_TARGET = ExecutionTarget()


def desktop_target(
    *,
    device_id: str,
    workspace_id: str,
    binding_id: str,
    project_mode: str = MODE_READONLY_INPUT,
    grant_version: int = 0,
    selection_generation: int = 0,
) -> ExecutionTarget:
    """Build a validated desktop target from identifiers already verified
    against the live binding.

    Callers must pass identifiers that came from the server's own records (or a
    verified reference), never from a request body the model could influence.
    """
    return ExecutionTarget(
        location=LOCATION_DESKTOP,
        project_mode=project_mode,
        device_id=_clean_id(device_id),
        workspace_id=_clean_id(workspace_id),
        binding_id=_clean_id(binding_id),
        grant_version=_clean_int(grant_version),
        selection_generation=_clean_int(selection_generation),
    )
