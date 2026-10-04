# encoding:utf-8
"""The channel-instances module is partitioned by ownership (task 8.12 / D4b).

``channel/channel_instances.py`` is a merge hotspot: both sides add module-level
symbols in the same region (right after ``CREDENTIAL_KEYS``). Upstream adds its
channel type labels / default-name symbols there; this fork adds its
credential-minimum sets and tenant-runtime symbols. When both additions land on
the same anchor, every upstream merge produces a conflict.

Decision D4b splits the file into two clearly-marked regions divided by one
line, so each side's *new* symbols land in a different region instead of the
same literal:

* upstream (and shared legacy) symbols go **above** the divider;
* fork-only symbols (credential minimums, tenant runtime, tenant console
  contract) go **below** it.

This is *human discipline, not a mechanism* -- see ``design.md`` D4b for the
trigger conditions that would convert it into a moved/mechanised module.

The second half of the test is a golden snapshot: ``required_credential_keys``
must keep returning exactly what it returns today for every declared channel
type, so a pure reorder cannot silently change the credential contract.

Note: upstream's ``_CHANNEL_TYPE_LABELS`` / ``default_instance_name`` are not
on this branch yet (the upstream merge has not happened). The test therefore
asserts their *placement when present* plus that the divider text records where
they belong, which is what makes them land in the right region when they do
arrive.
"""

import ast
from pathlib import Path

import pytest

import channel.channel_instances as ci


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "channel" / "channel_instances.py"
)

#: The one line that separates the two regions. The rule is stated verbatim so
#: it is greppable and so the test can locate the boundary mechanically.
DIVIDER = (
    "# PARTITION DIVIDER (D4b): add fork symbols below this line only; "
    "upstream symbols above."
)

#: Symbols this fork added to the module. Every one of them must live below the
#: divider; two of them sharing the upstream insertion anchor is exactly the
#: collision D4b removes.
FORK_SYMBOLS = {
    "REQUIRED_CREDENTIAL_KEYS",
    "required_credential_keys",
    # tenant runtime state
    "_runtime_state",
    "_runtime_state_guard",
    "_restart_locks",
    "_restart_locks_guard",
    "_instance_restart_lock",
    "_record_runtime_state",
    "instance_runtime_state",
    "_runtime_manager",
    "_stop_instance_runtime",
    "apply_tenant_instance_runtime",
    "load_tenant_channel_instances",
    # tenant console contract
    "TENANT_CHANNEL_LABELS",
    "TENANT_CHANNEL_APPEARANCE",
    "CREDENTIAL_FIELD_LABELS",
    "_SECRET_KEY_HINTS",
    "tenant_channel_types",
}

#: Upstream-only symbols (from ``origin/master``) that must land above the
#: divider. They are absent on this branch today; the test guards their
#: placement once an upstream merge introduces them.
UPSTREAM_SYMBOLS = {
    "_CHANNEL_TYPE_LABELS",
    "default_instance_name",
}

#: Symbols that are upstream-owned or shared by both sides and must stay above
#: the divider -- proof the divider was not inverted or moved too high.
UPSTREAM_OR_SHARED_SYMBOLS = {
    "new_instance_id",
    "CREDENTIAL_KEYS",
    "MULTI_INSTANCE_READY",
    "ChannelInstance",
    "resolve_channel_instances",
    "bootstrap_legacy_instances",
    "upsert_instance",
    "get_instance",
}

#: Golden snapshot of today's credential contract. If a reorder ever changes
#: one of these values, that is a behavioural change, not a move.
GOLDEN_REQUIRED_CREDENTIAL_KEYS = {
    "wechatcom_app": ("wechatcom_corp_id", "wechatcomapp_agent_id", "wechatcomapp_secret",
                      "wechatcomapp_token", "wechatcomapp_aes_key"),
    "wechat_kf": ("wechat_kf_corp_id", "wechat_kf_secret", "wechat_kf_token", "wechat_kf_aes_key"),
    "wechatmp": ("wechatmp_app_id", "wechatmp_app_secret", "wechatmp_token"),
    "feishu": ("feishu_app_id", "feishu_app_secret"),
    "wecom_bot": ("wecom_bot_id", "wecom_bot_secret"),
    "qq": ("qq_app_id", "qq_app_secret"),
    "telegram": ("telegram_token",),
    "slack": ("slack_bot_token", "slack_app_token"),
    "discord": ("discord_token",),
}

