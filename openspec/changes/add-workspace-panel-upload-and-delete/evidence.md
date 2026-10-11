# Evidence — `add-workspace-panel-upload-and-delete`

Every number below was produced by running the commands named beside it on this
host, in this working tree. Nothing here restates the design: where a claim in
`design.md` is asserted, the measurement that decides it is quoted, and where
the measurement disagrees with the design the disagreement is written down
instead of being smoothed over (see §7, the proxy ceiling).

Reproduce with:

```
python scripts/verify_workspace_upload_scale.py                        # §2, §3, §4, §5
python scripts/verify_workspace_proxy_limit.py                         # §7
python -m pytest tests/test_workspace_upload_delete.py \
    tests/test_workspace_trash.py tests/test_safe_fs.py \
    tests/test_workspace_user_dir.py tests/test_workspace_edit.py \
    tests/test_workspace_layout_template.py \
    tests/test_agent_user_file_access.py tests/test_agent_user_file_http.py \
    tests/test_private_agent_file_scope.py tests/test_object_scope.py \
    tests/test_route_registry.py -q                                    # §1
node tests/test_console_workspace_upload_frontend.cjs                  # §1
node tests/test_console_workspace_delete_frontend.cjs                  # §1
python scripts/check-route-coverage.py                                 # §6
python scripts/check_change_deltas.py add-workspace-panel-upload-and-delete
```

---

## 1. Test results

### 1.1 Backend, over the real WSGI app

`python -m pytest ... -q` over the Python suites task 8.1 names:

```
1 failed, 269 passed, 2 warnings, 1 error, 11 subtests passed in 479.76s
```

Both non-passes are **pre-existing and unrelated**, and both were confirmed by
stashing this change's source edits and re-running:

| Result | Test | Verified pre-existing |
| --- | --- | --- |
| `failed` | `test_private_agent_file_scope.py::PrivateAgentFileScopeTests::test_file_serve_still_serves_shared_agent_files` — `404 Not Found` for a shared Agent file | yes — identical result with `common/safe_fs.py`, `agent/workspace/service.py` and `channel/web/` stashed |
| `error` (teardown) | session fixture `workspace_out_of_the_way` cannot delete its temp workspace: `PermissionError: [WinError 32] ... cow-tests-*\cow\memory\long-term\index.db` | yes — identical with the change stashed (`cow-tests-b9_p3q_3`) |

The teardown error is a Windows-only file-lock artifact of the **test harness**:
a memory-index SQLite connection is still open when the session temp directory is
removed, so the cleanup raises *after* every assertion has passed. It touches
neither the file panel nor any file this change edits.

Per-suite counts for the suites this change owns or extends:

| Suite | Result | Status |
| --- | --- | --- |
| `tests/test_workspace_upload_delete.py` | **46 passed** | new file |
| `tests/test_workspace_trash.py` | **35 passed** | new file |
| `tests/test_safe_fs.py` | **20 passed**, 8 subtests | +3 cases added here (§8.1) |
| `tests/test_workspace_user_dir.py` | passed | unchanged |
| `tests/test_workspace_edit.py` | passed | unchanged |
| `tests/test_workspace_layout_template.py` | 10 passed | unchanged |
| `tests/test_console_upload_transport.py` | 9 passed | unchanged (the un-widened surface) |

### 1.2 Frontend (Node, `node:test`)

| Suite | pass | fail |
| --- | --- | --- |
| `test_console_workspace_upload_frontend.cjs` | 31 | 0 |
| `test_console_workspace_delete_frontend.cjs` | 28 | 0 |
| `test_console_workspace_frontend.cjs` | 22 | 0 |
| `test_console_i18n_parity.cjs` | 6 | 0 |
| `test_console_i18n_coverage.cjs` | 4 | 0 |
| `test_console_upload_frontend.cjs` (chat attachments, unchanged) | 6 | 0 |
| `test_console_view_registry.cjs` | 7 | 0 |
| `test_knowledge_console_frontend.cjs` | 10 | 0 |

### 1.3 i18n snapshot

51 keys × 3 languages were added by this change, plus one more in §8 — 52 keys,
156 strings. The regenerated fixture is **purely additive**:

```
tests/fixtures/console_i18n_snapshot.json | 156 ++++++++++++++++++++++++++++++
1 file changed, 156 insertions(+)
```

`scripts/regen_i18n_snapshot.cjs` refuses to write unless every key the fixture
already had keeps its exact value, and it preserves the fixture's existing
language order. Both guards earned their place: re-sorting the three languages
alone (nothing else) rewrites **5779 of the 5840 lines**, turning a one-key
change into an unreviewable diff.

