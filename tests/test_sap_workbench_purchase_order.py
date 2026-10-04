"""Isolated ADT table/query envelopes and business data; no MCP/SAP/UI calls."""
import asyncio
from copy import deepcopy
from decimal import localcontext
import re

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend import purchase_order as po

NUMBER = '4500000123'
CLIENT = '200'


def tables():
    return {
        'EKKO': [{'MANDT': CLIENT, 'EBELN': NUMBER, 'BSTYP': 'F', 'BSART': 'NB',
            'BSAKZ': '', 'LOEKZ': '', 'MEMORY': '', 'MEMORYTYPE': '', 'LIFNR': '0000100001',
            'BUKRS': '1000', 'EKORG': '1000', 'EKGRP': '001', 'WAERS': 'CNY',
            'BEDAT': '20261004', 'ZTERM': '0001', 'INCO1': 'FOB', 'INCO2': '上海', 'RESWK': ''}],
        'EKPO': [{'MANDT': CLIENT, 'EBELN': NUMBER, 'EBELP': '00010', 'BUKRS': '1000',
            'PSTYP': '0', 'KNTTP': '', 'LOEKZ': '', 'MATNR': '000000000000100001', 'TXZ01': '测试物料',
            'WERKS': '1000', 'LGORT': '0001', 'MEINS': 'ST', 'MENGE': '10.000', 'NETPR': '5.50',
            'PEINH': '1', 'BPRME': 'ST', 'BPUMZ': '1', 'BPUMN': '1', 'NETWR': '55.00',
            'MWSKZ': 'J1', 'RETPO': '', 'UMSON': '', 'KONNR': '', 'KTPNR': '00000',
            'PACKNO': '0000000000', 'SOBKZ': '', 'ELIKZ': '', 'EREKZ': '',
            'BSTAE': '', 'ABSKZ': '', 'NOVET': '', 'STAPO': '', 'UEBPO': '00000'}],
        'EKET': [
            {'MANDT': CLIENT, 'EBELN': NUMBER, 'EBELP': '00010', 'ETENR': '0001', 'EINDT': '20261012',
             'MENGE': '4.000', 'LPEIN': '1', 'UZEIT': '000000', 'STARTDATE': '00000000', 'ENDDATE': '00000000'},
            {'MANDT': CLIENT, 'EBELN': NUMBER, 'EBELP': '00010', 'ETENR': '0002', 'EINDT': '20261014',
             'MENGE': '6.000', 'LPEIN': '1', 'UZEIT': '000000', 'STARTDATE': '00000000', 'ENDDATE': '00000000'}],
    }


def expected():
    # Written independently of reader output: prices, dates, units and all
    # items/schedules must agree, not merely a caller-provided match boolean.
    return {'client': CLIENT, 'document_number': NUMBER, 'header': {
        'document_category': 'F', 'document_type': 'NB', 'supplier': '0000100001',
        'company_code': '1000', 'purchasing_organization': '1000', 'purchasing_group': '001',
        'currency': 'CNY', 'order_date': '2026-10-04', 'payment_terms': '0001',
        'incoterms_code': 'FOB', 'incoterms_location': '上海'}, 'items': [{
        'item_number': '00010', 'material': '000000000000100001', 'description': '测试物料',
        'plant': '1000', 'storage_location': '0001', 'quantity': '10', 'order_unit': 'ST',
        'net_price': '5.50', 'price_unit': '1', 'price_unit_measure': 'ST', 'net_amount': '55.00',
        'tax_code': 'J1', 'delivery_completed': False, 'invoice_completed': False,
        'schedules': [{'schedule_number': '0001', 'delivery_date': '2026-10-12', 'quantity': '4'},
                      {'schedule_number': '0002', 'delivery_date': '2026-10-14', 'quantity': '6'}]}]}


def envelope(table, rows, columns):
    return {'table': table, 'backend': 'adt', 'row_count': len(rows),
            'columns': list(columns), 'rows': deepcopy(rows)}


