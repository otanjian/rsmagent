"""OS boundary at the actual Bash launch, shared by foreground/background/retry.

No role, grant, approval or quota is created here. The Agent checks those before
calling the tool; launch rechecks identity/resource eligibility after any wait.
Desktop project workers already have their own OS boundary and retain it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache

from common.runtime_identity import current_identity


class ExecutionUnavailable(RuntimeError):
    pass


def inside(root, path):
    return Path(path).is_relative_to(Path(root))


def real(path):
    return str(Path(path).expanduser().resolve())


def owner():
    ident = current_identity()
    return (ident.tenant_id, ident.user_id, ident.agent_id, ident.session_id)


@dataclass(frozen=True)
class Boundary:
    cwd: str
    writable: tuple[str, ...]
    readonly: tuple[str, ...]
    excluded: tuple[str, ...]
    private: tuple[tuple[str, str], ...] = ()


def authorize():
    from auth.service import get_identity_service
    ident = current_identity()
    if not all((ident.tenant_id, ident.user_id, ident.agent_id)):
        raise ExecutionUnavailable('execution identity unavailable')
    svc = get_identity_service()
    binding = svc.get_agent_binding(ident.agent_id)
    if (not binding or binding['tenant_id'] != ident.tenant_id or
            binding.get('private_owner_user_id') not in (None, '', ident.user_id)):
        raise ExecutionUnavailable('execution agent denied')
    # check_resource_action rechecks membership, user, tenant and resource state.
    if not (svc.check_resource_action(ident.user_id, ident.tenant_id, 'tool',
                                     'builtin:bash', 'execute', permission='tool.execute')
            or svc.tenant_admin_may_execute_tool(ident.user_id, ident.tenant_id,
                                                'builtin:bash', agent_id=ident.agent_id)):
        raise ExecutionUnavailable('execution permission revoked')
    if ident.web_auth_session_id:
        import time
        rows = svc._store.execute(
            'SELECT user_id, expires_at, revoked_at FROM auth_sessions WHERE id=?',
            (ident.web_auth_session_id,))
        if not rows or rows[0]['user_id'] != ident.user_id or rows[0]['revoked_at'] or rows[0]['expires_at'] <= time.time():
            raise ExecutionUnavailable('execution session expired')
    return svc, ident



def runtime_roots():
    """Return a fresh set of read-only runtime paths; discovery is cached.

    Resolve PATH on each launch so installing/switching a tool invalidates the
    cache. Only static installations are cached, never identity or credentials.
    """
    executables = tuple(
        (name, real(path))
        for name in ('node', 'npm', 'git', 'rg', 'uv', 'ffmpeg', 'soffice')
        if (path := shutil.which(name))
    )
    interpreters = (real(sys.prefix), real(sys.base_prefix))
    if getattr(sys, 'frozen', False):
        interpreters += (real(sys.executable),)
    return set(_runtime_roots(interpreters, executables, sys.platform))


@lru_cache(maxsize=8)
def _runtime_roots(interpreters, executables, platform):
    roots = set(interpreters)
    for name, executable in executables:
        roots.add(executable)
        # npm's JS entry imports its package libraries. This covers nvm and
        # standalone Node distributions without exposing their enclosing home.
        if name == 'npm':
            package = Path(executable).parent.parent
            if package.name == 'npm' and (package / 'package.json').is_file():
                roots.add(str(package))
        for base in ('/opt/homebrew/Cellar', '/usr/local/Cellar'):
            if inside(base, executable):
                parts = Path(executable).relative_to(base).parts
                if len(parts) >= 2:
                    roots.add(str(Path(base) / parts[0] / parts[1]))
    if platform == 'darwin':
        queue = [path for path in roots if Path(path).is_file()]
        seen = set()
        while queue and len(seen) < 128:
            binary = queue.pop()
            if binary in seen:
                continue
            seen.add(binary)
            result = subprocess.run(['/usr/bin/otool', '-L', binary], capture_output=True,
                                    text=True, timeout=3, env={'PATH': '/usr/bin:/bin'})
            for line in result.stdout.splitlines()[1:]:
                library = line.strip().split(' (', 1)[0]
                if library.startswith(('/opt/homebrew/', '/usr/local/')) and Path(library).is_file():
                    library = real(library)
                    roots.add(library)
                    queue.append(library)
    return frozenset(roots)


def shipped_resources():
    source = Path(__file__).parents[2]
    return {real(source / name) for name in ('skills', 'Scene', 'scenes') if (source / name).is_dir()}

def resolve(cwd, svc, ident):
    from common import state_dir
    from agent.registry import get_agent_registry
    from config import get_data_root
    registry = get_agent_registry()
    profile = registry.get(ident.agent_id)
    shared = real(state_dir.shared_root(ident))
    workspace = real(profile.workspace)
    writable = {shared, workspace, real(state_dir.user_root(ident)),
                real(state_dir.agent_user_root(ident))}
    # Local projects use the existing Desktop worker, never arbitrary cwd grants.
    cwd = real(cwd)
    if not any(inside(root, cwd) for root in writable):
        raise ExecutionUnavailable('execution directory outside authorized roots')
    excluded = {real(get_data_root()), real(Path.home() / '.cow'),
                real(Path(__file__).parents[2] / 'config.json'),
                real(Path(shared) / 'mcp.json'), real(Path(workspace) / 'mcp.json'),
                real(Path(shared) / '.env'), real(Path(workspace) / '.env')}
    from auth.service import identity_db_path
    database = real(identity_db_path())
    excluded.update((database, database + '-wal', database + '-shm'))
    for row in svc.tenant_shared_roots():
        if row['id'] != ident.tenant_id:
            excluded.add(real(row['shared_root']))
    bindings = {row['agent_id']: row for row in svc.list_agent_bindings()}
    for other in registry.list(include_disabled=True):
        if other.id == ident.agent_id:
            continue
        binding = bindings.get(other.id, {})
        if (binding.get('tenant_id') != ident.tenant_id or
                binding.get('private_owner_user_id') not in (None, '', ident.user_id)):
            excluded.add(real(other.workspace))
    # Mask entire containers, then expose only this owner. This also denies
    # directories created by another member after the command has started.
    agents_container = str(Path(shared) / 'agents')
    private = ((str(Path(shared) / 'users'), real(state_dir.user_root(ident))),
               (str(Path(workspace) / 'user'), real(state_dir.agent_user_root(ident))),
               (agents_container, workspace if inside(agents_container, workspace) else None))
    # Never allow an instance/control root to be mounted as a business root.
    if any(inside(block, root) or inside(root, block) and root == block
           for block in excluded for root in writable):
        raise ExecutionUnavailable('execution roots overlap protected data')
    if any(inside(block, cwd) for block in excluded):
        raise ExecutionUnavailable('execution directory is private')
    readonly = runtime_roots()
    # Only shipped code/resources, not the source root containing instance data.
    readonly.update(shipped_resources())
    return Boundary(cwd, tuple(sorted(writable)), tuple(sorted(readonly)), tuple(sorted(excluded)), private)


_ENV = {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TERM', 'SSL_CERT_FILE',
        'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'HTTP_PROXY',
        'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy',
        'all_proxy', 'no_proxy', 'OPENAI_API_BASE'}
_ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_OWNED_ENV = {'HOME', 'PATH', 'TMP', 'TEMP', 'TMPDIR', 'XDG_CACHE_HOME', 'AGENT_WORKSPACE',
              'ENV', 'BASH_ENV', 'NODE_OPTIONS', 'NODE_PATH', 'RUBYOPT', 'PERL5OPT'}


def _business_name(name):
    return isinstance(name, str) and _ENV_NAME.fullmatch(name) and name not in _OWNED_ENV and not name.startswith(('COW_', 'PYTHON', 'LD_', 'DYLD_'))


def _matches(item, ident):
    return (item.get('tenant_id') == ident.tenant_id and item.get('agent_id') == ident.agent_id
            and (not item.get('user_id') or item['user_id'] == ident.user_id))


def environment(source, temporary, boundary, svc, ident):
    from config import conf
    result = {key: str(value) for key, value in source.items() if key in _ENV and value is not None}
    result.setdefault('PATH', '/usr/local/bin:/usr/bin:/bin')
    result.update(HOME=temporary, TMPDIR=temporary, TMP=temporary, TEMP=temporary,
                  XDG_CACHE_HOME=temporary + '/cache', AGENT_WORKSPACE=boundary.cwd,
                  COW_AGENT_RUN_ID=ident.run_id or '')
    secrets = {}
    # Nonsecret business settings (provider/model/endpoint names) use the same
    # explicit subject mapping; no arbitrary parent environment is broadcast.
    for item in conf().get('execution_environment', []) or []:
        if _matches(item, ident):
            for name, value in item.get('values', {}).items():
                if not _business_name(name) or not isinstance(value, str):
                    raise ExecutionUnavailable('invalid execution environment mapping')
                result[name] = value
    # Explicit deployment mappings, no wildcard subject or plaintext value.
    # The credential service is called fresh on every actual process launch.
    for item in conf().get('execution_credentials', []) or []:
        if not _matches(item, ident):
            continue
        name = item.get('env', '')
        if not _business_name(name):
            raise ExecutionUnavailable('invalid execution credential mapping')
        if item.get('resource_kind') != 'tool' or item.get('resource_id') != 'builtin:bash':
            raise ExecutionUnavailable('unsupported execution credential purpose')
        value = svc.resolve_credential(actor_user_id=ident.user_id,
            tenant_id=ident.tenant_id, name=item['credential'], resource_kind='tool', resource_id='builtin:bash')
        if item.get('field'):
            value = json.loads(value)[item['field']]
            if not isinstance(value, str):
                raise ExecutionUnavailable('credential field must be a string')
        secrets[name] = value
    result.update(secrets)
    return result, secrets


def _quoted(value):
    return json.dumps(str(value), ensure_ascii=False)


def mac_profile(boundary, temporary):
    # Same Seatbelt primitives as Desktop local-execution/sandbox.ts. Backend
    # networking remains enabled; Desktop's project network contract is separate.
    system = ['/usr', '/System', '/bin', '/sbin', '/Library/Apple', '/dev',
              '/private/etc/hosts', '/private/etc/resolv.conf', '/private/var/run/resolv.conf', '/private/etc/services',
              '/private/etc/protocols', '/private/etc/ssl/cert.pem',
              '/private/var/db/dyld', '/private/var/select',
              '/System/Volumes/Preboot']
    lines = ['(version 1)', '(deny default)', '(allow process-exec process-fork)',
             '(allow mach-lookup)',
             # Explicit deny is defense in depth, NOT evidence of process-env
             # isolation: some macOS kernels bypass this for KERN_PROCARGS2.
             # The readiness probe tests that behavior and rejects the candidate.
             '(deny sysctl-read (sysctl-name-prefix "kern.procargs"))',
             '(allow sysctl-read (sysctl-name-prefix "hw.") (sysctl-name "kern.ostype" "kern.osrelease" "kern.osversion" "kern.osproductversion" "kern.version" "kern.argmax" "kern.boottime" "kern.hostname" "kern.clockrate" "kern.hv_vmm_present"))',
             '(allow signal (target self) (target children))',
             '(allow network* (local ip) (remote ip))', '(allow file-read-metadata)', '(allow file-read* (literal "/"))']
    for root in system + list(boundary.readonly) + list(boundary.writable) + [temporary]:
        lines.append(f'(allow file-read* (subpath {_quoted(root)}))')
        for parent in Path(root).parents:
            lines.append(f'(allow file-read* (literal {_quoted(parent)}))')
    for root in list(boundary.writable) + [temporary]:
        lines.append(f'(allow network* (local unix-socket (subpath {_quoted(root)})) (remote unix-socket (subpath {_quoted(root)})))')
        lines.append(f'(allow file-write* (subpath {_quoted(root)}))')
    for file in ['/dev/null', '/dev/stdout', '/dev/stderr']:
        lines.append(f'(allow file-write* (literal {_quoted(file)}))')
    for root in boundary.readonly:
        lines.append(f'(deny file-write* (subpath {_quoted(root)}))')
    for root in boundary.excluded:
        exceptions = ''.join(f' (require-not (subpath {_quoted(runtime)}))' for runtime in boundary.readonly if inside(root, runtime) and runtime != root)
        lines.append(f'(deny file-read* (require-all (subpath {_quoted(root)}){exceptions}))')
        lines.append(f'(deny file-write* (subpath {_quoted(root)}))')
    for container, own in boundary.private:
        if own is None:
            lines.append(f'(deny file-read* file-write* (subpath {_quoted(container)}))')
            continue
        lines.append(f'(deny file-write* (require-all (subpath {_quoted(container)}) (require-not (subpath {_quoted(own)}))))')
        lines.append(f'(deny file-read* (require-all (subpath {_quoted(container)}) (require-not (subpath {_quoted(own)})) (require-not (literal {_quoted(container)}))))')
    return '\n'.join(lines)


def linux_command(boundary, temporary, command):
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise ExecutionUnavailable('bubblewrap unavailable')
    argv = [bwrap, '--die-with-parent', '--new-session', '--unshare-user',
            '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--cap-drop', 'ALL',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    # Preserve networking, but no host /run, /proc, home or data-root mount.
    for root in ['/usr', '/bin', '/sbin', '/lib', '/lib64'] + list(boundary.readonly):
        if Path(root).exists():
            argv += ['--ro-bind', root, root]
    for file in ['/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/ssl/certs', '/etc/localtime']:
        if Path(file).exists():
            argv += ['--ro-bind', file, file]
    for root in boundary.writable:
        argv += ['--bind', root, root]
    # Reassert readonly runtime mounts nested below writable business parents.
    for root in boundary.readonly:
        argv += ['--ro-bind', root, root]
    argv += ['--bind', temporary, temporary]
    for container, own in boundary.private:
        argv += ['--tmpfs', container]
        if own:
            argv += ['--bind', own, own]
    for root in boundary.excluded:
        if any(inside(parent, root) for parent in boundary.writable + boundary.readonly) and Path(root).exists():
            argv += ['--tmpfs', root] if Path(root).is_dir() else ['--ro-bind', '/dev/null', root]
    return argv + ['--chdir', boundary.cwd, '--'] + command


def popen(command, **kwargs):
    # Windows is outside this change. Desktop project workers are already
    # launched inside their verified project sandbox and install no backend hook.
    if sys.platform == 'win32':
        return subprocess.Popen(command, **kwargs)
    svc, ident = authorize()
    boundary = resolve(kwargs['cwd'], svc, ident)
    for root in boundary.writable:
        Path(root).mkdir(parents=True, exist_ok=True)
    temporary = real(tempfile.mkdtemp(prefix='cow-exec-'))
    os.chmod(temporary, 0o700)
    try:
        env, secrets = environment(kwargs.get('env') or {}, temporary, boundary, svc, ident)
        args = ['/bin/sh', '-c', command] if kwargs.pop('shell', False) else list(command)
        if sys.platform == 'darwin':
            args = ['/usr/bin/sandbox-exec', '-p', mac_profile(boundary, temporary)] + args
        elif sys.platform.startswith('linux'):
            args = linux_command(boundary, temporary, args)
        else:
            raise ExecutionUnavailable('execution platform unsupported')
        kwargs.update(env=env, close_fds=True, start_new_session=True)
        process = subprocess.Popen(args, **kwargs)
        process.execution_cleanup = lambda: shutil.rmtree(temporary, ignore_errors=True)
        process.execution_secrets = secrets
        return process
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def install():
    from agent.tools.bash import launcher
    launcher.install(popen)


def access_owner():
    authorize()
    return owner()


popen.access_owner = access_owner


def output_directory():
    if sys.platform == 'win32':
        return None
    from common.state_dir import agent_user_root
    root = agent_user_root(ensure=True)
    if root is None:
        raise ExecutionUnavailable('execution output owner unavailable')
    return str(root)


popen.output_directory = output_directory


def read_filter():
    """Freeze the current identity's boundary for one recursive file operation."""
    if sys.platform == 'win32':
        return None
    from agent.permission.isolation import resolve_boundary, _in_blocked
    boundary = resolve_boundary()
    def allowed(path):
        if not boundary.active or not boundary.read_roots:
            return False
        path = real(path)
        # Traverse private containers to reach this owner, then filter each child.
        if any(path == container and own for container, own in boundary.private):
            return True
        return not _in_blocked(boundary, path, boundary.read_roots)
    return allowed


popen.read_filter = read_filter
