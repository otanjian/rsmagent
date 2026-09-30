# Phase 2 Group 10 evidence — quotas & chunked uploads

Change: `add-desktop-remote-web-workbench` · tasks 10.1–10.6

## What landed

| Piece | Location |
|---|---|
| Migration 39 | `auth/store.py:_migration_39` — `desktop_transfers` / `desktop_transfer_chunks` / `desktop_storage_reservations` |
| Transfer service | `integrations/desktop/transfers.py` |
| HTTP handlers | `channel/web/fork/handlers/desktop.py` (+ `route_registry` / `web_channel` imports) |
| Desktop single-chunk window | `desktop/src/main/local-files/transfer.ts` |

## Quota model (task 10.1)

- Limits read from existing `quota_limits` rows for metric `storage_bytes` (tenant `user_id=''` and optional per-user).
- Occupancy = `SUM(reserved_bytes)` where reservation state ∈ `{reserved, committed}`.
- **Does not** write or read `quota_usage` day/month windows (those auto-clear; contracts §6 forbid that for stock).
- Quota store probe failure → `quota_unavailable` 503 (fail-closed, never fail-open).
- Cancel / expire releases `reserved` → `released`; commit flips reservation to `committed`.

## Protocol notes

- Create is **native Bearer only**; Cookie / unpaired Web → `auth_required`.
- Idempotency key: `(command_id, source_version)`.
- Chunks are sequential; same offset+digest is idempotent; different digest → `chunk_conflict` 409.
- `storage_rel` / staging absolute paths never appear in the public projection.
- Commit re-checks source_version (→ `file_changed`), final sha256 (→ `checksum_mismatch`), then `verifying` → rename → `committed`. Group 11 owns the durable publish ledger / reconciler hardening around crash windows.

## Tests run (this machine)

```
.venv/bin/python -m pytest tests/test_desktop_transfer.py \
  tests/test_desktop_gateway.py tests/test_desktop_file_access.py \
  tests/test_desktop_migration_drill.py -q -p no:randomly
→ 56 passed

node --test tests/test_desktop_transfer_frontend.cjs \
  tests/test_desktop_local_files.cjs
→ 16 passed

.venv/bin/python scripts/check-route-coverage.py → OK (221 routes)
openspec validate add-desktop-remote-web-workbench --strict → valid
```

## Acceptance mapping

| ID | Coverage |
|---|---|
| F09 | `test_commit_rejects_source_version_change_and_bad_digest`; node retry-once / give-up |
| F10 | `test_sequential_chunks_idempotent_conflict`; `test_cancel_releases_reservation_and_beats_commit` |
| F11 | `test_concurrent_reservation_does_not_overdraw`; `test_quota_unavailable_fails_closed`; `test_cancel_frees_quota_for_next_transfer` |

## Still deferred / next

- Capability switch `desktop_local_files_enabled` remains **false**.
- Group 11: durable verifying/publishing ledger, reconciler, `client_files` tool, browse visibility of committed-only inputs.
- Packaging / Windows probes remain out of scope per current direction.
