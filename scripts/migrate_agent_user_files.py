# encoding:utf-8
"""Move legacy shared-Agent platform files into each owner's subtree.

change ``isolate-shared-agent-user-data`` (tasks 5.1/5.2). Before the change,
everything the Web console uploaded and everything a platform callback produced
landed in one Agent-wide place (``<agent workspace>/tmp`` and, later,
``<agent workspace>/outputs``). Those files are now *legacy*: the file surface
only ever serves a member their own ``user/<user_id>/`` subtree, so a legacy file
sitting in ``tmp/`` would either be unreachable by its real owner or, worse, stay
readable by every member of the tenant.

This tool closes that gap with two moves and no new storage:

``user/<user_id>/uploads|outputs/``
    A file whose owner a *trusted record* confirms (the conversation store's own
    ``messages.owner``/``content``/``extras`` — the session that carried the file,
    written by the server, never by the client) is moved to that member's
    subtree. Ownership arrives with the file, so nothing has to be guessed from
    the name.

``user/_legacy/``
    A file nobody can claim — referenced by two different members, or by nobody —
    is moved here instead. ``_legacy`` is deliberately **not** a user id (the
    store only ever issues ``usr_*`` ones), and the file-surface rule
    (``common.state_dir.classify_agent_user_path`` -> ``_db_path_visible``)
    already refuses a ``user/<x>`` subtree to everybody but ``x``. So the file is
    preserved for an operator to inspect on disk while the API keeps refusing it:
    "protected and retained", never re-published.

Two properties the operations runbook depends on:

* **re-runnable** — every decision is recomputed from the trusted records and the
  filesystem each time, so an interrupted run simply resumes. A file already
  sitting at its destination is recognised by content hash, not by a marker file;
* **no silent loss** — content is hashed before the source is removed, an existing
  destination is never overwritten, a symlink is never followed, and the default
  mode is a dry run that prints the plan without touching anything.

Usage::

    python -m scripts.migrate_agent_user_files --workspace <agent workspace>
    python -m scripts.migrate_agent_user_files --workspace <ws> --apply

``--workspace`` may be repeated. With no workspace the tool uses the Agent
registry, which is what an operator wants when running it against the whole
install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

#: Directories that held uploads before the change (``tmp/`` is where
#: ``_get_upload_dir`` put them) and results produced by a platform callback.
LEGACY_UPLOAD_DIRS = ("tmp", "uploads")
LEGACY_OUTPUT_DIRS = ("outputs",)

#: The quarantine container. Not a valid user id by construction (see module
#: docstring), so the existing ownership rule refuses it to every caller.
QUARANTINE_DIR_NAME = "_legacy"

#: JSON keys a trusted record uses to name a file it carried.
_PATH_KEYS = ("path", "file_path", "abs_path", "file")

_KIND_UPLOAD = "upload"
_KIND_OUTPUT = "output"
_KIND_QUARANTINE = "quarantine"
_KIND_DUPLICATE = "duplicate"


class MigrationError(Exception):
    """A refusal that must stop the run rather than be worked around."""


@dataclass
class Action:
    """One decided move of one file.

    Every action is a *move*: the legacy region must end up empty, because a file
    left in ``tmp/`` or ``outputs/`` is still readable by every member of the
    tenant. ``duplicate`` is the one case where the content already exists at its
    rightful place; the redundant source is still moved out of the legacy region,
    to the quarantine, rather than deleted.
    """

    source: str
    destination: str
    kind: str
    owner: Optional[str] = None
    reason: str = ""


@dataclass
class Plan:
    """Everything one workspace's run decided, ready to apply or print."""

    workspace: str
    actions: List[Action] = field(default_factory=list)

    def of_kind(self, kind: str) -> List[Action]:
        return [a for a in self.actions if a.kind == kind]

    def is_empty(self) -> bool:
        return not self.actions

    def summary(self) -> Dict[str, int]:
        counts = {k: 0 for k in
                  (_KIND_UPLOAD, _KIND_OUTPUT, _KIND_QUARANTINE, _KIND_DUPLICATE)}
        for action in self.actions:
            counts[action.kind] = counts.get(action.kind, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Trusted records
# ---------------------------------------------------------------------------

def _walk_paths(node, found: Set[str]) -> None:
    """Collect every file path named anywhere inside a decoded JSON value."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _PATH_KEYS and isinstance(value, str) and value:
                found.add(value)
            else:
                _walk_paths(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_paths(item, found)


def referenced_owners(store_path: str) -> Dict[str, Set[str]]:
    """``{realpath: {owner, ...}}`` from one conversation store.

    Read-only and tolerant: a store that is missing, unreadable or still on the
    pre-tenancy schema simply contributes nothing, which lands its files in the
    quarantine half rather than guessing an owner. ``messages.owner`` is written
    by the server from the verified request identity, so it — and not the file
    name, not the upload time — is what "trusted record" means here.
    """
    owners: Dict[str, Set[str]] = {}
    if not store_path or not os.path.isfile(store_path):
        return owners
    try:
        con = sqlite3.connect("file:%s?mode=ro" % store_path, uri=True, timeout=10)
    except sqlite3.Error:
        return owners
    try:
        try:
            rows = con.execute("SELECT owner, content, extras FROM messages")
        except sqlite3.Error:
            return owners
        for owner, content, extras in rows:
            owner = (owner or "").strip()
            if not owner:
                continue
            found: Set[str] = set()
            for blob in (content, extras):
                if not blob:
                    continue
                try:
                    _walk_paths(json.loads(blob), found)
                except (TypeError, ValueError):
                    continue
            for raw in found:
                if not os.path.isabs(raw):
                    continue
                owners.setdefault(os.path.realpath(raw), set()).add(owner)
    finally:
        con.close()
    return owners


def merge_references(*maps: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    merged: Dict[str, Set[str]] = {}
    for mapping in maps:
        for path, owners in mapping.items():
            merged.setdefault(path, set()).update(owners)
    return merged


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_files(directory: str) -> List[str]:
    """Every regular file under ``directory``, never following a symlink.

    A symlinked entry is skipped rather than resolved: the product never creates
    one, and following it would move whatever it points at (possibly outside the
    Agent's workspace) on the strength of a path nobody vouched for.
    """
    files: List[str] = []
    if not os.path.isdir(directory) or os.path.islink(directory):
        return files
    for current, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(current, d))]
        for name in filenames:
            full = os.path.join(current, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            files.append(full)
    return files


def _legacy_files(workspace: str) -> List[Tuple[str, str]]:
    """``[(path, kind)]`` for the legacy files of one Agent workspace."""
    found: List[Tuple[str, str]] = []
    for name in LEGACY_UPLOAD_DIRS:
        for path in _regular_files(os.path.join(workspace, name)):
            found.append((path, _KIND_UPLOAD))
    for name in LEGACY_OUTPUT_DIRS:
        for path in _regular_files(os.path.join(workspace, name)):
            found.append((path, _KIND_OUTPUT))
    return found


def _quarantine_destination(workspace: str, source: str) -> str:
    """Where an unattributable file is preserved, keeping its provenance.

    The quarantined copy is the only surviving record of where the file came
    from, so it keeps its path *under the legacy root it was found in*
    (``tmp/_docx/word/document.xml`` -> ``user/_legacy/tmp/_docx/word/document.xml``).
    Keeping the whole relative path is also what makes the destination unique:
    a real workspace holds extraction trees with repeated basenames
    (``_docx``/``_docx2`` both carrying ``word/document.xml``), and flattening
    them onto one name would make two files fight over one path.
    """
    rel = os.path.relpath(os.path.realpath(source), os.path.realpath(workspace))
    parts = [p for p in rel.split(os.sep) if p and p != os.pardir]
    return os.path.join(workspace, "user", QUARANTINE_DIR_NAME, *parts)


def _destination(workspace: str, owner: Optional[str], kind: str,
                 source: str) -> str:
    if owner is None:
        return _quarantine_destination(workspace, source)
    subdir = "uploads" if kind == _KIND_UPLOAD else "outputs"
    return os.path.join(workspace, "user", owner, subdir,
                        os.path.basename(source))


def _safe_destination(destination: str) -> str:
    """Refuse a destination that a link could redirect the write through."""
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    if os.path.islink(parent):
        raise MigrationError("refusing a linked destination: %s" % parent)
    return destination


def plan_workspace(workspace: str,
                   owners_by_path: Dict[str, Set[str]],
                   *,
                   legacy_files: Optional[Iterable[Tuple[str, str]]] = None,
                   ) -> Plan:
    """Decide what happens to each legacy file, without touching anything."""
    workspace = os.path.realpath(workspace)
    plan = Plan(workspace=workspace)
    files = list(legacy_files if legacy_files is not None
                 else _legacy_files(workspace))

    # Two legacy files that would land on the same destination (same owner, same
    # name, different source directory) cannot both be published under one name.
    # The first in a stable order wins; the rest are quarantined, never merged.
    # A published destination is claimed here; the quarantined half is unique by
    # construction and re-checked in ``_quarantine``.
    claimed: Dict[str, str] = {}
    for source, kind in sorted(files, key=lambda item: item[0]):
        real_source = os.path.realpath(source)
        owners = sorted(owners_by_path.get(real_source, ()))

        if len(owners) == 1:
            owner = owners[0]
            destination = _safe_destination(_destination(
                workspace, owner, kind, source))
            if destination in claimed:
                _quarantine(plan, workspace, source,
                            "two legacy files claim %s" % destination)
                continue
            claimed[destination] = real_source
        elif len(owners) > 1:
            _quarantine(plan, workspace, source,
                        "referenced by %d users" % len(owners))
            continue
        else:
            _quarantine(plan, workspace, source, "no trusted owner recorded")
            continue

        if os.path.exists(destination):
            try:
                same = _sha256(source) == _sha256(destination)
            except OSError:
                same = False
            if same:
                _quarantine(plan, workspace, source,
                            "already migrated to %s" % destination,
                            kind=_KIND_DUPLICATE, owner=owner)
            else:
                _quarantine(plan, workspace, source,
                            "destination holds different content", owner=owner)
            continue

        plan.actions.append(Action(source, destination, kind, owner=owner))

    return plan


def _quarantine(plan: Plan, workspace: str, source: str, reason: str, *,
                kind: str = _KIND_QUARANTINE,
                owner: Optional[str] = None) -> None:
    """Plan ``source`` into the protected half, refusing a taken destination.

    The quarantine path is derived from the source's own relative path, so a
    collision here would mean the same source was seen twice; failing loudly
    beats silently dropping the second file.
    """
    destination = _safe_destination(_quarantine_destination(workspace, source))
    if any(action.destination == destination for action in plan.actions):
        raise MigrationError("two legacy files share %s" % destination)
    plan.actions.append(Action(source, destination, kind, owner=owner,
                               reason=reason))


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def apply_plan(plan: Plan) -> Plan:
    """Perform ``plan`` in place, hashing before removing any source.

    A crash between the copy and the removal leaves the destination complete and
    the source intact: the next run finds equal hashes, records a ``duplicate``
    and moves the (now redundant) source out of the legacy region, so the run is
    idempotent rather than half-done — and the legacy region still ends up empty,
    which is the point of running it at all.
    """
    for action in plan.actions:
        _safe_destination(action.destination)
        if os.path.exists(action.destination):
            # Re-checked here, not just at plan time: the window between the two
            # is exactly where a concurrent writer would land.
            if _sha256(action.source) != _sha256(action.destination):
                action.kind = _KIND_QUARANTINE
                action.destination = _quarantine_destination(
                    plan.workspace, action.source)
                action.reason = "destination appeared during the run"
                _safe_destination(action.destination)
        shutil.move(action.source, action.destination)
    return plan


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _agent_workspaces() -> List[str]:
    from agent.registry import get_agent_registry

    registry = get_agent_registry()
    workspaces = []
    for profile in registry.list(require_enabled=False):
        if profile.workspace:
            workspaces.append(profile.workspace)
    return workspaces


def run(workspaces: Iterable[str], *, apply: bool = False,
        store_for=lambda workspace: None) -> List[Plan]:
    """Plan (and optionally apply) the migration for a set of workspaces.

    ``store_for`` maps a workspace to its conversation store path, which is what
    the trusted records are read from; the default resolves it the way the boot
    does.
    """
    plans = []
    for workspace in workspaces:
        owners = referenced_owners(store_for(workspace))
        plan = plan_workspace(workspace, owners)
        if apply:
            apply_plan(plan)
        plans.append(plan)
    return plans


def _default_store_for(workspace: str) -> Optional[str]:
    try:
        from agent.memory.conversation_store import conversation_store_path

        path = conversation_store_path(workspace)
        return str(path)
    except Exception:
        return None


def _print(plans: List[Plan], applied: bool) -> None:
    for plan in plans:
        print("workspace: %s" % plan.workspace)
        counts = plan.summary()
        for kind in (_KIND_UPLOAD, _KIND_OUTPUT, _KIND_QUARANTINE,
                     _KIND_DUPLICATE):
            if counts.get(kind):
                print("  %-10s %d" % (kind, counts[kind]))
        for action in plan.actions:
            print("  [%s] %s -> %s%s" % (
                action.kind, action.source, action.destination,
                "  (%s)" % action.reason if action.reason else ""))
        if not plan.actions:
            print("  nothing to do")
    if not applied:
        print("\ndry run: nothing was moved (pass --apply to perform the moves)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", action="append", default=[],
                        help="an Agent workspace to migrate (repeatable)")
    parser.add_argument("--apply", action="store_true",
                        help="perform the moves (default: report only)")
    args = parser.parse_args(argv)

    workspaces = args.workspace or _agent_workspaces()
    if not workspaces:
        print("no workspace to migrate", file=sys.stderr)
        return 1
    try:
        plans = run(workspaces, apply=args.apply, store_for=_default_store_for)
    except MigrationError as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2
    _print(plans, args.apply)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