class Mcp:
    def __init__(self, data=None, modify=None):
        self.data = tables() if data is None else data
        self.modify, self.calls = modify, []

    async def read_json(self, connection, tool, arguments):
        assert connection == 'sap-pyrfc'
        self.calls.append((tool, deepcopy(arguments)))
        if tool == 'run_query':
            assert set(arguments) == {'sql_query', 'row_count'} and arguments['row_count'] == 1
            match = re.fullmatch(r"SELECT COUNT\(\*\) AS ROW_TOTAL FROM (EKKO|EKPO|EKET) WHERE EBELN = '([0-9]{10})'", arguments['sql_query'])
            assert match, 'Only fixed scoped SELECT COUNT is used'
            table, number = match.groups()
            count = sum(row['EBELN'] == number for row in self.data[table])
            payload = envelope('QUERY', [{'ROW_TOTAL': str(count)}], ['ROW_TOTAL'])
        else:
            assert tool == 'read_table'
            assert set(arguments) == {'table_name', 'fields', 'where', 'row_count'}
            table = arguments['table_name']
            assert arguments['fields'] == ','.join(po.FIELDS[table]) and arguments['row_count'] == 500
            match = re.fullmatch(r"EBELN = '([0-9]{10})'", arguments['where'])
            assert match
            rows = [row for row in self.data[table] if row['EBELN'] == match.group(1)][:500]
            payload = envelope(table, rows, po.FIELDS[table])
        return self.modify(payload, len(self.calls)) if self.modify else payload


def reader(mcp=None):
    return po.PurchaseOrderReader(Mcp() if mcp is None else mcp, CLIENT)


def read(mcp=None):
    return asyncio.run(reader(mcp).read(NUMBER))


def test_complete_standard_material_order_is_assembled_from_all_three_tables():
    mcp = Mcp()
    actual = read(mcp)
    assert actual['document'] == expected()
    assert actual['row_counts'] == {'EKKO': 1, 'EKPO': 1, 'EKET': 2}
    assert actual['scope'] == po.SCOPE and actual['content_digest'].startswith('sha256:')
    assert actual['submission_authority'] is False
    assert actual['business_validated'] is False and actual['complete_business_document'] is False
    assert actual['consistency'] == 'equal_repeated_reads_not_transaction_snapshot'
    assert len(mcp.calls) == 18
    assert all(call[1].get('row_count') == (1 if call[0] == 'run_query' else 500) for call in mcp.calls)
    assert not any('row_skip' in args or 'connection_id' in args for _, args in mcp.calls)


def test_full_comparison_uses_actual_data_and_is_not_generic_commit_success():
    result = asyncio.run(reader().compare(expected()))
    assert result['projection_matches'] is True and result['submission_authority'] is False
    assert result['business_validated'] is False and result['complete_business_document'] is False
    assert not {'matches', 'read_only', 'parameter_digest', 'outcome'} & set(result)


@pytest.mark.parametrize('field,value', [
    ('supplier', '0000100002'), ('company_code', '2000'), ('purchasing_organization', '2000'),
    ('purchasing_group', '002'), ('currency', 'USD'), ('order_date', '2026-10-05'),
    ('payment_terms', '0002'), ('incoterms_code', 'CIF'), ('incoterms_location', '北京'),
])
def test_header_business_mismatches_are_not_accepted(field, value):
    wanted = expected()
    wanted['header'][field] = value
    with pytest.raises(WorkbenchError, match='purchase_order_mismatch'):
        asyncio.run(reader().compare(wanted))


@pytest.mark.parametrize('field,value', [
    ('material', '000000000000100002'), ('description', '不同物料'), ('plant', '2000'),
    ('storage_location', '0002'), ('tax_code', 'J2'), ('delivery_completed', True), ('invoice_completed', True),
])
def test_item_business_mismatches_are_not_accepted(field, value):
    wanted = expected()
    wanted['items'][0][field] = value
    with pytest.raises(WorkbenchError, match='purchase_order_mismatch'):
        asyncio.run(reader().compare(wanted))


def test_price_quantity_unit_and_each_schedule_are_compared_independently():
    for alter in ('price', 'price_unit', 'quantity', 'unit', 'date', 'schedule_split'):
        wanted = expected(); item = wanted['items'][0]
        if alter == 'price': item.update(net_price='6.00', net_amount='60.00')
        if alter == 'price_unit': item.update(price_unit='2', net_amount='27.50')
        if alter == 'quantity':
            item.update(quantity='11', net_amount='60.50'); item['schedules'][1]['quantity'] = '7'
        if alter == 'unit': item.update(order_unit='KG', price_unit_measure='KG')
        if alter == 'date': item['schedules'][1]['delivery_date'] = '2026-10-15'
        if alter == 'schedule_split':
            item['schedules'][0]['quantity'] = '5'; item['schedules'][1]['quantity'] = '5'
        with pytest.raises(WorkbenchError, match='purchase_order_mismatch'):
            asyncio.run(reader().compare(wanted))


