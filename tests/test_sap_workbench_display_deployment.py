"""Standalone Linux display deployment checks; no Docker or browser process."""
import base64
import hashlib
import importlib.util
import io
import json
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / 'Scene/sap_workbench/deployment'


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


entry = module('sap_display_entry', 'entrypoint.py')
assets = module('sap_display_assets', 'fetch-browser-assets.py')


def test_compose_exposes_only_loopback_novnc_and_keeps_components_optional():
    compose = yaml.safe_load((DEPLOY / 'browser-compose.yaml').read_text())
    browser = compose['services']['browser']
    assert list(compose['services']) == ['browser']
    assert browser['ports'] == ['127.0.0.1:${SAP_WORKBENCH_NOVNC_PORT:-6080}:6080']
    assert browser['user'] == '10001:10001'
    assert browser['read_only'] and browser['init'] and browser['cap_drop'] == ['ALL']
    assert 'no-new-privileges:true' in browser['security_opt']
    assert 'seccomp=./chrome-seccomp.json' in browser['security_opt']
    assert browser['restart'] == 'no'
    assert 'volumes' not in browser and 'network_mode' not in browser and 'privileged' not in browser
    assert ':?' in compose['secrets']['vnc_password']['file']
    assert browser['mem_limit'] == '2g' and browser['pids_limit'] == 256


def test_upstream_resources_and_architecture_bases_are_pinned():
    versions = json.loads((DEPLOY / 'browser-versions.json').read_text())
    dockerfile = (DEPLOY / 'browser.Dockerfile').read_text()
    assert versions['runtime_verified'] is False and versions['workbench_binding_verified'] is False
    for image in versions['base_images'].values():
        assert '@sha256:' in image and image in dockerfile
    for package, version in versions['debian_packages'].items():
        assert package + '=' + version in dockerfile
    assert versions['debian_snapshot'] in dockerfile
    assert 'trusted=yes' not in dockerfile and '--allow-unauthenticated' not in dockerfile
    for spec in versions['chrome']['archives'].values():
        assert 'generation=' + spec['generation'] in spec['url']
        assert len(base64.b64decode(spec['md5_base64'], validate=True)) == 16
    profile = (DEPLOY / 'chrome-seccomp.json').read_bytes()
    assert hashlib.sha256(profile).hexdigest() == versions['seccomp']['sha256']
    assert json.loads(profile)['defaultAction'] == 'SCMP_ACT_ERRNO'


def test_command_plan_has_one_headed_browser_and_a_real_rfb_bridge(tmp_path):
    plan = entry.commands(1280, 800, tmp_path, tmp_path / 'auth')
    assert list(plan) == ['xvfb', 'chrome', 'x11vnc', 'websockify']
    assert '1280x800x24' in plan['xvfb']
    assert plan['chrome'][-1] == 'about:blank'
    assert '--remote-debugging-address=127.0.0.1' in plan['chrome']
    assert plan['x11vnc'][plan['x11vnc'].index('-display') + 1] == ':99'
    assert plan['websockify'][-2:] == ['0.0.0.0:6080', '127.0.0.1:5900']
    assert plan['websockify'][0] == '/opt/websockify/run', 'upstream run is a shell module launcher'
    assert '--file-only' in plan['websockify']
    assert '-localhost' in plan['x11vnc'] and '-rfbauth' in plan['x11vnc']
    for command in plan.values():
        assert not any(flag in command for flag in ['--headless', '--no-sandbox', '--ignore-certificate-errors', '-nopw'])


@pytest.mark.parametrize('values', [{'SAP_BROWSER_WIDTH': '1'}, {'SAP_BROWSER_HEIGHT': '10000'},
                                   {'SAP_BROWSER_WIDTH': 'abc'}, {'SAP_BROWSER_HEIGHT': '800;echo bad'}])
def test_display_dimensions_reject_bad_input(values):
    with pytest.raises(ValueError):
        entry.dimensions(values)


def test_display_dimensions_have_bounded_defaults():
    assert entry.dimensions({}) == (1280, 800)
    assert entry.dimensions({'SAP_BROWSER_WIDTH': '3840', 'SAP_BROWSER_HEIGHT': '2160'}) == (3840, 2160)


