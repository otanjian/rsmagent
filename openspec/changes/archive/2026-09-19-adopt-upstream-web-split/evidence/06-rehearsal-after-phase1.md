# Rehearsal after phase 1 — what the split actually changed

Date: 2026-09-19
Rehearsal clone: `/tmp/rehearsal/repo` (isolated, `--no-hardlinks`)
Head rehearsed: `7de163e4` (phase 1)
Source: `origin/master` @ `8f1b19f1`
Target line: `origin/rdai` @ `b5c5090f`
Merge base: `e5e2a52d`

Run with `PYTHON=/usr/bin/python3 scripts/sync-from-master.sh origin master`
(diagnostic: it merges and always aborts, so nothing is committed here).

## Conflict inventory

| | pre-phase-1 | after phase 1 |
|---|---|---|
| conflicted files | 45 | **46** |
| baseline-expected (`scripts/conflict-baseline.txt`) | 21 | 21 |
| new drift | 24 | 25 |

Set difference against the pre-phase-1 rehearsal
(`/tmp/rsmagent-sync-20260919-000059/conflicts.txt`):

- **introduced:** `tests/test_qianfan_provider.py`
- **removed:** none

`tests/test_qianfan_provider.py` is the one deliberate side effect: phase 1
retargeted its source assertion from `channel/web/web_channel.py` to the web
layer, upstream also edits that file, and the two edits meet. The resolution is
to keep the retargeted assertion. It is registered in
`tasks.md` (3.10) with the other web test drift for phase 2.

## `channel/web/web_channel.py` is still a conflict — with a different shape

Before phase 1 the file could not be merged mechanically: 8,409 lines of fork
monolith (with the fork's authorization interwoven into 56 upstream handler
bodies) against upstream's 177-line URL table. The merge produced a conflict
spanning essentially the whole file, and no per-hunk decision could preserve
both sides.

Now:

| | lines | conflicted bytes |
|---|---|---|
| upstream `web_channel.py` | 177 | — |
| fork entry module | 627 | — |
| merged result | — | 25,397 of 25,659 (3 hunks) |

The conflict still spans nearly the whole file — git finds no common context
between a 627-line file and a 177-line one — but both sides are now small
enough to resolve *by composition rather than by merging text*: take upstream's
shape and add the fork's `channel/web/fork/...` handler imports. That is a
decision a reviewer can check.

The part that made it unmergeable is gone from the conflict entirely: the fork's
implementation (686,092 bytes, 22 modules) now lives in `channel/web/fork/**`,
files upstream does not have, so the merge never sees them.

## What this does and does not establish

Establishes: the structural blocker recorded in
`/tmp/rsmagent-sync-20260919-000059/DIAGNOSTIC.md` — "resolution requires
re-anchoring the fork identity model onto upstream's split" — has been carried
out, and the remaining `web_channel.py` conflict is a reviewable composition
instead of an unresolvable one.

Does not establish:

- the merge is not done — 46 files still conflict, 25 of them drift against the
  baseline that phase 2 has to adjudicate file by file;
- `channel/web/static/js/console.js` and `channel/web/static/css/console.css`
  are still `modify/delete` conflicts (upstream deleted and split them; the
  fork still holds 932 KB and 197 KB monoliths). **Phase 1 did not touch these**
  — the frontend split is phase 3, and until it lands the merge cannot go in.
  `console.css` is newly drifted relative to the baseline because upstream
  changed the file it deletes; `console.js` was already a known conflict.
