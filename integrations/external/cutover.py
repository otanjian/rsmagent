# encoding:utf-8
"""The two steps that come after a successful import (task 12.5).

The migration itself is deliberately non-destructive: ``preflight`` and
``import`` write nothing to the legacy store, and the tests pin that, because
until the switch is flipped the legacy store is the *live* one — a migration run
that edited it while the runtime still reads it would be a data loss that looks
like a successful run. The consequences of that are settled here, in a separate,
explicit, separately-authorized pair of operations:

* :func:`redact_legacy_secrets` blanks the plaintext credentials in the legacy
  files, but only for records the ledger says were imported *and* only for slots
  whose value the new store actually references. This is the step that ends the
  window in which a credential exists in two places with two different
  protections.
* :func:`refresh_caches` drops the process-level caches that were populated from
  the store *before* the import. Without it the cutover is only visible after a
  restart: MCP discovery memos still describe the old tool set, and a freshly
  imported connection is simply missing from the listing that reads them.

Both are idempotent and both report before they act. Neither is called from
``import_connections``: an operator has to be able to import, look at
:func:`integrations.external.migration.verify`, and only then decide to make the
legacy plaintext go away.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from common.log import logger
from integrations.external.errors import ExternalConnectionError

#: Refusal codes. Named so the CLI and the tests branch on one string.
NOT_AUTHORIZED = "not_authorized"
NOT_IMPORTED = "not_imported"
SECRET_NOT_CARRIED = "secret_not_carried"
CHANGED_AFTER_IMPORT = "changed_after_import"
BAD_LEGACY_FILE = "bad_legacy_file"
VERIFY_FAILED = "verify_failed"

#: Which literal legacy fields hold each secret slot. Read off the legacy shapes
#: rather than guessed: the ERP store's own writer uses ``password`` and older
#: files use ``passwd``, and the MCP loader puts secrets in the ``env`` map
#: (stdio) or the ``headers`` map (remote). The slot names are the control
#: plane's (``password``, ``env``, ``header``) -- they are not the field names,
#: and treating them as such would silently blank nothing for an MCP header.
_ERP_SLOT_FIELDS: Dict[str, Sequence[str]] = {
    "password": ("password", "passwd"),
}
_MCP_SLOT_FIELDS: Dict[str, Sequence[str]] = {
    "env": ("env",),
    "header": ("headers",),
}

#: The fields whose *values* are secrets inside a map, rather than the field
#: itself being one.
SECRET_MAPS: Sequence[str] = ("env", "headers")

#: A redacted value is emptied, not replaced with a token. A token would be a
#: value: a loader that read it would send it as a credential, and a file that
#: still looks like a working store is one an operator will not notice is dead.
REDACTED = ""


def _migration():
    from integrations.external import migration

    return migration


def _service(identity: Any = None, service: Any = None) -> Any:
    if service is not None:
        return service
    if identity is not None:
        from integrations.external.service import ExternalConnectionService

        return ExternalConnectionService(identity)
    from integrations.external.service import get_external_connection_service

    return get_external_connection_service()


def _resolve(identity: Any = None, service: Any = None):
    """``(the identity service, its store)`` from whichever shape was passed.

    The migration's scan needs the *service*: tenant discovery comes from
    ``tenant_shared_roots`` and the ledger lives under ``_store``. Handing it a
    bare store instead does not raise -- it silently finds no tenants, reports
    no records, and reads an empty ledger, which is the worst failure mode
    available here: a cleanup that says "nothing to do" while every plaintext
    password is still on disk. So the two are resolved together, from any of the
    three objects callers in this package actually hold (the identity service,
    the external-connection service that wraps it, or the store itself).
    """
    for candidate in (identity, getattr(service, "_identity", None), service):
        if candidate is None:
            continue
        store = getattr(candidate, "_store", candidate)
        if hasattr(candidate, "tenant_shared_roots") and hasattr(store, "execute"):
            return candidate, store
    for candidate in (identity, getattr(service, "_identity", None), service):
        store = getattr(candidate, "_store", None)
        if store is not None and hasattr(store, "execute"):
            return candidate, store
    raise ExternalConnectionError("no identity store for the cutover",
                                  code=BAD_LEGACY_FILE, status=500)


# -- legacy plaintext redaction ----------------------------------------------

def _slot_fields(record: Mapping[str, Any]) -> Dict[str, Sequence[str]]:
    """``slot -> the literal legacy fields that hold it`` for this record."""
    known = (_ERP_SLOT_FIELDS if record.get("kind") == "erp"
             else _MCP_SLOT_FIELDS)
    slots = {str(slot) for slot in (record.get("secret_slots") or ())}
    return {slot: fields for slot, fields in known.items() if slot in slots}


def _secret_fields_for(record: Mapping[str, Any]) -> List[str]:
    """Every literal field name in the legacy record that holds a secret."""
    out: List[str] = []
    for fields in _slot_fields(record).values():
        for name in fields:
            if name not in out:
                out.append(name)
    return out


def _blank(record: Mapping[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
    """A copy of one legacy record with the named secret fields emptied.

    Recursive into the MCP secret maps on purpose: the credential there is a
    *value* in a map whose key is the header or variable name, so blanking the
    map itself would drop a non-secret fact (which header is used) while
    blanking the entries drops only the secret.
    """
    out = dict(record)
    for name in fields:
        if name in SECRET_MAPS:
            current = out.get(name)
            if isinstance(current, Mapping):
                out[name] = {str(k): REDACTED for k in current}
            else:
                out[name] = {}
            continue
        if name in out:
            out[name] = REDACTED
    return out


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write_atomic(path: str, document: Any) -> None:
    """Replace the file in one step, keeping its mode.

    A redaction that is interrupted half-way must not leave a file that parses
    as valid JSON holding a mixture of blanked and live credentials — that is a
    store that looks fine and works, which is the worst state to discover during
    an incident. ``os.replace`` makes the choice all-or-nothing.
    """
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:  # pragma: no cover - the caller just read the file
        mode = 0o600
    tmp = path + ".redact.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _entries_in(document: Any, fmt: str) -> List[Mapping[str, Any]]:
    """The record list inside one legacy file, whatever shape it uses."""
    if fmt == "erp_json":
        return [item for item in document if isinstance(item, Mapping)] \
            if isinstance(document, list) else []
    if isinstance(document, list):
        return [item for item in document if isinstance(item, Mapping)]
    if isinstance(document, Mapping):
        raw = (document.get("mcpServers") or document.get("mcp_servers")
               or document)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, Mapping)]
        if isinstance(raw, Mapping):
            entries: List[Mapping[str, Any]] = []
            for name, cfg in raw.items():
                if isinstance(cfg, Mapping):
                    entries.append({"name": name, **cfg})
            return entries
    return []


def _locator_file(locator: str) -> str:
    """The file a scanned record came from.

    A scan labels each record ``<path>#<index>`` so two records in one file have
    distinct locators; the file itself is the path before that suffix. Stripping
    only a trailing ``#<digits>`` keeps a ``#`` that is genuinely part of a
    filename.
    """
    return re.sub(r"#\d+$", "", str(locator or ""))


def _entry_key(entry: Mapping[str, Any], fmt: str) -> str:
    """How one on-disk entry is matched back to a scanned record."""
    if fmt == "erp_json":
        return str(entry.get("id") or "")
    return str(entry.get("name") or "")


def _collect(report: Dict[str, Any], entry: Dict[str, Any]) -> None:
    """Fold one file's outcome into the top-level report."""
    report["files"].append(entry)
    report["redacted"].extend(entry["redacted"])
    report["skipped"].extend(entry["skipped"])
    report.setdefault("already_redacted", []).extend(entry["already_redacted"])


