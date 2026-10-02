"""Two independently keyed deployments use the actual IAM and master relay.

The transport relay is in process; model execution is stubbed. These tests do
not claim a hosted LinkAI relay or a production remote model was exercised.
"""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace
import uuid

import pytest
from Crypto.Signature import eddsa

from auth import peer_identity as peer
from common.runtime_identity import RuntimeIdentity, current_identity, use_identity
from tests._helpers import build_identity


@pytest.fixture
def deployments(tmp_path, monkeypatch):
    from agent.registry import AgentRegistry
    side = ContextVar('peer_test_deployment', default='a')
    nodes = {}
    for name in ('a', 'b'):
        aid = 'agent-' + name
        stack = build_identity(tmp_path / name, agents=(aid,), tenant_code='site-' + name)
        stack.agent_role('peer-member', [aid])
        uid = stack.member('alice', ['peer-member'])
        seed = bytes.fromhex(('11' if name == 'a' else '22') * 32)
        monkeypatch.setenv('TEST_PEER_KEY_' + name, seed.hex())
        public = eddsa.import_private_key(seed).public_key().export_key(format='raw').hex()
        settings = {'identity_mode': 'database', 'agent_delegation': {}, 'peer_identity': {
            'deployment_id': name, 'signing_key_env': 'TEST_PEER_KEY_' + name,
            'peers': {}, 'trusted_deployments': {},
        }}
        registry = AgentRegistry.from_config({'default_agent_id': aid, 'agent_workspace': stack.shared_root,
            'agents': [{'id': aid, 'name': aid, 'workspace': stack.shared_root, 'enabled': True}]})
        nodes[name] = SimpleNamespace(stack=stack, uid=uid, settings=settings, registry=registry, public=public,
            identity=RuntimeIdentity(user_id=uid, tenant_id=stack.tenant_id, agent_id=aid))
    for name, other in (('a', 'b'), ('b', 'a')):
        local, remote = nodes[name], nodes[other]
        local.settings['peer_identity']['trusted_deployments'][other] = {
            'public_key': remote.public, 'tenants': {remote.stack.tenant_id: local.stack.tenant_id}}
        local.settings['peer_identity']['peers']['agent-' + other] = {
            'deployment_id': other, 'tenants': {local.stack.tenant_id: remote.stack.tenant_id},
            'source_agents': ['agent-' + name],
        }
        local.stack.service.bind_external_identity(actor_user_id=local.stack.root, user_id=local.uid,
            provider='peer', issuer=other, subject=peer.binding_subject(remote.stack.tenant_id, remote.uid))
    monkeypatch.setattr('config.conf', lambda: nodes[side.get()].settings)
    monkeypatch.setattr('auth.service.get_identity_service', lambda: nodes[side.get()].stack.service)
    monkeypatch.setattr('agent.registry.get_agent_registry', lambda: nodes[side.get()].registry)
    @contextmanager
    def at(name, identity=None):
        token = side.set(name)
        try:
            with use_identity(identity or nodes[name].identity):
                yield nodes[name]
        finally:
            side.reset(token)
    return nodes, at


def request(mode='delegate'):
    return {'request_id': uuid.uuid4().hex, 'source_agent_id': 'agent-a', 'source_name': 'A',
            'target_agent_id': 'agent-b', 'task': 'Read the synthetic marker',
            'root_session_id': 'session_same_name', 'trace': ['agent-a', 'agent-b'],
            'depth': 1, 'members': [], 'peers': [], 'timeout': 5, 'mode': mode}


def signed(deployments, mode='delegate'):
    nodes, at = deployments
    payload = request(mode)
    with at('a'):
        outbound = peer.sign_request(payload)
    return payload, outbound