@pytest.mark.parametrize('number', [None, 4500000123, True, '', '450000123', '0000000000',
    '４５０００００１２３', "4500000123' OR 1=1", '4500000123;DELETE', '4500000123\n'])
def test_bad_document_identifier_is_rejected_before_any_private_read(number):
    mcp = Mcp()
    with pytest.raises(WorkbenchError, match='purchase_order_number_invalid'):
        asyncio.run(reader(mcp).read(number))
    assert mcp.calls == []


@pytest.mark.parametrize('client', [None, 200, True, '20', '2000', '2OO', '２００'])
def test_bad_client_has_no_reader(client):
    with pytest.raises(WorkbenchError, match='purchase_order_client_invalid'):
        po.PurchaseOrderReader(Mcp(), client)


@pytest.mark.parametrize('shape', ['subset', 'matches_flag', 'unknown_header', 'missing_item',
    'missing_schedule', 'extra_schedule', 'unknown_item', 'bad_boolean', 'foreign_client'])
def test_partial_or_caller_declared_completeness_is_rejected_before_reads(shape):
    wanted = expected(); mcp = Mcp()
    if shape == 'subset': del wanted['header']['supplier']
    if shape == 'matches_flag': wanted['matches'] = True
    if shape == 'unknown_header': wanted['header']['complete'] = True
    if shape == 'missing_item': wanted['items'] = []
    if shape == 'missing_schedule': del wanted['items'][0]['schedules']
    if shape == 'extra_schedule': wanted['items'][0]['schedules'][0]['matches'] = True
    if shape == 'unknown_item': wanted['items'][0]['verified'] = True
    if shape == 'bad_boolean': wanted['items'][0]['delivery_completed'] = 1
    if shape == 'foreign_client': wanted['client'] = '201'
    with pytest.raises(WorkbenchError, match='purchase_order_expected_invalid'):
        asyncio.run(reader(mcp).compare(wanted))
    assert mcp.calls == []


def test_expected_schedule_limit_is_for_whole_order_not_each_item():
    wanted = expected(); first = wanted['items'][0]
    first.update(quantity='250', net_amount='1375.00')
    line = first['schedules'][0]
    first['schedules'] = [{**line, 'schedule_number': f'{index:04}', 'quantity': '1'} for index in range(1, 251)]
    second = deepcopy(first)
    second.update(item_number='00020', quantity='251', net_amount='1380.50')
    second['schedules'].append({**line, 'schedule_number': '0251', 'quantity': '1'})
    wanted['items'].append(second); mcp = Mcp()
    with pytest.raises(WorkbenchError, match='purchase_order_expected_invalid'):
        asyncio.run(reader(mcp).compare(wanted))
    assert mcp.calls == []


@pytest.mark.parametrize('mutation', ['error', 'truncated', 'partial', 'wrong_table', 'rfc_backend',
    'wrong_count', 'bool_count', 'reordered_columns', 'missing_column', 'extra_column',
    'missing_value', 'extra_value', 'numeric_value', 'bad_meta'])
def test_bad_or_unproved_transport_projection_cannot_form_business_evidence(mutation):
    def modify(payload, call):
        if call != 2: return payload
        if mutation in {'error', 'truncated', 'partial'}: payload[mutation] = True
        if mutation == 'wrong_table': payload['table'] = 'EKPO'
        if mutation == 'rfc_backend': payload['backend'] = 'pyrfc'
        if mutation == 'wrong_count': payload['row_count'] = 2
        if mutation == 'bool_count': payload['row_count'] = True
        if mutation == 'reordered_columns': payload['columns'].reverse()
        if mutation == 'missing_column': payload['columns'].pop()
        if mutation == 'extra_column': payload['columns'].append('UNKNOWN')
        if mutation == 'missing_value': del payload['rows'][0]['LIFNR']
        if mutation == 'extra_value': payload['rows'][0]['PASSWORD'] = 'private-fixture-marker'
        if mutation == 'numeric_value': payload['rows'][0]['EBELN'] = 4500000123
        if mutation == 'bad_meta': payload['column_meta'] = [{'name': 'UNKNOWN'}]
        return payload
    with pytest.raises(WorkbenchError, match='purchase_order_data_invalid') as error:
        read(Mcp(modify=modify))
    assert 'private-fixture-marker' not in str(error.value)


@pytest.mark.parametrize('value', ['501', '9999999999', '-1', '1.0', 'NaN', '', '1,000', True, 1])
def test_count_query_is_strict_and_cap_is_not_silently_paged(value):
    def modify(payload, call):
        if call == 1: payload['rows'][0]['ROW_TOTAL'] = value
        return payload
    with pytest.raises(WorkbenchError): read(Mcp(modify=modify))


