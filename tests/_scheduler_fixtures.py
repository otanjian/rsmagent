"""Ready tenant members for upstream scheduler tests running in the IAM fork."""
import pytest
from common.runtime_identity import RuntimeIdentity, use_identity
from tests._helpers import WebAppHarness


@pytest.fixture
def scheduler_member(tmp_path):
    app = WebAppHarness(tmp_path / 'scheduler-identity')
    app.add_agent('primary')
    role = app.role('scheduler-member', ['chat.use', 'agent.use', 'agent.read'],
                    grants=[('agent', 'agent:primary', 'use'), ('agent', 'agent:primary', 'read')])
    user = app.member('scheduler-member', [role['code']])
    try:
        with use_identity(RuntimeIdentity(user_id=user, tenant_id=app.tenant_id,
                                          agent_id='primary', session_id='owned-session')):
            yield app
    finally:
        app.close()