---

## 2. Scale — 5000 small files across multiple folders

`python scripts/verify_workspace_upload_scale.py --files 5000 --big-mb 200`

The panel's shape, reproduced: one file per request, **3 in flight**, folder
structure three levels deep (`tree/g00..g11/s00..s02/`).

```
scale.files: 5000
scale.folders: 12
scale.concurrency: 3
scale.requests: 5000
scale.saved: 5000
scale.failed: 0
scale.failure_details: []
scale.renamed: 0
scale.declared_bytes: 40886124
scale.sum_served_bytes: 40886124
scale.on_disk_files: 5000
scale.on_disk_bytes: 40886124
scale.structure_mismatches: 0
scale.request_body_bytes: 42536124
scale.request_overhead_ratio: 1.0404
scale.wall_seconds: 212.05
scale.mean_request_ms: 42.41
scale.progress_events: 5000
scale.progress_monotonic: True
scale.progress_final_bytes: 40886124
scale.progress_final_vs_disk: 0.0
```

Every dimension that carries a correctness claim reproduced identically on a
second, independent run; only the two timing figures moved
(`wall_seconds` 227.9 → 212.1, `mean_request_ms` 45.6 → 42.4, i.e. ~7% run-to-run
variance on a shared host). The counts, byte totals, ratio, and progress figures
were byte-identical across both runs.

What each row settles:

- **5000 files, 0 failures, 0 renames, 0 structure mismatches.** Every file's
  size on disk equals the size declared, and the set of relative paths on disk
  is exactly the set that was sent — the folder hierarchy survived intact.
- **`request_overhead_ratio` = 1.0404.** Multipart headers and boundaries cost
  4.04% over the file bytes. This is the number task 4.6's proportional scaling
  (`scale = file bytes / event.total`) exists to correct; without it the panel's
  byte readout would be 4% high on every drop.
- **`progress_final_vs_disk` = 0.0** — the readout (sum of settled file sizes)
  equals the bytes on disk to the byte, well inside the < 1% acceptance bar.
- **`progress_monotonic` = True** across 5000 settled files. The readout never
  goes backwards, which is what the two-phase progress (discover, then upload)
  is designed to guarantee.
- **212.1 s wall / 42.4 ms per request** (227.9 s / 45.6 ms on the first run).
  The per-request cost is dominated by the per-request authorization and tenant
  resolution the design predicted (D6), not by I/O: ~23.6 files/s aggregated.

## 3. Scale — one 200MB file, same code path

```
big.declared_bytes: 209715200
big.status: success
big.response_bytes: 209715200
big.on_disk_bytes: 209715200
big.digest_match: True
big.wall_seconds: 1.171
big.mb_per_second: 179.2
```

(Second run: 1.255 s / 167.1 MB/s. `big.code: None` on both runs is the success
case — there is no code to carry.)

`digest_match` compares SHA-256 of what was sent against SHA-256 of what landed,
so the 200MB file is verified byte-for-byte, not merely present. The file went
through the same `POST /api/workspace/upload` path as each of the 5000 small
files above — no branch, no "one big file owns a batch" special case.

## 4. Ceilings — the app's own limit and the neighbours it does not affect

At 200MB + 1 KiB:

```
over_limit.oversize_bytes: 209716224
over_limit.status: error
over_limit.code: too_large
over_limit.message: file is larger than 209715200 bytes
over_limit.no_partial_file: True
over_limit.leftovers: []
over_limit.neighbour_status: success
over_limit.directory: ['fine.txt']
```

- The refusal carries a **stable code** and the measured limit, so the panel
  names the reason instead of showing "it failed".
- **Nothing is left behind**: no `too-big.bin`, no `.ws-upload-*.part` sibling.
  The temp-then-rename write is what makes the connection-dies-mid-transfer case
  leave the real name free.
- **The neighbour still lands.** A refused file is one refused *request*, which
  is the whole point of D6: a drop containing one oversized file does not fail
  the rest.

## 5. Destructive verification on real data

Not a stand-in: a real directory tree with substructure, a 1 MiB file, a
non-ASCII name, and a deep path.

### 5.1 delete → restore is lossless

```
destructive.delete_deleted: ['agents/private-agent/trashwalk/dir-a']
destructive.delete_source_gone: True
destructive.trash_holds_it: True
destructive.trash_entry: {'rel': 'agents/private-agent/trashwalk/dir-a',
                          'kind': 'directory', 'size': 0}
destructive.restore_restored: [{'index': 0,
    'rel': 'agents/private-agent/trashwalk/dir-a',
    'path': 'agents/private-agent/trashwalk/dir-a', 'renamed': False}]
destructive.restore_failed: []
destructive.restore_identical: True
destructive.restore_paths: ['dir-a/a.txt', 'dir-a/b.bin', 'dir-a/sub/c.txt',
                            'dir-a/sub/deep/d.txt', 'dir-a/中文名.txt']
destructive.batch_removed_after_full_restore: True
```

