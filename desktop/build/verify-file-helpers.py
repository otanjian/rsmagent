#!/usr/bin/env python3
"""Reject packaging a write-enabled fs-guard or a read-only project-fs-guard."""
import json
from pathlib import Path
import struct
import subprocess
import sys


def probe(binary, operation):
    body = json.dumps({'id': 'package-check', 'op': operation, 'params': {}}).encode()
    result = subprocess.run([str(binary.resolve())], input=struct.pack('>I', len(body)) + body,
                            capture_output=True, timeout=10, check=True)
    assert len(result.stdout) >= 4, f'{binary.name}: missing response'
    length, = struct.unpack('>I', result.stdout[:4])
    assert len(result.stdout) == length + 4, f'{binary.name}: malformed response'
    response = json.loads(result.stdout[4:])
    assert response['id'] == 'package-check' and response['ok'] is False, response
    return response['error']['code']


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parents[1] / 'resources'
    suffix = '.exe' if sys.platform == 'win32' else ''
    for operation in ('write', 'edit', 'mkdir'):
        assert probe(root / ('fs-guard' + suffix), operation) == 'unknown_op', \
            f'fs-guard unexpectedly contains {operation}; build the two --bin targets separately'
        assert probe(root / ('project-fs-guard' + suffix), operation) == 'invalid_params', \
            f'project-fs-guard is missing {operation}'
    print('PASS: packaged read helper excludes writes; project helper recognizes write/edit/mkdir')


if __name__ == '__main__':
    main()
