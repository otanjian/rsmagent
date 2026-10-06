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


MCP_PYTHON_ENV = 'SAP_MCP_PYTHON'
#: The MCP worker runs from the SAP connectivity checkout's own virtualenv, so
#: its interpreter is platform-specific: POSIX keeps ``bin/python`` while Windows
#: installs ``Scripts/python.exe``. Defaulting to the POSIX layout alone makes
#: every Windows deployment report ``mcp_runtime_missing``, which disables the
#: whole configured read-only MCP path -- the same class of POSIX-only
#: assumption :data:`SUBPROCESS_ENV_KEEP` was written to remove. An explicit
#: :data:`MCP_PYTHON_ENV` stays authoritative so a wrong override is reported
#: rather than silently replaced.
MCP_VENV_RELATIVE = 'rsmcode/sap-connect/sap-pyrfc/.venv'
MCP_PYTHON_NAMES = {'win32': ('Scripts/python.exe', 'bin/python'), 'default': ('bin/python', 'Scripts/python.exe')}


def mcp_python_candidates(platform=None):
    """Interpreter candidates for this platform, most specific first."""
    root = Path(__file__).resolve().parents[3]
    venv = root.parent / MCP_VENV_RELATIVE
    names = MCP_PYTHON_NAMES.get(platform or sys.platform, MCP_PYTHON_NAMES['default'])
    return [str(venv / name) for name in names]


def mcp_python():
    override = os.environ.get(MCP_PYTHON_ENV)
    if override is not None:
        return override
    candidates = mcp_python_candidates()
    # Falling back to this platform's expected path keeps mcp_runtime_missing
    # naming a path that would actually work once the venv is provisioned.
    return next((path for path in candidates if os.path.isfile(path)), candidates[0])


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
        mcp_python=mcp_python(),
    )


def executable_exists(value):
    return bool(value and Path(value).is_absolute() and Path(value).is_file() and os.access(value, os.X_OK))


#: Environment names a spawned execution process legitimately needs. Platform,
#: SAP and provider credentials are never inherited by a child. The Windows
#: system locations are not optional plumbing: without ``SystemRoot`` the loader
#: cannot resolve the DLLs Bun is linked against, and the host exits before it
#: can report a port. A POSIX-only allowlist silently breaks every Windows
#: deployment, so the same names the MCP stdio toolchain keeps are kept here.
SUBPROCESS_ENV_KEEP = (
    'PATH', 'HOME', 'TMPDIR', 'LANG',
    'SYSTEMROOT', 'SystemRoot', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT',
    'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS',
    'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA',
    'TEMP', 'TMP', 'HOMEDRIVE', 'HOMEPATH',
)


def subprocess_env():
    """The sanitized environment for this scene's child processes."""
    return {key: os.environ[key] for key in SUBPROCESS_ENV_KEEP if key in os.environ}


#: The user profile whose OpenCode stores a workbench session must share.
#: OpenCode keeps provider credentials (``.local/share/opencode/auth.json``),
#: the provider/MCP configuration (``.config/opencode/opencode.jsonc``) and
#: session state under the user profile, and resolves all of them from
#: ``os.homedir()``/``$HOME`` via ``xdg-basedir``.
PROFILE_ENV = 'SAP_WORKBENCH_OPENCODE_PROFILE'
#: The profile this deployment installs OpenCode under, used only when the
#: inherited home is a service account's. Set :data:`PROFILE_ENV` on a machine
#: that installs under a different user.
PROFILE_DEFAULT = r'C:\Users\Administrator'
#: Home directories Windows hands to service accounts. They are created empty,
#: so they never hold the OpenCode install the console and its Web UI use.
SERVICE_HOMES = ('systemprofile', 'localservice', 'networkservice')


def opencode_home():
    """The profile directory whose OpenCode stores a workbench session reads.

    The console runs as a service account, whose profile holds none of them:
    a session started there sees only the public model catalogue, but the free
    tier refuses to serve it, so every run fails with "OpenCode's free tier can
    only be used from within OpenCode". Pin the same profile the standalone
    instance uses (``scripts/start-opencode-web.ps1`` does the same) so both
    read one set of credentials, providers and MCP servers.

    Only a service account's own profile is replaced. A developer or desktop
    session already points at the profile that holds the stores, and moving it
    would put the user's own credentials out of reach.
    """
    configured = (os.environ.get(PROFILE_ENV) or '').strip()
    if configured:
        return configured
    current = os.environ.get('USERPROFILE') or os.environ.get('HOME') or ''
    lowered = current.lower()
    if current and not any(name in lowered for name in SERVICE_HOMES):
        return current
    # The default only applies where it exists, so a deployment that installs
    # under another user keeps the profile it inherited instead of being
    # silently pointed at a directory that is not there.
    return PROFILE_DEFAULT if Path(PROFILE_DEFAULT).is_dir() else current


#: Bun reuses already-transpiled modules across runs when this directory is
#: set. Every session host dynamically imports -- and therefore re-transpiles --
#: the whole OpenCode TypeScript source tree, and that transpile is the dominant
#: part of the create request the user waits on. Measured on the deployment
#: host, a cold file cache start took about 19s against about 3.4s warm. One
#: shared directory lets a later session -- and the catalog warm-up -- reuse
#: what an earlier start already produced, so the work survives even when the
#: warm-up process itself is cancelled by the session that follows it.
TRANSPILER_CACHE_ENV = 'BUN_RUNTIME_TRANSPILER_CACHE_PATH'
#: Scene-side switch: ``0/false/no/off`` leaves the variable unset and restores
#: the previous behaviour (no reuse between starts).
TRANSPILER_CACHE_SWITCH = 'SAP_WORKBENCH_TRANSPILER_CACHE'
#: The shared cache lives beside the sessions it serves, under the runtime root,
#: which the deployment already excludes from Git alongside the other runtime
#: state.
TRANSPILER_CACHE_DIRNAME = '.transpiler-cache'