`restore_identical: True` is a SHA-256 comparison of the **whole tree** before
the delete against the tree after the restore — same key set, same digest per
file. The 1 MiB file, the deep path and the non-ASCII name all came back
intact. The empty batch directory is gone once its last item was restored.

### 5.2 an occupied destination is renamed around, never over

```
destructive.occupied_restore: [{'index': 0,
    'rel': 'agents/private-agent/trashwalk/dir-a',
    'path': 'agents/private-agent/trashwalk/dir-a (1)', 'renamed': True}]
destructive.occupier_untouched: True
destructive.original_location_now: ['occupant.txt']
destructive.renamed_copy_identical: True
```

The occupant's bytes are compared before and after: untouched. The restored tree
is verified digest-for-digest against the original, under its new name. The
response reports the **actual** path (`dir-a (1)`), so the panel can show the
user where it really landed.

### 5.3 purge destroys only the bin

```
destructive.purge: {'status': 'success', 'purged': 1, 'failed': []}
destructive.purge_removed_from_bin: ['agents/private-agent/drop/big/one.bin']
destructive.purge_left_live_files_alone: ['dir-a/occupant.txt']
destructive.empty_all: {'status': 'success', 'purged': 1, 'failed': []}
destructive.trash_now: []
```

Purging one batch removed exactly that batch; a full purge emptied the bin and
left every live file in place.

### 5.4 every refusal, by its stable code

```
guards.agent_internal(file)   AGENT.md                 -> agent_internal
guards.agent_internal(dir)    memory/                  -> agent_internal
guards.user_container         user/                    -> user_container
guards.own_user_dir           user/<uid>               -> user_container
guards.trash_not_targetable   user/<uid>/.trash        -> trash_not_targetable
guards.range_root             <agent dir>              -> own_directory_root
guards.escape                 ../../AGENT.md           -> outside_own_directory
guards.absolute               /etc/passwd              -> outside_own_directory
guards.missing                <absent path>            -> not_found
guards.upload.into_trash      upload into .trash       -> trash_not_targetable
```

`user/<uid>` returns `user_container` rather than a dedicated code because it is
the bin's parent: deleting it would carry the recycle bin away and make the very
same delete unrecoverable (design D13).

### 5.5 the bin is hidden by rule, not by its dot prefix

```
hidden.tree_with_show_hidden: ['mine.txt']
hidden.dot_trash_listed: False
hidden.search_hits_for_trashed_file: []
hidden.search_leaks: []
```

A visible neighbour (`mine.txt`) was uploaded first, so the listing is proof of
an **exclusion** rather than of an empty directory. `.trash` stays out of both
listing and search **with `show_hidden=1`** — the case a dot-prefix rule silently
fails (design D12).

## 6. Scope boundaries, measured

Cross-member and cross-scope attempts all refused without side effects:

```
boundary.shared_agent_internal  agents/<shared>/memory          -> outside_own_directory
boundary.colleague_subtree      agents/<shared>/user/<bob>      -> outside_own_directory
boundary.shared_root            agents/<shared>                 -> outside_own_directory
boundary.upload_to_colleague    upload into agents/.../user/<bob> -> outside_own_directory
boundary.own_subtree_writable   upload into own user/<uid>      -> success
```

The last row is the control: the same member and the same request shape succeed
inside their own subtree, so the four refusals are the scope rule talking and not
a handler that refuses everything.

Route bookkeeping:

```
python scripts/check-route-coverage.py
route-coverage: 209 routes (68 upstream, 141 fork), 255 method entries
OK
```

### 6.1 OpenSpec delta check

```
python scripts/check_change_deltas.py add-workspace-panel-upload-and-delete
FAIL (proposed): 3 problem(s)
  - agent/memory/conversation_store.py: marked seam:conversation-store but this change never names it
  - agent/tools/scheduler/integration.py: marked seam:scheduler but this change never names it
  - tests/test_scheduler_web_update.py: marked seam:scheduler but this change never names it
```

All three findings are **conflict-coverage** rows, and none is a delta finding:

- **The delta half passes cleanly.** The checker reports no `ADDED` restating an
  existing requirement (neither in `platform-file-browsing` nor in any other
  capability), and no `MODIFIED` header that fails to match the baseline — which
  is the half task 8.6 names. Confirmed by running the delta half alone:
  `check_change_deltas.py add-workspace-panel-upload-and-delete --conflict-baseline <empty>`
  → `OK (proposed)`.
