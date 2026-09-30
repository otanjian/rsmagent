# Phase 2 evidence summary

Change: `add-desktop-remote-web-workbench`

Capability switch **`desktop_local_files_enabled` remains false**. This document
records the macOS-testable slice evidence for Groups 7–12. Packaging, Windows
fs-guard/signing, and the full 512 MiB / 100-connection load run are **not**
claimed here.

## Per-group evidence

| Group | Evidence file | Tests (this machine) |
|---|---|---|
| 7 fs-guard (macOS) | `evidence/phase-2-group-7.md` | cargo 51 + process probes |
| 8 devices/bindings | tasks §8 | `test_desktop_file_access.py` 18 + local-files.cjs 12 |
| 9 gateway | `evidence/phase-2-group-9.md` | gateway.py 14 + device-connection.cjs 6 |
| 10 transfers | `evidence/phase-2-group-10.md` | transfer.py 11 + transfer_frontend.cjs 4 |
| 11 publish / tools | `evidence/phase-2-group-11.md` | publish.py 7 |
| 12 UI / gates | this file + bridge.cjs / phase2_gates.py | see below |

## Cross-cutting verification (Groups 10–12)

```
.venv/bin/python -m pytest \
  tests/test_desktop_phase2_gates.py \
  tests/test_desktop_publish.py \
  tests/test_desktop_transfer.py \
  tests/test_desktop_gateway.py -q -p no:randomly
→ 35 passed

node --test \
  tests/test_desktop_local_files_bridge.cjs \
  tests/test_desktop_host_frontend.cjs \
  tests/test_desktop_transfer_frontend.cjs \
  tests/test_desktop_local_files.cjs
→ 41 passed

scripts/check-route-coverage.py → OK
openspec validate add-desktop-remote-web-workbench --strict → valid
```

## Acceptance ID map (phase 2)

| ID | Status on this machine |
|---|---|
| F01–F02, F08 | Covered in file_access / local-files |
| F03–F06 | macOS helper probes in Group 7; Windows deferred |
| F07 | gateway dual-lease / epoch fencing |
| F09–F11 | transfer suite |
| F12–F13, F15–F16 | publish + client_files materialize |
| F14 | phase-1 downloads suite (save-as) |
| F17 | `saveAsApprovalApplicability` / refuse write-back |
| Perf (100/20/512MiB) | **Not run** — micro RSS/cancel smoke only; do not open the switch on this evidence alone |

## Platform helper

- macOS: `desktop/native/fs-guard` openat+O_NOFOLLOW path (Group 7)
- Windows: deferred per current direction

## Switch gate

Do **not** set `desktop_local_files_enabled=true` until:

1. Phase-1 evidence remains green
2. This phase-2 slice is accepted on the target platform
3. Full performance baseline is recorded on a load host (or explicitly waived)
4. Windows (if claimed) has its own helper/signing evidence
