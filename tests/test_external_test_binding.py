# encoding:utf-8
"""A test result is bound to the connection version and secrets it tested.

Change ``add-external-system-access``, task 4.4: "实现草稿 test 与已保存 test
的秘密解析/版本绑定;草稿不持久化,旧版本结果不能写当前状态,保存后需重新测试".

The property under test is not "a probe runs" — that is
``tests/test_external_adapter_contract.py`` for the staged result and
``tests/test_external_mcp_adapter.py`` for a real adapter. What is measured here
is the *binding*, because that is the part that fails quietly: a green test
result that outlives the configuration it was produced from is worse than no
result at all, since the console then shows a connection as "tested" while
nothing about the current configuration has been checked.

Four rules, each with the failure it prevents:

* **A draft persists nothing.** Testing an unsaved form must not create a row,
  a secret or a summary, and must not overwrite a saved connection — otherwise
  "test first, then save" would write the business domain from an endpoint that
  has no connection yet, and a draft could stamp a saved row as tested.
* **A saved test is bound to a version and a secret set.** ``last_test`` returns
  a summary only when both still match the connection, so an edit or a rotated
  credential reads as ``untested`` rather than as the previous verdict.
* **A result that arrives after the configuration moved is discarded.** The
  probe runs against a row that can change underneath it; storing such a result
  would attribute it to a configuration it never saw.
* **``record=False`` records nothing.** A caller that only wants to look at a
  result does not stamp the row.

The adapter is a throwaway kind registered for the duration of each test, so a
probe cannot reach the network and the *staging* is scripted. What is asserted
is the runtime's bookkeeping, not an adapter's behaviour.
"""

from __future__ import annotations

import json

import pytest

from integrations.external import registry
from integrations.external.adapters import base as base_mod
from integrations.external.adapters.base import (
    ConnectionAdapter,
    ProbeResult,
    register_adapter,
    stage_failed,
    stage_ok,
    STAGE_AUTH,
    STAGE_NETWORK,
)
from integrations.external.errors import ExternalConnectionError

from tests import _external_oneagent as oneagent
from tests._helpers import build_identity

MASTER_KEY = "unit-test-master-key"

#: A *real* kind. The connection table constrains ``kind`` to the four product
#: types, and that constraint is deliberate — inventing a fifth kind for a test
#: would either need the schema relaxed or would test a row the product cannot
#: hold. So the tests use MCP and replace its adapter with a scripted one: the
#: runtime bookkeeping under test is kind-agnostic, and this way the row, the
#: validator and the DB constraint are all the real ones.
KIND = "mcp"
#: The connection data is OneAgent's own (see ``tests/_external_oneagent.py``),
#: so the config that flows through the validator and the version/secret
#: bookkeeping is a shape the product actually meets rather than an invented one.
CONFIG = dict(oneagent.MCP_REMOTE_CONFIG)
SLOT = "header"
SECRET = oneagent.MCP_REMOTE_SECRET[SLOT]
ACTION = "tools.list"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


@pytest.fixture(autouse=True)
def _open_test_class(monkeypatch):
    """Open the ``test`` class the way a deployment does: in configuration."""
    from config import conf

    monkeypatch.setitem(
        conf().setdefault("external_connections", {}),
        "readiness", {KIND: {"test": True}})


@pytest.fixture
def probe_kind(monkeypatch):
    """Replace the real MCP adapter with a scripted one.

    ``result`` is read at probe time, so a test can change the answer between
    two calls. The registry is restored afterwards, so no other module sees the
    scripted adapter.
    """
    state = {"result": ProbeResult(stages=(stage_ok("handshake"),)), "ctx": None}

    real_classes = dict(base_mod._ADAPTER_CLASSES)  # noqa: SLF001
    real_instances = dict(base_mod._ADAPTERS)  # noqa: SLF001

    class _BindingAdapter(ConnectionAdapter):
        kind = KIND
        actions = frozenset({ACTION})
        write_actions = frozenset()

        def probe(self, ctx):
            # Recorded so an *editing* draft can be asked which credential it
            # actually resolved: "the store was consulted" is invisible in the
            # returned verdict once the result is scrubbed.
            state["ctx"] = ctx
            return state["result"]

    register_adapter(_BindingAdapter)
    try:
        yield state
    finally:
        base_mod._ADAPTER_CLASSES.clear()  # noqa: SLF001
        base_mod._ADAPTER_CLASSES.update(real_classes)  # noqa: SLF001
        base_mod._ADAPTERS.clear()  # noqa: SLF001
        base_mod._ADAPTERS.update(real_instances)  # noqa: SLF001


