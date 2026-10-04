"""Approval cannot bind ambiguous clipped business values; no SAP is called."""
import asyncio
from copy import deepcopy

import pytest

from Scene.sap_workbench.backend.commit import fingerprint
from Scene.sap_workbench.backend.configuration import WorkbenchError
from tests.test_sap_workbench_commit import app, prepared


@pytest.mark.parametrize('part',[
    'field_value', 'field_label', 'utf16_field_value', 'utf16_field_label',
    'cell_value', 'utf16_cell_value', 'rows', 'columns', 'table_truncated',
    'invalid_row', 'invalid_cell',
])
def test_ambiguous_or_invalid_snapshot_refused_before_approval(prepared,part):
    page=prepared.observation['page']
    page['tables']=[{'id':'fixture-table','rows':[['item','10']]}]
    if part=='field_value': page['fields'][0]['value']='x'*300
    elif part=='field_label': page['fields'][0]['label']='x'*200
    elif part=='utf16_field_value': page['fields'][0]['value']='\U0001f600'*150
    elif part=='utf16_field_label': page['fields'][0]['label']='\U0001f600'*100
    elif part=='cell_value': page['tables'][0]['rows']=[['x'*160]]
    elif part=='utf16_cell_value': page['tables'][0]['rows']=[['\U0001f600'*80]]
    elif part=='rows': page['tables'][0]['rows']=[['item'] for _ in range(20)]
    elif part=='columns': page['tables'][0]['rows']=[['item']*16]
    elif part=='table_truncated': page['tables'][0]['truncated']=True
    elif part=='invalid_row': page['tables'][0]['rows']=[{'hidden':'not a cell list'}]
    else: page['tables'][0]['rows']=[[{'hidden':'not a text value'}]]
    with pytest.raises(WorkbenchError,match='commit_page_unsupported'):
        asyncio.run(prepared.consumer.prepare('clipped-call',observe=prepared.observe))
    assert prepared.app.service.list_approvals(
        actor_user_id=prepared.app.admin_id,tenant_id=prepared.app.tenant_id)==[]


def test_below_limits_remains_bound_and_changes_digest(prepared):
    page=prepared.observation['page']
    page['fields'][0].update(value='\U0001f600'*149+'a',label='\U0001f600'*99+'a')
    page['tables']=[{'id':'fixture-table','rows':[['item']*15 for _ in range(19)]}]
    page['tables'][0]['rows'][0][0]='\U0001f600'*79+'a'
    before=fingerprint(prepared.runtimes['alice'],prepared.observation)
    changed=deepcopy(prepared.observation)
    changed['page']['tables'][0]['rows'][0][0]='\U0001f600'*79+'b'
    after=fingerprint(prepared.runtimes['alice'],changed)
    assert before['digest']!=after['digest']
    assert before['context_digest']!=after['context_digest']


def test_common_truncated_prefix_cannot_stand_in_for_two_original_values(prepared):
    prefix='reviewable business text '*20
    originals=[prefix+'FIRST',prefix+'SECOND']
    observations=[]
    for original in originals:
        observation=deepcopy(prepared.observation)
        observation['page']['fields'][0]['value']=original[:300]
        observations.append(observation)
    assert observations[0]==observations[1]
    for observation in observations:
        with pytest.raises(WorkbenchError,match='commit_page_unsupported'):
            fingerprint(prepared.runtimes['alice'],observation)
