# encoding:utf-8
"""Encrypted, controlled backup of the pre-cutover external-connection state.

Why this exists
---------------
The cutover reads the legacy stores once, imports them idempotently, and then
task 12.5 *removes* the plaintext secrets those stores hold. Removing them is
only defensible if there is a copy an operator can actually restore from, so the
backup is a precondition of the cleanup rather than a nicety
(design §166: 生成加密受控备份).

Four properties, each answering a specific way this goes wrong:

**Encrypted with the deployment's credential key.** The payload is a JSON
document containing the legacy files byte-for-byte — including the plaintext
passwords and tokens those files hold — so it is encrypted with
:func:`auth.crypto.encrypt_secret`, the same key that protects live credentials.
A backup is therefore exactly as protected as the store it came from: no second
key to distribute, no key file to lose beside the backup, and a deployment that
cannot encrypt cannot silently write a plaintext dump instead (no key →
``CredentialCryptoError``, and nothing is written).

**Never plaintext on disk, not even transiently.** The document is serialized,
encrypted in memory, and only the ciphertext is written. There is no temp file to
clean up and no window in which a crash leaves one behind — which is the usual
way "encrypted backup" turns out not to have been.

**A digest that can be checked without the key.** A sidecar ``.manifest.json``
records the plaintext digest, the file count, the scope keys and the source
paths. It carries *no* secret material, so an operator can confirm which backup
is which and that the ciphertext has not been truncated, without decrypting
anything and without the master key on the box.

**Controlled destination.** The target must be an existing directory that is not
a symlink; it is created with mode ``0700`` if the caller asks for a new one, and
the backup file is written ``0600``. "Controlled" is otherwise a word rather than
a property — an encrypted blob in a world-readable directory is still a copy of
every credential in the deployment.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from common.log import logger
from integrations.external.errors import ExternalConnectionError

#: Versioned so a future reader can refuse a format it does not understand
#: instead of silently mis-parsing an older or newer document.
BACKUP_FORMAT = "cow-external-connections-backup/1"

#: Refusal codes. Named so the CLI and the tests branch on one string.
NOT_AUTHORIZED = "not_authorized"
BAD_DESTINATION = "bad_backup_destination"
BACKUP_UNREADABLE = "backup_unreadable"
BACKUP_DIGEST_MISMATCH = "backup_digest_mismatch"
#: The backup does not cover Agent assignment while the deployment relies on it.
BACKUP_INCOMPATIBLE_ASSIGNMENT_STATE = "backup_incompatible_assignment_state"

#: The control-plane tables that belong in a backup. Listed explicitly rather
#: than discovered from ``sqlite_master``: a backup whose contents depend on
#: which migrations happen to have run is a backup nobody can restore
#: deterministically, and a table added later must be a deliberate decision.
#:
#: ``external_connection_idempotency`` is deliberately *not* here. It is a
#: short-lived replay ledger keyed by actor and endpoint: restoring it would
#: replay a create against a store that already holds the result, and the import
#: the backup exists to protect is itself idempotent by source hash, so nothing
#: depends on this table surviving.
#:
#: The two Agent-assignment tables *are* here. They hold the 已配置 restriction
#: that narrows a connection to named Agents, so a backup that omits them is not
#: a copy of the state an operator would be restoring: it would come back as
#: 沿用原权限, silently widening every connection the tenant had already narrowed.
BACKED_UP_TABLES: Sequence[str] = (
    "external_connections",
    "external_connection_catalog_versions",
    "external_connection_secret_refs",
    "external_connection_tenant_access",
    "external_connection_tests",
    "external_connection_migrations",
    "external_connection_maintenance_windows",
    "platform_connection_secrets",
    "external_connection_agent_assignment_sets",
    "external_connection_agent_assignments",
)

#: The Agent-assignment tables, as one named group.
#:
#: Named separately because they are also the *coverage marker*: a backup taken
#: before this change neither holds these rows nor mentions these names, and that
#: absence is what :func:`assert_restore_compatible` refuses to read as "no
#: restrictions exist". Taken from one constant so the coverage question has one
#: answer.
ASSIGNMENT_TABLES: Sequence[str] = (
    "external_connection_agent_assignment_sets",
    "external_connection_agent_assignments",
)

#: Tables whose rows carry the scope they belong to (``scope_key``, or the
#: ``scope``/``tenant_id`` pair the connection rows use).
SCOPE_KEYED_TABLES: Sequence[str] = (
    "external_connections",
    "external_connection_catalog_versions",
    "external_connection_migrations",
    "external_connection_maintenance_windows",
)

#: Tables with no scope of their own, plus the column pointing at the connection
#: or platform connection they belong to.
CONNECTION_KEYED_TABLES: Mapping[str, str] = {
    "external_connection_secret_refs": "connection_id",
    "external_connection_tests": "connection_id",
    "external_connection_tenant_access": "platform_connection_id",
    "platform_connection_secrets": "platform_connection_id",
}

#: Tables keyed by the tenant that owns the row, filtered by the tenants named
#: in the requested scopes.
#:
#: The assignment tables cannot be selected by connection id: their
#: ``logical_connection_id`` may be the id of a *platform* template while the row
#: belongs to the consuming tenant, and keying on the connection would either
#: ship another tenant's relation or drop the tenant's own. The tenant is the
#: only correct scope, and it is the same column every read path uses.
TENANT_KEYED_TABLES: Sequence[str] = ASSIGNMENT_TABLES


def _service(identity: Any = None, service: Any = None) -> Any:
    """The external-connection service for this call.

    ``migration.py``'s convention: an explicit service wins (a caller inside a
    transaction or a test with its own fixture), then an explicit ``identity``
    store, then the process-wide service over the configured identity database.
    """
    if service is not None:
        return service
    if identity is not None:
        from integrations.external.service import ExternalConnectionService

        return ExternalConnectionService(identity)
    from integrations.external.service import get_external_connection_service

    return get_external_connection_service()


def _max_bytes() -> int:
    """Bound on one legacy file, so a runaway file cannot exhaust memory.

    Overridable through the deployment config for an unusually large store; the
    default is generous because refusing is worse than a slow backup.
    """
    try:
        from common.config import conf

        value = ((conf() or {}).get("external_connections") or {}).get(
            "backup_max_file_bytes")
        if value:
            return int(value)
    except Exception:  # noqa: BLE001 - a missing config is not a failure
        pass
    return 64 * 1024 * 1024


def _check_destination(out_dir: str, *, create: bool) -> str:
    target = os.path.abspath(os.path.expanduser(str(out_dir or "").strip()))
    if not target:
        raise ExternalConnectionError("a backup directory is required",
                                      code=BAD_DESTINATION, status=400)
    if os.path.islink(target):
        # Following a symlink means the file lands wherever it points, which is
        # exactly the control the caller is trying to establish.
        raise ExternalConnectionError(
            "the backup directory must not be a symlink",
            code=BAD_DESTINATION, status=400)
    if not os.path.isdir(target):
        if not create:
            raise ExternalConnectionError(
                "the backup directory does not exist: %s" % target,
                code=BAD_DESTINATION, status=400)
        os.makedirs(target, mode=0o700, exist_ok=True)
        if os.path.islink(target):  # a race created one between the two checks
            raise ExternalConnectionError(
                "the backup directory must not be a symlink",
                code=BAD_DESTINATION, status=400)
    return target


def _read_source(path: str) -> Dict[str, Any]:
    """One legacy file, as bytes plus the facts a verify pass needs."""
    if os.path.islink(path):
        raise ExternalConnectionError(
            "refusing to back up a symlinked legacy store: %s" % path,
            code=BACKUP_UNREADABLE, status=409)
    limit = _max_bytes()
    size = os.path.getsize(path)
    if size > limit:
        raise ExternalConnectionError(
            "legacy store is larger than the backup limit (%d > %d): %s"
            % (size, limit, path),
            code=BACKUP_UNREADABLE, status=409)
    with open(path, "rb") as handle:
        raw = handle.read()
    return {
        "path": path,
        "size": len(raw),
        # The digest is of the *plaintext* bytes, so it is meaningful after a
        # restore and comparable with the live file.
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_b64": base64.b64encode(raw).decode("ascii"),
    }


def _rows_for_tables(store: Any, scope_keys: Sequence[str]) -> Dict[str, Any]:
    """The control-plane rows for the given scopes, secrets excluded as values.

    ``external_connections`` carries no secret (only a reference), and
    ``external_connection_secret_refs`` points at ``credentials`` /
    ``platform_connection_secrets`` by id and version. Those *ids and versions*
    are what a restore needs to reattach a live credential; the credential
    material itself is already protected by the identity store's own encryption
    and is deliberately not duplicated here, so a backup cannot become a second,
    independently-decryptable copy of every password.

    ``external_connections`` is keyed by ``(scope, tenant_id)`` rather than by
    ``scope_key``, so it is filtered through :func:`_row_scope_key` while the
    tables that carry a literal ``scope_key`` are compared directly. Both go
    through the same key spelling, which is the migration's — a second spelling
    here would make "what does this backup cover" unanswerable.
    """
    wanted = {str(key) for key in scope_keys}
    out: Dict[str, Any] = {}
    for table in BACKED_UP_TABLES:
        rows = store.execute("SELECT * FROM %s" % table)  # noqa: S608 - fixed list
        # Materialise first: the rows come back as ``sqlite3.Row``, which has no
        # ``.get`` and is not JSON-serializable.
        out[table] = [dict(row) for row in rows]
    # Tables fall in two groups. Some carry their own scope and are selected by
    # it directly; the rest point at a connection and are selected by the rows
    # the first group produced. The split is explicit because a table that
    # *looks* like it belongs in the wrong group would silently ship another
    # tenant's rows, and the column names differ enough that guessing is not
    # safe.
    for table in SCOPE_KEYED_TABLES:
        out[table] = [row for row in out[table]
                      if _row_scope_key(row) in wanted]
    connection_ids = {str(row["id"]) for row in out["external_connections"]}
    for table, column in CONNECTION_KEYED_TABLES.items():
        out[table] = [row for row in out[table]
                      if str(row.get(column) or "") in connection_ids]
    # The assignment tables are scoped by tenant rather than by connection: a
    # tenant's relation to a platform template names the *template* as its
    # logical connection, which is not in this scope's connection list, while the
    # row is unambiguously the tenant's. Filtering them by connection id would
    # drop exactly the relations that restrict an inherited template.
    tenant_ids = _tenant_ids_for(wanted)
    for table in TENANT_KEYED_TABLES:
        out[table] = [row for row in out[table]
                      if str(row.get("tenant_id") or "") in tenant_ids]
    return out


def _tenant_ids_for(scope_keys: Sequence[str]) -> set:
    """The tenants named by a set of scope keys.

    Read from the same key spelling :func:`~integrations.external.migration.scope_key`
    produces, so "which tenants does this backup cover" has one answer rather
    than a second parsing rule that can drift.
    """
    from integrations.external import registry

    prefix = "%s:" % registry.SCOPE_TENANT
    return {str(key)[len(prefix):] for key in scope_keys
            if str(key).startswith(prefix) and len(str(key)) > len(prefix)}


def _row_scope_key(row: Mapping[str, Any]) -> str:
    if "scope_key" in row:
        return str(row["scope_key"] or "")
    scope = str(row.get("scope") or "")
    tenant_id = row.get("tenant_id")
    if not scope:
        return ""
    return "%s:%s" % (scope, tenant_id if tenant_id is not None else "-")


def create_backup(*, actor_user_id: str, out_dir: str, identity: Any = None,
                  sources: Any = (), erp_files: Any = (), mcp_files: Any = (),
                  scope_keys: Optional[Sequence[str]] = None,
                  note: str = "", create_dir: bool = False,
                  include_control_plane: bool = True,
                  service: Any = None) -> Dict[str, Any]:
    """Encrypt and write one backup; return the report, never the payload.

    Requires a platform admin: a backup spans tenants and carries their
    credentials, so it is not a tenant-scoped read. The check is here rather
    than in the CLI so that any caller — CLI, a future scheduler, a test — is
    held to it.
    """
    service = _service(identity, service)
    service.require_platform_admin(actor_user_id)

    # Imported here so a deployment that never takes a backup does not pay for
    # the migration module's discovery code.
    from integrations.external.migration import discover_legacy_sources

    discovered = discover_legacy_sources(identity, erp_files=erp_files,
                                         mcp_files=mcp_files)
    chosen = []
    for source in [*sources] if sources else discovered:
        locator = getattr(source, "locator", source if isinstance(source, str) else "")
        if isinstance(locator, str) and locator and os.path.isfile(locator):
            chosen.append(locator)
    # Preserve discovery order but drop repeats; the same file can be found both
    # through a bound Agent workspace and through an explicit argument.
    seen: set = set()
    ordered: List[str] = []
    for path in chosen:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        ordered.append(path)

    files = [_read_source(path) for path in ordered]

    keys = list(scope_keys or _all_scope_keys(service))
    control_plane = (_rows_for_tables(service._store, sorted(keys))  # noqa: SLF001
                     if include_control_plane else {})
    document: Dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "note": str(note or "")[:500],
        "created_by": actor_user_id,
        "scope_keys": sorted(keys),
        "legacy_files": files,
        "control_plane": control_plane,
        # Named explicitly, not inferred from the row counts: a table that exists
        # but happens to be empty would otherwise look like a table this backup
        # never knew about, and the two mean opposite things on a restore. An
        # assignment-free backup declares nothing, which is the truth: it makes
        # no claim about the restriction regime.
        "assignment_tables": (list(ASSIGNMENT_TABLES) if control_plane else []),
    }
    serialized = json.dumps(document, ensure_ascii=False, sort_keys=True,
                            default=str)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    target = _check_destination(out_dir, create=create_dir)
    # The name carries the timestamp for an operator's benefit and the digest so
    # two runs over the same state are recognisable as the same content; the
    # digest is over the *plaintext* document, which is what a verify pass
    # recomputes.
    name = "external-connections-%s-%s.json.enc" % (_stamp(), digest[:16])
    path = os.path.join(target, name)

    from auth.crypto import encrypt_secret

    # Encrypt before opening the file: if there is no master key this raises and
    # nothing at all is created, so the failure cannot leave a partial or
    # plaintext artifact behind.
    ciphertext = encrypt_secret(serialized)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(ciphertext)
        handle.flush()
        os.fsync(handle.fileno())

    manifest = {
        "format": BACKUP_FORMAT,
        "backup_name": name,
        "plaintext_sha256": digest,
        "ciphertext_sha256": hashlib.sha256(
            ciphertext.encode("utf-8")).hexdigest(),
        "plaintext_bytes": len(serialized.encode("utf-8")),
        "legacy_files": [
            {"path": f["path"], "size": f["size"], "sha256": f["sha256"]}
            for f in files],
        "scope_keys": sorted(keys),
        "table_counts": {table: len(rows) for table, rows in
                         document["control_plane"].items()},
        "assignment_tables": list(document["assignment_tables"]),
        "note": str(note or "")[:500],
        "created_by": actor_user_id,
    }
    manifest_path = path + ".manifest.json"
    tmp_manifest = manifest_path + ".tmp"
    with open(tmp_manifest, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2,
                  sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_manifest, 0o600)
    # Publish atomically: a reader either sees no manifest or the whole one.
    os.replace(tmp_manifest, manifest_path)

    logger.info("[external] backup written to %s (%d legacy files, %d bytes)",
                path, len(files), manifest["plaintext_bytes"])
    return {"backup_file": path, "manifest_file": manifest_path, **manifest}


def verify_backup(path: str, *, with_key: bool = True) -> Dict[str, Any]:
    """Check a backup without (necessarily) decrypting it.

    ``with_key=False`` checks only what the ciphertext and the sidecar can prove
    — that the file is intact and which backup it is — so an operator can
    inventory backups on a host that does not hold the master key. With the key,
    the plaintext digest is recomputed as well, which is what proves the backup
    would actually decrypt to the state it claims.
    """
    path = os.path.abspath(os.path.expanduser(str(path or "")))
    manifest_path = path + ".manifest.json"
    for required in (path, manifest_path):
        if not os.path.isfile(required):
            raise ExternalConnectionError("backup file not found: %s" % required,
                                          code=BACKUP_UNREADABLE, status=404)
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    with open(path, encoding="utf-8") as handle:
        ciphertext = handle.read()
    actual = hashlib.sha256(ciphertext.encode("utf-8")).hexdigest()
    expected = str(manifest.get("ciphertext_sha256") or "")
    if expected and actual != expected:
        raise ExternalConnectionError(
            "backup ciphertext does not match its manifest",
            code=BACKUP_DIGEST_MISMATCH, status=409)
    report: Dict[str, Any] = {
        "backup_file": path,
        "ciphertext_sha256": actual,
        "plaintext_sha256": manifest.get("plaintext_sha256"),
        "plaintext_bytes": manifest.get("plaintext_bytes"),
        "legacy_files": manifest.get("legacy_files", []),
        "scope_keys": manifest.get("scope_keys", []),
        "table_counts": manifest.get("table_counts", {}),
        "assignment_tables": manifest.get("assignment_tables", []),
        "decrypted": False,
    }
    if not with_key:
        return report
    from auth.crypto import CredentialCryptoError, decrypt_secret

    try:
        plaintext = decrypt_secret(ciphertext)
    except CredentialCryptoError as error:
        raise ExternalConnectionError(
            "backup could not be decrypted with this deployment's key: %s"
            % error, code=BACKUP_UNREADABLE, status=409) from error
    recomputed = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    if recomputed != str(manifest.get("plaintext_sha256") or ""):
        raise ExternalConnectionError(
            "backup plaintext does not match its manifest",
            code=BACKUP_DIGEST_MISMATCH, status=409)
    report["decrypted"] = True
    report["file_count"] = len(json.loads(plaintext).get("legacy_files", []))
    return report


def assert_restore_compatible(path: str, *, identity: Any = None,
                              service: Any = None) -> Dict[str, Any]:
    """Refuse a restore whose backup cannot speak about 已配置 restrictions.

    A backup taken before Agent assignment exists holds neither assignment table
    and says so by not naming them. Restoring it is not merely "older data": the
    restriction it never knew about would be gone, and the *only* reading of the
    result that this system allows is 沿用原权限 — every connection the tenant had
    narrowed coming back open. That is the silent widening the spec forbids, so
    it is refused here rather than reported after the fact.

    Called by the restore entry point *before* it writes anything, and on a dry
    run too, so an operator learns about the incompatibility while the decision
    is still reversible. The check reads only the manifest — no master key
    needed — because an operator on a host without the key is exactly the one who
    needs to know that this file is not the restore they think it is.

    A backup that names the tables is compatible whatever their counts: "covered
    and empty" is a real state (nobody is assigned) and is restored as such.
    """
    service = _service(identity, service)
    verified = verify_backup(path, with_key=False)
    declared = [str(name) for name in verified.get("assignment_tables") or []]
    if all(table in declared for table in ASSIGNMENT_TABLES):
        return {"restorable": True, "assignment_tables": declared,
                "configured_sets": None, "reason": ""}
    configured = _configured_assignment_sets(service)
    if not configured:
        # Nothing to lose: the deployment has no 已配置 connection, so this
        # backup restores to a state that is consistent with the live one.
        return {"restorable": True, "assignment_tables": declared,
                "configured_sets": 0, "reason": "no_configured_assignments"}
    raise ExternalConnectionError(
        "backup %s predates Agent assignment but this deployment holds %d"
        " configured connection(s); restoring it would silently widen them to"
        " 沿用原权限. Take a fresh backup, or unconfigure those connections"
        " first." % (os.path.basename(path), configured),
        code=BACKUP_INCOMPATIBLE_ASSIGNMENT_STATE, status=409)


def _configured_assignment_sets(service: Any) -> int:
    """How many connections this deployment has narrowed, or 0 when unknown.

    A store that cannot answer is reported as 0 only because the caller has
    already established the backup does not name the tables; refusing every
    restore on an unreadable store would block the drill for a different reason
    than the one being checked.
    """
    try:
        rows = service._store.execute(  # noqa: SLF001
            "SELECT COUNT(*) AS c FROM external_connection_agent_assignment_sets"
            " WHERE configured=1")
    except Exception:  # noqa: BLE001 - a pre-migration store has no such table
        return 0
    return int(rows[0]["c"]) if rows else 0


def _all_scope_keys(service: Any) -> List[str]:
    """Every scope the store currently knows about, plus the platform scope.

    Read from the rows rather than assumed, so a backup of a deployment with no
    tenant connections still records the platform key and a restore can tell
    "there were none" from "this table was not covered".
    """
    from integrations.external import registry

    keys = {_scope_key_for(registry.SCOPE_PLATFORM, None)}
    try:
        rows = service._store.execute(  # noqa: SLF001
            "SELECT DISTINCT scope, tenant_id FROM external_connections")
    except Exception:  # noqa: BLE001 - an empty store has nothing to enumerate
        return sorted(keys)
    for row in rows:
        keys.add(_scope_key_for(row["scope"], row["tenant_id"]))
    try:
        for row in service._store.execute(  # noqa: SLF001
                "SELECT DISTINCT scope_key FROM external_connection_catalog_versions"):
            if row["scope_key"]:
                keys.add(str(row["scope_key"]))
    except Exception:  # noqa: BLE001
        pass
    return sorted(keys)


def _scope_key_for(scope: str, tenant_id: Any) -> str:
    from integrations.external.migration import scope_key

    return scope_key(scope, tenant_id)


def _stamp() -> str:
    import time

    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


# -- retention ---------------------------------------------------------------
#
# A backup is taken before every irreversible cutover step, so a deployment that
# follows the runbook accumulates them. Left alone they grow without bound; but
# the obvious "keep the newest N" rule is the wrong one on its own, because the
# newest backup can be a *bad* one -- a partial export, a wrong key, a truncated
# copy -- and a retention pass that deletes the last good backup in favour of a
# broken newer one turns a recoverable cutover into an unrecoverable one. So the
# rule here is: prove a backup is usable before letting it count as the one to
# keep, and never delete a backup that is the newest *verified* one.

#: Refusal codes for the retention pass.
BAD_RETENTION = "bad_retention_policy"
NO_USABLE_BACKUP = "no_usable_backup"

#: What a backup file this module wrote looks like. Only files matching this are
#: ever candidates for deletion: a retention pass must not be able to remove an
#: operator's unrelated file that happens to sit in the same directory.
BACKUP_NAME_PATTERN = re.compile(
    r"^external-connections-\d{8}T\d{6}Z-[0-9a-f]{16}\.json\.enc$")

DEFAULT_KEEP = 10
DEFAULT_MAX_AGE_DAYS = 30


def _list_backups(out_dir: str) -> List[Dict[str, Any]]:
    """Backups in ``out_dir``, newest first, with what the manifest proves.

    Listing is a plain directory read and never follows a symlink: a symlinked
    "backup" is something a retention pass must not delete (it is not our file)
    and must not count as one of the backups being kept (it may point anywhere).
    """
    target = os.path.abspath(os.path.expanduser(str(out_dir or "")))
    if not os.path.isdir(target) or os.path.islink(target):
        return []
    found: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(target)):
        if not BACKUP_NAME_PATTERN.match(name):
            continue
        path = os.path.join(target, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        entry: Dict[str, Any] = {"backup_file": path, "name": name,
                                 "manifest_ok": False}
        try:
            entry["mtime"] = os.path.getmtime(path)
            entry["size"] = os.path.getsize(path)
        except OSError:  # pragma: no cover - a racing unlink
            continue
        try:
            with open(path + ".manifest.json", encoding="utf-8") as handle:
                manifest = json.load(handle)
            entry["manifest_ok"] = (
                str(manifest.get("backup_name") or "") == name
                and str(manifest.get("format") or "") == BACKUP_FORMAT)
            entry["plaintext_sha256"] = manifest.get("plaintext_sha256")
            entry["scope_keys"] = manifest.get("scope_keys", [])
        except Exception:  # noqa: BLE001 - a bad manifest is a fact to report
            entry["manifest_ok"] = False
        found.append(entry)
    found.sort(key=lambda item: (item["mtime"], item["name"]), reverse=True)
    return found


def prune_backups(*, actor_user_id: str, out_dir: str, keep: int = DEFAULT_KEEP,
                  max_age_days: int = DEFAULT_MAX_AGE_DAYS,
                  identity: Any = None, service: Any = None,
                  confirm: bool = False,
                  check_key: bool = True) -> Dict[str, Any]:
    """Drop old backups, keeping a proven-usable one whatever its age.

    ``confirm=False`` (the default) reports what *would* be removed and removes
    nothing -- the CLI's dry run. Three rules hold either way:

    * a backup is deleted only when a *newer* backup that verifies is being
      kept, so a pass cannot leave the deployment with nothing to restore from
      and cannot prefer an older state over a newer one that works;
    * only files this module wrote, in this directory, are candidates, so a
      mistyped ``--dir`` cannot turn into an ``rm``;
    * when nothing verifies, nothing is removed at all -- the broken files are
      the evidence for working out what went wrong.

    Refuses when the policy would keep nothing (``keep < 1``) rather than
    reading a zero as "delete everything".
    """
    service = _service(identity, service)
    service.require_platform_admin(actor_user_id)
    if int(keep) < 1:
        raise ExternalConnectionError("retention must keep at least one backup",
                                      code=BAD_RETENTION, status=400)
    if int(max_age_days) < 0:
        raise ExternalConnectionError("retention age cannot be negative",
                                      code=BAD_RETENTION, status=400)
    target = os.path.abspath(os.path.expanduser(str(out_dir or "")))
    backups = _list_backups(target)
    if not backups:
        raise ExternalConnectionError("no backups in %s" % (target or "?"),
                                      code=NO_USABLE_BACKUP, status=404)

    cutoff = _now() - int(max_age_days) * 86400
    kept: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    protected = 0
    newer_usable_kept = False
    # Newest first, so ``newer_usable_kept`` means exactly "a usable backup that
    # we are keeping is newer than this one". That is the whole safety rule: a
    # backup may be deleted only when another one that verifies survives *and*
    # is newer, so a pass can never take away the last restorable artifact and
    # can never prefer an older state over a newer one that works.
    for index, entry in enumerate(backups):
        in_policy = index < int(keep) and entry["mtime"] >= cutoff
        if in_policy:
            kept.append(entry)
            if _is_usable(entry, check_key=check_key):
                newer_usable_kept = True
            continue
        if newer_usable_kept:
            removed.append(entry)
            continue
        # Outside the policy and nothing newer verifies: kept anyway. This is the
        # branch that makes the count policy a target rather than an instruction
        # to destroy the only file that still reads back.
        protected += 1
        kept.append(entry)
        if _is_usable(entry, check_key=check_key):
            newer_usable_kept = True

    # Nothing was deleted above unless something usable was already being kept,
    # so this can only fire when the directory holds no usable backup at all --
    # in which case deleting the broken ones would destroy the evidence an
    # operator needs to work out what went wrong.
    if not any(_is_usable(entry, check_key=check_key) for entry in kept):
        raise ExternalConnectionError(
            "refusing to prune: no backup in %s verifies" % (target or "?"),
            code=NO_USABLE_BACKUP, status=409)

    report: Dict[str, Any] = {
        "out_dir": target,
        "policy": {"keep": int(keep), "max_age_days": int(max_age_days)},
        "total": len(backups),
        "kept": [entry["name"] for entry in kept],
        "protected": protected,
        "to_remove": [entry["name"] for entry in removed],
        "removed": [],
        "confirmed": bool(confirm),
    }
    if not confirm:
        return report
    for entry in removed:
        for path in (entry["backup_file"],
                     entry["backup_file"] + ".manifest.json"):
            try:
                os.unlink(path)
            except FileNotFoundError:  # pragma: no cover - racing removal
                continue
        report["removed"].append(entry["name"])
    logger.info("[external] backup retention removed %d of %d backups in %s",
                len(report["removed"]), len(backups), target)
    return report


def _is_usable(entry: Mapping[str, Any], *, check_key: bool) -> bool:
    """Whether this backup can still be read back.

    A file whose manifest is missing or inconsistent is *not* usable even when
    it is the newest: on the runbook's own terms it is not a backup, it is an
    unnamed blob, and treating it as the one to keep is exactly the mistake this
    ordering exists to prevent.
    """
    if not entry.get("manifest_ok"):
        return False
    try:
        verify_backup(entry["backup_file"], with_key=check_key)
    except ExternalConnectionError:
        return False
    return True


def _now() -> int:
    import time

    return int(time.time())