#: One value per *declared* channel type (i.e. every key of ``CREDENTIAL_KEYS``),
#: including the types deliberately left without a verified minimum set.
GOLDEN_PER_DECLARED_TYPE = {
    "wechatcom_app": ("wechatcom_corp_id", "wechatcomapp_agent_id", "wechatcomapp_secret",
                      "wechatcomapp_token", "wechatcomapp_aes_key"),
    "wechat_kf": ("wechat_kf_corp_id", "wechat_kf_secret", "wechat_kf_token", "wechat_kf_aes_key"),
    "wechatmp": ("wechatmp_app_id", "wechatmp_app_secret", "wechatmp_token"),
    "feishu": ("feishu_app_id", "feishu_app_secret"),
    "dingtalk": (),
    "wecom_bot": ("wecom_bot_id", "wecom_bot_secret"),
    "weixin": (),
    "qq": ("qq_app_id", "qq_app_secret"),
    "telegram": ("telegram_token",),
    "slack": ("slack_bot_token", "slack_app_token"),
    "discord": ("discord_token",),
}


def _source_lines():
    return MODULE_PATH.read_text(encoding="utf-8").splitlines()


def _divider_line(lines):
    found = [index for index, line in enumerate(lines, start=1) if line.strip() == DIVIDER]
    assert len(found) == 1, (
        "expected exactly one partition divider %r, found %d" % (DIVIDER, len(found))
    )
    return found[0]


def _top_level_symbol_lines():
    """name -> 1-based line number of every top-level def/class/assignment."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    positions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            positions[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    positions[target.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            positions[node.target.id] = node.lineno
    return positions


# --- the partitions --------------------------------------------------------


def test_the_divider_exists_and_states_the_rule():
    lines = _source_lines()
    divider = _divider_line(lines)
    rule = lines[divider - 1]
    assert "add fork symbols below this line only" in rule
    assert "upstream symbols above" in rule


def test_every_fork_symbol_lives_below_the_divider():
    positions = _top_level_symbol_lines()
    divider = _divider_line(_source_lines())
    missing = sorted(FORK_SYMBOLS - set(positions))
    assert not missing, "fork symbols were renamed/dropped: %s" % missing
    misplaced = sorted(
        name for name in FORK_SYMBOLS if positions[name] < divider
    )
    assert not misplaced, (
        "fork symbols found above the divider (move them into the fork region "
        "at the bottom of the file): %s" % misplaced
    )


def test_no_fork_symbol_is_defined_above_the_divider():
    """The inverse guard: the upstream region must stay fork-symbol-free."""
    positions = _top_level_symbol_lines()
    divider = _divider_line(_source_lines())
    above = {name for name, line in positions.items() if line < divider}
    assert not (above & FORK_SYMBOLS)


def test_upstream_symbols_live_above_the_divider():
    positions = _top_level_symbol_lines()
    divider = _divider_line(_source_lines())
    # Absent here (the upstream merge has not happened); once introduced they
    # must be above the divider and never in the fork region.
    for name in sorted(UPSTREAM_SYMBOLS):
        if name in positions:
            assert positions[name] < divider, (
                "upstream-only symbol %s must not live in the fork region" % name
            )


def test_upstream_region_keeps_the_shared_symbols():
    positions = _top_level_symbol_lines()
    divider = _divider_line(_source_lines())
    stray = sorted(
        name for name in UPSTREAM_OR_SHARED_SYMBOLS
        if positions.get(name, 0) > divider
    )
    assert not stray, (
        "shared/upstream symbols were moved into the fork region: %s" % stray
    )


def test_the_divider_records_where_upstream_symbols_go():
    """Upstream's labels/default-name symbols are named above the divider.

    They are not on this branch yet, so the placement contract is recorded in
    the region itself; when an upstream merge introduces them they land above.
    """
    lines = _source_lines()
    divider = _divider_line(lines)
    above = "\n".join(lines[: divider - 1])
    for name in sorted(UPSTREAM_SYMBOLS):
        assert name in above, (
            "the upstream region should name %s as an upstream-owned symbol" % name
        )


# --- golden snapshot: reordering must not change the contract --------------


def test_the_full_required_mapping_is_unchanged():
    assert set(ci.REQUIRED_CREDENTIAL_KEYS) == set(GOLDEN_REQUIRED_CREDENTIAL_KEYS)
    assert dict(ci.REQUIRED_CREDENTIAL_KEYS) == GOLDEN_REQUIRED_CREDENTIAL_KEYS


def test_the_declared_type_list_is_unchanged():
    assert set(ci.CREDENTIAL_KEYS) == set(GOLDEN_PER_DECLARED_TYPE)


@pytest.mark.parametrize("channel_type", sorted(GOLDEN_PER_DECLARED_TYPE))
def test_required_credential_keys_does_not_regress_for_each_declared_type(channel_type):
    assert ci.required_credential_keys(channel_type) == GOLDEN_PER_DECLARED_TYPE[channel_type]


def test_a_type_without_a_verified_set_still_returns_empty():
    assert ci.required_credential_keys("nonsense") == ()


def test_every_required_key_is_still_a_declared_key():
    for channel_type, required in ci.REQUIRED_CREDENTIAL_KEYS.items():
        assert channel_type in ci.CREDENTIAL_KEYS
        for key in required:
            assert key in ci.CREDENTIAL_KEYS[channel_type]
