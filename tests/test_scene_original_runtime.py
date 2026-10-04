"""Exercise original OneAgent handlers through the rsmagent integration seam."""
import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web
from auth.runtime import RequestContext
from channel.web import web_channel
from common.runtime_identity import RuntimeIdentity, use_identity
from Scene._shared.http import HANDLERS
from Scene._shared.frontend import SceneAssetHandler, runtime_script
from Scene._shared.original import load


class OriginalSceneRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.admin = True
        @contextlib.contextmanager
        def scope():
            with use_identity(RuntimeIdentity(user_id='test', tenant_id='test')):
                yield RequestContext('test', 'test', 'test', False, False, 'test', {}, {'chat.use'}, self.admin)
        self.scope = patch.object(web_channel, '_db_scope', scope)
        self.scope.start(); self.addCleanup(self.scope.stop)
        self.ws = patch.object(web_channel, '_get_workspace_root', return_value=self.scratch.name)
        self.ws.start(); self.addCleanup(self.ws.stop)
        self.app = web.application((
            '/upload', 'WorkbenchUploadHandler', '/excel', 'WorkbenchParseExcelHandler',
            '/voucher', 'VoucherTemplateHandler', '/schedule', 'SchedulingScheduleHandler',
            '/history', 'SchedulingHistoryHandler', '/airbag/(.*)', 'AirbagSchedulingHandler',
            '/erp', 'ErpConnectionsHandler', '/assets/(.*)', 'SceneAssetHandler',
            '/import', 'ProcurementImportHandler',
        ), dict(HANDLERS, SceneAssetHandler=SceneAssetHandler), autoreload=False)

    def request(self, url, body=None, method=None):
        return self.app.request(url, method=method or ('POST' if body is not None else 'GET'),
                                data=json.dumps(body) if body is not None else None,
                                headers={'Host': 'test', 'Content-Type': 'application/json'})

    def payload(self, url, body=None, method=None):
        response = self.request(url, body, method)
        self.assertEqual(response.status, '200 OK', response.data[:500])
        data = json.loads(response.data)
        self.assertEqual(data.get('status'), 'success', data.get('message'))
        return data

    def test_original_upload_and_filename_guard(self):
        data = self.payload('/upload', {'session_id': 's1', 'scene_id': 'procurement_supplier',
            'files': {'data': {'filename': '供应商.csv', 'content': '名称,金额\n测试,100'}}})
        self.assertEqual(Path(data['file_path']).read_text(), '名称,金额\n测试,100')
        self.assertTrue(data['project_root'].endswith('Scene/procurement_supplier'))
        denied = self.request('/upload', {'session_id': '../escape', 'files': {'a': {'filename': '../oops', 'content': 'x'}}})
        self.assertEqual(denied.status, '400 Bad Request')

    def test_upload_validates_derived_filename_before_writing_any_file(self):
        denied = self.request('/upload', {'session_id': 'guard', 'files': {
            'valid': {'filename': 'valid.csv', 'content': 'must not be written'},
            '../../../escaped': {'content': 'escape'},
        }})
        self.assertEqual(denied.status, '400 Bad Request')
        self.assertFalse((Path(self.scratch.name) / 'escaped.csv').exists())
        self.assertFalse((Path(self.scratch.name) / 'tmp/workbench/guard/valid.csv').exists())

    def test_upload_does_not_follow_a_symlinked_session_directory(self):
        root = Path(self.scratch.name)
        outside = root / 'outside'; outside.mkdir()
        parent = root / 'tmp/workbench'; parent.mkdir(parents=True)
        (parent / 'linked').symlink_to(outside, target_is_directory=True)
        denied = self.request('/upload', {'session_id': 'linked', 'files': {
            'data': {'content': 'must not escape'},
        }})
        self.assertEqual(denied.status, '400 Bad Request')
        self.assertEqual(list(outside.iterdir()), [])

    def test_original_excel_parser(self):
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active
        ws.append(['项目', '金额']); ws.append(['收入', 120]); ws.append(['成本', 80])
        data = io.BytesIO(); wb.save(data)
        result = self.payload('/excel', {'filename': 'test.xlsx', 'file_content': base64.b64encode(data.getvalue()).decode()})
        self.assertIn('收入', json.dumps(result, ensure_ascii=False))
        self.assertIn('120', json.dumps(result))

    def test_original_multipart_import(self):
        boundary = 'scene-test-boundary'
        body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="input.csv"\r\n'
                'Content-Type: text/csv\r\n\r\n供应商,金额\r\n测试,25\r\n--' + boundary + '--\r\n')
        result = self.app.request('/import', method='POST', data=body, headers={
            'Host': 'test', 'Content-Length': str(len(body.encode('utf-8'))),
            'Content-Type': 'multipart/form-data; boundary=' + boundary})
        self.assertEqual(result.status, '200 OK')
        data = json.loads(result.data)
        self.assertEqual(data['status'], 'success', data.get('message'))
        self.assertEqual(data['data'], [{'供应商': '测试', '金额': '25'}])

    def test_original_voucher_template(self):
        result = self.payload('/voucher', {'software': 'kingdee'})
        content = Path(result['filepath']).read_bytes()
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'))
        self.assertTrue(result['download_url'].startswith('/preview/'))

    def test_original_scheduling_engine_and_history(self):
        result = self.payload('/schedule', {'orders': [
            {'order_id': 'WO-1', 'product': 'Test', 'quantity': 10, 'due_date': '2030-12-31',
             'process_route': [{'process_name': 'cut', 'time_per_unit': 1}]}],
            'params': {'iterations': 2, 'mode': 'forward'}})
        self.assertIn('WO-1', result['gantt_html'])
        self.assertTrue(result['schedule_id'])
        history = self.payload('/history')
        self.assertIn(result['schedule_id'], json.dumps(history))

    def test_airbag_template_and_default_rules(self):
        rules = self.payload('/airbag/config')
        self.assertIsInstance(rules['data'], dict)
        template = self.payload('/airbag/template')
        self.assertIn('生产排产模板', json.dumps(template, ensure_ascii=False))
        self.admin = False
        self.assertEqual(self.request('/airbag/config', {'data': {}}, 'PUT').status, '403 Forbidden')

    def test_erp_config_is_read_only_and_needs_management_authority(self):
        # Connection management moved to the console page (change
        # add-external-system-access, task 6.5). The scene address keeps only the
        # non-secret read: a password is reported as present, never returned, and
        # the second writable form is gone -- so a POST is not handled at all
        # rather than handled-and-refused. The full create/read round trip needs
        # a bootstrapped identity tenant and is covered by
        # ``tests/test_external_erp_adapter.py``.
        listed = self.payload('/erp')
        self.assertEqual(listed['connections'], [])
        self.assertIn('catalog_revision', listed)

        # No write verb is routed, so an old unversioned full-table save can
        # neither clobber the store nor be silently accepted.
        refused = self.request('/erp', {'connections': []})
        self.assertIn(refused.status, ('405 Method Not Allowed', '404 Not Found'),
                      refused.status)

        # Reads still need the management authority; a plain member gets 403.
        self.admin = False
        self.assertEqual(self.request('/erp').status, '403 Forbidden')

    def test_source_asset_allowlist_and_bundle(self):
        self.assertEqual(self.request('/assets/runtime.js').status, '200 OK')
        self.assertEqual(self.request('/assets/finance_voucher/backend/VoucherTemplateHandler.py').status, '404 Not Found')
        self.assertEqual(self.request('/assets/../config.json').status, '404 Not Found')
        self.assertIn(b'function openVoucherWorkbench', runtime_script())
        self.assertTrue(Path(load('sap.query_planner').QueryPlanner().skill_planner_path).is_file())


if __name__ == '__main__':
    unittest.main()
