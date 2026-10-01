# encoding:utf-8
"""Every tool that can write is classified, and the inventory is checked (8.6).

A36 (``acceptance.md``):

    对每个具有 cwd 或落盘能力的现有工具逐项回归 | 均有本机执行、显式资源传输或明确
    不支持的分类；不能遗漏后静默写服务端

The failure mode is *omission*, and its shape is specific. A tool nobody classified
does not fail loudly under a local project -- it runs, and it writes **wherever its
own default cwd points**, which is the server. The user asked for a file on their
machine; the file is written on the server; nothing errors and nothing is reported.
So the classification must be a closed set over the *real* tool inventory, and the
inventory has to be derived from the code rather than from a list that agrees with
itself.

Three dispositions, taken from the acceptance line verbatim:

``LOCAL``
    Runs in the project and writes there. ``bash`` is local in a stricter sense --
    it is the one that runs inside the kernel sandbox rather than in the backend
    process.

``TRANSFER``
    Its output is a file that may only become a local input through an explicit,
    verified landing (``resource_landing``), or the landing is refused. The fetch
    itself keeps using its own backend; what must not happen is a downloaded file
    being addressed as a project path.

``SERVER``
    Deliberately left to the server's own authorization and service ownership --
    memory, knowledge, the scheduler store, MCP configuration. This is not an
    omission: the spec says those keep their existing service location and must not
    receive an unresolvable client path just because a project is open, and
    ``DISPOSITIONS`` records them explicitly so "deliberate" is distinguishable
    from "forgotten".

The check with teeth is :func:`disk_writing_tools` plus
``test_every_disk_writing_tool_has_a_disposition``: it scans the real tool modules
for write operations and fails when one has no classification. A hand-written table
cannot catch a tenth tool; a scan can.

``disk_writing_tools`` is a *heuristic* (it reads source, it does not execute it).
The asymmetry is deliberate: a false positive costs one line in :data:`SERVER_TOOLS`
plus a reason, while a false negative is exactly the silent server write A36 exists
to prevent. So the patterns are broad and the failure is loud.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional

__all__ = [
    "LOCAL",
    "TRANSFER",
    "SERVER",
    "LOCAL_TOOLS",
    "TRANSFER_TOOLS",
    "SERVER_TOOLS",
    "DISPOSITIONS",
    "Decision",
    "Dispositions",
    "disposition",
    "module_writes_files",
    "disk_writing_tools",
    "inventory",
    "reset_inventory",
    "unclassified_writer_refusal",
]

LOCAL = "local"
TRANSFER = "transfer"
SERVER = "server"

#: Runs in the project. The file tools write where the panel shows; ``bash`` is
#: executed by the sandboxed worker with the project and the run's temp directory
#: as its only writable roots.
LOCAL_TOOLS = (
    "read", "write", "edit", "ls", "search_files", "bash", "send",
)

#: Produces a downloaded file. The file may only enter the project through an
#: explicit landing, or the landing is refused (``design.md``: "文件落点必须显式转成
#: 本机输入，或拒绝该落盘动作").
TRANSFER_TOOLS = (
    "web_fetch", "browser",
)

#: Server services by design. Listed explicitly so a reader can tell these apart
#: from a tool that was simply never thought about.
SERVER_TOOLS: FrozenSet[str] = frozenset({
    "memory",
    "knowledge",
    "scheduler",
    "env_config",
    "mcp",
    "todo",
    "subagent",
    "agent_delegate",
    "vision",
    "web_search",
    "client_files",
    "evolution_undo",
    "external",
})

DISPOSITIONS: Dict[str, str] = {
    **{name: LOCAL for name in LOCAL_TOOLS},
    **{name: TRANSFER for name in TRANSFER_TOOLS},
    **{name: SERVER for name in SERVER_TOOLS},
}

#: Directories under ``agent/tools/`` that are infrastructure, not tools. Scanned
#: out of the inventory so their helpers do not read as unclassified tools.
_NON_TOOL_DIRS: FrozenSet[str] = frozenset({"utils", "__pycache__"})

#: Write operations, matched against module source. Deliberately broad: see the
#: module docstring on why a false positive is the cheap error.
_WRITE_PATTERNS = (
    # An open() whose mode starts with w/a/x -- including the ``"wb"`` form.
    re.compile(r"open\s*\([^)]*,\s*['\"][wax]"),
    re.compile(r"\.write_text\s*\("),
    re.compile(r"\.write_bytes\s*\("),
    re.compile(r"os\.replace\s*\("),
    re.compile(r"os\.rename\s*\("),
    re.compile(r"shutil\.(copy|copy2|copyfile|move|rmtree)\s*\("),
    re.compile(r"json\.dump\s*\("),
    re.compile(r"\.savefig\s*\("),
    re.compile(r"\.to_excel\s*\("),
    re.compile(r"mkdtemp\s*\("),
)


def disposition(tool_name: str) -> str:
    """The disposition of ``tool_name``, defaulting to ``SERVER``.

    Unknown means server-owned, **not** local: guessing "local" would run an
    unclassified tool against the project, and guessing "refuse" would break tools
    that legitimately never touch a project file. The refusal for a tool that
    *can write* and has no classification is :class:`Dispositions`' job, where the
    write capability is known.
    """
    return DISPOSITIONS.get(str(tool_name or ""), SERVER)


@dataclass(frozen=True)
class Decision:
    """Whether a call may proceed, and why not when it may not."""

    allowed: bool
    disposition: str = SERVER
    code: str = ""
    message: str = ""


class Dispositions:
    """Decides whether a tool may run, given what it can do.

    Separated from the table so the refusal can be exercised with an empty table
    -- which is how the "unclassified" path stays tested rather than assumed.
    """

    def __init__(self, table: Optional[Mapping[str, str]] = None):
        self._table = dict(DISPOSITIONS if table is None else table)

    def of(self, tool_name: str) -> str:
        return self._table.get(str(tool_name or ""), SERVER)

    def decide(self, tool_name: str, *, can_write: bool,
               landing_available: bool = False) -> Decision:
        """``can_write`` comes from the tool, never from its name."""
        name = str(tool_name or "")
        kind = self.of(name)

        if can_write and name not in self._table:
            # The A36 case: it writes files and nobody said where they go. Running
            # it would write wherever its default cwd points -- the server -- which
            # is the silent failure the requirement names.
            return Decision(
                allowed=False, disposition=SERVER, code="unclassified_tool",
                message=(f"tool {name!r} can write files and has no disposition for a "
                         f"local project; it was not run, so nothing was written to "
                         f"the server. Classify it in agent/desktop_local/"
                         f"tool_disposition.py."))

        if kind == TRANSFER and can_write and not landing_available:
            return Decision(
                allowed=False, disposition=TRANSFER, code="resource_unavailable",
                message=(f"tool {name!r} produces a file, and this run has no verified "
                         f"landing for it; the file was not written into the project "
                         f"and was not silently addressed as a server path."))

        return Decision(allowed=True, disposition=kind)


def module_writes_files(path: str) -> bool:
    """Whether the module at ``path`` contains a write operation."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            source = handle.read()
    except OSError:
        return False
    return any(pattern.search(source) for pattern in _WRITE_PATTERNS)


