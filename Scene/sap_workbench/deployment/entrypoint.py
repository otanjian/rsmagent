"""Supervise one X display, one headed Chrome and its real RFB/noVNC bridge."""
import base64
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

RUNTIME = Path('/tmp/sap-browser')
SECRET = Path('/run/secrets/vnc_password')
DISPLAY = ':99'


def dimensions(environment):
    values = []
    for key, default, minimum, maximum in [('SAP_BROWSER_WIDTH', '1280', 640, 3840),
                                           ('SAP_BROWSER_HEIGHT', '800', 480, 2160)]:
        raw = environment.get(key, default)
        if not isinstance(raw, str) or not raw.isdecimal() or not minimum <= int(raw) <= maximum:
            raise ValueError('display dimensions are outside the supported bounds')
        values.append(int(raw))
    return tuple(values)


def validate_secret(path):
    # x11vnc -storepasswd creates an eight-byte RFB auth file. Do not accept a
    # plaintext SAP password or silently start a VNC server without auth.
    if not path.is_file() or path.stat().st_size != 8:
        raise ValueError('a mounted x11vnc auth file is required')
    # Bind-mounted Compose secrets retain host permissions. Confirm this
    # non-root process can read it before starting Xvfb or Chrome.
    with path.open('rb') as source:
        if len(source.read(9)) != 8:
            raise ValueError('a mounted x11vnc auth file is required')


def commands(width, height, runtime=RUNTIME, secret=SECRET):
    return {
        'xvfb': ['Xvfb', DISPLAY, '-screen', '0', f'{width}x{height}x24',
                 '-nolisten', 'tcp', '-auth', str(runtime / 'Xauthority')],
        'chrome': ['/opt/chrome/chrome', '--no-first-run', '--no-default-browser-check',
                   '--disable-sync', '--disable-background-networking',
                   '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9222',
                   '--user-data-dir=' + str(runtime / 'profile'),
                   f'--window-size={width},{height}', '--window-position=0,0', '--new-window', 'about:blank'],
        'x11vnc': ['x11vnc', '-display', DISPLAY, '-auth', str(runtime / 'Xauthority'),
                   '-localhost', '-rfbport', '5900', '-rfbauth', str(secret),
                   '-forever', '-shared', '-noxdamage', '-quiet'],
        # Upstream `run` is a shell launcher which changes to its package root
        # and execs python3 -m websockify. It must not be evaluated as Python.
        'websockify': ['/opt/websockify/run', '--web=/opt/novnc',
                      '--file-only', '0.0.0.0:6080', '127.0.0.1:5900'],
    }


def local_open(url):
    # Container component checks must never go through an inherited proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=1)


def chrome_ready(expected_version):
    with local_open('http://127.0.0.1:9222/json/version') as response:
        data = json.load(response)
    return data.get('Browser') == 'Chrome/' + expected_version


def rfb_ready():
    with socket.create_connection(('127.0.0.1', 5900), timeout=1) as connection:
        connection.settimeout(1)
        header = b''
        while len(header) < 12:
            chunk = connection.recv(12 - len(header))
            if not chunk:
                return False
            header += chunk
        return header in {b'RFB 003.003\n', b'RFB 003.007\n', b'RFB 003.008\n'}


def novnc_ready():
    with local_open('http://127.0.0.1:6080/vnc.html') as response:
        return response.status == 200 and b'noVNC' in response.read(65536)


def websocket_ready():
    # Verify actual WebSocket -> RFB bytes. Stop before authentication or any
    # display input, so component health never operates the browser page.
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    request = ('GET /websockify HTTP/1.1\r\nHost: 127.0.0.1:6080\r\n'
               'Upgrade: websocket\r\nConnection: Upgrade\r\n'
               'Origin: http://127.0.0.1:6080\r\nSec-WebSocket-Version: 13\r\n'
               'Sec-WebSocket-Protocol: binary\r\nSec-WebSocket-Key: ' + key + '\r\n\r\n')
    with socket.create_connection(('127.0.0.1', 6080), timeout=1) as connection:
        connection.settimeout(1)
        connection.sendall(request.encode('ascii'))
        with connection.makefile('rb') as stream:
            first = stream.readline(1024)
            if not first.startswith(b'HTTP/1.1 101 '):
                return False
            headers, size = {}, len(first)
            while size < 16384:
                line = stream.readline(1024)
                size += len(line)
                if line == b'\r\n':
                    break
                if not line or b':' not in line:
                    return False
                name, value = line.split(b':', 1)
                headers[name.strip().lower()] = value.strip()
            else:
                return False
            accepted = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            if headers.get(b'sec-websocket-accept') != accepted or headers.get(b'upgrade', b'').lower() != b'websocket':
                return False
            banner = b''
            while len(banner) < 12:
                frame = stream.read(2)
                # websockify can receive the TCP banner in several chunks;
                # each becomes a complete binary WebSocket frame. Bound both
                # the total bytes and frame count without accepting masking,
                # text frames, oversized payloads or an incomplete banner.
                if len(frame) != 2 or frame[0] != 0x82 or not 0 < frame[1] <= 12 - len(banner):
                    return False
                data = stream.read(frame[1])
                if len(data) != frame[1]:
                    return False
                banner += data
            return banner in {b'RFB 003.003\n', b'RFB 003.007\n', b'RFB 003.008\n'}


