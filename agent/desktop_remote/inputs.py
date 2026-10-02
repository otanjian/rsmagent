"""Staging a remote call's path arguments (task 3.6 / 6.3, remote half).

The local half (:mod:`agent.desktop_local.source_resolver`) can *reach* the
filesystem, so it can tell "relative id inside the project" from "absolute path
this machine can see" and rewrite the second into the first. This half cannot:
the project directory is on the user's machine, and the Agent process has no
handle on it. So the rules here are the strict subset that needs no filesystem
at all:

* a relative project id passes through (after the same grammar check the picker
  and the local mode use, so both ends call the same file the same name);
* an absolute path -- or ``~``, or a backslash form -- is **refused by name**;
* nothing is uploaded, copied or "landed" on this side.

That is deliberately more restrictive than the local branch, and it is the
honest answer: an absolute server path names a file on *this* machine, which is
not the machine the tool will run on. Rewriting it, or silently dropping it,
would produce a call that fails on the device for a reason the model cannot see
-- or worse, one that touches a same-named file that happens to exist there.

Existence is never checked here, exactly as in the local branch: ``write`` to a
file that does not exist yet is an ordinary, correct call.
"""

from __future__ import annotations

import os
from typing import Any, List, Tuple

from agent.desktop_local.source_resolver import (
    clean_relative,
    iter_path_arguments,
    replace_path_argument,
)
from agent.desktop_local.source_resolver import ToolInputs

#: An absolute path (or a home/drive form) cannot be resolved by this process.
REFUSAL_REMOTE_ABSOLUTE = (
    "本机运行只能按项目内相对引用取文件，服务器绝对路径（{raw}）没有下发到设备："
    "它指向的是 Agent 这台机器上的位置，不是用户机器上的位置。"
    "请改用项目内相对引用，或先把它作为项目内文件存在。"
)

#: ``..``, a drive letter, an empty component -- the picker never emits these.
REFUSAL_REMOTE_UNSAFE = (
    "该引用不是合法的项目内相对引用（{raw}），已拒绝；没有改写，也没有当成路径下发。"
)


def _looks_absolute(raw: str) -> bool:
    """Whether ``raw`` names a location rather than a project-relative id."""
    return bool(raw.startswith(("~", "\\"))) or os.path.isabs(raw)


def prepare_remote_arguments(tool_name: str, arguments: Any) -> ToolInputs:
    """Stage a device-delegated call's arguments, or refuse the call.

    Returns the same :class:`ToolInputs` the local branch returns, so both
    branches of the run feed the tool an argument dict that means the same thing
    -- and a refusal is reported the same way, by name, without running.
    """
    prepared = dict(arguments or {})
    refusals: List[str] = []
    rewrites: List[Tuple[str, str]] = []

    for key, raw in iter_path_arguments(prepared):
        if _looks_absolute(raw):
            refusals.append(REFUSAL_REMOTE_ABSOLUTE.format(raw=raw))
            continue
        relative = clean_relative(raw)
        if relative is None:
            refusals.append(REFUSAL_REMOTE_UNSAFE.format(raw=raw))
            continue
        if relative != raw:
            replace_path_argument(prepared, key, raw, relative)
            rewrites.append((raw, relative))

    return ToolInputs(arguments=prepared, refusals=refusals, rewrites=rewrites)
