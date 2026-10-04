"""Complete bounded JSON for the scene's private purchase-order reader.

This path is not a model tool. It never truncates a result or substitutes a
redaction in business data; incomplete, ambiguous or sensitive results fail.
Transport completeness does not establish a complete or matching document.
"""
import json
import math
import re

JSON_BYTES = 48000  # The JSON envelope remains below the worker pipe limit.
MAX_NODES = 10000
MAX_DEPTH = 16
TABLES = frozenset({'EKKO', 'EKPO', 'EKET'})
TOOLS = frozenset({'read_table', 'run_query'})
WHERE = re.compile(r"EBELN = '[0-9]{10}'")
COUNT = re.compile(r"SELECT COUNT\(\*\) AS ROW_TOTAL FROM (?:EKKO|EKPO|EKET) WHERE EBELN = '[0-9]{10}'")
FIELD = re.compile(r'[A-Z][A-Z0-9_]{0,29}')
SENSITIVE = re.compile(r'password|passwd|token|secret|cookie|authorization|connection_id', re.I)


class McpDataError(ValueError):
    def __init__(self, code='mcp_result_invalid'):
        super().__init__(code)
        self.code = code


def verification_arguments(connection, tool, arguments):
    """Only fixed PO table reads/counts; no generic SQL or caller identity."""
    if connection != 'sap-pyrfc' or not isinstance(tool, str) or tool not in TOOLS or not isinstance(arguments, dict):
        raise McpDataError('mcp_action_forbidden')
    if tool == 'run_query':
        if (set(arguments) != {'sql_query', 'row_count'} or type(arguments['row_count']) is not int
                or arguments['row_count'] != 1 or not isinstance(arguments['sql_query'], str)
                or not COUNT.fullmatch(arguments['sql_query'])):
            raise McpDataError('mcp_action_forbidden')
        return
    if (set(arguments) - {'table_name', 'fields', 'where', 'row_count', 'row_skip'}
            or not {'table_name', 'fields', 'where', 'row_count'} <= set(arguments)):
        raise McpDataError('mcp_action_forbidden')
    table, fields, where = (arguments[key] for key in ('table_name', 'fields', 'where'))
    if (not isinstance(table, str) or table not in TABLES or not isinstance(fields, str)
            or not isinstance(where, str) or not WHERE.fullmatch(where)
            or type(arguments['row_count']) is not int or not 1 <= arguments['row_count'] <= 500
            or type(arguments.get('row_skip', 0)) is not int or arguments.get('row_skip', 0) != 0):
        raise McpDataError('mcp_action_forbidden')
    columns = [field.strip() for field in fields.split(',')]
    if (not 1 <= len(columns) <= 48 or len(set(columns)) != len(columns)
            or any(not FIELD.fullmatch(field) or SENSITIVE.search(field) for field in columns)):
        raise McpDataError('mcp_action_forbidden')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise McpDataError()
        result[key] = value
    return result


def _constant(_):
    raise McpDataError()


def _encoded(value):
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        if len(text.encode('utf-8')) > JSON_BYTES:
            raise McpDataError('mcp_result_too_large')
        return text
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, McpDataError):
            raise
        raise McpDataError() from None


def validate_data(value, connection_ids=()):
    if not isinstance(value, dict) or 'error' in value:
        raise McpDataError()
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise McpDataError('mcp_result_too_large')
        if isinstance(item, dict):
            if any(not isinstance(key, str) or SENSITIVE.search(key) for key in item):
                raise McpDataError()
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if any(private and private in item for private in connection_ids):
                raise McpDataError()
        elif item is not None and type(item) not in {bool, int, float}:
            raise McpDataError()
        elif isinstance(item, float) and not math.isfinite(item):
            raise McpDataError()
    _encoded(value)
    return value


def _decode(text, byte_limit):
    if not isinstance(text, str):
        raise McpDataError()
    try:
        if len(text.encode('utf-8')) > byte_limit:
            raise McpDataError('mcp_result_too_large')
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        if isinstance(error, McpDataError):
            raise
        raise McpDataError() from None
    return value


def decode_data(text, connection_ids=()):
    value = _decode(text, JSON_BYTES)
    return validate_data(value, connection_ids)


def decode_envelope(text):
    # The private worker adds exactly {"data":...}\n (10 UTF-8 bytes).
    # It must not reduce the accepted data bytes/depth/nodes by one wrapper.
    value = _decode(text, JSON_BYTES + 10)
    if not isinstance(value, dict) or set(value) != {'data'}:
        raise McpDataError('mcp_call_failed')
    validate_data(value['data'])
    return value


def _structured_data(value, connection_ids):
    # FastMCP wraps a tool annotated -> str in exactly {"result": text}.
    # Decode that text once with the same strict business-data limits; the SDK
    # wrapper's JSON escaping/depth must not reduce the accepted data boundary.
    if isinstance(value, dict) and 'result' in value:
        if set(value) != {'result'} or not isinstance(value['result'], str):
            raise McpDataError()
        return decode_data(value['result'], connection_ids)
    return validate_data(value, connection_ids)


def result_data(result, connection_ids=()):
    """Accept one complete JSON payload, with consistent SDK representations."""
    if getattr(result, 'isError', False):
        raise McpDataError('mcp_call_failed')
    content = list(getattr(result, 'content', ()) or ())
    structured = getattr(result, 'structuredContent', None)
    if content:
        if len(content) != 1 or getattr(content[0], 'type', '') != 'text':
            raise McpDataError()
        value = decode_data(getattr(content[0], 'text', None), connection_ids)
        if structured is not None and _encoded(_structured_data(structured, connection_ids)) != _encoded(value):
            raise McpDataError()
        return value
    return _structured_data(structured, connection_ids)
