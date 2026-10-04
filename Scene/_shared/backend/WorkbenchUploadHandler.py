"""Store workbench uploads beneath the selected tenant's workspace."""
import base64
import binascii
import json
import uuid
from pathlib import Path

import web

from common import safe_fs
from Scene._shared.host import _get_workspace_root, _require_auth


def _component(value):
    if not isinstance(value, str) or len(safe_fs.split_relative(value)) != 1:
        raise ValueError("invalid filename or session identifier")
    return value


class WorkbenchUploadHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b'{}')
            if not isinstance(body, dict):
                raise ValueError("JSON object required")
            files = body.get('files')
            if not isinstance(files, dict) or not files:
                raise ValueError("No files provided")
            session = _component(body.get('session_id') or uuid.uuid4().hex[:8])
            prepared = []
            for kind, item in files.items():
                if not isinstance(item, dict):
                    raise ValueError("file must be an object")
                filename = _component(item.get('filename', f'{kind}.csv'))
                content = item.get('content', '')
                if not isinstance(content, str):
                    raise ValueError("file content must be a string")
                data = (base64.b64decode(content, validate=True)
                        if item.get('is_base64') else content.encode('utf-8'))
                prepared.append((kind, filename, data))
            root = Path(_get_workspace_root())
            directory = f'tmp/workbench/{session}'
            safe_fs.mkdir(root, directory)
            saved = {}
            for kind, filename, data in prepared:
                relative = f'{directory}/{filename}'
                safe_fs.write_bytes_atomic(root, relative, data)
                saved[kind] = str(root / relative)
        except (ValueError, TypeError, binascii.Error, safe_fs.UnsafePathError) as error:
            raise web.HTTPError('400 Bad Request', {'Content-Type': 'application/json'},
                                json.dumps({'status': 'error', 'message': str(error)}))
        return json.dumps({
            'status': 'success', 'files': saved,
            'upload_dir': str(root / directory), 'summary': body.get('summary', {}),
            'file_path': next(iter(saved.values())),
            'project_root': str(Path(__file__).resolve().parents[2]),
        }, ensure_ascii=False)