def _is_blank(entry_value: Mapping[str, Any], fields: Iterable[str]) -> bool:
    """Whether every named secret field in this entry is already empty."""
    for name in fields:
        if name in SECRET_MAPS:
            current = entry_value.get(name)
            if isinstance(current, Mapping):
                if any(str(value) != REDACTED for value in current.values()):
                    return False
            elif current not in (None, {}, ""):
                return False
            continue
        if str(entry_value.get(name) or "") != REDACTED:
            return False
    return True


def redact_legacy_secrets(*, actor_user_id: str, identity: Any = None,
                          service: Any = None, sources: Any = None,
                          erp_files: Any = (), mcp_files: Any = (),
                          confirm: bool = False) -> Dict[str, Any]:
    """Blank the legacy plaintext for records that are provably carried over.

    Guard rails, all of them load-bearing:

    * **platform admin.** The legacy files hold other tenants' credentials; a
      tenant-scoped caller must not be able to destroy them.
    * **verified first.** The scan is the preflight scan, so a record that is
      still importable blocks nothing here but is reported as skipped: the
      redaction is only for records whose ``(source_hash, scope_key)`` the ledger
      says ``imported``. A record that was never imported keeps its password,
      because deleting it would lose the only copy.
    * **slot by slot.** A recorded import is not enough on its own: the new
      store must actually *reference* the slot. That covers the case where the
      import wrote the connection but the secret write failed — the row exists,
      the ledger says imported, and the value in the file is still the only one.
    * **``confirm`` defaults to false.** The default call is the dry run.

    Never backs up and never deletes the file: an unreadable or unparseable
    legacy file is reported and left alone, so a half-understood store is never
    edited by a step whose job is to *remove* information.
    """
    service = _service(identity, service)
    service.require_platform_admin(actor_user_id)
    subject, store = _resolve(identity, service)
    migration = _migration()

    records = migration.inventory(subject, sources=sources,
                                  erp_files=erp_files, mcp_files=mcp_files,
                                  actor_user_id=actor_user_id)
    report: Dict[str, Any] = {
        "confirmed": bool(confirm),
        "redacted": [],
        "skipped": [],
        "already_redacted": [],
        "files": [],
    }
    # Group the decisions per file first: the file is rewritten once, and a file
    # with nothing eligible is never opened for writing at all.
    per_file: Dict[str, List[Mapping[str, Any]]] = {}
    for record in records:
        locator = _locator_file(record.get("source_locator"))
        if record.get("kind") == "unknown" or not locator:
            # The scan could not parse this source at all. Nothing can be
            # decided about records it does not describe -- but the file is very
            # likely still holding plaintext, so it is named in the report
            # instead of being silently absent from it.
            report["files"].append({
                "locator": locator, "redacted": [], "skipped": [],
                "already_redacted": [], "written": False,
                "error": str((record.get("reason_detail") or {}).get("error")
                             or BAD_LEGACY_FILE),
                "note": "left untouched"})
            continue
        per_file.setdefault(locator, []).append(record)

    for locator, file_records in sorted(per_file.items()):
        fmt = str(file_records[0].get("source_format") or "")
        entry: Dict[str, Any] = {"locator": locator, "redacted": [],
                                 "skipped": [], "already_redacted": [],
                                 "written": False}
        try:
            document = _load_json(locator)
        except Exception as error:  # noqa: BLE001 - a bad file is left alone
            entry["error"] = "%s: %s" % (type(error).__name__, error)
            entry["note"] = "left untouched"
            _collect(report, entry)
            continue
        by_key = {_entry_key(item, fmt): item for item in _entries_in(document, fmt)}
        if not by_key:
            entry["error"] = BAD_LEGACY_FILE
            entry["note"] = "left untouched"
            _collect(report, entry)
            continue

        eligible: Dict[str, List[str]] = {}
        for record in file_records:
            key = str(record.get("legacy_id") or "")
            fields = _secret_fields_for(record)
            if not fields:
                # The scan declares no secret slot for this record, so there is
                # no slot the control plane could be holding. Whatever plaintext
                # the entry still has -- an unrepresentable record's password,
                # say -- has no second copy, and the report says so rather than
                # leaving the value an unexplained survivor.
                entry["skipped"].append({"legacy_id": record["legacy_id"],
                                         "reason": NOT_IMPORTED})
                continue
            ledger = migration.ledger_entry(store, record["source_hash"],
                                            record["scope_key"])
            exact = ledger is not None and ledger["result"] == "imported"
            if not exact:
                ledger = migration.ledger_by_legacy_id(
                    store, record["legacy_id"], record["scope_key"])
                if ledger is None or ledger["result"] != "imported":
                    entry["skipped"].append({"legacy_id": record["legacy_id"],
                                             "reason": NOT_IMPORTED})
                    continue
            try:
                mapping = json.loads(ledger["mapping_json"] or "{}")
            except ValueError:
                mapping = {}
            carried = migration.carried_secret_slots(
                store, str(mapping.get("connection_id") or ""))
            # Slot by slot: a slot whose value the new store does not reference
            # has no copy anywhere else, so it stays in the file.
            wanted = set(_slot_fields(record))
            missing = sorted(slot for slot in wanted if slot not in carried)
            if missing:
                entry["skipped"].append({"legacy_id": record["legacy_id"],
                                         "reason": SECRET_NOT_CARRIED,
                                         "slots": missing})
                continue
            item = by_key.get(key)
            if item is None:
                # The record was scanned from this file but no entry in it
                # matches: refusing to guess which one to blank is the only safe
                # answer, because a wrong match destroys a live credential.
                entry["skipped"].append({"legacy_id": record["legacy_id"],
                                         "reason": BAD_LEGACY_FILE})
                continue
            blank = _is_blank(item, fields)
            if not exact and not blank:
                # The identity was imported, but this content is not the content
                # that was imported -- and it still holds a live value. That is
                # a rotated credential the control plane never received, so
                # blanking it would destroy the only copy. The ledger match
                # proves too little to act on; it is reported instead.
                entry["skipped"].append({"legacy_id": record["legacy_id"],
                                         "reason": CHANGED_AFTER_IMPORT,
                                         "fields": sorted(fields)})
                continue
            if blank:
                # Already done by a previous run. Reported rather than counted
                # as a redaction, so a second run is visibly a no-op instead of
                # looking like it blanked the same five passwords twice.
                entry["already_redacted"].append(
                    {"legacy_id": record["legacy_id"], "fields": sorted(fields)})
                continue
            eligible[key] = fields
            entry["redacted"].append({"legacy_id": record["legacy_id"],
                                      "fields": sorted(fields)})

        if not eligible:
            entry["note"] = "nothing left to redact in this file"
            _collect(report, entry)
            continue
        if not confirm:
            entry["note"] = "dry run"
            _collect(report, entry)
            continue

        entries = _entries_in(document, fmt)
        rewritten = [
            _blank(item, eligible[_entry_key(item, fmt)])
            if _entry_key(item, fmt) in eligible else dict(item)
            for item in entries
        ]
        # Preserve a non-list wrapper (``{"mcpServers": [...]}``) instead of
        # flattening the file: the loader reads that shape, and a redaction that
        # changed the shape would be a migration of its own.
        if isinstance(document, Mapping) and not isinstance(document, list):
            out: Any = dict(document)
            for name in ("mcpServers", "mcp_servers"):
                if name in out:
                    out[name] = rewritten
                    break
            else:
                out = rewritten
        else:
            out = rewritten
        _write_atomic(locator, out)
        entry["written"] = True
        entry["entries"] = len(rewritten)
        _collect(report, entry)
        logger.info("[external] redacted %d legacy record(s) in %s",
                    len(entry["redacted"]), locator)

    report["counts"] = {"redacted": len(report["redacted"]),
                        "skipped": len(report["skipped"]),
                        "already_redacted": len(report["already_redacted"]),
                        "files": len(report["files"])}
    return report