def test_cloud_transport_runs_master_delegate_and_verifies_return_identity(deployments):
    from common.cloud_client import CloudClient, _make_peer_transport
    from agent.multiagent import InvokeRequest
    from bridge.reply import Reply, ReplyType
    nodes, at = deployments
    calls, events = [], []
    class Bridge:
        agent_registry = nodes['b'].registry
        def agent_reply(self, prompt, *, context, on_event):
            calls.append((current_identity(), context, prompt))
            on_event({'type': 'message_update', 'data': {'delta': 'synthetic peer reply'}})
            return Reply(ReplyType.TEXT, 'synthetic peer reply')
    receiver = CloudClient.__new__(CloudClient)
    receiver._agent_bridge = lambda: Bridge()
    def relay(body):
        def reply(chunk):
            with at('a'):
                action = 'agent_invoke_event' if chunk['chunk_type'] == 'event' else 'agent_invoke_result'
                assert transport.handle_message({'action': action, 'data': chunk})
        with at('b'):
            receiver.on_chat({'action': 'agent_invoke', 'payload': copy.deepcopy(body)}, reply)
    transport = _make_peer_transport(relay)
    transport.register_peers([{'id': 'agent-b', 'name': 'B'}])
    with at('a'):
        assert transport.get_peer('agent-b') is not None
        result = transport.invoke(InvokeRequest(request_id='roundtrip', target_id='agent-b',
            task='synthetic task', source_id='agent-a', source_name='A', root_session_id='session_same_name',
            trace=('agent-a', 'agent-b'), depth=1, timeout_seconds=5),
            on_event=lambda event: events.append((current_identity(), event)))
    assert result.ok, result.error
    assert result.content == 'synthetic peer reply'
    assert calls[0][0].user_id == nodes['b'].uid
    assert calls[0][0].tenant_id == nodes['b'].stack.tenant_id
    assert calls[0][1]['delegation_root_session'].startswith('peer_')
    assert events[0][0] == nodes['a'].identity
    assert events[0][1]['data']['delta'] == 'synthetic peer reply'


@pytest.mark.parametrize('field,value', [('task', 'tampered'), ('mode', 'clear'),
    ('target_agent_id', 'other'), ('history', [{'role': 'user', 'text': 'injected'}]),
    ('root_session_id', 'someone_else'), ('members', ['private-agent'])])
def test_signed_content_cannot_be_changed_by_relay(deployments, field, value):
    _, at = deployments
    payload, _ = signed(deployments)
    payload[field] = value
    with at('b'), pytest.raises(peer.PeerIdentityError):
        peer.verify_request(payload)


@pytest.mark.parametrize('claim,value', [('target_tenant', 'other'), ('subject', 'admin'),
    ('audience', 'other'), ('issuer', 'other'), ('expires_at', 99999999999)])
def test_unsigned_identity_changes_never_grant_authority(deployments, claim, value):
    _, at = deployments
    payload, _ = signed(deployments)
    payload['peer_identity']['claims'][claim] = value
    with at('b'), pytest.raises(peer.PeerIdentityError):
        peer.verify_request(payload)


@pytest.mark.parametrize('mode', ['delegate', 'speak', 'clear'])
def test_all_modes_bind_member_and_claim_nonce_once(deployments, monkeypatch, mode):
    nodes, at = deployments
    calls, chunks = [], []
    payload, outbound = signed(deployments, mode)
    def business(scoped, bridge, send):
        calls.append((scoped, current_identity()))
        send({'chunk_type': 'result', 'request_id': scoped['request_id'], 'status': 'done', 'content': mode})
    monkeypatch.setattr('agent.multiagent.inbound.serve_invoke', business)
    with at('b'):
        peer.serve_authenticated(payload, lambda: object(), chunks.append)
        peer.serve_authenticated(payload, lambda: pytest.fail('replay ran'), chunks.append)
    assert len(calls) == 1
    assert calls[0][0]['mode'] == mode
    assert calls[0][1].user_id == nodes['b'].uid
    assert chunks[1]['error'] == 'peer_request_replayed'
    with at('a'):
        peer.verify_response(outbound, chunks[0], 'result')
        with pytest.raises(peer.PeerIdentityError):
            peer.verify_response(outbound, chunks[0], 'result')
    rows = nodes['b'].stack.service._store.execute('SELECT * FROM peer_identity_nonces')
    assert len(rows) == 1 and rows[0]['user_id'] == nodes['b'].uid


@pytest.mark.parametrize('revocation', ['binding', 'member', 'agent', 'tenant_mapping'])
def test_receiver_revalidates_current_local_authority(deployments, revocation):
    nodes, at = deployments
    payload, _ = signed(deployments)
    receiver = nodes['b']
    if revocation == 'binding':
        receiver.stack.service._store.execute("DELETE FROM external_identities WHERE provider='peer'")
    elif revocation == 'member':
        receiver.stack.service._store.execute('UPDATE memberships SET active=0 WHERE user_id=?', (receiver.uid,))
    elif revocation == 'agent':
        receiver.stack.service._store.execute('UPDATE agent_bindings SET private_owner_user_id=? WHERE agent_id=?',
                                              (receiver.stack.root, 'agent-b'))
    else:
        receiver.settings['peer_identity']['trusted_deployments']['a']['tenants'].clear()
    chunks = []
    with at('b'):
        peer.serve_authenticated(payload, lambda: pytest.fail('unauthorized request executed'), chunks.append)
    assert chunks[0]['status'] == 'failed'
    assert not receiver.stack.service._store.execute('SELECT * FROM peer_identity_nonces')