def x_ready(environment):
    return subprocess.run(['xdpyinfo', '-display', DISPLAY], env=environment,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2).returncode == 0


class Supervisor:
    def __init__(self, *, environment, popen=subprocess.Popen, clock=time.monotonic,
                 sleep=time.sleep, killpg=os.killpg, stopped=None):
        self.environment = environment
        self.popen, self.clock, self.sleep, self.killpg = popen, clock, sleep, killpg
        self.stopped = stopped if stopped is not None else threading.Event()
        self.processes = {}

    def start(self, name, command):
        if self.stopped.is_set():
            raise RuntimeError('display startup timed out or was stopped')
        self.processes[name] = self.popen(command, env=self.environment, start_new_session=True)

    def alive(self):
        return all(process.poll() is None for process in self.processes.values())

    def wait_ready(self, ready, seconds):
        deadline = self.clock() + seconds
        while self.clock() < deadline and not self.stopped.is_set():
            if not self.alive():
                raise RuntimeError('a display component exited during startup')
            try:
                if ready():
                    if self.stopped.is_set():
                        raise RuntimeError('display startup timed out or was stopped')
                    return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            self.sleep(.1)
        raise RuntimeError('display startup timed out or was stopped')

    def monitor(self):
        while not self.stopped.is_set():
            if not self.alive():
                raise RuntimeError('a display component exited')
            self.sleep(.2)

    def _signal_group(self, process, sig):
        # Darwin can briefly report EPERM while an orphan's process group is
        # disappearing after TERM. Retry the same owned group for at most
        # 40ms; a persistent permission failure is never treated as cleanup.
        for attempt in range(5):
            try:
                self.killpg(process.pid, sig)
                return
            except ProcessLookupError:
                return
            except PermissionError:
                if attempt == 4:
                    raise
                self.sleep(.01)

    def stop(self):
        self.stopped.set()
        processes = list(reversed(list(self.processes.values())))
        failures = []
        for process in processes:
            # The Chrome parent can have exited while its renderers remain;
            # signal its original process group even when poll() is non-None.
            try:
                self._signal_group(process, signal.SIGTERM)
            except OSError as error:
                failures.append(error)
        deadline = self.clock() + 8
        for process in processes:
            try:
                process.wait(timeout=max(.01, deadline - self.clock()))
            except subprocess.TimeoutExpired:
                pass
            except (OSError, subprocess.SubprocessError) as error:
                failures.append(error)
        for process in processes:
            try:
                self._signal_group(process, signal.SIGKILL)
            except OSError as error:
                failures.append(error)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            except (OSError, subprocess.SubprocessError) as error:
                failures.append(error)
        if failures:
            # Finish every owned group's cleanup before reporting failure.
            # A fixed message cannot expose a command line or environment.
            raise OSError('display process group cleanup failed') from failures[0]


def start_unit(supervisor, plan, version):
    supervisor.start('xvfb', plan['xvfb'])
    supervisor.wait_ready(lambda: x_ready(supervisor.environment), 15)
    supervisor.start('chrome', plan['chrome'])
    supervisor.wait_ready(lambda: chrome_ready(version), 45)
    supervisor.start('x11vnc', plan['x11vnc'])
    supervisor.wait_ready(rfb_ready, 15)
    supervisor.start('websockify', plan['websockify'])
    supervisor.wait_ready(lambda: novnc_ready() and websocket_ready(), 15)


def main():
    if os.geteuid() == 0:
        raise ValueError('run the browser display as its non-root container user')
    width, height = dimensions(os.environ)
    validate_secret(SECRET)
    version = json.loads(Path(__file__).with_name('browser-versions.json').read_text())['chrome']['version']
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = {**os.environ, 'DISPLAY': DISPLAY, 'XAUTHORITY': str(RUNTIME / 'Xauthority'),
                   'XDG_CONFIG_HOME': str(RUNTIME / 'config'), 'XDG_CACHE_HOME': str(RUNTIME / 'cache')}
    auth = RUNTIME / 'Xauthority'
    auth.touch(mode=0o600, exist_ok=True)
    subprocess.run(['xauth', '-f', str(auth), 'add', DISPLAY, 'MIT-MAGIC-COOKIE-1', secrets.token_hex(16)],
                   env=environment, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    supervisor = Supervisor(environment=environment)
    signal.signal(signal.SIGTERM, lambda *_: supervisor.stopped.set())
    signal.signal(signal.SIGINT, lambda *_: supervisor.stopped.set())
    try:
        start_unit(supervisor, commands(width, height), version)
        (RUNTIME / 'processes.json').write_text(json.dumps({name: proc.pid for name, proc in supervisor.processes.items()}))
        print('SAP browser display ready: headed Chrome, Xvfb, RFB and noVNC', flush=True)
        supervisor.monitor()
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        # No command lines, environment or upstream page data in this error.
        print('SAP browser display failed: ' + str(error), file=sys.stderr)
        return 1
    finally:
        (RUNTIME / 'processes.json').unlink(missing_ok=True)
        supervisor.stop()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError):
        print('SAP browser display configuration failed', file=sys.stderr)
        sys.exit(1)