#: The inventory is a source scan, so it is computed once per process. Kept in a
#: module global so a caller on the dispatch path does not re-walk the tree on
#: every tool call.
_INVENTORY: Optional[FrozenSet[str]] = None


def inventory(tools_dir: Optional[str] = None) -> FrozenSet[str]:
    """The set of tools under ``agent/tools`` whose code writes files, cached."""
    global _INVENTORY
    if tools_dir is not None:
        return frozenset(disk_writing_tools(tools_dir))
    if _INVENTORY is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _INVENTORY = frozenset(disk_writing_tools(os.path.join(here, "tools")))
    return _INVENTORY


def reset_inventory() -> None:
    """Forget the cached inventory (tests that add or remove a tool module)."""
    global _INVENTORY
    _INVENTORY = None


def unclassified_writer_refusal(tool_name: str) -> str:
    """``""`` when the tool is fine, else the reason it must not run.

    This is the A36 gate, kept here rather than in the dispatch path so the rule
    and its explanation live together. It reads the *inventory*, not the table:
    asking the table whether a tool is classified would answer "SERVER" for
    anything missing and the gate would never fire.
    """
    name = str(tool_name or "")
    if not name or name not in inventory() or name in DISPOSITIONS:
        return ""
    return (
        f"工具 {name!r} 可以写文件，但在本机项目下没有执行方式分类（本机执行、显式传输"
        f"或明确不支持），因此这次调用没有执行，也没有改写服务器上的文件。请在 "
        f"agent/desktop_local/tool_disposition.py 中为它登记分类。")


def disk_writing_tools(tools_dir: str) -> List[str]:
    """Names of tools under ``tools_dir`` whose code writes files.

    A tool is a subdirectory; every ``.py`` in it is inspected. Read-only modules
    are excluded, which is what makes the result useful: ``read`` is a file tool
    that never writes, while ``env_config`` writes a server-side config file and
    ``web_fetch`` writes a download.
    """
    found: List[str] = []
    if not os.path.isdir(tools_dir):
        return found
    for name in sorted(os.listdir(tools_dir)):
        if name in _NON_TOOL_DIRS or name.startswith("."):
            continue
        directory = os.path.join(tools_dir, name)
        if not os.path.isdir(directory):
            continue
        for root, _dirs, files in os.walk(directory):
            if any(module_writes_files(os.path.join(root, f))
                   for f in files if f.endswith(".py")):
                found.append(name)
                break
    return found
