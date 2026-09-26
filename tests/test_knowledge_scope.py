"""Effective-document enumeration and managed-path protection (task 1.3)."""

from pathlib import Path

import pytest

from agent.knowledge.catalog import LIFECYCLE_DISABLED, KnowledgeCatalog
from agent.knowledge.scope import KnowledgeScope, ManagedPathError
from agent.knowledge.service import KnowledgeService


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()
    return root


def _registered_root(tmp_path: Path):
    root = _root(tmp_path)
    catalog = KnowledgeCatalog(str(root)).initialize()
    catalog.ensure_internal_dirs()
    return root, catalog


def test_legacy_root_has_no_managed_dirs_and_full_effective_set(tmp_path):
    root = _root(tmp_path)
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    (root / "index.md").write_text("# Knowledge Index\n", encoding="utf-8")
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "secret.md").write_text("# secret\n", encoding="utf-8")

    scope = KnowledgeScope(str(root))
    assert scope.managed_dirs() == []
    assert scope.is_managed(root / "notes" / "a.md") is False
    scope.guard(root / "notes" / "a.md")
    assert sorted(scope.iter_effective_rel_paths()) == ["index.md", "notes/a.md"]


def test_managed_dirs_excluded_from_effective_set(tmp_path):
    root, catalog = _registered_root(tmp_path)
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    original = catalog.originals_dir / "src_x" / "1" / "blob.pdf"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"%PDF")
    (catalog.originals_dir / "leak.md").write_text("# leak\n", encoding="utf-8")

    scope = KnowledgeScope(str(root))
    assert scope.is_managed(original) is True
    assert sorted(scope.iter_effective_rel_paths()) == ["notes/a.md"]
    with pytest.raises(ManagedPathError):
        scope.guard(catalog.originals_dir / "leak.md")


def test_only_active_conversion_batch_is_effective(tmp_path):
    root, catalog = _registered_root(tmp_path)
    source = catalog.create_source("report.xlsx")
    active = catalog.create_task(source["source_id"], "convert", target_version=1)
    stale = catalog.create_task(source["source_id"], "convert", target_version=1)
    catalog.set_active_task(source["source_id"], active["task_id"], version=1)

    converted = catalog.converted_dir
    for task in (active["task_id"], stale["task_id"]):
        folder = converted / source["source_id"] / "1" / task
        folder.mkdir(parents=True)
        (folder / "content.md").write_text(f"# {task}\n", encoding="utf-8")

    scope = KnowledgeScope(str(root))
    effective = sorted(scope.iter_effective_rel_paths())
    assert len(effective) == 1
    assert active["task_id"] in effective[0]
    assert stale["task_id"] not in effective[0]
    assert scope.is_effective_rel_path(effective[0]) is True
    assert scope.is_effective_rel_path(
        f"{catalog.relative(converted)}/{source['source_id']}/1/{stale['task_id']}/content.md"
    ) is False

    # Disabling the source removes the converted body immediately, before any
    # cache is physically cleaned.
    catalog.set_lifecycle(source["source_id"], LIFECYCLE_DISABLED)
    assert scope.is_effective_rel_path(effective[0]) is False
    assert sorted(KnowledgeScope(str(root)).iter_effective_rel_paths()) == []


def test_service_blocks_writes_inside_managed_subtrees(tmp_path):
    root, catalog = _registered_root(tmp_path)
    (root / "notes").mkdir()
    svc = KnowledgeService(str(tmp_path))

    ok = svc.dispatch("create_document", {"path": "notes/ok.md", "content": "# ok"})
    assert ok["code"] == 200

    for path in (
        f"{catalog.originals_dir.name}/evil.md",
        f"{catalog.converted_dir.name}/evil.md",
        f".ingestion/evil.md",
    ):
        result = svc.dispatch("create_document", {"path": path, "content": "# x"})
        assert result["code"] == 409, path
        assert result["message"] == "managed_knowledge_path"

    assert not (catalog.originals_dir / "evil.md").exists()
    assert not (catalog.converted_dir / "evil.md").exists()


def test_service_blocks_ancestor_category_delete_and_rename(tmp_path):
    root, catalog = _registered_root(tmp_path)
    # A managed subtree is a root-level directory, so a category rename/delete
    # that targets it (or a path that resolves to it) must be refused.
    svc = KnowledgeService(str(tmp_path))
    assert svc.dispatch("delete_category", {"path": catalog.originals_dir.name,
                                            "confirm": True})["code"] == 409
    assert svc.dispatch("rename_category", {"path": catalog.originals_dir.name,
                                            "new_path": "moved"})["code"] == 409
    assert catalog.originals_dir.is_dir()


def test_symlink_alias_to_managed_subtree_is_refused(tmp_path):
    root, catalog = _registered_root(tmp_path)
    alias = root / "alias"
    try:
        alias.symlink_to(catalog.originals_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    svc = KnowledgeService(str(tmp_path))
    result = svc.dispatch("create_document", {"path": "alias/evil.md",
                                              "content": "# x"})
    assert result["code"] == 409
    read = svc.dispatch("read", {"path": "alias/evil.md"})
    assert read["code"] == 409
    assert not (catalog.originals_dir / "evil.md").exists()


def test_move_both_ends_are_protected(tmp_path):
    root, catalog = _registered_root(tmp_path)
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("# a\n", encoding="utf-8")
    normal = root / "normal"
    normal.mkdir()

    svc = KnowledgeService(str(tmp_path))
    # Target is a managed directory.
    into_managed = svc.dispatch("move_documents", {
        "paths": ["notes/a.md"],
        "target_category": catalog.originals_dir.name,
    })
    assert into_managed["code"] == 409
    assert (root / "notes" / "a.md").is_file()

    # Source is a managed file (placed directly on disk, as a converter would).
    managed_file = catalog.originals_dir / "src_x" / "1" / "blob.md"
    managed_file.parent.mkdir(parents=True)
    managed_file.write_text("# original\n", encoding="utf-8")
    out_of_managed = svc.dispatch("move_documents", {
        "paths": [f"{catalog.originals_dir.name}/src_x/1/blob.md"],
        "target_category": "normal",
    })
    assert out_of_managed["code"] == 409
    assert managed_file.is_file()
    assert not (normal / "blob.md").exists()


def test_listing_graph_and_index_exclude_managed_content(tmp_path):
    root, catalog = _registered_root(tmp_path)
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    (catalog.originals_dir / "hidden.md").write_text("# Hidden\n", encoding="utf-8")

    svc = KnowledgeService(str(tmp_path))
    listing = svc.list_tree()
    dirs = [node["dir"] for node in listing["tree"]]
    assert "notes" in dirs
    assert catalog.originals_dir.name not in dirs

    graph = svc.build_graph()
    assert [node["id"] for node in graph["nodes"]] == ["notes/a.md"]

    assert svc.rebuild_index_md() is True
    index_text = (root / "index.md").read_text(encoding="utf-8")
    assert "notes/a.md" in index_text
    assert "Hidden" not in index_text
