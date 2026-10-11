"""Personal-turn attachments land in ``U`` before the message is sent.

change ``add-coding-agent-personal-workspace`` (task 3.3). The composer's
attachments -- file picker, drag, paste, folder drop -- must reach the account
directory *before* a message references them, and a message must never be
dispatched with an attachment the server cannot confirm. These tests pin the
three layers that make that true against a scripted remote:

* :mod:`agent.coding.files`' reference helpers (a stable single-segment id, an
  existence check, and the marker a turn carries);
* the handler helpers (:func:`channel.web.fork.handlers.workspace.coding_remote_upload_dir`
  and ``write_remote_attachment``), whose stable target makes a retry idempotent
  and refuses to overwrite an existing attachment with different bytes;
* ``WebChannel._personal_attachment_refs``, which validates every attachment of
  a personal turn against ``U`` and refuses the turn (so the console keeps its
  draft) instead of dropping a missing file.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

from agent.coding.files import CodingRemoteFiles, reference_line, safe_segment


U = "C:/projects/erp/user/alice"

#: A 1x1 red PNG, so the "bytes become an image part" path is exercised without
#: a fixture file on disk.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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
        return []

    def read_file(self, directory, path):
        if path not in self.files:
            raise FileNotFoundError(f"File not found: {path}")
        return self.files[path]

    def upload_file(self, workspace_id, relative_path, content):
        self.files[relative_path] = (bytes(content), "application/octet-stream")
        return {"path": relative_path, "name": relative_path.rsplit("/", 1)[-1],
                "size": len(content), "renamed": False}


def _files(**files):
    return CodingRemoteFiles(
        _FakeRemote(files=files, dirs=set()),
        U,
        workspace_id="ocws_alice",
    )


# --- the stable attachment id ---------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("att_abc123", "att_abc123"),
    ("dir_1-2_3", "dir_1-2_3"),
])
def test_a_safe_segment_is_kept(raw, expected):
    assert safe_segment(raw) == expected


@pytest.mark.parametrize("raw", [
    "", None, "a/b", "a\\b", "..", "a.b", "a b", "att" * 40,
])
def test_an_unsafe_segment_is_refused_not_rewritten(raw):
    assert safe_segment(raw) is None


# --- the turn marker -------------------------------------------------------


def test_a_reference_line_names_the_relative_id():
    line = reference_line("uploads/att_1/note.txt")

    assert "uploads/att_1/note.txt" in line


def test_a_directory_reference_says_it_is_a_directory():
    assert reference_line("uploads", is_dir=True) != reference_line("uploads")


def test_an_escaping_reference_becomes_a_refusal_not_a_drop():
    line = reference_line("../secret")

    assert "../secret" not in line
    assert "未生效" in line


# --- existence and reference on the DTO layer ------------------------------


def test_reference_verifies_the_file_then_describes_it():
    files = _files(**{"uploads/att_1/note.txt": (b"hi", "text/plain")})

    ref = files.reference("uploads/att_1/note.txt")

    assert ref["relative"] == "uploads/att_1/note.txt"
    assert ref["is_dir"] is False
    assert "uploads/att_1/note.txt" in ref["line"]


def test_reference_reports_a_missing_entry_as_not_found():
    files = _files()

    with pytest.raises(FileNotFoundError):
        files.reference("uploads/att_1/missing.txt")


def test_exists_is_false_for_an_escaping_id():
    files = _files()

    assert files.exists("../secret") is False


# --- the handler helpers ---------------------------------------------------


def test_the_upload_directory_is_scoped_per_session_and_attachment():
    from channel.web.fork.handlers.workspace import coding_remote_upload_dir

    assert coding_remote_upload_dir("sess-1", "att_9") == "uploads/sess-1/att_9"
    # No session still gets a landing area, and an unsafe id is dropped rather
    # than silently mangled into a different name.
    assert coding_remote_upload_dir("", "att_9") == "uploads/manual/att_9"
    assert coding_remote_upload_dir("sess-1", "../x") == "uploads/sess-1"


# --- the default output folder (task 3.4) ----------------------------------


def test_the_output_folder_is_grouped_by_session_and_by_run():
    from agent.coding.files import output_relative_dir

    first = output_relative_dir("sess-1", "req-9")
    second = output_relative_dir("sess-2", "req-9")
    later = output_relative_dir("sess-1", "req-10")

    assert first == "outputs/sess-1/req-9"
    # Two sessions generating the same-named report do not share a folder, and
    # neither do two runs of one session.
    assert len({first, second, later}) == 3


def test_the_output_folder_degrades_one_level_at_a_time():
    from agent.coding.files import output_relative_dir

    # No turn id yet: the session's folder, not a made-up identifier.
    assert output_relative_dir("sess-1") == "outputs/sess-1"
    # No session: the manual area, exactly like an upload with no session.
    assert output_relative_dir("", "req-9") == "outputs/manual/req-9"
    assert output_relative_dir(None, None) == "outputs/manual"


def test_an_unsafe_output_id_is_dropped_not_rewritten():
    from agent.coding.files import output_relative_dir

    assert output_relative_dir("sess-1", "../escape") == "outputs/sess-1"
    assert output_relative_dir("a/b", "req") == "outputs/manual/req"


def test_the_output_marker_names_the_relative_folder():
    from agent.coding.files import output_line

    line = output_line("sess-1", "req-9")

    assert line.startswith("[") and line.endswith("]")
    assert "outputs/sess-1/req-9" in line
    # A relative id inside `U`, never a platform-local path.
    assert "C:" not in line and U not in line


def test_the_handler_helper_and_the_source_agree():
    from agent.coding.files import output_relative_dir
    from channel.web.fork.handlers.workspace import coding_remote_output_dir

    for session, turn in (("sess-1", "req-9"), ("", ""), ("sess-1", None)):
        assert coding_remote_output_dir(session, turn) == output_relative_dir(session, turn)


def test_write_remote_attachment_stores_a_new_file():
    from channel.web.fork.handlers.workspace import write_remote_attachment

    files = _files()
    written = write_remote_attachment(files, "uploads/sess/att_1/note.txt", b"hello")

    assert written["path"] == "uploads/sess/att_1/note.txt"
    assert files._client.files["uploads/sess/att_1/note.txt"][0] == b"hello"


def test_write_remote_attachment_reuses_identical_bytes_on_retry():
    from channel.web.fork.handlers.workspace import write_remote_attachment

    files = _files(**{"uploads/sess/att_1/note.txt": (b"hello", "text/plain")})
    written = write_remote_attachment(files, "uploads/sess/att_1/note.txt", b"hello")

    assert written["path"] == "uploads/sess/att_1/note.txt"
    assert written["size"] == 5


def test_write_remote_attachment_refuses_to_overwrite_with_different_bytes():
    from channel.web.fork.handlers.workspace import (
        WS_ATTACHMENT_CONFLICT,
        WorkspaceUploadError,
        write_remote_attachment,
    )

    files = _files(**{"uploads/sess/att_1/note.txt": (b"hello", "text/plain")})

    with pytest.raises(WorkspaceUploadError) as raised:
        write_remote_attachment(files, "uploads/sess/att_1/note.txt", b"DIFFERENT")

    assert raised.value.code == WS_ATTACHMENT_CONFLICT
    # The original is untouched.
    assert files._client.files["uploads/sess/att_1/note.txt"][0] == b"hello"


def test_write_remote_attachment_refuses_an_escaping_target():
    from channel.web.fork.handlers.workspace import (
        WorkspaceUploadError,
        write_remote_attachment,
    )

    with pytest.raises(WorkspaceUploadError):
        write_remote_attachment(_files(), "../escape.txt", b"x")


# --- the personal-turn validation -----------------------------------------


class _RefFiles:
    """A minimal ``files`` stand-in for ``_personal_attachment_refs``."""

    def __init__(self, present=()):
        self.present = dict(present)

    def reference(self, rel):
        if rel not in self.present:
            raise FileNotFoundError(rel)
        data = self.present[rel]
        is_dir = data is None
        return {"relative": rel, "is_dir": is_dir,
                "line": reference_line(rel, is_dir=is_dir)}

    def read_bytes(self, rel):
        return self.present[rel], "image/png"


def _validate(attachments, present):
    from channel.web.fork.runtime import WebChannel

    # ``WebChannel`` is wrapped by ``@singleton``; the undecorated class is what
    # carries the validation seam, and calling it unbound keeps the test off the
    # channel's (config-reading) constructor.
    remote = (_RefFiles(present), object())
    return WebChannel.__wrapped__._personal_attachment_refs(
        WebChannel.__wrapped__, remote, attachments)


def test_a_stored_attachment_becomes_a_relative_marker():
    error, refs, context = _validate(
        [{"file_path": "uploads/sess/att_1/note.txt", "file_type": "file"}],
        {"uploads/sess/att_1/note.txt": b"hi"},
    )

    assert error is None
    assert len(refs) == 1
    assert "uploads/sess/att_1/note.txt" in refs[0]
    assert context == []


def test_a_missing_attachment_refuses_the_turn():
    error, refs, context = _validate(
        [{"file_path": "uploads/sess/att_1/missing.txt", "file_type": "file"}],
        {},
    )

    body = json.loads(error)
    assert body["status"] == "error"
    assert body["code"] == "attachment_missing"
    assert refs == [] and context == []


def test_an_absolute_path_is_refused_not_treated_as_an_id():
    error, refs, _context = _validate(
        [{"file_path": "C:/Windows/system32/evil.txt", "file_type": "file"}],
        {"C:/Windows/system32/evil.txt": b"evil"},
    )

    body = json.loads(error)
    assert body["code"] == "invalid_reference"
    assert refs == []


def test_a_remote_image_is_read_back_from_the_stored_file():
    error, refs, context = _validate(
        [{"file_path": "uploads/sess/att_1/pic.png", "file_type": "image"}],
        {"uploads/sess/att_1/pic.png": b"PNGDATA"},
    )

    assert error is None
    assert context == [{
        "path": "uploads/sess/att_1/pic.png",
        "file_type": "image",
        "name": "pic.png",
        "data": b"PNGDATA",
    }]


# --- bytes become an image part -------------------------------------------


def test_image_bytes_build_a_part_without_a_local_file():
    from agent.attachments import AttachmentPolicy, build_image_parts

    parts, notices = build_image_parts(
        [{"path": "uploads/sess/att_1/pic.png", "name": "pic.png", "data": _PNG_BYTES}],
        AttachmentPolicy(),
    )

    assert notices == []
    assert len(parts) == 1
    assert parts[0].media_type == "image/png"
    assert parts[0].data


def test_undecodable_bytes_are_reported_not_dropped_silently():
    from agent.attachments import AttachmentPolicy, build_image_parts

    parts, notices = build_image_parts(
        [{"path": "uploads/sess/att_1/x.png", "name": "x.png", "data": b"not an image"}],
        AttachmentPolicy(),
    )

    assert parts == []
    assert len(notices) == 1


# --- the upload route ------------------------------------------------------


class _Part:
    """One multipart file part, in the two attributes the route reads."""

    def __init__(self, filename, content):
        self.filename = filename
        self.file = io.BytesIO(content)


def _upload(params, files=None):
    from channel.web.fork.runtime import WebChannel

    from agent.coding.files import RemoteSource

    # ``_upload_coding_remote`` is deliberately self-free -- everything it needs
    # (the binding, the session) arrives as arguments -- so the route can be
    # exercised without a running channel.
    return WebChannel.__wrapped__._upload_coding_remote(
        None, (files or _files(), RemoteSource(U, "ocws_alice")),
        params, "agent-1", "sess-1")


def test_a_composer_file_lands_in_the_account_directory():
    files = _files()
    payload = json.loads(_upload({
        "file": [_Part("note.txt", b"hello")],
        "attachment_id": ["att_1"],
    }, files))

    assert payload["status"] == "success"
    assert payload["remote"] is True
    assert payload["file_path"] == "uploads/sess-1/att_1/note.txt"
    assert files._client.files["uploads/sess-1/att_1/note.txt"][0] == b"hello"


def test_the_upload_answer_carries_the_owning_source():
    payload = json.loads(_upload({"file": [_Part("note.txt", b"hello")]}))

    assert payload["source"]["kind"] == "coding_remote"
    assert payload["source"]["workspace_id"] == "ocws_alice"


def test_an_image_thumbnail_is_a_relaying_download_url_not_a_local_path():
    payload = json.loads(_upload({
        "file": [_Part("pic.png", _PNG_BYTES)],
        "attachment_id": ["att_2"],
    }))

    assert payload["file_type"] == "image"
    assert "/api/workspace/download?" in payload["preview_url"]
    assert "path=uploads%2Fsess-1%2Fatt_2%2Fpic.png" in payload["preview_url"]
    # Never a platform-local path, which the browser could not resolve anyway.
    assert "C:" not in payload["preview_url"]


def test_a_file_with_no_session_still_has_a_landing_area():
    payload = json.loads(_upload({"file": [_Part("note.txt", b"hi")]}))

    assert payload["file_path"] == "uploads/sess-1/note.txt"


def test_a_retry_of_the_same_bytes_does_not_write_a_second_copy():
    files = _files()
    request = lambda: {"file": [_Part("note.txt", b"hello")],  # noqa: E731
                       "attachment_id": ["att_1"]}

    first = json.loads(_upload(request(), files))
    second = json.loads(_upload(request(), files))

    assert first["file_path"] == second["file_path"]
    assert list(files._client.files) == ["uploads/sess-1/att_1/note.txt"]


def test_a_retry_with_different_bytes_is_refused_as_a_conflict():
    files = _files()
    _upload({"file": [_Part("note.txt", b"hello")], "attachment_id": ["att_1"]}, files)

    payload = json.loads(_upload(
        {"file": [_Part("note.txt", b"TAMPERED")], "attachment_id": ["att_1"]}, files))

    assert payload["status"] == "error"
    assert payload["code"] == "attachment_conflict"
    assert files._client.files["uploads/sess-1/att_1/note.txt"][0] == b"hello"


def test_an_oversized_attachment_is_refused_with_the_panel_code():
    from channel.web.fork.handlers.workspace import WS_UPLOAD_MAX_BYTES

    payload = json.loads(_upload({
        "file": [_Part("big.bin", b"x" * (WS_UPLOAD_MAX_BYTES + 1))],
    }))

    assert payload["status"] == "error"
    assert payload["code"] == "too_large"


def test_an_escaping_file_name_is_contained_not_forwarded():
    files = _files()
    payload = json.loads(_upload({
        "file": [_Part(r"..\..\evil.txt", b"x")],
        "attachment_id": ["att_3"],
    }, files))

    assert payload["status"] == "success"
    assert payload["file_path"] == "uploads/sess-1/att_3/evil.txt"
    assert all(not rel.startswith("..") for rel in files._client.files)


def test_a_folder_drop_reaches_the_remote_below_the_stable_id():
    files = _files()
    payload = json.loads(_upload({
        "files": [_Part("a.txt", b"a"), _Part("b.txt", b"b")],
        "relative_paths": ["pkg/a.txt", "pkg/b.txt"],
        "upload_id": ["up1"],
        "attachment_id": ["att_4"],
    }, files))

    assert payload["upload_type"] == "directory"
    assert payload["root_name"] == "pkg"
    assert payload["file_count"] == 2
    assert set(files._client.files) == {
        "uploads/sess-1/att_4/webdir_up1/pkg/a.txt",
        "uploads/sess-1/att_4/webdir_up1/pkg/b.txt",
    }


def test_a_request_with_no_file_is_an_error_not_a_silent_success():
    payload = json.loads(_upload({}))

    assert payload["status"] == "error"