- **The three rows belong to the sync change.** `scripts/conflict-baseline.txt`
  is the upstream-sync conflict baseline (`adopt-upstream-web-split` /
  `fork-decoupling-and-tenant-hardening`), and its `seam:` rows are the
  conversation-store and scheduler seams *those* changes own. Naming them in this
  change's docs to silence the checker would be a false claim of coverage over
  another change's seams, so they are recorded here instead:
  `check_change_deltas.py fork-decoupling-and-tenant-hardening` → `OK (applied)`,
  `check_change_deltas.py adopt-upstream-web-split` → `OK (applied)`.

The merge semantics task 8.6 asks to confirm: this change's delta is
**`ADDED`-only** — four new requirements appended to `platform-file-browsing`,
which already holds three. Nothing in the existing
`land-shared-agent-panel-on-own-files` landing rules or
`isolate-shared-agent-user-data` ownership rules is restated, modified or
widened; the new endpoints consume those rules through the same seams
(`_workspace_request_scope`, `_db_path_visible`, `classify_agent_user_path`) they
already define.

## 7. Deployment numbers — and one prerequisite that is **not** met

`python scripts/verify_workspace_proxy_limit.py` reads the deployed nginx config
and then proves its value over real TLS:

```
config.directives: [(27, 'client_max_body_size 100M;'),
                    (168, 'client_max_body_size 1024M;'),
                    (201, 'client_max_body_size 100M;'),
                    (216, 'client_max_body_size 100M;'),
                    (226, 'client_max_body_size 100M;')]
config.smallest_bytes: 104857600
config.smallest_mib: 100.0
probe.1mib.server: nginx/1.30.4
probe.1mib.status: 404
probe.over_ceiling.requested_bytes: 104857601
probe.over_ceiling.status: 413
probe.over_ceiling.seconds: 5.008
probe.over_ceiling.server: nginx/1.30.4
probe.over_ceiling.body_head: <html>
verdict.configured_ceiling_meets_requirement: False
verdict.proxy_accepts_single_file_ceiling: False
```

Read this carefully, because the two probe rows say different things:

- **`probe.1mib` → 404 from `nginx/1.30.4`** is the proxy *forwarding*: the
  request reached the application at `127.0.0.1:9900`, which answered its own
  "not found" for an unauthenticated probe (`probe.1mib.body_head` is the
  application's 9-byte `not found`, not nginx's HTML). So this is a real
  end-to-end round trip, not a proxy-only port check.
- **`probe.over_ceiling` → 413 with an nginx HTML body**, decided in 5.0 s
  without a byte of the body being sent — so it is the proxy refusing, and there
  is no `code` for the panel to read.

**Conclusion, stated plainly: the deployed ceiling is 100 MiB and the panel
promises 200 MiB per file, so on this host today a 100–200 MiB file is refused by
the proxy (413), not by the application.** The code is correct and the panel
distinguishes the two (it reads a code-less 413 as `too_large`), but the operator
prerequisite in `proposal.md` is outstanding. The required change is one line —
`client_max_body_size` at the `http` level (line 27) raised to ≥ 210 MiB; note
that line 168's `1024M` is inside a Gitea `location` and does not apply here.

### 7.1 The three failure modes, and which layer answers

| Layer | Condition | Signal the panel sees | Panel wording |
| --- | --- | --- | --- |
| Reverse proxy | body over `client_max_body_size` | HTTP **413**, nginx HTML, **no** `code` | `too_large` (derived from the status) |
| Application | body over `WS_UPLOAD_MAX_BYTES` (200 MiB) | `{"code": "too_large"}`, message names the byte limit | `too_large` (from the code) |
| Application | payload did not arrive whole | `{"code": "incomplete_upload"}` | `ws_upload_err_incomplete` |
| Application | bytes fine, destination out of scope | `{"code": "outside_own_directory"}` | `ws_lock_outside` |

The last two rows are deliberately distinct: a cut-short transfer is remedied by
uploading again, an oversized file by splitting it, and a scope refusal by
choosing another destination.

### 7.2 Temporary disk

`web.py`'s multipart parser spools each `filename` part to a system temp file
before the handler sees it, so **one request's temp footprint is one file**
(≤ 200 MiB), independent of how many files the drop holds. This is confirmed by
the 5000-file run: `scale.requests: 5000` with a 200 MiB ceiling never exceeded
one file's worth of spool at a time (peak ≈ concurrency × one file = 3 × 200 MiB
worst case) and completed with 0 failures on a host with 68.7 GiB free.

