"""Real fork HTTP contracts for the October upstream sync; isolated identities/files."""
import json
from pathlib import Path

import pytest

from common.runtime_identity import identity_scope


def setup_members(web_app):
    app = web_app('sync')
    app.add_agent('primary')
    role = app.role('reader', ['chat.use', 'history.read', 'agent.read', 'agent.use'],
                    grants=[('agent', 'agent:primary', 'read'), ('agent', 'agent:primary', 'use')])
    ids = {name: app.member(name, [role['code']]) for name in ('alice', 'bob')}
    tokens = {name: app.login(name) for name in ids}
    return app, ids, tokens


def menu(title):
    return {'version': 1, 'groups': [{'id': 'mine', 'title': title, 'items': [
        {'id': 'chat', 'type': 'builtin', 'view': 'chat'},
        {'id': 'future', 'type': 'builtin', 'view': 'future-view'},
    ]}]}


def test_menu_is_personal_preserves_unknown_and_rejects_cross_origin(web_app):
    app, ids, tokens = setup_members(web_app)
    saved = app.post('/api/menu', {'menu': menu('Alice')}, token=tokens['alice'])
    assert saved.status == '200 OK', saved.data
    body = app.json(saved)
    assert body['menu']['groups'][0]['title'] == 'Alice'
    unknown = next(i for i in body['menu']['groups'][0]['items'] if i['id'] == 'future')
    assert unknown['view'] == 'future-view' and unknown['available'] is False
    assert app.json(app.get('/api/menu', token=tokens['bob']))['menu'] is None
    rejected = app.post('/api/menu', {'menu': menu('Attack')}, token=tokens['alice'], headers={'Origin': 'https://attacker.invalid'})
    assert rejected.status.startswith('403')
    assert app.json(app.get('/api/menu', token=tokens['alice']))['menu']['groups'][0]['title'] == 'Alice'


