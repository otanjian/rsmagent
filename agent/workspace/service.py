"""
Workspace file service - browse, search and edit the agent workspace.

Backs the file manager tab, the preview panel editor and the `@` file
reference picker in the web UI.

Every path goes through :meth:`WorkspaceService.resolve`, which rejects
anything that escapes the workspace root after `..` and symlinks are resolved.

Only :meth:`write_text` mutates anything, and only for a file that already
exists and holds plain text. It is reachable from the web console but *not*
from :meth:`dispatch`, so remote transports keep the read-only surface.
"""

import base64
import json
import mimetypes
import os
import tempfile
import time
from typing import Dict, List, Optional

from common import safe_fs
from common.log import logger

from agent.protocol.artifact import classify_kind, is_editable, is_previewable

# Directories that are large, noisy, or purely internal. Still listable when the
# user explicitly navigates into them, but skipped by recursive search.
SEARCH_SKIP_DIRS = {"tmp", "node_modules", "__pycache__", "venv", ".git", ".venv"}

# Agent bookkeeping. Reachable by search, but ranked below user-facing files so
# they don't crowd out real results in the `@` picker.
SEARCH_DEMOTE_DIRS = {"memory", "skills", "knowledge", "scheduler", "plans"}
SEARCH_DEMOTE_PENALTY = 25

MAX_ENTRIES = 500
MAX_SEARCH_WALK = 20000

# Largest text body returned by `read` in one response; longer files are cut
# short and flagged as truncated rather than refused.
MAX_TEXT_BYTES = 1024 * 1024

# `file` transfers bytes in chunks because a remote caller may sit behind a
# message transport with a per-message size limit. 768 KiB of raw bytes is
# ~1 MiB once base64-encoded, which leaves ample headroom.
DEFAULT_CHUNK_BYTES = 768 * 1024
MAX_CHUNK_BYTES = 2 * 1024 * 1024

# Refuse to serve anything larger; well above what a browser preview needs.
MAX_FILE_BYTES = 64 * 1024 * 1024

# mtime is compared as a float that has been through JSON on both sides, so
# allow for the last bit of the timestamp rather than requiring bit equality.
MTIME_EPSILON = 1e-6

#: Name of the per-user recycle bin. It lives inside the user's own directory
#: (``user/<uid>/.trash``) so it inherits the ordinary ownership rule instead of
#: needing a second authorization story. Being a dotfile is *not* what hides it
#: — the listing route exposes a ``show_hidden`` toggle — so both the listing and
#: the search seam exclude this name explicitly.
TRASH_DIR_NAME = ".trash"

#: Directory inside a batch holding the payload: one entry per item, named by
#: the item's slot in the batch (see :meth:`WorkspaceService._payload_rel`).
TRASH_FILES_DIR = "files"

#: Bookkeeping for one delete request, stored beside the payload.
TRASH_META_NAME = "batch.json"

#: How long a batch stays in the bin before the cleanup point collects it. An
#: implementation constant rather than a setting: this is an operations number,
#: and making it configurable would add a read path that needs its own
#: registration and verification for no product behaviour.
TRASH_RETENTION_SECONDS = 30 * 24 * 60 * 60

#: Items at the **workspace root** that belong to the Agent rather than to the
#: user: its persona, its memory, its knowledge base and scheduler, and the rest
#: of the machinery described by ``agent/prompt/workspace.py``. Deleting them
#: does not "clean up a folder", it breaks the Agent, and nothing in a filename
#: distinguishes "the user tidying their files" from "the user deleted their
#: Agent's memory".
#:
#: Matching is by the path's **first component only** (see
#: :func:`undeletable_reason`), so it never re-applies at a deeper level: a
#: ``memory/`` inside the user's own directory is the user's own content and
#: stays deletable. That also keeps the rule explainable in one sentence.
UNDELETABLE_ROOT_ENTRIES = frozenset({
    "AGENT.md", "USER.md", "RULE.md", "BOOTSTRAP.md", "MEMORY.md",
    "memory", "scheduler", "tmp", "knowledge", "skills", "websites",
    "subagents", "system", "plans",
})

#: The per-user container at the workspace root. Its children are members'
#: private subtrees, so the container and each member's own directory are
#: handled by :func:`undeletable_reason` rather than by the set above.
USER_CONTAINER_NAME = "user"


class WorkspaceConflictError(Exception):
    """The file changed on disk since the caller read it."""


def _decode_utf8(raw: bytes):
    """
    Decode as UTF-8, reporting whether anything had to be replaced.

    A file that isn't valid UTF-8 - a legacy GBK or Latin-1 document, or a
    binary that slipped past the extension check - still has to preview, but an
    editor must not offer to save it: the round-trip would write a replacement
    character over every byte that failed to decode.

    :return: ``(text, lossy)``
    """
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), True


