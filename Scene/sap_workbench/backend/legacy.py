"""Rollback gate for the retired self-managed engine path.

The workbench used to run one Bun/OpenCode engine per binding and reverse-proxy
its Web UI: `runtime.py`'s non-iframe ("screen") branch spawned it, `prewarm.py`
warmed it up ahead of the create that paid for the start, and
`opencode_adapter/{server,host,native-host,credentials,context}.ts` implemented
it. The conversation is now the platform's own coding session, which the page
mounts from the embed address the coding entry returns, so the scene starts no
engine at all.

That code is deliberately **retained, not removed**: a deployment that has to
fall back can bring the old path back without a code change. It is switched off
unless this flag is set, so the default behaviour is exactly one path -- the
platform entry -- and there is no second, untested way to create a session.
"""
from __future__ import annotations

import os

#: Set to 1/true/yes/on to let a new binding reserve the retired engine mode
#: again. Any other value (including unset) keeps the platform entry only.
ENGINE_ENV = 'SAP_WORKBENCH_LEGACY_ENGINE'

_TRUTHY = frozenset({'1', 'true', 'yes', 'on'})


def engine_enabled(environ=None):
    """True only when this deployment explicitly asks for the old path.

    ``environ`` is injectable so a test can assert the exact parsing of every
    accepted spelling without mutating the process environment.
    """
    raw = (os.environ if environ is None else environ).get(ENGINE_ENV)
    return raw is not None and raw.strip().lower() in _TRUTHY