def test_menu_atomic_failure_keeps_saved_value_and_reset_is_private(web_app, monkeypatch):
    app, ids, tokens = setup_members(web_app)
    assert app.post('/api/menu', {'menu': menu('Before')}, token=tokens['alice']).status == '200 OK'
    from channel.web.fork.handlers import menu as handler
    with monkeypatch.context() as patch:
        patch.setattr(handler, 'write_json_atomic', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
        assert app.post('/api/menu', {'menu': menu('After')}, token=tokens['alice']).status.startswith('500')
    assert app.json(app.get('/api/menu', token=tokens['alice']))['menu']['groups'][0]['title'] == 'Before'
    assert app.post('/api/menu', {'menu': None}, token=tokens['bob']).status == '200 OK'
    assert app.json(app.get('/api/menu', token=tokens['alice']))['menu'] is not None


def owned_store(app, user_id):
    from agent.registry import get_agent_registry
    from agent.memory import get_conversation_store
    profile = get_agent_registry().get('primary')
    Path(profile.workspace).mkdir(parents=True, exist_ok=True)
    return get_conversation_store(profile.workspace), profile


def test_artifact_index_scopes_before_pagination_and_mutations(web_app):
    app, ids, tokens = setup_members(web_app)
    store, profile = owned_store(app, ids['alice'])
    path = Path(profile.workspace) / 'report.html'
    path.write_text('<h1>Report</h1>')
    for name in ('alice', 'bob'):
        with identity_scope(user_id=ids[name], tenant_id=app.tenant_id, agent_id='primary'):
            store.append_messages('web:' + name, [{'role':'user', 'content':name}], channel_type='web')
            assert store.record_artifacts('web:' + name, [{'path':str(path), 'kind':'html', 'source':'write'}]) == 1
    rows = {}
    for name in ids:
        response = app.get('/api/artifacts?scope=all&limit=1', token=tokens[name])
        assert response.status == '200 OK', response.data
        rows[name] = app.json(response)['items']
        assert len(rows[name]) == 1
    assert rows['alice'][0]['id'] != rows['bob'][0]['id']
    bad = app.post('/api/artifacts/rename', {'agent_id':'primary','id':rows['bob'][0]['id'],'title':'hijack'}, token=tokens['alice'])
    assert bad.status.startswith('404'), bad.data
    good = app.post('/api/artifacts/rename', {'agent_id':'primary','id':rows['alice'][0]['id'],'title':'My report'}, token=tokens['alice'])
    assert good.status == '200 OK', good.data
    assert path.read_text() == '<h1>Report</h1>'


def test_artifact_failure_retries_committed_messages_once(web_app, monkeypatch):
    app, ids, tokens = setup_members(web_app)
    store, profile = owned_store(app, ids['alice'])
    from agent.workspace.personal_default import personal_default_dir
    with identity_scope(user_id=ids['alice'], tenant_id=app.tenant_id, agent_id='primary'):
        target = Path(personal_default_dir('primary', ensure=True)) / 'report.md'
    target.write_text('body')
    messages = [{'role':'user','content':'create report'},
                {'role':'assistant','content':[{'type':'tool_use','id':'write-1','name':'write','input':{'path':str(target)}}]}]
    with identity_scope(user_id=ids['alice'], tenant_id=app.tenant_id, agent_id='primary'):
        store.append_messages('web:retry', messages, channel_type='web')
        with monkeypatch.context() as patch:
            patch.setattr(store, 'record_artifacts', lambda *a, **k: (_ for _ in ()).throw(OSError('index unavailable')))
            with pytest.raises(OSError):
                store.retry_artifact_index('web:retry')
        assert store.load_messages('web:retry') == messages
        assert store.retry_artifact_index('web:retry') == 1
        assert len(store.list_artifacts()['items']) == 1
        assert store.retry_artifact_index('web:retry') == 0
        assert store.load_messages('web:retry') == messages


def test_device_history_never_indexes_server_same_named_file(tmp_path):
    from agent.protocol.artifact import collect_message_artifacts
    target = tmp_path / 'report.md'
    target.write_text('server secret')
    messages = [{'role':'assistant','extras':{'origin':{'source':'desktop','device_id':'d'}},
                 'content':[{'type':'tool_use','id':'w','name':'write','input':{'path':str(target)}}]}]
    assert collect_message_artifacts(messages, str(tmp_path)) == []


def test_relative_artifacts_resolve_each_owners_actual_tool_directory(web_app):
    from agent.workspace.personal_default import personal_default_dir
    app, ids, tokens = setup_members(web_app)
    store, profile = owned_store(app, ids['alice'])
    # A same-named shared file must not mask the caller's generated file.
    (Path(profile.workspace) / 'report.md').write_text('shared decoy')
    messages = [{'role': 'user', 'content': 'create report'},
                {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'id': 'w', 'name': 'write',
                     'input': {'path': 'report.md'}}]},
                {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'w',
                     'content': json.dumps({'path': 'report.md'})}]}]
    for name in ids:
        with identity_scope(user_id=ids[name], tenant_id=app.tenant_id, agent_id='primary'):
            directory = personal_default_dir('primary', ensure=True)
            target = Path(directory) / 'report.md'
            target.write_text(name)
            store.append_messages('web:' + name, messages, channel_type='web')
            assert store.retry_artifact_index('web:' + name) == 1
            items = store.list_artifacts()['items']
            assert [item['path'] for item in items] == [str(target)]
            assert store.retry_artifact_index('web:' + name) == 0
    for name in ids:
        response = app.get('/api/artifacts?scope=all', token=tokens[name])
        assert response.status == '200 OK', response.data
        items = app.json(response)['items']
        assert len(items) == 1
        assert ids[name] in items[0]['path']


def test_api_key_management_uses_existing_iam_and_one_shot_secret(web_app, monkeypatch):
    monkeypatch.setenv('COW_CREDENTIAL_MASTER_KEY', '01' * 32)
    app, ids, tokens = setup_members(web_app)
    app.member('admin', ['tenant_admin'])
    token = app.login('admin')
    denied = app.post('/api/external_api', {'action':'create'}, token=tokens['alice'])
    assert denied.status.startswith('403'), denied.data
    created = app.post('/api/external_api', {'action':'create', 'user_id':ids['alice']}, token=token)
    assert created.status == '200 OK', created.data
    issued = app.json(created)['issued']
    key = issued['api_key']
    assert key.startswith('sak_')
    listing = app.get('/api/external_api?user_id=' + ids['alice'], token=token)
    assert key.encode() not in listing.data
    assert app.service.authenticate_service_account_api_key(key)['user_id'] == ids['alice']
    rotated = app.post('/api/external_api', {'action':'rotate', 'user_id':ids['alice'], 'credential_id':issued['id']}, token=token)
    assert rotated.status == '200 OK', rotated.data
    from auth.service import IdentityServiceError
    with pytest.raises(IdentityServiceError):
        app.service.authenticate_service_account_api_key(key)
    next_key = app.json(rotated)['issued']['api_key']
    revoked = app.post('/api/external_api', {'action':'revoke','user_id':ids['alice'],'credential_id':issued['id']}, token=token)
    assert revoked.status == '200 OK', revoked.data
    with pytest.raises(IdentityServiceError):
        app.service.authenticate_service_account_api_key(next_key)
    assert app.post('/api/external_api', {'action':'disable'}, token=token).status.startswith('403')


