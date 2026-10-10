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
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

#: Directories that held uploads before the change (``tmp/`` is where
#: ``_get_upload_dir`` put them) and results produced by a platform callback.
LEGACY_UPLOAD_DIRS = ("tmp", "uploads")
LEGACY_OUTPUT_DIRS = ("outputs",)

#: Agent-internal entries at a workspace root that are never *business* files:
#: the persona, memory, knowledge, scheduler and the rest of the machinery. A
#: business migration MUST NOT move them, and the preflight names them as
#: excluded rather than guessing. Mirrors
#: ``agent.workspace.service.UNDELETABLE_ROOT_ENTRIES`` plus the ``user/``
#: container (whose contents are already in the unified layout); kept local so
#: this offline tool does not import the runtime's config-bearing modules.
CORE_ROOT_ENTRIES = frozenset({
    "AGENT.md", "USER.md", "RULE.md", "BOOTSTRAP.md", "MEMORY.md",
    "memory", "scheduler", "tmp", "knowledge", "skills", "websites",
    "subagents", "system", "plans",
})

#: The per-user container. Its contents already use the unified layout and are
#: never re-scanned; the recycle bin lives under it.
USER_CONTAINER_NAME = "user"

#: Root entries that hold credentials or runtime state rather than business data.
#: Moving them would break the Agent and drop secrets into a user's tree, so they
#: are refused like configuration. Dot-prefixed names (``.env``, ``.preview_secret``,
#: ``.git``) are refused by the same rule without being enumerated here.
NON_BUSINESS_ROOT_NAMES = frozenset({
    "mcp.json", "identity.db", "conversations.db", "memory.db",
})

#: The recycle bin names, mirrored from ``agent.workspace.service`` so this
#: offline tool can rewrite restore targets without importing the runtime.
TRASH_DIR_NAME = ".trash"
TRASH_META_NAME = "batch.json"

#: The quarantine container. Not a valid user id by construction (see module
#: docstring), so the existing ownership rule refuses it to every caller.
QUARANTINE_DIR_NAME = "_legacy"

#: JSON keys a trusted record uses to name a file it carried.
_PATH_KEYS = ("path", "file_path", "abs_path", "file")

#: Default file name for the exported attachment map, kept in step with
#: ``agent.workspace.legacy_attachment_map.DEFAULT_NAME`` (that module pulls in
#: the runtime's ``agent.workspace`` package, so this offline tool names the file
#: itself rather than importing it).
DEFAULT_MAP_NAME = "legacy_attachment_map.json"

