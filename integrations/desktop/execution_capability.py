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

from typing import Any, Dict, List, Optional, Sequence

from auth import capability_matrix
from auth import desktop_contracts_v2 as v2
from auth import desktop_contracts_v3 as v3

#: Public capability names the meta/hello block uses.
FILES_CAPABILITY = "desktop_project_execution"
SCRIPTS_CAPABILITY = "desktop_project_scripts"

#: Deployment switches, one per surface (``config.available_setting``).
FILES_SWITCH = "desktop_project_execution_enabled"
SCRIPTS_SWITCH = "desktop_project_scripts_enabled"

#: Config keys naming the Windows platforms whose evidence has been accepted.
#:
#: These are *not* new product switches: they name evidence an operator recorded
#: after looking at W01-W16 / S01-S12 results, and they default to empty, so a
#: deployment that does nothing keeps every Windows surface closed. They are read
#: through the same ``config.available_setting`` map the two existing execution
#: switches use (``config-template.json`` documents them), which is what keeps
#: "no new Windows-only switch" true in the sense D4 means: nothing here can
#: *widen* the capability intersection, only record that a slice of it is done.
FILE_PLATFORMS_SETTING = "desktop_project_execution_platforms"
SCRIPT_PLATFORMS_SETTING = "desktop_project_scripts_platforms"
RUNTIMES_SETTING = "desktop_project_script_runtimes"
HELPER_UNAVAILABLE_SETTING = "desktop_project_helper_unavailable_platforms"

#: The reason a platform with no usable helper reports. It is not one of the
#: contract's ``reason_values`` on purpose -- the contract describes *contract*
#: reasons, and "reinstall the app" is an operator/UI state, so it travels beside
#: the reason (see ``helper_available``) rather than replacing it with a value the
#: contract does not know.
HELPER_REASON = "helper_missing"


def _setting_names(settings: Any, key: str) -> tuple:
    """Read a list-of-names setting, defaulting to *nothing accepted*.

    An unparseable value means empty, the same convention the boolean switches
    use: a typo must never open a surface. Accepts a list, or a comma/space
    separated string from a config file.
    """
    from config import available_setting

    if settings is not None and key in settings:
        raw = settings[key]
    else:
        raw = available_setting.get(key, ())
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = [piece.strip() for piece in raw.replace(",", " ").split()]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(piece).strip() for piece in raw]
    else:
        return ()
    return tuple(piece for piece in parts if piece)


def _setting_runtimes(settings: Any, key: str) -> Dict[str, tuple]:
    """Read ``{platform: [runtime, ...]}``, defaulting to *nothing verified*."""
    from config import available_setting

    if settings is not None and key in settings:
        raw = settings[key]
    else:
        raw = available_setting.get(key, {})
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, tuple] = {}
    for platform, languages in raw.items():
        if isinstance(languages, str):
            languages = [piece.strip() for piece in languages.replace(",", " ").split()]
        if isinstance(languages, (list, tuple, set)):
            out[str(platform)] = tuple(str(x).strip() for x in languages if str(x).strip())
    return out


def _accepted(names: Sequence[str], platform: str) -> bool:
    """Whether ``platform`` appears in an acceptance list.

    Compared exactly: an entry is a platform *name* an operator wrote down after
    looking at evidence, never a prefix or a wildcard, so "windows" cannot
    accidentally open "windows-server-2025" or anything else.
    """
    return platform in tuple(names)


def accepted_runtimes(platform: str, settings: Any = None) -> List[str]:
    """The verified script runtimes on ``platform``, in contract order.

    An empty result means two different things and they must not be confused:

    * the platform is not configured at all -- nothing has been recorded either
      way, so posix keeps the script surface its capability slice already
      accepted;
    * the platform is configured with an empty list -- an operator wrote down
      "no language is verified here", which closes the surface.
    """
    declared = _setting_runtimes(settings, RUNTIMES_SETTING)
    if platform not in declared:
        return []
    return [language for language in v3.RUNTIME_LANGUAGES
            if language in declared[platform]]


def runtime_recorded(platform: str, settings: Any = None) -> bool:
    """Whether an operator recorded a per-language list for ``platform``."""
    return platform in _setting_runtimes(settings, RUNTIMES_SETTING)


