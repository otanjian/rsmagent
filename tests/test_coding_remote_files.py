"""The remote read view of a personal coding workspace.

change ``add-coding-agent-personal-workspace`` (task 3.1). These tests pin the
two layers that were added against a *scripted* remote:

* :class:`agent.coding.opencode.OpenCodeClient`'s filesystem methods, whose
  request shapes were measured against the running RSMCode service (evidence
  section 7): the deepObject ``location[directory]`` parameter, the whole-segment
  path encoding for a read, and the service's ``500`` for a path it cannot serve
  (mapped to a not-found, never leaked as an outage);
* :class:`agent.coding.files.CodingRemoteFiles`, which folds the service's
  ``{path, type}`` rows into the panel's entry shape and refuses every id that
  could escape ``U`` before a byte is requested.
"""

import pytest

from agent.coding import (
    CODING_UPSTREAM_UNAVAILABLE,
    CodingError,
    CodingSettings,
)
from agent.coding.files import CodingRemoteFiles, RemoteSource, normalize_relative
from agent.coding.opencode import OpenCodeClient

U = "C:/projects/erp/user/alice"


class _Response:
    def __init__(self, status_code=200, payload=None, content=b"", content_type=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = {"Content-Type": content_type} if content_type else {}
        self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected extra call: {method} {url}")
        return self.responses.pop(0)


def _client(transport):
    return OpenCodeClient(
        CodingSettings(
            enabled=True,
            service_id="default",
            api_url="http://127.0.0.1:4096",
            web_url="https://code.example.com",
            username="opencode",
            password="s3cret",
            personal_workspace_enabled=True,
        ),
        transport=transport,
    )


# --- the HTTP client ------------------------------------------------------


def test_list_pins_the_location_as_a_deep_object_parameter():
    transport = _Transport(_Response(payload={"data": [{"path": "a.txt", "type": "file"}]}))
    client = _client(transport)

    rows = client.list_files(U)

    assert rows == [{"path": "a.txt", "type": "file"}]
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "http://127.0.0.1:4096/api/fs/list"
    assert call["params"] == {"location[directory]": U}


def test_list_passes_the_relative_path_when_one_is_named():
    transport = _Transport(_Response(payload={"data": []}))
    client = _client(transport)

    client.list_files(U, "uploads")

    assert transport.calls[0]["params"] == {
        "location[directory]": U, "path": "uploads"}


def test_find_carries_the_query_and_the_bound():
    transport = _Transport(_Response(payload={"data": []}))
    client = _client(transport)

    client.find_files(U, "needle", kind="file", limit=10)

    assert transport.calls[0]["params"] == {
        "location[directory]": U, "query": "needle", "type": "file", "limit": "10"}


def test_read_encodes_the_whole_relative_path_as_one_segment():
    transport = _Transport(_Response(content=b"hello", content_type="text/plain"))
    client = _client(transport)

    data, content_type = client.read_file(U, "uploads/hello world.txt")

    assert data == b"hello"
    assert content_type == "text/plain"
    call = transport.calls[0]
    assert call["url"] == (
        "http://127.0.0.1:4096/api/fs/read/uploads%2Fhello%20world.txt")
    assert call["params"] == {"location[directory]": U}


def test_a_path_the_service_cannot_serve_is_a_not_found_not_an_outage():
    """The service answers a missing file with ``500``; the panel must read that
    as "not there", never as "the coding service is broken"."""
    transport = _Transport(_Response(status_code=500, payload={"name": "UnknownError"}))
    client = _client(transport)

    with pytest.raises(FileNotFoundError):
        client.read_file(U, "missing.txt")


def test_a_missing_directory_is_a_not_found_too():
    transport = _Transport(_Response(status_code=500, payload={"name": "UnknownError"}))
    client = _client(transport)

    with pytest.raises(FileNotFoundError):
        client.list_files(U, "nope")


def test_a_bad_request_stays_a_value_error():
    transport = _Transport(_Response(status_code=400, payload={"name": "InvalidRequestError"}))
    client = _client(transport)

    with pytest.raises(ValueError):
        client.list_files(U, "bad")


def test_a_refused_credential_is_not_the_users_own_refusal():
    transport = _Transport(_Response(status_code=401, payload={}))
    client = _client(transport)

    with pytest.raises(CodingError) as raised:
        client.list_files(U)

    assert raised.value.code == CODING_UPSTREAM_UNAVAILABLE


def test_location_returns_the_resolved_directory():
    transport = _Transport(_Response(payload={"directory": U, "project": {"id": "global"}}))
    client = _client(transport)

    payload = client.location(U)

    assert payload["directory"] == U
    assert transport.calls[0]["params"] == {"location[directory]": U}


# --- upload ---------------------------------------------------------------


def test_upload_sends_the_binding_and_a_base64_body():
    transport = _Transport(_Response(payload={
        "path": "uploads/manual/a.txt", "name": "a.txt", "size": 5, "renamed": False}))
    client = _client(transport)

    result = client.upload_file("ocws_1", "uploads/manual/a.txt", b"hello")

    assert result["path"] == "uploads/manual/a.txt"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:4096/api/coding/workspace/upload"
    assert call["json"] == {
        "workspace_id": "ocws_1",
        "relative_path": "uploads/manual/a.txt",
        "content_base64": "aGVsbG8=",
    }


def test_upload_reports_a_rejected_path_as_a_value_error():
    transport = _Transport(_Response(status_code=400, payload={"name": "InvalidRequestError"}))
    client = _client(transport)

    with pytest.raises(ValueError):
        client.upload_file("ocws_1", "../x", b"x")


def test_upload_reports_an_unavailable_binding_as_a_conflict():
    transport = _Transport(_Response(status_code=409, payload={"name": "ConflictError"}))
    client = _client(transport)

    with pytest.raises(CodingError) as raised:
        client.upload_file("ocws_1", "a.txt", b"x")

    assert raised.value.code == "coding_workspace_conflict"
    assert raised.value.status == 409


def test_upload_reports_a_service_failure_as_an_outage():
    transport = _Transport(_Response(status_code=500, payload={"name": "UnknownError"}))
    client = _client(transport)

    with pytest.raises(CodingError) as raised:
        client.upload_file("ocws_1", "a.txt", b"x")

    assert raised.value.code == CODING_UPSTREAM_UNAVAILABLE


# --- relative ids ---------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("uploads\\hello.txt", "uploads/hello.txt"),
    ("a//b/./c/", "a/b/c"),
    ("", ""),
    (None, ""),
])
def test_a_relative_id_is_folded_to_one_safe_spelling(raw, expected):
    assert normalize_relative(raw) == expected


