#!/usr/bin/env python3
"""Fail-closed upstream synchronization gate for the assembly actually shipped.

--structure-only is an investigation aid, never behavior/production acceptance.
The default runs each required file and checks exact case ids, not pass totals.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from upstream_sync_manifest import load_manifest, validate_manifest

ROOT = Path(__file__).resolve().parents[1]
LEDGER = 'docs/design/upstream-sync/capabilities.json'


def command(args, root, env=None, timeout=180):
    try:
        p = subprocess.run(args, cwd=root, env=env, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)


def candidate_digest(root):
    """Hash tracked + nonignored candidate source, including gate inputs.

    Installed dependencies, generated builds/reports and Python caches are not
    source. Unstaged and new implementation files are included before commit.
    """
    code, listing = command(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], root)
    if code:
        raise RuntimeError('Cannot identify candidate files: ' + listing)
    digest = hashlib.sha256()
    for relative in sorted(set(listing.split('\0')) - {''}):
        # Planning/evidence may be recorded after a run. Keep the executable
        # contract ledger, all runtime packages, dependency locks and CI inputs.
        if relative.startswith(('openspec/', 'doc/')):
            continue
        if relative.startswith('docs/') and not relative.startswith('docs/design/upstream-sync/'):
            continue
        path = root / relative
        digest.update(relative.encode() + b'\0')
        if path.is_symlink():
            digest.update(b'link\0' + os.readlink(path).encode())
        else:
            digest.update(b'file\0' + path.read_bytes() if path.is_file() else b'<deleted>')
        digest.update(b'\0')
    return digest.hexdigest()


def history_errors(data, previous):
    errors = []
    now = {c['id']: c for c in data['capabilities']}
    for old in previous.get('capabilities', []):
        ident = old['id']
        if ident not in now:
            errors.append(f'{ident}: historical capability removed')
        elif old.get('source') != now[ident].get('source') and old.get('evidence') == now[ident].get('evidence'):
            errors.append(f'{ident}: source changed without new evidence')
    return errors


def source_errors(root, data, source, baseline):
    errors = []
    if source != data['source_sha']:
        errors.append('source SHA differs from reviewed manifest; classify the new source first')
    code, output = command(['git', 'cat-file', '-e', source + '^{commit}'], root)
    if code:
        return errors + ['fixed source commit unavailable: ' + output]
    census = load_manifest(root / 'docs/design/upstream-sync/source-census.json')
    code, output = command(['git', 'ls-tree', '-r', source, '--', 'channel/web'], root)
    current = {}
    for line in output.splitlines():
        meta, path = line.split('\t', 1)
        current[path] = meta.split()[2]
    if code or current != census['files'] or census['source_sha'] != source:
        errors.append('upstream web file census changed; new/changed modules require review and mapping')
    for capability in data['capabilities']:
        for path in capability['source']['paths']:
            code, _ = command(['git', 'cat-file', '-e', source + ':' + path], root)
            if code:
                errors.append(capability['id'] + ': source path unavailable: ' + path)
    forks = load_manifest(root / 'channel/web/static/js/fork/manifest.json')
    for name, mapping in forks.items():
        code, content = command(['git', 'show', mapping['upstream_commit'] + ':' + mapping['upstream_path']], root)
        if code or hashlib.sha256(content.encode()).hexdigest() != mapping['upstream_sha256']:
            errors.append('fork source mapping/hash invalid: ' + name)
    # Reference namespaces must still correspond to the declared source (or
    # an unchanged, documented pre-existing fork delta in the fixed target).
    # This catches a merge that adds/replaces modules but forgets to advance
    # source_sha: comparing two editable manifest files alone cannot do that.
    areas = ('channel/web/core/', 'channel/web/api/', 'channel/web/static/js/core/',
             'channel/web/static/js/chat/', 'channel/web/static/js/views/')
    expected = {path for path in current if path.startswith(areas) and path.endswith(('.js', '.py'))}
    actual = {str(p.relative_to(root)) for area in areas for p in (root / area).rglob('*')
              if p.is_file() and p.suffix in {'.js', '.py'}}
    if expected != actual:
        errors.append('unclassified upstream module addition/removal: ' + ', '.join(sorted(expected ^ actual)))
    for path in sorted(expected & actual):
        content = (root / path).read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        if blob == current[path]:
            continue
        code, old = command(['git', 'rev-parse', data['target_base_sha'] + ':' + path], root)
        if code or blob != old.strip():
            errors.append('reference module differs from source and fixed target: ' + path)
    previous = load_manifest(root / 'docs/design/upstream-sync/history.json')
    if previous['source_sha'] != source:
        code, changes = command(['git', 'diff', '--name-only', previous['source_sha'], source, '--', 'channel/web'], root)
        classified = {p for cap in data['capabilities'] for p in cap['source']['paths']}
        for review in data.get('source_reviews', []):
            if (review.get('classification') in {'dependency', 'reference', 'behavior'}
                and review.get('owner') and review.get('evidence')
                and all((root / p).is_file() for p in review['evidence'])):
                classified.add(review.get('path'))
        unclassified = set(changes.splitlines()) - classified
        if code or unclassified:
            errors.append('changed source paths lack per-file capability/review mapping: ' + ', '.join(sorted(unclassified)))
    errors += history_errors(data, previous)
    if baseline:
        code, content = command(['git', 'show', baseline + ':' + LEDGER], root)
        if code:
            # Bootstrap is allowed only before the fixed target which introduced
            # this ledger. Later accepted candidates must carry their history.
            ancestor, _ = command(['git', 'merge-base', '--is-ancestor', baseline, data['target_base_sha']], root)
            if ancestor:
                errors.append('baseline ledger unavailable; do not silently drop historical findings')
        else:
            errors += history_errors(data, json.loads(content))
    return errors


def junit_errors(path, required):
    if not path.is_file():
        return ['pytest machine result missing']
    nodes = list(ET.parse(path).getroot().iter('testcase'))
    errors = []
    for name in required:
        matches = [n for n in nodes if n.attrib.get('name') == name]
        if len(matches) != 1 or any(n.find(tag) is not None for n in matches for tag in ('failure', 'error', 'skipped')):
            errors.append('pytest required case did not pass: ' + name)
    return errors


def tap_errors(text, required):
    cases = {}
    for line in text.splitlines():
        match = re.match(r'^(ok|not ok) \d+ - (.*?)(?: # (SKIP|TODO).*)?$', line)
        if match:
            cases.setdefault(match[2], []).append(match[1] == 'ok' and not match[3])
    return ['Node required case did not pass: ' + name for name in required if cases.get(name) != [True]]


def browser_errors(path, names):
    if not path.is_file():
        return ['browser machine result missing']
    result = load_manifest(path)
    errors = []
    for name in names:
        cases = [r for r in result.get('scenarios', []) if r.get('name') == name]
        if len(cases) != 1 or cases[0].get('passed') is not True:
            errors.append('browser required case did not pass: ' + name)
    if result.get('pageErrors') or result.get('unexpectedRoutes'):
        errors.append('browser encountered script errors or missing routes/assets')
    return errors


def runtime_errors(root):
    # Import and invoke the real app in an isolated process, not upstream build_app.
    code, output = command([sys.executable, '-B', 'scripts/probe-console-runtime.py'], root)
    try:
        result = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ['actual runtime probe failed: ' + output[-4000:]]
    return result.get('errors', []) + (['runtime process failed'] if code else [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--source', help='fixed full source SHA; never a floating branch')
    parser.add_argument('--baseline', default=os.environ.get('UPSTREAM_SYNC_BASE_SHA'), help='previous accepted candidate commit containing the historical ledger')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--structure-only', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    out = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix='upstream-sync-'))
    out.mkdir(parents=True, exist_ok=True)
    data = load_manifest(root / LEDGER)
    report = {'scope': 'structure-only' if args.structure_only else 'required-local-behavior',
              'production_acceptance': False, 'candidate_digest': candidate_digest(root),
              'source_sha': args.source or data['source_sha'], 'layers': {}}
    report['layers']['ledger'] = validate_manifest(data, root)
    if not report['layers']['ledger']:
        report['layers']['source-history'] = source_errors(root, data, report['source_sha'], args.baseline)
    report['layers']['actual-entry-dependencies'] = runtime_errors(root)
    for script in ('check-route-coverage.py', 'check-web-module-seams.py'):
        code, log = command([sys.executable, 'scripts/' + script], root)
        (out / (script + '.log')).write_text(log)
        report['layers'][script] = [] if code == 0 else [script + ' failed (see log)']
    if not args.structure_only:
        required = load_manifest(root / 'docs/design/upstream-sync/required-tests.json')
        failures = []
        with tempfile.TemporaryDirectory(prefix='sync-data-') as test_data:
            env = {**os.environ, 'COW_DATA_DIR': test_data, 'PYTHON_BIN': sys.executable}
            # A debug filter must not accidentally turn the required run into a subset.
            env.pop('COW_BROWSER_SCENARIO', None)
            for index, group in enumerate(required['pytest']):
                result = out / f'pytest-{index}.xml'
                code, log = command([sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                    '-p', 'no:randomly', '--junitxml=' + str(result), group['file']], root, env)
                (out / f'pytest-{index}.log').write_text(log)
                failures += junit_errors(result, group['ids'])
                if code:
                    failures.append(group['file'] + ' exited nonzero')
            for index, group in enumerate(required['node']):
                code, log = command(['node', '--test', '--test-reporter=tap', group['file']], root, env)
                (out / f'node-{index}.tap').write_text(log)
                failures += tap_errors(log, group['names'])
                if code:
                    failures.append(group['file'] + ' exited nonzero')
            browser_out = out / 'browser'
            env['COW_KNOWLEDGE_BROWSER_OUTPUT'] = str(browser_out)
            code, log = command(['node', required['browser']['file']], root, env, timeout=300)
            (out / 'browser.log').write_text(log)
            failures += browser_errors(browser_out / 'results.json', required['browser']['names'])
            if code:
                failures.append('required browser process failed')
        report['layers']['required-cases'] = failures
    report['ending_digest'] = candidate_digest(root)
    report['layers']['candidate-stability'] = [] if report['candidate_digest'] == report['ending_digest'] else ['candidate changed during execution; rerun against one candidate']
    report['unaccepted_capabilities'] = [c['id'] for c in data['capabilities'] if c['verification'] != 'passed']
    report['passed'] = not any(report['layers'].values())
    # Local checks never claim target deployment / remote CI / production parity.
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('Artifacts: ' + str(out))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
