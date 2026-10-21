"""Actual rdai startup serves verified TLS and refuses invalid configuration."""
import ssl
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture
def certificate(tmp_path):
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


def test_fork_startup_serves_tls_with_the_configured_certificate(web_app, certificate, monkeypatch):
    cert, key = certificate
    app = web_app('tls', settings={'web_host':'127.0.0.1', 'web_port':0,
        'web_https_enabled':True,'web_ssl_certfile':str(cert),'web_ssl_keyfile':str(key)})
    monkeypatch.delenv('COW_DESKTOP', raising=False)
    monkeypatch.delenv('COW_WEB_PORT', raising=False)
    import webbrowser
    monkeypatch.setattr(webbrowser, 'open', lambda *args: None)
    from channel.web.fork.runtime import WebChannel
    channel = WebChannel()
    errors=[]
    def start():
        try: channel.startup()
        except Exception as error: errors.append(error)
    thread=threading.Thread(target=start, daemon=True)
    thread.start()
    try:
        deadline=time.monotonic()+10
        while not errors and time.monotonic()<deadline:
            server=channel._http_server
            sock=getattr(server,'socket',None)
            if sock is not None and sock.getsockname()[1]:break
            time.sleep(.02)
        assert not errors, errors
        port=channel._http_server.socket.getsockname()[1]
        context=ssl.create_default_context(cafile=str(cert))
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        with opener.open(f'https://localhost:{port}/',timeout=5) as response:
            assert response.status == 200
            assert b'/assets/js/core/nav.js' in response.read()
        assert channel._http_server.ssl_adapter is not None
    finally:
        if channel._http_server:channel._http_server.stop()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize('invalid', ['missing', 'malformed'])
def test_fork_tls_errors_do_not_start_plain_http(web_app, tmp_path, monkeypatch, invalid):
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    if invalid == 'malformed':
        cert.write_text('not a certificate');key.write_text('not a private key')
    app=web_app('tls-invalid', settings={'web_host':'127.0.0.1','web_port':0,
        'web_https_enabled':True,'web_ssl_certfile':str(cert),'web_ssl_keyfile':str(key)})
    monkeypatch.delenv('COW_DESKTOP', raising=False)
    monkeypatch.delenv('COW_WEB_PORT', raising=False)
    from channel.web.fork.runtime import WebChannel
    channel=WebChannel()
    channel._http_server=None
    with pytest.raises((ValueError,ssl.SSLError)):
        channel.startup()
    server=channel._http_server
    assert server is None or getattr(server,'socket',None) is None