@pytest.fixture
def stack(tmp_path):
    return build_identity(tmp_path)


@pytest.fixture
def svc(stack):
    from integrations.external.service import ExternalConnectionService
    return ExternalConnectionService(stack.service)


def _create(svc, stack, **overrides):
    values = dict(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        tenant_id=stack.tenant_id, kind=KIND, name="Binding target",
        config=dict(CONFIG), secrets={SLOT: SECRET})
    values.update(overrides)
    return svc.create_connection(**values)


def _test_rows(svc, connection_id):
    return svc._store.execute(  # noqa: SLF001
        "SELECT * FROM external_connection_tests WHERE connection_id=?",
        (connection_id,))


# -- a draft persists nothing ---------------------------------------------

def test_a_draft_test_creates_no_connection_row(svc, stack, probe_kind):
    """An unsaved form has no connection, and testing it must not make one."""
    before = svc._store.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM external_connections")[0]["n"]

    payload = svc.probe_draft(KIND, dict(CONFIG), secrets={SLOT: SECRET},
                              actor_user_id=stack.root,
                              tenant_id=stack.tenant_id)

    after = svc._store.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM external_connections")[0]["n"]
    assert after == before
    assert payload["draft"] is True
    assert payload["recorded"] is False


def test_a_draft_test_stores_no_summary(svc, stack, probe_kind):
    """The draft's result goes to the caller and nowhere else."""
    connection = _create(svc, stack)
    payload = svc.probe_draft(KIND, dict(CONFIG), secrets={SLOT: SECRET},
                              actor_user_id=stack.root,
                              tenant_id=stack.tenant_id)

    assert payload["ok"] is True
    assert _test_rows(svc, connection["id"]) == []
    # And the saved connection still reads as never tested.
    assert svc.runtime().last_test(
        connection["id"], tenant_id=stack.tenant_id) is None


def test_a_draft_test_cannot_overwrite_a_saved_connections_verdict(
        svc, stack, probe_kind):
    """A passing draft must not turn a failing saved connection green."""
    probe_kind["result"] = ProbeResult(stages=(
        stage_failed("handshake", "connection_refused", stage=STAGE_NETWORK),))
    connection = _create(svc, stack)
    svc.runtime().probe(connection["id"], actor_user_id=stack.root,
                        tenant_id=stack.tenant_id)
    failed = svc.runtime().last_test(connection["id"], tenant_id=stack.tenant_id)
    assert failed is not None and failed["result"] != "ok"

    # The draft succeeds; the recorded verdict is unchanged.
    probe_kind["result"] = ProbeResult(stages=(stage_ok("handshake"),))
    svc.probe_draft(KIND, dict(CONFIG), secrets={SLOT: SECRET},
                    actor_user_id=stack.root, tenant_id=stack.tenant_id)

    still = svc.runtime().last_test(connection["id"], tenant_id=stack.tenant_id)
    assert still is not None and still["result"] == failed["result"]


def test_a_draft_test_writes_no_credential(svc, stack, probe_kind):
    """The one-shot secret is used and dropped, never stored."""
    before = svc._store.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM credentials")[0]["n"]

    svc.probe_draft(KIND, dict(CONFIG),
                    secrets={SLOT: "a-brand-new-credential"},
                    actor_user_id=stack.root, tenant_id=stack.tenant_id)

    after = svc._store.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM credentials")[0]["n"]
    assert after == before


