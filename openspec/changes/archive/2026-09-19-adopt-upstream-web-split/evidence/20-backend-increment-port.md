# 20 — Porting upstream's handler increments into the fork's parallel handlers

`evidence/18-backend-increment-gap.md` named the gap class; this records the
first two members of it being **closed by porting**, and the method that found
them. Both were found the same way: a test file that exists on *both* lines.

## How the gap surfaced: silently retargeted fork tests

`tests/test_web_search.py` and `tests/test_chat_model_fallback.py` exist at the
fork point **and** on `origin/master`. Upstream added 537 and 342 lines to them
respectively; because the fork's copies were the same files, the merge folded
upstream's new cases in, and every import in them named `channel.web.api.models`
— upstream's module. So the merged tests **passed** while asserting upstream's
handler, and the fork's own handler (`channel/web/fork/handlers/models.py`, the
one `build_web_app()` actually serves) went unverified. `scripts/migration/find_retargeted_tests.py`
flags exactly this shape.

Repointing them at the served stack (`from channel.web.web_channel import
ModelsHandler` — the entry module re-exports the fork's class; the `conf` patch
targets moved from `channel.web.api.models.conf` to `channel.web.web_channel.conf`)
turned the drift into 11 failures, which are the two ported increments.

## Port 1 — the three search providers

| side | `_SEARCH_PROVIDERS` |
| --- | --- |
| merged runtime (`agent/tools/web_search/web_search.py::PROVIDER_ORDER`) | `bocha, qianfan, zhipu, linkai, anysearch, serply, tavily, searxng, keenable` |
| fork handler before | the first six |
| fork handler after | identical to the runtime |

The fork's own comment says the tuple "mirrors PROVIDER_ORDER … keep them in
sync", and the test asserts that equality. The mechanism was already there
(`_search_provider_key`, `_search_capability`, `_handle_set_search_credential`);
what was missing was the data and the two new capabilities searxng and keenable
bring:

- `tavily` / `keenable`: own key in `tools.web_search.<provider>_api_key`, or the
  matching environment variable;
- `searxng`: an instance **URL** (`searxng_url`), no key — `_search_capability`
  now reports `needs_url` and echoes `url_masked`, and the save path takes
  `url` instead of `api_key`;
- `anysearch` / `keenable`: the keyless tier, `anonymous` in the payload and
  `<provider>_anonymous` in the config, only when the key is empty.

Ported with the fork's idioms (`cls._is_real_key`, `ConfigHandler._mask_key`,
`from channel.web.web_channel import conf` inside the method) rather than
upstream's module-level helpers, because fork tests patch through the entry
module.

## Port 2 — the fallback **chain**

This one was not cosmetic: the two sides wrote different config shapes.

| | before (fork handler) | after (ported) |
| --- | --- | --- |
| persisted | `{enabled, provider, model, max_switches}` | `{enabled, chain: [{provider, model}, …]}` |
| capability | single `current_provider` / `current_model` + `max_switches` | `chain` (unbounded), plus `current_provider`/`current_model` kept as link 1 for older clients |
| save path | `_set_chat_fallback(provider, model, enabled, max_switches)` | `_set_chat_fallback(provider, model, enabled, chain=None)` |

Why it is a merge obligation, not a fork preference: **the runtime is
upstream's**. `config.py::_migrate_chat_fallback()` upgrades the single-model
shape to `chain` at startup, and `bridge/agent_bridge.py` walks
`conf()["chat_fallback"]["chain"]` (`_fallback_depth`, `use_fallback()`). The
fork's console writing `provider`/`model`/`max_switches` meant the user could
save a backup model that the served runtime would ignore.

Ported from upstream, including the two behaviours that came with the chain:

- links are validated per entry (`custom:<id>` via `_normalized_custom_provider`,
  now extracted as a helper so the chain reuses it), half-filled and
  never-filled rows are **dropped** rather than rejected, an unknown provider
  anywhere in the chain is an error, and enabling with no usable link is an
  error while disabling is always allowed;
- disabling clears engaged fallbacks across live agents
  (`Bridge().get_agent_bridge().clear_all_model_fallbacks()`), so turning it off
  takes effect immediately instead of at the next run boundary.

`max_switches` is gone from the payload: `_migrate_chat_fallback` drops it too
("the chain length is the new bound"), and nothing in the served frontend
(`channel/web/static/**`) reads it. The runtime's own pass limit lives in
`bridge/agent_bridge.py::_FALLBACK_MAX_PASSES`.

## The obligation this turned up in the guards

`tests/test_upstream_core_seams.py::test_the_merge_obligation_is_recorded` reads
`scripts/conflict-baseline.txt` and requires the `_import_local_file` obligation
to be **recorded there**. Re-freezing the baseline for the new tips dropped that
string, so the re-frozen file now carries an explicit section: unconflicted
upstream modules are listed with what makes them safe (byte-identical to
`origin/master`), and `_import_local_file` with its loopback + desktop-token
guard is named as the obligation that is satisfied *by not editing it*.

Its sibling guard was worse than stale — it was **skipping**:

```python
source = web_layer_source()          # fork layer only
if "_import_local_file" not in source:
    self.skipTest("upstream _import_local_file not merged yet (obligation recorded)")
```

`web_layer_source()` excludes `channel/web/api/**` and `channel/web/core/**`
(that is what makes it the *fork's* layer), so once upstream landed the feature
there the guard could never see it and skipped forever — green while
unverified. It now reads the whole layer (`include_upstream=True`), slices from
`def _import_local_file` to the next method, and pins both the calls
(`_is_loopback_request()`, `_desktop_token_matches()`) and the discriminating
literals of the implementations they call (`REMOTE_ADDR`, `X_COW_DESKTOP_TOKEN`)
— stronger than the previous "the word `token` appears" check, and it now
actually runs instead of skipping.

## Verification

| check | result |
| --- | --- |
| `tests/test_web_search.py` + `tests/test_chat_model_fallback.py` (retargeted) | 89 passed, 37 subtests — **0 failures** (was 11 on the retargeted stack) |
| `test_upstream_core_seams.py` | 14 passed, 0 skipped (was 13 passed + 1 skipped) |
| `test_models_handler.py`, `test_fallback_provider_credentials.py`, `test_subagent_fallback_scope.py`, `test_chat_fallback_chain_migration.py`, `test_route_registry.py`, `test_upstream_drift_guards.py`, `test_sync_report.py`, `test_console_channel_manager_resolution.py` | all passed |
| full suite | see `tasks.md` 3.6 / 3.10 for the recorded run |

Pre-existing, **not** merge-caused: `test_personal_console_frontend.py` fails
because `tests/test_personal_console_frontend.cjs` opens
`channel/web/static/js/personal-console.js`, which does not exist at the fork
point either (it is in all four baseline failure lists). It belongs to the
front-end unit (`evidence/17`), not to this port.

## What is still open

`scripts/migration/measure_backend_increment_gap.py` (base `e5e2a52d`, upstream
`origin/master`) still reports **39** unported methods / ~450 upstream lines
after these two ports — with the caveat that its judgement is text similarity,
so a port written in the fork's idioms still counts as absent. The two ports
above are examples of the *kind* of member: a method string showing up is a
question to adjudicate, not a verdict. The remaining list is dominated by
`channel/web/api/models.py` (provider overview, vision/asr/tts/embedding/image
capabilities), `channel/web/api/scheduler.py` and `channels.py`; each needs the
same treatment — read upstream's change, decide whether the fork's served
handler is missing behaviour (port it, with the retargeted test as the proof) or
is deliberately different (record it).