def test_expired_request_and_wrong_tenant_route_are_rejected(deployments, monkeypatch):
    nodes, at = deployments
    payload, _ = signed(deployments)
    now = payload['peer_identity']['claims']['expires_at'] + 1
    with at('b'), monkeypatch.context() as m:
        m.setattr(peer.time, 'time', lambda: now)
        with pytest.raises(peer.PeerIdentityError): peer.verify_request(payload)
    with at('a', nodes['a'].identity.derive(tenant_id=nodes['b'].stack.tenant_id)):
        with pytest.raises(Exception): peer.sign_request(request())


def test_result_signature_and_source_logout_are_checked(deployments):
    nodes, at = deployments
    source = nodes['a']
    login = source.stack.service.login('alice', source.stack.MEMBER_PASSWORD)
    row = source.stack.service.verify_session(login.token)['session']
    identity = source.identity.derive(web_auth_session_id=row['id'])
    payload = request()
    with at('a', identity): outbound = peer.sign_request(payload)
    with at('b'):
        claims = peer.verify_request(payload)
        chunk = peer.sign_response(claims, {'chunk_type': 'result', 'request_id': payload['request_id'],
                                           'status': 'done', 'content': 'scoped answer'}, 0)
    altered = copy.deepcopy(chunk)
    altered['content'] = 'forged answer'
    with at('a'), pytest.raises(peer.PeerIdentityError): peer.verify_response(outbound, altered, 'result')
    source.stack.service.revoke_session(login.token)
    with at('a'), pytest.raises(peer.PeerIdentityError): peer.verify_response(outbound, chunk, 'result')
    with at('a', identity), pytest.raises(peer.PeerIdentityError): peer.sign_request(request())


def test_speak_then_clear_touches_only_the_mapped_owners_real_transcript(deployments, monkeypatch):
    from agent.memory import get_conversation_store
    from agent.chat.session_service import SessionService
    from agent.multiagent.inbound import _speak_session_id
    nodes, at = deployments
    node = nodes['b']
    bridge = SimpleNamespace(agent_registry=node.registry)
    sessions, chunks = [], []
    def model_turn(self, *, session_id, send_chunk_fn, **kwargs):
        sessions.append(session_id)
        store = get_conversation_store(node.stack.shared_root)
        store.append_messages(session_id, [{'role': 'user', 'content': 'synthetic question'},
                                          {'role': 'assistant', 'content': 'synthetic answer'}], channel_type='agent')
        send_chunk_fn({'chunk_type': 'content', 'delta': 'synthetic answer'})
    monkeypatch.setattr('agent.chat.service.ChatService.run', model_turn)
    monkeypatch.setattr(SessionService, '_remove_agent', lambda *a, **k: None)
    speech, _ = signed(deployments, 'speak')
    clear, _ = signed(deployments, 'clear')
    with at('b'):
        peer.serve_authenticated(speech, lambda: bridge, chunks.append)
        assert chunks[-1]['status'] == 'done', chunks
        store = get_conversation_store(node.stack.shared_root)
        assert store.get_context_start_seq(sessions[0]) == 0
        # Another local conversation is neither guessed nor inherited.
        local_sid = _speak_session_id(speech['root_session_id'])
        store.append_messages(local_sid, [{'role': 'user', 'content': 'local private turn'}], channel_type='web')
        peer.serve_authenticated(clear, lambda: bridge, chunks.append)
        assert chunks[-1]['status'] == 'done', chunks
        assert store.get_context_start_seq(sessions[0]) == 2
        assert store.get_context_start_seq(local_sid) == 0
    with at('b', node.identity.derive(user_id=node.stack.root)):
        assert store.get_context_start_seq(sessions[0]) == 0


def test_nonce_survives_restart_and_parallel_replay_cannot_execute_twice(deployments, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from auth.service import IdentityService
    nodes, at = deployments
    payload, _ = signed(deployments)
    calls = []
    def business(scoped, bridge, send):
        calls.append(scoped['request_id'])
        send({'chunk_type': 'result', 'request_id': scoped['request_id'], 'status': 'done'})
    monkeypatch.setattr('agent.multiagent.inbound.serve_invoke', business)
    def receive():
        chunks = []
        with at('b'): peer.serve_authenticated(payload, lambda: object(), chunks.append)
        return chunks[-1]
    with ThreadPoolExecutor(max_workers=2) as executor:
        replies = list(executor.map(lambda _: receive(), range(2)))
    assert sorted(r['status'] for r in replies) == ['done', 'failed']
    assert len(calls) == 1
    nodes['b'].stack.service = IdentityService(nodes['b'].stack.service._store.db_path)
    assert receive()['error'] == 'peer_request_replayed'
    assert len(calls) == 1
