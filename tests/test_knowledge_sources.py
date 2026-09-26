"""Original source assets: staging, dedup, versions and lifecycle (phase B).

Task 2.1 - 2.4 acceptance: idempotent per-file upload, limits, dedup, explicit
same-name handling, immutable versions, quota, safe download, disable/delete and
the cleanup branch of the serial executor.
"""

import threading
from pathlib import Path

import pytest

from config import conf
from agent.knowledge.catalog import (
    LIFECYCLE_DELETED,
    LIFECYCLE_DISABLED,
    TASK_COMPLETE,
    TASK_QUEUED,
    TASK_RUNNING,
    KnowledgeCatalog,
)
from agent.knowledge.locks import KnowledgeUnavailableError, write_marker
from agent.knowledge.runner import process_pending_tasks
from agent.knowledge.scope import KnowledgeScope
from agent.knowledge.sources import (
    SourceAssetService,
    SourceNotFoundError,
    SourceQuotaError,
    SourceUnavailableError,
    SourceValidationError,
)


@pytest.fixture
def workspace(tmp_path: Path):
    (tmp_path / "knowledge").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def upload_enabled(monkeypatch):
    monkeypatch.setitem(conf(), "knowledge_source_upload_enabled", True)
    monkeypatch.setitem(conf(), "knowledge_source_max_files", 100)
    monkeypatch.setitem(conf(), "knowledge_source_max_file_size", 10 * 1024 * 1024)
    monkeypatch.setitem(conf(), "knowledge_source_max_batch_size", 200 * 1024 * 1024)
    monkeypatch.setitem(conf(), "knowledge_source_storage_quota", 0)


def svc(workspace: Path, **kwargs) -> SourceAssetService:
    return SourceAssetService(str(workspace), **kwargs)


def _upload(service, name, content, **kwargs):
    return service.save_files([{"filename": name, "content": content}], **kwargs)


def test_saves_any_format_and_reports_saved_not_searchable(workspace):
    service = svc(workspace)
    payload = b"\x00\x01DWG-binary\xff"

    result = _upload(service, "plan.dwg", payload, category="drawings")
    assert result["saved"] == 1 and result["failed"] == 0
    receipt = result["results"][0]
    assert receipt["status"] == "saved"
    assert receipt["source"]["searchable"] is False
    assert receipt["source"]["converted"] is False

    source_id = receipt["source_id"]
    assert receipt["latest_version"] == 1
    assert receipt["source"]["latest_version"] == 1
    assert receipt["source"]["size"] == len(payload)
    download = service.download(source_id)
    assert Path(download["path"]).read_bytes() == payload
    assert download["filename"] == "plan.dwg"
    assert download["preview"] is False
    # Any content type is served as a download; active content must not render.
    assert download["content_type"]

    detail = service.get_detail(source_id)
    assert detail["source"]["latest_version"] == 1
    assert detail["versions"][0]["commit_state"] == "committed"
    # No converter ran, so the original never enters the effective document set.
    assert sorted(KnowledgeScope(str(workspace / "knowledge")).iter_effective_rel_paths()) == []


def test_batch_limits_refuse_without_residue(workspace, monkeypatch):
    service = svc(workspace)
    monkeypatch.setitem(conf(), "knowledge_source_max_files", 1)
    with pytest.raises(SourceQuotaError):
        service.save_files([
            {"filename": "a.txt", "content": b"a"},
            {"filename": "b.txt", "content": b"b"},
        ])

    monkeypatch.setitem(conf(), "knowledge_source_max_files", 100)
    monkeypatch.setitem(conf(), "knowledge_source_max_file_size", 4)
    result = service.save_files([
        {"filename": "ok.txt", "content": b"fine"},
        {"filename": "big.bin", "content": b"way too large"},
    ])
    assert result["saved"] == 1 and result["failed"] == 1
    assert result["results"][1]["code"] == "source_quota_exceeded"
    # Only the accepted file exists, and nothing is left pending.
    assert [s["name"] for s in service.list_sources()["sources"]] == ["ok.txt"]
    catalog = KnowledgeCatalog.open_if_exists(str(workspace / "knowledge"))
    assert catalog.has_blocking_tasks() is False
    stored = [p.name for p in catalog.originals_dir.rglob("*") if p.is_file()]
    assert stored == ["ok.txt"]