def test_a_draft_test_refuses_a_slot_the_type_does_not_have(svc, stack,
                                                            probe_kind):
    """A supplied value the type never declares is refused, not ignored.

    Silently dropping it would let the console show a form whose credential was
    accepted and used for nothing.
    """
    with pytest.raises(ExternalConnectionError) as error:
        svc.probe_draft(KIND, dict(CONFIG), secrets={"not_a_slot": "v"},
                        actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert error.value.code == "unknown_secret_slot"


def test_a_draft_test_refuses_a_config_the_type_would_not_accept(
        svc, stack, probe_kind):
    """Config validation is the same one a save applies."""
    with pytest.raises(ExternalConnectionError) as error:
        svc.probe_draft("not_a_kind", dict(CONFIG), secrets={},
                        actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert error.value.code == "unknown_type"


def test_a_draft_test_is_refused_while_the_deployment_has_not_opened_testing(
        svc, stack, probe_kind, monkeypatch):
    """``test_not_available`` — a draft is not a way around the switch."""
    from config import conf
    monkeypatch.setitem(
        conf().setdefault("external_connections", {}),
        "readiness", {KIND: {"test": False}})

    with pytest.raises(ExternalConnectionError) as error:
        svc.probe_draft(KIND, dict(CONFIG), secrets={SLOT: SECRET},
                        actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert error.value.code == "test_not_available"


# -- a saved test is bound to a version and a secret set -------------------

def test_a_saved_test_records_the_version_it_tested(svc, stack, probe_kind):
    connection = _create(svc, stack)
    payload = svc.runtime().probe(connection["id"], actor_user_id=stack.root,
                                  tenant_id=stack.tenant_id)

    assert payload["recorded"] is True
    rows = _test_rows(svc, connection["id"])
    assert len(rows) == 1
    assert int(rows[0]["config_version"]) == int(connection["version"])
    assert svc.runtime().last_test(
        connection["id"], tenant_id=stack.tenant_id) is not None


def test_an_edit_makes_the_previous_result_unreadable(svc, stack, probe_kind):
    """保存后需重新测试: the edit bumps the version, so the old green is gone."""
    connection = _create(svc, stack)
    svc.runtime().probe(connection["id"], actor_user_id=stack.root,
                        tenant_id=stack.tenant_id)
    assert svc.runtime().last_test(
        connection["id"], tenant_id=stack.tenant_id) is not None

    edited = svc.update_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        connection_id=connection["id"],
        expected_version=int(connection["version"]),
        tenant_id=stack.tenant_id, name="Binding target (renamed)")

    assert int(edited["version"]) != int(connection["version"])
    assert svc.runtime().last_test(
        connection["id"], tenant_id=stack.tenant_id) is None


def test_a_rotated_credential_makes_the_previous_result_unreadable(
        svc, stack, probe_kind):
    """A summary produced with another secret is not evidence about this one."""
    connection = _create(svc, stack)
    svc.runtime().probe(connection["id"], actor_user_id=stack.root,
                        tenant_id=stack.tenant_id)
    assert svc.runtime().last_test(
        connection["id"], tenant_id=stack.tenant_id) is not None

    svc.update_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        connection_id=connection["id"],
        expected_version=int(connection["version"]),
        tenant_id=stack.tenant_id,
        secrets={SLOT: "a-different-secret"})

    assert svc.runtime().last_test(
        connection["id"], tenant_id=stack.tenant_id) is None


# -- the marker itself ----------------------------------------------------
#
# ``secret_versions`` is read in two places that both need it to mean "these
# are the secrets the other facts describe": the persisted test summary, and
# the digest an approval is verified against. Its own properties are asserted
# here rather than only through those two consumers.

def test_the_marker_moves_when_the_secret_material_moves(svc, stack):
    """Rotating in place must be visible; the credential row is not replaced."""
    connection = _create(svc, stack)
    before = svc.runtime().secret_versions(connection["id"])
    assert before.get(SLOT)

    svc.update_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        connection_id=connection["id"],
        expected_version=int(connection["version"]),
        tenant_id=stack.tenant_id, secrets={SLOT: "a-new-secret"})

    after = svc.runtime().secret_versions(connection["id"])
    assert after != before, (
        "an in-place rotation reuses the credential row, so a marker keyed on"
        " the row's id alone would not notice it")


