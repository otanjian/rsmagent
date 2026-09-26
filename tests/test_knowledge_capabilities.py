"""Capability projection and default-off flags (task 1.5)."""

from pathlib import Path

import pytest

from config import available_setting, conf
from agent.knowledge.capabilities import knowledge_capabilities
from agent.knowledge.catalog import KnowledgeCatalog
from agent.knowledge.scope import KnowledgeScope


def test_new_switches_default_to_off():
    assert available_setting["knowledge_source_upload_enabled"] is False
    assert available_setting["knowledge_conversion_enabled"] is False
    # A minimal test config that omits them must still read as off.
    assert not conf().get("knowledge_source_upload_enabled")
    assert not conf().get("knowledge_conversion_enabled")


def test_projection_separates_configuration_permission_and_dependency():
    caps = knowledge_capabilities(can_write=False, dependency=lambda: (True, ""))
    assert caps["knowledge_enabled"] is True
    assert caps["agent_search"]["enabled"] is True
    assert caps["source_upload"]["configured"] is False
    assert caps["source_upload"]["available"] is False
    assert caps["conversion"]["available"] is False
    assert caps["source_upload"]["reason"]


def test_upload_needs_permission_and_flag(monkeypatch):
    monkeypatch.setitem(conf(), "knowledge_source_upload_enabled", True)
    assert knowledge_capabilities(can_write=True,
                                  dependency=lambda: (True, ""))["source_upload"]["available"]
    assert not knowledge_capabilities(can_write=False,
                                      dependency=lambda: (True, ""))["source_upload"]["available"]

    monkeypatch.setitem(conf(), "knowledge_source_upload_enabled", False)
    assert not knowledge_capabilities(can_write=True,
                                      dependency=lambda: (True, ""))["source_upload"]["available"]


def test_conversion_needs_flag_and_real_dependency(monkeypatch):
    monkeypatch.setitem(conf(), "knowledge_source_upload_enabled", True)
    monkeypatch.setitem(conf(), "knowledge_conversion_enabled", True)

    ready = knowledge_capabilities(can_write=True, dependency=lambda: (True, ""))
    assert ready["conversion"]["available"] is True
    assert ready["conversion"]["reason"] == ""

    missing = knowledge_capabilities(
        can_write=True, dependency=lambda: (False, "no parser"))
    assert missing["conversion"]["available"] is False
    assert missing["conversion"]["reason"] == "no parser"
    # Upload still works with conversion unavailable: saving does not require it.
    assert missing["source_upload"]["available"] is True


def test_master_switch_disables_everything(monkeypatch):
    monkeypatch.setitem(conf(), "knowledge", False)
    monkeypatch.setitem(conf(), "knowledge_source_upload_enabled", True)
    monkeypatch.setitem(conf(), "knowledge_conversion_enabled", True)
    caps = knowledge_capabilities(can_write=True, dependency=lambda: (True, ""))
    assert caps["knowledge_enabled"] is False
    assert caps["source_upload"]["available"] is False
    assert caps["conversion"]["available"] is False


def test_protection_and_filtering_survive_flags_being_off(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("# a\n", encoding="utf-8")
    catalog = KnowledgeCatalog(str(root)).initialize()
    catalog.ensure_internal_dirs()
    (catalog.originals_dir / "hidden.md").write_text("# hidden\n", encoding="utf-8")

    monkeypatch.setitem(conf(), "knowledge_source_upload_enabled", False)
    monkeypatch.setitem(conf(), "knowledge_conversion_enabled", False)

    scope = KnowledgeScope(str(root))
    assert sorted(scope.iter_effective_rel_paths()) == ["notes/a.md"]
    with pytest.raises(Exception):
        scope.guard(catalog.originals_dir / "hidden.md")