def helper_usability(platform: str, declared: Any = None,
                     settings: Any = None) -> Dict[str, Any]:
    """Whether the shipped file helper is usable on ``platform``.

    Two sources say so and both must not be negative: the deployment's own record
    (:data:`HELPER_UNAVAILABLE_SETTING`, set from installer reports) and what the
    device itself declared (``helper_available`` in its hello block). A device
    that reports ``False`` is believed even when the record names nothing, because
    it is the end that can actually see its own binary.

    On posix neither source can close the surface: this change does not ship a
    Windows helper, and a stale or unrelated entry naming posix must not break
    the platform whose helper already works. The invalid record is still reported
    so an operator can see it.
    """
    if platform != "win32":
        return {"available": True, "reason": "", "source": "not_win32",
                "ignored_record": _accepted(
                    _setting_names(settings, HELPER_UNAVAILABLE_SETTING), platform)}
    if declared is False:
        return {"available": False, "reason": HELPER_REASON, "source": "device"}
    if _accepted(_setting_names(settings, HELPER_UNAVAILABLE_SETTING), platform):
        return {"available": False, "reason": HELPER_REASON, "source": "operator"}
    if declared is True:
        return {"available": True, "reason": "", "source": "device"}
    # Unknown is not a refusal: a helper that has not been reported missing is
    # presumed present, so this input can only ever close the surface.
    return {"available": True, "reason": "", "source": "unreported"}


def _acceptance(platform: str, settings: Any = None) -> Dict[str, Any]:
    """The acceptance state of a platform, in v3's per-surface vocabulary.

    ``files`` asks only whether the file boundary is accepted; ``scripts`` asks
    for the script launcher *and* a verified runtime. Neither is inferred from the
    other, and neither is inferred from the POSIX acceptance the capability
    matrix already carries. A platform v3 does not admit at all is rejected by the
    contract before this function is consulted.
    """
    if platform != "win32":
        # posix keeps the answers it always had: the capability matrix's accepted
        # slice is what gates it, exactly as before this change. A per-language
        # list is a narrowing an operator may add, never a precondition, so its
        # absence must not close a surface the matrix already accepted.
        if runtime_recorded(platform, settings) and not accepted_runtimes(platform, settings):
            return {"files": True, "scripts": False, "reason": "runtime_unavailable"}
        return {"files": True, "scripts": True, "reason": ""}
    file_platforms = _setting_names(settings, FILE_PLATFORMS_SETTING)
    if not _accepted(file_platforms, platform):
        return {"files": False, "scripts": False, "reason": "not_accepted"}
    script_platforms = _setting_names(settings, SCRIPT_PLATFORMS_SETTING)
    runtimes = accepted_runtimes(platform, settings)
    if not _accepted(script_platforms, platform):
        # Files may be open while scripts are not: the state v3 exists to make
        # expressible, and not an error.
        return {"files": True, "scripts": False, "reason": "not_accepted"}
    if not runtimes:
        # The script launcher is accepted but no language has been verified yet,
        # so "which runtime" is the missing piece rather than the platform.
        return {"files": True, "scripts": False, "reason": "runtime_unavailable"}
    return {"files": True, "scripts": True, "reason": ""}


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


def _host_platform_tools(platform: str) -> List[str]:
    """The contract's tools this *host's* platform could run.

    A named function rather than an inline comprehension so "which platform is
    this build running on" has one place to be answered -- and one place for a
    test host without acceptance evidence to substitute, without touching the
    contract's own ``tool_supported_on`` (which other tests assert on directly).
    """
    return [tool for tool in v2.TOOLS["required"]
            if v2.tool_supported_on(tool, platform)]


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
    runnable = _host_platform_tools(platform)
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