@pytest.mark.parametrize('data', [None, b'', b'plaintext-SAP-password', b'x' * 9])
def test_display_never_starts_without_an_rfb_auth_file(tmp_path, data):
    path = tmp_path / 'secret'
    if data is not None:
        path.write_bytes(data)
    with pytest.raises(ValueError):
        entry.validate_secret(path)


def test_display_accepts_a_binary_rfb_auth_file(tmp_path):
    path = tmp_path / 'secret'
    path.write_bytes(bytes(range(8)))
    entry.validate_secret(path)


def test_bind_mount_secret_permissions_are_checked_before_starting_components(tmp_path, monkeypatch):
    path = tmp_path / 'secret'
    path.write_bytes(bytes(range(8)))
    original_open = Path.open
    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError('container user cannot read mounted secret')
        return original_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', unreadable)
    with pytest.raises(PermissionError):
        entry.validate_secret(path)


class Process:
    def __init__(self, pid, *, code=None, stuck=False):
        self.pid, self.code, self.stuck = pid, code, stuck
        self.waits = []

    def poll(self):
        return self.code

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.stuck:
            raise subprocess.TimeoutExpired('stub', timeout)
        self.code = 0
        return self.code


def supervisor():
    commands, killed, elapsed = [], [], [0]
    stopped = threading.Event()
    def popen(command, **options):
        assert options['start_new_session'] is True
        commands.append(command)
        return Process(1000 + len(commands))
    def sleep(seconds):
        elapsed[0] += seconds
    value = entry.Supervisor(environment={'DISPLAY': ':99'}, popen=popen, clock=lambda: elapsed[0],
                             sleep=sleep, killpg=lambda *args: killed.append(args), stopped=stopped)
    return value, commands, killed, elapsed


def test_component_startup_checks_each_real_protocol_in_order(monkeypatch):
    value, commands, _, _ = supervisor()
    checks = []
    for name in ['x_ready', 'chrome_ready', 'rfb_ready', 'novnc_ready', 'websocket_ready']:
        monkeypatch.setattr(entry, name, lambda *args, key=name: checks.append(key) or True)
    plan = entry.commands(1280, 800)
    entry.start_unit(value, plan, '154.0.8037.92')
    assert commands == list(plan.values())
    assert checks == ['x_ready', 'chrome_ready', 'rfb_ready', 'novnc_ready', 'websocket_ready']


def test_startup_failure_does_not_skip_readiness_or_leave_started_groups(monkeypatch):
    value, commands, killed, elapsed = supervisor()
    monkeypatch.setattr(entry, 'x_ready', lambda *_: True)
    monkeypatch.setattr(entry, 'chrome_ready', lambda *_: False)
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            entry.start_unit(value, entry.commands(1280, 800), '154.0.8037.92')
    finally:
        value.stop()
    assert len(commands) == 2
    assert elapsed[0] < 46
    assert killed[:2] == [(1002, signal.SIGTERM), (1001, signal.SIGTERM)]
    assert killed[2:] == [(1002, signal.SIGKILL), (1001, signal.SIGKILL)]


def test_early_component_exit_fails_instead_of_serving_a_partial_display():
    value, _, _, _ = supervisor()
    value.start('xvfb', ['stub'])
    value.processes['xvfb'].code = 1
    with pytest.raises(RuntimeError, match='exited during startup'):
        value.wait_ready(lambda: True, 15)


def test_component_crash_after_startup_is_detected():
    value, _, _, _ = supervisor()
    value.start('chrome', ['stub'])
    value.processes['chrome'].code = 1
    with pytest.raises(RuntimeError, match='component exited'):
        value.monitor()


def test_shutdown_is_bounded_and_kills_remaining_children_of_an_exited_parent():
    value, _, killed, _ = supervisor()
    value.start('xvfb', ['stub'])
    value.start('chrome', ['stub'])
    value.processes['chrome'].code = 1
    value.processes['xvfb'].stuck = True
    value.stop()
    assert (1002, signal.SIGKILL) in killed and (1001, signal.SIGKILL) in killed
    assert all(0 < timeout <= 8 for p in value.processes.values() for timeout in p.waits)
    value.stopped.set()
    value.monitor()


def test_shutdown_retries_permission_race_only_until_owned_group_disappears():
    value, _, killed, elapsed = supervisor()
    value.start('fixture', ['stub'])
    attempts = []
    def racing_signal(group, sig):
        attempts.append((group, sig))
        if len(attempts) < 3:
            raise PermissionError('temporary process group race')
        raise ProcessLookupError('process group disappeared')
    value.killpg = racing_signal
    value.stop()
    assert len(attempts) == 4 and elapsed[0] == .02
    assert value.stopped.is_set()


