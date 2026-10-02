# Phase 3 evidence (partial — non-build)

Change: `add-desktop-remote-web-workbench`

## Completed without packaging

| Area | Evidence |
|---|---|
| Lifecycle prefs / wake / notification click | `tests/test_desktop_lifecycle.cjs` 5 passed； `desktop/src/main/remote/lifecycle.ts`；`powerMonitor` + quit grant clear in `index.ts` |
| Diagnostics scrub / protocol negotiate | `tests/test_desktop_diagnostics.cjs` 3 passed |
| User + ops docs | `docs/user-guide.md`, `docs/ops-runbook.md` |

## Explicitly deferred (builds / signed packages)

- 13.5 L01–L04 packaged sleep/tray/notification permission drills
- 14.2 / 14.3 / 14.5 / 14.6 update feed, signing, notarization, L05–L08
- Groups 15–16 entire local-processing worker chain
- 17.3 live migration-recovery drill; 17.6 archive

## Switches

All phase switches remain **false**. Do not enable notifications or local processing on this evidence alone.
