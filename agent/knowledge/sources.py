"""Original source assets: staging, dedup, immutable versions and lifecycle.

Change ``add-traceable-knowledge-ingestion`` (design D3 / D4, phase B).

This is the *only* code allowed to write inside the managed ``originals`` and
``converted`` trees. Everything else goes through ``KnowledgeService``, which
refuses managed paths. The service therefore owns the exact guarantees the
console promises:

- **Any format** is stored byte-for-byte, with the original filename, size,
  hash and upload time. No converter is required to *save* a file.
- **Nothing is searchable until a conversion batch is published.** ``saved`` is
  never reported as ``searchable``; a source without an ``active_task_id``
  contributes no documents to the effective set.
- **Idempotent retries.** Each file carries a request key; the same key with the
  same bytes returns the same source/version instead of allocating another one,
  and the same key with different bytes is a conflict.
- **Immutable versions.** A committed original is never overwritten in place; a
  new upload to the same source appends ``latest_version + 1`` after validating
  the caller's expected version.

Concurrency: the shared cross-process *usage* lock is held for the whole call,
so a mode switch cannot move the root mid-write. Two uploads may still run
concurrently (shared locks coexist), so an in-process per-root mutex additionally
serializes the decide-and-publish section. That makes two simultaneous uploads of
identical content converge on one source in the common single-process
deployment; the catalog transaction still prevents two processes from the same
version number.
"""

from __future__ import annotations

import functools
import hashlib
import mimetypes
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Iterable, Optional

from common.log import logger
from agent.knowledge.catalog import (
    COMMIT_COMMITTED,
    COMMIT_PENDING,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DELETED,
    LIFECYCLE_DISABLED,
    TASK_CANCELLED,
    TASK_QUEUED,
    TASK_RUNNING,
    KnowledgeCatalog,
)
from agent.knowledge.locks import (
    KnowledgeRootLock,
    KnowledgeUnavailableError,
    read_marker,
)

#: Built-in limits, overridden by settings. ``0`` in a setting means "default".
DEFAULT_MAX_FILES = 100
DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024
DEFAULT_MAX_BATCH_SIZE = 200 * 1024 * 1024

#: Content types safe to render inline. Anything else is forced to a download,
#: and every response carries ``nosniff``: active content (HTML/SVG/scripts)
#: must never execute with the console's origin.
_PREVIEW_TYPES = frozenset({
    "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/mp4", "audio/webm",
    "video/mp4", "video/webm", "video/ogg",
    "text/plain", "text/csv", "text/markdown",
})

_UNSAFE_NAME = re.compile(r'[<>:"|?*\x00-\x1f/\\]')
_BLOB_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class SourceError(ValueError):
    """Base class for a source-asset refusal with HTTP semantics."""

    code = "source_error"
    status = 400


class SourceValidationError(SourceError):
    code = "source_invalid"
    status = 400


class SourceNotFoundError(SourceError):
    code = "source_not_found"
    status = 404


class SourceConflictError(SourceError):
    code = "source_conflict"
    status = 409


class SourceQuotaError(SourceError):
    code = "source_quota_exceeded"
    status = 409


class SourceUnavailableError(SourceError):
    code = "source_unavailable"
    status = 503


def source_limits() -> dict:
    """The server-enforced limits projected to the upload panel."""
    from config import conf

    cfg = conf()

    def _int(key: str, default: int) -> int:
        try:
            value = int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    return {
        "max_files": _int("knowledge_source_max_files", DEFAULT_MAX_FILES),
        "max_file_size": _int("knowledge_source_max_file_size", DEFAULT_MAX_FILE_SIZE),
        "max_batch_size": _int("knowledge_source_max_batch_size", DEFAULT_MAX_BATCH_SIZE),
        "storage_quota": max(0, int(cfg.get("knowledge_source_storage_quota", 0) or 0)),
    }