@pytest.mark.parametrize('phase', ['term', 'kill', 'wait'])
def test_one_cleanup_error_never_skips_other_groups_or_reports_success(phase):
    value, _, killed, elapsed = supervisor()
    value.start('other', ['stub'])
    value.start('denied', ['stub'])
    process = value.processes['denied']
    def signal_group(group, sig):
        killed.append((group, sig))
        if group == process.pid and ((phase == 'term' and sig == signal.SIGTERM) or
                                     (phase == 'kill' and sig == signal.SIGKILL)):
            raise PermissionError('fixture denied')
    if phase == 'wait':
        process.wait = lambda timeout: (_ for _ in ()).throw(OSError('fixture wait failed'))
    value.killpg = signal_group
    with pytest.raises(OSError, match='^display process group cleanup failed$') as error:
        value.stop()
    assert isinstance(error.value.__cause__, OSError)
    assert value.processes['other'].code == 0
    assert (1001, signal.SIGTERM) in killed and (1001, signal.SIGKILL) in killed
    assert elapsed[0] <= .04


def test_chrome_readiness_rejects_wrong_version_or_a_headless_shell(monkeypatch):
    for browser, expected in [('Chrome/154.0.8037.92', True), ('Chrome/154.0.8037.95', False),
                               ('HeadlessChrome/154.0.8037.92', False)]:
        monkeypatch.setattr(entry, 'local_open', lambda _: io.BytesIO(json.dumps({'Browser': browser}).encode()))
        assert entry.chrome_ready('154.0.8037.92') is expected


@pytest.mark.parametrize('chunks,expected', [([b'RFB ', b'003.008\n'], True),
                                           ([b'RFB 003.ABC\n'], False), ([b'HTTP/1.1 200'], False),
                                           ([b'RFB ', b''], False)])
def test_rfb_readiness_requires_a_complete_real_protocol_banner(monkeypatch, chunks, expected):
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def settimeout(self, value): assert value == 1
        def recv(self, _): return chunks.pop(0) if chunks else b''
    def connect(address, timeout):
        assert address == ('127.0.0.1', 5900) and timeout == 1
        return Connection()
    monkeypatch.setattr(entry.socket, 'create_connection', connect)
    assert entry.rfb_ready() is expected


def test_component_health_requires_all_pids_and_real_display_protocols(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'entrypoint', entry)
    health = module('sap_display_health', 'healthcheck.py')
    monkeypatch.setattr(health, 'RUNTIME', tmp_path)
    state = {'xvfb': 101, 'chrome': 102, 'x11vnc': 103, 'websockify': 104}
    (tmp_path / 'processes.json').write_text(json.dumps(state))
    checked = []
    monkeypatch.setattr(health.os, 'kill', lambda pid, sig: checked.append((pid, sig)))
    monkeypatch.setattr(health, 'chrome_ready', lambda version: version == '154.0.8037.92')
    monkeypatch.setattr(health, 'rfb_ready', lambda: True)
    monkeypatch.setattr(health, 'novnc_ready', lambda: True)
    monkeypatch.setattr(health, 'websocket_ready', lambda: True)
    assert health.healthy() is True
    assert checked == [(101, 0), (102, 0), (103, 0), (104, 0)]
    monkeypatch.setattr(health, 'rfb_ready', lambda: False)
    assert health.healthy() is False
    state['chrome'] = 0
    (tmp_path / 'processes.json').write_text(json.dumps(state))
    assert health.healthy() is False
    state.pop('websockify')
    (tmp_path / 'processes.json').write_text(json.dumps(state))
    assert health.healthy() is False


@pytest.mark.parametrize('mutation,expected', [('none', True), ('split', True), ('http', False), ('accept', False),
                                              ('upgrade', False), ('text', False), ('masked', False),
                                              ('length', False), ('banner', False)])