def join_rel(dir_rel: str, rel: str) -> str:
    """Join a directory and a client-supplied relative path; validate the result.

    The single place the two are combined, so both the escape rules (``..``,
    drive letter, empty component) and the "still under the directory" rule are
    enforced by :func:`common.safe_fs.split_relative` rather than re-implemented
    per caller.

    The two arguments are treated differently on purpose:

    * ``dir_rel`` is a position the console already listed, so ``/`` and ``""``
      both mean "the scope root" and a leading or trailing slash is just sloppy
      input. It is still traversal-checked, so ``..`` cannot climb out.
    * ``rel`` is the file being addressed, and an **absolute** value is refused
      outright rather than trimmed. Quietly reading ``/etc/passwd`` as
      ``etc/passwd`` is how an operator's mistake turns into a surprise write
      somewhere in the workspace; a refusal is unambiguous.
    """
    left = (dir_rel or "").replace("\\", "/").strip("/")
    right = (rel or "").replace("\\", "/")
    if right.startswith("/"):
        raise safe_fs.UnsafePathError("absolute path is not allowed")
    right = right.rstrip("/")
    combined = "/".join(part for part in (left, right) if part)
    safe_fs.split_relative(combined)
    return combined


def _is_trash_root(rel: str) -> bool:
    """True for ``<...>/user/<uid>/.trash`` -- the bin, whatever its prefix.

    Matched on the **last three components** rather than an absolute depth,
    because ``WorkspaceService.root`` is whatever root the file API serves and
    that is not always the Agent's own workspace: in database mode it is the
    tenant's shared root, where the same bin is addressed as
    ``agents/<id>/user/<uid>/.trash``. Matching a fixed depth silently stopped
    hiding the bin there as soon as ``show_hidden=1`` was passed.

    Deliberately narrow otherwise. A bare "hide anything called ``.trash``"
    rule would also hide a user's *own* folder that happens to be named that
    deeper inside their tree, which is their content and none of the console's
    business. The bin only ever exists directly under a ``user/<uid>``, so
    requiring that parent is both sufficient and honest.
    """
    parts = (rel or "").replace("\\", "/").strip("/").split("/")
    return (len(parts) >= 3 and parts[-3] == USER_CONTAINER_NAME
            and parts[-1] == TRASH_DIR_NAME)


def undeletable_reason(rel: str) -> Optional[str]:
    """Why ``rel`` must not be deleted, or ``None`` when it may be.

    Deliberately a small closed set of reasons rather than a boolean, so the
    response can tell the user *which* rule stopped them ("this is the Agent's
    memory") instead of a bare refusal.

    The rules are the four classes from the ``platform-file-browsing`` spec:

    * ``agent_internal`` -- the path's first component is one of the Agent's own
      entries at the workspace root (:data:`UNDELETABLE_ROOT_ENTRIES`). Because
      only the first component is matched, ``user/<uid>/memory`` is *not*
      covered: the user's own ``memory/`` is the user's own content.
    * ``user_container`` -- the workspace-root ``user`` container, or a member's
      own ``user/<uid>`` directory itself. The latter is also the recycle bin's
      parent, so deleting it would carry the bin away with it and make the very
      same delete unrecoverable.
    * ``trash_not_targetable`` -- the recycle bin and anything inside it.
    * (the writable-scope root itself is refused by the caller, which knows the
      scope; it is not derivable from the path alone.)

    ``rel`` is validated first: an escape shape raises
    :class:`common.safe_fs.UnsafePathError` rather than being classified.
    """
    parts = safe_fs.split_relative(rel)
    first = parts[0]
    if first == TRASH_DIR_NAME:
        return "trash_not_targetable"
    if first == USER_CONTAINER_NAME:
        if len(parts) <= 2:
            return "user_container"
        if parts[2] == TRASH_DIR_NAME:
            return "trash_not_targetable"
        return None
    if first in UNDELETABLE_ROOT_ENTRIES:
        return "agent_internal"
    return None


def unuploadable_reason(rel: str) -> Optional[str]:
    """Why ``rel`` must not be *written to*, or ``None`` when it may be.

    Deliberately much narrower than :func:`undeletable_reason`, and the
    asymmetry is the point (design D13): uploading is **appending**, deleting is
    **erasing**. Adding a file to the Agent's ``memory/`` does not break the
    Agent; removing ``memory/`` does. Constraining both would only stop the user
    from filing anything into their own Agent, so only one rule carries over:

    * the recycle bin -- writing into it would leave a deleted entry
      indistinguishable from a live one, sitting where a restore does not look.

    `user_container` and the range root are not repeated here because a write
    cannot reach them in the first place: they are either outside the writable
    range or are the range itself, both already settled by the caller.
    """
    return ("trash_not_targetable"
            if undeletable_reason(rel) == "trash_not_targetable" else None)