_KIND_UPLOAD = "upload"
_KIND_OUTPUT = "output"
_KIND_BUSINESS = "business"
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

    ``digest`` is the source's content hash at plan time, recorded in the
    manifest; ``tenant``/``agent``/``evidence`` identify the object and the
    trusted record the owner came from. ``result`` is filled by
    :func:`apply_plan` and left empty by a dry run. ``mapping_to`` is the
    destination an old attachment should resolve to once the move is done (empty
    for a quarantined or duplicate file, which publishes no new location).
    """

    source: str
    destination: str
    kind: str
    owner: Optional[str] = None
    reason: str = ""
    digest: str = ""
    tenant: str = ""
    agent: str = ""
    evidence: str = ""
    mapping_to: str = ""
    result: str = ""


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
                  (_KIND_UPLOAD, _KIND_OUTPUT, _KIND_BUSINESS,
                   _KIND_QUARANTINE, _KIND_DUPLICATE)}
        for action in self.actions:
            counts[action.kind] = counts.get(action.kind, 0) + 1
        return counts

    def manifest(self) -> Dict:
        """The run's operational record (not runtime state).

        One entry per planned move with the object identity, the trusted
        evidence, the content digest and the outcome, so a later restore or an
        operator audit can re-derive every decision from real files rather than
        from a marker. Written to a protected location by :func:`write_manifest`.
        """
        return {
            "workspace": self.workspace,
            "actions": [
                {
                    "source": a.source,
                    "destination": a.destination,
                    "kind": a.kind,
                    "owner": a.owner,
                    "reason": a.reason,
                    "digest": a.digest,
                    "tenant": a.tenant,
                    "agent": a.agent,
                    "evidence": a.evidence,
                    "mapping_to": a.mapping_to,
                    "result": a.result,
                }
                for a in self.actions
            ],
        }

    def mapping(self) -> Dict[str, str]:
        """``{old absolute path: new absolute path}`` for published moves.

        Only files that ended up in the unified layout (``upload``, ``output``,
        ``business``) are mapped: a quarantined file publishes no new location,
        and a ``duplicate``'s content is already where it belongs. Chained
        redirection is impossible by construction -- the table is flat and only
        the migration command ever writes it.
        """
        out: Dict[str, str] = {}
        for action in self.actions:
            if action.kind in (_KIND_UPLOAD, _KIND_OUTPUT, _KIND_BUSINESS) \
                    and action.owner and action.destination:
                out[os.path.realpath(action.source)] = \
                    os.path.realpath(action.destination)
        return out


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


def _check_destination(destination: str) -> str:
    """Refuse a destination a link could redirect the write through.

    **Read-only**: the preflight MUST NOT create the target directory (spec
    ``agent-work-file-layout-migration``), so this only inspects, and
    :func:`_ensure_destination` does the ``mkdir`` at apply time. A parent that
    does not exist yet is not a symlink, so it passes here.
    """
    parent = os.path.dirname(destination)
    if os.path.islink(parent):
        raise MigrationError("refusing a linked destination: %s" % parent)
    return destination


def _ensure_destination(destination: str) -> str:
    """Create the destination's parent at apply time, then re-check the link.

    The window between planning and applying is exactly where a concurrent
    writer, or a symlink planted meanwhile, would land, so the check is repeated
    *after* the directory exists.
    """
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    if os.path.islink(parent):
        raise MigrationError("refusing a linked destination: %s" % parent)
    return destination


def _nearest_existing(path: str) -> str:
    """The closest existing ancestor of ``path`` (``path`` itself if it exists).

    Used for the device check: the destination usually does not exist yet, so
    its own ``st_dev`` is unavailable and the mount it *would* land on is the one
    its nearest existing ancestor already lives on.
    """
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _device_of(path: str) -> int:
    try:
        return os.stat(path).st_dev
    except OSError as exc:
        raise MigrationError("cannot stat %s: %s" % (path, exc))


def _assert_same_device(source: str, destination: str) -> None:
    """Refuse a move ``shutil.move`` would silently turn into copy+delete.

    ``rename`` across two filesystems raises ``EXDEV``, and the fallback copies
    then unlinks -- a window where a crash leaves neither a complete copy nor the
    original. The migration is a same-filesystem operation by requirement, so a
    cross-device destination is refused rather than papered over.
    """
    if _device_of(_nearest_existing(source)) != _device_of(
            _nearest_existing(destination)):
        raise MigrationError(
            "cross-device move refused: %s -> %s" % (source, destination))


def _digest_or_empty(source: str) -> str:
    try:
        return _sha256(source)
    except OSError:
        return ""


def plan_workspace(workspace: str,
                   owners_by_path: Dict[str, Set[str]],
                   *,
                   legacy_files: Optional[Iterable[Tuple[str, str]]] = None,
                   business_files: Optional[Iterable[str]] = None,
                   exclude: Iterable[str] = (),
                   tenant: str = "",
                   agent: str = "") -> Plan:
    """Decide what happens to each legacy file, without touching anything.

    Two inventories are planned into one table:

    * the ``tmp/``/``uploads/``/``outputs/`` payloads whose destination is keyed
      by *basename* (that is the historical contract, and renaming them would
      break the platform callbacks that re-derive the path);
    * *business* files that lived at the workspace root -- documents the user
      made or uploaded -- whose **relative structure is preserved** under
      ``user/<uid>/`` so a nested tree keeps its shape.

    The preflight only reads. It never creates the quarantine directory or any
    destination (spec ``agent-work-file-layout-migration``); the ``mkdir``
    happens in :func:`apply_plan`. Ambiguous ownership, configuration mixed into
    the business inventory, and destination collisions are *reported* by being
    quarantined with a reason, never guessed at.
    """
    workspace = os.path.realpath(workspace)
    plan = Plan(workspace=workspace)
    files = list(legacy_files if legacy_files is not None
                 else _legacy_files(workspace))
    excluded = {QUARANTINE_DIR_NAME, USER_CONTAINER_NAME, *exclude}

    def _not_business(name: str) -> bool:
        """A root entry name that is configuration, credentials or state.

        Refused rather than migrated: ``AGENT.md`` is the Agent's identity and
        ``.env``/``mcp.json`` are its credentials, neither of which belongs in a
        member's business directory.
        """
        return (not name or name.startswith(".") or name in excluded
                or name in CORE_ROOT_ENTRIES
                or name in NON_BUSINESS_ROOT_NAMES)

    # Two legacy files that would land on the same destination (same owner, same
    # name, different source directory) cannot both be published under one name.
    # The first in a stable order wins; the rest are quarantined, never merged.
    # A published destination is claimed here; the quarantined half is unique by
    # construction and re-checked in ``_quarantine``.
    claimed: Dict[str, str] = {}

    def _claim(source: str, destination: str, kind: str, owner: str,
               reason: str = "", evidence: str = "") -> None:
        if _device_of(_nearest_existing(source)) != _device_of(
                _nearest_existing(destination)):
            _quarantine(plan, workspace, source, "cross-device move refused",
                        owner=owner, tenant=tenant, agent=agent)
            return
        if os.path.exists(destination):
            try:
                same = _sha256(source) == _sha256(destination)
            except OSError:
                same = False
            if same:
                _quarantine(plan, workspace, source,
                            "already migrated to %s" % destination,
                            kind=_KIND_DUPLICATE, owner=owner,
                            tenant=tenant, agent=agent)
            else:
                _quarantine(plan, workspace, source,
                            "destination holds different content", owner=owner,
                            tenant=tenant, agent=agent)
            return
        plan.actions.append(Action(
            source, destination, kind, owner=owner, reason=reason,
            digest=_digest_or_empty(source), tenant=tenant, agent=agent,
            evidence=evidence, mapping_to=destination))

    for source, kind in sorted(files, key=lambda item: item[0]):
        real_source = os.path.realpath(source)
        owners = sorted(owners_by_path.get(real_source, ()))

        if len(owners) == 1:
            owner = owners[0]
            destination = _check_destination(_destination(
                workspace, owner, kind, source))
            if destination in claimed:
                _quarantine(plan, workspace, source,
                            "two legacy files claim %s" % destination,
                            owner=owner, tenant=tenant, agent=agent)
                continue
            claimed[destination] = real_source
        elif len(owners) > 1:
            _quarantine(plan, workspace, source,
                        "referenced by %d users" % len(owners),
                        tenant=tenant, agent=agent)
            continue
        else:
            _quarantine(plan, workspace, source, "no trusted owner recorded",
                        tenant=tenant, agent=agent)
            continue

        _claim(source, destination, kind, owner)

    if business_files is None:
        business_files = _root_business_entries(workspace, _not_business)
    for source in sorted(_iter_business_files(business_files)):
        rel = os.path.relpath(os.path.realpath(source), workspace)
        top = rel.split(os.sep, 1)[0]
        if _not_business(top):
            # Configuration mixed into the business inventory is refused, not
            # migrated: ``AGENT.md`` is the Agent's identity and ``.env`` is its
            # credentials.
            _quarantine(plan, workspace, source,
                        "configuration entry %s is not business data" % top,
                        tenant=tenant, agent=agent)
            continue
        real_source = os.path.realpath(source)
        owners = sorted(owners_by_path.get(real_source, ()))
        if len(owners) != 1:
            reason = ("referenced by %d users" % len(owners) if owners
                      else "no trusted owner recorded")
            _quarantine(plan, workspace, source, reason,
                        tenant=tenant, agent=agent)
            continue
        owner = owners[0]
        destination = _check_destination(
            os.path.join(workspace, USER_CONTAINER_NAME, owner, rel))
        if destination in claimed:
            _quarantine(plan, workspace, source,
                        "two business files claim %s" % destination,
                        owner=owner, tenant=tenant, agent=agent)
            continue
        claimed[destination] = real_source
        _claim(source, destination, _KIND_BUSINESS, owner,
               evidence="trusted conversation owner")

    return plan


def _root_business_entries(workspace: str,
                           is_excluded) -> List[str]:
    """Root entries that are candidates for the *business* inventory.

    A candidate is a real file or directory at the workspace root that is not
    Agent configuration, credentials or state, not the per-user container, and
    not one of the legacy upload directories (those are planned by basename
    through :func:`_legacy_files`, and planning them twice would move them
    twice). ``is_excluded`` is the caller's predicate so the automatic scan and
    the explicit ``--business`` path agree on one rule.
    """
    try:
        names = sorted(os.listdir(workspace))
    except OSError as exc:
        raise MigrationError("cannot list %s: %s" % (workspace, exc))
    out: List[str] = []
    for name in names:
        if is_excluded(name) or name in LEGACY_UPLOAD_DIRS \
                or name in LEGACY_OUTPUT_DIRS:
            continue
        out.append(os.path.join(workspace, name))
    return out


def _iter_business_files(paths: Iterable[str]) -> Iterable[str]:
    """Expand business candidates to individual files, never following links.

    Ownership is decided per file, from the trusted record that named *that*
    file, so a directory holding files from two members is split correctly
    instead of being attributed wholesale. A symlink is skipped: moving one
    would publish whatever it points at under the target's owner.
    """
    for path in paths:
        if os.path.islink(path):
            continue
        if os.path.isdir(path):
            for current, dirs, names in os.walk(path):
                dirs[:] = sorted(
                    d for d in dirs
                    if not os.path.islink(os.path.join(current, d)))
                for name in sorted(names):
                    candidate = os.path.join(current, name)
                    if not os.path.islink(candidate):
                        yield candidate
        elif os.path.isfile(path):
            yield path


def _quarantine(plan: Plan, workspace: str, source: str, reason: str, *,
                kind: str = _KIND_QUARANTINE,
                owner: Optional[str] = None,
                tenant: str = "", agent: str = "") -> None:
    """Plan ``source`` into the protected half, refusing a taken destination.

    The quarantine path is derived from the source's own relative path, so a
    collision here would mean the same source was seen twice; failing loudly
    beats silently dropping the second file.
    """
    destination = _check_destination(_quarantine_destination(workspace, source))
    if any(action.destination == destination for action in plan.actions):
        raise MigrationError("two legacy files share %s" % destination)
    plan.actions.append(Action(
        source, destination, kind, owner=owner, reason=reason,
        digest=_digest_or_empty(source), tenant=tenant, agent=agent))


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def apply_plan(plan: Plan) -> Plan:
    """Perform ``plan`` in place, hashing before removing any source.

    A crash between the move's copy and its unlink leaves the destination
    complete and the source intact: the next run finds equal hashes, records a
    ``duplicate`` and moves the (now redundant) source out of the legacy region,
    so the run is idempotent rather than half-done -- and the legacy region still
    ends up empty, which is the point of running it at all.

    Nothing is ever overwritten: a destination that appeared between planning and
    applying sends the source to the quarantine instead. A single failing file is
    recorded as ``failed`` in the manifest and does not abort the batch, so a
    retry resumes where it stopped (spec: idempotent retry, no online state
    machine).
    """
    for action in plan.actions:
        try:
            _ensure_destination(action.destination)
            _assert_same_device(action.source, action.destination)
        except MigrationError as exc:
            action.result = "failed"
            action.reason = _append_reason(action.reason, str(exc))
            continue
        if not os.path.exists(action.source):
            action.result = "missing"
            action.reason = _append_reason(action.reason, "source already gone")
            continue
        if os.path.exists(action.destination):
            # Re-checked here, not just at plan time: the window between the two
            # is exactly where a concurrent writer would land.
            try:
                same = _sha256(action.source) == _sha256(action.destination)
            except OSError:
                same = False
            if same:
                action.kind = _KIND_DUPLICATE
                action.reason = _append_reason(
                    action.reason, "already migrated to %s" % action.destination)
            else:
                action.kind = _KIND_QUARANTINE
                action.reason = _append_reason(
                    action.reason, "destination appeared during the run")
            action.mapping_to = ""
            action.destination = _quarantine_destination(plan.workspace,
                                                        action.source)
            try:
                _ensure_destination(action.destination)
            except MigrationError as exc:
                action.result = "failed"
                action.reason = _append_reason(action.reason, str(exc))
                continue
        try:
            shutil.move(action.source, action.destination)
        except OSError as exc:
            action.result = "failed"
            action.reason = _append_reason(action.reason, str(exc))
            continue
        action.result = {
            _KIND_DUPLICATE: "duplicate",
            _KIND_QUARANTINE: "quarantined",
        }.get(action.kind, "moved")
    return plan


def _append_reason(reason: str, extra: str) -> str:
    return "%s; %s" % (reason, extra) if reason else extra


# ---------------------------------------------------------------------------
# Manifest, mapping and recycle-bin rewrite (offline, no runtime state)
# ---------------------------------------------------------------------------

def _write_protected_json(path: str, payload: Dict) -> None:
    """Write an internal artifact ``0600``, atomically.

    The manifest and the attachment map name real files and their owners, so
    they are written to a protected path rather than into the workspace, and the
    rename keeps a reader from seeing a half-written table.
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_manifest(plans: Iterable[Plan], path: str) -> str:
    """Persist every planned move, its digest and its outcome as one JSON file."""
    payload = {
        "version": 1,
        "generated_at": time.time(),
        "plans": [plan.manifest() for plan in plans],
    }
    _write_protected_json(path, payload)
    return os.path.abspath(path)


