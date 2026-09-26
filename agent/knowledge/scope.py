"""The effective knowledge document set and managed-path protection.

Change ``add-traceable-knowledge-ingestion`` (design D7 / D9, task 1.3).

Every knowledge entry point -- directory listing, ``index.md`` regeneration,
the graph, ``MemoryManager`` sync/rebuild and direct path reads -- must agree
on one eligible set:

    ordinary ``*.md`` (hidden and managed subtrees excluded)
  + converted ``*.md`` whose ``task_id`` is the source's current
    ``active_task_id``

Originals, staging, control records and historical conversion batches are
always excluded. A disabled or deleted source's converted text leaves the set
immediately, before any index cache is physically cleaned, so a stale index can
never keep returning expired content.

A root that has never opted into source assets has no registered managed
directories, so this filter and guard are transparent there: legacy MD/TXT
behaviour is byte-for-byte unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, List, Optional

from agent.knowledge.catalog import KnowledgeCatalog


class ManagedPathError(ValueError):
    """A request targeted a registered managed subtree.

    Distinct from an ordinary invalid path: the API maps it to
    ``409 managed_knowledge_path`` and MUST leave no filesystem side effect.
    """


class KnowledgeScope:
    """Effective-set and write-protection view of one knowledge root."""

    def __init__(self, knowledge_root: str):
        self.root = Path(knowledge_root).resolve()
        self._catalog: Optional[KnowledgeCatalog] = None
        self._resolved = False

    # ------------------------------------------------------------------
    # Managed directories
    # ------------------------------------------------------------------
    @property
    def catalog(self) -> Optional[KnowledgeCatalog]:
        """The root's catalog, or ``None`` when it never registered one."""
        if not self._resolved:
            self._catalog = KnowledgeCatalog.open_if_exists(str(self.root))
            self._resolved = True
        return self._catalog

    def refresh(self) -> None:
        """Forget the cached catalog after an initialisation or mode switch."""
        self._catalog = None
        self._resolved = False

    def managed_dirs(self) -> List[Path]:
        catalog = self.catalog
        return catalog.registered_managed_dirs() if catalog else []

    @staticmethod
    def _contains(ancestor: str, descendant: str) -> bool:
        try:
            return os.path.commonpath([ancestor, descendant]) == ancestor
        except ValueError:
            return False

    def is_managed(self, path) -> bool:
        """True when ``path`` is inside a registered managed subtree."""
        real = os.path.realpath(str(path))
        for managed in self.managed_dirs():
            root = os.path.realpath(str(managed))
            if real == root or real.startswith(root + os.sep):
                return True
        return False

    def guard(self, path) -> None:
        """Reject a path that is managed, or an ancestor of a managed subtree.

        The ancestor half is what stops a category rename/delete from
        recursively moving or removing a managed tree it happens to contain.
        """
        real = os.path.realpath(str(path))
        for managed in self.managed_dirs():
            root = os.path.realpath(str(managed))
            if self._contains(root, real) or self._contains(real, root):
                raise ManagedPathError(
                    f"managed knowledge path is protected: {real}"
                )

    # ------------------------------------------------------------------
    # Effective documents
    # ------------------------------------------------------------------
    def _managed_realpaths(self) -> List[str]:
        return [os.path.realpath(str(p)) for p in self.managed_dirs()]

    def iter_effective_rel_paths(self) -> Iterator[str]:
        """Yield root-relative posix paths of the eligible knowledge documents."""
        managed = self._managed_realpaths()
        if self.root.is_dir():
            for md in sorted(self.root.rglob("*.md")):
                rel = md.relative_to(self.root)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                real = os.path.realpath(str(md))
                if any(real == root or real.startswith(root + os.sep)
                       for root in managed):
                    continue
                yield rel.as_posix()

        catalog = self.catalog
        if catalog is None:
            return
        active = catalog.active_task_ids()
        if not active:
            return
        converted = catalog.converted_dir
        if not converted.is_dir():
            return
        prefix = catalog.relative(converted)
        for md in sorted(converted.rglob("*.md")):
            rel = md.relative_to(converted)
            parts = rel.parts
            if any(part.startswith(".") for part in parts):
                continue
            # Layout: <source-id>/<version>/<task-id>/<...>.md. Only the task
            # the source currently points at is eligible; every historical
            # batch stays out of the set even while it is still on disk.
            if len(parts) < 3 or parts[2] not in active:
                continue
            yield (Path(prefix) / rel).as_posix()

    def is_effective_rel_path(self, rel_path) -> bool:
        """Query-time eligibility check for one already-known path."""
        if rel_path is None:
            return False
        rel = str(rel_path).replace("\\", "/").lstrip("/")
        parts = rel.split("/")
        if not parts or any(p in ("", ".", "..") for p in parts):
            return False
        if any(part.startswith(".") for part in parts):
            return False

        absolute = self.root.joinpath(*parts)
        if not self.is_managed(absolute):
            return rel.lower().endswith(".md")

        catalog = self.catalog
        if catalog is None:
            return False
        try:
            converted_rel = Path(*parts).relative_to(catalog.relative(catalog.converted_dir))
        except ValueError:
            return False
        if len(converted_rel.parts) < 3:
            return False
        task_id = converted_rel.parts[2]
        return task_id in catalog.active_task_ids()
