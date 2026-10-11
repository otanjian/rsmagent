"""The panel serves a personal coding session from the remote ``U``.

change ``add-coding-agent-personal-workspace`` (task 3.1). A session whose
coding link carries a ``workspace_id`` is *personal*: its files live in
``U = P/user/<account>`` on the OpenCode host, so the console's file routes must
answer from there rather than from the platform's own workspace. These tests
drive the real routes with a scripted remote and assert three things:

* the read routes dispatch to the remote source and label every row
  ``source: "coding_remote"``, with no platform-local ``abs_path``/``raw_url``;
* the download route relays the remote bytes and refuses every non-remote
  source, so it can never become a second way to read the local disk;
* a personal binding is recognised only from the session's own link, and a link
  from a different service instance is refused rather than served.

A separate fixture replaces the binding resolver, so no socket is opened and the
platform's own workspace is never consulted.
"""

import io
import json
import os
from urllib.parse import quote

import pytest

from agent.coding.files import CodingRemoteFiles, RemoteSource
from tests._helpers import WebAppHarness

AGENT = "shared-agent"
U = "C:/projects/erp/user/alice"


class _FakeRemote:
    """A scripted OpenCode filesystem, keyed by normalized relative id."""

    def __init__(self, files=None, dirs=()):
        self.files = dict(files or {})
        self.dirs = set(dirs)

    def list_files(self, directory, path=""):
        prefix = path + "/" if path else ""
        rows, seen = [], set()
        for rel in list(self.files) + list(self.dirs):
            if prefix and not rel.startswith(prefix):
                continue
            rest = rel[len(prefix):] if prefix else rel
            if not rest:
                continue
            head, sep, _ = rest.partition("/")
            if head in seen:
                continue
            seen.add(head)
            full = prefix + head
            is_dir = bool(sep) or full in self.dirs
            rows.append({
                "path": full.replace("/", "\\") + ("\\" if is_dir else ""),
                "type": "directory" if is_dir else "file",
            })
        return rows

    def find_files(self, directory, query, kind=None, limit=None):
        rows = []
        for rel in sorted(list(self.files) + list(self.dirs)):
            if query.lower() not in rel.lower():
                continue
            is_dir = rel in self.dirs
            if kind and kind != ("directory" if is_dir else "file"):
                continue
            rows.append({"path": rel, "type": "directory" if is_dir else "file"})
        return rows[:limit] if limit else rows

    def read_file(self, directory, path):
        if path not in self.files:
            raise FileNotFoundError(f"File not found: {path}")
        return self.files[path]


def _remote():
    client = _FakeRemote(
        files={"uploads/hello.txt": (b"hello", "text/plain"),
               "notes.md": (b"# notes", "text/markdown")},
        dirs={"uploads", "outputs"},
    )
    return CodingRemoteFiles(client, U, workspace_id="ocws_alice"), RemoteSource(U, "ocws_alice")


@pytest.fixture()
def web_harness(tmp_path, monkeypatch):
    harness = WebAppHarness(tmp_path)
    harness.workspace = os.path.join(harness.shared_root, "agents", AGENT)
    harness.write_roster([
        {"id": AGENT, "name": "Shared", "workspace": harness.workspace},
    ])
    harness.add_agent(AGENT)
    harness.alice = harness.member("alice", ["member"])

    import channel.web.fork.handlers.workspace as workspace_handler
    harness.workspace_handler = workspace_handler
    harness.real_remote_service = workspace_handler._coding_remote_service
    monkeypatch.setattr(
        workspace_handler, "_coding_remote_service", lambda ctx, session, agent: None)
    yield harness
    harness.close()


def _bind(harness, monkeypatch, value):
    monkeypatch.setattr(
        harness.workspace_handler,
        "_coding_remote_service",
        lambda ctx, session, agent: value,
    )


def _json(response):
    return json.loads(response.data.decode("utf-8"))


def _url(route, **params):
    return route + "?" + "&".join(f"{k}={quote(str(v))}" for k, v in params.items())


# --- the read routes ------------------------------------------------------


