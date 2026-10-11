"""The read view of a personal coding workspace, addressed by relative id.

change ``add-coding-agent-personal-workspace`` (task 3.1). A coding Agent's
personal files live in ``U = P/user/<account>`` *on the OpenCode host*, not on
the platform. The console file panel therefore may not be served by the local
:class:`~agent.workspace.service.WorkspaceService`, which would answer from the
platform's own disk and show a tree the session's tools never touch.

This module adapts the remote filesystem routes
(:meth:`agent.coding.opencode.OpenCodeClient.list_files`, ``find_files``,
``read_file``) to the *same* response shapes the local service uses, so the
panel contract is unchanged and only the origin differs. It also owns the two
rules that make that adaptation safe:

* **the location is always the verified ``U``** -- no client request ever
  supplies a directory or an account, and every call pins the location to the
  binding this object was built for;
* **a reference is one relative id inside ``U``** -- normalized like the local
  service's ``join_rel`` and refused on anything that could escape (an absolute
  path, a drive/UNC prefix, ``..``), rather than being forwarded to the host's
  path rules.

Deliberately absent: any write, delete, move or recycle-bin operation. The
remote slice implemented here is read-only; the panel disables the rest by
capabilities rather than pretending they work.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional

from agent.protocol.artifact import classify_kind, is_previewable
from agent.coding import CODING_WORKSPACE_CONFLICT, CodingError
from agent.workspace.service import MAX_ENTRIES, MAX_TEXT_BYTES, _decode_utf8

#: The panel origin for a personal coding session's files. It is the value the
#: binding descriptor already publishes (``CodingWorkspaceService.describe``),
#: so the client sees one name for the one thing.
KIND = "coding_remote"

#: The text marker an attachment or `@` reference becomes in a personal turn.
#: It names the file by its *relative id inside the account directory*, which is
#: the working directory of the session's own tools -- unlike the server/desktop
#: labels, it is not a path this platform may open.
FILE_LABEL = ("个人目录文件", "Personal directory file")
DIR_LABEL = ("个人目录目录", "Personal directory directory")
REFUSED_LABEL = ("个人目录引用未生效", "Personal directory reference not applied")

#: The text marker that names this turn's default delivery folder (task 3.4).
OUTPUT_LABEL = ("个人目录输出位置", "Personal directory output location")

#: Where the system's own generated files belong inside ``U``, beside the
#: ``uploads/`` area the composer writes to (design section 4).
OUTPUT_ROOT = "outputs"

#: What the remote source can actually do. The read slice is complete; the
#: write-side operations the design lists as optional (edit, rename, delete,
#: move, recycle bin) have no compatible remote implementation yet, so they are
#: advertised as absent and the panel hides them rather than offering a button
#: the backend would refuse. Upload is required and is present.
REMOTE_CAPABILITIES = {
    "read": True,
    "download": True,
    "upload": True,
    "write": False,
    "rename": False,
    "move": False,
    "delete": False,
    "trash": False,
}

#: A *legacy* session's panel (design section 6): a coding conversation created
#: before the personal capability, whose execution directory is still the project
#: root ``P``. Its file button opens the caller's account directory ``U`` so the
#: personal files are reachable, but the running session's tools work in ``P``,
#: so an upload here could never be found by them. The panel is therefore
#: read/download only and carries the legacy marker for the notice.
LEGACY_CAPABILITIES = {**REMOTE_CAPABILITIES, "upload": False}


def normalize_relative(raw: Any) -> Optional[str]:
    """One safe relative id inside ``U``, or ``None`` when it cannot be one.

    Windows- and POSIX-shaped inputs are folded to ``a/b/c``: separators
    normalized, ``.`` segments dropped, a trailing separator trimmed so the
    location root is spelled ``""``. An absolute path (a leading separator or a
    UNC prefix), a drive letter (any ``:``) and a ``..`` segment are refused --
    not sanitized -- because sanitizing would let a caller reach a different
    file than the one they named.

    The host's own path rules are still the backstop: the service refuses an
    escape as well, and the client maps that refusal to a not-found. This is the
    *front* door, so a malformed reference is answered cleanly instead of
    costing a round trip.
    """
    text = "" if raw is None else str(raw)
    text = text.replace("\\", "/").strip()
    if text.startswith("/"):
        return None
    if ":" in text:
        return None
    parts: List[str] = []
    for segment in text.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            return None
        parts.append(segment)
    return "/".join(parts)


def safe_segment(raw: Any, *, limit: int = 64) -> Optional[str]:
    """One safe single path segment (an attachment id), or ``None``.

    Used for the stable attachment identifier that makes an upload retry land on
    the *same* stored name: only ``[A-Za-z0-9_-]`` survives, separators and
    dots are refused outright (never silently stripped into a different name),
    and the result is length-bounded.
    """
    text = "" if raw is None else str(raw).strip()
    if not text or len(text) > limit:
        return None
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in text):
        return None
    return text


def output_relative_dir(session_id: Any, token: Any = None) -> str:
    """``outputs/<session>[/<token>]`` -- this turn's default delivery folder.

    Two sessions generating the same-named file must not overwrite each other,
    so the system's own outputs are grouped by session; a second segment carries
    the piece of work's existing identifier (the message/execution id the
    platform already has for the turn) so two turns of one session stay apart
    too. A session that has no id yet, or no turn id, falls back to the coarser
    folder rather than inventing an identifier -- a made-up one would be a name
    the console could not reproduce on the next turn.

    Ids are folded through :func:`safe_segment`, so a caller cannot smuggle a
    path shape (or a second segment) into the location. The *platform* only
    names this folder: it has no remote mkdir route, and the session's own tools
    create it when they write, which is why nothing here touches the disk.
    """
    sid = safe_segment(session_id, limit=80)
    base = f"{OUTPUT_ROOT}/{sid}" if sid else f"{OUTPUT_ROOT}/manual"
    turn = safe_segment(token, limit=80)
    if turn:
        base = f"{base}/{turn}"
    return base


def output_line(session_id: Any, token: Any = None) -> str:
    """The marker a personal turn carries to name where its outputs belong.

    It is a relative id inside ``U`` -- the working directory of the session's
    own tools -- so the model can write there and the panel can list the same
    folder, with no index or sync service in between.
    """
    from common import i18n

    rel = output_relative_dir(session_id, token)
    return f"[{i18n.t(*OUTPUT_LABEL)}: {rel}]"


def reference_line(relative: Any, *, is_dir: bool = False) -> str:
    """The text marker one personal-directory reference becomes in a turn.

    The marker names the file by its relative id inside ``U`` -- the session's
    own working directory -- so the coding agent opens the stored original with
    its own tools. A reference that is not a safe relative id is reported as
    refused rather than dropped, so the model is never left guessing.
    """
    from common import i18n

    rel = normalize_relative(relative)
    if rel is None:
        return ("[%s: 引用超出个人目录范围，未生效]"
                % i18n.t(*REFUSED_LABEL))
    label = i18n.t(*(DIR_LABEL if is_dir else FILE_LABEL))
    return f"[{label}: {rel}]"


class RemoteSource:
    """The file panel's *coding_remote* origin, in the shape the panel expects.

    ``root`` is the remote ``U`` (already surfaced to the client by the session
    binding), and it is never used to build a *platform-local* path: the DTOs
    below carry relative ids only. The panel's client distinguishes it from the
    server root by ``kind`` (``describe``), exactly like a desktop source.
    """

    kind = KIND
    is_desktop = False
    is_remote = True
    available = True
    refusal = None

    def __init__(self, directory: str, workspace_id: str = "", *, legacy: bool = False):
        self.directory = directory
        self.workspace_id = workspace_id
        #: True for an old session's panel: it opens ``U`` but the session itself
        #: still runs in ``P``, so the panel says so and offers no upload.
        self.legacy = bool(legacy)

    @property
    def root(self) -> str:
        """The remote working directory, for callers that read ``source.root``.

        It is a path on the *OpenCode host*; nothing in the platform may open
        it locally. Kept as an attribute only so shared describers do not have
        to special-case a remote source.
        """
        return self.directory

    def describe(self) -> Dict[str, Any]:
        label = self.directory.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        described = {
            "kind": self.kind,
            "available": True,
            "refusal": None,
            "workspace_id": self.workspace_id,
            "label": label or self.directory,
            "capabilities": dict(
                LEGACY_CAPABILITIES if self.legacy else REMOTE_CAPABILITIES),
        }
        if self.legacy:
            # The client keys its notice off this flag; the sentence itself is a
            # translation, so it lives with the other panel strings, not here.
            described["legacy"] = True
        return described


class CodingRemoteFiles:
    """Relative-id read access to one personal coding directory.

    Built for exactly one verified binding: ``directory`` is ``U`` and the
    client pins every request to it. The method names and return shapes mirror
    :class:`~agent.workspace.service.WorkspaceService`'s read half so the web
    handlers can dispatch on the source without rewriting their contract.
    """

    def __init__(self, client: Any, directory: str, *, workspace_id: str = ""):
        self._client = client
        self.directory = directory
        self.workspace_id = workspace_id

    # -- containment -------------------------------------------------------

    @staticmethod
    def relative(raw: Any) -> str:
        """The relative id for a request, raising ``ValueError`` on an escape."""
        rel = normalize_relative(raw)
        if rel is None:
            raise ValueError("the path escapes the personal directory")
        return rel

    def _entry(self, row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """One remote ``{path, type}`` row as a panel entry, or ``None``.

        The service reports no size or mtime, so both are zero here: the panel
        shows a name and can open or download it, and does not pretend to know
        a size it never measured. ``kind`` is classified from the name, which is
        how a file's preview affordance is decided on either source.
        """
        path = normalize_relative(row.get("path"))
        if not path:
            return None
        is_dir = str(row.get("type") or "") == "directory"
        name = path.rsplit("/", 1)[-1]
        kind = "directory" if is_dir else classify_kind(name)
        return {
            "name": name,
            "path": path,
            "is_dir": is_dir,
            "kind": kind,
            "previewable": (not is_dir) and is_previewable(kind),
            "size": 0,
            "mtime": 0.0,
        }

    # -- reads -------------------------------------------------------------

    def list_dir(
        self,
        rel_path: str = "",
        show_hidden: bool = False,
        allow_entry: Any = None,
    ) -> Dict[str, Any]:
        """Direct children of one directory inside ``U``."""
        rel = self.relative(rel_path)
        rows = self._client.list_files(self.directory, rel)
        entries: List[Dict[str, Any]] = []
        truncated = False
        for row in rows:
            entry = self._entry(row)
            if entry is None:
                continue
            if not show_hidden and entry["name"].startswith("."):
                continue
            if allow_entry is not None and not allow_entry(entry["path"]):
                continue
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            entries.append(entry)
        dirs = sorted((e for e in entries if e["is_dir"]),
                      key=lambda e: e["name"].lower())
        files = sorted((e for e in entries if not e["is_dir"]),
                       key=lambda e: e["name"].lower())
        return {
            "path": rel,
            "root": self.directory,
            "entries": dirs + files,
            "truncated": truncated,
        }

    def search(self, query: str, limit: int = 30, allow_dir: Any = None) -> Dict[str, Any]:
        """Name search below ``U``, bounded like the local service.

        ``allow_dir`` is accepted for signature parity but is not consulted:
        the remote ``find`` has no per-directory prune hook, and the *only*
        root searched is the caller's own ``U``, so there is no other member's
        subtree to prune away.
        """
        text = str(query or "").strip()
        if not text:
            return {"query": "", "results": []}
        try:
            bound = max(1, min(int(limit), MAX_ENTRIES))
        except (TypeError, ValueError):
            bound = 30
        rows = self._client.find_files(self.directory, text, limit=bound)
        results: List[Dict[str, Any]] = []
        for row in rows:
            entry = self._entry(row)
            if entry is None:
                continue
            results.append(entry)
            if len(results) >= bound:
                break
        return {"query": text, "results": results}

    def stat_file(self, rel_path: str) -> Dict[str, Any]:
        """One entry's metadata, resolving a directory as well as a file.

        There is no remote stat route, so an entry is resolved by listing its
        parent and matching the name -- the same information the tree already
        showed, which keeps "open this path" consistent with "browse to it".
        """
        rel = self.relative(rel_path)
        if not rel:
            name = self.directory.rstrip("/").rsplit("/", 1)[-1] or self.directory
            return {
                "name": name,
                "path": "",
                "is_dir": True,
                "kind": "directory",
                "previewable": False,
                "size": 0,
                "mtime": 0.0,
            }
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for entry in self.list_dir(parent)["entries"]:
            if entry["path"] == rel:
                return entry
        raise FileNotFoundError(f"File not found: {rel}")

    def read_text(self, rel_path: str, max_bytes: int = MAX_TEXT_BYTES) -> Dict[str, Any]:
        """One file as text, with the same fields the local editor expects.

        ``editable`` is always ``False``: this slice reads only, so the editor
        must never offer to save something the platform cannot write back.
        """
        rel = self.relative(rel_path)
        if not rel:
            raise ValueError("path is required")
        raw, _ = self._client.read_file(self.directory, rel)
        size = len(raw)
        try:
            bound = max(1, min(int(max_bytes or MAX_TEXT_BYTES), MAX_TEXT_BYTES))
        except (TypeError, ValueError):
            bound = MAX_TEXT_BYTES
        body = raw[:bound]
        truncated = size > len(body)
        content, lossy = _decode_utf8(body)
        return {
            "path": rel,
            "content": content,
            "truncated": truncated,
            "lossy": lossy,
            "size": size,
            "mtime": 0.0,
            "editable": False,
        }

    def read_bytes(self, rel_path: str):
        """Raw bytes and content type of one file, for a download."""
        rel = self.relative(rel_path)
        if not rel:
            raise ValueError("path is required")
        return self._client.read_file(self.directory, rel)

    def reference(self, rel_path: str) -> Dict[str, Any]:
        """Verify one stored reference and describe how a turn should name it.

        Used when a personal turn carries an attachment or `@` reference: the
        file must already exist in the account directory (an upload lands it
        before the message is sent), so a reference that cannot be found is
        reported rather than silently dropped -- and it is *never* re-copied.
        Returns ``{relative, is_dir, line}``; raises ``ValueError`` for a path
        that is not a safe relative id and ``FileNotFoundError`` when the entry
        is not there.
        """
        rel = self.relative(rel_path)
        if not rel:
            raise ValueError("path is required")
        entry = self.stat_file(rel)
        return {
            "relative": rel,
            "is_dir": bool(entry.get("is_dir")),
            "line": reference_line(rel, is_dir=bool(entry.get("is_dir"))),
        }

    def exists(self, rel_path: str) -> bool:
        """Whether an entry with this relative id is present in ``U``."""
        rel = normalize_relative(rel_path)
        if not rel:
            return False
        try:
            self.stat_file(rel)
            return True
        except FileNotFoundError:
            return False

    def upload_file(self, rel_path: str, data: bytes) -> Dict[str, Any]:
        """Store one file inside ``U`` and report where it landed.

        The binding id is what the server needs to find the directory, so a
        source with no ``workspace_id`` refuses rather than guessing one. The
        stored name is the server's (it renames on collision), and it is folded
        back through :func:`normalize_relative` so a caller cannot be handed a
        path shape the rest of the panel does not use.
        """
        rel = self.relative(rel_path)
        if not rel:
            raise ValueError("path is required")
        if not self.workspace_id:
            raise CodingError(
                CODING_WORKSPACE_CONFLICT,
                "the personal directory binding is unknown",
                409,
            )
        result = self._client.upload_file(self.workspace_id, rel, data)
        stored = normalize_relative(result.get("path")) or rel
        return {
            "path": stored,
            "name": str(result.get("name") or stored.rsplit("/", 1)[-1]),
            "size": int(result.get("size") or len(data)),
            "renamed": bool(result.get("renamed")),
        }

    def meta(self) -> Dict[str, Any]:
        return {
            "root": self.directory,
            "exists": True,
            "server_time": time.time(),
        }