### 7.3 Recycle-bin capacity

Occupancy grows by the size of what is deleted and shrinks only on purge or
retention expiry. The bound is `Σ(deleted bytes within TRASH_RETENTION_SECONDS)`,
so a deployment that deletes 10 GiB/day with a 30-day window needs ~300 GiB
headroom in the Agent workspaces. Knobs, in order of preference: (1) users purge
from the bin (the panel offers it, including empty-all); (2) shorten
`TRASH_RETENTION_SECONDS` (`agent/workspace/service.py`) — a redeploy, one
constant; (3) treat bin occupancy as an ordinary quota input in the
`resource-quota` capability, which this change deliberately does not implement
(design Non-Goals).

## 8. Four defects found by the acceptance runs, fixed here

The measurements above are what surfaced both; neither was visible to the unit
suites.

### 8.1 A concurrent first drop failed with `FileExistsError` — fixed

The 300-file scale run failed 2 of 300 requests:

```
scale.failure_details: [{'rel': 'tree/g01/s00/f0001.txt', 'status': 'error',
  'message': "[WinError 183] 当文件已存在时，无法创建该文件。:
  '...\\rsm-scale-pzbu14sn\\tenants\\acme\\agents\\private-agent\\drop'"}]
```

Cause: every directory-creation site in `common/safe_fs.py` was a
check-then-create, and the panel runs **three uploads at once**, so the first
drop into a folder has all three racing to create each level. On Windows (the
`dir_fd`-less path, with no already-open descriptor to lean on) the loser got
`FileExistsError` for a request that asked for nothing unusual. It was a genuine
bug on the platform this deployment runs on, not a test artifact.

Fix: `_mkdir_level()` in `common/safe_fs.py` tolerates `FileExistsError` and then
**re-validates the level**, so a symlink or a plain file planted in the gap is
still refused. All four `os.mkdir` sites now go through it (the `dir_fd` path
re-checks against the open descriptor).

Evidence:

| Run | Result |
| --- | --- |
| 300 files, before the fix | 2 failed / 300 |
| 300 files, after the fix | 0 failed / 300 |
| 5000 files, after the fix | 0 failed / 5000 |

Regression tests: `tests/test_safe_fs.py::ConcurrentCreateTests` (3 cases — a
peer winning the race is harmless; a link planted in the gap is refused; a file
planted in the gap is refused).

### 8.2 The app's own over-limit response had no machine-readable code

The first acceptance run recorded `over_limit.code: None` with the reason only in
the free-text `message`. The proxy's 413 has no code either, so the panel would
have had to string-match the message to tell the two apart — exactly the kind of
coupling that breaks silently on a wording change.

Fix: `WorkspaceUploadError` with stable codes `too_large` and `incomplete_upload`
(the latter also distinguishes a cut-short transfer, whose remedy differs), and
`_upload_error_response()` to render them. New i18n key
`ws_upload_err_incomplete`; new frontend mapping. Tests:
`tests/test_workspace_upload_delete.py::test_a_file_over_the_servers_own_ceiling_is_refused_by_code`
and `..._test_a_payload_that_does_not_arrive_whole_is_refused_by_its_own_code`;
`tests/test_console_workspace_upload_frontend.cjs` asserts all four layers stay
distinguishable.

### 8.3 The panel's markup shipped into a file nothing renders — found at startup

Found while bringing the service up on the new code, by asking the served page
for the ids the script looks up.

The console page is assembled by `channel/web/core/template.py` from
`channel/web/chat.html` plus the fragments that shell `<!--#include-->`s. Three
fragments are included (`tasks.html`, `task-edit.html`, `run-detail.html`); the
workspace panel is **inline in the shell**. A second copy of the same panel also
exists at `channel/web/templates/views/chat.html`, and **no include reaches it**
(the only reference to that path anywhere is the example in `template.py`'s own
docstring). This change's new controls — the delete/trash buttons, the upload
progress block, the drop hint — were written into that unreachable copy.

Consequences, had it shipped: `getElementById('ws-upload-progress')` and
`getElementById('ws-btn-delete')` return `null`, so the progress bar and the
delete entry never appear, and every unit test still passes. Verified against the
page the server actually returned:

```
ids in the served page before the fix:
  ws-body-files, ws-body-preview, ws-breadcrumb, ws-btn-copy, ws-btn-download,
  ws-btn-edit, ws-btn-edit-cancel, ws-btn-external, ws-btn-save, ws-file-list,
  ws-preview-content, ws-preview-title, ws-resizer, ws-search-input
  -> no ws-upload-progress, no ws-btn-delete, no ws-btn-trash, no ws-drop-hint
```