def test_retry_with_same_request_key_is_idempotent(workspace):
    service = svc(workspace)
    first = _upload(service, "contract.pdf", b"%PDF-1.4 one", request_id="req-1")
    second = _upload(service, "contract.pdf", b"%PDF-1.4 one", request_id="req-1")

    assert first["results"][0]["source_id"] == second["results"][0]["source_id"]
    assert first["results"][0]["version"] == second["results"][0]["version"] == 1
    assert second["results"][0]["status"] == "reused"
    assert len(service.list_sources()["sources"]) == 1

    conflict = _upload(service, "contract.pdf", b"%PDF-1.4 other", request_id="req-1")
    item = conflict["results"][0]
    assert item["status"] == "conflict" and item["reason"] == "request_key_conflict"


def test_identical_content_is_deduplicated_across_requests(workspace):
    service = svc(workspace)
    first = _upload(service, "a.txt", b"same bytes", request_id="r1")
    second = _upload(service, "b.txt", b"same bytes", request_id="r2")

    assert second["results"][0]["status"] == "reused"
    assert second["results"][0]["reason"] == "duplicate_content"
    assert second["results"][0]["source_id"] == first["results"][0]["source_id"]
    assert len(service.list_sources()["sources"]) == 1


def test_same_name_requires_an_explicit_choice(workspace):
    service = svc(workspace)
    _upload(service, "report.md", b"v1", request_id="r1")

    asked = _upload(service, "report.md", b"v2-different", request_id="r2")
    item = asked["results"][0]
    assert item["status"] == "conflict" and item["reason"] == "name_conflict"
    assert item["existing"]["latest_version"] == 1

    created = _upload(service, "report.md", b"v2-different", request_id="r3",
                      conflict="new")
    assert created["results"][0]["status"] == "saved"
    assert len(service.list_sources()["sources"]) == 2

    updated = _upload(service, "report.md", b"v3", request_id="r4",
                      conflict="update", expected_version=1)
    assert updated["results"][0]["status"] == "saved"
    assert updated["results"][0]["version"] == 2
    assert updated["results"][0]["source_id"] == item["existing"]["source_id"]


