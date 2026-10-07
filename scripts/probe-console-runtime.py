#!/usr/bin/env python3
"""Read-only WSGI and loaded-resource probe for the rdai console."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from channel.web.web_channel import build_web_app
from channel.web.route_registry import shell_entry_paths


def probe():
    errors = []
    app = build_web_app()
    html = app.request('/').data.decode()
    paths = shell_entry_paths()
    for path in paths:
        response = app.request(path)
        if not response.status.startswith('200') or b'id="console-page-routes"' not in response.data:
            errors.append('page entry missing: ' + path)
    scripts = re.findall(r'<script[^>]+src="/assets/([^"?]+)', html)
    styles = re.findall(r'<link[^>]+href="/assets/(css/[^"?]+)', html)
    loaded = scripts + styles
    if len(loaded) != len(set(loaded)):
        errors.append('duplicate runtime resource')
    if re.search(r'(?:src|href|data-fork-fragment)="assets/', html):
        errors.append('relative first-party resource breaks deep links')
    contract = json.loads((ROOT / 'docs/design/upstream-sync/runtime-contract.json').read_text())
    for path in loaded:
        target = ROOT / 'channel/web/static' / path
        if not target.is_file():
            errors.append('loaded resource missing: ' + path)
    for path in contract['required_assets']:
        if path not in loaded:
            errors.append('required implementation is not loaded: ' + path)
    for path in contract['forbidden_parallel_assets']:
        if path in loaded:
            errors.append('parallel implementation loaded: ' + path)
    for before, after in contract['load_order']:
        if before not in scripts or after not in scripts or scripts.index(before) >= scripts.index(after):
            errors.append('dependency order: ' + before + ' before ' + after)
    for path, tokens in contract['implementation_contracts'].items():
        source = (ROOT / path).read_text() if (ROOT / path).is_file() else ''
        for token in tokens:
            if token not in source:
                errors.append('missing runtime hook/dependency: ' + path + ': ' + token)
    return {'errors': errors, 'page_paths': paths, 'loaded_resources': loaded}


if __name__ == '__main__':
    result = probe()
    print(json.dumps(result))
    raise SystemExit(bool(result['errors']))
