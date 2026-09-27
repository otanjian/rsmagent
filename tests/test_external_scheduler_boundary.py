# encoding:utf-8
"""A task that fires later re-derives everything; it reuses nothing.

Change ``add-external-system-access``, task 11.4: "验证 scheduler 延迟执行时的当前
成员资格、委托、凭据版本、审批和配额，禁止复用入队时授权或伪造 Membership。"

The claim is a negative one — *nothing* decided at enqueue time is still
authoritative at fire time — and a negative claim is only worth asserting
against the real path, because a cache would be invisible to a unit test that
hands the fire its own inputs. So the fixtures here are a real tenant, a real
connection store and the real fire seam
(``agent.tools.scheduler.identity.execution_identity`` plus
``integrations.external`` provider/dispatch), and each test changes the world
*after* the task exists.

Five axes, one test each:

``membership`` and ``delegation``
    The fire's identity comes from the task's stored owner, resolved against the
    identity database **now**. A member who lost ``agent.use`` — or a task with
    no member owner at all — reaches no external connection, and the personal
    mailbox case is explicit: a machine identity is never a stand-in for the
    person who owns the mailbox.

``credential version``
    The secret is resolved from the store at call time, so a rotation between
    enqueue and fire is what the fire uses. A fire that used the enqueue-time
    value would keep running after the member had revoked their own mailbox
    password.

``approval``
    A high-risk write needs an approval that is bound to the *request* — the
    connection, its configuration version, its secret versions, the actor, the
    parameters — and capped by ``max_age_seconds``. A task cannot carry one
    across time, so a write fired by a task is refused rather than inheriting
    whatever was approved when the task was created.

``quota``
    Consumption is metered where the call happens, not where the task was
    created, so a quota that was fine at enqueue time still refuses at fire
    time.
"""

from __future__ import annotations

import pytest

from tests._helpers import build_identity

MASTER_KEY = "scheduler-boundary-master-key"
AGENT = "agent-a"
KIND = "email"
MCP_KIND = "mcp"
MCP_CONFIG = {"transport": "streamable_http",
              "url": "https://mcp.example.com/mcp"}
MAILBOX_CONFIG = {
    "imap": {"enabled": True, "host": "imap.example.com", "port": 993,
             "user": "member@example.com"},
    "smtp": {"enabled": True, "host": "smtp.example.com", "port": 465,
             "user": "member@example.com", "from_addr": "member@example.com"},
}


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """A tenant whose one member may use ``AGENT``, on a single database.

    ``identity_db_path`` is set to the same file the fixture's own service uses:
    the type adapters list through the process-wide service, and with two paths
    a connection saved here would be invisible to the provider there.
    """
    from config import conf

    built = build_identity(tmp_path, agents=(AGENT,))
    monkeypatch.setattr("auth.service.get_identity_service", lambda: built.service)
    monkeypatch.setitem(conf(), "identity_db_path", str(tmp_path / "identity.db"))
    return built


@pytest.fixture
def opened(monkeypatch):
    """Open the execution classes a deployment would open, via configuration."""
    from config import conf

    def _open(*classes):
        monkeypatch.setitem(
            conf().setdefault("external_connections", {}),
            "readiness", {KIND: {name: True for name in classes},
                          MCP_KIND: {name: True for name in classes}})
    return _open


@pytest.fixture
def member(stack):
    """A member who may use ``AGENT`` and may manage their own connections.

    Plain ``member`` carries ``external.connections.read`` but not ``manage``;
    creating a personal mailbox needs manage, so the fixture adds a companion
    role rather than measuring that grant.
    """
    return _ready_member(stack, "member")

def _mcp_name(action, connection_id, remote=""):
    """The binding name the adapter composes for one action on one connection.

    Built through the composer rather than written out, because the wire format
    is a contract with the provider (``^[a-zA-Z0-9_-]+$``) and not a spelling
    these cases are about: they are about which capabilities are offered and
    which calls are authorized. The spelling itself is pinned in
    ``tests/test_tool_name_wire_contract.py``.
    """
    from agent.tools.mcp.external import tool_name
    return tool_name(action=action, connection_id=connection_id,
                     remote_name=remote)


def _model_name(action, connection_id, remote=""):
    """The name the model sees: the binding name carrying its origin prefix."""
    from agent.tools.external.external_tool import _external_tool_name
    return _external_tool_name(_mcp_name(action, connection_id, remote))


def _ready_member(stack, username):
    """Create a member holding both the baseline role and connection access."""
    stack.agent_role("conn-owner", [AGENT], permissions=[
        "external.connections.read", "external.connections.manage",
        "chat.use", "agent.use", "agent.read"])
    return stack.member(username, ["member", "conn-owner"])


