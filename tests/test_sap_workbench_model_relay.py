"""Scene model relay against loopback provider and real temporary IAM quota."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer

from Scene.sap_workbench.backend.model_relay import relay, validate_request
from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime
from tests.test_sap_workbench import app, API, payload
from tests.test_sap_workbench_access import setup_scene


def body(**changes):
    return {'model':'test-model','messages':[{'role':'user','content':'read test page'}], **changes}


@pytest.mark.parametrize('changes', [
    {'messages':None}, {'messages':[]}, {'messages':[1]}, {'messages':[{'content':[None]}]},
    {'messages':[{'content':[{'type':'image_url','image_url':'no'}]}]},
    {'max_tokens':-1}, {'max_tokens':'100'}, {'max_tokens':True}, {'max_completion_tokens':None},
    {'n':2}, {'n':True},
])
def test_malformed_requests_are_rejected_before_consumption(changes):
    with pytest.raises(web.HTTPBadRequest): validate_request(body(**changes), 'test-model')


def test_output_limit_is_capped_without_losing_tool_call_messages():
    request = body(max_completion_tokens=50000)
    request['messages'].append({'role':'assistant','content':None,'tool_calls':[]})
    result = validate_request(request,'test-model')
    assert result['max_tokens'] == 4096 and 'max_completion_tokens' not in result


@pytest.mark.parametrize('revocation', ['config', 'logout'])
def test_model_revocation_during_body_parse_prevents_quota_and_provider(app, monkeypatch, revocation):
    store, rows, config = setup_scene(app)
    runtime = WorkbenchRuntime(SimpleNamespace(),store,rows['alice'],app.login('alice'),'http://localhost:9899')
    runtime.model = {'model':'test-model','url':'https://model.invalid/chat/completions','key':'fixture-provider-key'}
    runtime.client = SimpleNamespace(post=AsyncMock())
    monkeypatch.setattr('auth.service.get_identity_service',lambda:app.service)
    consume = Mock(wraps=app.service.consume_quota)
    monkeypatch.setattr(app.service, 'consume_quota', consume)
    async def parse():
        if revocation == 'config':
            payload(app,app.put(API+'/config',{'version':1,'config':config},token=app.login('root')))
        else:
            app.service.revoke_session(runtime.token)
        return body()
    async def run():
        with pytest.raises(WorkbenchError, match='config_conflict' if revocation == 'config' else 'platform_login_required'):
            await relay(runtime,SimpleNamespace(json=parse))
    asyncio.run(run())
    consume.assert_not_called()
    runtime.client.post.assert_not_awaited()
    assert 'model.reserve' not in str(app.service.list_audit(app.tenant_id))


@pytest.mark.parametrize('failure', ['audit', 'config', 'logout', 'late_audit'])
def test_model_pre_dispatch_failure_refunds_only_its_own_bucket_and_preserves_reference(app, monkeypatch, failure):
    store, rows, config = setup_scene(app)
    runtime = WorkbenchRuntime(SimpleNamespace(),store,rows['alice'],app.login('alice'),'http://localhost:9899')
    runtime.model = {'model':'test-model','url':'https://model.invalid/chat/completions','key':'fixture-provider-key'}
    runtime.client = SimpleNamespace(post=AsyncMock())
    monkeypatch.setattr('auth.service.get_identity_service',lambda:app.service)
    tick = [1791028800]
    monkeypatch.setattr('calendar.timegm',lambda _:tick[0])
    app.service.set_quota(actor_user_id=app.admin_id,tenant_id=app.tenant_id,metric='tokens',hard_limit=20000)
    refund = Mock(wraps=app.service.refund_quota)
    monkeypatch.setattr(app.service, 'refund_quota', refund)
    original_audit = runtime.audit
    reservations = []
    def audit(action, details):
        assert action == 'model.reserve'
        reservations.append(details)
        if failure in {'audit','late_audit'}:
            if failure == 'late_audit': tick[0] += 1
            raise RuntimeError('synthetic-private-audit-details')
        original_audit(action, details)
        if failure == 'config':
            payload(app,app.put(API+'/config',{'version':1,'config':config},token=app.login('root')))
        else:
            app.service.revoke_session(runtime.token)
    runtime.audit = audit
    async def run():
        expected = 'audit_unavailable' if failure in {'audit','late_audit'} else 'config_conflict' if failure == 'config' else 'platform_login_required'
        with pytest.raises(WorkbenchError, match=expected) as error:
            await relay(runtime,SimpleNamespace(json=AsyncMock(return_value=body())))
        assert 'synthetic-private-audit-details' not in str(error.value)
    asyncio.run(run())
    runtime.client.post.assert_not_awaited()
    assert len(reservations) == 1
    if failure == 'late_audit': refund.assert_not_called()
    else:
        refund.assert_called_once()
        assert refund.call_args.kwargs['reference'] == reservations[0]['reference']
        assert refund.call_args.kwargs['amount'] == reservations[0]['tokens']
    usage = app.service.quota_status(actor_user_id=app.admin_id,tenant_id=app.tenant_id)['usage']
    used = sum(item['used'] for item in usage if item['user_id']=='' and item['metric']=='tokens')
    assert used == (reservations[0]['tokens'] if failure == 'late_audit' else 0)
    audits = str(app.service.list_audit(app.tenant_id))
    assert 'fixture-provider-key' not in audits and 'synthetic-private-audit-details' not in audits


@pytest.mark.parametrize('case', ['json', 'stream', 'unknown_usage', 'invalid_usage', 'late_usage', 'denied', 'revoked', 'provider_failure'])
def test_model_meter_and_lifecycle_use_real_identity_without_external_requests(app, monkeypatch, case):
    store, rows, _ = setup_scene(app)
    runtime = WorkbenchRuntime(SimpleNamespace(),store,rows['alice'],app.login('alice'),'http://localhost:9899')
    # Fix just the shared meter's clock so both reserve and refund address the
    # same bucket, except for the explicit cross-window regression below.
    tick = [1791028800]
    monkeypatch.setattr('calendar.timegm',lambda _:tick[0])
    limit = 1 if case == 'denied' else 20000
    app.service.set_quota(actor_user_id=app.admin_id,tenant_id=app.tenant_id,metric='tokens',hard_limit=limit)
    received = []

    async def run():
        async def provider(request):
            received.append(await request.json())
            assert request.headers['Authorization'] == 'Bearer fixture-provider-key'
            if case == 'provider_failure': return web.json_response({'error':'private provider details'},status=500)
            if case == 'late_usage': tick[0] += 1
            usage = None if case == 'unknown_usage' else {'total_tokens':True if case == 'invalid_usage' else 23}
            value = {'choices':[], 'usage':usage}
            if case == 'stream':
                response = web.StreamResponse(headers={'Content-Type':'text/event-stream'})
                await response.prepare(request)
                wire = ('data: '+json.dumps(value)+'\n\ndata: [DONE]\n\n').encode()
                await response.write(wire[:14]); await response.write(wire[14:]); await response.write_eof()
                return response
            return web.Response(text=json.dumps(value,indent=2),content_type='application/json')
        provider_app = web.Application(); provider_app.router.add_post('/chat/completions',provider)
        async def run_relay(request): return await relay(runtime,request)
        gateway_app = web.Application(middlewares=[runtime.boundary])
        gateway_app.router.add_post('/model/chat/completions',run_relay)
        async with TestServer(provider_app) as upstream, TestServer(gateway_app) as gateway, ClientSession() as client:
            runtime.client = client
            runtime.model = {'model':'test-model','url':str(upstream.make_url('/chat/completions')),'key':'fixture-provider-key'}
            if case == 'revoked': app.service.revoke_session(runtime.token)
            response = await client.post(gateway.make_url('/model/chat/completions'),json=body(max_tokens=99999),
                                         headers={'Authorization':'Bearer '+runtime.secret})
            output = await response.text()
            expected = 429 if case == 'denied' else 401 if case == 'revoked' else 502 if case == 'provider_failure' else 200
            assert response.status == expected
            assert 'fixture-provider-key' not in output and 'private provider details' not in output
    asyncio.run(run())
    rows = app.service.quota_status(actor_user_id=app.admin_id,tenant_id=app.tenant_id)['usage']
    used = sum(item['used'] for item in rows if item['user_id']=='' and item['metric']=='tokens')
    if case in {'denied','revoked'}:
        assert received == [] and used == 0
    else:
        assert len(received)==1 and received[0]['max_tokens']==4096
        if case in {'json','stream'}: assert used == 23
        else: assert used > 4096
    audits = str(app.service.list_audit(app.tenant_id))
    assert 'read test page' not in audits and 'fixture-provider-key' not in audits
