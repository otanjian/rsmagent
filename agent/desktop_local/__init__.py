"""The trusted same-machine registry of local project roots.

Change ``align-desktop-project-execution-with-master`` (task 3.2 / 3.3). The
absolute path of a picked directory never leaves the client machine — but in
**local mode** the client machine and the Python backend are the same machine.
The desktop main process therefore registers the resolved root with the backend
it just started, over the loopback origin it already trusts, and the backend
keeps it in memory for the life of the process.

Three rules make this safe rather than a hole in D4:

1. **Registration is native-only, loopback-only, and scoped.** A caller must
   present the native credential for the same user, from a registered loopback
   origin, and name a live binding it owns. A page, a model, or an ordinary Web
   request cannot register a root (see ``integrations/desktop/local_root.py``).
2. **Nothing is persisted and nothing is forwarded.** The registry is a plain
   in-process dict; it is never written to disk and never enters a response, a
   prompt, an artifact or a protocol frame. A backend restart starts empty, so a
   stale root cannot be reused after the client is gone.
3. **A lookup is re-authorized.** Consumers ask for the root *for a target*; the
   entry is only returned when user / tenant / device / workspace / binding /
   grant version all still match, and the caller's ambient identity agrees.

This module is deliberately free of I/O so the scope math is unit-testable.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from common.log import logger

_MAX_ROOT = 4096


class LocalRootError(ValueError):
    """A refused registration or lookup, with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LocalRootEntry:
    """One registered local root, keyed by the authorization that produced it."""

    user_id: str
    tenant_id: str
    device_id: str
    workspace_id: str
    binding_id: str
    grant_version: int
    project_mode: str
    absolute_path: str

    def scope_key(self) -> tuple:
        return (
            self.user_id, self.tenant_id, self.device_id,
            self.workspace_id, self.binding_id,
        )


