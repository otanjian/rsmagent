"""Argument validation for the scene-mediated business-data channel.

Why this is a separate module from ``mcp_data``: ``mcp_data`` is the scene's
*private* purchase-order reader. It accepts one fixed predicate
(``EBELN = '<10 digits>'``), one of three tables, and a scene-owned decoder, and
it is deliberately absent from every model surface. The business channel is
general by design, so it needs its own bounds rather than that narrow shape --
but "general" must not mean "unbounded".

Every rule here exists to keep one property: the caller can describe *what
business data it wants* and nothing else.

* the connection is chosen server-side, so ``connection`` is a fixed value and
  the connection id is attached by :meth:`SapMcpLogin._call`;
* identity keys are refused outright (defence in depth -- ``_call`` also refuses
  them), so a caller can never re-point the call at another account or system;
* ``run_query`` is restricted to a single read-only ``SELECT`` so the channel
  cannot degrade into a general SQL console;
* every value is size- and shape-bounded.

``where`` and ``sql_query`` are passed to the MCP server, which builds the RFC
call. We therefore also refuse statement separators and comment starts, so a
value cannot end the intended statement and begin another.
"""
import json
import re

#: Only this registered connection exposes the three tools by these names
#: (``sap-abap`` names its table read ``adt_read_table``).
CONNECTION = 'sap-pyrfc'
TOOLS = frozenset({'read_table', 'run_query', 'call_rfc'})

MAX_FIELDS = 64
MAX_ROWS = 500
MAX_SKIP = 100000
MAX_WHERE = 512
MAX_SQL = 2000
MAX_PARAMS = 8000

#: A single SAP identifier: a table or a column.
TABLE = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,29}')
#: A function module: a plain name (``BAPI_PO_GETDETAIL1``) or the namespaced
#: ``/NS/NAME`` form the docstring above cites. Both alternatives are matched in
#: full, so a name can never carry whitespace, a separator or punctuation.
FUNCTION = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,39}|/[A-Za-z0-9_]{1,20}/[A-Za-z0-9_]{1,20}')
#: Statement separator, comment starts, and control characters.
UNSAFE = re.compile(r'[;\x00-\x08\x0b\x0c\x0e-\x1f]|--|/\*|\*/')
SELECT = re.compile(r'\s*SELECT\b', re.IGNORECASE)

IDENTITY_KEYS = frozenset({'connection_id', 'user', 'password', 'client',
                           'host', 'url', 'tenant_id', 'session_id'})


class McpBusinessError(ValueError):
    def __init__(self, code='mcp_action_forbidden'):
        super().__init__(code)
        self.code = code


def _text(value, limit):
    if not isinstance(value, str) or not value or len(value) > limit or UNSAFE.search(value):
        raise McpBusinessError()
    return value


def _row_count(value):
    if type(value) is not int or not 1 <= value <= MAX_ROWS:
        raise McpBusinessError()
    return value


def _read_table(arguments):
    if set(arguments) - {'table_name', 'fields', 'where', 'row_count', 'row_skip'}:
        raise McpBusinessError()
    table = arguments.get('table_name')
    if not isinstance(table, str) or not TABLE.fullmatch(table):
        raise McpBusinessError()
    fields = arguments.get('fields')
    if fields is not None:
        if not isinstance(fields, str) or not fields:
            raise McpBusinessError()
        columns = [column.strip() for column in fields.split(',')]
        if (not 1 <= len(columns) <= MAX_FIELDS or len(set(columns)) != len(columns)
                or any(not TABLE.fullmatch(column) for column in columns)):
            raise McpBusinessError()
    if 'where' in arguments:
        _text(arguments['where'], MAX_WHERE)
    _row_count(arguments.get('row_count'))
    if 'row_skip' in arguments:
        skip = arguments['row_skip']
        if type(skip) is not int or not 0 <= skip <= MAX_SKIP:
            raise McpBusinessError()


def _run_query(arguments):
    if set(arguments) != {'sql_query', 'row_count'}:
        raise McpBusinessError()
    query = _text(arguments['sql_query'], MAX_SQL)
    # Read-only: the statement must open with SELECT. This is a bound on the
    # channel, not a substitute for the SAP account's own authorizations.
    if not SELECT.match(query):
        raise McpBusinessError()
    _row_count(arguments['row_count'])


def _call_rfc(arguments):
    if set(arguments) - {'function_name', 'parameters_json'}:
        raise McpBusinessError()
    name = arguments.get('function_name')
    if not isinstance(name, str) or not FUNCTION.fullmatch(name):
        raise McpBusinessError()
    raw = arguments.get('parameters_json')
    if raw is None:
        return
    if not isinstance(raw, str) or not raw or len(raw.encode('utf-8')) > MAX_PARAMS:
        raise McpBusinessError()
    try:
        parameters = json.loads(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise McpBusinessError() from None
    if not isinstance(parameters, dict):
        raise McpBusinessError()
    # Parameters address business fields only; a connection or an account is
    # never a parameter of this channel.
    if IDENTITY_KEYS.intersection(key.lower() for key in parameters if isinstance(key, str)):
        raise McpBusinessError()


_VALIDATORS = {'read_table': _read_table, 'run_query': _run_query, 'call_rfc': _call_rfc}


def business_arguments(connection, tool, arguments):
    """Validate one business call; raise ``McpBusinessError`` on any refusal."""
    if connection != CONNECTION:
        raise McpBusinessError('mcp_connection_unavailable')
    # A tool outside the allowlist is a forbidden *action*, which is a different
    # fact from an unknown connection: the caller named something real that this
    # channel does not do, rather than asking for a system it cannot reach.
    if not isinstance(tool, str) or tool not in TOOLS:
        raise McpBusinessError('mcp_action_forbidden')
    if not isinstance(arguments, dict):
        raise McpBusinessError()
    _VALIDATORS[tool](arguments)