def test_menu_keeps_existing_business_entries_and_denies_management(web_app):
    app, ids, tokens = setup_members(web_app)
    result = app.json(app.get('/api/menu', token=tokens['alice']))['available_views']
    assert result['chat'] and result['history'] and result['artifacts']
    assert not result['platform'] and not result['logs'] and not result['branding']


def test_artifact_all_scope_skips_foreign_agents_without_http_error(web_app):
    app, ids, tokens = setup_members(web_app)
    app.add_agent('ungranted')
    response = app.get('/api/artifacts?scope=all', token=tokens['alice'])
    assert response.status == '200 OK', response.data
    assert app.json(response)['items'] == []
    assert app.get('/api/artifacts?scope=ungranted', token=tokens['alice']).status.startswith('403')


def test_artifact_recovery_keeps_tool_use_before_persisted_result(web_app):
    app, ids, _ = setup_members(web_app)
    store, profile = owned_store(app, ids['alice'])
    from agent.workspace.personal_default import personal_default_dir
    with identity_scope(user_id=ids['alice'], tenant_id=app.tenant_id, agent_id='primary'):
        target = Path(personal_default_dir('primary', ensure=True)) / 'from-bash.md'
    target.write_text('generated')
    with identity_scope(user_id=ids['alice'], tenant_id=app.tenant_id, agent_id='primary'):
        # A command's file report arrives only in the later persisted tool result.
        store.append_messages('web:partial', [
            {'role':'user','content':'create a file'},
            {'role':'assistant','content':[{'type':'tool_use','id':'cmd','name':'bash','input':{'command':'generate'}}]},
        ], channel_type='web')
        assert store.retry_artifact_index('web:partial') == 1
        store.append_messages('web:partial', [
            {'role':'user','content':[{'type':'tool_result','tool_use_id':'cmd','content':json.dumps({'files_written':[str(target)]})}]},
            {'role':'assistant','content':'done'},
        ], channel_type='web')
        assert store.retry_artifact_index('web:partial') == 1
        assert len(store.load_messages('web:partial')) == 4
        assert [row['path'] for row in store.list_artifacts()['items']] == [str(target)]
        # The cursor must start at the human turn, not the tool result message.
        conn = store._connect()
        try:
            assert store._latest_visible_user_seq(conn, 'web:partial', before_seq=2) == 0
        finally:
            conn.close()


def test_real_service_key_directory_rotation_and_identity_outage(web_app, monkeypatch):
    monkeypatch.setenv('COW_CREDENTIAL_MASTER_KEY', '01' * 32)
    app, ids, tokens = setup_members(web_app)
    app.member('apiadmin', ['tenant_admin'])
    key = app.service.create_service_account_api_key(actor_user_id=app.admin_id,
        tenant_id=app.tenant_id, user_id=ids['alice'])
    headers={'Authorization':'Bearer ' + key['api_key']}
    response=app.get('/v1/models', headers=headers)
    assert response.status == '200 OK', response.data
    assert app.json(response)['data'] == []  # No model grants, no discovery leakage.
    anonymous=app.get('/v1/models')
    assert anonymous.status.startswith('401')
    app.service.revoke_service_account_api_key(actor_user_id=app.admin_id,
        tenant_id=app.tenant_id, credential_id=key['id'])
    assert app.get('/v1/models', headers=headers).status.startswith('401')
    import auth.service
    with monkeypatch.context() as patch:
        patch.setattr(auth.service, 'get_identity_service', lambda: (_ for _ in ()).throw(RuntimeError('offline')))
        outage=app.get('/v1/models', headers=headers)
        assert outage.status.startswith('503'), outage.data
        assert b'offline' not in outage.data