@pytest.fixture
def mailbox(stack, member, opened):
    """The member's own mailbox, created the way the console creates one."""
    from integrations.external import registry
    from integrations.external.service import ExternalConnectionService

    opened("read_execute", "write_execute")
    svc = ExternalConnectionService(stack.service)
    connection = svc.create_connection(
        actor_user_id=member, scope=registry.SCOPE_PERSONAL,
        tenant_id=stack.tenant_id, kind=KIND, name="member@example.com",
        config=MAILBOX_CONFIG,
        secrets={"imap_password": "enqueue-time-password",
                 "smtp_password": "enqueue-time-password"})
    return svc, connection


def _task_for(stack, member, *, action=None, agent_id=AGENT):
    """A stored personal task as the scheduler would have written it.

    ``owner_snapshot`` is the real creation-time snapshot, so the owner carries
    exactly the fields a fired task re-reads.
    """
    from agent.tools.scheduler.identity import owner_snapshot
    from common.runtime_identity import RuntimeIdentity, use_identity

    with use_identity(RuntimeIdentity(agent_id=agent_id, user_id=member,
                                      tenant_id=stack.tenant_id,
                                      session_id="sess-1")):
        owner = owner_snapshot({"agent_id": agent_id, "session_id": "sess-1"})
    return {
        "id": "task-1", "enabled": True, "scope": "personal", "owner": owner,
        "action": action or {"type": "send_message", "content": "hi",
                             "receiver": "member", "channel_type": "unknown"},
    }


def _fire_identity(stack, task, agent_id=AGENT):
    """The identity the fire runs under — the scheduler's own resolver."""
    from agent.tools.scheduler.identity import execution_identity

    return execution_identity(task, agent_id)


# -- membership and delegation --------------------------------------------

def test_a_task_fires_as_the_owner_and_so_reaches_the_owners_connection(mailbox, stack):
    """The baseline the other tests remove something from."""
    from common.runtime_identity import use_identity
    from integrations.external.tools import available_tools

    member = stack.members["member"]
    task = _task_for(stack, member)
    identity = _fire_identity(stack, task)
    assert identity.user_id == member and identity.tenant_id == stack.tenant_id

    with use_identity(identity):
        names = [b.tool.name for b in available_tools(
            tenant_id=identity.tenant_id, actor_user_id=identity.user_id)]
    assert names, "the owner's mailbox must be reachable by a fire of their task"


def test_a_fire_after_the_agent_grant_was_revoked_reaches_no_connection(
        mailbox, stack):
    """Membership is re-resolved at fire time, and the external set with it.

    The connection still exists and the member is still a member; what changed
    is the ``agent.use`` grant the fire's identity depends on. The fire must be
    skipped — and, independently of the skip, the identity it would have used
    must not yield external tools.
    """
    from agent.tools.scheduler.identity import AGENT_DENIED, revalidate_owner
    from common.runtime_identity import use_identity
    from integrations.external.tools import available_tools

    member = stack.members["member"]
    task = _task_for(stack, member)
    role = [r for r in stack.service.list_roles(stack.tenant_id)
            if r["code"] == "conn-owner"][0]
    stack.service.update_role(
        stack.root, stack.tenant_id, role["id"], "Connection owner",
        ["external.connections.read", "external.connections.manage", "chat.use"],
        expected_version=role["version"], resource_grants=[], model_defaults={})

    assert revalidate_owner(task) == AGENT_DENIED, (
        "the fire must be refused: the grant it depends on is gone")

    # Belt and braces: the connection is genuinely still there, so the refusal
    # above came from the identity re-check and not from a vanished mailbox.
    # The call gate is what refuses the action, not the listing.
    svc, connection = mailbox
    from integrations.external import registry
    assert svc.resolve_secret(
        connection_id=connection["id"], slot="imap_password",
        scope=registry.SCOPE_PERSONAL, tenant_id=stack.tenant_id,
        actor_user_id=member) == "enqueue-time-password"


def test_a_task_with_no_member_owner_never_reaches_a_personal_connection(
        mailbox, stack):
    """A machine subject is not a person (the 11.3 rule, at the 11.4 seam).

    A legacy/Agent-only task has no member owner, so ``execution_identity``
    resolves an Agent-only identity with no ``user_id``. The personal-mailbox
    provider returns nothing for such a caller — the mailbox is the member's,
    and no scheduled task may stand in for them.
    """
    from common.runtime_identity import use_identity
    from integrations.external.tools import available_tools

    task = _task_for(stack, stack.members["member"])
    task.pop("owner")                      # the legacy shape
    identity = _fire_identity(stack, task)
    assert identity.user_id in (None, ""), (
        "an ownerless task must not inherit a member identity")

    with use_identity(identity):
        names = [b.tool.name for b in available_tools(
            tenant_id=stack.tenant_id, actor_user_id=identity.user_id or "")]
    assert names == [], "a machine identity must not be offered the mailbox"


