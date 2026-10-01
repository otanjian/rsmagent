"""The v2 execution payload: canonical digest, device frame, result handling.

Change ``align-desktop-project-execution-with-master``, task 6.2.

Three jobs, one place, so HTTP, the command service and the broker cannot build
the same frame three slightly different ways:

* :func:`params_digest` -- the canonical envelope digest. It covers the tool and
  its schema version, the full arguments, the run/tool-call association, the
  origin and binding, the grant version, the selection generation and the Skill
  resource digests. Both ends must compute the *same* bytes; the desktop client
  has its own implementation and ``tests/test_desktop_execution_payload.py``
  pins the exact string, so a change on one side fails the other side's test.
* :func:`device_execution_frame` -- the ``execute_tool`` frame built from the
  *durable row* (never from a caller's message), checked against the v2 contract
  before it is returned so this server cannot emit a frame the device would
  refuse.
* :func:`validate_device_result` -- the shape check for what comes back, plus the
  normalisation that keeps "may have written files" visible.

Nothing here authorizes anything.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from auth import desktop_contracts_v2 as v2

#: The execution source recorded for a command the Agent runtime enqueued.
#: Kept in step with ``agent.desktop_remote.ORIGIN_RUNTIME`` by the digest test:
#: both ends must produce the same envelope, so the value cannot be a local
#: decision of either one.
ORIGIN_RUNTIME = "runtime"

#: The exact fields the digest covers, in this order. Order is part of the
#: contract: both implementations build this list, then serialize sorted.
DIGEST_FIELDS = (
    "tool",
    "tool_schema_version",
    "arguments",
    "run_id",
    "tool_call_id",
    "session_id",
    "agent_id",
    "origin",
    "binding_id",
    "workspace_id",
    "device_id",
    "grant_version",
    "selection_generation",
    "skill_resources",
)


def canonical_text(envelope: Dict[str, Any]) -> str:
    """The canonical JSON text the digest is taken over.

    Public so both implementations can be pinned against the *same* string: the
    desktop client checks the identical literal in
    ``tests/test_desktop_execution_contract.cjs``.
    """
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _canonical_bytes(envelope: Dict[str, Any]) -> bytes:
    return canonical_text(envelope).encode("utf-8")


def check_canonical_value(value: Any, *, path: str = "envelope") -> None:
    """Refuse a value whose text form could differ between implementations.

    Non-integer numbers are the one JSON shape where Python and JavaScript can
    disagree (``1.0`` vs ``1``, ``1e400`` vs ``Infinity``), so the digest covers
    only values both sides serialize identically. This is a safety rule, not
    pedantry: two different digests for one command is exactly the confusion the
    digest exists to prevent.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise ValueError("%s must not contain a non-integer number" % path)
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            check_canonical_value(item, path="%s[%d]" % (path, index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("%s must use string keys" % path)
            check_canonical_value(item, path="%s.%s" % (path, key))
        return
    raise ValueError("%s contains a non-JSON value" % path)


def canonical_skill_resources(resources: Any) -> List[Dict[str, str]]:
    """The one canonical form of a skill set: normalised, then sorted by id.

    Used for *both* the digest input and the value stored on the row and handed
    to the device. That is the point of having it: "the set the digest covers" and
    "the set the device is asked to mount" must not be two different lists, or a
    device could be handed an order the digest does not represent (and the two
    ends would disagree about a set that is really the same).
    """
    return sorted(skill_digests(resources),
                  key=lambda entry: str(entry.get("skill_id")))


def canonical_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """The digest input: only the covered fields, with stable defaults.

    Absent optional fields are normalised to ``None``/``[]`` rather than dropped,
    so "no skills" and "skills forgotten" cannot produce the same digest.
    """
    out: Dict[str, Any] = {}
    for key in DIGEST_FIELDS:
        value = envelope.get(key)
        if key == "skill_resources":
            value = canonical_skill_resources(value)
        out[key] = value if value is not None else None
    check_canonical_value(out)
    return out


def params_digest(envelope: Dict[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical envelope."""
    return "sha256:" + hashlib.sha256(_canonical_bytes(canonical_envelope(envelope))).hexdigest()


def skill_digests(resources: Any) -> List[Dict[str, str]]:
    """Normalise declared Skill resources to ``(skill_id, digest)`` pairs.

    A resource without a digest is refused rather than hashed as "unknown": the
    digest is what lets the execution end prove it mounted the reviewed package.
    """
    out: List[Dict[str, str]] = []
    for entry in resources or []:
        if not isinstance(entry, dict):
            raise ValueError("skill resource is not an object")
        skill_id = entry.get("skill_id")
        digest = entry.get("digest")
        if not skill_id or not digest:
            raise ValueError("skill resource needs skill_id and digest")
        out.append({"skill_id": str(skill_id), "digest": str(digest)})
    return out


def envelope_for_command(*, tool: str, tool_schema_version: int,
                         arguments: Dict[str, Any], run_id: str,
                         tool_call_id: str, session_id: Optional[str],
                         agent_id: Optional[str], origin: Optional[str],
                         binding_id: str, workspace_id: str, device_id: str,
                         grant_version: int, selection_generation: int,
                         resources: Any = None) -> Dict[str, Any]:
    """Build the digest envelope for a v2 command.

    One constructor so the digest a command is stored with is the digest the
    frame, the permit and the journal carry -- three readers, one input.
    """
    return {
        "tool": tool,
        "tool_schema_version": int(tool_schema_version),
        "arguments": arguments or {},
        "run_id": run_id,
        "tool_call_id": tool_call_id,
        "session_id": session_id,
        "agent_id": agent_id,
        "origin": origin,
        "binding_id": binding_id,
        "workspace_id": workspace_id,
        "device_id": device_id,
        "grant_version": int(grant_version),
        "selection_generation": int(selection_generation),
        "skill_resources": skill_digests(resources),
    }


def device_execution_frame(command: Dict[str, Any],
                           *, platform: str = "posix") -> Dict[str, Any]:
    """The ``execute_tool`` frame for one durable v2 command row.

    ``command`` is the public projection from :meth:`CommandService._public_command`
    (which is itself built from the row). The frame is checked with the contract
    validator first: a server that could emit an invalid frame would be handing
    the device something it must refuse, and the failure would surface as an
    unexplained device-side rejection instead of a server bug.
    """
    frame = {
        "type": "execute_tool",
        "protocol_major": v2.PROTOCOL_MAJOR,
        "command_id": command["id"],
        "run_id": command.get("run_id") or "",
        "tool_call_id": command.get("tool_call_id") or "",
        "binding_id": command["binding_id"],
        "workspace_id": command.get("workspace_id") or "",
        "device_id": command["device_id"],
        "grant_version": command.get("grant_version") or 1,
        "selection_generation": command.get("selection_generation") or 0,
        "connection_epoch": command.get("connection_epoch") or "",
        "tool": command.get("tool_name") or "",
        "tool_schema_version": command.get("tool_schema_version")
        or v2.TOOL_SCHEMA_VERSION,
        "arguments": command.get("params") or {},
        "params_digest": command.get("params_digest") or "",
        "expires_at": command.get("deadline_at") or 0,
    }
    if command.get("session_id"):
        frame["session_id"] = command["session_id"]
    if command.get("agent_id"):
        frame["agent_id"] = command["agent_id"]
    if command.get("skill_resources"):
        frame["skill_resources"] = list(command["skill_resources"])
    if command.get("permission_mode"):
        frame["permission_mode"] = command["permission_mode"]
    if command.get("approval_id"):
        frame["approval_id"] = command["approval_id"]
    # The digest covers ``origin``, so the frame has to carry it or the device
    # could not recompute the value it is asked to verify. It is the server's own
    # recorded source -- never a caller's field -- and a runtime-initiated
    # command uses the constant that says so.
    frame["origin"] = command.get("origin") or ORIGIN_RUNTIME
    problems = v2.validate_execute_frame(frame, platform=platform)
    if problems:
        raise ValueError("refusing to emit an invalid execute_tool frame: %s"
                         % "; ".join(problems))
    return frame


def validate_device_result(payload: Any) -> List[str]:
    """Shape-check what the device sent back, before it is stored.

    The device is the only writer of these fields, so the server's job is to
    refuse a claim it cannot act on: an unknown phase, an effect that contradicts
    the phase, or a terminal phase without both timestamps.
    """
    problems = v2.validate_result_frame(payload)
    if problems:
        return problems
    phase = payload.get("execution_phase")
    effects = payload.get("effects")
    allowed = v2.effects_for(phase, error_code=payload.get("error_code"))
    if allowed is not None and effects not in (allowed, "unknown"):
        # "unknown" is always allowed: it is the honest, weaker claim.
        problems.append("effects %r contradicts phase %r" % (effects, phase))
    if payload.get("started_at") and payload.get("finished_at") \
            and payload["finished_at"] < payload["started_at"]:
        problems.append("finished_at is before started_at")
    return problems