def write_mapping(plans: Iterable[Plan], path: str) -> Dict[str, str]:
    """Build and persist the **single-hop** ``old path -> new path`` map.

    Only published moves are mapped, so the table is flat and terminal: nothing
    points at a path that is itself an old path, which is what makes a lookup a
    single table hit rather than a walk (spec: one hop, no chaining).
    """
    mapping: Dict[str, str] = {}
    for plan in plans:
        mapping.update(plan.mapping())
    _write_protected_json(path, {"version": 1, "mapping": mapping})
    return mapping


def load_mapping(path: str) -> Dict[str, str]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    raw = data.get("mapping") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in (raw or {}).items()}


def _iter_trash_batches(workspace: str):
    """``(user_id, batch_id, batch.json path)`` for every bin under ``user/``."""
    user_root = os.path.join(workspace, USER_CONTAINER_NAME)
    if not os.path.isdir(user_root):
        return
    for uid in sorted(os.listdir(user_root)):
        trash = os.path.join(user_root, uid, TRASH_DIR_NAME)
        if not os.path.isdir(trash):
            continue
        for batch in sorted(os.listdir(trash)):
            meta = os.path.join(trash, batch, TRASH_META_NAME)
            if os.path.isfile(meta):
                yield uid, batch, meta


def _rel_candidates(workspace: str, rel: str) -> List[Tuple[str, str]]:
    """``(absolute path, prefix)`` forms a recorded ``rel`` may take.

    The bin is written by the runtime, whose service root is either the Agent
    workspace (``rel`` is workspace-relative) or the tenant's shared root (``rel``
    is ``agents/<id>/...``). Both are tried, and the prefix that matched is
    carried along so a rewritten path keeps the same shape.
    """
    out: List[Tuple[str, str]] = [
        (os.path.realpath(os.path.join(workspace, rel)), "")]
    base = os.path.basename(workspace)
    parent = os.path.basename(os.path.dirname(workspace))
    prefix = "%s/%s/" % (parent, base)
    if parent == "agents" and rel.startswith(prefix):
        out.append((os.path.realpath(
            os.path.join(workspace, rel[len(prefix):])), prefix))
    return out


