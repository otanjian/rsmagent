"""Private, read-only ordinary material PO projection; not submission authority.

The local sap-pyrfc gateway returns ``{table,backend,row_count,columns,rows}``
from read_table and the same shape (table=QUERY) from ADT run_query. Its ADT
preview caps reads at 500 and cannot page with row_skip. Counts, exact keys,
foreign keys, two equal complete reads and business checks therefore precede
every result. These sequential reads are NOT a transaction snapshot and do
not establish a GUI baseline, SAP identity, tax/condition totals or TCURX.

SAP's API CDS field mapping is the source for EKKO/EKPO/EKET:
https://help.sap.com/docs/SAP_S4HANA_ON-PREMISE/af9ef57f504840d2b81be8667206d485/1d6f6bea1c3b4f049742e15a81ff86a0.html
Schedule EINDT/MENGE semantics:
https://help.sap.com/docs/SAP_S4HANA_ON-PREMISE/8308e6d301d54584a33cd04a9861bc52/6e52f71dfb5a481b810ce0697708baf2.html
EKKO MEMORY/MEMORYTYPE (parked/held/incomplete documents):
https://help.sap.com/docs/SUPPORT_CONTENT/spmm/3362167263.html

Only a scene-owned ConfiguredMcp.read_json implementation may supply reads;
neither model-provided SQL nor a caller's matches=True is used. Nothing in
this module registers a submission adapter or returns CommitConsumer's
generic success contract. A future verified GUI collector must construct
the entire expected document, then independently cover the omitted scope.
"""
import asyncio
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import json
import re
import unicodedata

from .configuration import WorkbenchError
from .deadline import task_timeout

SCOPE = 'standard_material_purchase_order_v1'
MAX_ROWS = 500
READ_SECONDS = 90
# Restrict the first projection to conventional two-decimal currencies. This
# is a supported-shape rule, not proof of the system's TCURX customization.
TWO_DECIMAL_CURRENCIES = frozenset({'CNY', 'USD', 'EUR', 'GBP', 'CHF', 'HKD',
                                   'AUD', 'CAD', 'SGD', 'NZD'})
FIELDS = {
    'EKKO': ('MANDT', 'EBELN', 'BSTYP', 'BSART', 'BSAKZ', 'LOEKZ', 'MEMORY', 'MEMORYTYPE',
             'LIFNR', 'BUKRS', 'EKORG', 'EKGRP', 'WAERS', 'BEDAT', 'ZTERM',
             'INCO1', 'INCO2', 'RESWK'),
    'EKPO': ('MANDT', 'EBELN', 'EBELP', 'BUKRS', 'PSTYP', 'KNTTP', 'LOEKZ',
             'MATNR', 'TXZ01', 'WERKS', 'LGORT', 'MEINS', 'MENGE', 'NETPR',
             'PEINH', 'BPRME', 'BPUMZ', 'BPUMN', 'NETWR', 'MWSKZ', 'RETPO',
             'UMSON', 'KONNR', 'KTPNR', 'PACKNO', 'SOBKZ', 'ELIKZ', 'EREKZ',
             'BSTAE', 'ABSKZ', 'NOVET', 'STAPO', 'UEBPO'),
    'EKET': ('MANDT', 'EBELN', 'EBELP', 'ETENR', 'EINDT', 'MENGE', 'LPEIN',
             'UZEIT', 'STARTDATE', 'ENDDATE'),
}
HEADER_KEYS = frozenset({'document_category', 'document_type', 'supplier', 'company_code',
    'purchasing_organization', 'purchasing_group', 'currency', 'order_date', 'payment_terms',
    'incoterms_code', 'incoterms_location'})
ITEM_KEYS = frozenset({'item_number', 'material', 'description', 'plant', 'storage_location',
    'quantity', 'order_unit', 'net_price', 'price_unit', 'price_unit_measure', 'net_amount',
    'tax_code', 'delivery_completed', 'invoice_completed', 'schedules'})
SCHEDULE_KEYS = frozenset({'schedule_number', 'delivery_date', 'quantity'})


def _fail(code='purchase_order_data_invalid', status=502):
    raise WorkbenchError(code, status)


def _text(value, maximum, *, required=False, pattern=None):
    if (not isinstance(value, str) or len(value) > maximum or
            any(unicodedata.category(char).startswith('C') for char in value)):
        _fail()
    value = value.strip(' ')
    if required and not value or pattern is not None and value and not re.fullmatch(pattern, value):
        _fail()
    return value


