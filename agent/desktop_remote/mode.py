"""Which machine a session's project work runs on (task 6.3).

The two desktop modes differ in exactly one fact, and it is a fact about *this
process*, not about the request:

* **local** -- this process is the backend the desktop shell started, so the
  trusted same-machine registry answers with a real directory and the run
  resolves ``execution_cwd`` here (``agent.desktop_local``);
* **remote** -- this process is a server the shell connected *to*, so the
  registry has nothing to answer with and the directory exists only on the
  user's machine, behind the device command channel.

So the rule reads the resolved cwd rather than guessing: a target this process
could resolve is local, and one it could not is delegated -- never the other way
round, because "could not resolve here" must not be read as "run it here
anyway". Nobody gets to *choose* the mode from a request body: a client cannot
make a server take a directory it does not have, and a path can never appear in
a target.

Delegation is additionally behind the ``desktop_project_execution`` deployment
switch (task 6.1 / 11.2, off by default). With the switch closed the existing
answer stands: the call is refused as unavailable rather than sent anywhere.

A target that is only a read-only reference returns False here -- that is not an
execution grant, and this function must never upgrade one.
"""

from __future__ import annotations

from typing import Any

from common.log import logger


def delegation_enabled() -> bool:
    """Whether this deployment may hand project execution to a device."""
    try:
        from integrations.desktop.execution_capability import (
            execution_switch_open,
        )

        return execution_switch_open()
    except Exception:
        # A deployment whose switch cannot be read is a deployment that did not
        # say yes: delegation stays closed rather than defaulting open.
        logger.warning("[DesktopRemote] could not read the execution switch",
                       exc_info=True)
        return False


def remote_mode_for(identity: Any = None) -> bool:
    """Whether this run's project calls have to be delegated to the device."""
    if identity is None:
        from common.runtime_identity import current_identity

        identity = current_identity()
    target = getattr(identity, "execution_target", None)
    if not getattr(target, "is_desktop", False):
        return False
    if not getattr(target, "allows_project_execution", False):
        # ``readonly-input`` is a file *reference*, not permission to execute:
        # delegating it would hand the device a write the user never granted.
        return False
    if getattr(identity, "execution_cwd", None):
        # Resolved here: this is the local mode, and the local path owns it. The
        # delegation must not run a second time for the same call.
        return False
    return delegation_enabled()
