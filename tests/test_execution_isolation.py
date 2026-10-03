# encoding:utf-8
"""Tenant execution isolation gate (open-database-runtime 6.x).

The gate confines database-mode code execution (bash) and file tools to the
current tenant's roots: cross-tenant reads, home/data-root credential reads,
path traversal and symlink escape are refused before any subprocess or file
call. Pure path-free calls and legacy (non-tenant) runs pass through.
"""

import os

import pytest

from agent.permission import isolation as iso
from agent.permission.isolation import _Boundary, isolation_decision


def _boundary(tmp_path, *, other_root=None, home=None, data_root=None,
              tenant_id="t1"):
    own = tmp_path / "own"
    work = tmp_path / "work"
    usr = tmp_path / "users" / "u1"
    tdir = tmp_path / "tmp"
    for root in (own, work, usr, tdir):
        root.mkdir(parents=True, exist_ok=True)
    boundary = _Boundary(
        read_roots=[os.path.realpath(p) for p in (own, work, usr, tdir)],
        write_roots=[os.path.realpath(p) for p in (work, usr, tdir)],
        blocked=[os.path.realpath(home or tmp_path / "home"), os.path.realpath(data_root or tmp_path / "data")],
        engineering=os.path.realpath(work),
        tenant_id=tenant_id,
    )
    if other_root is not None:
        other_root.mkdir(parents=True, exist_ok=True)
        boundary.blocked.append(os.path.realpath(other_root))
    return boundary, {k: str(v) for k, v in {
        "own": own, "work": work, "usr": usr, "tdir": tdir,
    }.items()}


class _Ident:
    user_id = "u1"
    tenant_id = "t1"
    agent_id = "alpha"


@pytest.fixture(autouse=True)
def _db_identity(monkeypatch, tmp_path):
    """Fake the DB-mode current identity and the boundary resolver."""
    monkeypatch.setattr(iso, "enabled", lambda: True)
    monkeypatch.setattr(iso, "_current_identity", lambda: _Ident())
    monkeypatch.setattr(iso, "_svc", lambda: object())
    monkeypatch.setattr(iso, "resolve_boundary",
                        lambda ident=None: _boundary(tmp_path)[0])
    yield


def test_legacy_no_tenant_run_is_unaffected(monkeypatch, tmp_path):
    """A legacy install (isolation gate off) keeps the historical unconfined
    behaviour even though no tenant identity is present.

    Note the gate itself is what makes a run "legacy": ``enabled()`` is False
    whenever ``database_mode()`` is False. A database-mode run that merely lost
    its identity is NOT this case — see the fail-closed tests below.
    """
    monkeypatch.setattr(iso, "enabled", lambda: False)
    monkeypatch.setattr(iso, "database_mode", lambda: False)
    monkeypatch.setattr(iso, "_current_identity", lambda: type(
        "E", (), {"user_id": None, "tenant_id": None, "agent_id": None})())
    assert isolation_decision("bash", {"command": "cat /etc/hostname"}, None).allowed


def test_bash_read_outside_home_blocked(monkeypatch, tmp_path):
    boundary, _ = _boundary(tmp_path, home=os.path.expanduser("~"))
    decision = iso._check_bash(boundary, {"command": "cat ~/.ssh/id_rsa"}, str(boundary.engineering))
    assert not decision.allowed and "隔离" in decision.reason


def test_bash_cross_tenant_read_blocked(tmp_path):
    boundary, _ = _boundary(tmp_path, other_root=tmp_path / "other-tenant")
    (tmp_path / "other-tenant" / "secret.txt").write_text("x", encoding="utf-8")
    decision = iso._check_bash(
        boundary, {"command": f"cat {tmp_path}/other-tenant/secret.txt"},
        str(boundary.engineering))
    assert not decision.allowed


def test_bash_redirect_into_other_tenant_blocked(tmp_path):
    boundary, _ = _boundary(tmp_path, other_root=tmp_path / "other-tenant")
    decision = iso._check_bash(
        boundary, {"command": f"echo hi > {tmp_path}/other-tenant/out.txt"},
        str(boundary.engineering))
    assert not decision.allowed


