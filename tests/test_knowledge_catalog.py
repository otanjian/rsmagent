"""Tests for the traceable-ingestion catalog (change task 1.2)."""

import sqlite3
from pathlib import Path

import pytest

from agent.knowledge.catalog import (
    COMMIT_PENDING,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DISABLED,
    TASK_INTERRUPTED,
    TASK_QUEUED,
    TASK_RUNNING,
    CatalogError,
    KnowledgeCatalog,
)


def catalog(tmp_path: Path) -> KnowledgeCatalog:
    root = tmp_path / "knowledge"
    root.mkdir()
    return KnowledgeCatalog(str(root)).initialize()


def test_initialize_registers_identity_revision_and_dirs(tmp_path):
    cat = catalog(tmp_path)
    kb_id = cat.knowledge_id
    assert kb_id
    assert cat.revision == 0
    assert cat.control_dir == (tmp_path / "knowledge" / ".ingestion")
    assert cat.db_path.is_file()
    assert cat.originals_dir.name == "originals"
    assert cat.converted_dir.name == "converted"
    assert cat.staging_dir == cat.control_dir / "staging"


def test_initialize_is_idempotent_and_keeps_identity(tmp_path):
    cat = catalog(tmp_path)
    first_id = cat.knowledge_id
    cat.set_lifecycle(*_a_source(cat), LIFECYCLE_DISABLED)
    revision = cat.revision

    reopened = KnowledgeCatalog(str(tmp_path / "knowledge")).initialize()
    assert reopened.knowledge_id == first_id
    assert reopened.revision == revision
    assert reopened.originals_dir.name == "originals"


def test_existing_manual_directory_forces_conflict_free_name(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    # A human category that happens to use the reserved candidate name, with
    # real content that must never be adopted as managed storage.
    manual = root / "originals"
    manual.mkdir()
    (manual / "keep.md").write_text("# keep\n", encoding="utf-8")

    cat = KnowledgeCatalog(str(root)).initialize()
    assert cat.originals_dir.name == "originals-ingestion"
    assert (manual / "keep.md").read_text(encoding="utf-8") == "# keep\n"
    assert cat.originals_dir != manual

    # The chosen name is recorded and reused on the next open.
    reopened = KnowledgeCatalog(str(root)).initialize()
    assert reopened.originals_dir.name == "originals-ingestion"


def test_registered_name_is_reused_even_when_missing(tmp_path):
    cat = catalog(tmp_path)
    assert cat.originals_dir.name == "originals"
    cat.ensure_internal_dirs()
    # Remove the directory; the mapping is a decision, not a probe.
    (tmp_path / "knowledge" / "originals").rmdir()
    reopened = KnowledgeCatalog(str(tmp_path / "knowledge")).initialize()
    assert reopened.originals_dir.name == "originals"


def test_legacy_markdown_is_never_moved(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "index.md").write_text("# Knowledge Index\n", encoding="utf-8")
    notes = root / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n", encoding="utf-8")

    KnowledgeCatalog(str(root)).initialize()

    assert (root / "index.md").read_text(encoding="utf-8") == "# Knowledge Index\n"
    assert (notes / "a.md").read_text(encoding="utf-8") == "# A\n"


def test_versions_are_immutable_and_allocated_in_order(tmp_path):
    cat = catalog(tmp_path)
    source = cat.create_source("contract.pdf", category="legal", created_by="u1")

    v1 = cat.add_version(source["source_id"], original_name="contract.pdf",
                         original_path="originals/src/1/contract.pdf", size=10,
                         content_hash="hash-1", request_key="req-1")
    assert v1["version"] == 1
    v2 = cat.add_version(source["source_id"], original_name="contract.pdf",
                         original_path="originals/src/2/contract.pdf", size=20,
                         content_hash="hash-2", request_key="req-2")
    assert v2["version"] == 2

    assert cat.get_source(source["source_id"])["latest_version"] == 2
    assert [v["version"] for v in cat.list_versions(source["source_id"])] == [1, 2]
    assert cat.find_version_by_request_key(source["source_id"], "req-1")["version"] == 1
    assert cat.find_version_by_hash(source["source_id"], "hash-2")["version"] == 2
    assert cat.find_version_by_hash(source["source_id"], "missing") is None


def test_stale_version_is_rejected(tmp_path):
    cat = catalog(tmp_path)
    source = cat.create_source("a.txt")
    cat.add_version(source["source_id"], original_name="a.txt",
                    original_path="originals/a/1/a.txt", size=1,
                    content_hash="h1")
    with pytest.raises(CatalogError):
        cat.add_version(source["source_id"], original_name="a.txt",
                        original_path="originals/a/1/a.txt", size=1,
                        content_hash="h2", version=1)


def test_revision_advances_on_eligibility_changes(tmp_path):
    cat = catalog(tmp_path)
    source = cat.create_source("a.txt")
    assert cat.revision == 0

    cat.set_lifecycle(source["source_id"], LIFECYCLE_DISABLED)
    assert cat.revision == 1

    task = cat.create_task(source["source_id"], "convert", target_version=1)
    cat.set_active_task(source["source_id"], task["task_id"], version=1)
    assert cat.revision == 2
    assert cat.active_task_ids() == set()

    cat.set_lifecycle(source["source_id"], LIFECYCLE_ACTIVE)
    assert cat.active_task_ids() == {task["task_id"]}
    assert cat.active_source_ids() == {source["source_id"]}


def test_task_lifecycle_and_busy_detection(tmp_path):
    cat = catalog(tmp_path)
    source = cat.create_source("a.txt")
    task = cat.create_task(source["source_id"], "convert", target_version=1)
    assert task["status"] == TASK_QUEUED
    assert cat.has_blocking_tasks() is True

    cat.update_task(task["task_id"], status=TASK_RUNNING, stage="converting")
    assert cat.has_blocking_tasks() is True
    assert cat.get_task(task["task_id"])["stage"] == "converting"

    cat.update_task(task["task_id"], status="complete", manifest={"docs": ["a.md"]})
    assert cat.has_blocking_tasks() is False
    assert cat.get_task(task["task_id"])["manifest"] == {"docs": ["a.md"]}
    assert [t["task_id"] for t in cat.list_tasks(source_id=source["source_id"])] == [task["task_id"]]


def test_pending_version_blocks_until_committed(tmp_path):
    cat = catalog(tmp_path)
    source = cat.create_source("a.txt")
    version = cat.add_version(source["source_id"], original_name="a.txt",
                              original_path="originals/a/1/a.txt", size=1,
                              content_hash="h1", commit_state=COMMIT_PENDING)
    assert cat.has_blocking_tasks() is True

    cat.set_version_commit_state(version["version_id"], "committed")
    assert cat.has_blocking_tasks() is False


def test_mark_interrupted_reclassifies_running_only(tmp_path):
    cat = catalog(tmp_path)
    source = cat.create_source("a.txt")
    running = cat.create_task(source["source_id"], "convert", target_version=1,
                              status=TASK_RUNNING)
    queued = cat.create_task(source["source_id"], "convert", target_version=1)

    assert cat.mark_interrupted_tasks() == 1
    assert cat.get_task(running["task_id"])["status"] == TASK_INTERRUPTED
    assert cat.get_task(running["task_id"])["stage"] is None
    assert cat.get_task(queued["task_id"])["status"] == TASK_QUEUED


def _a_source(cat: KnowledgeCatalog):
    return (cat.create_source("a.txt")["source_id"],)
