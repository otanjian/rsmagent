"""
Artifact detection - decide which agent-written files are user-facing outputs.

The agent writes many files that are internal bookkeeping (memory logs, skills,
knowledge base pages). Only files a human would actually want to open should be
surfaced in the chat UI as previewable artifacts.

A file written *in a local project* (change task 9.1) gets one extra thing: an
"origin" that says which device, project, run and tool call produced it, plus an
opaque source version. That is what lets a card stay pointed at the machine that
actually holds the bytes instead of at a server path that was never written. The
origin carries identifiers only -- never an absolute path -- because the root
mapping must not travel as protocol or artifact metadata.
"""

import hashlib
import os
from typing import Any, Dict, Optional

from common.log import logger
from common.utils import expand_path

# Directories under the workspace that hold agent-internal state, never artifacts.
INTERNAL_DIRS = {
    "memory",
    "knowledge",
    "skills",
    "tmp",
    "scheduler",
    "plans",
}

# Workspace-root files that are part of the agent's own configuration.
INTERNAL_FILES = {
    "AGENT.md",
    "RULE.md",
    "MEMORY.md",
    "USER.md",
    "BOOTSTRAP.md",
    "mcp.json",
}

_EXT_KINDS = {
    "html": {".html", ".htm"},
    "markdown": {".md", ".markdown"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico"},
    "video": {".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v"},
    "audio": {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"},
    "pdf": {".pdf"},
    "csv": {".csv", ".tsv"},
    "code": {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".go",
        ".rs", ".rb", ".php", ".sh", ".sql", ".css", ".scss", ".json", ".yaml",
        ".yml", ".xml", ".toml", ".ini",
    },
    "text": {".txt", ".log"},
    "office": {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"},
}

# Kinds the frontend can render inline in the preview panel.
PREVIEWABLE_KINDS = {
    "html", "markdown", "image", "video", "audio", "pdf", "csv", "code", "text",
}

# Kinds whose bytes are plain text, so the preview panel can offer an editor.
# Deliberately a subset of PREVIEWABLE_KINDS: an image or a PDF previews fine
# but would be destroyed by a round-trip through a text area.
EDITABLE_KINDS = {"html", "markdown", "csv", "code", "text"}

_KIND_BY_EXT: Dict[str, str] = {
    ext: kind for kind, exts in _EXT_KINDS.items() for ext in exts
}

# ---------------------------------------------------------------------------
# The artifact reference protocol (contracts/desktop/v2.json -> "artifact")
# ---------------------------------------------------------------------------
#
# The reference the *protocol* speaks and the kind the *panel* renders are two
# different vocabularies: the panel wants "markdown" so it can pick an editor,
# the contract wants "text" so a client only has to know six buckets. Keeping
# them apart is deliberate -- collapsing them would either break the existing
# renderer or widen the contract with UI-only values.
_PROTOCOL_KIND = "local-artifact-v1"

_PROTOCOL_KIND_BY_PREVIEW = {
    "html": "text",
    "markdown": "text",
    "csv": "text",
    "code": "text",
    "text": "text",
    "image": "image",
    "pdf": "pdf",
    "office": "office",
    "video": "binary",
    "audio": "binary",
    "file": "binary",
}

_ARCHIVE_EXTS = {
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst",
}

#: Files up to this size get a content digest as their version. Above it, the
#: version falls back to a ``stat:`` token: hashing a multi-gigabyte artifact on
#: the request that produced it would stall the very click the user just made,
#: and the requirement is explicitly "大文件摘要计算不得阻塞 UI".
SOURCE_VERSION_DIGEST_MAX_BYTES = 8 * 1024 * 1024

_DIGEST_CHUNK = 1024 * 1024


def protocol_kind(path: str) -> str:
    """The contract's coarse kind (``text``/``office``/``image``/...).

    Derived from the extension, not from the display name, and never from the
    caller's claim -- an artifact whose kind does not match its bytes is exactly
    what a card pointing at the wrong file would look like.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _ARCHIVE_EXTS:
        return "archive"
    return _PROTOCOL_KIND_BY_PREVIEW.get(classify_kind(path), "binary")


def source_version(path: str) -> str:
    """An opaque version token for a file that exists right now.

    Content digest when the file is small enough to read, otherwise a
    ``stat:<size>-<mtime_ns>`` token. Opaque on purpose: callers only ever
    compare two of these for equality, so the shape may change without a
    protocol break, and the name the user gave the file is never part of it
    (two different reports must not look like the same version because both are
    called ``报告.xlsx``).
    """
    stat = os.stat(path)
    size = int(stat.st_size)
    if size > SOURCE_VERSION_DIGEST_MAX_BYTES:
        return "stat:%d-%d" % (size, int(stat.st_mtime_ns))
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_DIGEST_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def artifact_id(run_id: str, tool_call_id: str, relative_path: str) -> str:
    """A stable id for one produced file, with no path baked into the value.

    Deterministic so a replayed card and the live one are the *same* artifact
    instead of two, and hashed so the id itself cannot be used to learn where
    the file lives.
    """
    material = "\u0000".join((str(run_id or ""), str(tool_call_id or ""),
                              str(relative_path or "")))
    return "art_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def desktop_origin(target: Any, *, run_id: str = "", tool_call_id: str = "",
                   owner: str = "", tenant_id: str = "") -> Optional[Dict[str, Any]]:
    """Run-scope half of a local artifact's origin, or ``None`` for the server.

    ``None`` is the pre-existing case and keeps server artifacts byte-identical:
    a target that is not a desktop one must not acquire device identifiers it
    never had.
    """
    if target is None or not getattr(target, "is_desktop", False):
        return None
    return {
        "source": "desktop",
        "artifact_protocol": _PROTOCOL_KIND,
        "device_id": getattr(target, "device_id", "") or "",
        "workspace_id": getattr(target, "workspace_id", "") or "",
        "binding_id": getattr(target, "binding_id", "") or "",
        "project_mode": getattr(target, "project_mode", "") or "",
        "grant_version": int(getattr(target, "grant_version", 0) or 0),
        "run_id": str(run_id or ""),
        "tool_call_id": str(tool_call_id or ""),
        "owner": str(owner or ""),
        "tenant_id": str(tenant_id or ""),
    }


def get_workspace_root() -> str:
    """Absolute path of the routed Agent's workspace."""
    from common.state_dir import real_state_root

    return real_state_root()


def classify_kind(path: str) -> str:
    """Map a file extension to a coarse preview kind."""
    ext = os.path.splitext(path)[1].lower()
    return _KIND_BY_EXT.get(ext, "file")


def is_previewable(kind: str) -> bool:
    return kind in PREVIEWABLE_KINDS


def is_editable(kind: str) -> bool:
    return kind in EDITABLE_KINDS


def resolve_workspace_path(path: str, workspace_root: str) -> str:
    """Resolve a tool `path` argument the same way the file tools do."""
    expanded = expand_path(path)
    if os.path.isabs(expanded):
        return os.path.realpath(expanded)
    return os.path.realpath(os.path.join(workspace_root, expanded))


def _is_internal(abs_path: str, workspace_root: str) -> bool:
    """True when the file is agent bookkeeping rather than a user-facing output."""
    name = os.path.basename(abs_path)
    if name.startswith("."):
        return True

    try:
        rel = os.path.relpath(abs_path, workspace_root)
    except ValueError:
        # Different drive on Windows: outside the workspace, judge by name only.
        return False

    if rel.startswith(".."):
        # Outside the workspace (e.g. editing project source): not an artifact.
        return True

    parts = rel.split(os.sep)
    if len(parts) == 1:
        return name in INTERNAL_FILES
    if parts[0] in INTERNAL_DIRS:
        return True
    return any(p.startswith(".") for p in parts[:-1])


def build_artifact(path: str, workspace_root: Optional[str] = None,
                   origin: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
    """
    Build artifact metadata for a file the agent just wrote.

    Returns None when the file is internal, missing, or not worth surfacing.
    ``origin`` attaches the device/project/run identity a local file needs
    (task 9.1); it is only ever added *after* the file itself has been verified
    to exist, which is what makes a published card a statement about a real file
    rather than about what the model said it wrote.
    """
    if not path:
        return None

    root = workspace_root or get_workspace_root()
    # resolve_workspace_path() realpath-resolves the file (following symlinks),
    # so the root must be resolved the same way or the relpath check below sees
    # a mismatched prefix (e.g. /var vs /private/var on macOS) and wrongly treats
    # an in-project file as "outside the workspace".
    try:
        root = os.path.realpath(expand_path(root))
    except Exception:
        pass
    try:
        abs_path = resolve_workspace_path(path, root)
    except Exception:
        return None

    if _is_internal(abs_path, root):
        return None
    if not os.path.isfile(abs_path):
        return None

    try:
        size = os.path.getsize(abs_path)
    except OSError:
        size = 0

    try:
        rel_path = os.path.relpath(abs_path, root)
    except ValueError:
        rel_path = abs_path

    kind = classify_kind(abs_path)
    artifact = {
        "type": "artifact",
        "path": abs_path,
        "rel_path": rel_path,
        "dir": os.path.dirname(abs_path),
        "file_name": os.path.basename(abs_path),
        "kind": kind,
        "previewable": is_previewable(kind),
        "size": size,
    }
    local = _local_origin(abs_path, rel_path, origin)
    if local is not None:
        artifact["origin"] = local
    return artifact


def _local_origin(abs_path: str, rel_path: str,
                  origin: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Complete a desktop origin with the per-file half, or return None.

    A relative path that escapes the root (``..``) is not a project reference at
    all, so it cannot be published as one -- the contract refuses absolute or
    climbing ``relative_path``s, and a card that names a file outside the
    authorized project would be pointing at something this run was never
    granted. A version we cannot read also drops the origin rather than
    publishing an unverifiable one.
    """
    if not origin or origin.get("source") != "desktop":
        return None
    if not rel_path or os.path.isabs(rel_path) or rel_path.startswith(".."):
        return None
    try:
        version = source_version(abs_path)
    except OSError:
        return None
    try:
        size = int(os.path.getsize(abs_path))
    except OSError:
        size = 0
    complete = dict(origin)
    complete.update({
        "artifact_id": artifact_id(origin.get("run_id", ""),
                                   origin.get("tool_call_id", ""), rel_path),
        "relative_path": rel_path,
        "file_name": os.path.basename(abs_path),
        "kind": protocol_kind(abs_path),
        "size": size,
        "source_version": version,
    })
    return complete


def safe_build_artifact(path: str, workspace_root: Optional[str] = None,
                        origin: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
    """build_artifact that never raises - artifact reporting must not break a tool call."""
    try:
        return build_artifact(path, workspace_root, origin)
    except Exception as e:
        logger.debug(f"[Artifact] skipped {path}: {e}")
        return None