def test_bash_write_outside_tenant_write_roots_blocked(tmp_path):
    # Home/data are blocked, so this is refused by the blocked-root check; use
    # an external read-allowed location for the pure write-root refusal.
    outside = tmp_path / "outside-keep"
    outside.mkdir()
    boundary, _ = _boundary(tmp_path)
    decision = iso._check_bash(
        boundary, {"command": f"echo hi > {outside}/x.txt"}, str(boundary.engineering))
    assert not decision.allowed


def test_bash_allowed_inside_tenant(tmp_path):
    boundary, dirs = _boundary(tmp_path)
    assert iso._check_bash(boundary, {"command": f"ls {dirs['work']}"},
                           str(boundary.engineering)).allowed
    assert iso._check_bash(boundary, {"command": f"echo hi > {dirs['tdir']}/x.txt"},
                           str(boundary.engineering)).allowed
    # Reading a file in the tenant's own shared root is fine even when the
    # shared root is outside the *write* roots.
    assert iso._check_bash(boundary, {"command": f"cat {dirs['own']}/README.md"},
                           str(boundary.engineering)).allowed
    # Pure logic / path-free commands are untouched.
    assert iso._check_bash(boundary, {"command": "pwd && date"},
                           str(boundary.engineering)).allowed


def test_bash_symlink_escape_blocked(tmp_path):
    boundary, dirs = _boundary(tmp_path, other_root=tmp_path / "other-tenant")
    secret = tmp_path / "other-tenant" / "secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("x", encoding="utf-8")
    link = os.path.join(dirs["work"], "escape")
    os.symlink(secret, link)
    decision = iso._check_bash(boundary, {"command": f"cat {link}"},
                               str(boundary.engineering))
    assert not decision.allowed  # realpath resolves the link out of the tenant


def test_bash_parent_traversal_blocked(tmp_path):
    boundary, dirs = _boundary(tmp_path, other_root=tmp_path / "other-tenant")
    decision = iso._check_bash(
        boundary,
        {"command": f"cat {dirs['work']}/../other-tenant/secret.txt"},
        str(boundary.engineering))
    assert not decision.allowed


def test_bash_unparsable_fails_closed(tmp_path):
    boundary, _ = _boundary(tmp_path)
    # Unbalanced quotes cannot be lexed; fail closed rather than run.
    decision = iso._check_bash(boundary, {"command": "echo \"oops"},
                               str(boundary.engineering))
    assert not decision.allowed


def test_write_tool_confined_to_tenant(tmp_path):
    boundary, dirs = _boundary(tmp_path)
    assert isolation_decision("write", {"path": f"{dirs['work']}/a.md", "content": "x"},
                              cwd=dirs["work"]).allowed
    other = tmp_path / "other-tenant"
    other.mkdir()
    boundary.blocked.append(str(other))
    decision = isolation_decision("write", {"path": f"{other}/x.md", "content": "x"},
                                  cwd=dirs["work"])
    assert not decision.allowed
    # A writable path outside the tenant roots but not blocked is also denied.
    outside = tmp_path / "plain-outside"
    outside.mkdir()
    decision = isolation_decision("edit", {"path": f"{outside}/x.md", "content": "x"},
                                  cwd=dirs["work"])
    assert not decision.allowed


def test_read_tool_allowed_anywhere_outside_blocked(tmp_path):
    boundary, dirs = _boundary(tmp_path)
    outside = tmp_path / "plain-outside"
    outside.mkdir()
    (outside / "x.txt").write_text("x", encoding="utf-8")
    assert isolation_decision("read", {"path": f"{outside}/x.txt"},
                              cwd=dirs["work"]).allowed
    home = tmp_path / "home"
    home.mkdir()
    boundary.blocked.append(str(home))
    assert not isolation_decision("read", {"path": f"{home}/notes.txt"},
                                  cwd=dirs["work"]).allowed


