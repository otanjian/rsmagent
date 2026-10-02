import contextlib
import json
from unittest.mock import patch


@contextlib.contextmanager
def _null_scope():
    """Stub for ``_db_scope`` in direct-handler unit tests (no HTTP context)."""
    yield None


@contextlib.contextmanager
def _authorized_write_scope(tmp_path):
    """Stub the database authorization chain for knowledge write handlers.

    ``KnowledgeActionHandler``/``KnowledgeImportHandler`` no longer gate on
    ``_guard_not_database``; they resolve a tenant context and authorize the
    write (by data root + Agent ownership, see
    ``channel/web/web_channel.py::_knowledge_write_authorized``). These unit
    tests exercise the delegation contract only, so the identity layer is
    stubbed here (route-level authorization is covered by
    ``tests/test_knowledge_console_database.py``).

    ``_knowledge_workspace_root`` is the seam the handlers resolve their base
    through (the Agent's own ``knowledge/`` if it has one, else the tenant
    shared copy); stubbing it keeps this contract test off the Agent roster.
    """
    (tmp_path / "knowledge").mkdir(exist_ok=True)
    with patch("channel.web.web_channel._db_scope", _null_scope), \
            patch("channel.web.web_channel._require_knowledge_write",
                  lambda ctx, agent_id: None), \
            patch("channel.web.web_channel._require_tenant_agent_binding",
                  lambda ctx, agent_id: agent_id), \
            patch("channel.web.web_channel._require_private_owner", lambda ctx, agent_id: None), \
            patch("channel.web.web_channel._knowledge_workspace_root",
                  return_value=str(tmp_path)):
        yield


def test_knowledge_action_handler_delegates_to_dispatch(tmp_path):
    from channel.web.web_channel import KnowledgeActionHandler

    request = {"action": "create_category", "payload": {"path": "research"}}
    dispatched = {"action": "create_category", "code": 200, "message": "success",
                  "payload": {"path": "research", "created": True}}

    with _authorized_write_scope(tmp_path), \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel.web.data", return_value=json.dumps(request).encode()), \
         patch("agent.knowledge.service.KnowledgeService.dispatch", return_value=dispatched) as dispatch:
        response = json.loads(KnowledgeActionHandler().POST())

    dispatch.assert_called_once_with("create_category", {"path": "research"})
    assert response["status"] == "success"
    assert response["payload"]["created"] is True


def test_knowledge_action_handler_preserves_dispatch_error(tmp_path):
    from channel.web.web_channel import KnowledgeActionHandler

    dispatched = {"action": "delete_documents", "code": 403,
                  "message": "protected knowledge file: index.md", "payload": None}
    request = {"action": "delete_documents", "payload": {"paths": ["index.md"]}}

    with _authorized_write_scope(tmp_path), \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel.web.data", return_value=json.dumps(request).encode()), \
         patch("agent.knowledge.service.KnowledgeService.dispatch", return_value=dispatched):
        response = json.loads(KnowledgeActionHandler().POST())

    assert response["status"] == "error"
    assert response["code"] == 403
    assert response["message"] == "protected knowledge file: index.md"


def test_knowledge_frontend_management_contract():
    # The page is assembled from templates/, so assert against what is served.
    from channel.web.core import template
    html = template.render("chat.html")
    from conftest import console_js
    js = console_js()

    assert 'id="knowledge-dialog-overlay"' in html
    assert 'id="knowledge-dialog-textarea"' in html
    assert 'id="knowledge-document-form"' in html
    assert 'id="knowledge-document-path-preview"' in html
    assert "function openKnowledgeDialog(" in js
    assert "function _knowledgeCategoryPaths(" in js
    assert "dispatchKnowledgeAction('create_category'" in js
    assert "dispatchKnowledgeAction('create_document'" in js
    assert "dispatchKnowledgeAction('rename_category'" in js
    assert "dispatchKnowledgeAction('delete_category'" in js
    assert "dispatchKnowledgeAction('delete_documents'" in js
    assert "dispatchKnowledgeAction('move_documents'" in js
    assert 'id="knowledge-import-input"' in html
    assert "function createKnowledgeDocument(" in js
    assert "function openKnowledgeDocumentEditor(" in js
    assert "documentPathPreview.textContent = options.category" in js
    assert "options.type === 'document'" in js
    assert "input.classList.toggle('hidden', options.type === 'select' || options.type === 'textarea' || options.type === 'document')" in js
    assert "function selectKnowledgeImportFiles(" in js
    assert "function importKnowledgeDocuments(" in js
    assert "function validateKnowledgeImportFiles(" in js
    assert "KNOWLEDGE_IMPORT_MAX_FILE_SIZE" in js
    assert "fetch(_kbUrl('/api/knowledge/import')" in js
    assert "initKnowledgeImportDropZone()" in js

    knowledge_section = js[js.index("// Knowledge View"):js.index("function _hasFilterMatch")]
    assert "prompt(" not in knowledge_section
    assert "alert(" not in knowledge_section
    assert "if (path === 'index.md' || path === 'log.md') return '';" in knowledge_section


class UploadedFile:
    def __init__(self, filename, content):
        self.filename = filename
        self.value = content


def test_knowledge_import_handler_delegates_to_dispatch(tmp_path):
    from channel.web.web_channel import KnowledgeImportHandler

    dispatched = {"action": "import_documents", "code": 200, "message": "success",
                  "payload": {"imported": 2, "skipped": 0, "failed": 0}}
    params = {
        "target_category": ["notes"],
        "conflict_strategy": ["rename"],
        "files": [UploadedFile("a.md", b"# A"), UploadedFile("b.txt", b"B")],
    }

    with _authorized_write_scope(tmp_path), \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.core._common._multipart_lists", return_value=params), \
         patch("agent.knowledge.service.KnowledgeService.dispatch", return_value=dispatched) as dispatch:
        response = json.loads(KnowledgeImportHandler().POST())

    dispatch.assert_called_once()
    action, payload = dispatch.call_args.args
    assert action == "import_documents"
    assert payload["target_category"] == "notes"
    assert payload["conflict_strategy"] == "rename"
    assert [f["filename"] for f in payload["files"]] == ["a.md", "b.txt"]
    assert response["status"] == "success"
    assert response["payload"]["imported"] == 2


def test_knowledge_import_handler_rejects_large_content_length(tmp_path):
    from channel.web.web_channel import KnowledgeImportHandler
    from agent.knowledge.service import KnowledgeService
    assert KnowledgeService.MAX_IMPORT_TOTAL_SIZE == 200 * 1024 * 1024

    # The batch-size guard runs before identity resolution, so no auth stub is
    # needed here: an oversized body is refused outright.
    with patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel.web.ctx") as ctx:
        ctx.env = {"CONTENT_LENGTH": str(KnowledgeService.MAX_IMPORT_TOTAL_SIZE + 1)}
        response = json.loads(KnowledgeImportHandler().POST())

    assert response["status"] == "error"
    assert response["code"] == 413
    assert response["message"] == "import batch too large"
