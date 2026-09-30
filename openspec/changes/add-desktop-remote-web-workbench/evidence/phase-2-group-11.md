# Phase 2 Group 11 evidence — publish ledger & client_files

Change: `add-desktop-remote-web-workbench` · tasks 11.1–11.8

## What landed

| Piece | Location |
|---|---|
| Migration 40 | `auth/store.py:_migration_40` — `desktop_publish_ledger`, `desktop_run_inputs` |
| Publish / reconciler | `integrations/desktop/publish.py` |
| Commit ledger wiring | `integrations/desktop/transfers.py:commit_transfer` |
| Staging invisibility | `channel/web/fork/handlers/files.py:_db_path_visible` via `path_is_unpublished_staging` |
| Agent tool | `agent/tools/client_files/` (+ `__init__.py` export) |
| Command enqueue without Bearer | `CommandService.create_command_for_context` |

## Commit / recover protocol

1. Integrity + B-order re-check
2. Persist ledger `intent` + transfer `verifying` **before** rename
3. `os.replace` staging → `desktop-inputs/<id>/<safe_name>`
4. Ledger `renamed` → transfer `publishing` → `committed` + reservation `committed` + audit
5. Reconciler finishes orphans at `intent`/`renamed`; never re-meters a committed reservation
6. `audit_probe` failure → `audit_unavailable` 503 with **no** `artifact_ref`

## client_files

- `is_available()` follows `desktop_local_files` capability (still closed in deployment)
- Device ops enqueue durable commands after AccessService B-order checks
- `materialize` of a **committed** transfer copies into `agent_user_work_dir/.../desktop-inputs/<run_id>/` and returns the **server** path + `source_version` (F16 offline-safe)
- Progress phases: `pending_device` / `reading` / `transferring` / `ready` / `error` via `client_files_progress`

## Tests

```
.venv/bin/python -m pytest tests/test_desktop_publish.py \
  tests/test_desktop_transfer.py -q -p no:randomly
→ 18 passed
```

| ID | Coverage |
|---|---|
| F12 | `test_reconciler_finishes_rename_crash` |
| F13 | PDF bytes materialized to server work dir |
| F15 | `test_delete_run_input_releases_stock` |
| F16 | offline materialize of committed transfer (no device wake) |

## Still deferred

- Capability switch remains **false**
- Group 12 UI panel / F01–F17 full matrix / performance baseline
- Packaging / Windows remain out of scope