def rewrite_trash_targets(workspace: str, mapping: Dict[str, str], *,
                          apply: bool = False,
                          backup: bool = True) -> List[Dict]:
    """Point a trashed item's *recorded original path* at the migrated location.

    A legacy file could be moved to the unified layout while a trashed sibling
    still remembers a path in the legacy region; restoring that sibling would
    put it back where a future owner-only read would not find it. This rewrites
    the recorded path, backs the original metadata up beside it, and adds no new
    restore protocol -- the existing restore service keeps its conflict rules.

    Returns one report dict per changed batch; ``apply=False`` (the default) is a
    dry run.
    """
    workspace = os.path.realpath(workspace)
    reports: List[Dict] = []
    for uid, batch, meta_path in _iter_trash_batches(workspace):
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        changes: List[Dict[str, str]] = []
        for item in meta.get("items") or []:
            if not isinstance(item, dict):
                continue
            rel = item.get("rel")
            if not isinstance(rel, str) or not rel:
                continue
            for candidate, prefix in _rel_candidates(workspace, rel):
                new_abs = mapping.get(candidate)
                if not new_abs:
                    continue
                new_rel = prefix + os.path.relpath(
                    new_abs, workspace).replace(os.sep, "/")
                item["rel"] = new_rel
                changes.append({"from": rel, "to": new_rel})
                break
        if not changes:
            continue
        reports.append({
            "user_id": uid, "batch_id": batch, "path": meta_path,
            "changes": changes, "applied": bool(apply),
        })
        if not apply:
            continue
        if backup:
            backup_path = meta_path + ".pre-migration.bak"
            if not os.path.exists(backup_path):
                shutil.copy2(meta_path, backup_path)
        tmp = meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        os.replace(tmp, meta_path)
    return reports


