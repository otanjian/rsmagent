# 15 — Legacy channel bootstrap: upstream's repair vs the fork's policy

Status: **decided (fork keeps its policy), 2 upstream tests skipped with a reason.**
Recorded for `MODIFIED 删除/修改类冲突有显式决策并记录`.

## What upstream added

Upstream's new `tests/test_web_channel_disconnect.py` pins a repair:

- `channel/channel_instances.py::bootstrap_legacy_instances` folds a flat
  `channel_type` + credentials in `config.json` into an explicit
  `channel_instances` record on every roster (`team.json`) write;
- because that bootstrap re-materialises the record, deleting the instance
  alone is undone by the next write, so
  `_handle_instance_disconnect` must also prune the type from
  `channel_type` (`_prune_legacy_channel_type`).

Two tests in that file assert the bootstrap exists
(`test_disconnecting_bootstrapped_legacy_instance_stays_removed`,
`test_disconnecting_one_of_several_keeps_the_type`).

## Why the fork does not do it

The fork's database identity mode deliberately refuses to synthesise channel
instances from flat configuration, and this is **enshrined in a fork test**:

- `channel/channel_instances.py::bootstrap_legacy_instances` — "Return existing
  `channel_instances` only — never synthesize from `channel_type`… database
  identity requires explicit registration, so flat `channel_type` credentials
  are ignored" (present on the fork's HEAD, i.e. pre-existing, not a merge
  artifact);
- `resolve_channel_instances` refuses the implicit legacy fallback the same way;
- `tests/test_channel_instances.py::test_bootstrap_does_not_synthesize_from_flat_credentials`
  asserts `bootstrap_legacy_instances(settings, {}, "primary") == []`;
- `channel/web/fork/handlers/channels.py` therefore has no
  `_prune_legacy_channel_type`: there is nothing to resurrect, so nothing to
  prune.

Channels must be registered explicitly (roster record or tenant-owned instance)
so a tenant never starts a channel nobody registered for it.

## Decision

Restoring upstream's bootstrap would reverse a fork policy that has its own
test and its own security rationale, so the fork keeps its behaviour. The two
upstream tests are **skipped, not rewritten**: their premise (the resurrection
the prune guards against) cannot occur here, so a rewrite would assert an
unrelated contract. The skip reason points at the fork test that states the
policy, so the divergence stays visible and cheap to re-check on the next sync.

`tests/test_web_channel_disconnect.py` is therefore **fork-modified** and will
need this skip re-applied on the next upstream sync (drift point).

## Follow-up (not blocking the merge)

The three routing tests in the same file (`_handle_disconnect` vs
`_handle_instance_disconnect`) pass unchanged and cover behaviour the fork does
share. Worth adding a *fork-side* equivalent of "a disconnect sticks" against
`channel/web/fork/handlers/channels.py` in a later change — the fork's own
handler is the one its console actually serves.