def test_websocket_health_proves_binary_rfb_bridge_before_authentication(monkeypatch, mutation, expected):
    written = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def settimeout(self, value): assert value == 1
        def sendall(self, data): written.append(data)
        def makefile(self, mode):
            assert mode == 'rb'
            key = written[0].decode().split('Sec-WebSocket-Key: ')[1].split('\r\n')[0]
            accepted = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            if mutation == 'accept': accepted = b'wrong'
            response = (b'HTTP/1.1 200 OK\r\n' if mutation == 'http' else b'HTTP/1.1 101 Switching Protocols\r\n')
            response += b'Upgrade: ' + (b'http' if mutation == 'upgrade' else b'websocket') + b'\r\n'
            response += b'Sec-WebSocket-Accept: ' + accepted + b'\r\n\r\n'
            if mutation == 'split':
                response += b'\x82\x04RFB \x82\x08003.008\n'
            else:
                response += bytes([0x81 if mutation == 'text' else 0x82, 0x8c if mutation == 'masked' else 11 if mutation == 'length' else 12])
                response += b'HTTP/1.1 200' if mutation == 'banner' else b'RFB 003.008\n'
            return io.BytesIO(response)
    def connect(address, timeout):
        assert address == ('127.0.0.1', 6080) and timeout == 1
        return Connection()
    monkeypatch.setattr(entry.socket, 'create_connection', connect)
    assert entry.websocket_ready() is expected
    assert len(written) == 1, 'health stops before sending RFB auth or browser input'


def test_download_checks_pinned_integrity_and_discards_corrupt_artifacts(tmp_path, monkeypatch):
    data = b'upstream artifact'
    monkeypatch.setattr(assets.urllib.request, 'urlopen', lambda *_, **__: io.BytesIO(data))
    path = tmp_path / 'artifact'
    spec = {'url': 'https://official.example/artifact', 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    assets.download(spec, path)
    assert path.read_bytes() == data
    for change in [{'sha256': '0' * 64}, {'size': len(data) + 1}]:
        with pytest.raises(ValueError, match='checksum or size'):
            assets.download({**spec, **change}, path)
        assert not path.exists()
    md5 = {'url': spec['url'], 'size': len(data), 'md5_base64': base64.b64encode(hashlib.md5(data).digest()).decode()}
    assets.download(md5, path)
    assert path.read_bytes() == data


@pytest.mark.parametrize('name', ['/absolute/file', 'source/../outside', 'other/file'])
def test_archive_paths_cannot_escape_the_pinned_top_directory(name):
    with pytest.raises(ValueError):
        assets.relative_path(name, 'source')


def test_zip_unpack_preserves_executable_bits_without_privilege_bits(tmp_path):
    archive = tmp_path / 'zip'
    with zipfile.ZipFile(archive, 'w') as source:
        item = zipfile.ZipInfo('source/browser')
        item.external_attr = (stat.S_IFREG | 0o4755) << 16
        source.writestr(item, b'browser')
    target = tmp_path / 'unpacked'
    assets.unpack(archive, target, 'source', zipped=True)
    assert (target / 'browser').read_bytes() == b'browser'
    assert (target / 'browser').stat().st_mode & 0o7777 == 0o755


@pytest.mark.parametrize('zipped', [True, False])
def test_archive_links_are_rejected(tmp_path, zipped):
    archive = tmp_path / 'archive'
    if zipped:
        with zipfile.ZipFile(archive, 'w') as source:
            item = zipfile.ZipInfo('source/link')
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            source.writestr(item, '/etc/passwd')
    else:
        with tarfile.open(archive, 'w:gz') as source:
            item = tarfile.TarInfo('source/link')
            item.type = tarfile.SYMTYPE
            item.linkname = '/etc/passwd'
            source.addfile(item)
    with pytest.raises(ValueError, match='link'):
        assets.unpack(archive, tmp_path / 'destination', 'source', zipped=zipped)


def test_websockify_same_directory_aliases_remain_inside_the_archive(tmp_path):
    archive = tmp_path / 'archive'
    with tarfile.open(archive, 'w:gz') as source:
        item = tarfile.TarInfo('source/run')
        item.size = 3
        source.addfile(item, io.BytesIO(b'run'))
        alias = tarfile.TarInfo('source/websockify.py')
        alias.type = tarfile.SYMTYPE
        alias.linkname = 'run'
        source.addfile(alias)
    destination = tmp_path / 'destination'
    assets.unpack(archive, destination, 'source')
    assert (destination / 'websockify.py').is_symlink()
    assert (destination / 'websockify.py').read_bytes() == b'run'