**Why no test caught it, and what now does.** The frontend suites build their DOM
by hand (`tests/_console_dom.cjs` registers the ids a test needs), which is
correct for testing the *script* — but it means no suite ever read the page. The
two files drifted with the whole suite green.

Fix: the markup is now in `channel/web/chat.html`, the file the server renders
(the unreachable copy is kept in step, since it is the upstream-shaped view).

New tests in `tests/test_console_workspace_upload_frontend.cjs`, both derived
from the script rather than maintained by hand:

| Test | What it asserts |
| --- | --- |
| `every id the panel looks up exists in the page the server ships` | every `getElementById('<literal>')` in `workspace.js` is present in the page assembled the way `template.render` assembles it — minus ids the script assigns itself (`x.id = 'ws-editor'`), which are sourced from the script, not markup |
| `the panel markup is the assembled page's, not a fragment nobody renders` | the shipped page itself carries the panel's controls, so an edit that lands only in an unrendered copy fails |

Both are falsifiable, which was checked rather than assumed: run against the
pre-fix page, the id check reports exactly

```
ws-btn-delete, ws-btn-purge, ws-btn-restore, ws-drop-hint, ws-drop-hint-text,
ws-upload-count, ws-upload-detail, ws-upload-dismiss, ws-upload-fill,
ws-upload-progress, ws-upload-retry, ws-upload-title          (12 ids)
```

and `ws-editor` is correctly *not* among them.

> Three of those ids (`ws-btn-delete`, `ws-btn-purge`, `ws-btn-restore`) were
> later removed on purpose when the selection model became per-row actions —
> see §8.4. The id check is derived from the script, so it followed the removal
> on its own; the page-markup test was the one that had to be updated, and it now
> pins those ids as *absent*.

Post-fix, the served page carried every panel id it then had, with no duplicate
ids (`template.render('chat.html')` → 294138 chars, missing ids: none, duplicate
ids: none). No restart was needed to apply it: the assembler caches each fragment
against its mtime, so an edit is picked up on the next request.

This is the one gap the browser-level suites would have caught, and it is why
§11 lists the absent browser pass as a risk rather than a formality.

### 8.4 The tick box was inert, and undelatable rows still offered one

Found on the live panel, by asking what the per-row tick boxes were *for*: they
looked decorative, and the honest answer was that they behaved that way.

Two wiring defects, one on each side of the same column:

1. **A tick was invisible.** Every one of the three row renderers ships
   `<span class="ws-row-check"><i class="far fa-square"></i></span>`, nothing ever
   swapped that glyph, and no stylesheet in the tree named `.ws-checked` — the
   class the selection is read from. So ticking a row changed the toolbar button
   and nothing else. The one interaction the whole batch delete is built on had
   no feedback of its own.

2. **The lock rule never applied, and a locked row still offered a box.** The
   comment above the rule says a row the caller may not delete "carries the lock
   instead of a checkbox", but the rule was written as
   `.ws-file-row.ws-row-locked` — a class nothing ever sets, because the row
   carries an *attribute* (`data-ws-locked="1"`, set in `wsRowAttrs`). So the
   intended dimming never happened, and a row the server had already marked
   undeletable rendered a tick box that the action would then exclude.

The suspicion that the *lock set itself* was wrong did not survive checking: the
row data on the workspace in the report was read back through
`agent.workspace.service.undeletable_reason`, and the locks are exactly the
documented classes —

```
🔒 agent_internal   AGENT.md  MEMORY.md  RULE.md  USER.md  knowledge/  memory/  scheduler/  skills/
🔒 user_container   user/
   deletable        -p/  output/  delivery.json  regen_checklist.py
```

— so the rule was right and only its presentation was broken.

A first fix restored the missing feedback (glyph swap, tinted row, the lock rule
keyed off `data-ws-locked`). That stood, but the tick box itself did not survive
the next question: **why does a file list carry checkboxes at all?** The answer
was that delete acted on a selection, which is a batch model nothing else in the
panel used — and a locked row then had to *offer* a control whose only outcome
was to be excluded. So the model was replaced rather than repaired.

**The selection model is gone; an action belongs to its row.** Removed
altogether: the tick box markup, `.ws-row-check` / `.ws-checked`, the whole
`#ws-file-list .ws-file-row.ws-checked` query surface (`wsCheckedRows`,
`wsToggleChecked`, `wsPaintCheck`, `wsCheckRange`, `wsCheckedBytes`,
`wsCheckedBinGroups`, `wsUpdateSelectionUi`), the Ctrl/Cmd+click and Shift+click
handling, and the three toolbar buttons that had nothing left to act on
(`ws-btn-delete`, `ws-btn-restore`, `ws-btn-purge`).