def test_path_free_readonly_tools_untouched(tmp_path):
    assert isolation_decision("web_search", {"query": "x"}, None).allowed
    assert isolation_decision("memory_search", {"query": "x"}, None).allowed
    assert isolation_decision("bash", {}, None).allowed  # background follow-up


def test_disabled_isolation_refuses_bash_in_db_but_not_readonly(monkeypatch, tmp_path):
    """Task 6.3: DB mode with the slice disabled fails closed for arbitrary
    code; pure-LLM and read-only tools stay unaffected."""
    monkeypatch.setattr(iso, "enabled", lambda: False)
    monkeypatch.setattr(iso, "database_mode", lambda: True)
    decision = isolation_decision("bash", {"command": "echo hi"}, None)
    assert not decision.allowed and "未启用" in decision.reason
    # Read-only / path-free tools are not gated.
    assert isolation_decision("read", {"path": f"{tmp_path}/x.txt"}, None).allowed
    assert isolation_decision("web_search", {"query": "x"}, None).allowed
    assert isolation_decision("write", {"path": f"{tmp_path}/x.txt", "content": "x"},
                              None).allowed


def test_disabled_isolation_legacy_unaffected(monkeypatch, tmp_path):
    """Non-DB installs keep the historical unconfined behaviour."""
    monkeypatch.setattr(iso, "enabled", lambda: False)
    monkeypatch.setattr(iso, "database_mode", lambda: False)
    assert isolation_decision("bash", {"command": "echo hi"}, None).allowed


def test_rule_text_is_stable_and_honest(tmp_path):
    boundary, _ = _boundary(tmp_path, other_root=tmp_path / "other")
    decision = iso._check_bash(
        boundary, {"command": f"cat {tmp_path}/other/secret"}, str(boundary.engineering))
    assert "隔离边界拒绝" in decision.reason
    assert "Isolation boundary refused" in decision.reason
    assert "not an OS sandbox" in decision.reason


# ---------------------------------------------------------------------------
# Fail-closed on an unresolvable identity (fork-decoupling 5.1-5.6)
# ---------------------------------------------------------------------------

def _no_identity():
    return type("E", (), {"user_id": None, "tenant_id": None, "agent_id": None})()


def test_gate_on_without_identity_denies_code_tools(monkeypatch, tmp_path):
    """The gate being on while the identity cannot be resolved is the exact
    state the audit flagged: it MUST NOT be read as 'no user dimension'."""
    monkeypatch.setattr(iso, "_current_identity", _no_identity)
    decision = isolation_decision("bash", {"command": "echo hi"}, None)
    assert not decision.allowed
    assert "身份" in decision.reason


def test_gate_on_without_identity_denies_file_tools_too(monkeypatch, tmp_path):
    """No tool-type distinction: file tools and path-free tools are refused
    just like code tools, so losing the identity buys no unconfined path."""
    monkeypatch.setattr(iso, "_current_identity", _no_identity)
    assert not isolation_decision(
        "write", {"path": f"{tmp_path}/x.txt", "content": "x"}, None).allowed
    assert not isolation_decision(
        "read", {"path": f"{tmp_path}/x.txt"}, None).allowed
    assert not isolation_decision("web_search", {"query": "x"}, None).allowed


def test_gate_malfunction_denies_non_code_tools(monkeypatch, tmp_path):
    """A broken boundary resolver must refuse a file tool, not fall through to
    the historical unrestricted behaviour (scenario: 非代码类工具在门禁损坏时)."""
    def boom(ident=None):
        raise RuntimeError("identity db down")

    monkeypatch.setattr(iso, "resolve_boundary", boom)
    decision = isolation_decision(
        "write", {"path": f"{tmp_path}/x.txt", "content": "x"}, None)
    assert not decision.allowed