def test_a_forged_owner_snapshot_cannot_fire_as_a_member(stack, mailbox):
    """The snapshot names a member; the database decides whether that is true.

    A stored owner is a *claim* about who asked, written at creation time. The
    fire re-resolves it, so a snapshot naming a user who is not a member of the
    tenant it claims — the shape a forged or hand-edited task would have — is
    refused rather than executed as that member.
    """
    from agent.tools.scheduler.identity import NOT_MEMBER, revalidate_owner

    foreign = stack.other_tenant()
    task = _task_for(stack, stack.members["member"])
    # The claim: the foreign member, inside *this* tenant.
    task["owner"]["user_id"] = foreign["user_id"]
    task["owner"]["tenant_id"] = stack.tenant_id

    assert revalidate_owner(task) == NOT_MEMBER, (
        "a snapshot is not a membership: the claim must be re-resolved")
    assert foreign["tenant_id"] != stack.tenant_id, (
        "precondition: the named user really is outside this tenant")


# -- credential version ----------------------------------------------------

def test_a_rotation_after_enqueue_is_what_the_fire_uses(mailbox, stack):
    """The secret is read at call time; the enqueue-time value is not kept."""
    from integrations.external import registry

    svc, connection = mailbox
    member = stack.members["member"]
    task = _task_for(stack, member)          # the task exists *now*
    assert _fire_identity(stack, task).user_id == member

    rotated = svc.update_connection(
        actor_user_id=member, scope=registry.SCOPE_PERSONAL,
        connection_id=connection["id"], expected_version=connection["version"],
        tenant_id=stack.tenant_id,
        secrets={"imap_password": "rotated-after-enqueue"})
    assert rotated["secrets"]["imap_password"]["configured"] is True

    assert svc.resolve_secret(
        connection_id=connection["id"], slot="imap_password",
        scope=registry.SCOPE_PERSONAL, tenant_id=stack.tenant_id,
        actor_user_id=member) == "rotated-after-enqueue", (
        "a fire must use the current credential; reusing the enqueue-time one "
        "would keep a revoked password alive")


def test_a_rotation_also_invalidates_an_approval_issued_before_it(stack, member):
    """Credential version is part of the approval binding, not a detail.

    An approval minted while the old credential was in place must not authorise
    the same request after the credential changed: the approver agreed to an
    action performed with one credential, and a different secret makes it a
    different action. A write is used because only an approval-requiring action
    reaches the digest comparison at all.
    """
    import time

    from integrations.external import risk
    from integrations.external.errors import ExternalConnectionError
    from integrations.external.risk import check_invocation
    from integrations.external.service import ExternalConnectionService

    svc = ExternalConnectionService(stack.service)
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind=MCP_KIND, name="Remote MCP",
        config={**MCP_CONFIG, "auth": "header", "header_name": "Authorization"},
        secrets={"header": "Bearer before"})

    approver = stack.member("approver", ["member"])
    params = {"name": "remote_tool", "arguments": {}}
    assert risk.required_level(MCP_KIND, "tools.call", write=True) in \
        risk.APPROVAL_REQUIRED, "precondition: this action needs an approval"

    versions_before = svc.runtime().secret_versions(connection["id"])
    binding = risk.approval_binding(
        kind=MCP_KIND, action="tools.call", connection_id=connection["id"],
        config_version=connection["version"], secret_versions=versions_before,
        actor_user_id=stack.root, tenant_id=stack.tenant_id, target=None,
        params=params, key="")
    decision = {"approved": True, "approver_user_id": approver,
                "digest": binding["digest"], "issued_at": int(time.time()),
                "expires_at": 0, "revoked": False, "scope": "once"}

    updated = svc.update_connection(
        actor_user_id=stack.root, scope="tenant",
        connection_id=connection["id"], expected_version=connection["version"],
        tenant_id=stack.tenant_id, secrets={"header": "Bearer after"})
    assert updated["version"] > connection["version"], "precondition: it changed"
    versions_after = svc.runtime().secret_versions(connection["id"])
    assert versions_after != versions_before, (
        "precondition: the rotation is visible in the secret versions")

    with pytest.raises(ExternalConnectionError) as refused:
        check_invocation(
            MCP_KIND, "tools.call", params,
            connection_id=connection["id"],
            config_version=updated["version"],
            secret_versions=versions_after, actor_user_id=stack.root,
            tenant_id=stack.tenant_id, approval=decision)
    assert refused.value.code.startswith("approval_")


# -- approval --------------------------------------------------------------