| Where | Change |
| --- | --- |
| `wsRowDeleteHTML(entry)` | a row's own delete control, emitted **only** when the server said the entry is deletable; a locked row gets nothing, and its lock title carries the reason |
| `renderWorkspaceEntries` / `renderWorkspaceSearchResults` | the control is rendered last, i.e. to the right of the file name |
| `renderTrashEntries` | each bin row carries restore and purge instead — a bin entry is restored or destroyed, never deleted again |
| `askWorkspaceDelete(row)` | confirms with the same count/volume/directory wording a batch used, then posts exactly that one relative path |
| `restoreTrashRow(row)` / `askWorkspacePurge(row)` | address one bin entry by `batch_id` + `index` via `wsBinAddress`, and ignore a row with no address rather than guessing |
| list click handler | a `.ws-row-act` is checked **before** the row's own click, so pressing delete on a folder does not also open it; a bin row's own click is now inert |
| `wsUpdateToolbarState` | keeps only controls that act on the panel as a whole: enter bin, back, empty bin, refresh |
| `.ws-row-act` | muted until the row is pointed at, so at rest the list reads as names; `.ws-row-act-danger` turns red on hover |

The lock set itself was **not** wrong: read back through
`agent.workspace.service.undeletable_reason`, the workspace in the report locks
exactly the documented classes —

```
🔒 agent_internal   AGENT.md  MEMORY.md  RULE.md  USER.md  knowledge/  memory/  scheduler/  skills/
🔒 user_container   user/
   deletable        -p/  output/  delivery.json  regen_checklist.py
```

— so the rule was right, and what needed changing was the control it exposed.

Two details worth keeping:

- The row is reached as `row.querySelector('.ws-row-check')`-style class lookups
  only, never a descendant+tag selector: `tests/_console_dom.cjs` answers class
  selectors and **raises** on anything else, so such a selector would take the
  whole suite down instead of failing one assertion. The same harness detail is
  why the click tests fire on `#ws-file-list` with the button as `target` — it
  does not bubble, and the real listener is registered in the init section the
  harness slices off (`initWorkspaceFilesTab()` is called explicitly).
- `not_removable` maps to `ws_delete_missing` in `WS_FAILURE_KEYS`, so a
  refusal's resident label is that key and not the raw code.

