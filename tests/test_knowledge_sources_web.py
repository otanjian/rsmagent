"""Fork HTTP surface for original sources (task 2.3).

Delegation contracts and the download identity/disposition rules. Route-level
tenant authorization is covered by ``tests/test_knowledge_console_database.py``;
here the identity layer is stubbed so the handler's own behaviour is isolated.
"""

import contextlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from config import conf


@contextlib.contextmanager
def _identity_scope(tmp_path):
    """Stub the database authorization chain for source handlers."""
    (tmp_path / "knowledge").mkdir(exist_ok=True)
    with patch("channel.web.web_channel._db_scope", _null_scope), \
            patch("channel.web.web_channel._require_knowledge_write",
                  lambda ctx, agent_id: None), \
            patch("channel.web.web_channel._require_read_permission",
                  lambda ctx, permission: None), \
            patch("channel.web.web_channel._require_tenant_agent_binding",
                  lambda ctx, agent_id: agent_id), \
            patch("channel.web.web_channel._require_private_owner",
                  lambda ctx, agent_id: None), \
            patch("channel.web.web_channel._require_agent_action",
                  lambda ctx, agent_id, action, permission: None), \
            patch("channel.web.web_channel._knowledge_workspace_root",
                  return_value=str(tmp_path)):
        yield


@contextlib.contextmanager
def _null_scope():
    yield None


@contextlib.contextmanager
def _download_scope():
    yield None, "agent-1"


class UploadedFile:
    def __init__(self, filename, content):
        self.filename = filename
        self.value = content


@pytest.fixture(autouse=True)
def upload_enabled(monkeypatch):
    monkeypatch.setitem(conf(), "knowledge_source_upload_enabled", True)


def _params(**kwargs):
    """A dict-like web.input() result with attribute-free .get access."""
    class _Params(dict):
        def __getattr__(self, item):
            return self.get(item, "")
    return _Params(kwargs)


def test_list_handler_returns_sources_and_capabilities(tmp_path):
    from channel.web.web_channel import KnowledgeSourcesHandler

    with _identity_scope(tmp_path), \
            patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.input", return_value=_params(agent_id="a")):
        response = json.loads(KnowledgeSourcesHandler().GET())

    assert response["status"] == "success"
    assert response["sources"] == []
    assert response["registered"] is False
    assert response["capabilities"]["source_upload"]["configured"] is True
    assert "limits" in response


def test_upload_handler_saves_and_reports_per_file_results(tmp_path):
    from channel.web.web_channel import KnowledgeSourceUploadHandler

    params = _params(
        agent_id="a", category="notes", conflict="ask", request_id="r1",
        files=[UploadedFile("a.txt", b"one"), UploadedFile("b.txt", b"two")],
    )
    with _identity_scope(tmp_path), \
            patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.ctx") as ctx, \
            patch("channel.web.web_channel._raw_web_input", return_value=params):
        ctx.env = {}
        response = json.loads(KnowledgeSourceUploadHandler().POST())

    assert response["status"] == "success"
    assert response["saved"] == 2
    assert [r["filename"] for r in response["results"]] == ["a.txt", "b.txt"]
    assert all(r["status"] == "saved" for r in response["results"])
    saved = list(Path(tmp_path, "knowledge").rglob("*.txt"))
    assert sorted(p.name for p in saved) == ["a.txt", "b.txt"]


def test_upload_handler_reports_an_oversized_file_per_item(tmp_path, monkeypatch):
    from channel.web.web_channel import KnowledgeSourceUploadHandler

    monkeypatch.setitem(conf(), "knowledge_source_max_file_size", 4)
    params = _params(
        agent_id="a", request_id="r1",
        files=[UploadedFile("ok.txt", b"fine"), UploadedFile("big.bin", b"too big")],
    )
    with _identity_scope(tmp_path), \
            patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.ctx") as ctx, \
            patch("channel.web.web_channel._raw_web_input", return_value=params):
        ctx.env = {}
        response = json.loads(KnowledgeSourceUploadHandler().POST())

    assert response["status"] == "success"
    assert response["saved"] == 1 and response["failed"] == 1
    assert response["results"][-1]["filename"] == "big.bin"
    assert response["results"][-1]["code"] == "source_quota_exceeded"
    # The oversized file left nothing behind.
    stored = sorted(p.name for p in Path(tmp_path, "knowledge").rglob("*.txt"))
    assert stored == ["ok.txt"]


def test_upload_handler_reports_large_body_before_identity(tmp_path):
    from channel.web.web_channel import KnowledgeSourceUploadHandler
    from agent.knowledge.sources import source_limits

    with patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.ctx") as ctx:
        ctx.env = {"CONTENT_LENGTH": str(source_limits()["max_batch_size"] + 1)}
        response = json.loads(KnowledgeSourceUploadHandler().POST())

    assert response["status"] == "error"
    assert response["code"] == "source_quota_exceeded"


def test_upload_handler_refuses_when_switch_off(tmp_path, monkeypatch):
    from channel.web.web_channel import KnowledgeSourceUploadHandler

    monkeypatch.setitem(conf(), "knowledge_source_upload_enabled", False)
    params = _params(agent_id="a", files=[UploadedFile("a.txt", b"one")])
    with _identity_scope(tmp_path), \
            patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.ctx") as ctx, \
            patch("channel.web.web_channel._raw_web_input", return_value=params):
        ctx.env = {}
        ctx.status = "200 OK"
        response = json.loads(KnowledgeSourceUploadHandler().POST())
        assert ctx.status == "503 Service Unavailable"

    assert response["status"] == "error"
    assert response["code"] == "source_unavailable"