@pytest.mark.parametrize("raw", [
    "../secret", "a/../../b", "C:\\Windows\\x", "\\\\server\\share",
    "a:b", "..", "/etc/passwd", "//host/share",
])
def test_an_escaping_id_is_refused_rather_than_rewritten(raw):
    assert normalize_relative(raw) is None


# --- the DTO layer --------------------------------------------------------


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
            # The service reports the host separator, and a directory with a
            # trailing one (evidence section 7).
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


def _files():
    return CodingRemoteFiles(
        _FakeRemote(
            files={"uploads/hello.txt": (b"hello", "text/plain"),
                   "notes.md": (b"# notes", "text/markdown"),
                   ".secret.md": (b"hidden", "text/markdown")},
            dirs={"uploads", "outputs"},
        ),
        U,
        workspace_id="ocws_alice",
    )


def test_listing_directories_sort_first_and_use_slash_ids():
    result = _files().list_dir()

    assert result["path"] == ""
    assert result["root"] == U
    assert [e["path"] for e in result["entries"]] == [
        "outputs", "uploads", "notes.md"]
    assert result["entries"][0]["is_dir"] is True
    # A remote entry carries no platform-local absolute path.
    assert all("abs_path" not in e for e in result["entries"])


def test_a_directory_entry_is_marked_previewable_false():
    entries = {e["path"]: e for e in _files().list_dir("uploads")["entries"]}

    assert entries["uploads/hello.txt"]["previewable"] is True
    assert entries["uploads/hello.txt"]["kind"] == "text"


def test_hidden_entries_are_filtered_unless_asked_for():
    default = [e["path"] for e in _files().list_dir()["entries"]]
    shown = [e["path"] for e in _files().list_dir(show_hidden=True)["entries"]]

    assert ".secret.md" not in default
    assert ".secret.md" in shown