**Spec check — the delta still holds.** `specs/platform-file-browsing/spec.md`
is written about API semantics, not a widget: "一次请求 SHALL 可包含多个待删条目"
(the request *may* carry several; the per-row UI simply sends one, and `wsRunDelete`
still takes a list), "确认文案 SHALL 包含本次待删的条目数与总体积" (satisfied with
count 1 and the row's own size), and the recycle-bin requirement asks for
"恢复到原位置与彻底删除" without naming a selection mechanism. The scenarios read
"用户对**已选中的**文件或目录发起删除" — the row the user acts on is that entry.
No spec text is contradicted, so the delta was not touched; what changed is the
implementation note in `tasks.md` §5.1/§5.4, which had named the batch model.

Tests, all derived from the rendered listing rather than hand-built rows where
it matters:

| Test | What it asserts |
| --- | --- |
| `a row offers its own delete control only when the server allowed it` | the *rendered* listing carries exactly one `data-ws-act="delete"`, on the deletable entry |
| `a locked row carries the reason on its lock instead of an action` | no action at all, a `ws-row-lock`, and the reason in its `title` |
| `a bin row offers restore and purge, not a delete` | both controls, neither a delete |
| `a row action is taken before the row own click, so it does not act too` | pressing a folder's delete raises the confirmation and issues **no** navigation, while the row's own click still does — a positive control, so the assertion is about routing and not a dead handler |
| `a locked row cannot be deleted even if the call is made` | the guard holds even when the function is called directly |
| `a row with no batch address is not acted on` | an unaddressable bin row is ignored, not guessed at |

Verified against the assets the server actually returns (no restart, same mtime
cache as §8.3):

```
GET /chat -> 200 (292878 bytes)
   present  ws-btn-trash / ws-btn-trash-back / ws-btn-purge-all / ws-btn-refresh
   removed  ws-btn-delete / ws-btn-restore / ws-btn-purge
  assets/js/workspace.js?v=1a0ea523a03   -> 200   askWorkspaceDelete, wsRowDeleteHTML,
                                                  restoreTrashRow, askWorkspacePurge present;
                                                  wsToggleChecked and ws-row-check absent
  assets/css/console.css?v=1a0ea4fe6d1   -> 200   .ws-row-act styled; .ws-row-check and
                                                  .ws-checked absent
```

Suites: `workspace_delete` 28, `workspace_upload` 31, `workspace` 22,
`i18n_parity` 6, `i18n_coverage` 4, `upload` 6, `view_registry` 7,
`knowledge_console` 10 — **0 failures**. The page-markup test in
`test_console_workspace_upload_frontend.cjs` now also pins the three removed
ids as *absent* from the shipped page, so the toolbar cannot quietly come back.

## 9. Retry semantics — what the wire actually does

```
retry.round1: [ok-0..ok-4 -> 'success',
               'retry/../escape.txt' -> 'outside_own_directory',
               'retry/../../user/x/.trash/hidden.txt' -> 'outside_own_directory']
retry.round2: [the same two items only, same codes]
retry.on_disk_before_duplicate: ['ok-0.txt', 'ok-1.txt', 'ok-2.txt',
                                 'ok-3.txt', 'ok-4.txt']
retry.duplicate_result: {'status': 'success', 'name': 'ok-0 (1).txt',
                         'renamed': True}
retry.on_disk_after_duplicate: ['ok-0 (1).txt', 'ok-0.txt', ...]
```

Two things this settles:

- **The second round carries only the failed items.** The client's entire retry
  logic is "re-send what the response named", and it needs no server state,
  because the first round's response already said which items those were
  (design D5).
- **The D5 residual is visible, not silent.** Re-sending a file the server
  already saved produces `ok-0 (1).txt` and reports `renamed: true` — the user
  can see the duplicate and delete it. The residual therefore degrades to
  "the user dropped the file twice", which is why no server-side idempotency
  record is warranted. (The residual's *probability* is not measured here; it
  requires a dropped response, which the in-process WSGI harness cannot produce.
  This is recorded as a known un-measured quantity rather than asserted.)

Note on the two failing items: they are permanent refusals (an escaping path and
the bin) chosen so that the second round's *shape* is observable — the retry
carries exactly those two and nothing else. That the codes repeat is the point;
a transient failure would have confounded "did it retry only the failures" with
"did it succeed this time".

## 10. Explicit non-goals of this change

Stated so the boundary is not mistaken for an omission:

- **No resumable upload and no chunk merging.** A single file is one request, so
  a dropped connection means re-sending that file. GB-scale single files are out
  of scope (design D7).
- **No move or rename.** Only append (upload) and erase (delete) exist. This is
  why an occupied restore destination is renamed around rather than vacated, and
  why the panel has no drag-to-move (design D14).
- **No server-side batch idempotency, TTL or cross-request state.** The bin is
  filesystem data, not database rows; the panel's limits are fixed constants, not
  configuration (design D5, D8).
- **No version history.** Each delete keeps one copy; deleting the same file
  twice yields two independent bin entries.
- **No disk-headroom pre-check and no `resource-quota` integration.** §7.3 gives
  the operator the arithmetic instead.
- **Empty directories are not preserved.** Structure fidelity covers the position
  of every file that exists; a folder with no files is not materialized (design
  D11).
- **The chat area's attachment drag is untouched**, as are the `/upload`
  contract, `GET /api/workspace/*` and the `write`/`user-dir` endpoints, and the
  execution layer's paths. Verified by `test_console_upload_transport.py` (9
  passed), `test_console_upload_frontend.cjs` (6 passed),
  `test_workspace_edit.py` and `test_workspace_user_dir.py` (passed).
- **The proxy prerequisite is not met on this host** (§7). The code is complete;
  the 100 MiB ceiling is an operator action.

## 11. Known un-measured quantities

Listed rather than implied, so no reader assumes a claim that was not made:

| Quantity | Why not measured here |
| --- | --- |
| The browser's `upload.onprogress` arithmetic | Needs a real browser; the formula and its proportional scaling are pinned by `tests/test_console_workspace_upload_frontend.cjs` (31 cases), which drives the shipped code under a stub XHR. **This is also the gap that let §8.3 ship an unreachable markup edit** — the id/page check added there narrows it, but a browser pass remains the only thing that exercises the real parser, the real Tailwind JIT and the real XHR. |
| Probability of the D5 duplicate | Requires a response lost in transit; not producible through in-process WSGI (§9). |
| Behaviour behind a raised proxy ceiling | The proxy is 100 MiB today, so a 200 MiB file cannot traverse it to be observed end to end (§7). |
| Concurrent-first-drop behaviour on Linux/macOS | The `dir_fd` path is a different code path from the one that failed; the Windows path (§8.1) is the one this deployment runs. |