def _clean(value: Optional[str], what: str, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise LocalRootError("invalid_request", f"{what} is required")
    if len(text) > 128:
        raise LocalRootError("invalid_request", f"{what} is too long")
    return text


def _clean_path(raw: Optional[str]) -> str:
    text = str(raw or "").strip()
    if not text or "\x00" in text:
        raise LocalRootError("invalid_request", "an absolute path is required")
    if len(text) > _MAX_ROOT:
        raise LocalRootError("invalid_request", "the path is too long")
    # A registered root is what the OS resolved for the picker. A relative path
    # is never a picker result, so refuse rather than guess a base directory.
    if not os.path.isabs(text):
        raise LocalRootError("invalid_request", "the registered root must be absolute")
    return os.path.normpath(text)


class LocalRootRegistry:
    """In-memory, per-process. One entry per authorization scope."""

    def __init__(self) -> None:
        self._entries: Dict[tuple, LocalRootEntry] = {}
        self._lock = threading.Lock()

    def register(
        self,
        *,
        user_id: str,
        tenant_id: str,
        device_id: str,
        workspace_id: str,
        binding_id: str,
        grant_version: int,
        project_mode: str,
        absolute_path: str,
    ) -> LocalRootEntry:
        from agent.workspace.execution_target import (
            MODE_PROJECT_EXECUTION, MODE_READONLY_INPUT,
        )
        if project_mode not in (MODE_READONLY_INPUT, MODE_PROJECT_EXECUTION):
            raise LocalRootError("invalid_request", f"unknown project mode: {project_mode!r}")
        try:
            version = int(grant_version)
        except (TypeError, ValueError):
            raise LocalRootError("invalid_request", "grant_version must be an integer")
        if version < 1:
            raise LocalRootError("invalid_request", "grant_version must be positive")

        entry = LocalRootEntry(
            user_id=_clean(user_id, "user_id"),
            tenant_id=_clean(tenant_id, "tenant_id", required=False),
            device_id=_clean(device_id, "device_id"),
            workspace_id=_clean(workspace_id, "workspace_id"),
            binding_id=_clean(binding_id, "binding_id"),
            grant_version=version,
            project_mode=project_mode,
            absolute_path=_clean_path(absolute_path),
        )
        with self._lock:
            self._entries[entry.scope_key()] = entry
        logger.info(
            "[LocalRoot] registered workspace %s for device %s (mode=%s, v%d)",
            entry.workspace_id, entry.device_id, entry.project_mode, entry.grant_version,
        )
        return entry

    def lookup(
        self,
        *,
        user_id: str,
        tenant_id: str = "",
        device_id: str,
        workspace_id: str,
        binding_id: str,
        grant_version: int,
        require_mode: Optional[str] = None,
    ) -> Optional[LocalRootEntry]:
        """The entry for exactly this authorization, or None.

        A mismatch on any identifier (including the grant version) is a miss,
        never a fallback to a "close enough" entry: a re-picked directory bumps
        the version and must invalidate the old one.
        """
        key = (user_id, tenant_id, device_id, workspace_id, binding_id)
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.grant_version != int(grant_version or 0):
            return None
        if require_mode is not None and entry.project_mode != require_mode:
            return None
        return entry

    def revoke(
        self,
        *,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        device_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        binding_id: Optional[str] = None,
    ) -> int:
        """Drop every entry matching the given (non-None) identifiers.

        Called when a grant is explicitly revoked, a device disconnects, or the
        account/tenant/server changes. A revoked entry must stop being servable
        immediately, not at the next command's re-check.
        """
        removed = 0
        with self._lock:
            for key in list(self._entries):
                entry = self._entries[key]
                if user_id is not None and entry.user_id != user_id:
                    continue
                if tenant_id is not None and entry.tenant_id != tenant_id:
                    continue
                if device_id is not None and entry.device_id != device_id:
                    continue
                if workspace_id is not None and entry.workspace_id != workspace_id:
                    continue
                if binding_id is not None and entry.binding_id != binding_id:
                    continue
                del self._entries[key]
                removed += 1
        return removed

    def entry_for_path(
        self,
        absolute_path: Optional[str],
        *,
        user_id: str,
        tenant_id: str = "",
    ) -> Optional[LocalRootEntry]:
        """The live registration that really holds ``absolute_path``, or None.

        The question a replayed history card asks is *which* authorization
        produced a file: the run that wrote it may have happened in a project
        the session has since switched away from, and the answer decides both
        the card's identity (device/workspace/binding/relative path) and whether
        the card may be acted on at all. Deriving it from the session's current
        target instead would either lose the card when the directory changed or
        -- worse -- re-file it under the project that happens to be open now.

        Three properties, each of which has been a bug elsewhere in this change:

        * **live only.** Only entries that are in the registry *right now*
          answer. A revoked grant, a disconnected device or a re-picked
          directory is not resurrected from "recently used": that would make the
          most recent candidate stand in for an authorization.
        * **this user, this tenant.** Another account's registration for the
          same directory is not this caller's project, even though the bytes are
          on the same machine.
        * **innermost wins, realpath both sides.** Nested projects both hold a
          path, and the card needs the closest one; and the comparison is
          real-to-real so a link inside a project cannot make a file elsewhere
          look like a member of it.
        """
        try:
            real_path = os.path.realpath(os.path.expanduser(str(absolute_path or "")))
        except (TypeError, ValueError, OSError):
            return None
        # A relative path is never a recorded artifact location (the tools record
        # absolute ones), so it is "no project" rather than a guess against cwd.
        if not absolute_path or not os.path.isabs(str(absolute_path)):
            return None
        if "\x00" in str(absolute_path):
            return None

        best: Optional[LocalRootEntry] = None
        with self._lock:
            candidates = list(self._entries.values())
        for entry in candidates:
            if entry.user_id != user_id or entry.tenant_id != tenant_id:
                continue
            try:
                real_root = os.path.realpath(entry.absolute_path)
            except (TypeError, ValueError, OSError):
                continue
            if not real_root:
                continue
            if real_path != real_root and not real_path.startswith(real_root + os.sep):
                continue
            if best is None or len(real_root) > len(os.path.realpath(best.absolute_path)):
                best = entry
        return best

    def clear(self) -> int:
        """Forget everything (identity switch, shutdown)."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
        return count

    def list_for_device(self, device_id: str) -> List[LocalRootEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.device_id == device_id]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_registry: Optional[LocalRootRegistry] = None
_registry_lock = threading.Lock()


def registry() -> LocalRootRegistry:
    """The process-wide registry (there is one local backend per process)."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = LocalRootRegistry()
    return _registry


def reset_registry() -> None:
    """Test hook: drop the process-wide registry."""
    global _registry
    with _registry_lock:
        _registry = None