def test_fail_closed_denial_is_counted(monkeypatch, tmp_path):
    """A fail-closed refusal must be observable, not just a log line."""
    from common import security_events

    security_events.reset_counters()
    monkeypatch.setattr(iso, "_current_identity", _no_identity)
    isolation_decision("bash", {"command": "echo hi"}, None)
    assert security_events.counters().get("isolation", 0) >= 1


def test_legacy_mode_never_records_a_denial(monkeypatch, tmp_path):
    """Legacy installs are not a refusal case, so they must not inflate the
    counter (which exists to expose identity loss in database mode)."""
    from common import security_events

    security_events.reset_counters()
    monkeypatch.setattr(iso, "enabled", lambda: False)
    monkeypatch.setattr(iso, "database_mode", lambda: False)
    monkeypatch.setattr(iso, "_current_identity", _no_identity)
    assert isolation_decision("bash", {"command": "echo hi"}, None).allowed
    assert security_events.counters() == {}


# ---------------------------------------------------------------------------
# A blocked home must not shadow the tenant's own nested roots
# (fix-instance-root-trust: instance root ~/cow and the tenant base
# ~/.cow/tenant-roots live under home; blocking home wholesale refused every
# access to the tenant's own workspace as "outside the isolation root").
# ---------------------------------------------------------------------------

def test_blocked_home_does_not_shadow_nested_tenant_root(monkeypatch, tmp_path):
    """Regression: the default Agent has a private workspace
    ``<instance root>/agents/<id>`` and the tenant root sits under home. The
    tenant's own root MUST stay reachable; home paths outside it stay refused."""
    home = tmp_path / "home"
    root = home / "cow"
    work = root / "agents" / "alpha"
    ssh = home / ".ssh"
    for directory in (root, work, ssh):
        directory.mkdir(parents=True, exist_ok=True)
    (root / "AGENT.md").write_text("x", encoding="utf-8")
    (ssh / "id_rsa").write_text("secret", encoding="utf-8")
    boundary = _Boundary(
        read_roots=[str(root), str(work)],
        write_roots=[str(root), str(work)],
        blocked=[str(home), str(tmp_path / "data")],
        engineering=str(work),
        tenant_id="t1",
    )
    monkeypatch.setattr(iso, "resolve_boundary", lambda ident=None: boundary)

    # The tenant's own root is a legal root carved out of the blocked home.
    assert iso.isolation_decision("ls", {"path": str(root)}, cwd=str(work)).allowed
    assert iso.isolation_decision(
        "read", {"path": str(root / "AGENT.md")}, cwd=str(work)).allowed
    assert iso.isolation_decision(
        "write", {"path": str(root / "new.md"), "content": "x"}, cwd=str(work)).allowed
    assert iso._check_bash(boundary, {"command": f"ls {root}"}, str(work)).allowed
    assert iso._check_bash(
        boundary, {"command": f"cat {root}/AGENT.md"}, str(work)).allowed
    assert iso._check_bash(
        boundary, {"command": f"echo hi > {work}/out.txt"}, str(work)).allowed

    # Credentials under the same home but outside the tenant roots stay refused.
    assert not iso.isolation_decision(
        "read", {"path": str(ssh / "id_rsa")}, cwd=str(work)).allowed
    assert not iso._check_bash(
        boundary, {"command": f"cat {ssh}/id_rsa"}, str(work)).allowed


def test_private_subtrees_override_shared_parent_allowance(tmp_path):
    boundary, dirs = _boundary(tmp_path)
    parent = dirs['work'] + '/user'
    boundary.private = [(parent, parent + '/u1')]
    assert not iso._in_blocked(boundary, parent + '/u1/output.txt', boundary.read_roots)
    assert iso._in_blocked(boundary, parent + '/u2/output.txt', boundary.read_roots)
    assert iso._in_blocked(boundary, parent + '/new-owner/secret.txt', boundary.write_roots)
    boundary.hard_blocked = [dirs['work'] + '/mcp.json']
    assert iso._in_blocked(boundary, dirs['work'] + '/mcp.json', boundary.read_roots)