def test_the_marker_is_the_same_under_two_interpreter_hash_seeds(svc, stack):
    """Two interpreters must agree on the marker for one secret.

    An approval is minted by one worker and verified by another, and the digest
    covers this value. Python's built-in ``hash`` for strings is salted per
    interpreter (``PYTHONHASHSEED``), so a marker built from it would make every
    approval valid only in the process that issued it. Two subprocesses with
    deliberately different seeds have to produce the same numbers.
    """
    import json
    import os
    import subprocess
    import sys

    connection = _create(svc, stack)
    expected = svc.runtime().secret_versions(connection["id"])
    assert expected.get(SLOT)

    script = (
        "import json;"
        "from auth.service import IdentityService;"
        "from integrations.external.service import ExternalConnectionService;"
        "svc = ExternalConnectionService(IdentityService(%r));"
        "print(json.dumps(svc.runtime().secret_versions(%r), sort_keys=True))"
        % (str(stack.service._store.db_path), connection["id"]))

    seen = set()
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed,
               "COW_CREDENTIAL_MASTER_KEY": MASTER_KEY}
        out = subprocess.run([sys.executable, "-c", script], env=env,
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        seen.add(out.stdout.strip())
    assert len(seen) == 1, ("the marker must not depend on the hash seed, or an"
                            " approval stops verifying in another process")
    assert json.loads(seen.pop()) == expected


def test_the_newest_result_for_the_current_version_is_the_one_read_back(
        svc, stack, probe_kind):
    """The lookup filters by version rather than returning the newest row."""
    connection = _create(svc, stack)
    svc.runtime().probe(connection["id"], actor_user_id=stack.root,
                        tenant_id=stack.tenant_id)

    probe_kind["result"] = ProbeResult(stages=(
        stage_failed("handshake", "connection_refused", stage=STAGE_NETWORK),))
    svc.update_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        connection_id=connection["id"],
        expected_version=int(connection["version"]),
        tenant_id=stack.tenant_id, name="moved on")
    svc.runtime().probe(connection["id"], actor_user_id=stack.root,
                        tenant_id=stack.tenant_id)

    state = svc.runtime().last_test(connection["id"], tenant_id=stack.tenant_id)
    current = svc.runtime().snapshot(
        connection["id"], tenant_id=stack.tenant_id).version
    assert state is not None
    assert int(state["config_version"]) == int(current)
    assert state["result"] != "ok", "the older green must not be read back"


# -- a result that arrives after the configuration moved is discarded ------

def _stale_recorder(svc, stack, probe_kind):
    """A probe of a row that is edited from inside the probe itself.

    That is the race the task names: the row exists when the probe starts, the
    adapter is handed its version, and the configuration moves before the result
    can be recorded. Scripting the edit inside the adapter is what makes the race
    deterministic instead of a sleep-and-hope.
    """
    connection = _create(svc, stack)
    original = probe_kind["result"]

    def _hook(_ctx):
        svc.update_connection(
            actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
            connection_id=connection["id"],
            expected_version=int(connection["version"]),
            tenant_id=stack.tenant_id, name="renamed mid-probe")
        return original

    return connection, _hook


def test_a_result_that_lost_the_race_is_discarded_not_stored(
        svc, stack, probe_kind):
    """配置在测试期间变更 → 结果被丢弃并明确告知,而不是写成本次结果."""
    connection, hook = _stale_recorder(svc, stack, probe_kind)

    class _Racing(ConnectionAdapter):
        kind = KIND
        actions = frozenset({ACTION})
        write_actions = frozenset()

        def probe(self, ctx):
            return hook(ctx)

    register_adapter(_Racing)
    with pytest.raises(ExternalConnectionError) as error:
        svc.runtime().probe(connection["id"], actor_user_id=stack.root,
                            tenant_id=stack.tenant_id)

    assert error.value.code == "test_result_stale"
    assert error.value.status == 409
    # And nothing was stored for either version.
    assert _test_rows(svc, connection["id"]) == []
    assert svc.runtime().last_test(
        connection["id"], tenant_id=stack.tenant_id) is None


def test_a_stale_result_is_not_stored_at_all(svc, stack, probe_kind):
    """Discarded means discarded: no row is left behind for the old version."""
    connection = _create(svc, stack)
    snapshot = svc.runtime().snapshot(connection["id"], tenant_id=stack.tenant_id)
    svc.update_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        connection_id=connection["id"],
        expected_version=int(connection["version"]),
        tenant_id=stack.tenant_id, name="renamed before recording")

    # The recorder is the half that decides: handed a summary for a version the
    # row has already left, it reports the loss instead of storing it.
    stored = svc.runtime()._record_test(  # noqa: SLF001
        snapshot, ProbeResult(stages=(stage_ok("handshake"),)), "ok",
        actor_user_id=stack.root, duration_ms=1)
    assert stored is False
    assert _test_rows(svc, connection["id"]) == []


# -- record=False records nothing -----------------------------------------

def test_record_false_stores_nothing_even_when_the_result_is_good(
        svc, stack, probe_kind):
    connection = _create(svc, stack)
    payload = svc.runtime().probe(
        connection["id"], actor_user_id=stack.root,
        tenant_id=stack.tenant_id, record=False)

    assert payload["ok"] is True
    assert payload["recorded"] is False
    assert _test_rows(svc, connection["id"]) == []


# -- the summary never carries the secret ---------------------------------

