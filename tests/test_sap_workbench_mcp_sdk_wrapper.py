"""Installed FastMCP str output compatibility; no server or SAP requests."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from Scene.sap_workbench.backend.mcp_data import (
    JSON_BYTES, MAX_DEPTH, MAX_NODES, McpDataError, decode_data, result_data,
)


def result(*, text=None, structured=None):
    content = [] if text is None else [SimpleNamespace(type='text', text=text)]
    return SimpleNamespace(content=content, structuredContent=structured, isError=False)


@pytest.mark.parametrize('with_text', [False, True])
def test_exact_string_wrapper_decodes_one_complete_business_object(with_text):
    value = {'table': 'QUERY', 'rows': [{'ROW_TOTAL': '1'}], 'row_count': 1}
    wrapped = json.dumps(value, indent=2)
    # Different whitespace/key order is equivalent; bool and number are not.
    text = json.dumps(value, sort_keys=True) if with_text else None
    assert result_data(result(text=text, structured={'result': wrapped})) == value


@pytest.mark.parametrize('structured', [
    {'result': {'rows': []}}, {'result': None}, {'result': 1},
    {'result': '{"rows":[]}', 'extra': True},
    {'output': '{"rows":[]}'}, {'data': '{"rows":[]}'},
    {'result': '{"rows":["different"]}'}, {'result': '{"value":1}'},
])
def test_unknown_extra_wrong_type_and_conflicting_wrappers_are_refused(structured):
    with pytest.raises(McpDataError):
        result_data(result(text='{"rows":[]}', structured=structured))


@pytest.mark.parametrize('with_text', [False, True])
@pytest.mark.parametrize('wrapped', [
    '{', '[]', 'null', '"not an object"',
    '{"rows":[],"rows":[1]}', '{"rows":[NaN]}', '{"rows":[Infinity]}',
    '{"rows":[1e999]}', '{"error":false}',
    '{"rows":[{"password":"fixture-only"}]}',
    '{"rows":[{"connection_id":"fixture-only"}]}',
    '{"rows":["fixture-private-connection"]}',
    '{"rows":"\ud800"}',
    json.dumps({'rows': 'x' * JSON_BYTES}),
])
def test_wrapper_cannot_bypass_strict_json_or_private_identity_guard(wrapped, with_text):
    with pytest.raises(McpDataError):
        result_data(result(text='{"rows":[]}' if with_text else None,
                           structured={'result': wrapped}), ['fixture-private-connection'])


def test_bool_and_numeric_wrapped_values_are_not_canonically_equal():
    with pytest.raises(McpDataError):
        result_data(result(text='{"value":true}', structured={'result': '{"value":1}'}))


@pytest.mark.parametrize('boundary', ['bytes', 'depth', 'nodes'])
@pytest.mark.parametrize('with_text', [False, True])
def test_sdk_wrapper_preserves_all_accepted_inner_data_boundaries(boundary, with_text):
    if boundary == 'bytes':
        value = {'rows': 'x' * (JSON_BYTES - len('{"rows":""}'))}
    elif boundary == 'depth':
        value = 'leaf'
        for _ in range(MAX_DEPTH):
            value = {'child': value}
    else:
        value = {'rows': [0] * (MAX_NODES - 2)}
    data = json.dumps(value, separators=(',', ':'))
    assert decode_data(data) == value
    assert result_data(result(text=data if with_text else None, structured={'result': data})) == value


def test_plain_business_structured_dict_remains_compatible():
    value = {'table': 'EKKO', 'rows': [], 'row_count': 0}
    assert result_data(result(structured=value)) == value
    assert result_data(result(text=json.dumps(value), structured=value)) == value


def test_actual_worker_sdk_converts_read_table_and_run_query_string_results_offline():
    root = Path(__file__).resolve().parents[1]
    executable = os.environ.get('SAP_MCP_PYTHON', str(root.parent / 'rsmcode/sap-connect/sap-pyrfc/.venv/bin/python'))
    if not Path(executable).is_file():
        pytest.skip('Optional dedicated MCP worker interpreter is not installed')
    script = '''
import json, platform
from importlib.metadata import version
from mcp.server.fastmcp.utilities.func_metadata import func_metadata
from mcp.types import CallToolResult
from Scene.sap_workbench.backend.mcp_data import McpDataError, result_data

read_payload = {'table':'EKKO','backend':'adt','row_count':1,'columns':['MANDT','EBELN'],
                'rows':[{'MANDT':'200','EBELN':'4500000123'}]}
count_payload = {'table':'QUERY','backend':'adt','row_count':1,'columns':['ROW_TOTAL'],
                 'rows':[{'ROW_TOTAL':'1'}]}

# These are the local server's str annotations and argument signatures, with
# pure return fixtures in place of its registry/ADT/RFC implementation.
def read_table(connection_id: str, table_name: str, fields: str = '', where: str = '',
               row_count: int = 20, row_skip: int = 0) -> str:
    return json.dumps(read_payload, ensure_ascii=False, indent=2)

def run_query(connection_id: str, sql_query: str, row_count: int = 20) -> str:
    return json.dumps(count_payload, ensure_ascii=False, indent=2)

names = []
for function, arguments, expected in (
    (read_table, ('fixture-private-connection','EKKO'), read_payload),
    (run_query, ('fixture-private-connection',"SELECT COUNT(*) AS ROW_TOTAL FROM EKKO WHERE EBELN = '4500000123'"), count_payload),
):
    metadata = func_metadata(function)
    text = function(*arguments)
    content, structured = metadata.convert_result(text)
    assert metadata.wrap_output is True and structured == {'result': text}
    assert len(content) == 1 and content[0].type == 'text' and content[0].text == text
    sdk_result = CallToolResult(content=content, structuredContent=structured, isError=False)
    assert result_data(sdk_result, ['fixture-private-connection']) == expected
    assert result_data(CallToolResult(content=[], structuredContent=structured)) == expected
    # SDK objects must retain the same strict conflict/error handling.
    try:
        result_data(CallToolResult(content=content, structuredContent={'result':'{"rows":[]}'}))
    except McpDataError: pass
    else: raise AssertionError('Conflicting SDK representations were accepted')
    try:
        result_data(CallToolResult(content=content, structuredContent=structured, isError=True))
    except McpDataError as error: assert error.code == 'mcp_call_failed'
    else: raise AssertionError('SDK error was accepted')
    names.append(function.__name__)
print(json.dumps({'python':platform.python_version(),'mcp':version('mcp'),'tools':names}))
'''
    completed = subprocess.run([executable, '-B', '-c', script], cwd=root,
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert 'RuntimeWarning' not in completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome['tools'] == ['read_table', 'run_query']
    assert outcome['python'] and outcome['mcp']
    print(f"Offline worker contract: Python {outcome['python']}, MCP {outcome['mcp']}")