def test_lifecycle_handler_delegates_disable(tmp_path):
    from channel.web.web_channel import KnowledgeSourceLifecycleHandler

    request = {"source_id": "src_1", "action": "disable", "agent_id": "a"}
    seen = {}

    with _identity_scope(tmp_path), \
            patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.data",
                  return_value=json.dumps(request).encode()), \
            patch("agent.knowledge.sources.SourceAssetService.set_lifecycle",
                  side_effect=lambda sid, action: seen.update(source_id=sid, action=action)
                  or {"source": {"source_id": sid}}):
        response = json.loads(KnowledgeSourceLifecycleHandler().POST())

    assert seen == {"source_id": "src_1", "action": "disable"}
    assert response["status"] == "success"


def test_task_handler_requires_valid_action(tmp_path):
    from channel.web.web_channel import KnowledgeSourceTaskHandler

    with _identity_scope(tmp_path), \
            patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.ctx") as ctx, \
            patch("channel.web.web_channel.web.data",
                  return_value=json.dumps({"task_id": "t1", "action": "explode"}).encode()):
        ctx.status = "200 OK"
        response = json.loads(KnowledgeSourceTaskHandler().POST())

    assert response["code"] == "source_invalid"
    assert ctx.status == "400 Bad Request"


def test_convert_action_addresses_a_source_not_a_task(tmp_path):
    """``convert`` has no task yet, so it is validated on ``source_id``."""
    from channel.web.web_channel import KnowledgeSourceTaskHandler

    seen = {}

    with _identity_scope(tmp_path), \
            patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.data",
                  return_value=json.dumps({"source_id": "src_1", "action": "convert"}).encode()), \
            patch("agent.knowledge.sources.SourceAssetService.request_conversion",
                  side_effect=lambda sid: seen.update(source_id=sid) or {"task": None}):
        response = json.loads(KnowledgeSourceTaskHandler().POST())

    assert seen == {"source_id": "src_1"}
    assert response["status"] == "success"

    # A convert request without a source is a bad request, not a task lookup.
    with _identity_scope(tmp_path), \
            patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.ctx") as ctx, \
            patch("channel.web.web_channel.web.data",
                  return_value=json.dumps({"task_id": "t1", "action": "convert"}).encode()):
        ctx.status = "200 OK"
        response = json.loads(KnowledgeSourceTaskHandler().POST())

    assert response["code"] == "source_invalid"
    assert ctx.status == "400 Bad Request"


def test_convert_refusal_is_reported_with_its_reason(tmp_path):
    """With no converter installed the refusal carries the projected reason."""
    from channel.web.web_channel import KnowledgeSourceTaskHandler
    from agent.knowledge.sources import SourceAssetService

    # ``state_dir`` opts into an Agent's own knowledge/ by presence, so the
    # directory must exist before the service resolves its root -- and before
    # the upload, so the fixture writes into tmp_path and not the shared root.
    (tmp_path / "knowledge").mkdir(exist_ok=True)
    # A real service and a real source, so the refusal comes from the capability
    # projection rather than a stub: conversion is off and the skill missing.
    receipt = SourceAssetService(str(tmp_path)).save_files(
        [{"filename": "a.pdf", "content": b"%PDF-1.4 one"}], request_id="r1",
    )["results"][0]

    with _identity_scope(tmp_path), \
            patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.ctx") as ctx, \
            patch("channel.web.web_channel.web.data",
                  return_value=json.dumps({"source_id": receipt["source_id"],
                                           "action": "convert"}).encode()):
        ctx.status = "200 OK"
        response = json.loads(KnowledgeSourceTaskHandler().POST())

    assert response["status"] == "error"
    assert response["code"] == "source_unavailable"
    assert response["message"], "the refusal says why, so the console can show it"


def test_download_handler_streams_bytes_with_download_disposition(tmp_path):
    from channel.web.web_channel import KnowledgeSourceDownloadHandler
    from agent.knowledge.sources import SourceAssetService

    # ``state_dir`` opts into an Agent's own knowledge/ by presence, so the
    # directory must exist before the service resolves its root.
    (tmp_path / "knowledge").mkdir(exist_ok=True)
    service = SourceAssetService(str(tmp_path))
    receipt = service.save_files(
        [{"filename": "page.html", "content": b"<script>alert(1)</script>"}],
        request_id="r1",
    )["results"][0]

    headers = {}
    with _identity_scope(tmp_path), \
            patch("channel.web.fork.handlers.knowledge_sources._source_download_scope",
                  _download_scope), \
            patch("channel.web.web_channel.web.header",
                  side_effect=lambda k, v: headers.__setitem__(k, v)), \
            patch("channel.web.web_channel.web.input",
                  return_value=_params(agent_id="a", source_id=receipt["source_id"])):
        body = KnowledgeSourceDownloadHandler().GET()

    assert body == b"<script>alert(1)</script>"
    assert headers["X-Content-Type-Options"] == "nosniff"
    # Active content is never rendered inline with the console's origin.
    assert headers["Content-Disposition"].startswith("attachment;")
    assert headers["Cache-Control"] == "private, no-store"


def test_detail_handler_routes_source_errors_to_http_status(tmp_path):
    from channel.web.web_channel import KnowledgeSourceDetailHandler

    with _identity_scope(tmp_path), \
            patch("channel.web.web_channel.web.header"), \
            patch("channel.web.web_channel.web.ctx") as ctx, \
            patch("channel.web.web_channel.web.input",
                  return_value=_params(agent_id="a", source_id="src_missing")):
        ctx.status = "200 OK"
        response = json.loads(KnowledgeSourceDetailHandler().GET())
        assert ctx.status == "404 Not Found"

    assert response["code"] == "source_not_found"
