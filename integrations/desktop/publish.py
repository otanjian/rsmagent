"""Durable publish ledger, reconciler and staging visibility (tasks 11.1–11.3).

A commit is three recoverable steps, never a cross-filesystem+DB atomicity
assumption:

1. ``intent``     — digest accepted; rename not yet done
2. ``renamed``    — file sits at the publish path; DB not yet committed
3. ``committed``  — transfer row, reservation and audit finished

The reconciler finishes or isolates orphans. Ordinary browse/preview consumers
call :func:`path_is_unpublished_staging` so ``desktop-staging/`` never appears
in directory listings even when it shares a filesystem root with published
inputs.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from auth.desktop_contracts import LIMITS
from integrations.desktop.errors import DesktopAccessError

STAGING_PREFIX = "desktop-staging/"
INPUTS_PREFIX = "desktop-inputs/"
RETENTION_DAYS = int(LIMITS.get("job_input_retention_days", 7))
CHUNK_MAX = int(LIMITS.get("transfer_chunk_max_bytes", 4 * 1024 * 1024))

STEP_INTENT = "intent"
STEP_RENAMED = "renamed"
STEP_COMMITTED = "committed"
STEP_ISOLATED = "isolated"


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, secrets.token_urlsafe(18))


def path_is_unpublished_staging(path: Any) -> bool:
    """True when a filesystem path is under an unpublished staging tree.

    Used by browse/tree/search/raw/preview admission so an orphan rename or an
    in-flight chunk file cannot be discovered by ordinary directory scans
    (task 11.2 / F12).
    """
    text = "" if path is None else str(path)
    if not text:
        return False
    norm = text.replace("\\", "/")
    return ("/%s" % STAGING_PREFIX.rstrip("/")) in ("/" + norm) or norm.startswith(
        STAGING_PREFIX)


def path_is_desktop_input(path: Any) -> bool:
    text = "" if path is None else str(path)
    if not text:
        return False
    norm = text.replace("\\", "/")
    return ("/%s" % INPUTS_PREFIX.rstrip("/")) in ("/" + norm) or norm.startswith(
        INPUTS_PREFIX)


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK_MAX)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


class PublishService:
    """Ledger writer + reconciler bound to one identity database."""

    def __init__(self, identity_service, *, staging_root: Optional[Path] = None
                 ) -> None:
        self._svc = identity_service
        self._staging_root = Path(staging_root) if staging_root else None

    def _root(self) -> Path:
        if self._staging_root is not None:
            return self._staging_root.resolve()
        import tempfile
        base = Path(tempfile.gettempdir()) / "cow-desktop-staging"
        base.mkdir(parents=True, exist_ok=True)
        return base.resolve()

    def abs_of(self, storage_rel: str) -> Path:
        root = self._root()
        target = (root / storage_rel).resolve()
        if not str(target).startswith(str(root) + os.sep) and target != root:
            raise DesktopAccessError(
                "publish path escaped root", "invalid_request", 400)
        return target

    # -- ledger steps (11.1) ------------------------------------------------

    def begin_intent(
            self, con, *, transfer: Dict[str, Any], sha256: str,
            artifact_rel: str, now: Optional[int] = None) -> Dict[str, Any]:
        """Persist the verifying intent before any rename."""
        now = now or _now()
        existing = con.execute(
            "SELECT * FROM desktop_publish_ledger WHERE transfer_id=?",
            (transfer["id"],)).fetchone()
        if existing:
            row = dict(existing)
            if row["step"] == STEP_COMMITTED:
                return row
            return row
        ledger_id = _new_id("pub")
        con.execute(
            "INSERT INTO desktop_publish_ledger"
            " (id, transfer_id, tenant_id, user_id, sha256, staging_rel,"
            "  artifact_rel, step, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ledger_id, transfer["id"], transfer["tenant_id"],
             transfer["user_id"], sha256, transfer["storage_rel"],
             artifact_rel, STEP_INTENT, now, now))
        return dict(con.execute(
            "SELECT * FROM desktop_publish_ledger WHERE id=?",
            (ledger_id,)).fetchone())

    def mark_renamed(self, con, *, transfer_id: str,
                     now: Optional[int] = None) -> None:
        now = now or _now()
        con.execute(
            "UPDATE desktop_publish_ledger"
            " SET step=?, updated_at=? WHERE transfer_id=? AND step=?",
            (STEP_RENAMED, now, transfer_id, STEP_INTENT))

    def mark_committed(
            self, con, *, transfer_id: str, audit_ok: bool = True,
            reservation_ok: bool = True, now: Optional[int] = None) -> None:
        now = now or _now()
        con.execute(
            "UPDATE desktop_publish_ledger"
            " SET step=?, audit_ok=?, reservation_ok=?, updated_at=?,"
            "     finished_at=?"
            " WHERE transfer_id=? AND step IN (?, ?)",
            (STEP_COMMITTED, 1 if audit_ok else 0, 1 if reservation_ok else 0,
             now, now, transfer_id, STEP_INTENT, STEP_RENAMED))

    def isolate(self, con, *, transfer_id: str, reason: str,
                now: Optional[int] = None) -> None:
        now = now or _now()
        con.execute(
            "UPDATE desktop_publish_ledger"
            " SET step=?, isolate_reason=?, updated_at=?, finished_at=?"
            " WHERE transfer_id=? AND step != ?",
            (STEP_ISOLATED, reason, now, now, transfer_id, STEP_COMMITTED))

    # -- reconciler (11.3) --------------------------------------------------

    def reconcile(self, *, transfer_id: Optional[str] = None,
                  audit_available: bool = True) -> List[Dict[str, Any]]:
        """Finish or isolate incomplete publish rows. Idempotent; never
        re-meters a committed reservation.
        """
        if not audit_available:
            # Contracts: audit unavailable → refuse to deliver artifact_ref.
            raise DesktopAccessError(
                "audit service unavailable", "audit_unavailable", 503)

        clauses = ["step IN (?, ?)"]
        params: List[Any] = [STEP_INTENT, STEP_RENAMED]
        if transfer_id:
            clauses.append("transfer_id=?")
            params.append(transfer_id)
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_publish_ledger WHERE %s" % (
                " AND ".join(clauses)),
            tuple(params))
        results = []
        for raw in rows:
            results.append(self._reconcile_one(dict(raw)))
        return results

    def _reconcile_one(self, ledger: Dict[str, Any]) -> Dict[str, Any]:
        transfer_rows = self._svc._store.execute(
            "SELECT * FROM desktop_transfers WHERE id=?",
            (ledger["transfer_id"],))
        if not transfer_rows:
            con = self._svc._tx()
            with con:
                self.isolate(con, transfer_id=ledger["transfer_id"],
                             reason="transfer_missing")
                con.commit()
            return {"transfer_id": ledger["transfer_id"], "action": "isolated",
                    "reason": "transfer_missing"}

        transfer = dict(transfer_rows[0])
        if transfer["state"] == "committed":
            # Ledger lagged the transfer row — just close the ledger.
            con = self._svc._tx()
            with con:
                self.mark_committed(
                    con, transfer_id=transfer["id"],
                    audit_ok=True, reservation_ok=True)
                con.commit()
            return {"transfer_id": transfer["id"], "action": "ledger_closed"}

        artifact = self.abs_of(ledger["artifact_rel"])
        staging = self.abs_of(ledger["staging_rel"])
        now = _now()

        if ledger["step"] == STEP_INTENT:
            # Rename may or may not have happened. Prefer completing the rename
            # when staging still exists; otherwise verify the artifact.
            if staging.is_file():
                artifact.parent.mkdir(parents=True, exist_ok=True)
                if file_sha256(staging) != ledger["sha256"]:
                    con = self._svc._tx()
                    with con:
                        self.isolate(con, transfer_id=transfer["id"],
                                     reason="digest_mismatch")
                        con.execute(
                            "UPDATE desktop_transfers"
                            " SET state='failed', error_code='checksum_mismatch',"
                            "     error_message=?, updated_at=?, terminal_at=?"
                            " WHERE id=?",
                            ("reconciler digest mismatch", now, now,
                             transfer["id"]))
                        con.commit()
                    return {"transfer_id": transfer["id"],
                            "action": "isolated", "reason": "digest_mismatch"}
                os.replace(str(staging), str(artifact))
                con = self._svc._tx()
                with con:
                    self.mark_renamed(con, transfer_id=transfer["id"], now=now)
                    con.commit()
                ledger["step"] = STEP_RENAMED
            elif artifact.is_file():
                if file_sha256(artifact) != ledger["sha256"]:
                    con = self._svc._tx()
                    with con:
                        self.isolate(con, transfer_id=transfer["id"],
                                     reason="digest_mismatch")
                        con.commit()
                    return {"transfer_id": transfer["id"],
                            "action": "isolated", "reason": "digest_mismatch"}
                con = self._svc._tx()
                with con:
                    self.mark_renamed(con, transfer_id=transfer["id"], now=now)
                    con.commit()
                ledger["step"] = STEP_RENAMED
            else:
                con = self._svc._tx()
                with con:
                    self.isolate(con, transfer_id=transfer["id"],
                                 reason="missing_bytes")
                    con.execute(
                        "UPDATE desktop_transfers"
                        " SET state='failed', error_code='checksum_mismatch',"
                        "     error_message=?, updated_at=?, terminal_at=?"
                        " WHERE id=?",
                        ("reconciler missing bytes", now, now, transfer["id"]))
                    if transfer.get("reservation_id"):
                        con.execute(
                            "UPDATE desktop_storage_reservations"
                            " SET state='released', released_at=?, updated_at=?"
                            " WHERE id=? AND state='reserved'",
                            (now, now, transfer["reservation_id"]))
                    con.commit()
                return {"transfer_id": transfer["id"],
                        "action": "isolated", "reason": "missing_bytes"}

        if ledger["step"] == STEP_RENAMED or (
                ledger["step"] == STEP_INTENT and artifact.is_file()):
            if not artifact.is_file() or file_sha256(artifact) != ledger["sha256"]:
                con = self._svc._tx()
                with con:
                    self.isolate(con, transfer_id=transfer["id"],
                                 reason="digest_mismatch")
                    con.commit()
                return {"transfer_id": transfer["id"],
                        "action": "isolated", "reason": "digest_mismatch"}
            # Finish DB commit without re-metering: reservation reserved→committed
            # only when still reserved.
            con = self._svc._tx()
            with con:
                con.execute(
                    "UPDATE desktop_transfers"
                    " SET state='committed', artifact_ref=?, storage_rel=?,"
                    "     sha256=?, updated_at=?, terminal_at=?"
                    " WHERE id=? AND state != 'committed'",
                    (ledger["artifact_rel"], ledger["artifact_rel"],
                     ledger["sha256"], now, now, transfer["id"]))
                if transfer.get("reservation_id"):
                    con.execute(
                        "UPDATE desktop_storage_reservations"
                        " SET state='committed', updated_at=?"
                        " WHERE id=? AND state='reserved'",
                        (now, transfer["reservation_id"]))
                self.mark_committed(
                    con, transfer_id=transfer["id"],
                    audit_ok=True, reservation_ok=True, now=now)
                self._svc._audit.record(
                    actor_user_id=transfer["user_id"],
                    actor_username="",
                    action="desktop.transfer.reconcile",
                    target="desktop.transfer:%s" % transfer["id"],
                    redacted_changes={"artifact_ref": ledger["artifact_rel"]},
                    result="success", con=con)
                con.commit()
            return {"transfer_id": transfer["id"], "action": "committed"}

        return {"transfer_id": transfer["id"], "action": "noop",
                "step": ledger["step"]}

    # -- run input retention (11.7) -----------------------------------------

    def remember_run_input(
            self, *, transfer_id: str, tenant_id: str, user_id: str,
            agent_id: str, run_id: str, artifact_rel: str,
            source_version: str) -> Dict[str, Any]:
        now = _now()
        retain = now + max(1, RETENTION_DAYS) * 86400
        row_id = _new_id("rin")
        con = self._svc._tx()
        with con:
            con.execute(
                "INSERT INTO desktop_run_inputs"
                " (id, transfer_id, tenant_id, user_id, agent_id, run_id,"
                "  artifact_rel, source_version, retained_until, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row_id, transfer_id, tenant_id, user_id, agent_id, run_id,
                 artifact_rel, source_version, retain, now))
            con.commit()
        return {"id": row_id, "retained_until": retain,
                "artifact_rel": artifact_rel}

    def delete_run_input(
            self, *, token_user_id: str, input_id: str) -> Dict[str, Any]:
        """Owner-only delete of a committed input; releases stock occupancy."""
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_run_inputs WHERE id=?", (input_id,))
        if not rows:
            raise DesktopAccessError(
                "input not found", "resource_not_found", 404)
        row = dict(rows[0])
        if row["user_id"] != token_user_id:
            raise DesktopAccessError(
                "input not found", "resource_not_found", 404)
        if row.get("deleted_at"):
            return {"id": input_id, "deleted": True, "idempotent": True}
        now = _now()
        con = self._svc._tx()
        with con:
            con.execute(
                "UPDATE desktop_run_inputs SET deleted_at=? WHERE id=?",
                (now, input_id))
            # Release committed reservation stock for the underlying transfer
            # only when no other live run-input still references it.
            remaining = con.execute(
                "SELECT COUNT(*) AS c FROM desktop_run_inputs"
                " WHERE transfer_id=? AND deleted_at IS NULL AND id != ?",
                (row["transfer_id"], input_id)).fetchone()["c"]
            if remaining == 0:
                xfer = con.execute(
                    "SELECT reservation_id FROM desktop_transfers WHERE id=?",
                    (row["transfer_id"],)).fetchone()
                if xfer and xfer["reservation_id"]:
                    con.execute(
                        "UPDATE desktop_storage_reservations"
                        " SET state='released', released_at=?, updated_at=?"
                        " WHERE id=? AND state='committed'",
                        (now, now, xfer["reservation_id"]))
            con.commit()
        try:
            path = self.abs_of(row["artifact_rel"])
            if path.is_file():
                path.unlink()
        except (DesktopAccessError, OSError):
            pass
        return {"id": input_id, "deleted": True}

    def purge_expired_staging(self) -> int:
        """Drop expired unpublished transfers and release their reservations."""
        now = _now()
        rows = self._svc._store.execute(
            "SELECT * FROM desktop_transfers"
            " WHERE state IN ('reserved','receiving')"
            " AND expires_at <= ?",
            (now,))
        count = 0
        for raw in rows:
            row = dict(raw)
            con = self._svc._tx()
            with con:
                con.execute(
                    "UPDATE desktop_transfers"
                    " SET state='expired', updated_at=?, terminal_at=?,"
                    "     error_code='transfer_expired'"
                    " WHERE id=? AND state IN ('reserved','receiving')",
                    (now, now, row["id"]))
                if row.get("reservation_id"):
                    con.execute(
                        "UPDATE desktop_storage_reservations"
                        " SET state='released', released_at=?, updated_at=?"
                        " WHERE id=? AND state='reserved'",
                        (now, now, row["reservation_id"]))
                con.commit()
            try:
                path = self.abs_of(row["storage_rel"])
                if path.is_file():
                    path.unlink()
            except (DesktopAccessError, OSError):
                pass
            count += 1
        return count


_SERVICES: Dict[str, PublishService] = {}


def service_for(identity_service, **kwargs) -> PublishService:
    key = getattr(identity_service._store, "db_path", "") or ""
    if kwargs:
        service = PublishService(identity_service, **kwargs)
        _SERVICES[key] = service
        return service
    service = _SERVICES.get(key)
    if service is None:
        service = PublishService(identity_service)
        _SERVICES[key] = service
    return service


def reset_services() -> None:
    _SERVICES.clear()
