"""Cross-process root locks, busy contract and mode-switch marker (task 1.4)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent import team
from agent.admin import AgentAdminService
from agent.knowledge.catalog import KnowledgeCatalog
from agent.knowledge.locks import (
    KnowledgeBusyError,
    KnowledgeRootLock,
    KnowledgeUnavailableError,
    clear_marker,
    knowledge_root_is_busy,
    marker_path,
    read_marker,
    write_marker,
)
from agent.knowledge.service import KnowledgeService
from agent.registry import AgentRegistry, set_agent_registry


def test_usage_and_exclusive_locks_exclude_across_processes(tmp_path, monkeypatch):
    lock_dir = tmp_path / "locks"
    monkeypatch.setenv("COW_KNOWLEDGE_LOCK_DIR", str(lock_dir))
    root = tmp_path / "knowledge"
    root.mkdir()

    repo = str(Path(__file__).resolve().parents[1])
    script = (
        "import sys, time\n"
        f"sys.path.insert(0, r'{repo}')\n"
        "from agent.knowledge.locks import KnowledgeRootLock\n"
        f"lock = KnowledgeRootLock(r'{root}')\n"
        "ctx = lock.usage(timeout=5)\n"
        "assert ctx.__enter__() is True\n"
        "print('locked', flush=True)\n"
        "time.sleep(5)\n"
        "ctx.__exit__(None, None, None)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        # A mode switch cannot take the exclusive lock while the shared usage
        # lock is held by another process.
        with KnowledgeRootLock(root).exclusive(timeout=0.0) as acquired:
            assert acquired is False
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    with KnowledgeRootLock(root).exclusive(timeout=1.0) as acquired:
        assert acquired is True


def test_busy_detection_reads_persisted_work(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    assert knowledge_root_is_busy(root) is False

    catalog = KnowledgeCatalog(str(root)).initialize()
    source = catalog.create_source("a.txt")
    assert knowledge_root_is_busy(root) is False

    task = catalog.create_task(source["source_id"], "convert", target_version=1)
    assert knowledge_root_is_busy(root) is True
    catalog.update_task(task["task_id"], status="complete")
    assert knowledge_root_is_busy(root) is False


def test_marker_round_trip(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert read_marker(workspace) is None

    write_marker(workspace, {"from_mode": "own", "to_mode": "shared"})
    marker = read_marker(workspace)
    assert marker["to_mode"] == "shared"
    assert marker["created_at"]

    clear_marker(workspace)
    assert read_marker(workspace) is None
    assert not marker_path(workspace).exists()


def test_service_refuses_while_switch_recovery_is_pending(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("# a\n", encoding="utf-8")
    write_marker(tmp_path, {"from_mode": "own", "to_mode": "shared"})

    svc = KnowledgeService(str(tmp_path))
    with pytest.raises(KnowledgeUnavailableError):
        svc.list_tree()
    with pytest.raises(KnowledgeUnavailableError):
        svc.dispatch("create_document", {"path": "notes/b.md", "content": "# b"})
    assert not (root / "notes" / "b.md").exists()

    clear_marker(tmp_path)
    assert svc.list_tree()["stats"]["pages"] == 1


def _admin(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    research = tmp_path / "research"
    research.mkdir()
    settings = {
        "agent_workspace": str(tmp_path),
        "default_agent_id": "primary",
        "agents": [
            {"id": "primary", "name": "Primary", "workspace": str(primary),
             "enabled": True},
            {"id": "research", "name": "Research", "workspace": str(research),
             "enabled": True},
        ],
        "channel_instances": [],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(settings), encoding="utf-8")
    set_agent_registry(AgentRegistry.from_config(team.resolve(settings)))
    return AgentAdminService(str(config_path)), research


def test_mode_switch_is_busy_while_tasks_are_queued(tmp_path):
    service, research = _admin(tmp_path)
    try:
        kdir = research / "knowledge"
        kdir.mkdir()
        (kdir / "note.md").write_text("keep me\n", encoding="utf-8")
        catalog = KnowledgeCatalog(str(kdir)).initialize()
        source = catalog.create_source("a.txt")
        catalog.create_task(source["source_id"], "convert", target_version=1)

        with pytest.raises(KnowledgeBusyError):
            service.set_knowledge_mode("research", "shared")

        # A refusal changes nothing: no symlink, no stash, file intact.
        assert kdir.is_dir() and not kdir.is_symlink()
        assert (kdir / "note.md").read_text(encoding="utf-8") == "keep me\n"
        assert not (research / "knowledge.own").exists()
        assert read_marker(research) is None
    finally:
        set_agent_registry(None)


def test_successful_switch_clears_marker_and_preserves_own_base(tmp_path):
    service, research = _admin(tmp_path)
    try:
        kdir = research / "knowledge"
        kdir.mkdir()
        (kdir / "note.md").write_text("keep me\n", encoding="utf-8")

        result = service.set_knowledge_mode("research", "shared")
        assert result["mode"] == "shared"
        assert kdir.is_symlink()
        assert read_marker(research) is None
        stash = research / "knowledge.own"
        assert (stash / "note.md").read_text(encoding="utf-8") == "keep me\n"
    finally:
        set_agent_registry(None)


def test_pending_marker_is_replayed_before_a_new_switch(tmp_path):
    service, research = _admin(tmp_path)
    try:
        kdir = research / "knowledge"
        kdir.mkdir()
        (kdir / "note.md").write_text("keep me\n", encoding="utf-8")
        # Simulate an interruption after the marker was written but before the
        # move happened.
        write_marker(research, {"from_mode": "own", "to_mode": "shared"})

        result = service.set_knowledge_mode("research", "shared")

        assert result["mode"] == "shared"
        assert kdir.is_symlink()
        assert read_marker(research) is None
        assert (research / "knowledge.own" / "note.md").read_text(
            encoding="utf-8") == "keep me\n"
    finally:
        set_agent_registry(None)