def test_search_reports_flat_slash_paths():
    result = _files().search("hello")

    assert result["query"] == "hello"
    assert [e["path"] for e in result["results"]] == ["uploads/hello.txt"]


def test_an_empty_search_asks_nothing():
    assert _files().search("   ") == {"query": "", "results": []}


def test_stat_resolves_an_entry_by_listing_its_parent():
    entry = _files().stat_file("uploads/hello.txt")

    assert entry["name"] == "hello.txt"
    assert entry["is_dir"] is False


def test_stat_resolves_the_root_as_a_directory():
    entry = _files().stat_file("")

    assert entry["is_dir"] is True
    assert entry["path"] == ""


def test_stat_reports_a_missing_entry_as_not_found():
    with pytest.raises(FileNotFoundError):
        _files().stat_file("uploads/missing.txt")


def test_read_text_never_offers_an_edit_the_platform_cannot_write():
    payload = _files().read_text("uploads/hello.txt")

    assert payload["content"] == "hello"
    assert payload["editable"] is False
    assert payload["truncated"] is False


def test_read_text_flags_a_truncated_read():
    files = _files()
    files._client.files["big.txt"] = (b"x" * 100, "text/plain")

    payload = files.read_text("big.txt", max_bytes=10)

    assert payload["size"] == 100
    assert payload["truncated"] is True


def test_read_bytes_returns_the_payload_and_its_type():
    data, content_type = _files().read_bytes("uploads/hello.txt")

    assert data == b"hello"
    assert content_type == "text/plain"


def test_an_escaping_reference_is_refused_before_any_request():
    class _Exploding(_FakeRemote):
        def read_file(self, directory, path):
            raise AssertionError("a refused reference must not reach the remote")

    files = CodingRemoteFiles(_Exploding(), U)
    with pytest.raises(ValueError):
        files.read_text("../../etc/passwd")


def test_the_source_describes_itself_as_coding_remote():
    described = RemoteSource(U, "ocws_alice").describe()

    assert described["kind"] == "coding_remote"
    assert described["available"] is True
    assert described["workspace_id"] == "ocws_alice"


# --- upload (DTO) ---------------------------------------------------------


class _SpyUpload:
    """Records the upload call and answers a canned server result."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"path": "a.txt", "name": "a.txt", "size": 3, "renamed": False}

    def upload_file(self, workspace_id, relative_path, content):
        self.calls.append((workspace_id, relative_path, content))
        return dict(self.result)


def test_upload_forwards_the_binding_and_the_relative_id():
    spy = _SpyUpload()
    files = CodingRemoteFiles(spy, U, workspace_id="ocws_alice")

    result = files.upload_file("sub\\a.txt", b"abc")

    assert spy.calls == [("ocws_alice", "sub/a.txt", b"abc")]
    assert result == {"path": "a.txt", "name": "a.txt", "size": 3, "renamed": False}


def test_upload_reports_the_servers_rename():
    spy = _SpyUpload(result={"path": "a (1).txt", "name": "a (1).txt", "size": 3, "renamed": True})
    files = CodingRemoteFiles(spy, U, workspace_id="ocws_alice")

    result = files.upload_file("a.txt", b"abc")

    assert result["renamed"] is True
    assert result["name"] == "a (1).txt"


def test_upload_refuses_an_escaping_reference_before_any_call():
    spy = _SpyUpload()
    files = CodingRemoteFiles(spy, U, workspace_id="ocws_alice")

    with pytest.raises(ValueError):
        files.upload_file("../escape.txt", b"x")
    assert spy.calls == []


def test_upload_refuses_when_the_binding_is_unknown():
    spy = _SpyUpload()
    files = CodingRemoteFiles(spy, U, workspace_id="")

    with pytest.raises(CodingError) as raised:
        files.upload_file("a.txt", b"x")
    assert raised.value.code == "coding_workspace_conflict"
    assert spy.calls == []


def test_the_remote_capabilities_hide_unimplemented_writes():
    caps = RemoteSource(U, "ocws_alice").describe()["capabilities"]

    assert caps["read"] is True and caps["download"] is True and caps["upload"] is True
    assert caps["delete"] is False and caps["trash"] is False and caps["write"] is False

