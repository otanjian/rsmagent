# encoding:utf-8
"""Single-hop old-path -> new-path map for attachments moved by the migration.

change ``unify-private-agent-personal-workspace`` (task 3.3). When the layout
migration (``scripts/migrate_agent_user_files.py``) moves a legacy business file
into ``user/<uid>/``, a conversation that already recorded the file's absolute
location still names the *old* path. Rather than rewrite stored messages -- which
would touch every payload shape and every historical artifact -- the migration
exports a flat, protected table and the read paths consult it.

Three properties are load-bearing:

* **one hop** -- the table maps an old absolute path straight to its final
  location. A value that is itself a key would be a chain, and a chain is how a
  mapping turns into an open redirect; :func:`resolve_attachment_path` refuses it;
* **no new authority** -- resolution happens *before* the ordinary gates and
  grants nothing of its own. The mapped path is re-authorized exactly like any
  other path (owner, tenant, private-Agent rule), so the map cannot serve as an
  anonymous token, an administrator bypass, or a way to read another member's
  file;
* **no guessing** -- only exact absolute-path lookups. A relative reference whose
  original working directory is unknown is left alone rather than resolved
  against a directory it may never have belonged to.

A missing or corrupt table is treated as *no table at all*: new sessions and new
writes must not depend on a migration artifact (spec requirement).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Dict, Optional

#: Environment override for the table location, mainly for tests and for an
#: install that keeps its state somewhere unusual.
ENV_VAR = "RSM_LEGACY_ATTACHMENT_MAP"

#: Default file name under the (protected) state root's ``migration/`` folder.
DEFAULT_NAME = "legacy_attachment_map.json"

#: Resolution outcomes.
DIRECT = "direct"      # untouched: normal behaviour, no mapping involved
MAPPED = "mapped"      # the old path maps to an existing new location
CONFLICT = "conflict"  # the old path is occupied (or the table chains): refuse
MISSING = "missing"    # a mapping exists but its target is gone: refuse

_LOCK = threading.Lock()
_CACHE: Dict[str, object] = {"path": None, "mtime": None, "table": {}}


@dataclass(frozen=True)
class AttachmentResolution:
    """Where an addressed path should be read from, and why."""

    path: str
    status: str

    @property
    def ok(self) -> bool:
        return self.status in (DIRECT, MAPPED)


def map_file_path() -> str:
    """The protected table location: env override, else the state root."""
    override = (os.environ.get(ENV_VAR) or "").strip()
    if override:
        return os.path.realpath(os.path.expanduser(override))
    try:
        from common.state_dir import state_path

        return str(state_path("migration", DEFAULT_NAME))
    except Exception:  # noqa: BLE001 - no state root: compatibility stays off
        return os.path.join(os.sep, "nonexistent", DEFAULT_NAME)


def load_map(path: Optional[str] = None) -> Dict[str, str]:
    """The map, cached on the file's mtime. Never raises.

    A failed read (missing file, bad JSON, a directory in the way) yields an
    empty table. That is deliberate: the compatibility table is an operational
    artifact, and a reader must degrade to "no mapping" rather than break a
    session or a write because an old file is unreadable.
    """
    target = path or map_file_path()
    try:
        mtime = os.stat(target).st_mtime_ns
    except OSError:
        with _LOCK:
            if _CACHE["path"] == target:
                _CACHE.update(mtime=None, table={})
        return {}
    with _LOCK:
        if _CACHE["path"] == target and _CACHE["mtime"] == mtime:
            return dict(_CACHE["table"])  # type: ignore[arg-type]
    table: Dict[str, str] = {}
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        raw = data.get("mapping") if isinstance(data, dict) else None
        if isinstance(raw, dict):
            for old, new in raw.items():
                if isinstance(old, str) and isinstance(new, str) and old and new:
                    table[os.path.realpath(old)] = new
    except (OSError, ValueError):
        table = {}
    with _LOCK:
        _CACHE.update(path=target, mtime=mtime, table=table)
    return dict(table)


def reset_cache() -> None:
    """Drop the cache (tests, and after the table is rewritten in-process)."""
    with _LOCK:
        _CACHE.update(path=None, mtime=None, table={})


def resolve_attachment_path(path: str, *, map_path: Optional[str] = None,
                            exists=None) -> AttachmentResolution:
    """Resolve ``path`` through the map, returning the location to read.

    The decision table (spec: single hop, refuse ambiguous or reused paths):

    * the table is empty -> ``direct``, i.e. unchanged behaviour;
    * the addressed path still exists and is a mapped key -> ``conflict``: the
      old location is occupied by *something else* now, and reading it would
      serve a same-named replacement instead of the original;
    * the addressed path is a key and its target exists -> ``mapped``;
    * the target is gone, or the table chains -> ``missing``/``conflict``;
    * the path is not a key -> ``direct`` (the ordinary gates decide).
    """
    probe = exists if exists is not None else os.path.exists
    if not path:
        return AttachmentResolution(path, DIRECT)
    real = os.path.realpath(path)
    table = load_map(map_path)
    if not table:
        return AttachmentResolution(real, DIRECT)
    target = table.get(real)
    if target is None:
        return AttachmentResolution(real, DIRECT)
    if probe(real):
        return AttachmentResolution(real, CONFLICT)
    target_real = os.path.realpath(target)
    if target_real in table:
        # A value that is itself a key is a chain; a chain is never followed.
        return AttachmentResolution(real, CONFLICT)
    if not probe(target_real):
        return AttachmentResolution(target_real, MISSING)
    return AttachmentResolution(target_real, MAPPED)