def test_api_default_model_respects_pins_roles_and_live_grants(web_app, monkeypatch):
    from types import SimpleNamespace
    from agent.workspace import session_prefs
    from channel.web import openai_api
    app, ids, tokens = setup_members(web_app)
    service = app.service
    monkeypatch.setattr('auth.service.get_identity_service', lambda: service)
    catalog = [{'id':'custom:approved', 'models':['approved']}, {'id':'deepseek', 'models':['fallback']}]
    monkeypatch.setattr(openai_api, '_model_catalog', lambda: catalog)
    monkeypatch.setattr(openai_api, 'conf', lambda: {'model':'fallback', 'bot_type':'deepseek'})
    profile = SimpleNamespace(model=None, bot_type=None)
    monkeypatch.setattr('agent.registry.get_agent_registry', lambda: SimpleNamespace(get_addressed=lambda _: profile))
    prefs = {}
    default = {'status':'default', 'model':'provider:custom:approved:approved'}
    monkeypatch.setattr(session_prefs, 'get_prefs', lambda *a: prefs)
    monkeypatch.setattr(service, 'model_defaults_for', lambda *a: default)
    with identity_scope(user_id=ids['alice'], tenant_id=app.tenant_id, agent_id='primary'):
        assert openai_api._authorized_default_model('primary','owned') == ('custom:approved','approved')
        prefs.update(model='fallback', provider='deepseek')
        assert openai_api._authorized_default_model('primary','owned') == ('deepseek','fallback')
        # An unauthorized pin is refused, never silently replaced by a role/global default.
        prefs['model'] = 'not-granted'
        with pytest.raises(openai_api.OpenAIAPIError) as denied:
            openai_api._authorized_default_model('primary','owned')
        assert denied.value.status_code == 403
        prefs.clear(); default.update(status='conflict', models=['one','two'])
        with pytest.raises(openai_api.OpenAIAPIError) as conflict:
            openai_api._authorized_default_model('primary','owned')
        assert conflict.value.status_code == 409
        monkeypatch.setattr(service, 'model_defaults_for', lambda *a: (_ for _ in ()).throw(OSError('offline')))
        with pytest.raises(openai_api.OpenAIAPIError) as unavailable:
            openai_api._authorized_default_model('primary','owned')
        assert unavailable.value.status_code == 503


def test_actual_chat_api_accepts_service_key_without_browser_tenant_header(web_app, monkeypatch):
    from channel.web import openai_api
    from common.runtime_identity import current_identity
    monkeypatch.setenv('COW_CREDENTIAL_MASTER_KEY', '01' * 32)
    app = web_app('api-entry', settings={'external_api_enabled': True})
    app.add_agent('primary')
    monkeypatch.setattr(openai_api, 'conf', app._conf)
    monkeypatch.setattr('channel.web.fork.runtime._session_model_catalog',
                        lambda: [{'id': 'deepseek', 'models': ['approved']}])
    tenant = app.service._store.execute('SELECT version FROM tenants WHERE id=?', (app.tenant_id,))[0]
    app.service.set_tenant_resource_grants(actor_user_id=app.admin_id, tenant_id=app.tenant_id,
        expected_version=tenant['version'], grants=[
            {'resource_kind': 'model', 'resource_id': 'provider:deepseek:approved', 'action': 'use'}])
    role = app.role('api-model', ['chat.use', 'agent.use', 'model.use'], grants=[
        ('agent', 'agent:primary', 'use'), ('model', 'provider:deepseek:approved', 'use')])
    user = app.member('api-client', [role['code']])
    key = app.service.create_service_account_api_key(actor_user_id=app.admin_id,
        tenant_id=app.tenant_id, user_id=user)
    calls = []
    def run(query, session_id, send_chunk, **kwargs):
        calls.append((current_identity(), kwargs))
        send_chunk({'chunk_type': 'content', 'delta': 'accepted'})
        return {'input_tokens': 1, 'output_tokens': 1}
    monkeypatch.setattr(openai_api, '_run_chat_service', run)
    from bridge.bridge import Bridge
    from agent.registry import get_agent_registry
    monkeypatch.setattr(Bridge().get_agent_bridge(), 'agent_registry', get_agent_registry())
    body = {'agent_id': 'primary', 'model': 'approved', 'messages': [{'role': 'user', 'content': 'hello'}]}
    def request(headers=None, payload=None):
        return app.app.request('/v1/chat/completions', method='POST',
            headers=headers or {}, data=json.dumps(payload or body))
    headers = {'Authorization': 'Bearer ' + key['api_key']}
    assert request().status.startswith('401')
    response = request(headers)
    assert response.status == '200 OK', response.data
    assert app.json(response)['choices'][0]['message']['content'] == 'accepted'
    assert (calls[0][0].user_id, calls[0][0].tenant_id) == (user, app.tenant_id)
    assert calls[0][1]['model_override'] == ('deepseek', 'approved')
    assert request({**headers, 'X-Tenant-ID': 'another'}).status.startswith('403')
    assert request(headers, {**body, 'model': 'ungranted'}).status.startswith('404')
    app.service.revoke_service_account_api_key(actor_user_id=app.admin_id,
        tenant_id=app.tenant_id, credential_id=key['id'])
    assert request(headers).status.startswith('401')
    assert len(calls) == 1