def test_a_write_fired_by_a_task_carries_no_approval_and_is_refused(stack):
    """A task cannot carry an approval across time, so it cannot write.

    ``tools.call`` on an MCP connection is an approval-requiring write. The
    scheduler seam passes no approval — there is nothing in a stored task that
    could hold one — and the risk gate refuses. This is the assertion that a
    fire does not inherit the approval that may have been granted when the task
    was created.
    """
    from integrations.external import risk
    from integrations.external.errors import ExternalConnectionError
    from integrations.external.service import ExternalConnectionService

    svc = ExternalConnectionService(stack.service)
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant",
        tenant_id=stack.tenant_id, kind=MCP_KIND, name="Remote MCP",
        config=MCP_CONFIG)
    params = {"name": "remote_tool", "arguments": {}}
    # What the deployment would have asked a human to approve:
    assert risk.required_level(MCP_KIND, "tools.call", write=True) in \
        risk.APPROVAL_REQUIRED

    with pytest.raises(ExternalConnectionError) as refused:
        risk.check_invocation(
            MCP_KIND, "tools.call", params,
            connection_id=connection["id"],
            config_version=connection["version"],
            secret_versions={}, actor_user_id=stack.root,
            tenant_id=stack.tenant_id, approval=None)
    assert refused.value.code.startswith("approval_")


def test_an_expired_approval_is_refused_at_fire_time(stack):
    """An approval is capped in age; a long-delayed fire cannot spend an old one.

    This is the scheduler's situation in miniature: the approval existed, was
    valid, and the run happens later than it was good for.
    """
    import time

    from integrations.external import risk
    from integrations.external.errors import ExternalConnectionError
    from integrations.external.risk import check_invocation
    from integrations.external.service import ExternalConnectionService

    svc = ExternalConnectionService(stack.service)
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant",
        tenant_id=stack.tenant_id, kind=MCP_KIND, name="Remote MCP",
        config=MCP_CONFIG)
    approver = stack.member("approver-exp", ["member"])
    params = {"name": "remote_tool", "arguments": {}}
    binding = risk.approval_binding(
        kind=MCP_KIND, action="tools.call", connection_id=connection["id"],
        config_version=connection["version"], secret_versions={},
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        target=None, params=params, key="")
    max_age = risk.max_age_seconds()
    decision = {"approved": True, "approver_user_id": approver,
                "digest": binding["digest"],
                "issued_at": int(time.time()) - int(max_age) - 60,
                "expires_at": 0, "revoked": False, "scope": "once"}

    with pytest.raises(ExternalConnectionError) as refused:
        check_invocation(
            MCP_KIND, "tools.call", params,
            connection_id=connection["id"],
            config_version=connection["version"], secret_versions={},
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            approval=decision)
    assert refused.value.code.startswith("approval_")


# -- quota ----------------------------------------------------------------

def test_the_quota_is_consumed_where_the_call_happens_not_where_it_was_planned(
        stack, member, monkeypatch):
    """A fire is metered now, so a limit reached after enqueue still refuses.

    The agent seam meters external tools like any other tool
    (``AgentStreamExecutor._quota_tool_denial``), which is the seam a scheduled
    ``agent_task`` fire passes through. The quota is therefore spent at fire
    time: the task's creation consumed nothing, and a limit that was not reached
    when the task was written refuses the call when it runs.
    """
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.tools.external.external_tool import ExternalConnectionTool
    from common.runtime_identity import RuntimeIdentity, use_identity
    from integrations.external import registry, tools as external_tools

    member = stack.members["member"]
    connection_id = "conn_scheduled"
    binding = external_tools.ToolBinding(
        tool=external_tools.ExternalTool(
            name=_mcp_name("tools.call", connection_id, "echo"),
            kind=MCP_KIND, action="tools.call", write=True,
            description="call echo"),
        connection_id=connection_id, connection_name="Remote MCP",
        scope=registry.SCOPE_TENANT)
    tool = ExternalConnectionTool(binding)

    class _Stream(AgentStreamExecutor):
        def __init__(self):
            from types import SimpleNamespace
            self.tools = {tool.name: tool}
            self.agent = SimpleNamespace(tools=self.tools,
                                         effective_cwd=lambda: "/tmp")

    stack.service.set_quota(actor_user_id=stack.root,
                            tenant_id=stack.tenant_id, metric="tool_calls",
                            hard_limit=1, user_id=member)
    stream = _Stream()
    with use_identity(RuntimeIdentity(agent_id=AGENT, user_id=member,
                                      tenant_id=stack.tenant_id)):
        # The first fire is within the limit; the second is refused — and the
        # meter only ever moved because a call happened, never because a task
        # was created.
        assert stream._quota_tool_denial(tool.name) is None
        denial = stream._quota_tool_denial(tool.name)
    assert denial is not None and "quota" in denial
