"""Read local compatibility metadata without executing browsers or services."""
import argparse
from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sys
from xml.parsers.expat import ExpatError


BASELINE = Path(__file__).resolve().parents[1] / 'deployment/compatibility.json'
VERSION = re.compile(r'\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?')
COMMIT = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


@dataclass(frozen=True)
class MetadataPaths:
    chrome: Path | None
    system_plist: Path
    source: Path
    bun: Path | None
    mcp_venv: Path


def metadata_paths():
    # Imported lazily: this inspection stays usable without the scene's logger or
    # data-directory imports. Both interpreter layouts sit two levels above the
    # executable, so the venv root is derived the same way on Windows and POSIX.
    from .environment import mcp_python
    root = Path(__file__).resolve().parents[3]
    chrome = os.environ.get('SAP_WORKBENCH_CHROME', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    bun = shutil.which('bun')
    python = Path(mcp_python())
    return MetadataPaths(Path(chrome) if chrome else None,
                         Path('/System/Library/CoreServices/SystemVersion.plist'),
                         Path(os.environ.get('SAP_OPENCODE_ROOT', str(root.parent / 'rsmcode/opencode'))),
                         Path(bun) if bun else None, python.parent.parent)


def _bytes(path, limit=1024 * 1024):
    try:
        with path.open('rb') as source:
            data = source.read(limit + 1)
        return data if len(data) <= limit else None
    except (OSError, ValueError):
        return None


def _text(path):
    data = _bytes(path)
    if data is None:
        return None
    try:
        return data.decode('utf-8')
    except UnicodeError:
        return None


def _json(path):
    data = _text(path)
    try:
        return json.loads(data) if data is not None else None
    except (ValueError, RecursionError):
        return None


def _version(value):
    return value if isinstance(value, str) and VERSION.fullmatch(value) else None


def _plist_version(path, key):
    data = _bytes(path)
    try:
        value = plistlib.loads(data) if data is not None else None
        return _version(value.get(key)) if isinstance(value, dict) else None
    except (ValueError, TypeError, plistlib.InvalidFileException, OverflowError, ExpatError):
        return None


def chrome_version(binary):
    if binary is None or not binary.is_file():
        return None
    for parent in binary.parents:
        if parent.name == 'Contents':
            return _plist_version(parent / 'Info.plist', 'CFBundleShortVersionString')
    return None


def bun_version(binary):
    """Homebrew metadata is inspectable; arbitrary standalone binaries are not."""
    if binary is None or not binary.is_file():
        return None
    try:
        parents = binary.resolve().parents
    except (OSError, RuntimeError):
        return None
    for parent in parents:
        if parent.parent.name != 'bun' or parent.parent.parent.name != 'Cellar':
            continue
        receipt = _json(parent / 'INSTALL_RECEIPT.json')
        if isinstance(receipt, dict):
            source = receipt.get('source')
            versions = source.get('versions') if isinstance(source, dict) else None
            if isinstance(versions, dict) and _version(versions.get('stable')):
                return versions['stable']
        match = re.fullmatch(r'(\d+\.\d+\.\d+)(?:_\d+)?', parent.name)
        return match.group(1) if match else None
    return None


def git_revision(source):
    """Read HEAD/refs, including nested repositories and linked worktrees."""
    git = None
    for directory in (source, *source.parents):
        marker = directory / '.git'
        if marker.is_dir():
            git = marker
            break
        if marker.is_file():
            value = _text(marker)
            if value is None or not value.strip().startswith('gitdir: '):
                return None
            git = (directory / value.strip()[8:]).resolve()
            break
    if git is None:
        return None
    head = _text(git / 'HEAD')
    if head is None:
        return None
    head = head.strip()
    if COMMIT.fullmatch(head):
        return head
    if not head.startswith('ref: refs/'):
        return None
    ref = head[5:]
    if not re.fullmatch(r'refs/[A-Za-z0-9._/-]+', ref) or '..' in ref.split('/'):
        return None
    common = _text(git / 'commondir')
    dirs = [git, (git / common.strip()).resolve()] if common else [git]
    for directory in dirs:
        value = _text(directory / ref)
        if value is not None and COMMIT.fullmatch(value.strip()):
            return value.strip()
    for directory in dirs:
        packed = _text(directory / 'packed-refs')
        for line in (packed or '').splitlines():
            value, separator, name = line.partition(' ')
            if separator and name == ref and COMMIT.fullmatch(value):
                return value
    return None


def main_versions():
    try:
        aiohttp = _version(importlib.metadata.version('aiohttp'))
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
        aiohttp = None
    return {'main_python': _version(sys.version.split()[0]), 'aiohttp': aiohttp}


def mcp_versions(venv):
    values = {'mcp_python': None, 'mcp_sdk': None, 'anyio': None}
    for line in (_text(venv / 'pyvenv.cfg') or '').splitlines():
        key, separator, value = line.partition('=')
        if separator and key.strip() == 'version':
            values['mcp_python'] = _version(value.strip())
    try:
        sites = sorted(venv.glob('lib/python*/site-packages'))
        if not sites:
            return values
        # Multiple environment directories cannot identify the active worker.
        if len(sites) != 1:
            return values
        for distribution in importlib.metadata.distributions(path=[str(sites[0])]):
            name = distribution.metadata.get('Name', '').lower().replace('_', '-')
            if name in {'mcp', 'anyio'}:
                values['mcp_sdk' if name == 'mcp' else 'anyio'] = _version(distribution.version)
    except (OSError, ValueError, UnicodeError):
        pass
    return values


def local_versions(paths):
    package = _json(paths.source / 'packages/app/package.json')
    values = {'chrome': chrome_version(paths.chrome),
              'macos': _plist_version(paths.system_plist, 'ProductVersion'),
              'opencode_source': git_revision(paths.source),
              'opencode_web': _version(package.get('version')) if isinstance(package, dict) else None,
              'bun': bun_version(paths.bun)}
    values.update(main_versions())
    values.update(mcp_versions(paths.mcp_venv))
    return values


def load_baseline(path=BASELINE):
    baseline = _json(path)
    if not isinstance(baseline, dict) or baseline.get('format_version') != 1:
        raise ValueError('compatibility_baseline_invalid')
    if (not isinstance(baseline.get('baseline_id'), str) or not baseline['baseline_id']
            or not isinstance(baseline.get('sap_observation'), dict)
            or not isinstance(baseline.get('not_verified'), list)
            or not all(isinstance(value, str) for value in baseline['not_verified'])):
        raise ValueError('compatibility_baseline_invalid')
    entries = baseline.get('runtime_expectations')
    if not isinstance(entries, list) or not entries:
        raise ValueError('compatibility_baseline_invalid')
    known = {'chrome', 'macos', 'main_python', 'aiohttp', 'opencode_source',
             'opencode_web', 'bun', 'mcp_python', 'mcp_sdk', 'anyio'}
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('id') not in known or entry['id'] in seen:
            raise ValueError('compatibility_baseline_invalid')
        seen.add(entry['id'])
        expected = entry.get('expected')
        comparison = entry.get('comparison', 'exact')
        if comparison not in {'exact', 'commit_prefix'}:
            raise ValueError('compatibility_baseline_invalid')
        if comparison == 'commit_prefix':
            if entry['id'] != 'opencode_source' or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{10,64}', expected):
                raise ValueError('compatibility_baseline_invalid')
        elif not _version(expected):
            raise ValueError('compatibility_baseline_invalid')
    if seen != known:
        raise ValueError('compatibility_baseline_invalid')
    return baseline


def report(paths, baseline):
    versions = local_versions(paths)
    policy = baseline.get('policy')
    policy = policy if isinstance(policy, dict) else {}
    checks = []
    for entry in baseline['runtime_expectations']:
        actual = versions.get(entry['id'])
        matches = (bool(actual and actual.startswith(entry['expected']))
                   if entry.get('comparison') == 'commit_prefix' else actual == entry['expected'])
        checks.append({'id': entry['id'], 'expected': entry['expected'], 'actual': actual,
                       'status': 'unknown' if actual is None else 'matches' if matches else 'drift'})
    return {'mode': 'offline_compatibility_baseline', 'baseline_id': baseline['baseline_id'],
            'network_tested': False, 'services_started': False, 'system_modified': False,
            'baseline_policy': {
                'complete_deployment_lock': policy.get('complete_deployment_lock') is True,
                'runtime_enforced': policy.get('runtime_enforced') is True,
            },
            'working_tree_checked': False, 'all_metadata_matches': all(c['status'] == 'matches' for c in checks),
            'checks': checks, 'sap_current_version_checked': False,
            'sap_observation': baseline['sap_observation'], 'not_verified': baseline['not_verified']}


def main(argv=None):
    paths = metadata_paths()
    parser = argparse.ArgumentParser(description='Read offline SAP workbench version metadata; no browser or service execution.')
    parser.add_argument('--baseline', type=Path, default=BASELINE)
    parser.add_argument('--chrome', type=Path, default=paths.chrome)
    parser.add_argument('--system-plist', type=Path, default=paths.system_plist)
    parser.add_argument('--opencode-root', type=Path, default=paths.source)
    parser.add_argument('--bun', type=Path, default=paths.bun)
    parser.add_argument('--mcp-venv', type=Path, default=paths.mcp_venv)
    args = parser.parse_args(argv)
    try:
        baseline = load_baseline(args.baseline)
    except ValueError:
        print(json.dumps({'mode': 'offline_compatibility_baseline', 'error': 'compatibility_baseline_invalid',
                          'network_tested': False, 'services_started': False, 'system_modified': False}))
        return 2
    result = report(MetadataPaths(args.chrome, args.system_plist, args.opencode_root, args.bun, args.mcp_venv), baseline)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['all_metadata_matches'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
