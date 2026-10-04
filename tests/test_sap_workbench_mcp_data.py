"""Complete private MCP data contracts; no live SAP/model requests."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.mcp_data import (
    JSON_BYTES, MAX_DEPTH, MAX_NODES, McpDataError, decode_data, result_data, verification_arguments,
)
from Scene.sap_workbench.backend.mcp_login import SapMcpLogin, SapIdentity, McpLoginError
from tests.test_sap_workbench_mcp_runtime import Worker, setup


def sdk(value, *, structured=None, error=False):
    return SimpleNamespace(isError=error, content=[SimpleNamespace(type='text',text=json.dumps(value))],
                           structuredContent=structured)


def args(**changes):
    return {'table_name':'EKKO','fields':'MANDT,EBELN','where':"EBELN = '4500000123'",'row_count':500,**changes}


def test_complete_business_data_beyond_model_limit_is_not_clipped_or_replaced():
    value={'rows':[{'description':'x'*35000}],'row_count':1}
    assert result_data(sdk(value,structured=value))==value
    assert decode_data(json.dumps(value))==value


@pytest.mark.parametrize('text',[
    '{"rows":[],"rows":[1]}', '{"rows":[NaN]}', '{"rows":[Infinity]}',
    '{"rows":[1e999]}', '[]', 'null', '"not an object"', '{',
    '{"error":false}', '{"rows":[{"connection_id":"private"}]}',
    '{"rows":[{"password":"private"}]}', '{"rows":["fixture-private-connection"]}',
    json.dumps({'rows':'x'*JSON_BYTES}), json.dumps({'rows':'汉'*20000},ensure_ascii=False),
    '{"rows":"\ud800"}',
])
def test_incomplete_ambiguous_or_sensitive_json_never_becomes_data(text):
    with pytest.raises(McpDataError):
        decode_data(text,['fixture-private-connection'])


def test_deep_and_excessive_node_results_are_refused():
    value=[]
    for _ in range(20): value=[value]
    with pytest.raises(McpDataError): decode_data(json.dumps({'rows':value}))
    with pytest.raises(McpDataError): decode_data(json.dumps({'rows':[0]*10001}))


@pytest.mark.parametrize('failure',['sdk_error','multiple','image','mismatch','bool_number','empty'])
def test_sdk_representations_must_be_single_complete_and_consistent(failure):
    value=sdk({'rows':[]})
    if failure=='sdk_error': value.isError=True
    elif failure=='multiple': value.content*=2
    elif failure=='image': value.content=[SimpleNamespace(type='image',data='unused')]
    elif failure=='mismatch': value.structuredContent={'rows':['different']}
    elif failure=='bool_number': value=sdk({'value':True},structured={'value':1})
    else: value.content=[]
    with pytest.raises(McpDataError): result_data(value)
    assert result_data(SimpleNamespace(content=[],structuredContent={'rows':[]}))=={'rows':[]}


@pytest.mark.parametrize('changes',[
    {'table_name':'KNA1'}, {'fields':'COUNT(*)'}, {'fields':'MANDT,MANDT'},
    {'fields':'MANDT,PASSWORD'}, {'where':"EBELN = '4500000123' OR 1 = 1"},
    {'row_count':True}, {'row_count':501}, {'row_skip':1}, {'row_skip':False},
    {'connection_id':'foreign'}, {'client':'300'}, {'url':'https://foreign.invalid'},
])
def test_private_reads_cannot_expand_table_query_or_identity(changes):
    with pytest.raises(McpDataError): verification_arguments('sap-pyrfc','read_table',args(**changes))


@pytest.mark.parametrize('tool,arguments',[
    ([],{}), ('call_rfc',{}), ('run_query',{'sql_query':'SELECT * FROM EKKO','row_count':1}),
    ('run_query',{'sql_query':"SELECT COUNT(*) AS ROW_TOTAL FROM EKKO WHERE EBELN = '4500000123'",'row_count':True}),
    ('run_query',{'sql_query':"SELECT COUNT(*) AS ROW_TOTAL FROM EKKO WHERE EBELN = '4500000123'; DELETE FROM EKKO",'row_count':1}),
])
def test_private_json_path_is_not_generic_sql_or_rfc(tool,arguments):
    with pytest.raises(McpDataError): verification_arguments('sap-pyrfc',tool,arguments)


def test_fixed_count_and_read_arguments_are_accepted_only_on_pinned_gateway():
    verification_arguments('sap-pyrfc','read_table',args())
    for table in ('EKKO','EKPO','EKET'):
        verification_arguments('sap-pyrfc','run_query',{
            'sql_query':f"SELECT COUNT(*) AS ROW_TOTAL FROM {table} WHERE EBELN = '4500000123'",'row_count':1})
    with pytest.raises(McpDataError): verification_arguments('sap-abap','read_table',args())
    assert 'run_query' not in SapMcpLogin.READ_TOOLS


def test_model_call_cannot_select_private_worker_mode_or_count(monkeypatch):
    async def run():
        bridge=setup(monkeypatch)
        process=Worker()
        bridge.process=process
        bridge.ready={'connections':['sap-pyrfc']}
        for message in [
            {'connection':'sap-pyrfc','tool':'read_table','arguments':args(),'action':'read_json'},
            {'connection':'sap-pyrfc','tool':'run_query','arguments':{'sql_query':'SELECT * FROM EKKO'}},
        ]:
            with pytest.raises(WorkbenchError): await bridge.call(message)
        assert process.writes==[]
        await bridge.close()
    asyncio.run(run())


def test_complete_worker_json_roundtrip_preserves_business_values(monkeypatch):
    async def run():
        bridge=setup(monkeypatch)
        process=Worker()
        value={'rows':[{'description':'x'*35000}]}
        process.stdout.readline=AsyncMock(return_value=(json.dumps({'data':value})+'\n').encode())
        bridge.process,bridge.ready=process,{'connections':['sap-pyrfc']}
        assert await bridge.read_json('sap-pyrfc','read_table',args())==value
        assert process.writes[0]=={'action':'read_json','connection':'sap-pyrfc','tool':'read_table','arguments':args()}
        assert bridge.owner.authorize.await_count==2
        await bridge.close()
    asyncio.run(run())


@pytest.mark.parametrize('boundary', ['bytes', 'depth', 'nodes'])
def test_private_envelope_does_not_reduce_valid_data_limits(monkeypatch, boundary):
    if boundary == 'bytes':
        value = {'rows': 'x' * (JSON_BYTES - len('{"rows":""}'))}
    elif boundary == 'depth':
        value = 'leaf'
        for _ in range(MAX_DEPTH): value = {'child': value}
    else:
        value = {'rows': [0] * (MAX_NODES - 2)}
    data = json.dumps(value, separators=(',', ':'))
    assert decode_data(data) == value
    async def run():
        bridge = setup(monkeypatch)
        process = Worker()
        process.stdout.readline = AsyncMock(return_value=('{"data":' + data + '}\n').encode())
        bridge.process, bridge.ready = process, {'connections': ['sap-pyrfc']}
        assert await bridge.read_json('sap-pyrfc', 'read_table', args()) == value
        await bridge.close()
    asyncio.run(run())


@pytest.mark.parametrize('wire',[
    b'{', b'\xff\n', b'{"data":{"rows":[]},"data":{"rows":[1]}}\n',
    b'{"data":{"rows":[]},"extra":true}\n', b'{"data":{"error":"private diagnostic"}}\n',
    b'{"output":"clipped model text"}\n', b'{"data":{"rows":[NaN]}}\n',
    (json.dumps({'data':{'rows':'x'*JSON_BYTES}})+'\n').encode(),
])
def test_bad_worker_response_destroys_consumer_and_returns_fixed_error(monkeypatch,wire):
    async def run():
        bridge=setup(monkeypatch)
        process=Worker()
        process.stdout.readline=AsyncMock(return_value=wire)
        bridge.process,bridge.ready=process,{'connections':['sap-pyrfc']}
        with pytest.raises(WorkbenchError) as error:
            await bridge.read_json('sap-pyrfc','read_table',args())
        assert error.value.status==502 and 'private diagnostic' not in str(error.value)
        assert bridge.process is None and bridge.ready is None
    asyncio.run(run())


def test_revocation_after_data_arrives_discards_data_and_closes_worker(monkeypatch):
    async def run():
        bridge=setup(monkeypatch)
        process=Worker()
        process.stdout.readline=AsyncMock(return_value=b'{"data":{"rows":[]}}\n')
        bridge.process,bridge.ready=process,{'connections':['sap-pyrfc']}
        bridge.owner.authorize.side_effect=[None,WorkbenchError('config_conflict',409)]
        with pytest.raises(WorkbenchError,match='config_conflict'):
            await bridge.read_json('sap-pyrfc','read_table',args())
        assert bridge.process is None and process.writes[-1]=={'action':'close'}
    asyncio.run(run())


def test_login_private_json_uses_owned_connection_and_public_path_still_refuses_sql():
    async def run():
        identity=SapIdentity('TEST','200','UNIT',1)
        login=SapMcpLogin(identity,revalidate=AsyncMock(return_value=identity))
        session=SimpleNamespace(call_tool=AsyncMock(return_value=sdk({'rows':[]})))
        login._connections={'sap-pyrfc':(session,'fixture-private-connection',{'read_table':object(),'run_query':object()})}
        assert await login.call_json('sap-pyrfc','read_table',args())=={'rows':[]}
        session.call_tool.assert_awaited_once_with('read_table',{**args(),'connection_id':'fixture-private-connection'})
        with pytest.raises(McpLoginError,match='mcp_action_forbidden'):
            await login.call('sap-pyrfc','run_query',{})
        assert session.call_tool.await_count==1
    asyncio.run(run())