def content_type_of(filename: str) -> str:
    guessed = mimetypes.guess_type(filename or "")[0]
    return guessed or "application/octet-stream"


def is_safe_preview(filename: str, content_type: Optional[str] = None) -> bool:
    return (content_type or content_type_of(filename)) in _PREVIEW_TYPES


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _root_guarded(fn):
    """Hold the root's shared usage lock for one public operation.

    Mirrors ``KnowledgeService._guarded``: a pending mode-switch recovery answers
    ``503 knowledge_unavailable`` rather than guessing a library, and the shared
    lock keeps a concurrent mode switch from renaming the root mid-I/O.
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if read_marker(self.workspace_root) is not None:
            raise KnowledgeUnavailableError(
                "knowledge is temporarily unavailable: a mode switch is pending recovery"
            )
        with KnowledgeRootLock(self.knowledge_dir).usage():
            return fn(self, *args, **kwargs)

    return wrapper


_UPLOAD_MUTEXES: dict = {}
_UPLOAD_MUTEX_GUARD = threading.Lock()


def _upload_mutex(root: str) -> threading.RLock:
    key = os.path.realpath(str(root))
    with _UPLOAD_MUTEX_GUARD:
        lock = _UPLOAD_MUTEXES.get(key)
        if lock is None:
            lock = threading.RLock()
            _UPLOAD_MUTEXES[key] = lock
        return lock


def _display_name(raw) -> str:
    base = os.path.basename(str(raw or "").replace("\\", "/")).strip()
    if not base:
        raise SourceValidationError("filename is required")
    cleaned = _UNSAFE_NAME.sub("_", base).strip(". ")
    if not cleaned:
        raise SourceValidationError("invalid filename")
    return cleaned[:200]


def _blob_name(name: str) -> str:
    stem, ext = os.path.splitext(name)
    stem = _BLOB_UNSAFE.sub("_", stem)[:120].strip("._-") or "blob"
    ext = _BLOB_UNSAFE.sub("", ext)[:16]
    return f"{stem}{ext}"


def _validate_request_id(request_id: str) -> str:
    request_id = (request_id or "").strip()
    if not request_id:
        return ""
    if not _REQUEST_ID.match(request_id):
        raise SourceValidationError("invalid request id")
    return request_id


class _StorageAllowance:
    """Byte allowance for one root: reserve, commit, release (design D4).

    Usage is ``sum(version.size)`` over every non-deleted source, split into
    committed and pending. Pending records are the cross-process half of a
    reservation: another process's in-flight upload is already visible here. The
    in-process ``_held`` counter covers the short window before the pending
    record exists. A limit of ``0`` means unlimited and every method no-ops.
    """

    def __init__(self, catalog: KnowledgeCatalog, limit: int):
        self.catalog = catalog
        self.limit = int(limit or 0)
        self._held = 0
        self._lock = threading.Lock()

    @property
    def enforced(self) -> bool:
        return self.limit > 0

    def usage(self) -> dict:
        usage = self.catalog.storage_usage()
        with self._lock:
            usage["reserved"] = self._held
            usage["limit"] = self.limit
            usage["remaining"] = (
                max(0, self.limit - usage["total"] - self._held)
                if self.enforced else None
            )
        return usage

    def reserve(self, nbytes: int) -> "_Reservation":
        if not self.enforced:
            return _Reservation(None, 0)
        with self._lock:
            used = self.catalog.storage_usage()["total"] + self._held
            if used + nbytes > self.limit:
                raise SourceQuotaError(
                    "storage quota exceeded for this knowledge base"
                )
            self._held += nbytes
        return _Reservation(self, nbytes)


class _Reservation:
    """Idempotent reservation handle: commit keeps the bytes, release frees them."""

    def __init__(self, allowance: Optional[_StorageAllowance], nbytes: int):
        self._allowance = allowance
        self._nbytes = nbytes
        self.state = "held" if allowance is not None else "noop"

    def _settle(self, state: str) -> None:
        if self.state == "held" and self._allowance is not None:
            with self._allowance._lock:
                self._allowance._held = max(0, self._allowance._held - self._nbytes)
        self.state = state

    def commit(self) -> None:
        self._settle("committed")

    def release(self) -> None:
        self._settle("released")


class SourceAssetService:
    """Per-root source-asset operations shared by the console and the API."""

    def __init__(self, workspace_root: str, *, actor_user_id: Optional[str] = None,
                 actor_username: Optional[str] = None,
                 tenant_id: Optional[str] = None):
        from common import state_dir

        self.workspace_root = os.path.abspath(workspace_root)
        self.knowledge_dir = str(state_dir.knowledge_dir(base=self.workspace_root))
        self.actor_user_id = actor_user_id
        self.actor_username = actor_username
        self.tenant_id = tenant_id

    # ------------------------------------------------------------------
    # Catalog access
    # ------------------------------------------------------------------
    def _catalog(self, *, create: bool = False) -> Optional[KnowledgeCatalog]:
        if create:
            return KnowledgeCatalog(self.knowledge_dir).initialize()
        return KnowledgeCatalog.open_if_exists(self.knowledge_dir)

    def _require_upload_enabled(self) -> None:
        from config import conf

        cfg = conf()
        if not cfg.get("knowledge", True):
            raise SourceUnavailableError("knowledge is disabled")
        if not cfg.get("knowledge_source_upload_enabled", False):
            raise SourceUnavailableError(
                "source upload is not enabled on this deployment"
            )

    # ------------------------------------------------------------------
    # Projections
    # ------------------------------------------------------------------
    def _project_source(self, source: dict, latest: Optional[dict] = None,
                        latest_task: Optional[dict] = None) -> dict:
        converted = source.get("active_task_id") is not None
        return {
            "source_id": source["source_id"],
            "name": source["name"],
            "category": source["category"],
            "lifecycle": source["lifecycle"],
            "latest_version": int(source.get("latest_version") or 0),
            "active_version": int(source.get("active_version") or 0),
            "active_task_id": source.get("active_task_id"),
            "target_task_id": source.get("target_task_id"),
            "converted": converted,
            # "Saved" is not "searchable": a converted, active source with no
            # published batch stays out of the effective document set.
            "searchable": converted and source["lifecycle"] == LIFECYCLE_ACTIVE,
            # The newest conversion task, so the console can tell "still working"
            # from "failed" without a second request per row. Cleanup tasks are
            # excluded: after a delete they are the only task left, and they say
            # nothing about why the source is no longer searchable.
            "latest_task": self._project_task(latest_task) if latest_task else None,
            "size": int((latest or {}).get("size") or 0),
            "created_at": source.get("created_at"),
            "updated_at": source.get("updated_at"),
        }

    @staticmethod
    def _project_version(version: dict) -> dict:
        return {
            "version": int(version["version"]),
            "original_name": version["original_name"],
            "size": int(version["size"]),
            "content_hash": version["content_hash"],
            "commit_state": version["commit_state"],
            "created_at": version["created_at"],
            "ext": os.path.splitext(version["original_name"])[1].lower(),
        }

    @staticmethod
    def _project_task(task: dict) -> dict:
        return {
            "task_id": task["task_id"],
            "task_type": task["task_type"],
            "target_version": task.get("target_version"),
            "status": task["status"],
            "stage": task.get("stage"),
            "error": task.get("error"),
            "manifest": task.get("manifest"),
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
        }

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    @_root_guarded
    def list_sources(self) -> dict:
        catalog = self._catalog()
        if catalog is None:
            return {"sources": [], "registered": False, "limits": source_limits()}
        sources = []
        for source in catalog.list_sources():
            latest = (
                catalog.get_version(source["source_id"], int(source["latest_version"]))
                if int(source.get("latest_version") or 0) else None
            )
            sources.append(self._project_source(
                source, latest, self._latest_conversion_task(catalog, source["source_id"])))
        return {"sources": sources, "registered": True, "limits": source_limits()}

    @staticmethod
    def _latest_conversion_task(catalog: KnowledgeCatalog,
                                source_id: str) -> Optional[dict]:
        """The newest non-cleanup task for a source, or ``None``.

        ``list_tasks`` is ordered oldest-first, so the last conversion task is
        the current one. A cleanup task is skipped: it reports the state of the
        files, not of the conversion the console is describing.
        """
        tasks = [t for t in catalog.list_tasks(source_id=source_id)
                 if t["task_type"] != "cleanup"]
        return tasks[-1] if tasks else None

    @_root_guarded
    def get_detail(self, source_id: str) -> dict:
        catalog = self._catalog()
        source = catalog.get_source(source_id) if catalog else None
        if source is None or source["lifecycle"] == LIFECYCLE_DELETED:
            raise SourceNotFoundError("source not found")
        versions = catalog.list_versions(source_id)
        tasks = catalog.list_tasks(source_id=source_id)
        latest = next(
            (v for v in versions if int(v["version"]) == int(source["latest_version"])),
            None,
        )
        latest_task = self._latest_conversion_task(catalog, source_id)
        return {
            "source": self._project_source(source, latest, latest_task),
            "versions": [self._project_version(v) for v in versions],
            "tasks": [self._project_task(t) for t in tasks],
            "limits": source_limits(),
        }

    @_root_guarded
    def download(self, source_id: str, version: Optional[int] = None) -> dict:
        """Resolve an original for streaming; never returns a host path to the client.

        A deleted source is not readable, and a version that is not ``committed``
        is not a durable original — both answer not-found so no partial or
        already-invalidated bytes are served.
        """
        catalog = self._catalog()
        source = catalog.get_source(source_id) if catalog else None
        if source is None or source["lifecycle"] == LIFECYCLE_DELETED:
            raise SourceNotFoundError("source not found")
        target = int(version) if version else int(source["latest_version"] or 0)
        if target <= 0:
            raise SourceNotFoundError("source has no original version")
        record = catalog.get_version(source_id, target)
        if record is None or record["commit_state"] != COMMIT_COMMITTED:
            raise SourceNotFoundError("original version not found")

        root = Path(self.knowledge_dir).resolve()
        file_path = (root / record["original_path"]).resolve()
        managed = Path(catalog.originals_dir).resolve()
        if not _is_within(file_path, managed) or not file_path.is_file():
            raise SourceNotFoundError("original version not found")

        content_type = content_type_of(record["original_name"])
        return {
            "path": str(file_path),
            "filename": record["original_name"],
            "content_type": content_type,
            "size": int(record["size"]),
            "version": target,
            "source_id": source_id,
            "name": source["name"],
            "preview": is_safe_preview(record["original_name"], content_type),
        }

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------
    @_root_guarded
    def save_files(self, files: Iterable[dict], *, category: str = "",
                   conflict: str = "ask", target_source_id: Optional[str] = None,
                   expected_version: Optional[int] = None,
                   request_id: str = "") -> dict:
        """Stage, verify and publish every file of one upload request.

        A batch-level refusal (too many files, batch too large, bad request id)
        raises before anything is written. A per-file failure is reported in
        ``results`` and never consumes capacity or leaves a pending record.
        """
        self._require_upload_enabled()
        if not isinstance(files, list) or not files:
            raise SourceValidationError("no files provided")
        limits = source_limits()
        if len(files) > limits["max_files"]:
            raise SourceQuotaError(f"too many files: max {limits['max_files']}")
        if conflict not in ("ask", "new", "update"):
            raise SourceValidationError("invalid conflict strategy")
        request_id = _validate_request_id(request_id)
        category = (category or "").strip().strip("/")

        results = []
        saved = reused = conflicts = failed = 0
        total_size = 0
        with _upload_mutex(self.knowledge_dir):
            catalog = self._catalog(create=True)
            catalog.ensure_internal_dirs()
            allowance = _StorageAllowance(catalog, limits["storage_quota"])
            for index, item in enumerate(files):
                name = ""
                try:
                    if not isinstance(item, dict):
                        raise SourceValidationError("invalid file entry")
                    name = _display_name(item.get("filename"))
                    content = item.get("content")
                    if isinstance(content, str):
                        content = content.encode("utf-8")
                    if not isinstance(content, (bytes, bytearray)):
                        raise SourceValidationError("file content is required")
                    content = bytes(content)
                    size = len(content)
                    if size > limits["max_file_size"]:
                        raise SourceQuotaError(
                            f"file too large: max {limits['max_file_size']} bytes"
                        )
                    total_size += size
                    if total_size > limits["max_batch_size"]:
                        raise SourceQuotaError("upload batch too large")

                    result = self._save_one(
                        catalog, allowance, name=name, content=content,
                        digest=sha256_bytes(content), category=category,
                        conflict=conflict, target_source_id=target_source_id,
                        expected_version=expected_version,
                        request_id=request_id, index=index,
                    )
                except SourceError as exc:
                    failed += 1
                    results.append({
                        "filename": name, "status": "failed",
                        "code": exc.code, "reason": str(exc),
                    })
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad file must not kill the batch
                    logger.error(f"[SourceAssets] upload failed for {name!r}: {exc}")
                    failed += 1
                    results.append({
                        "filename": name, "status": "failed",
                        "code": "source_error", "reason": str(exc),
                    })
                    continue

                status = result["status"]
                if status == "saved":
                    saved += 1
                elif status == "reused":
                    reused += 1
                else:
                    conflicts += 1
                results.append(result)

        return {
            "results": results,
            "saved": saved,
            "reused": reused,
            "conflict": conflicts,
            "failed": failed,
            "request_id": request_id,
            "category": category,
            "limits": limits,
            "usage": allowance.usage(),
        }

    def _save_one(self, catalog: KnowledgeCatalog, allowance: _StorageAllowance,
                  *, name: str, content: bytes, digest: str, category: str,
                  conflict: str, target_source_id: Optional[str],
                  expected_version: Optional[int], request_id: str,
                  index: int) -> dict:
        request_key = f"{request_id}\x1f{index}\x1f{name}" if request_id else None
        # Reserve before the decision: begin_upload creates the pending version
        # record, and a reservation taken afterwards would count that same
        # record twice (once as pending storage, once as this request's hold).
        reservation = allowance.reserve(len(content))
        try:
            decision = catalog.begin_upload(
                name=name, category=category, content_hash=digest,
                blob_name=_blob_name(name), size=len(content),
                request_key=request_key, conflict=conflict,
                target_source_id=target_source_id,
                expected_version=expected_version,
                created_by=self.actor_user_id or "",
            )
        except Exception:
            reservation.release()
            raise
        outcome = decision["outcome"]
        publishes = outcome == "reserved" or (
            outcome == "reused" and decision["version"]["commit_state"] == COMMIT_PENDING)
        if not publishes:
            reservation.release()

        if outcome == "request_conflict":
            return {
                "filename": name, "status": "conflict",
                "reason": "request_key_conflict",
                "message": "this request id was already used for different content",
                "source_id": (decision.get("source") or {}).get("source_id"),
                "version": decision["version"]["version"],
            }
        if outcome == "stale_version":
            source = decision["source"]
            return {
                "filename": name, "status": "conflict",
                "reason": "stale_version",
                "message": "the source changed; refresh and retry",
                "source_id": source["source_id"],
                "latest_version": int(source["latest_version"]),
            }
        if outcome == "not_found":
            raise SourceNotFoundError("target source not found")
        if outcome == "name_conflict":
            existing = decision["source"]
            return {
                "filename": name, "status": "conflict",
                "reason": "name_conflict",
                "message": "a source with this name already exists",
                "existing": {
                    "source_id": existing["source_id"],
                    "name": existing["name"],
                    "category": existing["category"],
                    "latest_version": int(existing["latest_version"]),
                },
            }

        source = decision["source"]
        version = decision["version"]

        if outcome == "reused" and version["commit_state"] == COMMIT_COMMITTED:
            latest = catalog.get_version(source["source_id"],
                                         int(source["latest_version"]))
            return {
                "filename": name, "status": "reused",
                "reason": "duplicate_content",
                "message": "identical content is already stored in this knowledge base",
                **self._receipt(source, version, latest, digest),
            }

        # From here we must durably place the bytes. Any failure removes the
        # pending record so it neither claims the hash nor keeps the root busy.
        try:
            self._publish_blob(catalog, version, content, digest)
            reservation.commit()
        except Exception as exc:
            reservation.release()
            self._discard_failed_attempt(
                catalog, source, version,
                created_source=bool(decision.get("created_source")),
            )
            if isinstance(exc, SourceError):
                raise
            raise SourceValidationError(str(exc)) from exc

        # Re-read the source: begin_upload returned the row from *before* its own
        # latest_version update, so projecting it would report version 0 for a
        # first upload and a stale number for an update.
        source = catalog.get_source(source["source_id"])
        latest = catalog.get_version(source["source_id"], int(source["latest_version"]))
        return {
            "filename": name, "status": "saved",
            **self._receipt(source, version, latest, digest),
        }
    def _publish_blob(self, catalog: KnowledgeCatalog, version: dict,
                      content: bytes, digest: str) -> None:
        """Write the blob to staging, verify it, then atomically publish it."""
        final = Path(catalog.knowledge_root) / version["original_path"]
        blob = os.path.basename(version["original_path"])
        if final.exists():
            # A resumed pending version may already have its bytes on disk.
            if final.stat().st_size == len(content) and sha256_file(final) == digest:
                catalog.set_version_commit_state(version["version_id"], COMMIT_COMMITTED)
                return
            raise SourceValidationError("existing blob does not match the upload")

        final.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(catalog.staging_dir) / version["version_id"]
        staging.mkdir(parents=True, exist_ok=True)
        temp = staging / blob
        try:
            temp.write_bytes(content)
            if temp.stat().st_size != len(content) or sha256_file(temp) != digest:
                raise SourceValidationError("staged bytes failed hash verification")
            os.replace(temp, final)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if final.stat().st_size != len(content) or sha256_file(final) != digest:
            raise SourceValidationError("published blob failed hash verification")
        catalog.set_version_commit_state(version["version_id"], COMMIT_COMMITTED)
        shutil.rmtree(staging, ignore_errors=True)

    def _discard_failed_attempt(self, catalog: KnowledgeCatalog, source: dict,
                                version: dict, *, created_source: bool) -> None:
        """Undo an unpublished attempt: record, partial blob, then empty source."""
        try:
            catalog.delete_version_record(version["version_id"])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[SourceAssets] could not drop pending version: {exc}")
        try:
            final = Path(catalog.knowledge_root) / version["original_path"]
            if final.exists() and sha256_file(final) != version["content_hash"]:
                final.unlink()
            parent = final.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[SourceAssets] could not clean partial blob: {exc}")
        try:
            shutil.rmtree(Path(catalog.staging_dir) / version["version_id"],
                          ignore_errors=True)
        except Exception:  # pragma: no cover - defensive
            pass
        if created_source:
            try:
                catalog.delete_source_record(source["source_id"])
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"[SourceAssets] could not drop empty source: {exc}")

    def _receipt(self, source: dict, version: dict, latest: Optional[dict],
                 digest: str) -> dict:
        return {
            "source": self._project_source(source, latest),
            "source_id": source["source_id"],
            "version": int(version["version"]),
            "latest_version": int(source["latest_version"]),
            "content_hash": digest,
            "size": int(version["size"]),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @_root_guarded
    def set_lifecycle(self, source_id: str, action: str) -> dict:
        catalog = self._catalog(create=True)
        source = catalog.get_source(source_id)
        if source is None:
            raise SourceNotFoundError("source not found")

        if action == "disable":
            if source["lifecycle"] == LIFECYCLE_DELETED:
                raise SourceNotFoundError("source not found")
            catalog.set_lifecycle(source_id, LIFECYCLE_DISABLED)
            self._cancel_tasks(catalog, source_id, {"convert"})
            self._audit("knowledge.source.disable", f"source:{source_id}", "success")
            return self._lifecycle_result(catalog, source_id)

        if action == "enable":
            if source["lifecycle"] == LIFECYCLE_DELETED:
                raise SourceNotFoundError("source not found")
            if source["lifecycle"] != LIFECYCLE_ACTIVE:
                catalog.set_lifecycle(source_id, LIFECYCLE_ACTIVE)
            self._audit("knowledge.source.enable", f"source:{source_id}", "success")
            return self._lifecycle_result(catalog, source_id)

        if action == "delete":
            if source["lifecycle"] == LIFECYCLE_DELETED:
                # Deletion is effective, but its cleanup may still be pending.
                return self._lifecycle_result(catalog, source_id)
            catalog.set_lifecycle(source_id, LIFECYCLE_DELETED)
            self._cancel_tasks(catalog, source_id, {"convert"})
            task = catalog.create_task(
                source_id, "cleanup", target_version=int(source["latest_version"]),
                principal=self.actor_user_id or "",
            )
            catalog.set_target_task(source_id, task["task_id"])
            self._audit("knowledge.source.delete", f"source:{source_id}", "success")
            # Run the cleanup inline: the request already holds the usage lock,
            # and a failure leaves the task queued so the root stays busy and
            # the operation is retryable instead of half-reported as done.
            cleanup = self._run_cleanup(safe=True)
            result = self._lifecycle_result(catalog, source_id)
            result["cleanup_task_id"] = task["task_id"]
            result["cleanup"] = cleanup
            return result

        raise SourceValidationError(f"unknown lifecycle action: {action}")

    def _lifecycle_result(self, catalog: KnowledgeCatalog, source_id: str) -> dict:
        source = catalog.get_source(source_id)
        latest = (
            catalog.get_version(source_id, int(source["latest_version"]))
            if int(source.get("latest_version") or 0) else None
        )
        pending_cleanup = [
            self._project_task(t)
            for t in catalog.list_tasks(source_id=source_id, statuses=(TASK_QUEUED, TASK_RUNNING))
            if t["task_type"] == "cleanup"
        ]
        return {
            "source": self._project_source(source, latest),
            "cleanup_pending": bool(pending_cleanup),
            "tasks": [self._project_task(t) for t in catalog.list_tasks(source_id=source_id)],
        }

    def _cancel_tasks(self, catalog: KnowledgeCatalog, source_id: str,
                      task_types: set) -> int:
        cancelled = 0
        for task in catalog.list_tasks(source_id=source_id,
                                       statuses=(TASK_QUEUED, TASK_RUNNING)):
            if task["task_type"] not in task_types:
                continue
            catalog.update_task(task["task_id"], status=TASK_CANCELLED,
                                stage=None, error=None)
            cancelled += 1
        if cancelled:
            catalog.set_target_task(source_id, None)
        return cancelled

    def _run_cleanup(self, *, safe: bool) -> dict:
        from agent.knowledge import runner

        try:
            return runner.process_pending_tasks(self.knowledge_dir)
        except Exception as exc:
            if safe:
                logger.error(f"[SourceAssets] inline cleanup failed: {exc}")
                return {"processed": 0, "failed": 1, "error": str(exc)}
            raise

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    @_root_guarded
    def list_tasks(self, source_id: str) -> dict:
        catalog = self._catalog()
        source = catalog.get_source(source_id) if catalog else None
        if source is None:
            raise SourceNotFoundError("source not found")
        return {"tasks": [self._project_task(t)
                          for t in catalog.list_tasks(source_id=source_id)]}

    @_root_guarded
    def request_conversion(self, source_id: str) -> dict:
        """Ask for a conversion, or refuse with the reason it cannot run.

        Phase C owns the executor; there is deliberately no queue-and-hope path
        here. Creating a ``convert`` task the runner cannot pick up would leave
        the root reporting busy forever, which blocks mode switches -- a much
        worse failure than an honest refusal. Until the converter lands this
        always raises, and the console shows the projected reason instead of a
        button that silently does nothing.
        """
        catalog = self._catalog()
        source = catalog.get_source(source_id) if catalog else None
        if source is None or source["lifecycle"] == LIFECYCLE_DELETED:
            raise SourceNotFoundError("source not found")
        self._require_upload_enabled()
        from agent.knowledge.capabilities import knowledge_capabilities

        conversion = knowledge_capabilities(can_write=True)["conversion"]
        reason = "" if conversion["available"] else conversion["reason"]
        raise SourceUnavailableError(
            reason or "document conversion is not implemented yet")

    @_root_guarded
    def retry_task(self, task_id: str) -> dict:
        catalog = self._catalog(create=True)
        task = catalog.get_task(task_id)
        if task is None:
            raise SourceNotFoundError("task not found")
        if task["task_type"] != "cleanup":
            # Conversion retry arrives with the converter (phase C); refusing
            # here keeps a queued convert task from blocking mode switches.
            raise SourceUnavailableError("conversion is not available")
        if task["status"] == TASK_RUNNING:
            return {"task": self._project_task(task),
                    "cleanup": {"processed": 0, "skipped": "already_running"}}
        # Requeue first: a cleanup that failed earlier is left "queued" on
        # purpose (the root must stay busy), so queued alone does not mean the
        # request is already in flight.
        catalog.update_task(task_id, status=TASK_QUEUED, stage=None, error=None)
        cleanup = self._run_cleanup(safe=False)
        return {"task": self._project_task(catalog.get_task(task_id)),
                "cleanup": cleanup}

    @_root_guarded
    def cancel_task(self, task_id: str) -> dict:
        catalog = self._catalog(create=True)
        task = catalog.get_task(task_id)
        if task is None:
            raise SourceNotFoundError("task not found")
        if task["task_type"] != "convert":
            # Cancelling a cleanup would unblock a mode switch while the
            # deleted source's files are still on disk.
            raise SourceValidationError("cleanup tasks cannot be cancelled")
        if task["status"] in (TASK_QUEUED, TASK_RUNNING):
            catalog.update_task(task_id, status=TASK_CANCELLED, stage=None,
                                error=None)
            catalog.set_target_task(task["source_id"], None)
        return {"task": self._project_task(catalog.get_task(task_id))}

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    def _audit(self, action: str, target: str, result: str,
               changes: Optional[dict] = None) -> None:
        """Best-effort business audit: a trace, never an authorization.

        Mirrors the import surface's contract: a failure to record must not
        change whether the operation happened, and the payload carries ids,
        counts and outcomes only -- never content or host paths.
        """
        try:
            from auth.service import get_identity_service

            get_identity_service().record_audit(
                action=action,
                target=target,
                actor_user_id=self.actor_user_id,
                actor_username=self.actor_username,
                tenant_id=self.tenant_id,
                redacted_changes=changes or {},
                result=result,
            )
        except Exception as exc:  # pragma: no cover - depends on deployment
            logger.debug(f"[SourceAssets] audit unavailable for {action}: {exc}")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(child), str(parent)]) == str(parent)
    except ValueError:
        return False
