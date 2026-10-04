"""ME21N submission authority and durable outcome; SAP dispatch stays injected.

Callbacks are scene-owned code, never HTTP/model input. ``observe()`` returns
``{page, target_id, control_epoch}``; dispatch receives the approved action
record and returns a parsed receipt; verify receives that record with receipt
and must independently read SAP. Unknown outcomes never call dispatch again.
"""
import asyncio
import calendar
from contextlib import suppress
import hashlib
import json
import re
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from agent.approval_gate import APPROVAL_ARGUMENT, approval_decision, request_digest
from .configuration import WorkbenchError

OPERATION = 'save_purchase_order'
ACTION = 'tool:sap_save_purchase_order'
POLICY = {'approval_required_actions':[ACTION]}
SECRET = re.compile(r'password|passwd|口令|密码|token|authorization|cookie|secret',re.I)
DOCUMENT = re.compile(r'[0-9]{10}')
APPROVAL_CODES = frozenset({'approval_required','approval_unknown','approval_pending','approval_denied',
    'approval_revoked','approval_expired','approval_consumed','approval_mismatch','approval_unverified'})


def canonical(value):
    try:
        return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    except (TypeError,ValueError):
        raise WorkbenchError('commit_page_unsupported',409) from None


def _unclipped_text(value, limit):
    """Reject the ambiguous boundary of the DOM's UTF-16 slice limits.

    A value at the limit may be a longer value with an omitted suffix. It
    cannot bind a later approval to that unseen suffix. JavaScript counts
    UTF-16 code units, so Python's code-point length is insufficient here.
    """
    return isinstance(value,str) and len(value.encode('utf-16-le',errors='surrogatepass'))//2 < limit


def fingerprint(owner, observation):
    """Bind all observed business field/table values plus exact page/lease state.

    This binds the visible snapshot only; the registered production verifier
    must independently verify the complete business document. Field identity,
    labels and values plus the table snapshot participate in the digest. Password-looking or
    explicitly sensitive fields are rejected rather than put in any digest or
    approval payload. Non-secret business values enter the existing protected
    IAM approval payload for review, never scene action rows or audit changes.
    """
    if not isinstance(observation,dict): raise WorkbenchError('commit_page_unsupported',409)
    page = observation.get('page')
    target, epoch = observation.get('target_id'), observation.get('control_epoch')
    if (not isinstance(page,dict) or not isinstance(target,str) or not 1<=len(target)<=256
            or type(epoch) is not int or epoch<0):
        raise WorkbenchError('commit_page_unsupported',409)
    revision = page.get('revision')
    if (page.get('login') or page.get('truncated') or page.get('dialogs') or page.get('title') not in {'创建采购订单','Create Purchase Order'}
            or not isinstance(revision,str) or not 1<=len(revision)<=256):
        raise WorkbenchError('commit_page_unsupported',409)
    fields = page.get('fields')
    if not isinstance(fields,list) or not fields: raise WorkbenchError('commit_page_unsupported',409)
    business = []
    for field in fields:
        if not isinstance(field,dict) or field.get('sensitive') or SECRET.search(str(field.get('label',''))):
            raise WorkbenchError('commit_page_unsupported',409)
        if (not isinstance(field.get('id'),str) or not _unclipped_text(field.get('value'),300)
                or not _unclipped_text(field.get('label',''),200)):
            raise WorkbenchError('commit_page_unsupported',409)
        business.append({'id':field['id'],'label':field.get('label',''),'value':field['value']})
    tables = page.get('tables',[])
    if not isinstance(tables,list): raise WorkbenchError('commit_page_unsupported',409)
    # SNAPSHOT caps a visible table at 20 rows, 16 cells per row and 160
    # UTF-16 units per cell without retaining omitted business values. At a
    # cap, completeness is ambiguous even if the page-level flag is absent.
    for table in tables:
        if not isinstance(table,dict):
            raise WorkbenchError('commit_page_unsupported',409)
        rows = table.get('rows')
        if table.get('truncated') or not isinstance(rows,list) or len(rows)>=20:
            raise WorkbenchError('commit_page_unsupported',409)
        if any(not isinstance(row,list) or len(row)>=16
               or any(not _unclipped_text(cell,160) for cell in row) for row in rows):
            raise WorkbenchError('commit_page_unsupported',409)
    # Table cell values already belong to the trusted SAP snapshot. Never
    # accept a caller-supplied business summary in place of this observation.
    document = {'fields':business,'tables':tables}
    def secret_keys(value):
        if isinstance(value,dict):
            return any(SECRET.search(str(key)) or secret_keys(item) for key,item in value.items())
        return isinstance(value,list) and any(secret_keys(item) for item in value)
    if secret_keys(tables) or len(canonical(document).encode())>65536:
        raise WorkbenchError('commit_page_unsupported',409)
    document_digest = hashlib.sha256(canonical(document).encode()).hexdigest()
    sap = owner.config['sap']; url = urlsplit(sap['web_gui_url'])
    params = {'operation':OPERATION,'binding':owner.id,'binding_generation':owner.row['generation'],
        'config_version':owner.row['config_version'],'system':sap['system_id'],'client':sap['client'],
        'origin':f'{url.scheme}://{url.netloc}','browser_target':target,'control_epoch':epoch,
        'page_revision':revision,'document_digest':document_digest}
    context_digest = hashlib.sha256(canonical(params).encode()).hexdigest()
    target_ref = 'sap:'+hashlib.sha256(canonical({'binding':owner.id,'target':target,
                                               'system':sap['system_id'],'client':sap['client']}).encode()).hexdigest()
    return {'params':params,'digest':request_digest(ACTION,target_ref,params),'context_digest':context_digest,
            'target':target_ref,'revision':revision,'epoch':epoch,'field_count':len(business),'summary':document}


