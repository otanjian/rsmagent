# 08 — Phase 3 port strategy for the fork frontend

Phase 3 task 4.3 (decision) and the plan behind tasks 4.2–4.8. Input: the measured
divergence in `07-frontend-divergence.md`. This records the architecture, why the
obvious alternative does not work, and the concrete mechanism the port uses.

## What upstream's frontend actually is

Reading `channel/web/README.md` at `origin/master` and `core/template.py` settles
several constraints that decide the design. These are upstream's own rules, not
guesses:

- **No bundler. Every script is a classic script sharing one global scope**,
  executed in the order `chat.html` lists them via `defer`. Generated HTML leans
  on implicit globals (`onclick="foo()"`), so wrapping a file in an IIFE or
  switching to `type="module"` breaks it.
- **"The same top-level name declared in two files is a `SyntaxError` and a blank
  page."** (`const`/`let` in the shared global lexical environment.) A duplicate
  declaration is not a harmless override.
- Top-level `const`/`let` have a TDZ across files: a file cannot read one
  declared by a later file at its own top level. All immediate startup code is
  collected in `boot.js`, **which must load last**.
- `chat.html` is a thin shell. `<!--#include templates/xxx.html-->` markers are
  expanded **server-side** by `core/template.py`, which also stamps every
  `assets/js/**` and `assets/css/**` reference with a per-file mtime `?v=` query.
  Adding a script or stylesheet needs no Python change.
- `tools/check-load-order.mjs` validates that load order against the shell.

## Decision: fork-owned module override map, applied where the page is assembled

The fork keeps upstream's shell, templates, module split, and load order
**unmodified**, and declares a map from upstream module path to the fork's ported
copy:

```
assets/js/views/sessions.js   ->  assets/js/fork/views/sessions.js
assets/js/core/auth.js        ->  assets/js/fork/core/auth.js
assets/css/sessions.css       ->  assets/css/fork/sessions.css
...
```

The fork's page handler (fork-owned, `channel/web/fork/**`) assembles the page
with upstream's `core/template.py` and then applies the map, so the browser
receives the fork's module where the fork overrode one, in the position upstream's
shell put it. Fork-only modules (`todos.js`, `identity-admin.js`, `scenes/`, …)
continue to load after, as they do today. `boot.js` still loads last.

Fork modules live under `static/js/fork/**` and `static/css/fork/**`, mirroring
upstream's subpaths. **No fork edit ever lands in an upstream module**, which is
what makes the next sync conflict-free instead of re-fighting this same file.

### Why not the alternatives

- **Load a fork overlay after upstream's modules and redeclare the customized
  functions.** Rejected: upstream's rule above — a duplicate top-level `const`/`let`
  is a `SyntaxError` and a blank page, and the fork's customizations are not
  limited to `function` declarations. It also cannot change a `const` the
  customized code reads. Silent-blank-page failure modes are the worst kind to
  ship.
- **Edit upstream's modules in place.** Rejected: reintroduces exactly the
  file-level conflict Phase 1 removed for the backend, and violates
  `fork-upstream-decoupling`'s "定制逻辑位于稳定接缝而非上游核心文件内".
- **Keep `console.js`/`console.css` (resolve the conflict `keep-fork`).** Rejected
  for this change: the fork's `chat.html` markup is itself a conflict, and
  upstream's shell/templates/JS are one coupled unit — keeping the monolith means
  dropping upstream's frontend refactor *and* whatever feature and fix work
  landed in it, silently. Recorded here as the rejected option; if Phase 3 is
  ever descoped, it must be re-recorded as a deliberate `keep-fork` baseline
  decision with the dropped upstream increments enumerated, not left implicit.

## Port mechanism

`scripts/migration/port_frontend.py` (to be written, mirroring the backend's
`emit_fork_web.py`):

1. Compute the normalized base→fork hunk set (as in the analysis script).
2. Route each hunk to the upstream module owning its base region.
3. For each routed hunk, locate its base pre/post context in that module and
   splice the fork's exact text between them — the 258 JS / 67 CSS hunks
   measured re-anchorable.
4. Write `static/js/fork/<subpath>` (and CSS) from upstream's module plus the
   spliced hunks.
5. Emit the unrouted remainder (~107 JS, ~12 CSS hunks) as a hand-port worklist
   with base and fork samples, so the leftover is a reviewable list rather than
   silent loss.
6. Verify: `node --check` on every emitted module; the fork's own structural
   tests; `tools/check-load-order.mjs` against the fork's effective load order.

The emitter must be deterministic (byte-identical on re-run against an unchanged
input), as the backend's is — a non-deterministic emitter makes "did anything
drift?" unanswerable.

## Drift guard for ported modules

A ported fork module is a *copy* of an upstream module. When upstream later
changes the original, the fork's override silently keeps serving the old code.
So each ported module records the upstream file it was derived from and that
file's hash at port time:

`static/js/fork/manifest.json` — `{fork_path: {upstream_path, upstream_sha256}}`

with a gate that recomputes the hashes and fails when an upstream module has
moved. This is the frontend instance of the drift guard the design already
requires for the backend, and it is what makes "re-apply manually on drift" a
detectable obligation rather than a hope.

## Residual cost, stated plainly

- The fork re-owns **25 of 33** JS modules and **5 of 8** CSS modules, though the
  customization is concentrated: the top 7 JS modules carry 84 % of added lines.
- ~119 hunks need hand porting; `views/sessions.js` (21) and `views/agents.js`
  (20) dominate.
- The fork must track upstream's shell `chat.html` and load order. Adopting
  upstream's shell unmodified means upstream adding a script tag is inherited
  automatically; the fork's own assets are injected by the override map and the
  fork's fragment seam rather than by editing the shell.
- Fork `chat.html` markup customizations must be re-homed as fork template
  fragments. `static/fragments/` and the `data-fork-fragment` / `fork-fragment-mounted`
  seam already exist for this and are used by `appearance-dialog.html`.

## Open items carried into the port

- Whether the fork's `static/js/doc-editor.js` and `workspace.js` collide with
  upstream's `assets/js/doc-editor.js`; if they are fork versions of upstream
  files they belong in the override map, not in the fork-only list.
- `core/i18n.js` needs reading before porting: the fork *removed* 1295 lines
  there, and it is the one module where "port the diff" is the wrong mental
  model.