def _identifier(value, maximum, *, required=True):
    return _text(value, maximum, required=required, pattern=r'[A-Z0-9_./-]+')


def _number(value, length):
    value = _text(value, length, required=True, pattern=r'[0-9]{' + str(length) + '}')
    if not int(value):
        _fail()
    return value


def _date(value):
    value = _text(value, 10, required=True)
    if re.fullmatch(r'[0-9]{8}', value):
        value = value[:4] + '-' + value[4:6] + '-' + value[6:]
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        _fail()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        _fail()


def _decimal(value, *, digits=13, scale=3, positive=True):
    value = _text(value, 40, required=True, pattern=r'[0-9]+(?:\.[0-9]+)?')
    integer, _, fraction = value.partition('.')
    if len(integer.lstrip('0')) > digits - scale or len(fraction) > scale:
        _fail()
    result = Decimal(value)
    if positive and result <= 0:
        _fail()
    return result


def _quantity(value):
    text = format(_decimal(value), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _money(value):
    with localcontext() as context:
        context.prec = 40
        return format(_decimal(value, scale=2).quantize(Decimal('.01')), 'f')


def _integer(value):
    return str(int(_decimal(value, digits=5, scale=0)))


def _flag(value):
    value = _text(value, 1)
    if value not in {'', 'X'}:
        _fail()
    return value == 'X'


def _blank(row, fields, *, zero=False):
    for field in fields:
        value = _text(row[field], 40)
        if value and not (zero and re.fullmatch(r'0+', value)):
            _fail('purchase_order_shape_unsupported', 409)


def _digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _rows(payload, table, columns):
    allowed = {'table', 'backend', 'row_count', 'columns', 'column_meta', 'rows'}
    if (not isinstance(payload, dict) or set(payload) - allowed or payload.get('table') != table or
            payload.get('backend') != 'adt' or type(payload.get('row_count')) is not int or
            not isinstance(payload.get('rows'), list) or not isinstance(payload.get('columns'), list)):
        _fail()
    names, rows = payload['columns'], payload['rows']
    if (names != list(columns) or len(rows) != payload['row_count'] or len(rows) > MAX_ROWS or
            any(not isinstance(row, dict) or set(row) != set(columns) or
                any(not isinstance(value, str) for value in row.values()) for row in rows)):
        _fail()
    meta = payload.get('column_meta')
    if meta is not None and (not isinstance(meta, list) or len(meta) != len(columns) or any(
            not isinstance(item, dict) or item.get('name') != name
            for item, name in zip(meta, columns))):
        _fail()
    return rows


def _header(row):
    if _text(row['BSTYP'], 1) != 'F' or _text(row['BSART'], 4) != 'NB':
        _fail('purchase_order_shape_unsupported', 409)
    _blank(row, ('BSAKZ', 'LOEKZ', 'MEMORY', 'MEMORYTYPE', 'RESWK'))
    currency = _identifier(row['WAERS'], 5)
    if currency not in TWO_DECIMAL_CURRENCIES:
        _fail('purchase_order_shape_unsupported', 409)
    return {'document_category': 'F', 'document_type': 'NB',
        'supplier': _identifier(row['LIFNR'], 10), 'company_code': _identifier(row['BUKRS'], 4),
        'purchasing_organization': _identifier(row['EKORG'], 4),
        'purchasing_group': _identifier(row['EKGRP'], 3), 'currency': currency,
        'order_date': _date(row['BEDAT']), 'payment_terms': _identifier(row['ZTERM'], 4, required=False),
        'incoterms_code': _identifier(row['INCO1'], 3, required=False),
        'incoterms_location': _text(row['INCO2'], 28)}


def _item(row, company):
    if _text(row['PSTYP'], 1) != '0':
        _fail('purchase_order_shape_unsupported', 409)
    _blank(row, ('KNTTP', 'LOEKZ', 'RETPO', 'UMSON', 'KONNR', 'SOBKZ',
                 'BSTAE', 'ABSKZ', 'NOVET', 'STAPO'))
    _blank(row, ('KTPNR', 'PACKNO', 'UEBPO'), zero=True)
    if _identifier(row['BUKRS'], 4) != company:
        _fail()
    unit = _text(row['MEINS'], 3, required=True, pattern=r'[A-Z0-9%/]+')
    price_unit_measure = _text(row['BPRME'], 3, required=True, pattern=r'[A-Z0-9%/]+')
    if price_unit_measure != unit or _integer(row['BPUMZ']) != '1' or _integer(row['BPUMN']) != '1':
        _fail('purchase_order_shape_unsupported', 409)
    quantity, price, price_unit, amount = (_quantity(row['MENGE']), _money(row['NETPR']),
                                         _integer(row['PEINH']), _money(row['NETWR']))
    with localcontext() as context:
        context.prec = 40
        calculated = (Decimal(price) * Decimal(quantity) / Decimal(price_unit)).quantize(
            Decimal('.01'), rounding=ROUND_HALF_UP)
    if calculated != Decimal(amount):
        _fail('purchase_order_amount_mismatch', 409)
    return {'item_number': _number(row['EBELP'], 5), 'material': _identifier(row['MATNR'], 40),
        'description': _text(row['TXZ01'], 40, required=True), 'plant': _identifier(row['WERKS'], 4),
        'storage_location': _identifier(row['LGORT'], 4, required=False), 'quantity': quantity,
        'order_unit': unit, 'net_price': price, 'price_unit': price_unit,
        'price_unit_measure': price_unit_measure, 'net_amount': amount,
        'tax_code': _identifier(row['MWSKZ'], 2, required=False),
        'delivery_completed': _flag(row['ELIKZ']), 'invoice_completed': _flag(row['EREKZ'])}


def _schedule(row):
    if _text(row['LPEIN'], 1) != '1':
        _fail('purchase_order_shape_unsupported', 409)
    _blank(row, ('UZEIT', 'STARTDATE', 'ENDDATE'), zero=True)
    return {'schedule_number': _number(row['ETENR'], 4), 'delivery_date': _date(row['EINDT']),
            'quantity': _quantity(row['MENGE'])}


def _assemble(tables, client, number):
    if len(tables['EKKO']) != 1:
        _fail('purchase_order_not_found', 404) if not tables['EKKO'] else _fail()
    if not tables['EKPO']:
        _fail()
    for rows in tables.values():
        for row in rows:
            if _text(row['MANDT'], 3) != client or _number(row['EBELN'], 10) != number:
                _fail('purchase_order_scope_mismatch', 409)
    header = _header(tables['EKKO'][0])
    items = {}
    for row in tables['EKPO']:
        item = _item(row, header['company_code'])
        if item['item_number'] in items:
            _fail()
        items[item['item_number']] = item
    schedules = defaultdict(list)
    keys = set()
    for row in tables['EKET']:
        item_number = _number(row['EBELP'], 5)
        schedule = _schedule(row)
        key = (item_number, schedule['schedule_number'])
        if key in keys or item_number not in items:
            _fail()
        keys.add(key)
        schedules[item_number].append(schedule)
    for item_number, item in items.items():
        lines = schedules[item_number]
        with localcontext() as context:
            context.prec = 40
            scheduled = sum((Decimal(line['quantity']) for line in lines), Decimal(0))
        if not lines or scheduled != Decimal(item['quantity']):
            _fail('purchase_order_quantity_mismatch', 409)
        item['schedules'] = sorted(lines, key=lambda line: line['schedule_number'])
    return {'client': client, 'document_number': number, 'header': header,
            'items': [items[key] for key in sorted(items)]}


def _expected(expected, client):
    """An exact complete supported projection, never a subset or matches flag."""
    if not isinstance(expected, dict) or set(expected) != {'client', 'document_number', 'header', 'items'}:
        _fail('purchase_order_expected_invalid', 400)
    if expected['client'] != client:
        _fail('purchase_order_expected_invalid', 400)
    number = _number(expected['document_number'], 10)
    header, items = expected['header'], expected['items']
    if (not isinstance(header, dict) or set(header) != HEADER_KEYS or not isinstance(items, list) or
            not 1 <= len(items) <= MAX_ROWS or any(not isinstance(item, dict) or set(item) != ITEM_KEYS for item in items)):
        _fail('purchase_order_expected_invalid', 400)
    # Reuse the same business validation, but never invent missing fields.
    table_header = dict.fromkeys(FIELDS['EKKO'], '')
    table_header.update(MANDT=client, EBELN=number, BSTYP=header['document_category'], BSART=header['document_type'],
        LIFNR=header['supplier'], BUKRS=header['company_code'], EKORG=header['purchasing_organization'],
        EKGRP=header['purchasing_group'], WAERS=header['currency'], BEDAT=header['order_date'],
        ZTERM=header['payment_terms'], INCO1=header['incoterms_code'], INCO2=header['incoterms_location'])
    tables = {'EKKO': [table_header], 'EKPO': [], 'EKET': []}
    schedule_count = 0
    for item in items:
        lines = item['schedules']
        if (not isinstance(lines, list) or not 1 <= len(lines) <= MAX_ROWS or any(
                not isinstance(line, dict) or set(line) != SCHEDULE_KEYS for line in lines) or
                type(item['delivery_completed']) is not bool or type(item['invoice_completed']) is not bool):
            _fail('purchase_order_expected_invalid', 400)
        schedule_count += len(lines)
        if schedule_count > MAX_ROWS:
            _fail('purchase_order_expected_invalid', 400)
        row = dict.fromkeys(FIELDS['EKPO'], '')
        row.update(MANDT=client, EBELN=number, EBELP=item['item_number'], BUKRS=header['company_code'],
            PSTYP='0', MATNR=item['material'], TXZ01=item['description'], WERKS=item['plant'], LGORT=item['storage_location'],
            MENGE=item['quantity'], MEINS=item['order_unit'], NETPR=item['net_price'], PEINH=item['price_unit'],
            BPRME=item['price_unit_measure'], BPUMZ='1', BPUMN='1', NETWR=item['net_amount'], MWSKZ=item['tax_code'],
            ELIKZ='X' if item['delivery_completed'] else '', EREKZ='X' if item['invoice_completed'] else '')
        tables['EKPO'].append(row)
        for line in lines:
            schedule = dict.fromkeys(FIELDS['EKET'], '')
            schedule.update(MANDT=client, EBELN=number, EBELP=item['item_number'], ETENR=line['schedule_number'],
                            EINDT=line['delivery_date'], MENGE=line['quantity'], LPEIN='1')
            tables['EKET'].append(schedule)
    try:
        return _assemble(tables, client, number)
    except WorkbenchError:
        _fail('purchase_order_expected_invalid', 400)


class PurchaseOrderReader:
    def __init__(self, mcp, client):
        if not isinstance(client, str) or not re.fullmatch(r'[0-9]{3}', client):
            _fail('purchase_order_client_invalid', 400)
        self.mcp, self.client = mcp, client

    async def _count(self, table, number):
        sql = f"SELECT COUNT(*) AS ROW_TOTAL FROM {table} WHERE EBELN = '{number}'"
        payload = await self.mcp.read_json('sap-pyrfc', 'run_query', {'sql_query': sql, 'row_count': 1})
        rows = _rows(payload, 'QUERY', ('ROW_TOTAL',))
        if len(rows) != 1:
            _fail()
        value = _text(rows[0]['ROW_TOTAL'], 10, required=True, pattern=r'[0-9]+')
        count = int(value)
        if count > MAX_ROWS:
            _fail('purchase_order_limit_exceeded', 409)
        return count

    async def _round(self, number):
        tables, counts = {}, {}
        for table, fields in FIELDS.items():
            before = await self._count(table, number)
            payload = await self.mcp.read_json('sap-pyrfc', 'read_table', {
                'table_name': table, 'fields': ','.join(fields), 'where': f"EBELN = '{number}'", 'row_count': MAX_ROWS})
            tables[table] = _rows(payload, table, fields)
            after = await self._count(table, number)
            if before != after or before != len(tables[table]):
                _fail('purchase_order_read_incomplete', 409)
            counts[table] = before
        return _assemble(tables, self.client, number), counts

    async def read(self, number):
        try:
            number = _number(number, 10)
        except WorkbenchError:
            _fail('purchase_order_number_invalid', 400)
        try:
            async with task_timeout(READ_SECONDS):
                document, counts = await self._round(number)
                repeated, repeated_counts = await self._round(number)
        except asyncio.TimeoutError:
            _fail('purchase_order_read_timeout', 504)
        if document != repeated or counts != repeated_counts:
            _fail('purchase_order_read_changed', 409)
        return {'scope': SCOPE, 'document': document, 'row_counts': counts, 'content_digest': _digest(document),
                'consistency': 'equal_repeated_reads_not_transaction_snapshot', 'submission_authority': False,
                'business_validated': False, 'complete_business_document': False}

    async def compare(self, expected):
        try:
            expected = _expected(expected, self.client)
        except WorkbenchError:
            _fail('purchase_order_expected_invalid', 400)
        actual = await self.read(expected['document_number'])
        if expected != actual['document']:
            _fail('purchase_order_mismatch', 409)
        return {'scope': SCOPE, 'projection_matches': True, 'document_number': expected['document_number'],
                'content_digest': actual['content_digest'], 'row_counts': actual['row_counts'],
                'consistency': actual['consistency'], 'submission_authority': False,
                'business_validated': False, 'complete_business_document': False}
