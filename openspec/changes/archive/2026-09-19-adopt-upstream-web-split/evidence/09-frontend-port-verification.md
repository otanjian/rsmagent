# 09 — frontend port: what the tool ports, what it will not

Runs of `port_frontend.py` + `verify_frontend_port.py` against
`e5e2a52d`..`HEAD` onto `origin/master`. Supersedes the estimate in
`07-frontend-divergence.md` (which measured *re-anchorability* as a property of
context windows; this measures what the port actually achieves once mis-location
is designed out).

## Result

| | change clusters | ported mechanically | to adjudicate |
|---|---|---|---|
| `console.js` | 362 | 275 | 87 |
| `console.css` | 79 | 71 | 8 |

Fork-unique lines (lines present in the fork's monolith but in neither the merge
base nor any upstream module — the fork's genuine additions):

| | fork-unique lines | in emitted modules | on the worklist | unaccounted |
|---|---|---|---|---|
| `console.js` | 5222 | 2722 | 2500 | **0** |
| `console.css` | 1361 | 783 | 578 | **0** |

`node --check` passes on all 25 emitted JS modules (18597 lines + 4169 CSS lines).

The "unaccounted = 0" number is the one that matters. Every line the fork added
is either in the output or explicitly listed for review; nothing was dropped
silently. A naive "is this fork line present in the output?" check does not
establish that — most fork lines are upstream lines, so it reports false
partial-porting. Only fork-unique lines cannot match by accident, which is why
the verifier is built on them, and why it also re-runs `node --check` itself
rather than trusting the porter's claim.

## The deferred regions are semantic conflicts, not a backlog of typing

98 regions (87 JS + 8 CSS + 3 straddling) need adjudication across 23 modules.
They are the places where **both** sides changed the same code: in 37 cases
upstream rewrote the surrounding code, in 37 a splice was planned but left the
module unparseable (structural mismatch), in 13 the insertion point moved.

Auto-transplanting the fork's enclosing function is applicable to 56 of the 87 JS
regions — the enclosing symbol name exists in the target upstream module — and it
is **rejected**. It would overwrite upstream's version of that function, silently
discarding whatever upstream changed there, which is precisely the failure the
merge spec forbids. The reverse (take upstream's version) would discard the
fork's customization. Neither is decidable by a tool, so each region records both
sides and a required decision:

`frontend_adjudication.md` / `.json` — per region: the fork's text, the named
enclosing symbol, upstream's version of that symbol where it could be located
(47 of 98), and a `decision` field to fill in as `fork` / `upstream` / `merged`
with a rationale.

## Concentration

The heaviest modules for adjudication: `js/core/auth.js` (16 regions),
`js/views/agents.js` (12), `js/core/nav.js` (9), `js/views/sessions.js` (7),
`js/core/i18n.js` (6), `js/chat/new-chat.js` (4), `js/views/channels.js` (4),
`js/views/skills.js` (4), `js/views/tasks.js` (4), `css/sessions.css` (4).

`core/auth.js` leading again matches the backend finding: the fork's
database-identity model is concentrated in the login/logout/401 paths, so those
regions are the ones most likely to need the fork's version outright, and the
most important to review rather than auto-apply.

## Still not claimed

- The emitted modules are **not wired into page assembly** (no override map, no
  manifest) and are **not committed under `channel/web/static/`**.
- Parsing is not correctness. Nothing here shows the ported console *behaves*
  like the fork's — that is task 4.5's browser acceptance (login, context switch,
  streaming request, upload read-back, download preview).
- The 98 regions are unresolved, so the fork's frontend is not yet fully
  preserved. `console.js` / `console.css` must not be deleted, and the merge must
  not resolve their `DU` by deletion, until those decisions are recorded.