# -- caches populated before the import --------------------------------------

def refresh_caches(*, actor_user_id: str = "", identity: Any = None,
                   service: Any = None,
                   tenant_ids: Optional[Sequence[str]] = None,
                   reset_process_service: bool = False) -> Dict[str, Any]:
    """Drop the caches that were filled from the store before the import.

    Deliberately narrow, because a cache cleared too broadly is its own outage:
    only MCP discovery memos are dropped, and only for the tenants named (or for
    every tenant that currently has an MCP row, when none are named). The memo is
    the only cache that *holds a copy* of configuration — everything else reads
    the store per call — so this is the whole of what a restart would have
    fixed, done without a restart.

    ``actor_user_id`` is optional because the operation grants nothing: it
    invalidates in-process caches and reads no secrets. It is recorded in the
    report so the runbook can say who triggered the refresh.
    """
    service = _service(identity, service)
    _, store = _resolve(identity, service)

    tenants = [str(t) for t in (tenant_ids or ()) if str(t or "")]
    if not tenants:
        try:
            rows = store.execute(
                "SELECT DISTINCT tenant_id FROM external_connections"
                " WHERE kind='mcp' AND tenant_id IS NOT NULL")
            tenants = sorted({str(row["tenant_id"]) for row in rows})
        except Exception:  # noqa: BLE001 - an empty store has no tenants
            tenants = []

    dropped: List[str] = []
    memo_entries = 0
    unavailable = ""
    try:
        from agent.tools.mcp import external as mcp_external

        for tenant in tenants:
            with mcp_external._MEMO_LOCK:  # noqa: SLF001 - the module owns it
                before = sum(1 for key in mcp_external._MEMO  # noqa: SLF001
                             if key[0] == tenant)
            mcp_external.forget_tenant(tenant)
            memo_entries += before
            dropped.append(tenant)
    except Exception as error:  # noqa: BLE001 - an agent-less process has no memo
        # Reported rather than raised: the caches live in the agent process, and
        # a CLI or console process legitimately does not have them. Claiming a
        # refresh happened when there is nothing to refresh would be the lie;
        # failing the cutover over it would be the bug.
        unavailable = "%s: %s" % (type(error).__name__, error)

    service_reset = False
    if reset_process_service:
        from integrations.external import service as service_module

        for key in list(service_module._SERVICE_CACHE):  # noqa: SLF001
            if key != getattr(service, "_db_path", None):  # noqa: SLF001
                service_module._SERVICE_CACHE.pop(key, None)  # noqa: SLF001
        service_reset = True

    return {
        "tenants": dropped,
        "memo_entries_dropped": memo_entries,
        "cache_unavailable": unavailable,
        "process_service_reset": service_reset,
        "actor_user_id": str(actor_user_id or ""),
    }