def test_runtime_model_authorization_service_failure_does_not_allow_provider(web_app, monkeypatch):
    from bridge.agent_bridge import AgentLLMModel
    app, ids, _ = setup_members(web_app)
    monkeypatch.setattr('auth.service.get_identity_service', lambda: app.service)
    monkeypatch.setattr(app.service, 'resource_ids_for', lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    with identity_scope(user_id=ids['alice'], tenant_id=app.tenant_id, agent_id='primary'):
        with pytest.raises(RuntimeError, match='authorization service is unavailable'):
            AgentLLMModel.__new__(AgentLLMModel)._require_model_use('model')


def test_fallback_cannot_reuse_a_same_named_model_grant_from_another_provider(monkeypatch):
    from bridge.agent_bridge import AgentLLMModel
    model = AgentLLMModel.__new__(AgentLLMModel)
    monkeypatch.setattr(model, '_authorized_model_ids', lambda: {'provider:custom:primary:shared-model'})
    model.set_agent_default('custom:primary', 'shared-model')
    assert model._model_use_denial('shared-model') is None
    model._fallback_provider = 'custom:backup'
    model._fallback_model = 'shared-model'
    with pytest.raises(RuntimeError, match='not authorized'):
        model._require_model_use('shared-model')
    monkeypatch.setattr(model, '_authorized_model_ids', lambda: {'provider:custom:backup:shared-model'})
    assert model._model_use_denial('shared-model') is None


@pytest.mark.parametrize('payload', [
    {'action':'set_provider', 'provider_id':'openai', 'api_key':'synthetic-new-key'},
    {'action':'set_capability', 'capability':'chat', 'provider_id':'deepseek', 'model':'deepseek-chat'},
    {'action':'set_search_credential', 'provider':'tavily', 'api_key':'synthetic-search-key'},
    {'action':'set_voice_reply_mode', 'mode':'always'},
    {'action':'set_custom_provider', 'name':'Synthetic', 'api_base':'https://example.invalid/v1',
     'api_key':'synthetic-custom-key', 'model':'synthetic-model'},
])
def test_model_save_failure_keeps_live_config_and_file(web_app, monkeypatch, payload):
    import copy
    from channel.web.web_channel import conf
    from channel.web.fork.handlers.models import ModelsHandler
    app = web_app('model-atomic')
    token = app.login('root')
    before = copy.deepcopy(dict(conf()))
    config_path = Path(ModelsHandler._config_path())
    original = config_path.read_bytes() if config_path.exists() else None
    monkeypatch.setattr(ModelsHandler, '_write_file_config', lambda *a: (_ for _ in ()).throw(OSError('synthetic disk full')))
    result = app.post('/api/models', payload, token=token)
    assert app.json(result)['status'] == 'error', result.data
    assert app.json(result)['message'] == 'synthetic disk full', result.data
    assert dict(conf()) == before
    assert (config_path.read_bytes() if config_path.exists() else None) == original
