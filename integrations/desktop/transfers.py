"""Chunked desktop file transfers and stock-based storage reservations.

Change ``add-desktop-remote-web-workbench`` (tasks 10.1–10.5). Authorization
still lives in :mod:`access`; this module owns:

* transfer create with idempotency on ``(command_id, source_version)``;
* sequential chunk writes with same-offset digest dedupe / conflict;
* storage reservations adapted against ``quota_limits.storage_bytes`` using
  stock + in-flight occupancy (never the auto-clearing ``quota_usage`` window);
* cancel / expire / commit transitions with unique terminal states.

Staging paths are server-constructed relative paths under an injected staging
root; absolute client paths never appear in the database or public projection.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from auth.desktop_contracts import LIMITS, TRANSFERS
from integrations.desktop.access import AccessService
from integrations.desktop.errors import DesktopAccessError

CHUNK_MAX = int(LIMITS.get("transfer_chunk_max_bytes", 4 * 1024 * 1024))
FILE_MAX = int(LIMITS.get("file_max_bytes", 512 * 1024 * 1024))
PARALLEL_TRANSFERS = int(LIMITS.get("parallel_transfers_per_device", 2))
TRANSFER_TIMEOUT = int(LIMITS.get("transfer_timeout_seconds", 1800))
STAGING_TTL_HOURS = int(LIMITS.get("staging_ttl_hours", 24))

_ACTIVE = frozenset({"reserved", "receiving", "verifying", "publishing"})
_TERMINAL = frozenset(TRANSFERS["terminal_states"])
_QUOTA_METRIC = "storage_bytes"

AUDIT_CREATE = "desktop.transfer.create"
AUDIT_CHUNK = "desktop.transfer.chunk"
AUDIT_CANCEL = "desktop.transfer.cancel"
AUDIT_COMMIT = "desktop.transfer.commit"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")
_FILENAME_MAX = 180


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, secrets.token_urlsafe(18))


def safe_filename(raw: Any) -> str:
    """Server-side name construction: strip path components and control chars."""
    text = "" if raw is None else str(raw)
    text = text.replace("\x00", "")
    # Take the last path segment only — never trust a client-supplied tree.
    text = text.replace("\\", "/").split("/")[-1].strip()
    text = "".join(ch for ch in text if ord(ch) >= 32)
    text = _SAFE_NAME.sub("_", text).strip("._")
    if not text or text in (".", ".."):
        text = "file"
    if len(text) > _FILENAME_MAX:
        stem, dot, ext = text.rpartition(".")
        if dot and len(ext) <= 16:
            keep = _FILENAME_MAX - len(ext) - 1
            text = (stem[:keep] if keep > 0 else "file") + "." + ext
        else:
            text = text[:_FILENAME_MAX]
    return text


class TransferService:
    """Create / chunk / status / cancel / commit for one identity database."""

    def __init__(
            self, identity_service, access: Optional[AccessService] = None,
            *, staging_root: Optional[Path] = None,
            staging_root_factory: Optional[Callable[[], Path]] = None,
            quota_probe: Optional[Callable[[], None]] = None,
            audit_probe: Optional[Callable[[], None]] = None) -> None:
        self._svc = identity_service
        self._access = access or AccessService(identity_service)
        self._staging_root = staging_root
        self._staging_root_factory = staging_root_factory
        # Tests inject a probe that raises to simulate quota-store failure
        # without failing open (F11 / task 10.6).
        self._quota_probe = quota_probe
        # Tests inject a probe that raises to simulate audit unavailability
        # (task 11.3): commit must refuse before delivering artifact_ref.
        self._audit_probe = audit_probe

    def _root(self) -> Path:
        if self._staging_root is not None:
            return Path(self._staging_root)
        if self._staging_root_factory is not None:
            return Path(self._staging_root_factory())
        # Default: a process-private temp tree. Production wiring (Group 11)
        # replaces this with agent_user_work_dir-backed paths; Group 10 keeps
        # the relative storage_rel contract without depending on Agent layout.
        base = Path(tempfile.gettempdir()) / "cow-desktop-staging"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _staging_abs(self, storage_rel: str) -> Path:
        root = self._root().resolve()
        target = (root / storage_rel).resolve()
        if not str(target).startswith(str(root) + os.sep) and target != root:
            raise DesktopAccessError(
                "staging path escaped root", "invalid_request", 400)
        return target

    # -- quota stock (10.1) -------------------------------------------------

    def _stock_bytes(self, con, *, tenant_id: str, user_id: str):
        """Live occupancy: reserved + committed, never a clearing window.

        Returns ``(user_used, tenant_used)``.
        """
        row = con.execute(
            "SELECT COALESCE(SUM(reserved_bytes), 0) AS used"
            " FROM desktop_storage_reservations"
            " WHERE tenant_id=? AND user_id=? AND state IN ('reserved','committed')",
            (tenant_id, user_id)).fetchone()
        tenant_row = con.execute(
            "SELECT COALESCE(SUM(reserved_bytes), 0) AS used"
            " FROM desktop_storage_reservations"
            " WHERE tenant_id=? AND state IN ('reserved','committed')",
            (tenant_id,)).fetchone()
        return int(row["used"]), int(tenant_row["used"])

    def _reserve_in_tx(
            self, con, *, tenant_id: str, user_id: str, transfer_id: str,
            bytes_: int, now: int) -> str:
        """Atomically reserve ``bytes_`` against ``quota_limits.storage_bytes``.

        Fail-closed: any probe/storage error becomes ``quota_unavailable``.
        No configured limit still records the reservation so cancel can release
        it, but does not invent a second quota truth.
        """
        try:
            if self._quota_probe is not None:
                self._quota_probe()
            tenant_limit = con.execute(
                "SELECT hard_limit FROM quota_limits"
                " WHERE tenant_id=? AND user_id='' AND metric=?",
                (tenant_id, _QUOTA_METRIC)).fetchone()
            user_limit = con.execute(
                "SELECT hard_limit FROM quota_limits"
                " WHERE tenant_id=? AND user_id=? AND metric=?",
                (tenant_id, user_id, _QUOTA_METRIC)).fetchone()
            user_used, tenant_used = self._stock_bytes(
                con, tenant_id=tenant_id, user_id=user_id)
        except DesktopAccessError:
            raise
        except Exception as exc:
            raise DesktopAccessError(
                "quota service unavailable", "quota_unavailable", 503) from exc

        if tenant_limit and int(tenant_limit["hard_limit"]) > 0:
            if tenant_used + bytes_ > int(tenant_limit["hard_limit"]):
                raise DesktopAccessError(
                    "tenant storage quota exceeded", "quota_exceeded", 429)
        if user_limit and int(user_limit["hard_limit"]) > 0:
            if user_used + bytes_ > int(user_limit["hard_limit"]):
                raise DesktopAccessError(
                    "user storage quota exceeded", "quota_exceeded", 429)

        reservation_id = _new_id("rsv")
        con.execute(
            "INSERT INTO desktop_storage_reservations"
            " (id, tenant_id, user_id, transfer_id, reserved_bytes, state,"
            "  created_at, updated_at)"
            " VALUES (?,?,?,?,?,'reserved',?,?)",
            (reservation_id, tenant_id, user_id, transfer_id, int(bytes_),
             now, now))
        return reservation_id

    def _release_reservation(self, con, *, reservation_id: str, now: int,
                             to_state: str = "released") -> None:
        con.execute(
            "UPDATE desktop_storage_reservations"
            " SET state=?, updated_at=?, released_at=?"
            " WHERE id=? AND state='reserved'",
            (to_state, now, now if to_state == "released" else None,
             reservation_id))

    # -- create (10.2) ------------------------------------------------------

    def create_transfer(
            self, *, token: str, tenant_id: str, command_id: str,
            source_ref: Any, source_version: Any, total_bytes: Any,
            filename: Any, request_id: Optional[str] = None,
            run_id: Optional[str] = None) -> Dict[str, Any]:
        """Reserve quota and open a staging transfer. Native session only."""
        ctx = self._access.authenticate(token, require="native")
        self._access.require_tenant(ctx, tenant_id)

        cmd_rows = self._svc._store.execute(
            "SELECT * FROM desktop_commands WHERE id=?", (command_id,))
        if not cmd_rows:
            raise DesktopAccessError(
                "command not found", "resource_not_found", 404)
        command = dict(cmd_rows[0])
        if (command["user_id"] != ctx.user["id"]
                or command["tenant_id"] != tenant_id):
            raise DesktopAccessError(
                "command not found", "resource_not_found", 404)
        if command["state"] in ("cancelled", "expired", "failed"):
            raise DesktopAccessError(
                "command is not transferrable", "stale_context", 409)

        self._access.load_binding(ctx, command["binding_id"])
        self._access.verify_binding_scope(
            ctx, workspace_id=command.get("workspace_id"),
            grant_version=command.get("grant_version"))

        src_ref = ("" if source_ref is None else str(source_ref)).strip()
        src_ver = ("" if source_version is None else str(source_version)).strip()
        if not src_ref or not src_ver:
            raise DesktopAccessError(
                "source_ref and source_version are required",
                "invalid_request", 400)
        try:
            size = int(total_bytes)
        except (TypeError, ValueError):
            raise DesktopAccessError(
                "total_bytes must be an integer", "invalid_request", 400)
        if size < 0:
            raise DesktopAccessError(
                "total_bytes must be non-negative", "invalid_request", 400)
        if size > FILE_MAX:
            raise DesktopAccessError(
                "file exceeds the %d byte limit" % FILE_MAX,
                "limit_exceeded", 413)
        name = safe_filename(filename)
        req_id = (request_id or command["request_id"] or _new_id("xfer")).strip()
        now = _now()
        expires = now + max(60, STAGING_TTL_HOURS * 3600)

        con = self._svc._tx()
        with con:
            existing = con.execute(
                "SELECT * FROM desktop_transfers"
                " WHERE command_id=? AND source_version=?",
                (command_id, src_ver)).fetchall()
            if existing:
                con.commit()
                return self._public(dict(existing[0]))

            active = con.execute(
                "SELECT COUNT(*) AS c FROM desktop_transfers"
                " WHERE device_id=? AND state IN"
                " ('reserved','receiving','verifying','publishing')",
                (command["device_id"],)).fetchone()["c"]
            if active >= PARALLEL_TRANSFERS:
                raise DesktopAccessError(
                    "device transfer slots are full", "queue_full", 429)

            transfer_id = _new_id("xfer")
            storage_rel = "desktop-staging/%s/%s" % (transfer_id, name)
            # Insert the transfer row first so the reservation FK can land.
            con.execute(
                "INSERT INTO desktop_transfers"
                " (id, request_id, command_id, tenant_id, user_id, device_id,"
                "  binding_id, workspace_id, grant_version, agent_id,"
                "  business_session_id, run_id, source_ref, source_version,"
                "  total_bytes, received_bytes, filename, state, storage_rel,"
                "  expires_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,'reserved',?,?,?,?)",
                (transfer_id, req_id, command_id, tenant_id, ctx.user["id"],
                 command["device_id"], command["binding_id"],
                 command.get("workspace_id"), command.get("grant_version"),
                 command["agent_id"], command["business_session_id"],
                 run_id, src_ref, src_ver, size, name, storage_rel,
                 expires, now, now))
            reservation_id = self._reserve_in_tx(
                con, tenant_id=tenant_id, user_id=ctx.user["id"],
                transfer_id=transfer_id, bytes_=size, now=now)
            con.execute(
                "UPDATE desktop_transfers SET reservation_id=? WHERE id=?",
                (reservation_id, transfer_id))
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_CREATE,
                target="desktop.transfer:%s" % transfer_id,
                redacted_changes={
                    "command_id": command_id,
                    "total_bytes": size,
                    "filename": name,
                },
                result="success", con=con)
            con.commit()

        abs_path = self._staging_abs(storage_rel)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        # Pre-create an empty staging file so later chunk writes are appends
        # at known offsets rather than inventing a path from the client.
        if not abs_path.exists():
            abs_path.touch()
        return self.get_transfer(
            token=token, tenant_id=tenant_id, transfer_id=transfer_id)

    # -- chunks (10.3) ------------------------------------------------------

    def put_chunk(
            self, *, token: str, tenant_id: str, transfer_id: str,
            offset: int, body: bytes, content_sha256: Optional[str] = None
            ) -> Dict[str, Any]:
        """Append one sequential chunk. Native only; browsers are refused."""
        ctx = self._access.authenticate(token, require="native")
        self._access.require_tenant(ctx, tenant_id)
        if not isinstance(body, (bytes, bytearray)):
            raise DesktopAccessError(
                "chunk body must be binary", "invalid_request", 400)
        length = len(body)
        if length == 0:
            raise DesktopAccessError(
                "empty chunk", "invalid_request", 400)
        if length > CHUNK_MAX:
            raise DesktopAccessError(
                "chunk exceeds the %d byte limit" % CHUNK_MAX,
                "limit_exceeded", 413)
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            raise DesktopAccessError(
                "offset must be an integer", "invalid_request", 400)
        if offset < 0:
            raise DesktopAccessError(
                "offset must be non-negative", "invalid_request", 400)

        digest = hashlib.sha256(body).hexdigest()
        if content_sha256:
            declared = str(content_sha256).strip().lower()
            if declared != digest:
                raise DesktopAccessError(
                    "chunk digest mismatch", "checksum_mismatch", 422)

        transfer = self._load_owned_transfer(
            ctx, tenant_id=tenant_id, transfer_id=transfer_id)
        self._access.load_binding(ctx, transfer["binding_id"])
        self._access.verify_binding_scope(
            ctx, workspace_id=transfer.get("workspace_id"),
            grant_version=transfer.get("grant_version"))
        self._ensure_not_expired(transfer)

        if transfer["state"] in _TERMINAL:
            raise DesktopAccessError(
                "transfer is terminal", "stale_context", 409)
        if transfer["state"] not in ("reserved", "receiving"):
            raise DesktopAccessError(
                "transfer is not receiving chunks", "stale_context", 409)

        # Same-offset dedupe / conflict before any write.
        prior = self._svc._store.execute(
            "SELECT * FROM desktop_transfer_chunks"
            " WHERE transfer_id=? AND offset=?",
            (transfer_id, offset))
        if prior:
            row = dict(prior[0])
            if row["sha256"] == digest and int(row["length"]) == length:
                return self._public(transfer)
            raise DesktopAccessError(
                "chunk conflict at offset %d" % offset,
                "chunk_conflict", 409)

        expected = int(transfer["received_bytes"])
        if offset != expected:
            raise DesktopAccessError(
                "chunks must be sequential; expected offset %d" % expected,
                "invalid_request", 400)
        if expected + length > int(transfer["total_bytes"]):
            raise DesktopAccessError(
                "chunk would exceed total_bytes", "limit_exceeded", 413)

        abs_path = self._staging_abs(transfer["storage_rel"])
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        # Stream to disk at the declared offset; do not keep the full file in
        # memory beyond the single in-flight chunk window.
        with open(abs_path, "r+b" if abs_path.exists() else "w+b") as fh:
            fh.seek(offset)
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())

        now = _now()
        new_received = expected + length
        new_state = "receiving"
        con = self._svc._tx()
        with con:
            # Re-check under the write lock: a concurrent cancel / expire wins.
            live = con.execute(
                "SELECT * FROM desktop_transfers WHERE id=?",
                (transfer_id,)).fetchone()
            if live is None:
                raise DesktopAccessError(
                    "transfer not found", "resource_not_found", 404)
            live = dict(live)
            if live["state"] in _TERMINAL:
                raise DesktopAccessError(
                    "transfer is terminal", "stale_context", 409)
            if int(live["received_bytes"]) != expected:
                raise DesktopAccessError(
                    "chunk offset raced", "chunk_conflict", 409)
            try:
                con.execute(
                    "INSERT INTO desktop_transfer_chunks"
                    " (transfer_id, offset, length, sha256, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (transfer_id, offset, length, digest, now))
            except Exception as exc:
                # Unique PK collision under race → treat as conflict.
                raise DesktopAccessError(
                    "chunk conflict at offset %d" % offset,
                    "chunk_conflict", 409) from exc
            con.execute(
                "UPDATE desktop_transfers"
                " SET received_bytes=?, state=?, updated_at=?"
                " WHERE id=?",
                (new_received, new_state, now, transfer_id))
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_CHUNK,
                target="desktop.transfer:%s" % transfer_id,
                redacted_changes={"offset": offset, "length": length},
                result="success", con=con)
            con.commit()
        return self.get_transfer(
            token=token, tenant_id=tenant_id, transfer_id=transfer_id)

    # -- status / cancel / expire (10.5) ------------------------------------

    def get_transfer(
            self, *, token: str, tenant_id: str, transfer_id: str
            ) -> Dict[str, Any]:
        ctx = self._access.authenticate(token, require="any")
        self._access.require_tenant(ctx, tenant_id)
        transfer = self._load_owned_transfer(
            ctx, tenant_id=tenant_id, transfer_id=transfer_id)
        # Lazy expire: a GET past expires_at flips reserved/receiving rows.
        if (transfer["state"] in ("reserved", "receiving")
                and int(transfer["expires_at"]) <= _now()):
            return self._expire(transfer)
        return self._public(transfer)

    def cancel_transfer(
            self, *, token: str, tenant_id: str, transfer_id: str
            ) -> Dict[str, Any]:
        ctx = self._access.authenticate(token, require="any")
        self._access.require_tenant(ctx, tenant_id)
        transfer = self._load_owned_transfer(
            ctx, tenant_id=tenant_id, transfer_id=transfer_id)
        if transfer["state"] in _TERMINAL:
            return self._public(transfer)
        now = _now()
        con = self._svc._tx()
        with con:
            live = con.execute(
                "SELECT * FROM desktop_transfers WHERE id=?",
                (transfer_id,)).fetchone()
            if live is None:
                raise DesktopAccessError(
                    "transfer not found", "resource_not_found", 404)
            live = dict(live)
            if live["state"] in _TERMINAL:
                con.commit()
                return self._public(live)
            con.execute(
                "UPDATE desktop_transfers"
                " SET state='cancelled', updated_at=?, terminal_at=?,"
                "     error_code='cancelled', error_message=?"
                " WHERE id=? AND state IN"
                " ('reserved','receiving','verifying','publishing')",
                (now, now, "cancelled by caller", transfer_id))
            if live.get("reservation_id"):
                self._release_reservation(
                    con, reservation_id=live["reservation_id"], now=now)
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_CANCEL,
                target="desktop.transfer:%s" % transfer_id,
                redacted_changes={"previous_state": live["state"]},
                result="success", con=con)
            con.commit()
        self._cleanup_staging(live.get("storage_rel"))
        return self.get_transfer(
            token=token, tenant_id=tenant_id, transfer_id=transfer_id)

    def _expire(self, transfer: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        con = self._svc._tx()
        with con:
            live = con.execute(
                "SELECT * FROM desktop_transfers WHERE id=?",
                (transfer["id"],)).fetchone()
            if live is None:
                raise DesktopAccessError(
                    "transfer not found", "resource_not_found", 404)
            live = dict(live)
            if live["state"] in _TERMINAL:
                con.commit()
                return self._public(live)
            con.execute(
                "UPDATE desktop_transfers"
                " SET state='expired', updated_at=?, terminal_at=?,"
                "     error_code='transfer_expired',"
                "     error_message=?"
                " WHERE id=? AND state IN ('reserved','receiving')",
                (now, now, "transfer expired", transfer["id"]))
            if live.get("reservation_id"):
                self._release_reservation(
                    con, reservation_id=live["reservation_id"], now=now)
            con.commit()
        self._cleanup_staging(live.get("storage_rel"))
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_transfers WHERE id=?", (transfer["id"],))
        return self._public(dict(rows[0]))

    # -- commit (10.5 / hand-off to Group 11 publish ledger) ----------------

    def commit_transfer(
            self, *, token: str, tenant_id: str, transfer_id: str,
            total_bytes: Any, sha256: Any,
            source_version_after: Any) -> Dict[str, Any]:
        """Verify integrity and publish into a committed relative artifact.

        Group 11 owns the durable verifying/publishing ledger and reconciler;
        this path still performs the integrity / version / ownership / quota
        gates required by contracts §6 so F09–F11 can close without waiting.
        """
        ctx = self._access.authenticate(token, require="native")
        self._access.require_tenant(ctx, tenant_id)
        transfer = self._load_owned_transfer(
            ctx, tenant_id=tenant_id, transfer_id=transfer_id)
        self._access.load_binding(ctx, transfer["binding_id"])
        self._access.verify_binding_scope(
            ctx, workspace_id=transfer.get("workspace_id"),
            grant_version=transfer.get("grant_version"))
        self._ensure_not_expired(transfer)

        if transfer["state"] == "committed":
            return self._public(transfer)
        if transfer["state"] in _TERMINAL:
            raise DesktopAccessError(
                "transfer is terminal", "stale_context", 409)
        if transfer["state"] not in ("reserved", "receiving", "verifying"):
            raise DesktopAccessError(
                "transfer cannot commit from %s" % transfer["state"],
                "stale_context", 409)

        try:
            declared_total = int(total_bytes)
        except (TypeError, ValueError):
            raise DesktopAccessError(
                "total_bytes must be an integer", "invalid_request", 400)
        declared_hash = ("" if sha256 is None else str(sha256)).strip().lower()
        after = ("" if source_version_after is None
                 else str(source_version_after)).strip()
        if not declared_hash or len(declared_hash) != 64:
            raise DesktopAccessError(
                "sha256 is required", "invalid_request", 400)
        if after != transfer["source_version"]:
            raise DesktopAccessError(
                "source file changed during transfer", "file_changed", 409)
        if declared_total != int(transfer["total_bytes"]):
            raise DesktopAccessError(
                "total_bytes mismatch", "checksum_mismatch", 422)
        if int(transfer["received_bytes"]) != int(transfer["total_bytes"]):
            raise DesktopAccessError(
                "transfer is incomplete", "invalid_request", 400)

        abs_path = self._staging_abs(transfer["storage_rel"])
        if not abs_path.is_file():
            raise DesktopAccessError(
                "staging file missing", "checksum_mismatch", 422)
        hasher = hashlib.sha256()
        with open(abs_path, "rb") as fh:
            while True:
                block = fh.read(CHUNK_MAX)
                if not block:
                    break
                hasher.update(block)
        actual = hasher.hexdigest()
        if actual != declared_hash:
            raise DesktopAccessError(
                "final digest mismatch", "checksum_mismatch", 422)
        size_on_disk = abs_path.stat().st_size
        if size_on_disk != declared_total:
            raise DesktopAccessError(
                "final size mismatch", "checksum_mismatch", 422)

        now = _now()
        artifact_rel = "desktop-inputs/%s/%s" % (
            transfer_id, transfer["filename"])
        publish_abs = self._staging_abs(artifact_rel)
        publish_abs.parent.mkdir(parents=True, exist_ok=True)

        from integrations.desktop.publish import PublishService
        publisher = PublishService(
            self._svc, staging_root=self._root())

        # Optional audit probe for tests (task 11.3): when audit is unavailable
        # we must not deliver artifact_ref.
        audit_ok = True
        if getattr(self, "_audit_probe", None) is not None:
            try:
                self._audit_probe()
            except Exception as exc:
                raise DesktopAccessError(
                    "audit service unavailable", "audit_unavailable", 503
                ) from exc

        con = self._svc._tx()
        with con:
            live = con.execute(
                "SELECT * FROM desktop_transfers WHERE id=?",
                (transfer_id,)).fetchone()
            if live is None:
                raise DesktopAccessError(
                    "transfer not found", "resource_not_found", 404)
            live = dict(live)
            if live["state"] == "committed":
                con.commit()
                return self._public(live)
            if live["state"] in _TERMINAL:
                raise DesktopAccessError(
                    "transfer is terminal", "stale_context", 409)
            # Persist verifying intent BEFORE the rename (crash recovery).
            con.execute(
                "UPDATE desktop_transfers"
                " SET state='verifying', sha256=?, updated_at=?"
                " WHERE id=?",
                (actual, now, transfer_id))
            publisher.begin_intent(
                con, transfer={**live, "storage_rel": live["storage_rel"]},
                sha256=actual, artifact_rel=artifact_rel, now=now)
            con.commit()

        # Atomic rename within the same filesystem root.
        os.replace(str(abs_path), str(publish_abs))

        con = self._svc._tx()
        with con:
            live = con.execute(
                "SELECT * FROM desktop_transfers WHERE id=?",
                (transfer_id,)).fetchone()
            live = dict(live)
            if live["state"] == "committed":
                con.commit()
                return self._public(live)
            publisher.mark_renamed(con, transfer_id=transfer_id, now=now)
            con.execute(
                "UPDATE desktop_transfers"
                " SET state='publishing', updated_at=?"
                " WHERE id=?",
                (now, transfer_id))
            con.execute(
                "UPDATE desktop_transfers"
                " SET state='committed', artifact_ref=?, storage_rel=?,"
                "     sha256=?, updated_at=?, terminal_at=?"
                " WHERE id=?",
                (artifact_rel, artifact_rel, actual, now, now, transfer_id))
            if live.get("reservation_id"):
                con.execute(
                    "UPDATE desktop_storage_reservations"
                    " SET state='committed', updated_at=?"
                    " WHERE id=? AND state='reserved'",
                    (now, live["reservation_id"]))
            publisher.mark_committed(
                con, transfer_id=transfer_id, audit_ok=audit_ok,
                reservation_ok=True, now=now)
            self._svc._audit.record(
                actor_user_id=ctx.user["id"],
                actor_username=ctx.user["username"],
                action=AUDIT_COMMIT,
                target="desktop.transfer:%s" % transfer_id,
                redacted_changes={"artifact_ref": artifact_rel,
                                  "sha256": actual},
                result="success", con=con)
            con.commit()
        return self.get_transfer(
            token=token, tenant_id=tenant_id, transfer_id=transfer_id)

    # -- helpers ------------------------------------------------------------

    def _load_owned_transfer(
            self, ctx, *, tenant_id: str, transfer_id: str) -> Dict[str, Any]:
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_transfers WHERE id=?", (transfer_id,))
        if not rows:
            raise DesktopAccessError(
                "transfer not found", "resource_not_found", 404)
        row = dict(rows[0])
        if row["user_id"] != ctx.user["id"] or row["tenant_id"] != tenant_id:
            raise DesktopAccessError(
                "transfer not found", "resource_not_found", 404)
        return row

    def _ensure_not_expired(self, transfer: Dict[str, Any]) -> None:
        if (transfer["state"] in ("reserved", "receiving")
                and int(transfer["expires_at"]) <= _now()):
            self._expire(transfer)
            raise DesktopAccessError(
                "transfer expired", "transfer_expired", 410)

    def _cleanup_staging(self, storage_rel: Optional[str]) -> None:
        if not storage_rel:
            return
        try:
            path = self._staging_abs(storage_rel)
        except DesktopAccessError:
            return
        try:
            if path.is_file():
                path.unlink()
            parent = path.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

    def _public(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "request_id": row["request_id"],
            "command_id": row["command_id"],
            "state": row["state"],
            "total_bytes": int(row["total_bytes"]),
            "received_bytes": int(row["received_bytes"]),
            "acknowledged_offset": int(row["received_bytes"]),
            "chunk_size": CHUNK_MAX,
            "filename": row["filename"],
            "source_ref": row["source_ref"],
            "source_version": row["source_version"],
            "sha256": row.get("sha256"),
            "artifact_ref": row.get("artifact_ref"),
            "expires_at": int(row["expires_at"]),
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            "terminal_at": row.get("terminal_at"),
            # storage_rel is an internal server path — never returned.
        }


_SERVICES: Dict[str, TransferService] = {}


def service_for(identity_service, **kwargs) -> TransferService:
    key = getattr(identity_service._store, "db_path", "") or ""
    if kwargs:
        from integrations.desktop.access import service_for as access_for
        service = TransferService(
            identity_service, access=access_for(identity_service), **kwargs)
        _SERVICES[key] = service
        return service
    service = _SERVICES.get(key)
    if service is None:
        from integrations.desktop.access import service_for as access_for
        service = TransferService(
            identity_service, access=access_for(identity_service))
        _SERVICES[key] = service
    return service


def reset_services() -> None:
    """Test helper: drop cached services so a new staging root can bind."""
    _SERVICES.clear()