def execution_state_v3(*, settings: Any = None, platform: str = "posix",
                       tools: Optional[list] = None,
                       runtime: str = "",
                       helper_available: Any = None) -> Dict[str, Any]:
    """The v3 ``project_execution`` block for meta/hello.

    v2's composition plus one more input, because v3 admits a platform (win32)
    whose *acceptance* is not implied by the contract: the intersection here is

        deployment switch x capability slice x contract admission x acceptance

    where acceptance is :func:`_acceptance`. On posix the extra input is a no-op,
    so a working POSIX deployment reports exactly what it reported before. On
    win32 an unlisted platform reports ``not_accepted`` and a listed one reports
    only the surface whose evidence exists, which is the state task 6.1 opens.

    ``files_write_verified`` is not ``available``: it says the write boundary
    itself has been accepted, which for Windows is a separate claim from "the
    file tools can be offered".
    """
    files = _slice_availability(FILES_CAPABILITY,
                                configured=_switch(settings, FILES_SWITCH))
    scripts_slice = _slice_availability(SCRIPTS_CAPABILITY,
                                       configured=_switch(settings, SCRIPTS_SWITCH))
    acceptance = _acceptance(platform, settings)
    helper = helper_usability(platform, helper_available, settings)
    admitted = v3.tools_for_platform(platform)
    runnable = [tool for tool in admitted]
    if tools is not None:
        runnable = [tool for tool in runnable if tool in tools]
    script_tools = [tool for tool in runnable if tool in v3.TOOLS["script_tools"]]

    # A platform the contract does not admit has no tools and no acceptance; the
    # platform is then the binding reason, as it was in v2.
    if not admitted:
        return v3.execution_capability(platform=platform, tools=[],
                                       available=False,
                                       reason="platform_unsupported",
                                       runtime="")

    files_accepted = bool(acceptance.get("files"))
    available = bool(files.get("available") and files_accepted
                     and helper.get("available") and runnable)
    if available:
        reason = "available"
    else:
        # Precedence: what the operator can act on first. A closed declaration or
        # switch is blamed before the platform, the platform before a missing
        # acceptance entry, and the missing helper last because "reinstall" is
        # only the answer once everything else is already open.
        reason = files.get("reason") or "not_implemented"
        if files.get("available") and not files_accepted:
            reason = acceptance.get("reason") or "not_accepted"
        elif files.get("available") and files_accepted and not helper.get("available"):
            reason = "feature_unavailable"
        elif files.get("available") and not runnable:
            reason = "platform_unsupported"

    scripts_accepted = bool(acceptance.get("scripts"))
    scripts_available = bool(
        scripts_slice.get("available") and scripts_accepted
        and available and script_tools)
    if not available:
        scripts_reason = reason
    elif scripts_available:
        scripts_reason = "available"
    elif not scripts_slice.get("available"):
        scripts_reason = scripts_slice.get("reason") or "not_implemented"
    elif not scripts_accepted:
        scripts_reason = acceptance.get("reason") or "not_accepted"
    elif not script_tools:
        # The contract itself offers no script tool on this platform. On win32
        # that is permanent until the contract names a script launcher, so it is
        # reported as a platform fact rather than as a missing runtime.
        scripts_reason = "platform_unsupported"
    else:
        scripts_reason = "runtime_unavailable"

    # Only advertise a runtime that is actually verified on this platform; an
    # unverified language is refused rather than named in the capability block.
    verified = accepted_runtimes(platform, settings)
    advertised_runtime = runtime if (runtime and runtime in verified) else ""
    if not advertised_runtime and len(verified) == 1:
        advertised_runtime = verified[0]

    block = v3.execution_capability(
        platform=platform,
        tools=runnable,
        available=available,
        reason=reason,
        files_write_verified=available and files_accepted,
        scripts_verified=scripts_available,
        runtime=advertised_runtime,
    )
    block["surfaces"] = {
        "files": {"available": available, "reason": reason},
        "scripts": {"available": scripts_available, "reason": scripts_reason},
    }
    block["accepted_runtimes"] = list(verified)
    # A fact about the shipped artifact, reported beside the contract reason
    # rather than as one: "reinstall the app" is not a platform or authorization
    # state, and the contract's reason_values do not name it.
    block["helper_available"] = bool(helper.get("available"))
    if not helper.get("available"):
        block["helper_reason"] = helper.get("reason")
        block["helper_source"] = helper.get("source")
    return block


def execution_state_for(version: int, **kwargs: Any) -> Dict[str, Any]:
    """The capability block for the protocol major the peer actually speaks.

    A v2 peer keeps v2's block, including its refusal to offer anything on
    Windows; a v3 peer gets the block above. Selecting by major rather than
    always answering v3 is what keeps "an old client is not silently upgraded"
    true at the capability layer as well as in the frames.
    """
    if version == v3.PROTOCOL_MAJOR:
        return execution_state_v3(**kwargs)
    return execution_state(**kwargs)