def test_exact_500_rows_are_complete_only_with_matching_independent_counts():
    data = tables()
    data['EKPO'][0].update(MENGE='500.000', NETWR='2750.00')
    base = data['EKET'][0]
    data['EKET'] = [{**base, 'ETENR': f'{index:04}', 'MENGE': '1.000'} for index in range(1, 501)]
    result = read(Mcp(data))
    assert result['row_counts']['EKET'] == 500
    assert len(result['document']['items'][0]['schedules']) == 500


def test_count_disagrees_with_limited_read_or_changes_during_it():
    for altered_call in (7, 8, 9):
        def modify(payload, call):
            if call == altered_call:
                if call == 8: payload.update(row_count=1, rows=payload['rows'][:1])
                else: payload['rows'][0]['ROW_TOTAL'] = '3'
            return payload
        with pytest.raises(WorkbenchError, match='purchase_order_read_incomplete'):
            read(Mcp(modify=modify))


def test_two_equal_counts_do_not_hide_business_change_in_second_round():
    def modify(payload, call):
        if call == 11: payload['rows'][0]['LIFNR'] = '0000100002'
        return payload
    with pytest.raises(WorkbenchError, match='purchase_order_read_changed'):
        read(Mcp(modify=modify))


@pytest.mark.parametrize('table', ['EKKO', 'EKPO', 'EKET'])
def test_no_foreign_client_or_order_can_join_into_requested_document(table):
    for key, value in (('MANDT', '201'), ('EBELN', '4500000999')):
        def modify(payload, call):
            if payload['table'] == table: payload['rows'][0][key] = value
            return payload
        with pytest.raises(WorkbenchError, match='purchase_order_scope_mismatch'):
            read(Mcp(modify=modify))


@pytest.mark.parametrize('table,field,value', [
    ('EKKO', 'BSTYP', 'L'), ('EKKO', 'BSART', 'UB'), ('EKKO', 'LOEKZ', 'L'),
    ('EKKO', 'BSAKZ', 'T'), ('EKKO', 'MEMORY', 'X'), ('EKKO', 'MEMORYTYPE', 'P'),
    ('EKKO', 'MEMORYTYPE', 'H'), ('EKKO', 'MEMORYTYPE', 'A'), ('EKKO', 'RESWK', '2000'),
    ('EKKO', 'WAERS', 'JPY'), ('EKKO', 'WAERS', 'KWD'),
    ('EKPO', 'PSTYP', '9'), ('EKPO', 'PSTYP', '1'), ('EKPO', 'PSTYP', '3'),
    ('EKPO', 'KNTTP', 'K'), ('EKPO', 'LOEKZ', 'L'), ('EKPO', 'RETPO', 'X'),
    ('EKPO', 'UMSON', 'X'), ('EKPO', 'KONNR', '4600000001'), ('EKPO', 'PACKNO', '0000000001'),
    ('EKPO', 'SOBKZ', 'E'), ('EKPO', 'BPRME', 'KG'), ('EKPO', 'BPUMZ', '2'),
    ('EKPO', 'BPUMN', '2'), ('EKPO', 'BSTAE', '0001'), ('EKPO', 'ABSKZ', 'R'),
    ('EKPO', 'NOVET', 'X'), ('EKPO', 'STAPO', 'X'), ('EKPO', 'UEBPO', '00010'),
    ('EKET', 'LPEIN', '2'), ('EKET', 'UZEIT', '120000'), ('EKET', 'STARTDATE', '20261001'),
])
def test_unsupported_document_shapes_are_explicitly_refused(table, field, value):
    data = tables(); data[table][0][field] = value
    with pytest.raises(WorkbenchError, match='purchase_order_shape_unsupported'):
        read(Mcp(data))


