"""Minimal per-root serial executor (phase B: the cleanup branch).

Change ``add-traceable-knowledge-ingestion`` (design D5). Phase C adds the
conversion branch and the loop that discovers roots; phase B only has to remove
the files of an already-effective deletion through the same ``tasks`` table, so a
single root's queued cleanup runs in the request that created it (under the usage
lock) and can be retried later from the source detail.

Two properties matter here:

- an interrupted cleanup becomes retryable **and keeps the root busy**, because
  "interrupted" is not busy but the deleted source's files are still on disk;
- a failed cleanup stays ``queued`` rather than ``failed`` for the same reason:
  the root must not look idle (and therefore movable) while files remain.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from common.log import logger
from agent.knowledge.catalog import (
    LIFECYCLE_DELETED,
    TASK_COMPLETE,
    TASK_INTERRUPTED,
    TASK_QUEUED,
    TASK_RUNNING,
    KnowledgeCatalog,
)


def process_pending_tasks(knowledge_root) -> dict:
    """Run every queued cleanup task for one root.

    The caller holds the root's shared usage lock; this function never takes it,
    so it cannot deadlock against the request that enqueued the task.
    """
    catalog = KnowledgeCatalog.open_if_exists(str(knowledge_root))
    if catalog is None:
        return {"processed": 0, "failed": 0, "skipped": "no_catalog"}

    # A cleanup killed by a process exit is still persistent work: reclassify
    # orphaned "running" tasks first, then requeue the cleanup ones so the root
    # keeps reporting busy instead of looking idle with files still on disk.
    catalog.mark_interrupted_tasks(task_type="cleanup")
    for task in catalog.list_tasks(statuses=(TASK_INTERRUPTED,)):
        if task["task_type"] == "cleanup":
            catalog.update_task(task["task_id"], status=TASK_QUEUED, stage=None)

    processed = failed = 0
    for task in catalog.list_tasks(statuses=(TASK_QUEUED,)):
        if task["task_type"] != "cleanup":
            continue  # conversion tasks arrive with the converter (phase C)
        if _run_cleanup(catalog, task):
            processed += 1
        else:
            failed += 1
    return {"processed": processed, "failed": failed}


def _run_cleanup(catalog: KnowledgeCatalog, task: dict) -> bool:
    source = catalog.get_source(task["source_id"])
    catalog.update_task(task["task_id"], status=TASK_RUNNING, stage="cleanup")
    try:
        if source is not None and source["lifecycle"] != LIFECYCLE_DELETED:
            # Defensive: a source re-enabled after the task was queued must not
            # have its files removed by a stale cleanup.
            catalog.update_task(task["task_id"], status=TASK_COMPLETE,
                                stage="skipped", error=None)
            return True
        removed = _remove_source_files(catalog, task["source_id"])
        catalog.update_task(task["task_id"], status=TASK_COMPLETE,
                            stage="cleaned", error=None,
                            manifest={"removed": removed})
        return True
    except Exception as exc:
        catalog.update_task(task["task_id"], status=TASK_QUEUED,
                            stage="cleanup_failed", error=str(exc))
        logger.error(f"[KnowledgeRunner] cleanup failed for {task['source_id']}: {exc}")
        return False


def _remove_source_files(catalog: KnowledgeCatalog, source_id: str) -> list:
    """Remove a deleted source's originals and converted batches.

    Scoped to ``<managed-dir>/<source-id>`` only: the catalog's own directory
    mapping is the authority, so a manual category that happens to share the
    id's name is never touched.
    """
    removed = []
    for base in (catalog.originals_dir, catalog.converted_dir):
        target = Path(base) / str(source_id)
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            else:
                continue
        except Exception:
            raise
        removed.append(catalog.relative(target))
    return removed
