"""What this deployment may *claim* about project execution v2.

Change ``align-desktop-project-execution-with-master``, task 6.1.

One place composes the answer that ``GET /api/desktop/meta`` (and the device
``hello``) report, so the three inputs cannot drift apart:

* the declaration (:mod:`auth.capability_matrix`) -- implemented and accepted;
* the deployment switch (``desktop_project_execution_enabled`` /
  ``desktop_project_scripts_enabled``, both off by default);
* the platform the *execution end* actually supports, from the v2 contract.

Only the intersection becomes ``available``. A capability that is declared but
not yet accepted reports ``not_accepted``; one whose switch is off reports
``disabled_by_deployment``; a platform with no launcher reports
``platform_unsupported``. Scripts are additionally gated on their own switch and
on a script tool being in the advertised tool set, so the meta block can never
promise ``bash`` while the file-tool switch alone is on.

This module reports; it never authorizes. Every broker call re-checks identity,
membership, binding ownership, grant version, approval and quota.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from auth import capability_matrix
from auth import desktop_contracts_v2 as v2

#: Public capability names the meta/hello block uses.
FILES_CAPABILITY = "desktop_project_execution"
SCRIPTS_CAPABILITY = "desktop_project_scripts"

#: Deployment switches, one per surface (``config.available_setting``).
FILES_SWITCH = "desktop_project_execution_enabled"
SCRIPTS_SWITCH = "desktop_project_scripts_enabled"


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes", "on")
    return False


def _switch(settings: Any, key: str) -> bool:
    """Read a deployment switch, preferring the live value.

    A key that was never written falls back to the declaration so the answer is
    the declared default (off) rather than a silent ``None``. An unparseable
    value means *closed*, the same convention the v1 phase switches use.
    """
    from config import available_setting

    if settings is not None and key in settings:
        raw = settings[key]
    else:
        raw = available_setting.get(key, False)
    return _as_bool(raw)


def execution_switch_open() -> bool:
    """Whether this deployment hands project execution to devices at all.

    The read side of the ``desktop_project_execution`` switch, for the runtime
    seam that decides *which machine* a project call goes to
    (``agent.desktop_remote.mode``). It reports; it never authorizes -- every
    brokered call re-checks identity, binding, grant version and quota.
    """
    return _switch(None, FILES_SWITCH)


def _slice_availability(slice_id: str, *, configured: bool) -> Dict[str, Any]:
    """``capability_matrix.availability`` with an honest fallback.

    The slices are declared in the same change; if a build ever ships a caller
    before the declaration, "unknown" must read as unavailable-and-not-implemented
    rather than raising inside a public endpoint.
    """
    try:
        return capability_matrix.availability(slice_id, configured=configured)
    except KeyError:
        return {"implemented": False, "accepted": False, "configured": configured,
                "available": False, "reason": "not_implemented"}


def execution_state(*, settings: Any = None, platform: str = "posix",
                    tools: Optional[list] = None,
                    runtime: str = "") -> Dict[str, Any]:
    """The ``project_execution`` capability block for meta/hello.

    ``files_write_verified`` and ``scripts_verified`` are only true when the
    composed state is available *and* the execution end can actually enforce
    them on this platform -- the contract refuses to advertise an enforcement
    the launcher does not have (a Windows build reports no script tool rather
    than translating POSIX syntax).
    """
    files = _slice_availability(FILES_CAPABILITY,
                                configured=_switch(settings, FILES_SWITCH))
    scripts_slice = _slice_availability(SCRIPTS_CAPABILITY,
                                       configured=_switch(settings, SCRIPTS_SWITCH))
    runnable = [tool for tool in v2.TOOLS["required"]
                if v2.tool_supported_on(tool, platform)]
    if tools is not None:
        runnable = [tool for tool in runnable if tool in tools]
    script_tools = [tool for tool in runnable if tool in v2.TOOLS["script_tools"]]

    available = bool(files.get("available")) and bool(runnable)
    if not available:
        # Precedence mirrors ``availability()``: a capability that is not yet
        # accepted or is switched off is blamed *before* the platform, because
        # the platform is only the binding constraint once the rest is open.
        reason = files.get("reason") or "not_implemented"
        if files.get("available") and not runnable:
            reason = "platform_unsupported"
    else:
        reason = "available"

    if scripts_slice.get("available") and available and not script_tools:
        scripts_reason = "platform_unsupported"
    elif scripts_slice.get("available") and available:
        scripts_reason = "available"
    else:
        # A closed scripts slice explains itself (not_accepted, switched off);
        # a closed *files* slice takes precedence, because scripts cannot be
        # offered while the project surface itself is unavailable.
        scripts_reason = scripts_slice.get("reason") or "not_implemented"
        if not available:
            scripts_reason = reason
    scripts_available = scripts_reason == "available" and bool(script_tools)

    block = v2.execution_capability(
        platform=platform,
        tools=runnable,
        available=available,
        reason=reason,
        files_write_verified=available,
        scripts_verified=scripts_available,
        runtime=runtime,
    )
    # The two surfaces answer separately, so a client can offer the file tools
    # without offering scripts when only the file switch is on.
    block["surfaces"] = {
        "files": {"available": available, "reason": reason, "state": dict(files)},
        "scripts": {"available": scripts_available, "reason": scripts_reason,
                    "state": dict(scripts_slice)},
    }
    return block