class WorkspaceService:
    def __init__(self, workspace_root: str):
        self.root = os.path.realpath(os.path.expanduser(workspace_root))

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def resolve(self, rel_path: str) -> str:
        """Resolve a workspace-relative path, rejecting anything that escapes."""
        rel_path = (rel_path or "").replace("\\", "/").strip("/")
        full = os.path.realpath(os.path.join(self.root, rel_path))
        if full != self.root and os.path.commonpath([full, self.root]) != self.root:
            raise ValueError(f"Path escapes the workspace: {rel_path}")
        return full

    def to_workspace_rel(self, path: str) -> str:
        """
        Accept either form of path from a caller and return a relative one.

        An absolute path is only accepted when it points inside the workspace;
        otherwise `resolve` would silently reinterpret it as relative to the
        root (leading slashes are stripped) and read the wrong file.
        """
        path = (path or "").strip()
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            return path
        full = os.path.realpath(expanded)
        if full != self.root and os.path.commonpath([full, self.root]) != self.root:
            raise ValueError("Path is outside the workspace")
        return self.to_rel(full)

    def to_rel(self, abs_path: str) -> str:
        try:
            rel = os.path.relpath(abs_path, self.root)
        except ValueError:
            return abs_path
        return "" if rel == "." else rel.replace(os.sep, "/")

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------
    def list_dir(self, rel_path: str = "", show_hidden: bool = False,
                 allow_entry=None) -> Dict:
        """List one directory level, directories first then files by mtime desc.

        ``allow_entry`` is an optional ``abs_path -> bool`` admission rule. It
        runs *before* the entry cap is counted, so a caller that must hide some
        entries (the console file panel filtering another member's
        ``user/<id>``) neither returns them nor lets them push the visible ones
        past ``MAX_ENTRIES``. Default ``None`` keeps every existing caller's
        behaviour.

        The recycle bin is dropped unconditionally, including under
        ``show_hidden``. Being a dotfile is not what hides it -- ``show_hidden``
        exists precisely to reveal those -- so leaving it to the prefix rule
        would make the bin a normal, navigable folder whose contents look like
        live files.
        """
        full = self.resolve(rel_path)
        if not os.path.isdir(full):
            raise FileNotFoundError(f"Not a directory: {rel_path}")

        dirs: List[Dict] = []
        files: List[Dict] = []
        truncated = False
        try:
            with os.scandir(full) as it:
                for entry in it:
                    if not show_hidden and entry.name.startswith("."):
                        continue
                    if _is_trash_root(self.to_rel(entry.path)):
                        continue
                    if allow_entry is not None and not allow_entry(entry.path):
                        continue
                    if len(dirs) + len(files) >= MAX_ENTRIES:
                        truncated = True
                        break
                    item = self._describe(entry)
                    if item is None:
                        continue
                    (dirs if item["is_dir"] else files).append(item)
        except PermissionError:
            raise ValueError(f"Permission denied: {rel_path}")

        dirs.sort(key=lambda x: x["name"].lower())
        files.sort(key=lambda x: x["mtime"], reverse=True)

        return {
            "path": self.to_rel(full),
            "root": self.root,
            "entries": dirs + files,
            "truncated": truncated,
        }

    def _describe(self, entry) -> Optional[Dict]:
        try:
            stat = entry.stat(follow_symlinks=False)
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            return None
        kind = "directory" if is_dir else classify_kind(entry.name)
        return {
            "name": entry.name,
            "path": self.to_rel(entry.path),
            "abs_path": entry.path,
            "is_dir": is_dir,
            "kind": kind,
            "previewable": (not is_dir) and is_previewable(kind),
            "size": 0 if is_dir else stat.st_size,
            "mtime": stat.st_mtime,
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(self, query: str, limit: int = 30, allow_dir=None) -> Dict:
        """
        Subsequence match on the workspace-relative path, scored so that
        prefix matches on the entry name rank highest.

        Directories are included so a whole folder can be referenced (e.g. `@`
        a project dir); a matching folder naturally outranks the files inside it
        because those only match on the path, not the name.

        ``allow_dir`` is an optional ``abs_path -> bool`` traversal rule checked
        *before* a directory is descended into, so a subtree the caller may not
        read contributes neither results nor walk budget (the console file panel
        pruning another member's ``user/<id>``). Default ``None`` keeps every
        existing caller's behaviour.

        The recycle bin is never descended into: a deleted file is not a search
        result, and the walk would otherwise pull a whole second copy of the
        user's tree into the ranking.
        """
        query = (query or "").strip().lower()
        results: List[Dict] = []
        walked = 0

        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in SEARCH_SKIP_DIRS
                and not _is_trash_root(self.to_rel(os.path.join(dirpath, d)))
                and (allow_dir is None or allow_dir(os.path.join(dirpath, d)))
            ]
            for name in dirnames + filenames:
                is_dir = name in dirnames
                if name.startswith("."):
                    continue
                walked += 1
                if walked > MAX_SEARCH_WALK:
                    break
                entry = self._match(query, os.path.join(dirpath, name), name, is_dir)
                if entry:
                    results.append(entry)
            if walked > MAX_SEARCH_WALK:
                break

        results.sort(key=lambda x: (-x["_score"], -x["mtime"]))
        for r in results:
            r.pop("_score", None)
        return {"query": query, "results": results[:limit]}

    def _match(self, query: str, full: str, name: str, is_dir: bool) -> Optional[Dict]:
        """Score one entry against the query. None means it doesn't match."""
        rel = self.to_rel(full)
        score = self._score(query, name.lower(), rel.lower())
        if score < 0:
            return None

        parts = rel.split("/")
        # For a directory its own name counts, so `memory/` ranks low itself.
        if SEARCH_DEMOTE_DIRS.intersection(parts if is_dir else parts[:-1]):
            score -= SEARCH_DEMOTE_PENALTY

        kind = "directory" if is_dir else classify_kind(name)
        if kind == "file":
            # Unrecognized extension (or none at all): rarely what someone
            # means to reference, so keep it below real documents.
            score -= SEARCH_DEMOTE_PENALTY

        try:
            stat = os.stat(full)
        except OSError:
            return None

        return {
            "name": name,
            "path": rel,
            "abs_path": full,
            "is_dir": is_dir,
            "kind": kind,
            "previewable": (not is_dir) and is_previewable(kind),
            "size": 0 if is_dir else stat.st_size,
            "mtime": stat.st_mtime,
            "_score": score,
        }

    @staticmethod
    def _score(query: str, name: str, rel: str) -> int:
        """Higher is better; -1 means no match."""
        if not query:
            return 0
        if name.startswith(query):
            return 100
        if query in name:
            return 80
        if query in rel:
            return 60
        # Subsequence fallback so "idxhtml" finds "index.html".
        pos = 0
        for ch in query:
            pos = name.find(ch, pos)
            if pos < 0:
                return -1
            pos += 1
        return 30

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    def meta(self) -> Dict:
        return {
            "root": self.root,
            "exists": os.path.isdir(self.root),
            "server_time": time.time(),
        }

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------
    def dispatch(self, action: str, payload: Optional[dict] = None) -> dict:
        """
        Dispatch one read-only workspace action.

        Shared by every caller that reaches the workspace over a transport
        rather than in-process, so the path checks and size caps below apply
        uniformly. Actions: ``tree`` ``search`` ``resolve`` ``meta`` ``read``
        ``file``.

        Read-only by design: ``write_text`` is intentionally not dispatchable,
        so adding it here would hand write access to every remote transport
        that forwards its action string straight through.
        """
        payload = payload or {}
        try:
            if action == "tree":
                rel = self.to_workspace_rel(payload.get("path", ""))
                show_hidden = str(payload.get("show_hidden", "")).lower() in ("1", "true", "yes")
                result = self.list_dir(rel, show_hidden=show_hidden)

            elif action == "search":
                query = (payload.get("q") or payload.get("query") or "").strip()
                if not query:
                    return self._ok(action, {"query": "", "results": []})
                limit = max(1, min(int(payload.get("limit") or 30), 100))
                result = self.search(query, limit=limit)

            elif action == "resolve":
                rel = self.to_workspace_rel(payload.get("path", ""))
                result = {"file": self.stat_file(rel)}

            elif action == "meta":
                result = self.meta()

            elif action == "read":
                rel = self.to_workspace_rel(payload.get("path", ""))
                if not rel:
                    return self._err(action, 400, "path is required")
                result = self.read_text(rel, max_bytes=payload.get("max_bytes") or MAX_TEXT_BYTES)

            elif action == "file":
                rel = self.to_workspace_rel(payload.get("path", ""))
                if not rel:
                    return self._err(action, 400, "path is required")
                result = self.read_chunk(
                    rel,
                    offset=payload.get("offset") or 0,
                    chunk_size=payload.get("chunk_size") or DEFAULT_CHUNK_BYTES,
                )

            else:
                return self._err(action, 400, f"unknown action: {action}")

            return self._ok(action, result)

        except FileNotFoundError as e:
            return self._err(action, 404, str(e))
        except ValueError as e:
            # Path escapes, wrong entry type, oversized file.
            return self._err(action, 403, str(e))
        except PermissionError:
            return self._err(action, 403, "permission denied")
        except Exception as e:
            logger.error(f"[WorkspaceService] dispatch error: action={action}, error={e}")
            return self._err(action, 500, str(e))

    @staticmethod
    def _ok(action: str, payload) -> dict:
        return {"action": action, "code": 200, "message": "success", "payload": payload}

    @staticmethod
    def _err(action: str, code: int, message: str) -> dict:
        return {"action": action, "code": code, "message": message, "payload": None}

    def _resolve_file(self, rel_path: str) -> str:
        """Resolve a path that must point at a regular file."""
        full = self.resolve(rel_path)
        if os.path.isdir(full):
            raise ValueError(f"Not a file: {rel_path}")
        if not os.path.isfile(full):
            raise FileNotFoundError(f"File not found: {rel_path}")
        return full

    def read_text(self, rel_path: str, max_bytes: int = MAX_TEXT_BYTES) -> Dict:
        """
        Read a text file as a string.

        Undecodable bytes are replaced rather than raising, so a file with a
        stray encoding still previews instead of erroring out. `mtime` is the
        baseline an editor passes back to :meth:`write_text`, and `editable`
        says whether saving would be accepted at all.
        """
        full = self._resolve_file(rel_path)
        max_bytes = max(1, min(int(max_bytes or MAX_TEXT_BYTES), MAX_TEXT_BYTES))
        size = os.path.getsize(full)
        with open(full, "rb") as f:
            raw = f.read(max_bytes)
        truncated = size > len(raw)
        content, lossy = _decode_utf8(raw)
        return {
            "path": self.to_rel(full),
            "content": content,
            "truncated": truncated,
            "lossy": lossy,
            "size": size,
            "mtime": os.path.getmtime(full),
            # Neither a partial read nor a lossy decode may be edited: saving
            # would truncate the tail in the first case and write replacement
            # characters over every undecodable byte in the second.
            "editable": is_editable(classify_kind(full)) and not truncated and not lossy,
        }

    def write_text(self, rel_path: str, content: str,
                   expected_mtime: Optional[float] = None) -> Dict:
        """
        Overwrite an existing text file with `content`.

        The file must already exist: the preview panel edits what it shows, and
        a path that no longer resolves means the caller's view is stale rather
        than that a new file is wanted.

        :param expected_mtime: the mtime the caller last read. When given and
            the file has since changed - typically because the agent rewrote it
            while the user was typing - raise :class:`WorkspaceConflictError`
            instead of dropping those changes on the floor.
        """
        full = self._resolve_file(rel_path)

        kind = classify_kind(full)
        if not is_editable(kind):
            raise ValueError(f"Not an editable text file: {self.to_rel(full)}")

        if expected_mtime is not None:
            current = os.path.getmtime(full)
            if abs(current - float(expected_mtime)) > MTIME_EPSILON:
                raise WorkspaceConflictError(
                    f"File changed on disk since it was read: {self.to_rel(full)}"
                )

        # Re-derive both editability rules from disk rather than trusting that
        # the caller honoured the `editable` flag it was given by `read_text`.
        size = os.path.getsize(full)
        if size > MAX_TEXT_BYTES:
            raise ValueError(f"File too large to edit: {size} bytes")
        with open(full, "rb") as f:
            existing = f.read()
        if _decode_utf8(existing)[1]:
            raise ValueError(f"Not a UTF-8 text file: {self.to_rel(full)}")

        # A text area always hands back LF. Restore CRLF when the file already
        # used it, so saving one line does not rewrite every line.
        newline = "\r\n" if b"\r\n" in existing else "\n"
        data = (content or "").replace("\r\n", "\n").replace("\r", "\n")
        if newline != "\n":
            data = data.replace("\n", newline)
        encoded = data.encode("utf-8")
        if len(encoded) > MAX_TEXT_BYTES:
            raise ValueError(f"Content too large: {len(encoded)} bytes")

        self._replace_atomically(full, encoded)
        return {
            "path": self.to_rel(full),
            "size": os.path.getsize(full),
            "mtime": os.path.getmtime(full),
        }

    @staticmethod
    def _replace_atomically(full: str, data: bytes) -> None:
        """
        Write via a sibling temp file and rename over the target.

        A crash or a full disk then leaves the original file intact instead of
        half-written, and a reader never observes a partial document.
        """
        directory = os.path.dirname(full)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".cow-edit-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            # mkstemp creates the temp file 0600; carry over the original mode so
            # an edit does not silently strip the executable bit or group access.
            try:
                os.chmod(tmp_path, os.stat(full).st_mode & 0o7777)
            except OSError:
                pass
            os.replace(tmp_path, full)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def read_chunk(self, rel_path: str, offset: int = 0,
                   chunk_size: int = DEFAULT_CHUNK_BYTES) -> Dict:
        """
        Read one base64 chunk of a file.

        Callers pull successive offsets until `eof`, which keeps any single
        response small enough for a size-limited transport.
        """
        full = self._resolve_file(rel_path)
        size = os.path.getsize(full)
        if size > MAX_FILE_BYTES:
            raise ValueError(f"File too large: {size} bytes")

        offset = max(0, int(offset or 0))
        chunk_size = max(1, min(int(chunk_size or DEFAULT_CHUNK_BYTES), MAX_CHUNK_BYTES))
        with open(full, "rb") as f:
            f.seek(offset)
            raw = f.read(chunk_size)

        mime, _ = mimetypes.guess_type(full)
        return {
            "path": self.to_rel(full),
            "name": os.path.basename(full),
            "mime": mime or "application/octet-stream",
            "total_size": size,
            "offset": offset,
            "length": len(raw),
            "eof": offset + len(raw) >= size,
            "content_b64": base64.b64encode(raw).decode("ascii"),
        }

    def stat_file(self, rel_path: str) -> Dict:
        """
        Metadata for one entry, used when opening something by path.

        Directories resolve too: callers that reference a folder (drag, `@`)
        need to learn it's a folder rather than get an error.
        """
        full = self.resolve(rel_path)
        is_dir = os.path.isdir(full)
        if not is_dir and not os.path.isfile(full):
            raise FileNotFoundError(f"File not found: {rel_path}")
        stat = os.stat(full)
        kind = "directory" if is_dir else classify_kind(full)
        return {
            "name": os.path.basename(full) or self.to_rel(full),
            "path": self.to_rel(full),
            "abs_path": full,
            "is_dir": is_dir,
            "kind": kind,
            "previewable": (not is_dir) and is_previewable(kind),
            "size": 0 if is_dir else stat.st_size,
            "mtime": stat.st_mtime,
        }

    # ------------------------------------------------------------------
    # Writes (binary, directory-creating)
    # ------------------------------------------------------------------
    #
    # ``write_text`` above is the editor's path: it refuses to create a file and
    # only accepts UTF-8 text. An upload is neither — it creates its target and
    # its payload is opaque bytes — so it gets its own primitives, both routed
    # through ``common.safe_fs`` so every component is opened with ``O_NOFOLLOW``
    # and a swap for a symlink between validation and the write is refused
    # rather than followed.

    def ensure_dir(self, rel_path: str) -> str:
        """Create one directory (and its missing parents); return its rel path.

        Idempotent. Only called for directories a file is about to land in, or
        an explicit destination: nothing materializes an empty directory, since
        a folder with no content reappearing after a restore is not something
        anyone asked for.
        """
        safe_fs.mkdir(self.root, rel_path)
        return self.to_rel(os.path.join(self.root, rel_path))

    def write_bytes(self, rel_path: str, data: bytes) -> Dict:
        """Write ``data`` to ``rel_path``, creating parents; atomic.

        :raises common.safe_fs.UnsafePathError: for an escaping shape or a
            symlinked component.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        safe_fs.write_bytes_atomic(self.root, rel_path, bytes(data))
        st = safe_fs.stat(self.root, rel_path)
        return {
            "path": rel_path,
            "size": st.st_size if st is not None else len(data),
        }

    def exists(self, rel_path: str) -> bool:
        return safe_fs.exists(self.root, rel_path)

    def is_dir(self, rel_path: str) -> bool:
        return safe_fs.is_dir(self.root, rel_path)

    def is_file(self, rel_path: str) -> bool:
        return safe_fs.is_file(self.root, rel_path)

    def entry_size(self, rel_path: str) -> int:
        """Size of one *file*; a directory reports ``0``.

        Deliberately does not walk a directory. The only caller is the delete
        prompt, and walking a large tree inside a web request to render a
        nicer confirmation line is a hang waiting to happen. A directory's own
        size is meaningless anyway, and the bin reports its item count.
        """
        st = safe_fs.stat(self.root, rel_path)
        if st is None:
            raise FileNotFoundError(rel_path)
        return 0 if safe_fs.is_dir(self.root, rel_path) else st.st_size

    # ------------------------------------------------------------------
    # Recycle bin
    # ------------------------------------------------------------------
    #
    # A delete is a move into ``<agent>/user/<uid>/.trash/<batch>/files/<slot>``.
    # The batch metadata records each item's original relative path and its
    # state, so a restore needs no side table to know where something came from,
    # and one delete request is *one* batch directory rather than one per item:
    # deleting 5000 files must not mint 5000 directories, each needing its own
    # bookkeeping.
    #
    # ``agent_rel`` is the Agent's own directory relative to this service root,
    # and it is where the bin is anchored -- the bin belongs to the person, so it
    # must be ``<agent>/user/<uid>/.trash`` even when they may write the whole
    # Agent. ``user_id`` alone is not enough to place it: in database mode the
    # service root is the tenant's shared root, and a bin put at
    # ``<root>/user/<uid>/.trash`` would be inside no Agent at all, hence outside
    # every ownership rule. ``scope_rel`` is a different question -- where the
    # caller may *write* -- and the two must not be conflated.

    @staticmethod
    def new_batch_id() -> str:
        return "%s-%s" % (time.strftime("%Y%m%dT%H%M%S"), os.urandom(4).hex())

    def trash_root_rel(self, user_id: str, *, agent_rel: str = "") -> str:
        """The caller's own bin: ``<agent>/user/<uid>/.trash``.

        Anchored on the **Agent's** directory rather than on the caller's
        writable range, because the bin belongs to the *person*: it has to stay
        in their ``user/<uid>`` even when they may write the whole Agent (a
        private Agent), and it must not move when the Agent's visibility
        changes. Keeping it under a ``user/<uid>`` is also what makes it inherit
        the ordinary ownership rule instead of needing a second authorization.

        ``agent_rel`` is the Agent's directory relative to this service's root,
        which is not the Agent's workspace in database mode -- there the root is
        the tenant's shared root, so the Agent's own directory is
        ``agents/<id>``. Omitting it keeps the historical layout
        (``user/<uid>/.trash``) for a service rooted at the Agent workspace.
        """
        base = (join_rel(agent_rel, USER_CONTAINER_NAME)
                if agent_rel else USER_CONTAINER_NAME)
        return join_rel(join_rel(base, user_id), TRASH_DIR_NAME)

    def _batch_rel(self, user_id: str, batch_id: str, *,
                   agent_rel: str = "") -> str:
        safe_fs.split_relative(batch_id)
        return join_rel(self.trash_root_rel(user_id, agent_rel=agent_rel),
                        batch_id)

    def _batch_meta_rel(self, user_id: str, batch_id: str, *,
                        agent_rel: str = "") -> str:
        return join_rel(self._batch_rel(user_id, batch_id, agent_rel=agent_rel),
                        TRASH_META_NAME)

    def _payload_rel(self, user_id: str, batch_id: str, slot: int, *,
                     agent_rel: str = "") -> str:
        """Where one item's bytes sit inside its batch: ``files/<slot>``.

        Named by the item's position in the batch rather than by its original
        path. Mirroring the original tree inside the bin reads as tidier, but it
        duplicates the whole path under ``.trash/<batch>/files/`` -- and for a
        deep path that is enough to cross Windows' 248-character
        *create-directory* limit, where the move then fails with a bare "not
        found" and the batch keeps an empty shell. Nothing is lost by naming
        slots instead: the original path lives in the batch metadata, which
        restore and purge read anyway, and two same-named files in one batch can
        no longer collide.
        """
        return join_rel(self._batch_rel(user_id, batch_id, agent_rel=agent_rel),
                        join_rel(TRASH_FILES_DIR, str(int(slot))))

    def _read_batch(self, user_id: str, batch_id: str, *,
                    agent_rel: str = "") -> Optional[Dict]:
        raw = safe_fs.read_text(
            self.root, self._batch_meta_rel(user_id, batch_id,
                                            agent_rel=agent_rel))
        if raw is None:
            return None
        try:
            meta = json.loads(raw)
        except ValueError:
            logger.warning("[Workspace] unreadable trash batch %s", batch_id)
            return None
        return meta if isinstance(meta, dict) else None

    def _write_batch(self, user_id: str, batch_id: str, meta: Dict, *,
                     agent_rel: str = "") -> None:
        safe_fs.write_text_atomic(
            self.root,
            self._batch_meta_rel(user_id, batch_id, agent_rel=agent_rel),
            json.dumps(meta, ensure_ascii=False))

    def move_to_trash(self, user_id: str, items: List[Dict], *,
                      agent_rel: str = "") -> Dict:
        """Soft-delete ``items`` (``[{"rel", "kind", "size"}]``) into one batch.

        Returns ``{"batch_id", "moved": [...], "failed": [...]}``. Nothing is
        destroyed here: each item is an atomic rename into the bin, so a failure
        mid-batch leaves every other item exactly where it was and leaves no
        half-deleted directory behind.

        A batch whose every item failed is removed rather than left as an empty
        shell, so the bin never accumulates entries that stand for nothing.
        """
        batch_id = self.new_batch_id()
        moved: List[Dict] = []
        failed: List[Dict] = []
        for item in items:
            rel = item.get("rel") or ""
            try:
                payload = self._payload_rel(user_id, batch_id, len(moved),
                                            agent_rel=agent_rel)
                safe_fs.rename(self.root, rel, payload, create_parents=True)
                moved.append({
                    "rel": rel,
                    "kind": item.get("kind") or (
                        "directory" if item.get("is_dir") else "file"),
                    "size": int(item.get("size") or 0),
                    "deleted_at": time.time(),
                    "state": "trashed",
                    "restored_to": None,
                })
            except FileNotFoundError:
                failed.append({"rel": rel, "code": "not_found"})
            except FileExistsError:
                # Same relative path twice in one request; the first one won.
                failed.append({"rel": rel, "code": "duplicate"})
            except safe_fs.UnsafePathError:
                failed.append({"rel": rel, "code": "unsafe_path"})
            except OSError as exc:
                # EXDEV and friends: an atomic move is impossible, and the
                # alternative (copy, then delete) would leave a window where
                # neither a complete copy nor the original is guaranteed.
                logger.warning("[Workspace] trash move failed for %s: %s", rel, exc)
                failed.append({"rel": rel, "code": "not_movable"})

        if not moved:
            return {"batch_id": None, "moved": [], "failed": failed}

        self._write_batch(user_id, batch_id, {
            "batch_id": batch_id,
            "created_at": time.time(),
            "items": moved,
        }, agent_rel=agent_rel)
        return {"batch_id": batch_id, "moved": moved, "failed": failed}

    def list_trash(self, user_id: str, *, agent_rel: str = "") -> List[Dict]:
        """Still-trashed entries, newest batch first."""
        out: List[Dict] = []
        root_rel = self.trash_root_rel(user_id, agent_rel=agent_rel)
        for batch_id in safe_fs.list_names(self.root, root_rel):
            meta = self._read_batch(user_id, batch_id, agent_rel=agent_rel)
            if not meta:
                continue
            for index, item in enumerate(meta.get("items") or []):
                if (item or {}).get("state") != "trashed":
                    continue
                out.append({
                    "batch_id": batch_id,
                    "index": index,
                    "rel": item.get("rel"),
                    "kind": item.get("kind") or "file",
                    "size": int(item.get("size") or 0),
                    "deleted_at": item.get("deleted_at"),
                })
        out.sort(key=lambda e: e.get("deleted_at") or 0, reverse=True)
        return out

    def available_name(self, dest_rel: str) -> str:
        """``dest_rel``, or the first free ``name (n).ext`` variant of it.

        Used by both a restore and an upload: the original path is taken, and the
        alternative is a rename rather than a refusal, because neither operation
        is a move — "refuse" would leave the user with a file they cannot put
        anywhere.
        """
        if not safe_fs.exists(self.root, dest_rel):
            return dest_rel
        directory, _, name = dest_rel.rpartition("/")
        if "." in name.lstrip("."):
            stem, dot, ext = name.rpartition(".")
            suffix = dot + ext
        else:
            stem, suffix = name, ""
        for n in range(1, 1000):
            candidate = "%s (%d)%s" % (stem, n, suffix)
            candidate_rel = join_rel(directory, candidate) if directory else candidate
            if not safe_fs.exists(self.root, candidate_rel):
                return candidate_rel
        raise FileExistsError(dest_rel)

    def restore_from_trash(self, user_id: str, batch_id: str,
                           indices: Optional[List[int]] = None, *,
                           agent_rel: str = "", dest_guard=None) -> Dict:
        """Move entries back to the paths they were deleted from.

        Re-validates each destination before moving anything: an Agent's
        visibility can change between the delete and the restore, so a path that
        was inside the writable scope then may be outside it now. Restoring
        anyway would be a write that bypasses the narrowing, so such an item is
        refused with a reason rather than landing somewhere the caller may not
        write.

        ``dest_guard`` is the caller's ``rel -> refusal code or None`` rule and
        is what performs that re-validation; it is the same seam the delete used,
        so the two cannot disagree. ``undeletable_reason`` is applied as well,
        which is what protects a service rooted directly at an Agent workspace
        (where ``rel`` is agent-relative).
        """
        meta = self._read_batch(user_id, batch_id, agent_rel=agent_rel)
        if meta is None:
            return {"restored": [], "failed": [{"index": None,
                                                "code": "batch_not_found"}]}
        items = meta.get("items") or []
        targets = range(len(items)) if indices is None else indices
        restored: List[Dict] = []
        failed: List[Dict] = []
        for index in targets:
            if not isinstance(index, int) or not 0 <= index < len(items):
                failed.append({"index": index, "code": "not_found"})
                continue
            item = items[index]
            rel = item.get("rel") or ""
            if item.get("state") != "trashed":
                failed.append({"index": index, "rel": rel, "code": "not_found"})
                continue
            reason = dest_guard(rel) if dest_guard is not None else None
            if reason is None:
                reason = undeletable_reason(rel) if rel else "unsafe_path"
            if reason:
                failed.append({"index": index, "rel": rel, "code": reason})
                continue
            payload = self._payload_rel(user_id, batch_id, index,
                                        agent_rel=agent_rel)
            try:
                dest = self.available_name(rel)
                safe_fs.rename(self.root, payload, dest, create_parents=True)
            except safe_fs.UnsafePathError:
                failed.append({"index": index, "rel": rel, "code": "unsafe_path"})
                continue
            except FileNotFoundError:
                failed.append({"index": index, "rel": rel, "code": "not_found"})
                continue
            except OSError as exc:
                logger.warning("[Workspace] trash restore failed for %s: %s", rel, exc)
                failed.append({"index": index, "rel": rel, "code": "not_movable"})
                continue
            item["state"] = "restored"
            item["restored_to"] = dest
            item["restored_at"] = time.time()
            restored.append({"index": index, "rel": rel,
                             "path": dest, "renamed": dest != rel})
        self._write_batch(user_id, batch_id, meta, agent_rel=agent_rel)
        self._drop_batch_if_empty(user_id, batch_id, meta, agent_rel=agent_rel)
        return {"restored": restored, "failed": failed}

    def purge_trash(self, user_id: str, batch_id: str,
                    indices: Optional[List[int]] = None, *,
                    agent_rel: str = "") -> Dict:
        """Destroy entries in the bin. ``indices=None`` empties the batch.

        Only addresses paths inside the caller's own bin: ``batch_id`` selects
        a batch *by name* (validated by :func:`safe_fs.split_relative`) and is
        never concatenated from client input, so a guessed id reaches nothing
        beyond this user's bin.
        """
        meta = self._read_batch(user_id, batch_id, agent_rel=agent_rel)
        if meta is None:
            return {"purged": [], "failed": [{"index": None,
                                              "code": "batch_not_found"}]}
        items = meta.get("items") or []
        targets = range(len(items)) if indices is None else indices
        purged: List[Dict] = []
        failed: List[Dict] = []
        for index in targets:
            if not isinstance(index, int) or not 0 <= index < len(items):
                failed.append({"index": index, "code": "not_found"})
                continue
            item = items[index]
            rel = item.get("rel") or ""
            if item.get("state") != "trashed":
                failed.append({"index": index, "rel": rel, "code": "not_found"})
                continue
            payload = self._payload_rel(user_id, batch_id, index,
                                        agent_rel=agent_rel)
            try:
                safe_fs.remove_tree(self.root, payload)
            except safe_fs.UnsafePathError:
                failed.append({"index": index, "rel": rel, "code": "unsafe_path"})
                continue
            except OSError as exc:
                logger.warning("[Workspace] trash purge failed for %s: %s", rel, exc)
                failed.append({"index": index, "rel": rel, "code": "not_removable"})
                continue
            item["state"] = "purged"
            purged.append({"index": index, "rel": rel})
        self._write_batch(user_id, batch_id, meta, agent_rel=agent_rel)
        self._drop_batch_if_empty(user_id, batch_id, meta, agent_rel=agent_rel)
        return {"purged": purged, "failed": failed}

    def _drop_batch_if_empty(self, user_id: str, batch_id: str, meta: Dict, *,
                             agent_rel: str = "") -> bool:
        """Remove a batch directory once nothing in it is still trashed.

        Otherwise emptying a batch item by item would leave a directory holding
        only bookkeeping, and the bin's item count would stop matching its
        directory count — visible as an entry that lists nothing.
        """
        items = meta.get("items") or []
        if any((item or {}).get("state") == "trashed" for item in items):
            return False
        try:
            safe_fs.remove_tree(
                self.root, self._batch_rel(user_id, batch_id,
                                           agent_rel=agent_rel))
        except (OSError, safe_fs.UnsafePathError) as exc:
            logger.warning("[Workspace] trash batch cleanup failed for %s: %s",
                           batch_id, exc)
            return False
        return True

    def cleanup_trash(self, user_id: str,
                      max_age: int = TRASH_RETENTION_SECONDS, *,
                      agent_rel: str = "") -> int:
        """Collect batches older than ``max_age``; return how many were removed.

        Age comes from the batch directory's own mtime, never from walking its
        contents: the payload may itself contain symbolic links (it came from a
        user directory), and following one here would delete something outside
        the bin.
        """
        removed = 0
        cutoff = time.time() - max(0, int(max_age))
        root_rel = self.trash_root_rel(user_id, agent_rel=agent_rel)
        for batch_id in safe_fs.list_names(self.root, root_rel):
            batch_rel = join_rel(root_rel, batch_id)
            try:
                st = safe_fs.stat(self.root, batch_rel)
            except (OSError, safe_fs.UnsafePathError):
                continue
            if st is None or st.st_mtime >= cutoff:
                continue
            try:
                safe_fs.remove_tree(self.root, batch_rel)
                removed += 1
            except (OSError, safe_fs.UnsafePathError) as exc:
                logger.warning("[Workspace] trash retention failed for %s: %s",
                               batch_id, exc)
        return removed
