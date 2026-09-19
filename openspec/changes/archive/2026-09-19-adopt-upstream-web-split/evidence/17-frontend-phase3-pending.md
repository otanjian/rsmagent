# 17 — Phase 3 is not landed yet: what the merge serves today

This records the state of the frontend at the end of Phase 2 (merge conflicts
resolved, backend green) so that the gap between "upstream's console" and "the
console the fork serves" is explicit rather than a set of quietly failing tests.

## What the merge brought in, and what is served

The merge imports upstream's **split console**: `channel/web/static/js/{core,chat,views}/**`,
`channel/web/static/css/**`, the thin `chat.html` shell, the server-side
`<!--#include-->` expansion in `channel/web/core/template.py`, the router, and
`tools/check-load-order.mjs`.

The fork still **serves its monolith**: `channel/web/static/js/console.js`,
`channel/web/static/css/console.css`, and the fork's `channel/web/chat.html`.
Those three were resolved `keep-fork` for this merge (recorded in
`10-merge-dispositions.md` and `13-phase2-merge-dispositions.md`), so:

- the console a user sees is byte-for-byte the pre-merge console;
- upstream's split tree is present on disk but nothing loads it yet;
- upstream's own console tests assert the split page and therefore fail.

## Upstream increments not yet served (the cost being carried)

Enumerated so the `keep-fork` is a decision, not an accident:

1. the split shell (`chat.html` markup and its script order);
2. the router's vocabulary in the address bar (`views/`-based tabs, `#/…` routes);
3. per-file mtime `?v=` stamping of `assets/js/**` / `assets/css/**`;
4. the one-click update menu (`id="update-menu"`, `/api/update/check|start`);
5. whatever feature/fix work upstream landed inside the split modules.

Items 1–4 are exactly what the three skipped upstream test modules pin. Item 5
is bounded by the port: every fork-owned module is a copy of upstream's module
plus the fork's hunks, so the drop is the fork's *replacement* of the deferred
regions, itemised in `frontend_adjudication.md` (98 regions).

## Tests marked as this divergence

| file | marker | tests |
| --- | --- | --- |
| `tests/test_web_console_assets.py` | module-level `pytest.mark.skip` | 11 |
| `tests/test_web_console_routing.py` | module-level `pytest.mark.skip` | 3 |
| `tests/test_web_console_update.py::test_frontend_contract` | `pytest.skip` | 1 |

Each marker names this change and Phase 3 (tasks 4.4–4.9) and comes off with the
first Phase 3 task that serves the split page. `tests/test_tool_display.py`
reads the monolith directly with the same note (it switches to the `console_js()`
helper when the split lands).

## What Phase 3 still owes

Measured, reproducible state (`scripts/migration/port_frontend.py` on this tree;
`upstream/master` = `8f1b19f1`):

- **30 fork-owned modules staged** — 275 of 362 JS change clusters and 71 of 79
  CSS clusters re-anchored mechanically into `static/js/fork/**` and
  `static/css/fork/**`; 0 fork-unique lines unaccounted for
  (`verify_frontend_port.py`).
- **98 regions need adjudication** (`frontend_adjudication.md`, 4 914 fork lines):
  40 where upstream rewrote the surrounding code, 37 whose splice left the module
  unparseable, 18 with no verified context, 3 straddling an upstream module
  boundary. Each carries the fork's text and, where one exists, upstream's.
- **~30 fork `.cjs` frontend tests** (`test_*_frontend.cjs`, `branding_frontend.test.cjs`,
  `test_console_i18n_parity.cjs`) read `console.js`; they must read the assembled
  fork module set instead (`tests/_web_layer.cjs` exists for this).
- **The override map is not wired**: the fork's page handler must assemble the
  page with upstream's `core/template.py` and substitute `assets/js/<mod>.js` →
  `assets/js/fork/<mod>.js` (and the CSS equivalent) where the fork ported one.
- **The drift manifest** (`static/js/fork/manifest.json`, upstream path + sha256
  at port time) and the `check-load-order.mjs` run over the fork's effective
  order are not in place.

`openspec/changes/adopt-upstream-web-split/tasks.md` carries these as tasks
4.4c–4.9. Until they land, the fork's console is the monolith and the skipped
tests above are the deliberate, itemised divergence.
