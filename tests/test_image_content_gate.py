"""Offline validation of the layer scanner; not a real image/build acceptance."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest


spec = importlib.util.spec_from_file_location('image_content_gate', Path(__file__).resolve().parents[1] / 'scripts/check-image-content.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def _tar(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, data in files.items():
            item = tarfile.TarInfo(name); item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


@pytest.mark.parametrize('case', ['safe', 'deleted-layer', 'metadata', 'forbidden-path'])
def test_all_layers_and_metadata_are_checked(tmp_path, case):
    first = {'app/skills/legitimate.py': b'print("ok")'}
    config = b'{}'
    if case == 'deleted-layer':
        # The marker spans stream chunks and is subsequently deleted.
        first['app/secret.txt'] = b'x' * (1024 * 1024 - 10) + gate.MARKER
    elif case == 'metadata':
        config = json.dumps({'history': [{'created_by': gate.MARKER.decode()}]}).encode()
    elif case == 'forbidden-path':
        first['app/identity.db'] = b'synthetic'
    path = tmp_path / 'image.tar'
    path.write_bytes(_tar({'manifest.json': json.dumps([{'Config': 'config.json', 'Layers': ['first.tar', 'second.tar']}]).encode(),
        'config.json': config, 'first.tar': _tar(first), 'second.tar': _tar({'app/.wh.secret.txt': b''})}))
    if case == 'safe':
        assert gate.scan(path) == 1
    else:
        with pytest.raises(ValueError):
            gate.scan(path)