@pytest.mark.parametrize('table,field,value', [
    ('EKKO', 'BEDAT', '20260230'), ('EKKO', 'BEDAT', '04.10.2026'), ('EKKO', 'BEDAT', '00000000'),
    ('EKKO', 'LIFNR', ''), ('EKKO', 'BUKRS', 'abc'), ('EKPO', 'MATNR', ''), ('EKPO', 'WERKS', ''),
    ('EKPO', 'MENGE', '1,000.000'), ('EKPO', 'MENGE', '1e1'), ('EKPO', 'MENGE', '-10'),
    ('EKPO', 'MENGE', '0'), ('EKPO', 'MENGE', '10.0001'), ('EKPO', 'MENGE', '10000000000'),
    ('EKPO', 'NETPR', 'NaN'), ('EKPO', 'NETPR', 'Infinity'), ('EKPO', 'NETPR', '5.501'),
    ('EKPO', 'PEINH', '0'), ('EKPO', 'PEINH', '1.0'), ('EKPO', 'MEINS', ''),
    ('EKPO', 'TXZ01', 'text\x00suffix'), ('EKPO', 'TXZ01', 'text\u202esuffix'),
    ('EKPO', 'ELIKZ', '1'), ('EKET', 'ETENR', '0000'), ('EKET', 'EINDT', '2026-13-01'),
])
def test_invalid_business_scalars_are_never_coerced_or_locale_guessed(table, field, value):
    data = tables(); data[table][0][field] = value
    with pytest.raises(WorkbenchError, match='purchase_order_data_invalid'):
        read(Mcp(data))


def test_amount_and_schedule_totals_are_independent_business_invariants():
    for table, field, value, code in [
        ('EKPO', 'NETWR', '54.99', 'purchase_order_amount_mismatch'),
        ('EKET', 'MENGE', '3.000', 'purchase_order_quantity_mismatch'),
    ]:
        data = tables(); data[table][0][field] = value
        with pytest.raises(WorkbenchError, match=code): read(Mcp(data))


def test_duplicate_keys_missing_schedules_and_orphan_schedule_are_rejected():
    for kind in ('duplicate_item', 'duplicate_schedule', 'missing_schedule', 'orphan_schedule', 'two_headers'):
        data = tables()
        if kind == 'duplicate_item': data['EKPO'].append(deepcopy(data['EKPO'][0]))
        if kind == 'duplicate_schedule': data['EKET'].append(deepcopy(data['EKET'][0]))
        if kind == 'missing_schedule': data['EKET'] = []
        if kind == 'orphan_schedule': data['EKET'][0]['EBELP'] = '00020'
        if kind == 'two_headers': data['EKKO'].append(deepcopy(data['EKKO'][0]))
        with pytest.raises(WorkbenchError): read(Mcp(data))


def test_price_unit_is_not_assumed_one_and_quantity_precision_is_preserved():
    data = tables()
    data['EKPO'][0].update(MENGE='1.250', NETPR='100.00', PEINH='100', NETWR='1.25')
    data['EKET'][0]['MENGE'] = '0.250'; data['EKET'][1]['MENGE'] = '1.000'
    item = read(Mcp(data))['document']['items'][0]
    assert item['quantity'] == '1.25' and item['price_unit'] == '100' and item['net_amount'] == '1.25'


def test_business_arithmetic_is_independent_of_callers_decimal_context():
    data = tables()
    data['EKPO'][0].update(MENGE='500.000', NETPR='10000.00', NETWR='5000000.00')
    data['EKET'][0]['MENGE'] = '125.125'; data['EKET'][1]['MENGE'] = '374.875'
    with localcontext() as context:
        context.prec = 4
        item = read(Mcp(data))['document']['items'][0]
        assert item['net_amount'] == '5000000.00' and context.prec == 4


def test_read_order_and_machine_date_format_normalization_do_not_fake_change():
    data = tables(); data['EKET'].reverse(); data['EKKO'][0]['BEDAT'] = '2026-10-04'
    assert read(Mcp(data))['document'] == expected()


def test_absence_is_not_evidence_that_a_timed_out_save_never_submitted():
    data = {key: [] for key in tables()}
    with pytest.raises(WorkbenchError, match='purchase_order_not_found') as error:
        read(Mcp(data))
    assert error.value.status == 404


def test_whole_read_has_deadline_and_external_cancellation_is_preserved(monkeypatch):
    class Stalled:
        async def read_json(self, *args): await asyncio.Future()
    monkeypatch.setattr(po, 'READ_SECONDS', .01)
    with pytest.raises(WorkbenchError, match='purchase_order_read_timeout'):
        read(Stalled())
    async def cancel():
        task = asyncio.create_task(reader(Stalled()).read(NUMBER))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(cancel())


def test_private_mcp_refusal_stays_a_refusal_and_cannot_be_reinterpreted_as_absence():
    class Refused:
        async def read_json(self, *args): raise WorkbenchError('mcp_identity_forbidden', 403)
    with pytest.raises(WorkbenchError, match='mcp_identity_forbidden') as error:
        read(Refused())
    assert error.value.status == 403
