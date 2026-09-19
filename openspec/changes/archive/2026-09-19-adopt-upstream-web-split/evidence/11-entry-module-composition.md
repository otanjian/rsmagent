# 11 — `web_channel.py`: why the composition needs two namespaces

The last structural blocker in the backend merge, resolved down to a concrete
mechanism. `web_channel.py` conflicts 25,397 of 25,659 bytes — nearly whole-file —
because git finds no common context between the fork's 627-line entry module and
upstream's 177-line URL table. Resolution is by composition, not text merging
(`06-rehearsal-after-phase1.md`). This records what the composition has to
satisfy, and the one constraint that decides its shape.

## The two stacks are parallel, and both must survive

After the merge the tree carries two complete web stacks:

| | handlers | plumbing | app factory | WebChannel |
|---|---|---|---|---|
| upstream | `channel/web/api/**` (76 handler classes) | `channel/web/core/**` | `build_app()` over `URLS` | `core/channel.py` |
| fork | `channel/web/fork/**` (79 handler classes) | `fork/runtime.py` | `build_web_app()` over `_WEB_URLS` | `fork/runtime.py:657` |

This is design D2 working as intended: the fork's handler bodies carry its
authorization and tenant scoping internally, so it owns parallel implementations
rather than editing upstream's files. Upstream's `api/**` imports its own
`core/**` throughout (every `api/*.py` reaches into `core/_common`,
`core/channel`, `core/providers`, `core/template`), so that subtree is
self-consistent and stays intact.

## The constraint that decides the shape: 64 colliding handler names

    upstream api handlers: 76
    fork handlers:         79
    shared names:          64

`web.py` resolves the handler strings in a URL table against a namespace mapping
by name. Both sides name the same 64 classes (`ChatHandler`, `AuthLoginHandler`,
`ConfigHandler`, …), so **the entry module cannot import both into its own
globals()**: whichever import ran last would silently win, and then one of the two
URL tables would resolve to the other stack's handler. That is a silent
wrong-authorization failure, not a crash — the worst possible outcome here.

## Consequence for the resolution

The merged entry module must build each application against its **own** namespace:

```
URLS        = <upstream's tuple, verbatim>
_WEB_URLS   = _derive_web_urls()          # fork's, from route_registry

def build_app():
    """Upstream's factory. Kept because upstream's core/channel.py calls it."""
    return web.application(URLS, _upstream_namespace(), autoreload=False)

def build_web_app():
    """The fork's factory: fork handlers in globals, policy processor installed."""
    ...
```

with the upstream handler classes reached through their modules (a namespace built
from `channel.web.api.*`), never imported into the entry module's globals under
their public names.

`build_app()` cannot be dropped. Upstream's `core/channel.py` (new in this merge)
calls it at line 1507:

    from channel.web.web_channel import build_app
    app = build_app()

so removing it would break the standalone-upstream form. The fork's live path
resolves `channel.web.web_channel.WebChannel` through `channel_factory`, which
must therefore still be the fork's `WebChannel` (from `fork/runtime.py`) — and
`SERVING` likewise, since `app.py` waits on the name it imports and only the
fork's WebChannel sets the fork's event.

This is also exactly what task 5.4's four assembly states need:

| state | what must work |
|---|---|
| standalone upstream | `build_app()` over `api/**` + `core/**`, fork modules absent |
| full rdai | `build_web_app()` over fork handlers, policy installed |
| rdai without the mandatory authorization extension | fail closed, do not silently serve upstream's unguarded handler |
| rdai without the optional UI extension | `build_web_app()` still assembles |

## Status

Recorded, not yet implemented. Implementing it means restructuring the entry
module, then running the route-coverage check and the seam tests
(`test_route_registry.py`, `test_upstream_core_seams.py`,
`test_channel_signature_seam.py`, `test_http_policy.py`) plus the full §6.2
regression. It is the one remaining conflict whose resolution changes runtime
wiring rather than only content, so it should land as its own reviewed commit.
