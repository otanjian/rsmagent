"""Opt-in actual pinned noVNC/websockify startup, not a container/UI test."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / 'Scene/sap_workbench/deployment'
pytestmark = [
    pytest.mark.skipif(os.environ.get('SAP_DISPLAY_UPSTREAM_SMOKE') != '1',
        reason='Explicit opt-in downloads checksum-pinned official upstream assets and starts only fixture loopback services'),
    pytest.mark.skipif(os.name != 'posix', reason='The independent display fixture owns POSIX process groups'),
]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class RfbFixture(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        self.request.sendall(b'RFB 003.008\n')
        try:
            received = self.request.recv(12)
            if received:
                self.server.received.append(received)
                self.request.sendall(b'fixture-echo:' + received)
        except (TimeoutError, ConnectionError):
            pass


class FixtureServer(socketserver.ThreadingTCPServer):
    daemon_threads = True


def receive(stream, size):
    data = b''
    while len(data) < size:
        part = stream.read(size - len(data))
        assert part, 'Fixture connection ended before the expected protocol bytes'
        data += part
    return data


def websocket(stream):
    first, second = receive(stream, 2)
    assert first == 0x82 and not second & 0x80
    length = second & 0x7f
    if length == 126: length = struct.unpack('!H', receive(stream, 2))[0]
    assert length <= 128
    return receive(stream, length)


def test_actual_pinned_websockify_and_novnc_fixture_startup(monkeypatch):
    with tempfile.TemporaryDirectory(prefix='sap-display-upstream-fixture-') as temporary:
        verify_upstream_fixture(Path(temporary), monkeypatch)


def verify_upstream_fixture(tmp_path, monkeypatch):
    assets = load('sap_display_upstream_assets', 'fetch-browser-assets.py')
    entry = load('sap_display_upstream_entry', 'entrypoint.py')
    versions = json.loads((DEPLOY / 'browser-versions.json').read_text())
    for name in ('novnc', 'websockify'):
        spec = versions[name]
        archive = tmp_path / (name + '.tar.gz')
        assets.download(spec, archive)
        assets.unpack(archive, tmp_path / name, spec['prefix'])
    launch = tmp_path / 'websockify' / 'run'
    # Upstream's shell entrypoint executes python3. Use the same interpreter as
    # this isolated test without modifying the user's global PATH or packages.
    environment = {'PATH': str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}
    help_result = subprocess.run([str(launch), '--help'], env=environment,
        capture_output=True, text=True, timeout=5)
    assert help_result.returncode == 0 and '--file-only' in help_result.stdout
    with FixtureServer(('127.0.0.1', 0), RfbFixture) as server:
        server.received = []
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        output = (tmp_path / 'bridge.log').open('w+')
        process = None
        try:
            process = subprocess.Popen([str(launch), '--web=' + str(tmp_path / 'novnc'), '--file-only',
                '127.0.0.1:' + str(port), '127.0.0.1:' + str(server.server_address[1])],
                env=environment, stdout=output, stderr=output, start_new_session=True)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                assert process.poll() is None, 'Pinned upstream component exited during fixture startup'
                try:
                    with opener.open('http://127.0.0.1:' + str(port) + '/vnc.html', timeout=.2) as response:
                        assert response.status == 200 and b'noVNC' in response.read(65536)
                    break
                except OSError: time.sleep(.03)
            else: pytest.fail('Pinned upstream fixture did not become ready')
            for resource in ('app/ui.js', 'core/rfb.js'):
                with opener.open('http://127.0.0.1:' + str(port) + '/' + resource, timeout=1) as response:
                    assert response.status == 200 and len(response.read()) > 100
            real_connect = socket.create_connection
            class Ports:
                @staticmethod
                def create_connection(address, timeout):
                    remapped = {5900: server.server_address[1], 6080: port}.get(address[1], address[1])
                    return real_connect((address[0], remapped), timeout=timeout)
            monkeypatch.setattr(entry, 'socket', Ports)
            monkeypatch.setattr(entry, 'local_open', lambda url: opener.open(url.replace(':6080/', ':' + str(port) + '/'), timeout=1))
            assert entry.rfb_ready() and entry.novnc_ready() and entry.websocket_ready()
            # Exercise transport in both directions only with fixture protocol
            # bytes. No real RFB authentication, keyboard, browser or page input.
            key = base64.b64encode(os.urandom(16)).decode()
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            with real_connect(('127.0.0.1', port), timeout=2) as connection:
                connection.settimeout(2)
                connection.sendall(('GET /websockify HTTP/1.1\r\nHost: 127.0.0.1:' + str(port) + '\r\n'
                    'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                    'Sec-WebSocket-Protocol: binary\r\nSec-WebSocket-Key: ' + key + '\r\n\r\n').encode())
                with connection.makefile('rb') as stream:
                    assert stream.readline().startswith(b'HTTP/1.1 101 ')
                    headers = {}
                    while True:
                        line = stream.readline()
                        if line == b'\r\n': break
                        name, value = line.split(b':', 1)
                        headers[name.lower()] = value.strip()
                    assert headers[b'sec-websocket-accept'] == expected
                    banner = b''
                    while len(banner) < 12: banner += websocket(stream)
                    assert banner == b'RFB 003.008\n'
                    payload = b'RFB 003.008\n'
                    mask = os.urandom(4)
                    connection.sendall(bytes([0x82, 0x80 | len(payload)]) + mask +
                        bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))
                    echoed = b''
                    while len(echoed) < len(b'fixture-echo:' + payload): echoed += websocket(stream)
                    assert echoed == b'fixture-echo:' + payload
            assert payload in server.received
        finally:
            if process is not None:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=2)
                # Only this process group's fixture descendants are addressed.
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            output.close()
            server.shutdown()
            serving.join(timeout=2)
            assert not serving.is_alive()
        with pytest.raises(OSError): real_connect(('127.0.0.1', port), timeout=.2)
