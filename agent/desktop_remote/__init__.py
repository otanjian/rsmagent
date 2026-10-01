"""Remote project execution: the Agent-side half of "执行在我这台机器上".

Change ``align-desktop-project-execution-with-master``, task 6.3.

``agent.desktop_local`` is the mode where the picked directory is reachable by
*this* process (the desktop shell registered it with the backend it started).
This package is the other mode: the model runs on a server that cannot see the
user's disk, so a project tool call has to travel the device command channel
that already exists (``integrations.desktop.commands`` / the WSS gateway) and
come back as the tool's real result -- the same stdout, exit code, truncation
marker and artifacts the local mode would have produced.

Four rules shape the code:

1. **The gates run first, here.** The proxy tool is placed *behind* the existing
   tool gate (permission mode, allow/deny, skill eligibility, quota), keeps the
   master tool's name and schema, and is never a way to reach an execution
   surface the model could not reach anyway.
2. **One place decides local vs remote vs refusal.** ``dispatch.plan`` wraps the
   same grant rules the local mode uses (``agent.desktop_local.capabilities``)
   and adds the device's own declaration; a run never "falls back" between the
   two machines.
3. **No second business truth.** The command row, the ExecutionRun and the
   ``tool_call_id`` are the existing ones; this package stores nothing of its
   own and mints no parallel run.
4. **A result that is not real is not reported.** A command that never reached a
   terminal state becomes ``device_offline`` / ``deadline_exceeded`` /
   ``outcome_unknown`` -- never a success, and never a summary written here.

Nothing in this package authorizes anything by itself: identity comes from the
verified runtime identity, the binding from the identity store, and the final
start is decided by the server again on the broker path (task 6.5).
"""

from __future__ import annotations

# ``ORIGIN_RUNTIME`` is defined by the module that owns the frame, so the two
# ends of the digest cannot disagree about the string by construction.
from integrations.desktop.execution_payload import ORIGIN_RUNTIME

#: The meta/hello key the ``project_execution`` capability is reported under.
CAPABILITY_KEY = "project_execution"

__all__ = ["CAPABILITY_KEY", "ORIGIN_RUNTIME"]