def restore_manifest(manifest_path: str, *, apply: bool = False) -> List[Dict]:
    """Move every recorded published destination back to its source (undo).

    Only ``upload``/``output``/``business`` moves are reverted. A ``quarantine``
    or ``duplicate`` entry is deliberately left alone: the quarantine is a
    protective placement, and undoing it would re-expose the file to the tenant
    the migration had just taken it away from.

    A source that exists again -- a file written *after* the migration -- is
    **never** overwritten: the entry is reported as ``occupied`` and skipped, so
    restoring an old export cannot clobber newer work (spec: post-cutover files
    survive a restore).
    """
    with open(manifest_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    results: List[Dict] = []
    for plan in data.get("plans") or []:
        for action in plan.get("actions") or []:
            if action.get("kind") in (_KIND_QUARANTINE, _KIND_DUPLICATE):
                continue
            source = action.get("source")
            destination = action.get("destination")
            if not source or not destination or not os.path.exists(destination):
                continue
            if os.path.exists(source):
                results.append({"source": source, "destination": destination,
                                "status": "occupied"})
                continue
            if apply:
                _ensure_destination(source)
                shutil.move(destination, source)
            results.append({"source": source, "destination": destination,
                            "status": "restored" if apply else "restorable"})
    return results


def verify_manifest(manifest_path: str) -> Dict:
    """Check the recorded moves against the filesystem for a spot audit.

    Reports, per published action, whether the digest still matches at the
    destination and whether any source remains, so an operator can confirm a run
    before the backup is retired.
    """
    with open(manifest_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    checked = mismatched = remaining = 0
    for plan in data.get("plans") or []:
        for action in plan.get("actions") or []:
            if action.get("kind") in (_KIND_QUARANTINE, _KIND_DUPLICATE):
                continue
            checked += 1
            destination = action.get("destination") or ""
            digest = action.get("digest") or ""
            if not os.path.exists(destination) or (
                    digest and _sha256(destination) != digest):
                mismatched += 1
            if os.path.exists(action.get("source") or ""):
                remaining += 1
    return {"checked": checked, "mismatched": mismatched,
            "source_remaining": remaining}


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
        store_for=lambda workspace: None,
        business_for=None,
        exclude: Iterable[str] = (),
        manifest: Optional[str] = None,
        mapping: Optional[str] = None,
        rewrite_trash: bool = False) -> List[Plan]:
    """Plan (and optionally apply) the migration for a set of workspaces.

    ``store_for`` maps a workspace to its conversation store path, which is what
    the trusted records are read from; the default resolves it the way the boot
    does. ``business_for`` optionally returns the explicit business candidates
    for a workspace (otherwise the root is scanned). ``manifest``/``mapping``
    are output paths for the run's artifacts, written even on a dry run so a
    preflight leaves a record; ``rewrite_trash`` repoints trashed items' recorded
    original paths at the migrated locations.

    All outputs are plain files: there is no online state machine and no marker
    in the workspace, so a re-run recomputes everything from the trusted records
    and the filesystem.
    """
    plans = []
    for workspace in workspaces:
        owners = referenced_owners(store_for(workspace))
        business = business_for(workspace) if business_for else None
        plan = plan_workspace(workspace, owners, business_files=business,
                              exclude=exclude)
        if apply:
            apply_plan(plan)
        plans.append(plan)

    combined: Dict[str, str] = {}
    for plan in plans:
        combined.update(plan.mapping())
    if manifest:
        write_manifest(plans, manifest)
    if mapping and apply:
        # Only once the moves happened: a *prospective* table would claim the old
        # paths were relocated while they are still there, and
        # ``resolve_attachment_path`` would then read its own old path as
        # "occupied" and refuse to serve it.
        write_mapping(plans, mapping)
    if apply and rewrite_trash:
        for plan in plans:
            rewrite_trash_targets(plan.workspace, combined, apply=True)
    return plans


def _default_mapping_path() -> str:
    """Where the runtime reads the table from, so the export lands where it looks."""
    from common.state_dir import state_path

    return str(state_path("migration", DEFAULT_MAP_NAME))


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
        for kind in (_KIND_UPLOAD, _KIND_OUTPUT, _KIND_BUSINESS,
                     _KIND_QUARANTINE, _KIND_DUPLICATE):
            if counts.get(kind):
                print("  %-10s %d" % (kind, counts[kind]))
        for action in plan.actions:
            print("  [%s] %s -> %s%s%s" % (
                action.kind, action.source, action.destination,
                "  (%s)" % action.reason if action.reason else "",
                "  <%s>" % action.result if action.result else ""))
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
    parser.add_argument("--business", action="append", default=[],
                        metavar="REL",
                        help="an explicit business path, relative to the "
                             "workspace (repeatable); default: scan the root")
    parser.add_argument("--exclude", action="append", default=[],
                        metavar="NAME",
                        help="an extra root entry to leave alone (repeatable)")
    parser.add_argument("--manifest",
                        help="write the run's manifest (paths, digests, "
                             "outcomes) to this protected JSON file")
    parser.add_argument("--mapping", nargs="?", const="@default", default=None,
                        metavar="PATH",
                        help="with --apply: write the single-hop old->new "
                             "attachment map to this protected JSON file; omit "
                             "the value to use the state-root path the runtime "
                             "reads")
    parser.add_argument("--rewrite-trash", action="store_true",
                        help="with --apply: repoint trashed items' recorded "
                             "original paths at the migrated locations")
    parser.add_argument("--verify", metavar="MANIFEST",
                        help="spot-audit a manifest against the filesystem and "
                             "exit")
    parser.add_argument("--restore", metavar="MANIFEST",
                        help="undo a migration recorded in the manifest "
                             "(add --apply to perform it) and exit")
    args = parser.parse_args(argv)

    if args.verify:
        report = verify_manifest(args.verify)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if not report["mismatched"] and not report["source_remaining"] \
            else 3

    if args.restore:
        try:
            results = restore_manifest(args.restore, apply=args.apply)
        except (OSError, ValueError) as e:
            print("refused: %s" % e, file=sys.stderr)
            return 2
        for row in results:
            print("  [%s] %s <- %s" % (row["status"], row["source"],
                                       row["destination"]))
        if not args.apply:
            print("\ndry run: nothing was moved (pass --apply to perform it)")
        return 0

    workspaces = args.workspace or _agent_workspaces()
    if not workspaces:
        print("no workspace to migrate", file=sys.stderr)
        return 1

    business_for = None
    if args.business:
        def business_for(workspace):  # noqa: F811 - deliberate local override
            found = []
            for rel in args.business:
                candidate = os.path.join(workspace, rel)
                if os.path.exists(candidate):
                    found.append(candidate)
            return found

    try:
        plans = run(workspaces, apply=args.apply, store_for=_default_store_for,
                    business_for=business_for, exclude=args.exclude,
                    manifest=args.manifest,
                    mapping=(_default_mapping_path()
                             if args.mapping == "@default" else args.mapping),
                    rewrite_trash=args.rewrite_trash)
    except MigrationError as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2
    _print(plans, args.apply)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