def test_a_recorded_summary_never_contains_the_secret(svc, stack, probe_kind):
    """A stored summary is redacted, like every other persisted artifact."""
    probe_kind["result"] = ProbeResult(stages=(
        stage_ok("handshake"),
        stage_failed("handshake", "authorization_failed", stage=STAGE_AUTH,
                     detail="rejected credential " + SECRET),
    ))
    connection = _create(svc, stack)
    svc.runtime().probe(connection["id"], actor_user_id=stack.root,
                        tenant_id=stack.tenant_id)

    rows = _test_rows(svc, connection["id"])
    assert len(rows) == 1
    stored = json.dumps([dict(row) for row in rows], ensure_ascii=False)
    assert SECRET not in stored

# -- the editing draft (change align-mcp-editor-with-master, task 3.3) ------
#
# The master editor tests an *existing* connection before saving it. That draft
# carries the connection id and the version the caller was editing, which makes
# it a different thing from the unsaved-form draft above: a slot the caller did
# not replace has to come from the store, and a version that moved underneath
# the form has to invalidate the attempt instead of testing a mix of old and
# new. The keep/replace/clear vocabulary is the save's, so the browser is never
# handed the stored value back.

def test_an_editing_draft_resolves_an_untouched_slot_from_the_store(
        svc, stack, probe_kind):
    connection = _create(svc, stack)

    payload = svc.probe_draft(
        KIND, dict(CONFIG, url="https://other.example.com/mcp"),
        secrets={}, actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"],
        expected_version=int(connection["version"]))

    assert payload["ok"] is True
    # The blank field meant "keep": the stored credential is what was probed.
    assert probe_kind["ctx"].optional_secret(SLOT) == SECRET
    # And the address the caller typed is the one that was used.
    assert probe_kind["ctx"].config["url"] == "https://other.example.com/mcp"


def test_an_editing_draft_uses_a_replacement_for_this_attempt_only(
        svc, stack, probe_kind):
    connection = _create(svc, stack)

    svc.probe_draft(
        KIND, dict(CONFIG), secrets={SLOT: "a-one-shot-replacement"},
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"],
        expected_version=int(connection["version"]))

    assert probe_kind["ctx"].optional_secret(SLOT) == "a-one-shot-replacement"
    # A draft is not a rotation: the saved verdict is untouched and a later
    # saved test still probes the stored value.
    assert svc.runtime().last_test(
        connection["id"], tenant_id=stack.tenant_id) is None
    svc.runtime().probe(connection["id"], actor_user_id=stack.root,
                        tenant_id=stack.tenant_id)
    assert probe_kind["ctx"].optional_secret(SLOT) == SECRET


def test_an_editing_draft_can_clear_a_slot_for_this_attempt(
        svc, stack, probe_kind):
    """`null` is the explicit clear -- the probe must not fall back to the store."""
    connection = _create(svc, stack)

    svc.probe_draft(
        KIND, dict(CONFIG), secrets={SLOT: None},
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"],
        expected_version=int(connection["version"]))

    from integrations.external.adapters.base import AdapterError
    with pytest.raises(AdapterError):
        probe_kind["ctx"].secret(SLOT)


def test_an_editing_draft_is_refused_when_the_connection_moved(
        svc, stack, probe_kind):
    connection = _create(svc, stack)
    stale = int(connection["version"])
    svc.update_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        connection_id=connection["id"], expected_version=stale,
        tenant_id=stack.tenant_id, name="Renamed")

    with pytest.raises(ExternalConnectionError) as caught:
        svc.probe_draft(
            KIND, dict(CONFIG), secrets={},
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_version=stale)

    assert caught.value.code == "version_conflict"
    # Nothing was probed from the stale form.
    assert probe_kind["ctx"] is None


def test_an_editing_draft_reports_the_version_it_was_based_on(
        svc, stack, probe_kind):
    connection = _create(svc, stack)
    payload = svc.probe_draft(
        KIND, dict(CONFIG), secrets={}, actor_user_id=stack.root,
        tenant_id=stack.tenant_id, connection_id=connection["id"],
        expected_version=int(connection["version"]))

    assert payload["connection_id"] == connection["id"]
    assert payload["config_version"] == int(connection["version"])


def test_an_unsaved_draft_still_resolves_nothing_from_the_store(
        svc, stack, probe_kind):
    """The other direction: without an id the draft has no store to fall back to."""
    from integrations.external.adapters.base import AdapterError
    svc.probe_draft(KIND, dict(CONFIG), secrets={},
                    actor_user_id=stack.root, tenant_id=stack.tenant_id)

    with pytest.raises(AdapterError):
        probe_kind["ctx"].secret(SLOT)