def test_the_tree_serves_the_remote_directory(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/tree", agent=AGENT), token=web_harness.login("alice"))

    assert response.status.startswith("200"), response.data
    body = _json(response)
    assert body["source"]["kind"] == "coding_remote"
    assert body["root"] == U
    assert [e["path"] for e in body["entries"]] == ["outputs", "uploads", "notes.md"]
    assert all(e["source"] == "coding_remote" for e in body["entries"])
    # A remote row must not carry a platform-local path or a local serve URL.
    assert all("abs_path" not in e and "raw_url" not in e for e in body["entries"])


def test_the_tree_refuses_an_escaping_path(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/tree", agent=AGENT, path="../secret"),
        token=web_harness.login("alice"))

    body = _json(response)
    assert body["status"] == "error"
    assert body["code"] == "invalid_reference"


def test_search_answers_from_the_remote(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/search", agent=AGENT, q="hello"),
        token=web_harness.login("alice"))

    body = _json(response)
    assert body["source"]["kind"] == "coding_remote"
    assert [e["path"] for e in body["results"]] == ["uploads/hello.txt"]
    assert body["results"][0]["source"] == "coding_remote"


def test_resolve_offers_a_remote_download_not_a_local_url(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/resolve", agent=AGENT, path="uploads/hello.txt"),
        token=web_harness.login("alice"))

    body = _json(response)
    assert body["file"]["is_dir"] is False
    assert body["file"]["download_url"].startswith("/api/workspace/download?")
    assert "raw_url" not in body["file"]
    assert "preview_url" not in body["file"]


def test_read_returns_the_remote_text_and_never_offers_to_save_it(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/read", agent=AGENT, path="uploads/hello.txt"),
        token=web_harness.login("alice"))

    body = _json(response)
    assert body["content"] == "hello"
    assert body["editable"] is False


def test_meta_reports_the_remote_root(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/meta", agent=AGENT), token=web_harness.login("alice"))

    body = _json(response)
    assert body["source"]["kind"] == "coding_remote"
    assert body["root"] == U


# --- the download route ---------------------------------------------------


def test_download_streams_the_remote_bytes(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/download", agent=AGENT, path="uploads/hello.txt"),
        token=web_harness.login("alice"))

    assert response.status.startswith("200"), response.data
    assert response.data == b"hello"
    assert "text/plain" in response.headers.get("Content-Type", "")
    assert "attachment" in response.headers.get("Content-Disposition", "")


def test_download_refuses_a_non_remote_source(web_harness):
    """The default fixture has no personal binding, so this file is not remote.

    The route must refuse rather than fall back to the platform's own disk.
    """
    response = web_harness.get(
        _url("/api/workspace/download", agent=AGENT, path="uploads/hello.txt"),
        token=web_harness.login("alice"))

    assert not response.status.startswith("200")


def test_download_refuses_an_escaping_path(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/download", agent=AGENT, path="../secret"),
        token=web_harness.login("alice"))

    assert not response.status.startswith("200")


def test_download_reports_a_missing_remote_file_as_not_found(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/download", agent=AGENT, path="uploads/missing.txt"),
        token=web_harness.login("alice"))

    assert not response.status.startswith("200")


# --- generated results land in the session's own output folder (task 3.4) --


def _remote_with_output():
    """The scripted remote, plus a report generated by one run."""
    client = _FakeRemote(
        files={"outputs/sess-1/req-9/report.xlsx": (b"XLSXBYTES", "application/vnd.ms-excel"),
               "uploads/report.xlsx": (b"UPLOADED", "application/vnd.ms-excel")},
        dirs={"outputs", "uploads"},
    )
    return (CodingRemoteFiles(client, U, workspace_id="ocws_alice"),
            RemoteSource(U, "ocws_alice"))


def test_resolve_points_a_generated_result_at_the_remote_file(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote_with_output())

    response = web_harness.get(
        _url("/api/workspace/resolve", agent=AGENT, path="outputs/sess-1/req-9/report.xlsx"),
        token=web_harness.login("alice"))

    body = _json(response)
    assert body["file"]["path"] == "outputs/sess-1/req-9/report.xlsx"
    assert body["file"]["download_url"].startswith("/api/workspace/download?")
    # Nothing platform-local is offered for a file that lives on the host.
    assert "raw_url" not in body["file"] and "abs_path" not in body["file"]


def test_a_claimed_result_that_is_not_there_is_not_found(web_harness, monkeypatch):
    """A name the model *said* it wrote is not a file: the resolve says so.

    That answer is what keeps the console from rendering a download for a
    result the tool never delivered (spec: 工具没有实际交付文件).
    """
    _bind(web_harness, monkeypatch, _remote_with_output())

    response = web_harness.get(
        _url("/api/workspace/resolve", agent=AGENT,
             path="outputs/sess-1/req-9/never-written.xlsx"),
        token=web_harness.login("alice"))

    # The route's own convention: a refusal in the body, which the console's
    # fetch wrapper raises on (`status !== 'success'`).
    body = _json(response)
    assert body["status"] == "error"
    assert "never-written.xlsx" in body["message"]


def test_the_generated_result_downloads_its_own_bytes_not_a_same_named_upload(
        web_harness, monkeypatch):
    """Same name, two locations: the session's folder is what is read.

    The upload area and the output area can both hold `report.xlsx`, which is
    exactly why the default location is per session and per run instead of one
    shared `outputs/` a second conversation could overwrite.
    """
    _bind(web_harness, monkeypatch, _remote_with_output())

    response = web_harness.get(
        _url("/api/workspace/download", agent=AGENT, path="outputs/sess-1/req-9/report.xlsx"),
        token=web_harness.login("alice"))

    assert response.status.startswith("200"), response.data
    assert response.data == b"XLSXBYTES"


def test_the_tree_advertises_honest_capabilities(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/tree", agent=AGENT), token=web_harness.login("alice"))

    caps = _json(response)["source"]["capabilities"]
    assert caps["read"] and caps["download"] and caps["upload"]
    assert not caps["write"] and not caps["delete"] and not caps["trash"]


def test_meta_reports_the_remote_source_and_capabilities(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/meta", agent=AGENT), token=web_harness.login("alice"))

    body = _json(response)
    assert body["source"]["kind"] == "coding_remote"
    assert body["source"]["label"] == "alice"
    assert body["source"]["capabilities"]["upload"] is True


def test_write_is_refused_in_the_personal_directory(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.post(
        "/api/workspace/write", {"path": "notes.md", "content": "x"},
        token=web_harness.login("alice"))

    body = _json(response)
    assert body["status"] == "error"
    assert body["code"] == "unsupported_source"


def test_delete_is_refused_in_the_personal_directory(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.post(
        "/api/workspace/delete", {"targets": ["notes.md"]},
        token=web_harness.login("alice"))

    body = _json(response)
    assert body["status"] == "error"
    assert body["code"] == "unsupported_source"


def test_the_recycle_bin_is_refused_in_the_personal_directory(web_harness, monkeypatch):
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.get(
        _url("/api/workspace/trash", agent=AGENT), token=web_harness.login("alice"))

    body = _json(response)
    assert body["status"] == "error"
    assert body["code"] == "unsupported_source"


# --- the upload path ------------------------------------------------------


class _Part:
    """The shape web.py hands the handler for one multipart file."""

    def __init__(self, filename, data):
        self.filename = filename
        self.file = io.BytesIO(data)
        self.value = data


class _SpyRemoteFiles:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"path": "uploads/note.txt", "name": "note.txt",
                                 "size": 3, "renamed": False}

    def upload_file(self, relative_path, data):
        self.calls.append((relative_path, data))
        return dict(self.result)


def _upload_params(part, **over):
    params = {"file": part, "dir": "uploads", "relative_path": "note.txt"}
    params.update(over)
    return params


def test_upload_remote_targets_the_bound_relative_path():
    handler = web_harness_handler()
    svc = _SpyRemoteFiles()
    source = RemoteSource(U, "ocws_alice")

    out = handler._upload_remote(svc, source, _upload_params(_Part("note.txt", b"abc")), "")

    body = json.loads(out)
    assert body["status"] == "success"
    assert body["source"]["kind"] == "coding_remote"
    assert svc.calls == [("uploads/note.txt", b"abc")]


def test_upload_remote_defaults_to_manual_without_a_session():
    handler = web_harness_handler()
    svc = _SpyRemoteFiles()
    source = RemoteSource(U, "ocws_alice")

    handler._upload_remote(
        svc, source, _upload_params(_Part("note.txt", b"abc"), dir=""), None)

    assert svc.calls[0][0] == "uploads/manual/note.txt"


def test_upload_remote_refuses_an_escaping_destination():
    handler = web_harness_handler()
    svc = _SpyRemoteFiles()
    source = RemoteSource(U, "ocws_alice")

    out = handler._upload_remote(
        svc, source, _upload_params(_Part("note.txt", b"abc"), dir="../x"), "")

    body = json.loads(out)
    assert body["status"] == "error"
    assert body["code"] == "invalid_reference"
    assert svc.calls == []


def test_upload_remote_refuses_a_missing_file():
    handler = web_harness_handler()
    svc = _SpyRemoteFiles()
    source = RemoteSource(U, "ocws_alice")

    out = handler._upload_remote(svc, source, {"dir": "uploads"}, "")

    assert json.loads(out)["code"] == "no_file"


def test_upload_remote_enforces_the_size_ceiling():
    from channel.web.fork.handlers.workspace import (
        WS_UPLOAD_MAX_BYTES,
        WorkspaceUploadError,
        WorkspaceUploadHandler,
    )

    part = _Part("big.bin", b"x" * (WS_UPLOAD_MAX_BYTES + 1))
    with pytest.raises(WorkspaceUploadError):
        WorkspaceUploadHandler._read_part(part)


def test_upload_remote_reads_the_whole_part():
    from channel.web.fork.handlers.workspace import WorkspaceUploadHandler

    assert WorkspaceUploadHandler._read_part(_Part("a.txt", b"payload")) == b"payload"


def test_a_remote_upload_cannot_skip_the_csrf_gate(web_harness, monkeypatch):
    """The personal upload funnels through the same origin/CSRF gate as a local one.

    The remote transport is new; the identity and CSRF responsibility domain it
    writes under is not. A hand-made request must be refused by that gate before
    any byte reaches the host, so the new path cannot become a way *around* the
    control the local upload already answers to (spec: 不能绕过).
    """
    _bind(web_harness, monkeypatch, _remote())

    response = web_harness.post(
        "/api/workspace/upload",
        {"dir": "uploads", "relative_path": "x.txt"},
        token=web_harness.login("alice"),
        headers={"Origin": "https://evil.example"})

    assert response.status.startswith("403"), response.data
    assert b"csrf_failed" in response.data


def web_harness_handler():
    import channel.web.fork.handlers.workspace as workspace_handler

    return workspace_handler.WorkspaceUploadHandler


# --- binding detection ----------------------------------------------------


def _settings(**overrides):
    from agent.coding import CodingSettings

    values = {
        "enabled": True,
        "service_id": "default",
        "api_url": "http://127.0.0.1:4096",
        "web_url": "https://code.example.com",
        "username": "opencode",
        "password": "s3cret",
        "personal_workspace_enabled": True,
    }
    values.update(overrides)
    return CodingSettings(**values)


def _patch_link(monkeypatch, settings, link):
    import agent.coding as coding
    import channel.web.fork.handlers.coding as coding_handler

    monkeypatch.setattr(coding, "resolve_settings", lambda *a, **k: settings)

    class _Store:
        def get_coding_link(self, session_id):
            return link

    monkeypatch.setattr(coding_handler, "_conversation_store", lambda agent_id: _Store())


def test_a_personal_link_is_recognised(monkeypatch):
    import channel.web.fork.handlers.workspace as ws

    settings = _settings()
    link = {"service_id": "default", "project_dir": U, "workspace_id": "ocws_alice"}
    _patch_link(monkeypatch, settings, link)

    assert ws._coding_remote(None, "sess-1", AGENT) == (settings, link)


def test_an_ordinary_session_is_not_remote(monkeypatch):
    import channel.web.fork.handlers.workspace as ws

    link = {"service_id": "default", "project_dir": "/srv/x", "workspace_id": ""}
    _patch_link(monkeypatch, _settings(), link)

    assert ws._coding_remote(None, "sess-1", AGENT) is None


def test_a_session_with_no_link_is_not_remote(monkeypatch):
    import channel.web.fork.handlers.workspace as ws

    _patch_link(monkeypatch, _settings(), None)

    assert ws._coding_remote(None, "sess-1", AGENT) is None


def test_a_link_from_another_service_is_refused_not_served(web_harness, monkeypatch):
    """A link from a different service instance must not be served by this one.

    Driven through the route because the refusal is a ``web.HTTPError``, which
    only exists inside a request.
    """
    link = {"service_id": "other", "project_dir": U, "workspace_id": "ocws_alice"}
    _patch_link(monkeypatch, _settings(service_id="default"), link)
    monkeypatch.setattr(
        web_harness.workspace_handler, "_coding_remote_service",
        web_harness.real_remote_service)
    # A session id is what makes the panel look for a binding; its ownership is
    # a different rule (tested elsewhere), so it is stubbed here.
    monkeypatch.setattr(
        web_harness.workspace_handler, "_workspace_request_scope",
        lambda ctx, session, agent: AGENT)

    response = web_harness.get(
        _url("/api/workspace/tree", agent=AGENT, session="sess-1"),
        token=web_harness.login("alice"))

    assert response.status.startswith("409"), response.data
    assert b"coding_service_changed" in response.data


def test_a_disabled_capability_has_no_remote_source(monkeypatch):
    import channel.web.fork.handlers.workspace as ws

    link = {"service_id": "default", "project_dir": U, "workspace_id": "ocws_alice"}
    _patch_link(monkeypatch, _settings(enabled=False), link)

    assert ws._coding_remote(None, "sess-1", AGENT) is None


# --- an old coding session opens U with a notice (task 2.4, design section 6) -


def _patch_legacy_deps(monkeypatch, link, *, personal=True):
    """Wire the legacy resolver's dependencies to a scripted account directory."""
    import agent.coding as coding
    import agent.coding.workspace as workspace_mod
    import auth.service as auth_service

    monkeypatch.setattr(coding, "resolve_settings",
                        lambda *a, **k: _settings(personal_workspace_enabled=personal))
    _patch_link(monkeypatch, _settings(personal_workspace_enabled=personal), link)

    opened = {}

    class _Workspace:
        def __init__(self, identity, settings=None):
            pass

        def open(self, *, agent_id, base_project_dir, user_id, tenant_id, session_id=""):
            opened.update(agent_id=agent_id, base=base_project_dir,
                          user=user_id, tenant=tenant_id, session=session_id)
            return {"effective_directory": U, "workspace_id": "ocws_alice"}

    monkeypatch.setattr(workspace_mod, "CodingWorkspaceService", _Workspace)
    monkeypatch.setattr(auth_service, "get_identity_service", lambda: object())
    return opened


def test_a_legacy_session_opens_the_account_directory_with_a_notice(monkeypatch):
    """An old coding session's button opens U from its own project snapshot.

    A conversation created before the personal capability keeps running in the
    project root ``P``. Its panel opens the caller's ``U`` -- resolved from the
    session's project snapshot and the verified account -- marked ``legacy``: the
    panel reads and downloads, offers no upload (the running tools work in ``P``),
    and carries the notice the client renders.
    """
    import channel.web.fork.handlers.workspace as ws
    from types import SimpleNamespace

    link = {"service_id": "default", "project_dir": "C:/projects/erp",
            "workspace_id": ""}
    opened = _patch_legacy_deps(monkeypatch, link)

    files, source = ws._legacy_personal_remote(
        SimpleNamespace(user_id="u-alice", tenant_id="t-1"), "sess-legacy", AGENT)

    assert files.directory == U
    assert opened == {"agent_id": AGENT, "base": "C:/projects/erp",
                      "user": "u-alice", "tenant": "t-1", "session": "sess-legacy"}
    described = source.describe()
    assert described["kind"] == "coding_remote"
    assert described["legacy"] is True
    assert described["capabilities"]["read"] and described["capabilities"]["download"]
    assert described["capabilities"]["upload"] is False


def test_a_legacy_session_offers_no_panel_upload():
    """The legacy panel refuses an upload the running turn could not find."""
    from channel.web.fork.handlers.workspace import WorkspaceUploadHandler

    svc = _SpyRemoteFiles()
    source = RemoteSource(U, "ocws_alice", legacy=True)

    out = WorkspaceUploadHandler._upload_remote(
        svc, source, _upload_params(_Part("note.txt", b"abc")), "sess-legacy")

    body = json.loads(out)
    assert body["status"] == "error"
    assert body["code"] == "unsupported_source"
    assert svc.calls == []


def test_a_personal_session_is_not_treated_as_legacy(monkeypatch):
    """Only a session with no binding is legacy; a personal one keeps its own path."""
    import channel.web.fork.handlers.workspace as ws
    from types import SimpleNamespace

    link = {"service_id": "default", "project_dir": U, "workspace_id": "ocws_alice"}
    _patch_legacy_deps(monkeypatch, link)

    assert ws._legacy_personal_remote(
        SimpleNamespace(user_id="u-alice", tenant_id="t-1"), "sess-1", AGENT) is None


def test_a_legacy_session_is_not_remote_when_the_capability_is_off(monkeypatch):
    """With the switch off the old session keeps its original source."""
    import channel.web.fork.handlers.workspace as ws
    from types import SimpleNamespace

    link = {"service_id": "default", "project_dir": "C:/projects/erp",
            "workspace_id": ""}
    _patch_legacy_deps(monkeypatch, link, personal=False)

    assert ws._legacy_personal_remote(
        SimpleNamespace(user_id="u-alice", tenant_id="t-1"), "sess-legacy", AGENT) is None


# --- another member's session is not reachable (task 4.3) ------------------


def test_another_members_personal_session_is_refused_before_the_remote(
        web_harness, monkeypatch):
    """The panel is scoped to the caller's own session, and the remote is not asked.

    The remote source is derived *from the session link*, so a session that is
    not the caller's must be refused before that link is read at all -- otherwise
    "open another member's account directory" would be one query parameter away.
    The refusal is the same 404 a missing session gets, so it does not confirm
    the row exists, and the remote client is never invoked.
    """
    from agent.memory import get_conversation_store
    from common.runtime_identity import RuntimeIdentity, use_identity

    alice = web_harness.user_id("alice")
    web_harness.member("bob", ["member"])

    store = get_conversation_store(web_harness.agent_workspace(AGENT))
    with use_identity(RuntimeIdentity(agent_id=AGENT, user_id=alice,
                                      tenant_id=web_harness.tenant_id)):
        assert store.append_messages(
            "sess_alice", [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            channel_type="web")

    reached = []
    monkeypatch.setattr(
        web_harness.workspace_handler, "_coding_remote_service",
        lambda ctx, session, agent: reached.append(session) or _remote())

    response = web_harness.get(
        _url("/api/workspace/tree", agent=AGENT, session="sess_alice"),
        token=web_harness.login("bob"))

    assert response.status.startswith("404"), response.data
    assert b"session_not_found" in response.data
    assert reached == []


def test_revoking_the_binding_withdraws_the_personal_directory(web_harness, monkeypatch):
    """Dropping the link takes the remote source away instead of leaving it served.

    Revoking is the link row going away -- the same delete a session removal
    performs. The *real* store and the *real* binding resolver are used here (no
    scripted link), so this is the actual revoke path: while the link is present
    the resolver calls the session personal and pins it to ``U``; once the row is
    gone the resolver answers "not personal", the panel is no longer served from
    ``U``, and the file routes are refused without leaking the directory. The
    account keeps owning a directory it can no longer read.
    """
    import agent.coding as coding
    import channel.web.fork.handlers.workspace as ws
    from agent.memory import get_conversation_store
    from common.runtime_identity import RuntimeIdentity, use_identity

    monkeypatch.setattr(coding, "resolve_settings", lambda *a, **k: _settings())
    # The route must genuinely probe the store, not a scripted binding.
    monkeypatch.setattr(
        web_harness.workspace_handler, "_coding_remote_service",
        web_harness.real_remote_service)

    identity = RuntimeIdentity(agent_id=AGENT, user_id=web_harness.user_id("alice"),
                               tenant_id=web_harness.tenant_id)
    store = get_conversation_store(web_harness.agent_workspace(AGENT))
    with use_identity(identity):
        store.create_coding_link(
            "sess_revoke", "ext-1", "default", U,
            state="ready", workspace_id="ocws_alice")

        # Bound: the resolver reads the real link and pins the session to U.
        resolved = ws._coding_remote(None, "sess_revoke", AGENT)
        assert resolved is not None and resolved[1]["project_dir"] == U

        assert store.delete_coding_link("sess_revoke") is True

        # Revoked: the same resolver now answers "not personal" for both the
        # panel and the upload seam -- no directory is served from a dead link.
        assert ws._coding_remote(None, "sess_revoke", AGENT) is None
        assert ws._coding_remote_service(None, "sess_revoke", AGENT) is None

    token = web_harness.login("alice")
    tree = web_harness.get(
        _url("/api/workspace/tree", agent=AGENT, session="sess_revoke"), token=token)
    # The panel is no longer personal: no `coding_remote` descriptor is minted and
    # the account directory is never named, so a dead link cannot keep serving U.
    body = json.loads(tree.data.decode("utf-8"))
    source = body.get("source")
    kind = source.get("kind") if isinstance(source, dict) else source
    assert kind != "coding_remote"
    assert U.encode() not in tree.data

    download = web_harness.get(
        _url("/api/workspace/download", agent=AGENT, session="sess_revoke",
             path="uploads/hello.txt"),
        token=token)
    assert not download.status.startswith("200"), download.data
    assert U.encode() not in download.data