def transpiler_cache_enabled():
    raw = os.environ.get(TRANSPILER_CACHE_SWITCH)
    if raw is None:
        return True
    return raw.strip().lower() not in {'0', 'false', 'no', 'off'}


def transpiler_cache_dir(data_root=None):
    """The shared Bun transpiler cache directory for one data root."""
    if data_root is None:
        from config import get_data_root
        data_root = get_data_root()
    return Path(data_root) / 'scenes/sap_workbench_runtime' / TRANSPILER_CACHE_DIRNAME


def opencode_host_env(cache_dir=None):
    """The sanitized child environment with the OpenCode profile pinned.

    Same names as :func:`subprocess_env`, so the model execution process still
    inherits no platform or SAP credential of its own; it only stops resolving
    its own OpenCode stores to the service account's empty profile.

    ``cache_dir`` points Bun's transpiler cache at a directory shared by every
    session, so a start reuses what an earlier start already transpiled instead
    of parsing the engine again. The directory is created here -- the cache is
    only useful once something has written to it -- and a failure to create it
    is ignored rather than allowed to fail the start it is only optimising.
    """
    env = subprocess_env()
    home = opencode_home()
    if home:
        env['HOME'] = home
        env['USERPROFILE'] = home
        drive, tail = os.path.splitdrive(home)
        if drive:
            env['HOMEDRIVE'], env['HOMEPATH'] = drive, tail or '\\'
    if cache_dir and transpiler_cache_enabled():
        try:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            env[TRANSPILER_CACHE_ENV] = str(cache_dir)
        except OSError:
            pass
    return env


#: Deployment setting for remote display: the https origin (scheme + host) the
#: public entry fronts, e.g. ``https://rd.rsmxm.com.cn``. The browser address is
#: this origin plus :func:`base_path`, so every session lives under one fixed
#: path prefix on the console's own domain -- no extra DNS record or certificate.
#: Unset means "loopback only", which is the default every existing deployment
#: keeps.
PUBLIC_BASE_ENV = 'SAP_WORKBENCH_PUBLIC_BASE'
#: The path prefix the workbench is published under, e.g. ``/sapcode``. It is a
#: deployment-wide constant: the OpenCode assets are built with this base and
#: the runtime strips the same prefix, so both must agree.
BASE_PATH_ENV = 'SAP_WORKBENCH_BASE_PATH'
BASE_PATH_DEFAULT = '/sapcode'
#: Loopback port the public entry proxy forwards to. Only used with a public base.
PROXY_PORT_ENV = 'SAP_WORKBENCH_PROXY_PORT'
PROXY_PORT_DEFAULT = 9911


def base_path():
    """The deployment-wide path prefix, ``''`` for the root.

    ``/sapcode`` by default, overridable by :data:`BASE_PATH_ENV`; an empty
    string means the workbench is served at the root (no prefix). The value is
    the base the assets are built with and the prefix the runtime strips, so it
    is a single source of truth for both. Any other value carries a leading ``/``
    and no trailing one.
    """
    raw = (os.environ.get(BASE_PATH_ENV) or BASE_PATH_DEFAULT).strip()
    if raw and not raw.startswith('/'):
        raw = '/' + raw
    return raw.rstrip('/')


def public_origin():
    """The validated remote origin, or ``''`` when remote display is off.

    An origin that cannot be trusted is refused rather than half-honoured:
    remote display hands a session's browser origin to whoever reaches the
    public entry, so it must be an explicit https origin with no path -- the
    path prefix comes from :func:`base_path` and is never part of the origin.
    """
    raw = (os.environ.get(PUBLIC_BASE_ENV) or '').strip().rstrip('/')
    if not raw:
        return ''
    parsed = urlsplit(raw)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('%s must be an https origin with no path' % PUBLIC_BASE_ENV)
    if '{binding}' in raw:
        raise ValueError('%s: the {binding} subdomain form is not supported' % PUBLIC_BASE_ENV)
    return f'{parsed.scheme}://{parsed.netloc}'


def public_base():
    """The full remote base (origin + path prefix), or ``''`` when off."""
    origin = public_origin()
    return origin + base_path() if origin else ''


def proxy_port():
    """The fixed loopback port the public entry proxy targets."""
    raw = (os.environ.get(PROXY_PORT_ENV) or '').strip()
    if not raw:
        return PROXY_PORT_DEFAULT
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError('%s is outside the port range' % PROXY_PORT_ENV)
    return port


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
        prefix = base_path()
        for url in parser.urls:
            parsed = urlsplit(url)
            if parsed.scheme or parsed.netloc:
                return False
            path = unquote(parsed.path)
            # Assets are built with base=base_path (e.g. /sapcode/) but stored on
            # disk without that prefix, exactly like the runtime strips it when
            # serving. Strip it here too or every entry resolves one level deep
            # and the whole bundle is reported missing.
            if prefix and (path == prefix or path.startswith(prefix + '/')):
                path = path[len(prefix):] or '/'
            asset = (base / path.lstrip('/')).resolve()
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
