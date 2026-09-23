#!/usr/bin/env python3
"""Capture the 63 one-click scenarios from the published WorkBuddy page.

The source page is a client-rendered SPA. Its published artifact is plain HTML
and carries two JavaScript literals (``SOLUTIONS`` and ``ONECLICK``); pairing
them by deep-dive slug + owning library node yields the scenario catalog that
``webhelp/scenarios.json`` ships.

Usage::

    python3 capture_scenarios.py [output.json]

Requires ``node`` on PATH to parse the JavaScript literals (the repository
already uses it for front-end tests). The output is a dev artifact: re-running
it after the source page changes is a manual, reviewed step.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path

SOURCE_URL = 'https://www.workbuddy.link/p/ZljbYQzFpALFLcPwKPlct1'
DEEP_LINK = 'workbuddy://task?action=start&prompt='
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'

# Source library node -> catalog group id. Order is the serving order.
GROUPS = [
    ('manufacturing', 'XDAj9uPGyFKYk1zicA86ni'),
    ('hr', 'ALPypL0fZCtq6kc2StM8wC'),
    ('finance', 'hPAUJKV8TPAtMB4ZYl32yb'),
    ('sales', 'ots1af1M8J2fvk7OxWVwPg'),
]
REQUIRED = ('skillName', 'desc', 'caseHint', 'prompt')


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode('utf-8', 'replace')


def artifact_index(entry_html: str) -> str:
    match = re.search(r'window\.__PUBLISH_BOOTSTRAP__=(\{.*?\});</script>', entry_html, re.S)
    if not match:
        raise SystemExit('找不到 __PUBLISH_BOOTSTRAP__，来源页结构已变化')
    bootstrap = json.loads(match.group(1))
    return bootstrap['artifact']['url'].rstrip('/') + '/'


def literal(page: str, name: str, opener: str, closer: str) -> str:
    match = re.search(r'(?:var|const|let)\s+' + name + r'\s*=\s*' + re.escape(opener), page)
    if not match:
        raise SystemExit('找不到字面量 ' + name)
    depth, index, quote, escaped = 0, match.end() - 1, None, False
    while index < len(page):
        char = page[index]
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
        elif char in '"\'`':
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return page[match.end() - 1:index + 1]
        index += 1
    raise SystemExit('字面量 ' + name + ' 未闭合')


def evaluate(name: str, source: str):
    script = 'const value = (' + source + ');process.stdout.write(JSON.stringify(value));'
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit('node 解析 ' + name + ' 失败: ' + result.stderr.strip())
    return json.loads(result.stdout)


def node_of(url: str) -> str:
    match = re.match(r'https://[^/]+/page/([^/]+)/', url or '')
    return match.group(1) if match else ''


def slug_of(url: str) -> str:
    match = re.search(r'/([^/]+)\.html(?:[?#]|$)', url or '')
    return match.group(1) if match else ''


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else 'scenarios.json')
    page = fetch(artifact_index(fetch(SOURCE_URL)) + 'index.html')
    solutions = evaluate('SOLUTIONS', literal(page, 'SOLUTIONS', '[', ']'))
    oneclick = evaluate('ONECLICK', literal(page, 'ONECLICK', '{', '}'))

    index = {}
    for entry in solutions:
        slug, node = slug_of(entry.get('src', '')), node_of(entry.get('src', ''))
        if slug and node:
            index.setdefault((slug, node), entry)

    items, missing = [], []
    for group, library in GROUPS:
        for slug, entry in oneclick.items():
            if entry.get('node') != library:
                continue
            card = index.get((slug, library))
            for field in REQUIRED:
                if not (entry.get(field) or '').strip():
                    missing.append((slug, field))
            items.append({
                'slug': slug,
                'name': entry['skillName'],
                'group': group,
                'industry': (card or {}).get('ind', ''),
                'roles': (card or {}).get('roles', []),
                'pain': (card or {}).get('pain', ''),
                'desc': entry.get('desc', ''),
                'case_hint': entry.get('caseHint', ''),
                'prompt': entry.get('prompt', ''),
                'detail_url': (card or {}).get('src', ''),
            })
        if not any(item['group'] == group for item in items):
            raise SystemExit('分组 ' + group + ' 没有抓到场景')
    if missing:
        raise SystemExit('字段缺失: ' + repr(missing))
    if len(items) != len(oneclick):
        raise SystemExit('抓取条目数 %d 与 ONECLICK 条目数 %d 不一致' % (len(items), len(oneclick)))

    document = {
        'source': SOURCE_URL,
        'captured_at': date.today().isoformat(),
        'deep_link': DEEP_LINK,
        'groups': [{'id': group, 'library': library} for group, library in GROUPS],
        'items': items,
    }
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('写入 %s：%d 个场景 / %d 个分组' % (output, len(items), len(GROUPS)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