def test_versions_are_immutable_and_stale_updates_conflict(workspace):
    service = svc(workspace)
    first = _upload(service, "a.txt", b"one", request_id="r1")
    source_id = first["results"][0]["source_id"]

    stale = _upload(service, "a.txt", b"two", request_id="r2",
                    conflict="update", target_source_id=source_id,
                    expected_version=99)
    assert stale["results"][0]["status"] == "conflict"
    assert stale["results"][0]["reason"] == "stale_version"

    updated = _upload(service, "a.txt", b"two", request_id="r3",
                      conflict="update", target_source_id=source_id,
                      expected_version=1)
    assert updated["results"][0]["version"] == 2

    versions = service.get_detail(source_id)["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["content_hash"] != versions[1]["content_hash"]


def test_storage_quota_reserves_and_releases(workspace, monkeypatch):
    service = svc(workspace)
    monkeypatch.setitem(conf(), "knowledge_source_storage_quota", 10)

    ok = _upload(service, "a.txt", b"12345678", request_id="r1")
    assert ok["saved"] == 1
    assert ok["usage"]["remaining"] == 2

    refused = _upload(service, "b.txt", b"123456", request_id="r2")
    assert refused["results"][0]["status"] == "failed"
    assert refused["results"][0]["code"] == "source_quota_exceeded"
    assert len(service.list_sources()["sources"]) == 1

    # Deleting the first source releases its allowance immediately, before any
    # physical cleanup, so the refused upload now fits.
    service.set_lifecycle(ok["results"][0]["source_id"], "delete")
    accepted = _upload(service, "b.txt", b"123456", request_id="r3")
    assert accepted["saved"] == 1


def test_disable_and_delete_change_the_effective_set(workspace):
    service = svc(workspace)
    catalog = KnowledgeCatalog(str(workspace / "knowledge")).initialize()
    catalog.ensure_internal_dirs()
    receipt = _upload(service, "a.txt", b"one", request_id="r1")["results"][0]
    source_id = receipt["source_id"]

    # Give the source a converted batch so "searchable" has something to switch.
    task = catalog.create_task(source_id, "convert", target_version=1)
    batch = catalog.converted_dir / source_id / "1" / task["task_id"]
    batch.mkdir(parents=True)
    (batch / "content.md").write_text("# converted\n", encoding="utf-8")
    catalog.set_active_task(source_id, task["task_id"], version=1)

    scope = KnowledgeScope(str(workspace / "knowledge"))
    assert scope.iter_effective_rel_paths()
    assert service.get_detail(source_id)["source"]["searchable"] is True

    disabled = service.set_lifecycle(source_id, "disable")
    assert disabled["source"]["lifecycle"] == LIFECYCLE_DISABLED
    assert list(scope.iter_effective_rel_paths()) == []

    service.set_lifecycle(source_id, "enable")
    assert service.get_detail(source_id)["source"]["searchable"] is True

    deleted = service.set_lifecycle(source_id, "delete")
    assert deleted["source"]["lifecycle"] == LIFECYCLE_DELETED
    assert deleted["cleanup"]["processed"] == 1
    assert deleted["cleanup_pending"] is False
    assert list(scope.iter_effective_rel_paths()) == []
    # The cleanup actually removed both managed trees for the source.
    assert not (catalog.originals_dir / source_id).exists()
    assert not (catalog.converted_dir / source_id).exists()
    assert catalog.has_blocking_tasks() is False
    with pytest.raises(SourceNotFoundError):
        service.download(source_id)


def test_failed_cleanup_stays_busy_and_is_retryable(workspace, monkeypatch):
    service = svc(workspace)
    catalog = KnowledgeCatalog(str(workspace / "knowledge")).initialize()
    receipt = _upload(service, "a.txt", b"one", request_id="r1")["results"][0]
    source_id = receipt["source_id"]

    from agent.knowledge import runner

    def _boom(cat, sid):
        raise OSError("disk busy")

    monkeypatch.setattr(runner, "_remove_source_files", _boom)
    result = service.set_lifecycle(source_id, "delete")
    assert result["cleanup_pending"] is True
    assert result["cleanup"]["failed"] == 1
    tasks = [t for t in result["tasks"] if t["task_type"] == "cleanup"]
    assert tasks[0]["status"] == TASK_QUEUED and tasks[0]["stage"] == "cleanup_failed"
    # Files remain, so the root must still report busy (mode switch blocked).
    assert catalog.has_blocking_tasks() is True

    monkeypatch.undo()
    monkeypatch.setitem(conf(), "knowledge_source_upload_enabled", True)
    retried = service.retry_task(tasks[0]["task_id"])
    assert retried["task"]["status"] == TASK_COMPLETE
    assert not (catalog.originals_dir / source_id).exists()
    assert catalog.has_blocking_tasks() is False


def test_interrupted_cleanup_is_requeued_not_forgotten(workspace):
    service = svc(workspace)
    catalog = KnowledgeCatalog(str(workspace / "knowledge")).initialize()
    receipt = _upload(service, "a.txt", b"one", request_id="r1")["results"][0]
    source_id = receipt["source_id"]
    # Simulate a process that died mid-delete: lifecycle is effective, the task
    # was left running.
    catalog.set_lifecycle(source_id, LIFECYCLE_DELETED)
    task = catalog.create_task(source_id, "cleanup", target_version=1,
                               status=TASK_RUNNING)
    assert catalog.has_blocking_tasks() is True

    summary = process_pending_tasks(workspace / "knowledge")
    assert summary["processed"] == 1
    assert catalog.get_task(task["task_id"])["status"] == TASK_COMPLETE
    assert not (catalog.originals_dir / source_id).exists()


def test_a_failed_write_leaves_no_source_or_pending_record(workspace, monkeypatch):
    service = svc(workspace)
    catalog = KnowledgeCatalog(str(workspace / "knowledge")).initialize()
    catalog.ensure_internal_dirs()

    from agent.knowledge import sources as sources_module

    def _boom(self, catalog_, version, content, digest):
        raise OSError("cannot write")

    monkeypatch.setattr(sources_module.SourceAssetService, "_publish_blob", _boom)
    result = _upload(service, "a.txt", b"one", request_id="r1")
    assert result["failed"] == 1
    assert service.list_sources()["sources"] == []
    assert catalog.has_blocking_tasks() is False
    assert not list(catalog.originals_dir.rglob("*.txt"))


def test_upload_is_refused_when_the_switch_is_off(workspace, monkeypatch):
    monkeypatch.setitem(conf(), "knowledge_source_upload_enabled", False)
    service = svc(workspace)
    with pytest.raises(SourceUnavailableError):
        _upload(service, "a.txt", b"one")
    # Reads stay available and create no catalog on a legacy root.
    assert service.list_sources() == {
        "sources": [], "registered": False,
        "limits": service.list_sources()["limits"],
    }


def test_no_write_while_a_mode_switch_awaits_recovery(workspace):
    write_marker(str(workspace), {"step": "moving", "target": "own"})
    service = svc(workspace)
    with pytest.raises(KnowledgeUnavailableError):
        _upload(service, "a.txt", b"one")


def test_concurrent_identical_uploads_converge_on_one_source(workspace):
    service = svc(workspace)
    results = []
    barrier = threading.Barrier(2)

    def _run(request_id):
        barrier.wait()
        results.append(_upload(service, "same.txt", b"identical", request_id=request_id))

    threads = [threading.Thread(target=_run, args=(rid,)) for rid in ("r1", "r2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Both requests must succeed and converge on the same source: the retry
    # window is exactly where a duplicate would otherwise appear.
    assert len(results) == 2
    assert len(service.list_sources()["sources"]) == 1
    source_ids = {r["results"][0]["source_id"] for r in results}
    assert len(source_ids) == 1


def test_concurrent_updates_based_on_the_same_version_conflict(workspace):
    service = svc(workspace)
    first = _upload(service, "a.txt", b"one", request_id="r0")["results"][0]
    source_id = first["source_id"]

    outcomes = []
    barrier = threading.Barrier(2)

    def _run(request_id, content):
        barrier.wait()
        result = _upload(service, "a.txt", content, request_id=request_id,
                         conflict="update", target_source_id=source_id,
                         expected_version=1)
        outcomes.append(result["results"][0])

    threads = [
        threading.Thread(target=_run, args=("r1", b"two")),
        threading.Thread(target=_run, args=("r2", b"three")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    saved = [o for o in outcomes if o["status"] == "saved"]
    stale = [o for o in outcomes if o.get("reason") == "stale_version"]
    assert len(saved) == 1 and len(stale) == 1
    assert saved[0]["version"] == 2
    # No version number was reused and the first original is untouched.
    versions = service.get_detail(source_id)["versions"]
    assert [v["version"] for v in versions] == [1, 2]


def test_upload_holds_the_usage_lock_during_publication(workspace):
    service = svc(workspace)
    from agent.knowledge import sources as sources_module
    from agent.knowledge.locks import KnowledgeRootLock

    acquired = {}

    def _probe(self, catalog, version, content, digest):
        # Inside the write, a mode switch's exclusive lock must not be available.
        with KnowledgeRootLock(workspace / "knowledge").exclusive(timeout=0.0) as ok:
            acquired["exclusive"] = ok
        raise OSError("stop before writing")

    original = sources_module.SourceAssetService._publish_blob
    sources_module.SourceAssetService._publish_blob = _probe
    try:
        _upload(service, "a.txt", b"one", request_id="r1")
    finally:
        sources_module.SourceAssetService._publish_blob = original

    assert acquired["exclusive"] is False


def test_download_is_refused_while_a_switch_awaits_recovery(workspace):
    service = svc(workspace)
    receipt = _upload(service, "a.txt", b"one", request_id="r1")["results"][0]
    write_marker(str(workspace), {"step": "moving"})
    with pytest.raises(KnowledgeUnavailableError):
        service.download(receipt["source_id"])


def test_list_projects_the_newest_conversion_task_for_the_row(workspace):
    """The console's per-row state needs the task outcome, not just an id.

    ``target_task_id`` alone cannot tell "still converting" from "conversion
    failed", so both would read as "processing" forever.
    """
    service = svc(workspace)
    receipt = _upload(service, "a.pdf", b"%PDF-1.4 one", request_id="r1")["results"][0]
    source_id = receipt["source_id"]

    listed = service.list_sources()["sources"][0]
    assert listed["latest_task"] is None, "no conversion has been requested yet"

    catalog = KnowledgeCatalog.open_if_exists(str(workspace / "knowledge"))
    task = catalog.create_task(source_id, "convert", target_version=1)
    catalog.update_task(task["task_id"], status="failed", stage="extract",
                        error="encrypted")

    listed = service.list_sources()["sources"][0]
    assert listed["latest_task"]["task_id"] == task["task_id"]
    assert listed["latest_task"]["status"] == "failed"
    assert listed["latest_task"]["error"] == "encrypted"

    detail = service.get_detail(source_id)
    assert detail["source"]["latest_task"]["status"] == "failed"
    assert any(t["task_type"] == "cleanup" for t in detail["tasks"]) is False


def test_a_cleanup_task_is_not_reported_as_the_conversion_state(workspace):
    service = svc(workspace)
    receipt = _upload(service, "a.pdf", b"%PDF-1.4 one", request_id="r1")["results"][0]
    source_id = receipt["source_id"]
    catalog = KnowledgeCatalog.open_if_exists(str(workspace / "knowledge"))
    task = catalog.create_task(source_id, "convert", target_version=1)
    catalog.update_task(task["task_id"], status=TASK_COMPLETE, stage="indexed")
    service.set_lifecycle(source_id, "delete")

    # The deleted source is gone from the listing, and its detail is not readable
    # at all -- neither path can confuse the cleanup task for the conversion.
    assert service.list_sources()["sources"] == []
    with pytest.raises(SourceNotFoundError):
        service.get_detail(source_id)


def test_conversion_cannot_be_requested_without_an_executor(workspace):
    """Phase C owns the converter; until then the request must refuse.

    Queuing a convert task the runner cannot pick up would leave the root
    reporting busy, which blocks every mode switch on that root.
    """
    service = svc(workspace)
    receipt = _upload(service, "a.pdf", b"%PDF-1.4 one", request_id="r1")["results"][0]
    source_id = receipt["source_id"]

    with pytest.raises(SourceUnavailableError):
        service.request_conversion(source_id)

    catalog = KnowledgeCatalog.open_if_exists(str(workspace / "knowledge"))
    assert catalog.list_tasks(source_id=source_id) == [], "nothing was queued"


def test_conversion_request_on_a_missing_source_is_not_found(workspace):
    with pytest.raises(SourceNotFoundError):
        svc(workspace).request_conversion("src_missing")
