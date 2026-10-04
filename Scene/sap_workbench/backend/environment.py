"""Inspect this scene's local execution environment without starting services."""
import argparse
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import unquote, urlsplit

from .configuration import WorkbenchError
from .deployment import BROWSER_SERVICE_REF, BROWSER_SERVICE_REFS


@dataclass(frozen=True)
class RuntimePaths:
    source: Path
    assets: Path
    project: Path | None
    chrome: str
    bun: str
    mcp_python: str


def chrome_executable():
    override = os.environ.get('SAP_WORKBENCH_CHROME')
    if override is not None:
        return override
    # Keep this scene's probe free of logger/config imports: those can create
    # the platform data directory even when running an offline inspection.
    if sys.platform == 'darwin':
        candidates = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                      '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                      '/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta']
    elif sys.platform == 'win32':
        candidates = [os.path.join(base, vendor, product, 'Application', binary)
                      for name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')
                      if (base := os.environ.get(name))
                      for vendor, product, binary in [('Google', 'Chrome', 'chrome.exe'), ('Microsoft', 'Edge', 'msedge.exe')]]
    else:
        candidates = [shutil.which(name) for name in ('google-chrome', 'google-chrome-stable',
                      'chromium', 'chromium-browser', 'microsoft-edge')]
    return next((path for path in candidates if path and os.path.exists(path)), '')


def runtime_paths(project, *, data_root=None):
    if data_root is None:
        from config import get_data_root
        data_root = get_data_root()
    root = Path(__file__).resolve().parents[3]
    return RuntimePaths(
        source=Path(os.environ.get('SAP_OPENCODE_ROOT', str(root.parent / 'rsmcode/opencode'))),
        assets=Path(data_root) / 'scenes/sap_workbench_assets',
        project=Path(project) if project else None,
        chrome=chrome_executable(),
        bun=shutil.which('bun') or '',
        mcp_python=os.environ.get('SAP_MCP_PYTHON', str(root.parent / 'rsmcode/sap-connect/sap-pyrfc/.venv/bin/python')),
    )


def executable_exists(value):
    return bool(value and Path(value).is_absolute() and Path(value).is_file() and os.access(value, os.X_OK))


class _Entrypoints(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.scripts = 0

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'script' and attrs.get('src'):
            self.urls.append(attrs['src'])
            self.scripts += 1
        if tag == 'link' and attrs.get('rel') in {'stylesheet', 'modulepreload'} and attrs.get('href'):
            self.urls.append(attrs['href'])


def assets_ready(root):
    """An index alone cannot render the Web UI; verify its local entry files."""
    try:
        index = root / 'index.html'
        if index.stat().st_size > 1024 * 1024:
            return False
        parser = _Entrypoints()
        parser.feed(index.read_text(encoding='utf-8'))
        if not parser.scripts:
            return False
        base = root.resolve()
        for url in parser.urls:
            parsed = urlsplit(url)
            if parsed.scheme or parsed.netloc:
                return False
            asset = (base / unquote(parsed.path).lstrip('/')).resolve()
            if base not in asset.parents or not asset.is_file() or not asset.stat().st_size:
                return False
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def local_checks(paths, browser_ref):
    """Only the registered local node is implemented; never imply remote access."""
    checks = []
    def add(name, ready, reason):
        checks.append({'id': name, 'verification': 'passed' if ready else 'failed',
                       'environment': 'local', **({} if ready else {'reason': reason})})
    node = browser_ref in BROWSER_SERVICE_REFS
    add('browser', node and executable_exists(paths.chrome),
        'browser_service_unavailable' if not node else 'browser_unavailable')
    required = ['package.json', 'packages/core/src/session.ts', 'packages/server/src/handlers.ts',
                'packages/opencode/src/server/routes/instance/httpapi/server.ts']
    source_ready = (paths.source.is_absolute() and all((paths.source / name).is_file() for name in required)
                    and (paths.source / 'node_modules').is_dir())
    add('opencode_runtime', executable_exists(paths.bun) and source_ready, 'opencode_runtime_missing')
    add('embed', assets_ready(paths.assets), 'opencode_assets_missing')
    project = paths.project
    add('project', bool(project and project.is_absolute() and project.is_dir()
                        and os.access(project, os.R_OK | os.X_OK)), 'project_directory_missing')
    add('mcp_runtime', executable_exists(paths.mcp_python), 'mcp_runtime_missing')
    return checks


def require_local_runtime(paths, browser_ref, *, require_browser=True):
    for check in local_checks(paths, browser_ref):
        # MCP remains optional for the visual scene and is checked on use.
        optional = {'mcp_runtime'} | ({'browser'} if not require_browser else set())
        if check['id'] not in optional and check['verification'] != 'passed':
            raise WorkbenchError(check['reason'], 503)


def main():
    parser = argparse.ArgumentParser(description='SAP workbench offline environment check; no SAP, model or browser requests.')
    parser.add_argument('--data-root', required=True, type=Path)
    parser.add_argument('--project', required=True)
    parser.add_argument('--browser-ref', default=BROWSER_SERVICE_REF)
    args = parser.parse_args()
    checks = local_checks(runtime_paths(args.project, data_root=args.data_root), args.browser_ref)
    print(json.dumps({'mode': 'offline_local_environment', 'network_tested': False, 'checks': checks},
                     ensure_ascii=False, indent=2))
    return 0 if all(item['verification'] == 'passed' for item in checks) else 1


if __name__ == '__main__':
    raise SystemExit(main())
