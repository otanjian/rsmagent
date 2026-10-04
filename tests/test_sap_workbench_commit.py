"""Real IAM approval consumer and scene ledger; SAP callbacks are isolated."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from concurrent.futures import ThreadPoolExecutor

import pytest

from auth.service import IdentityServiceError
from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.commit import CommitConsumer, ACTION
from Scene.sap_workbench.backend.runtime import WorkbenchRuntime
from Scene.sap_workbench.backend.store import WorkbenchStore
from tests.test_sap_workbench import app, API, payload
from tests.test_sap_workbench_access import setup_scene


@pytest.fixture
def prepared(app, monkeypatch):
    store, old, config = setup_scene(app)
    role = next(row for row in app.service.list_roles(app.tenant_id) if row['code']=='sap-use')
    app.service.update_role(actor_user_id=app.admin_id,tenant_id=app.tenant_id,role_id=role['id'],name=role['name'],
        permissions=[*role['permissions'],'tool.execute'],expected_version=role['version'])
    config['commit_enabled']=True
    saved=payload(app,app.put(API+'/config',{'version':1,'config':config},token=app.login('root')))
    rows={name:store.reserve_session(app.tenant_id,app.user_id(name),'sap-coder','commit-unit-request',saved,'/shared-project')
          for name in ('alice','bob')}
    runtimes={name:WorkbenchRuntime(SimpleNamespace(),store,row,app.login(name),'http://localhost:9899') for name,row in rows.items()}
    monkeypatch.setattr('auth.service.get_identity_service',lambda:app.service)
    observation={'target_id':'fixture-owned-target','control_epoch':3,'page':{
        'revision':'page-fixture-1','title':'创建采购订单','login':False,'dialogs':[],
        'fields':[{'id':'vendor','label':'供应商','value':'VENDOR-UNIT-01'},
                  {'id':'amount','label':'金额','value':'100.00'}],'tables':[]}}
    return SimpleNamespace(app=app,store=store,runtimes=runtimes,config=config,observation=observation,
        consumer=CommitConsumer(runtimes['alice']),observe=AsyncMock(side_effect=lambda:deepcopy(observation)))


def approve(fixture, result):
    return fixture.app.service.decide_approval(actor_user_id=fixture.app.admin_id,tenant_id=fixture.app.tenant_id,
                                            approval_id=result['approval_id'],approve=True)


def verified(record, **changes):
    return {'outcome':'submitted','read_only':True,'matches':True,'document_number':'4500000123',
            'parameter_digest':record['parameter_digest'],**changes}


def receipt(**changes):
    return {'outcome':'submitted','document_number':'4500000123',**changes}


def test_commit_default_disabled_before_observation_or_approval(prepared):
    prepared.runtimes['alice'].config['commit_enabled']=False
    async def run():
        with pytest.raises(WorkbenchError,match='commit_disabled'):
            await prepared.consumer.prepare('commit-call',observe=prepared.observe)
    asyncio.run(run())
    prepared.observe.assert_not_awaited()
    assert prepared.app.service.list_approvals(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id)==[]


def test_truncated_page_is_never_eligible_for_approval(prepared):
    prepared.observation['page']['truncated']=True
    async def run():
        with pytest.raises(WorkbenchError,match='commit_page_unsupported'):
            await prepared.consumer.prepare('commit-call',observe=prepared.observe)
    asyncio.run(run())
    assert prepared.app.service.list_approvals(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id)==[]


def test_prepare_is_durable_idempotent_and_uses_real_reviewable_approval(prepared):
    async def run():
        one=await prepared.consumer.prepare('commit-call',observe=prepared.observe)
        two=await prepared.consumer.prepare('commit-call',observe=prepared.observe)
        assert one==two and one['state']=='prepared'
        rows=prepared.app.service.list_approvals(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id)
        assert len(rows)==1 and rows[0]['action']==ACTION and rows[0]['requester_user_id']==prepared.app.user_id('alice')
        review=json.loads(rows[0]['payload_json'])
        assert review['document']['fields'][1]['value']=='100.00' and review['_binding']['digest']
        record=prepared.store.commit_action(prepared.app.tenant_id,prepared.app.user_id('alice'),
                                           prepared.runtimes['alice'].id,one['action_id'])
        assert record['approval_ref']==one['approval_id'] and record['page_revision']=='page-fixture-1'
        assert record['control_epoch']==3 and record['config_version']==2
        assert 'VENDOR-UNIT-01' not in json.dumps(record)
        assert 'VENDOR-UNIT-01' not in str(prepared.app.service.list_audit(prepared.app.tenant_id))
        for _ in range(2): WorkbenchStore(prepared.store.path)
        assert await prepared.consumer.status(one['action_id'])==one
    asyncio.run(run())


@pytest.mark.parametrize('actor',['alice','bob'])
def test_public_approval_engine_rejects_self_or_unqualified_approver(prepared,actor):
    result=asyncio.run(prepared.consumer.prepare('commit-call',observe=prepared.observe))
    with pytest.raises(IdentityServiceError):
        prepared.app.service.decide_approval(actor_user_id=prepared.app.user_id(actor),tenant_id=prepared.app.tenant_id,
                                            approval_id=result['approval_id'],approve=True)
    assert prepared.app.service.list_approvals(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id)[0]['status']=='pending'


def test_approver_qualification_revoked_after_approval_prevents_consumption(prepared):
    app=prepared.app; svc=app.service
    def roles(values):
        row=svc.get_membership(app.user_id('bob'),app.tenant_id)
        return svc.update_member(actor_user_id=app.admin_id,tenant_id=app.tenant_id,member_id=row['id'],
            display_name='Bob',active=True,roles=values,department_id=None,position_text='',expected_version=row['version'])
    roles(['sap-use','tenant_admin'])
    result=asyncio.run(prepared.consumer.prepare('commit-call',observe=prepared.observe))
    svc.decide_approval(actor_user_id=app.user_id('bob'),tenant_id=app.tenant_id,approval_id=result['approval_id'],approve=True)
    roles(['sap-use'])
    async def run():
        dispatch=AsyncMock()
        with pytest.raises(WorkbenchError,match='approval_unverified'):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=AsyncMock())
        dispatch.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('state',['pending','denied','revoked','expired'])
def test_real_approval_states_refuse_without_dispatch(prepared,state):
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe)
        svc=prepared.app.service
        if state=='denied': svc.decide_approval(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id,
                                              approval_id=result['approval_id'],approve=False)
        if state in {'revoked','expired'}:
            approve(prepared,result)
            if state=='revoked': svc.revoke_approval(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id,
                                                     approval_id=result['approval_id'])
            else: svc._store.execute('UPDATE approvals SET expires_at=1 WHERE id=?',(result['approval_id'],))
        dispatch=AsyncMock(return_value=receipt())
        with pytest.raises(WorkbenchError,match='approval_'+state):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,
                                             verify=AsyncMock(side_effect=verified))
        dispatch.assert_not_awaited()
        row=prepared.store.commit_action(prepared.app.tenant_id,prepared.app.user_id('alice'),prepared.runtimes['alice'].id,result['action_id'])
        assert row['state']!='running'
    asyncio.run(run())


@pytest.mark.parametrize('change',['amount','revision','target','epoch','generation'])
def test_approval_is_bound_to_business_and_exact_page_lease(prepared,change):
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe); approve(prepared,result)
        if change=='amount': prepared.observation['page']['fields'][1]['value']='200.00'
        elif change=='revision': prepared.observation['page']['revision']='new-page'
        elif change=='target': prepared.observation['target_id']='other-owned-target'
        elif change=='epoch': prepared.observation['control_epoch']+=1
        else: prepared.runtimes['alice'].row['generation']+=1
        dispatch=AsyncMock()
        with pytest.raises(WorkbenchError,match='approval_mismatch'):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=AsyncMock())
        dispatch.assert_not_awaited()
        with pytest.raises(WorkbenchError,match='commit_request_mismatch'):
            await prepared.consumer.prepare('commit-call',observe=prepared.observe)
    asyncio.run(run())


@pytest.mark.parametrize('revocation',['logout','grant','tool_permission','config','switch'])
def test_execute_rechecks_real_execution_authority_before_side_effect(prepared,revocation):
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe); approve(prepared,result)
        app=prepared.app
        if revocation=='logout': app.service.revoke_session(prepared.runtimes['alice'].token)
        elif revocation in {'grant','tool_permission'}:
            role=next(row for row in app.service.list_roles(app.tenant_id) if row['code']=='sap-use')
            if revocation=='grant': app.revoke_grants(role)
            else: app.service.update_role(actor_user_id=app.admin_id,tenant_id=app.tenant_id,role_id=role['id'],name=role['name'],
                permissions=[p for p in role['permissions'] if p!='tool.execute'],expected_version=role['version'])
        elif revocation=='config': payload(app,app.put(API+'/config',{'version':2,'config':prepared.config},token=app.login('root')))
        else: prepared.runtimes['alice'].config['commit_enabled']=False
        dispatch=AsyncMock()
        with pytest.raises(WorkbenchError):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=AsyncMock())
        dispatch.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('stage',['quota','consume'])
@pytest.mark.parametrize('change',['logout','permission','amount','epoch','manual'])
def test_authority_or_page_change_during_admission_or_consumption_never_dispatches(prepared,monkeypatch,stage,change):
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe); approve(prepared,result)
        owner=prepared.runtimes['alice']; owner.controller=SimpleNamespace(control='automatic',pause=lambda:None)
        svc=prepared.app.service
        def alter():
            if change=='logout': svc.revoke_session(owner.token)
            elif change=='permission':
                role=next(row for row in svc.list_roles(prepared.app.tenant_id) if row['code']=='sap-use')
                svc.update_role(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id,
                    role_id=role['id'],name=role['name'],permissions=[p for p in role['permissions'] if p!='tool.execute'],
                    expected_version=role['version'])
            elif change=='amount': prepared.observation['page']['fields'][1]['value']='200.00'
            elif change=='epoch': prepared.observation['control_epoch']+=1
            else: owner.controller.control='manual'
        name='consume_quota' if stage=='quota' else 'consume_action_approval'
        original=getattr(svc,name)
        def consume(**kwargs):
            answer=original(**kwargs); alter(); return answer
        monkeypatch.setattr(svc,name,consume)
        dispatch=AsyncMock()
        with pytest.raises(WorkbenchError):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=AsyncMock())
        dispatch.assert_not_awaited()
        row=prepared.store.commit_action(prepared.app.tenant_id,owner.user,owner.id,result['action_id'])
        assert row['state']=='failed'
        approvals=svc.list_approvals(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id)
        assert approvals[0]['status']==('approved' if stage=='quota' else 'consumed')
    asyncio.run(run())


def test_success_requires_receipt_and_independent_read_only_match_then_cannot_replay(prepared):
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe); approve(prepared,result)
        dispatch=AsyncMock(return_value=receipt()); verify=AsyncMock(side_effect=verified)
        done=await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=verify)
        assert done['state']=='succeeded' and done['receipt']['document_number']=='4500000123'
        assert done['receipt']['business_validated'] and done['evidence_ref'].startswith('sha256:')
        with pytest.raises(WorkbenchError,match='already_dispatched'):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=verify)
        dispatch.assert_awaited_once(); verify.assert_awaited_once()
        approvals=prepared.app.service.list_approvals(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id)
        assert approvals[0]['status']=='consumed'
        audits=prepared.app.service.list_audit(prepared.app.tenant_id)
        actions={event['action'] for event in audits}
        assert {'approval.create','approval.approve','approval.consume','sap_workbench.commit.prepared',
                'sap_workbench.commit.dispatch','sap_workbench.commit.succeeded'}<=actions
        assert 'VENDOR-UNIT-01' not in str(audits)
    asyncio.run(run())


@pytest.mark.parametrize('failure',['prepared','dispatch','succeeded'])
def test_audit_failure_never_leaves_running_or_replays_uncertain_commit(prepared,monkeypatch,failure):
    tick=1791028800
    monkeypatch.setattr('calendar.timegm',lambda _:tick)
    svc=prepared.app.service
    svc.set_quota(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id,metric='tool_calls',hard_limit=10)
    audit=prepared.consumer._audit
    def broken(event,row,**facts):
        if event==failure: raise WorkbenchError('audit_unavailable',503)
        return audit(event,row,**facts)
    monkeypatch.setattr(prepared.consumer,'_audit',broken)
    async def run():
        dispatch=AsyncMock(return_value=receipt())
        if failure=='prepared':
            with pytest.raises(WorkbenchError,match='audit_unavailable'):
                await prepared.consumer.prepare('commit-call',observe=prepared.observe)
            with prepared.store._connection() as db:
                row=dict(db.execute('SELECT * FROM "cj-sap_workbench-actions" WHERE request_id=\'commit-call\'').fetchone())
            assert row['state']=='failed'
        else:
            result=await prepared.consumer.prepare('commit-call',observe=prepared.observe); approve(prepared,result)
            with pytest.raises(WorkbenchError,match='audit_unavailable' if failure=='dispatch' else 'commit_outcome_unknown'):
                await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,
                                                 verify=AsyncMock(side_effect=verified))
            row=prepared.store.commit_action(prepared.app.tenant_id,prepared.app.user_id('alice'),prepared.runtimes['alice'].id,result['action_id'])
            assert row['state']==('failed' if failure=='dispatch' else 'unknown')
        assert dispatch.await_count==(1 if failure=='succeeded' else 0)
    asyncio.run(run())
    usage=svc.quota_status(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id)['usage']
    used=sum(item['used'] for item in usage if item['user_id']=='' and item['metric']=='tool_calls')
    assert used==(1 if failure=='succeeded' else 0)


def test_real_quota_denial_refuses_before_consumption_or_dispatch(prepared,monkeypatch):
    monkeypatch.setattr('calendar.timegm',lambda _:1791028800)
    svc=prepared.app.service
    svc.set_quota(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id,metric='tool_calls',hard_limit=1)
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe); approve(prepared,result)
        assert svc.consume_quota(user_id=prepared.app.user_id('alice'),tenant_id=prepared.app.tenant_id,metric='tool_calls',amount=1)
        dispatch=AsyncMock()
        with pytest.raises(WorkbenchError,match='tool_quota_exhausted'):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=AsyncMock())
        dispatch.assert_not_awaited()
        assert svc.list_approvals(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id)[0]['status']=='approved'
    asyncio.run(run())


@pytest.mark.parametrize('reason',['label','sensitive','table'])
def test_sensitive_page_data_is_rejected_before_formal_approval(prepared,reason):
    if reason=='label': prepared.observation['page']['fields'][0]['label']='密码'
    elif reason=='sensitive': prepared.observation['page']['fields'][0]['sensitive']=True
    else: prepared.observation['page']['tables']=[{'secret':'fixture-private-value'}]
    async def run():
        with pytest.raises(WorkbenchError,match='commit_page_unsupported'):
            await prepared.consumer.prepare('commit-call',observe=prepared.observe)
    asyncio.run(run())
    assert prepared.app.service.list_approvals(actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id)==[]


def test_definite_sap_validation_failure_is_failed_and_never_replayed(prepared):
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe); approve(prepared,result)
        dispatch=AsyncMock(return_value={'outcome':'validation_failed','validated':True})
        verify=AsyncMock()
        done=await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=verify)
        assert done['state']=='failed' and done['receipt']['outcome']=='validation_failed'
        verify.assert_not_awaited()
        with pytest.raises(WorkbenchError,match='already_dispatched'):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=verify)
        dispatch.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['timeout','cancel','missing_receipt','wrong_number','unverified','write_verify','verify_error'])
def test_uncertain_dispatch_keeps_unknown_and_consumed_approval_without_replay(prepared,failure):
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe); approve(prepared,result)
        dispatch=AsyncMock(return_value=receipt()); verify=AsyncMock(side_effect=verified)
        if failure=='timeout': dispatch.side_effect=asyncio.TimeoutError()
        elif failure=='cancel': dispatch.side_effect=asyncio.CancelledError()
        elif failure=='missing_receipt': dispatch.return_value={}
        elif failure=='wrong_number': verify.side_effect=lambda record:verified(record,document_number='4500000999')
        elif failure=='unverified': verify.side_effect=lambda record:verified(record,matches=False)
        elif failure=='write_verify': verify.side_effect=lambda record:verified(record,read_only=False)
        else: verify.side_effect=RuntimeError('private SAP response must not leak')
        with pytest.raises(WorkbenchError,match='commit_outcome_unknown'):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=verify)
        status=await prepared.consumer.status(result['action_id']); assert status['state']=='unknown'
        if failure not in {'timeout','cancel','missing_receipt'}: assert status['receipt']['document_number']=='4500000123'
        for _ in range(2): prepared.store.recover_actions(prepared.runtimes['alice'].id)
        with pytest.raises(WorkbenchError,match='already_dispatched'):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=verify)
        dispatch.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('outcome',['submitted','not_submitted','unknown'])
def test_unknown_reconciliation_is_read_only_and_never_dispatches_or_reuses_old_request(prepared,outcome):
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe); approve(prepared,result)
        dispatch=AsyncMock(side_effect=asyncio.TimeoutError())
        with pytest.raises(WorkbenchError,match='unknown'):
            await prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,verify=AsyncMock())
        verify=AsyncMock(side_effect=lambda row:verified(row,outcome=outcome))
        if outcome=='unknown':
            with pytest.raises(WorkbenchError,match='unknown'): await prepared.consumer.reconcile(result['action_id'],verify=verify)
            assert (await prepared.consumer.status(result['action_id']))['state']=='unknown'
        else:
            fixed=await prepared.consumer.reconcile(result['action_id'],verify=verify)
            assert fixed['state']==('succeeded' if outcome=='submitted' else 'failed')
        with pytest.raises(WorkbenchError,match='already_dispatched'):
            await prepared.consumer.prepare('commit-call',observe=prepared.observe)
        dispatch.assert_awaited_once(); verify.assert_awaited_once()
    asyncio.run(run())


def test_other_platform_user_cannot_read_execute_or_reconcile_action(prepared):
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe)
        bob=CommitConsumer(prepared.runtimes['bob'])
        for method in (lambda:bob.status(result['action_id']),
                       lambda:bob.execute(result['action_id'],observe=prepared.observe,dispatch=AsyncMock(),verify=AsyncMock()),
                       lambda:bob.reconcile(result['action_id'],verify=AsyncMock())):
            with pytest.raises(WorkbenchError,match='action_not_found'): await method()
    asyncio.run(run())


def test_concurrent_consumers_execute_once_with_real_atomic_approval(prepared):
    async def run():
        result=await prepared.consumer.prepare('commit-call',observe=prepared.observe); approve(prepared,result)
        entered, release=asyncio.Event(),asyncio.Event()
        async def delayed(row): entered.set(); await release.wait(); return receipt()
        dispatch=AsyncMock(side_effect=delayed)
        first=asyncio.create_task(prepared.consumer.execute(result['action_id'],observe=prepared.observe,dispatch=dispatch,
                                                           verify=AsyncMock(side_effect=verified)))
        await entered.wait()
        with pytest.raises(WorkbenchError,match='already_dispatched'):
            await CommitConsumer(prepared.runtimes['alice']).execute(result['action_id'],observe=prepared.observe,
                                                        dispatch=dispatch,verify=AsyncMock(side_effect=verified))
        release.set(); assert (await first)['state']=='succeeded'
        dispatch.assert_awaited_once()
    asyncio.run(run())


def test_preparing_and_running_survive_additive_migration_and_recovery_without_replay(prepared):
    result=asyncio.run(prepared.consumer.prepare('commit-call',observe=prepared.observe))
    prepared.store.begin_commit(result['action_id'])
    with ThreadPoolExecutor(4) as threads:
        stores=list(threads.map(lambda _:WorkbenchStore(prepared.store.path),range(4)))
    for store in stores: store.recover_actions(prepared.runtimes['alice'].id)
    status=asyncio.run(prepared.consumer.status(result['action_id']))
    assert status['state']=='unknown' and status['approval_id']==result['approval_id']
    assert prepared.store.session(prepared.app.tenant_id,prepared.app.user_id('alice'),prepared.runtimes['alice'].id)['remote_session_id']
