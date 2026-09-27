# encoding:utf-8
"""``cow external-connections`` - the legacy-connection migration console.

Task group 12 of ``add-external-system-access`` makes the *existing* connection
data (the file-based ERP store and the legacy MCP configuration) importable into
the new control plane.  The import is an explicit operator action, never
something a normal request can reach, so it lives on the CLI:

* ``preflight``  - read-only: find every legacy record, hash it, say whether it
  can be imported and, when it cannot, exactly why.
* ``import``     - the only writing subcommand; without ``--yes`` it prints the
  plan and stops.
* ``verify``     - compare the legacy set with what the new store holds and
  report every unmapped record with its reason.
* ``status``     - the deployment switch (``store_version``) and what is pending.

Two rules the subcommands hold to:

* ``preflight`` / ``verify`` / ``status`` never write.  Only ``import --yes``
  does, and it says what it will do first.
* A report never contains a secret value.  Secrets are reported as slot *names*
  and presence only.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, Sequence

import click

from cli.utils import ensure_sys_path

#: Retention defaults, mirrored from ``integrations.external.backup`` so the
#: help text and the implementation cannot drift.
from integrations.external.backup import (DEFAULT_KEEP,
                                          DEFAULT_MAX_AGE_DAYS)


# -- wiring ------------------------------------------------------------------

def _identity():
    """The process-wide identity service (the same one the app uses).

    Imported on use so ``cow --help`` and the other commands do not pay for the
    identity stack when this command is not the one being run.
    """
    ensure_sys_path()
    from auth.service import get_identity_service

    return get_identity_service()


def _migration():
    ensure_sys_path()
    from integrations import external  # noqa: F401 - ensures the package imports

    from integrations.external import migration

    return migration


def _actor(identity, actor: Optional[str]) -> str:
    """The actor the import is audited against.

    A migration must not invent an actor.  ``--actor`` is the operator naming
    themselves; without it the command falls back to the instance's first
    platform admin, which is the user a maintenance-window action belongs to.
    """
    if actor:
        return actor
    users = identity.list_platform_users()
    if not users:
        raise click.ClickException(
            "no platform admin exists; pass --actor <user_id>"
        )
    return users[0]["id"]


def _explicit_sources(source: Sequence[str], tenant: Optional[str]):
    """Turn ``--source`` locators into legacy-source descriptors.

    A path's format is inferred from its name: a file called
    ``erp_connections.json`` (or any path ending in ``.erp.json``) is the ERP
    JSON store, everything else is an MCP configuration.  An explicit source has
    no derivable ownership, so ``--tenant`` names it; without one the record is
    reported as ``ownership_unknown`` rather than silently assigned.
    """
    if not source:
        return None, None
    from integrations.external.migration import (
        ERP_FORMAT, MCP_FORMAT, LegacySource)

    erp: List[Any] = []
    mcp: List[Any] = []
    for locator in source:
        path = str(locator)
        lowered = path.lower()
        is_erp = lowered.endswith("erp_connections.json") or lowered.endswith(
            ".erp.json")
        target = erp if is_erp else mcp
        target.append(LegacySource(
            locator=path,
            format=ERP_FORMAT if is_erp else MCP_FORMAT,
            tenant_id=tenant,
        ))
    return erp, mcp


def _echo_json(payload: Dict[str, Any]) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# -- rendering ---------------------------------------------------------------

def _render_preflight(report: Dict[str, Any]) -> None:
    summary = report["summary"]
    click.echo("Store version: %s" % report["store_version"])
    click.echo("Sources:")
    for source in report["sources"]:
        detail = "%s records=%d" % (source["format"], source["records"])
        if source.get("tenant_id"):
            detail += " tenant=%s" % source["tenant_id"]
        if source.get("error"):
            detail += " error=%s" % source["error"]
        click.echo("  %s (%s)" % (source["locator"], detail))
    click.echo("Records:")
    for record in report["records"]:
        state = "importable" if record["importable"] else "BLOCKED"
        if record["imported"]:
            state = "already-imported"
        line = "  [%s] %s/%s tenant=%s name=%s hash=%s" % (
            state, record["kind"], record["scope"],
            record["tenant_id"] or "-", record["name"] or "-",
            record["source_hash"][:12])
        if record["reason"]:
            line += " reason=%s" % record["reason"]
        click.echo(line)
    click.echo(
        "Summary: total=%d importable=%d already-imported=%d unimportable=%d"
        % (summary["total"], summary["importable"], summary["imported"],
           summary["unimportable"]))
    for reason, count in sorted(summary["by_reason"].items()):
        click.echo("  %s: %d" % (reason, count))


def _render_verify(report: Dict[str, Any]) -> None:
    click.echo("Store version: %s" % report["store_version"])
    click.echo("Counts by kind/scope:")
    for kind, scopes in sorted(report["counts_by_kind_scope"].items()):
        for scope, count in sorted(scopes.items()):
            click.echo("  %s/%s: %d" % (kind, scope, count))
    click.echo("Unmapped (%d):" % len(report["unmapped"]))
    for item in report["unmapped"]:
        click.echo("  %s tenant=%s name=%s reason=%s" % (
            item["source_locator"], item["tenant_id"] or "-",
            item["name"] or "-", item["reason"]))
    if report["secret_gaps"]:
        click.echo("Secret gaps (%d):" % len(report["secret_gaps"]))
        for item in report["secret_gaps"]:
            click.echo("  %s slot=%s reason=%s" % (
                item["source_locator"], item["slot"], item["reason"]))
    summary = report["summary"]
    click.echo("Summary: legacy=%d imported=%d unmapped=%d unimportable=%d "
               "secret-gaps=%d" % (
                   summary["legacy_total"], summary["imported"],
                   summary["unmapped"], summary["unimportable"],
                   summary["secret_gaps"]))
    click.echo("Verification: %s" % ("OK" if report["ok"] else "INCOMPLETE"))


def _render_import(report: Dict[str, Any], *, dry_run: bool) -> None:
    prefix = "Would import" if dry_run else "Import"
    click.echo("%s (batch %s, store_version=%s)" % (
        prefix, report.get("batch_id", "-"), report["store_version"]))
    for item in report["results"]:
        line = "  %s %s/%s tenant=%s name=%s" % (
            item["result"], item["kind"], item["scope"],
            item["tenant_id"] or "-", item["name"] or "-")
        if item.get("reason"):
            line += " reason=%s" % item["reason"]
        click.echo(line)
    click.echo("Summary:")
    for result, count in sorted(report.get("summary", {}).items()):
        click.echo("  %s: %d" % (result, count))
    if report.get("erp_defaults"):
        click.echo("ERP default reconciliation:")
        for decision in report["erp_defaults"]:
            click.echo("  tenant=%s applied=%s reason=%s" % (
                decision["tenant_id"], decision["applied"],
                decision["reason"]))


def _render_status(state: Dict[str, Any]) -> None:
    click.echo("store_version=%s (safe default: %s, configured: %s)" % (
        state["store_version"], state["safe_default"], state["configured"]))
    click.echo("allowed values: %s" % ", ".join(state["allowed"]))
    if state["unsafe_reason"]:
        click.echo("UNSAFE: %s" % state["unsafe_reason"])
    click.echo("pending importable legacy records: %d" % len(state["pending"]))
    for record in state["pending"]:
        click.echo("  %s tenant=%s name=%s" % (
            record["source_locator"], record["tenant_id"] or "-",
            record["name"] or "-"))
    click.echo("records with a blocking reason: %d" % len(state["blocking"]))
    for record in state["blocking"]:
        click.echo("  %s reason=%s" % (record["source_locator"],
                                       record["reason"]))
    click.echo("safe: %s" % state["ok"])


# -- command group -----------------------------------------------------------

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


@click.group(name="external-connections", context_settings=CONTEXT_SETTINGS)
def external_connections():
    """Migrate legacy ERP / MCP connections into the control plane."""


@external_connections.command("preflight")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option("--source", multiple=True,
              help="An extra legacy file to include (format inferred by name).")
@click.option("--tenant", default=None,
              help="Tenant id for --source files (no guess is made without it).")
def preflight_cmd(as_json, source, tenant):
    """Find every legacy record and report what could be imported.

    Read-only: nothing is written and no secret is ever printed.
    """
    migration = _migration()
    erp_files, mcp_files = _explicit_sources(source, tenant)
    report = migration.preflight(
        _identity(), erp_files=erp_files or (), mcp_files=mcp_files or ())
    if as_json:
        _echo_json(report)
    else:
        _render_preflight(report)


@external_connections.command("import")
@click.option("--actor", default=None,
              help="User id the import is audited against (default: first platform admin).")
@click.option("--yes", "confirmed", is_flag=True,
              help="Actually write; without it the command only prints the plan.")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option("--source", multiple=True,
              help="An extra legacy file to include (format inferred by name).")
@click.option("--tenant", default=None,
              help="Tenant id for --source files (no guess is made without it).")
def import_cmd(actor, confirmed, as_json, source, tenant):
    """Import legacy records into the control plane (idempotent).

    Without --yes this is a dry run: it prints exactly what it would do and
    writes nothing.
    """
    migration = _migration()
    identity = _identity()
    erp_files, mcp_files = _explicit_sources(source, tenant)
    if not confirmed:
        report = migration.preflight(
            identity, erp_files=erp_files or (), mcp_files=mcp_files or ())
        report["batch_id"] = ""
        results = [
            {"kind": record["kind"], "scope": record["scope"],
             "tenant_id": record["tenant_id"], "name": record["name"],
             "result": "would_import", "reason": record["reason"]}
            for record in report["records"] if record["importable"]
            and not record["imported"]
        ]
        report["results"] = results
        report["summary"] = {"would_import": len(results)}
        report["erp_defaults"] = []
        if as_json:
            _echo_json(report)
        else:
            _render_import(report, dry_run=True)
        click.echo("Dry run: pass --yes to write.")
        return
    report = migration.import_connections(
        identity, actor_user_id=_actor(identity, actor),
        erp_files=erp_files or (), mcp_files=mcp_files or (), confirm=True)
    if as_json:
        _echo_json(report)
    else:
        _render_import(report, dry_run=False)


@external_connections.command("verify")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option("--source", multiple=True,
              help="An extra legacy file to include (format inferred by name).")
@click.option("--tenant", default=None,
              help="Tenant id for --source files (no guess is made without it).")
def verify_cmd(as_json, source, tenant):
    """Compare the legacy set with what the new store holds."""
    migration = _migration()
    erp_files, mcp_files = _explicit_sources(source, tenant)
    report = migration.verify(
        _identity(), erp_files=erp_files or (), mcp_files=mcp_files or ())
    if as_json:
        _echo_json(report)
    else:
        _render_verify(report)
    sys.exit(0 if report["ok"] else 1)


@external_connections.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option("--source", multiple=True,
              help="An extra legacy file to include (format inferred by name).")
@click.option("--tenant", default=None,
              help="Tenant id for --source files (no guess is made without it).")
def status_cmd(as_json, source, tenant):
    """Show the store_version switch and what is still unmigrated."""
    migration = _migration()
    erp_files, mcp_files = _explicit_sources(source, tenant)
    state = migration.store_version_state(
        _identity(), erp_files=erp_files or (), mcp_files=mcp_files or ())
    if as_json:
        _echo_json(state)
    else:
        _render_status(state)


# -- the steps after a successful import (task 12.5) -------------------------

def _cutover():
    ensure_sys_path()
    from integrations.external import cutover

    return cutover


def _backups():
    ensure_sys_path()
    from integrations.external import backup

    return backup


def _render_redact(report: Dict[str, Any]) -> None:
    for entry in report["files"]:
        click.echo("%s%s" % (entry["locator"],
                             "" if entry["written"] else " (not written)"))
        for item in entry["redacted"]:
            click.echo("  redact legacy_id=%s fields=%s"
                       % (item["legacy_id"], ",".join(item["fields"])))
        for item in entry["skipped"]:
            click.echo("  skip   legacy_id=%s reason=%s%s"
                       % (item["legacy_id"], item["reason"],
                          " slots=%s" % ",".join(item.get("slots", []))
                          if item.get("slots") else ""))
        if entry.get("error"):
            click.echo("  error  %s" % entry["error"])
    counts = report["counts"]
    click.echo("Summary: redacted=%d skipped=%d files=%d"
               % (counts["redacted"], counts["skipped"], counts["files"]))
    click.echo("Confirmed: %s" % report["confirmed"])
    if not report["confirmed"]:
        click.echo("Dry run: pass --yes to blank the legacy plaintext.")


@external_connections.command("redact")
@click.option("--actor", default=None,
              help="User id the redaction is audited against (default: first platform admin).")
@click.option("--yes", "confirmed", is_flag=True,
              help="Actually blank the values; without it the command only prints the plan.")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option("--source", multiple=True,
              help="An extra legacy file to include (format inferred by name).")
@click.option("--tenant", default=None,
              help="Tenant id for --source files (no guess is made without it).")
def redact_cmd(actor, confirmed, as_json, source, tenant):
    """Blank the legacy plaintext for records the import carried over.

    Only records the ledger says were imported *and* whose secret slot the new
    store references are touched; everything else keeps its value and is
    reported as skipped. Run it after ``verify`` says OK.
    """
    cutover = _cutover()
    identity = _identity()
    erp_files, mcp_files = _explicit_sources(source, tenant)
    report = cutover.redact_legacy_secrets(
        actor_user_id=_actor(identity, actor), identity=identity,
        erp_files=erp_files or (), mcp_files=mcp_files or (),
        confirm=confirmed)
    if as_json:
        _echo_json(report)
    else:
        _render_redact(report)


@external_connections.command("refresh-caches")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option("--actor", default=None, help="User id recorded as the trigger.")
def refresh_caches_cmd(as_json, actor):
    """Drop the discovery caches filled before the import, without a restart."""
    cutover = _cutover()
    identity = _identity()
    report = cutover.refresh_caches(
        actor_user_id=_actor(identity, actor), identity=identity)
    if as_json:
        _echo_json(report)
    else:
        click.echo("tenants refreshed: %d" % len(report["tenants"]))
        click.echo("memo entries dropped: %d" % report["memo_entries_dropped"])
        if report["cache_unavailable"]:
            click.echo("cache unavailable here: %s" % report["cache_unavailable"])


@external_connections.command("prune-backups")
@click.option("--dir", "out_dir", required=True, help="The backup directory.")
@click.option("--keep", default=DEFAULT_KEEP, show_default=True,
              type=int, help="How many recent backups the policy aims to keep.")
@click.option("--max-age-days", default=DEFAULT_MAX_AGE_DAYS, show_default=True,
              type=int, help="Age beyond which a backup is no longer wanted.")
@click.option("--actor", default=None,
              help="User id the retention is audited against (default: first platform admin).")
@click.option("--yes", "confirmed", is_flag=True,
              help="Actually remove; without it the command only prints the plan.")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
def prune_backups_cmd(out_dir, keep, max_age_days, actor, confirmed, as_json):
    """Drop old backups, keeping a verified one whatever its age."""
    backups = _backups()
    identity = _identity()
    report = backups.prune_backups(
        actor_user_id=_actor(identity, actor), out_dir=out_dir, keep=keep,
        max_age_days=max_age_days, identity=identity, confirm=confirmed)
    if as_json:
        _echo_json(report)
    else:
        click.echo("backups: %d kept=%d protected=%d removable=%d"
                   % (report["total"], len(report["kept"]),
                      report["protected"], len(report["to_remove"])))
        for name in report["to_remove"]:
            click.echo("  %s%s" % (name, "" if report["confirmed"] else " (kept)"))
        click.echo("removed: %d" % len(report["removed"]))
        if not report["confirmed"]:
            click.echo("Dry run: pass --yes to remove.")


@external_connections.command("restore-files")
@click.option("--backup", "backup_path", required=True,
              help="The encrypted backup to read the legacy files from.")
@click.option("--dir", "out_dir", required=True,
              help="Where to write them; never the live store by accident.")
@click.option("--actor", default=None,
              help="User id the restore is audited against (default: first platform admin).")
@click.option("--yes", "confirmed", is_flag=True,
              help="Actually write the files; without it the command only prints the plan.")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
def restore_files_cmd(backup_path, out_dir, actor, confirmed, as_json):
    """Write a backup's legacy files back out, for a rollback drill (task 12.6)."""
    cutover = _cutover()
    identity = _identity()
    report = cutover.restore_files(
        backup_path, out_dir=out_dir, identity=identity,
        actor_user_id=_actor(identity, actor), confirm=confirmed)
    if as_json:
        _echo_json(report)
    else:
        for item in report["files"]:
            click.echo("  %s bytes=%s sha256=%s"
                       % (item["name"], item["bytes"],
                          str(item["sha256"])[:12]))
        click.echo("out_dir: %s" % report["out_dir"])
        # Said out loud rather than left to the operator's memory of which
        # version wrote the file: a backup without assignment coverage restores
        # to 沿用原权限, which is a fact about the restored state.
        restore = report.get("assignment_restore") or {}
        if restore.get("reason") == "no_configured_assignments":
            click.echo("note: this backup predates Agent assignment; no"
                       " connection here is configured, so nothing is widened.")
        if report["dry_run"]:
            click.echo("Dry run: pass --yes to write.")
        else:
            click.echo("restored: %d" % len(report["written"]))