class CommitConsumer:
    def __init__(self, owner):
        self.owner = owner

    def _service(self):
        from auth.service import get_identity_service
        return get_identity_service()

    async def _authorize(self, *, commit=True):
        ctx = await self.owner.authorize()
        if ctx.user_id != self.owner.user or ctx.tenant_id != self.owner.tenant:
            raise WorkbenchError('session_forbidden',403)
        if commit and not self.owner.config.get('commit_enabled'):
            raise WorkbenchError('commit_disabled',403)
        if commit and ('tool.execute' not in ctx.permissions or not self.owner.config.get('automation_enabled')):
            raise WorkbenchError('session_forbidden',403)
        controller = getattr(self.owner,'controller',None)
        if commit and controller is not None and controller.control!='automatic':
            raise WorkbenchError('control_paused',409)
        return ctx

    async def _fresh(self, row, observe):
        await self._authorize()
        facts = fingerprint(self.owner,await observe())
        await self._authorize()
        if facts['digest']!=row['parameter_digest'] or facts['context_digest']!=row['context_digest']:
            raise WorkbenchError('approval_mismatch',409)
        return facts

    def _record(self, action):
        if not isinstance(action,str) or not 1<=len(action)<=128: raise WorkbenchError('invalid_request',400)
        row = self.owner.store.commit_action(self.owner.tenant,self.owner.user,self.owner.id,action)
        if row['action_kind'] != OPERATION: raise WorkbenchError('commit_action_mismatch',409)
        return row

    def _audit(self, event, row, **facts):
        try:
            self._service().record_business_audit(actor_user_id=self.owner.user,tenant_id=self.owner.tenant,
                action='sap_workbench.commit.'+event,target=row['id'],redacted_changes={
                    'approval_ref':row.get('approval_ref'),'parameter_digest':row['parameter_digest'],
                    'operation':OPERATION,**facts})
        except Exception:
            raise WorkbenchError('audit_unavailable',503) from None

    @staticmethod
    def projection(row):
        return {'action_id':row['id'],'approval_id':row.get('approval_ref'),'state':row['state'],
                'operation':OPERATION,'receipt':row.get('receipt',{}),'evidence_ref':row.get('evidence_ref')}

    async def status(self, action):
        await self._authorize(commit=False)
        return self.projection(self._record(action))

    async def prepare(self, call_id, *, observe):
        await self._authorize()
        if not isinstance(call_id,str) or not 1<=len(call_id)<=128: raise WorkbenchError('invalid_request',400)
        facts = fingerprint(self.owner,await observe())
        await self._authorize()
        row, created = self.owner.store.reserve_commit(self.owner.tenant,self.owner.user,self.owner.id,call_id,
            generation=self.owner.row['generation'],kind=OPERATION,digest=facts['digest'],context_digest=facts['context_digest'],
            target=facts['target'],epoch=facts['epoch'],revision=facts['revision'],config_version=self.owner.row['config_version'])
        if not created: return self.projection(row)
        approval = None
        try:
            approval = self._service().request_approval(actor_user_id=self.owner.user,tenant_id=self.owner.tenant,
                agent_id=self.owner.row['agent_id'],action=ACTION,expires_in_s=600,target=facts['target'],digest=facts['digest'],
                payload={'operation':OPERATION,'binding':self.owner.id,'system':self.owner.config['sap']['system_id'],
                         'client':self.owner.config['sap']['client'],'page_revision':facts['revision'],
                         'parameter_digest':facts['digest'],'field_count':facts['field_count'],'document':facts['summary']})
            self.owner.store.attach_commit_approval(row['id'],approval['id'])
            row = self._record(row['id'])
            self._audit('prepared',row)
            return self.projection(row)
        except BaseException as error:
            with suppress(Exception):
                self.owner.store.commit_outcome(row['id'],'failed',expected=('preparing','prepared'))
            if approval:
                with suppress(Exception):
                    self._service().cancel_approval(actor_user_id=self.owner.user,tenant_id=self.owner.tenant,
                                                    approval_id=approval['id'])
            if isinstance(error,(WorkbenchError,asyncio.CancelledError)): raise
            raise WorkbenchError('commit_admission_unavailable',503) from None

    def _approval_ready(self, row):
        entries = self._service().list_approvals(actor_user_id=self.owner.user,tenant_id=self.owner.tenant)
        approval = next((entry for entry in entries if entry['id']==row['approval_ref']),None)
        if not approval: raise WorkbenchError('approval_unknown',404)
        state = approval['status']
        if state != 'approved':
            raise WorkbenchError('approval_'+state if 'approval_'+state in APPROVAL_CODES else 'approval_unverified',409)
        approver = approval.get('decision_by')
        svc = self._service()
        if not approver or approver==self.owner.user or not (
                svc.is_platform_admin_user(approver) or svc.is_tenant_admin(approver,self.owner.tenant)):
            raise WorkbenchError('approval_unverified',403)

    async def execute(self, action, *, observe, dispatch, verify):
        await self._authorize()
        row = self._record(action)
        if row['state'] != 'prepared': raise WorkbenchError('action_already_dispatched',409)
        self._approval_ready(row)
        facts = await self._fresh(row,observe)
        self.owner.store.begin_commit(action)
        svc, charged, dispatched = None, False, False
        window = calendar.timegm(time.gmtime())
        try:
            svc = self._service()
            if not svc.consume_quota(user_id=self.owner.user,tenant_id=self.owner.tenant,metric='tool_calls',amount=1):
                raise WorkbenchError('tool_quota_exhausted',429)
            charged = True
            self._audit('dispatch',row)
            facts = await self._fresh(row,observe)
            self._approval_ready(row)
            decision = approval_decision(ACTION,parameters={**facts['params'],APPROVAL_ARGUMENT:row['approval_ref']},
                target=facts['target'],identity=SimpleNamespace(user_id=self.owner.user,tenant_id=self.owner.tenant,
                    agent_id=self.owner.row['agent_id']),config=POLICY,service=svc)
            if not decision.allowed:
                raise WorkbenchError(decision.code if decision.code in APPROVAL_CODES else 'approval_unverified',409)
            # Consumption itself is synchronous, but may race an IAM write or
            # page/control transition. Authority is checked again before the
            # callback that can send a SAP side effect, even if it burns an
            # approval for a refused attempt.
            await self._fresh(row,observe)
            dispatched = True
            receipt = await dispatch(dict(row))
            safe = {'outcome':'unknown'}
            if isinstance(receipt,dict) and isinstance(receipt.get('document_number'),str) and DOCUMENT.fullmatch(receipt['document_number']):
                safe['document_number']=receipt['document_number']
            self.owner.store.commit_receipt(action,safe)
            await self._authorize()
            return await self._verify(row,receipt,verify,recovery=False)
        except BaseException as error:
            state = 'unknown' if dispatched else 'failed'
            current = self._record(action)
            self.owner.store.commit_outcome(action,state,receipt=current['receipt'])
            with suppress(Exception): self._audit(state,row)
            if not dispatched:
                if charged and calendar.timegm(time.gmtime())==window:
                    with suppress(Exception):
                        svc.refund_quota(user_id=self.owner.user,tenant_id=self.owner.tenant,metric='tool_calls',amount=1,
                                         reference=action,reason='SAP commit refused before dispatch')
                if isinstance(error,WorkbenchError): raise
                if isinstance(error,asyncio.CancelledError): raise
                raise WorkbenchError('commit_admission_unavailable',503) from None
            raise WorkbenchError('commit_outcome_unknown',409) from None

    async def _verify(self, row, receipt, verify, *, recovery):
        receipt = receipt if isinstance(receipt,dict) else {}
        safe = {'outcome':'unknown'}
        number = receipt.get('document_number')
        if isinstance(number,str) and DOCUMENT.fullmatch(number): safe['document_number']=number
        if not recovery and receipt.get('outcome')=='validation_failed' and receipt.get('validated') is True and not number:
            safe = {'outcome':'validation_failed'}
            self._audit('validation_failed',row)
            self.owner.store.commit_outcome(row['id'],'failed',receipt=safe)
            return self.projection(self._record(row['id']))
        if not recovery and (receipt.get('outcome')!='submitted' or 'document_number' not in safe):
            raise WorkbenchError('commit_outcome_unknown',409)
        result = await verify({**row,'receipt':safe})
        await self._authorize(commit=False)
        outcome = result.get('outcome') if isinstance(result,dict) else None
        verified = (isinstance(result,dict) and result.get('read_only') is True and result.get('matches') is True
                    and result.get('parameter_digest')==row['parameter_digest'])
        number = result.get('document_number') if isinstance(result,dict) else None
        if outcome=='submitted' and verified and isinstance(number,str) and DOCUMENT.fullmatch(number):
            if safe.get('document_number') and number!=safe['document_number']:
                raise WorkbenchError('commit_outcome_unknown',409)
            safe={'outcome':'submitted','document_number':number,'business_validated':True}
            state='succeeded'
        elif recovery and outcome=='not_submitted' and verified:
            safe={'outcome':'not_submitted','business_validated':True}; state='failed'
        else:
            raise WorkbenchError('commit_outcome_unknown',409)
        evidence = 'sha256:'+hashlib.sha256(canonical(safe).encode()).hexdigest()
        self._audit('reconciled' if recovery else 'succeeded',row,outcome=state,evidence_ref=evidence)
        self.owner.store.commit_outcome(row['id'],state,receipt=safe,evidence=evidence,
                                       expected=('unknown',) if recovery else ('running',))
        return self.projection(self._record(row['id']))

    async def reconcile(self, action, *, verify):
        await self._authorize(commit=False)
        row = self._record(action)
        if row['state']!='unknown': raise WorkbenchError('commit_not_unknown',409)
        try:
            return await self._verify(row,row['receipt'],verify,recovery=True)
        except BaseException:
            raise WorkbenchError('commit_outcome_unknown',409) from None