# -- CLI surface -------------------------------------------------------------

def restore_files(backup_path: str, *, out_dir: str, identity: Any = None,
                  service: Any = None, actor_user_id: str = "",
                  confirm: bool = False) -> Dict[str, Any]:
    """Write a backup's legacy files back out, for a rollback drill (12.6).

    Only the *legacy files* are restored, because they are the half a rollback
    needs: the control plane is switched back by flipping ``store_version``, and
    that switch only means anything while the legacy store is intact. Restoring
    into a directory the caller names — rather than over the live location — is
    intentional: a drill that overwrote the live store would turn a rehearsal
    into the incident it rehearses. A restore is verified against the manifest's
    own digest first, so a corrupt backup fails before anything is written.

    Checked before that first write, and on the dry run too: a backup that
    predates Agent assignment cannot express the 已配置 restriction, and
    restoring it would put the deployment back on 沿用原权限 for every connection a
    tenant had narrowed (change ``add-external-connection-agent-assignment``,
    spec: 恢复缺少状态的旧备份 MUST NOT 静默恢复原可用范围).
    """
    import base64

    from auth.crypto import decrypt_secret

    from integrations.external import backup as backup_module

    service = _service(identity, service)
    service.require_platform_admin(actor_user_id)
    compatibility = backup_module.assert_restore_compatible(
        backup_path, service=service)
    verified = backup_module.verify_backup(backup_path, with_key=True)
    with open(backup_path, encoding="utf-8") as handle:
        document = json.loads(decrypt_secret(handle.read()))
    target = os.path.abspath(os.path.expanduser(str(out_dir or "")))
    items = list(document.get("legacy_files", []))
    files = [{"name": os.path.basename(str(item.get("path") or "")),
              "bytes": len(base64.b64decode(str(item.get("content_b64") or ""))),
              "sha256": item.get("sha256")}
             for item in items]
    if not confirm:
        return {"dry_run": True, "out_dir": target, "files": files,
                "assignment_restore": compatibility,
                "plaintext_sha256": verified["plaintext_sha256"]}
    os.makedirs(target, exist_ok=True)
    written: List[Dict[str, Any]] = []
    for item, described in zip(items, files):
        name = described["name"]
        if not name:
            continue
        dest = os.path.join(target, name)
        # The digest is recomputed on the way out rather than trusted: a restore
        # is the last chance to notice that a manifest and its payload disagree.
        payload = base64.b64decode(str(item.get("content_b64") or ""))
        actual = _sha256(payload)
        if described["sha256"] and actual != str(described["sha256"]):
            raise ExternalConnectionError(
                "restored bytes for %s do not match the manifest" % name,
                code=VERIFY_FAILED, status=409)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        written.append({"path": dest, "sha256": actual, "bytes": len(payload)})
    return {"dry_run": False, "out_dir": target, "written": written,
            "files": files, "assignment_restore": compatibility,
            "plaintext_sha256": verified["plaintext_sha256"]}


def _sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()
