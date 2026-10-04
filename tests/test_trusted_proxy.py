import json
import web
import pytest
from channel.web import auth_handlers as auth


@pytest.fixture
def proxy_env(monkeypatch):
    monkeypatch.setattr(auth, 'config_trusted_proxies', lambda: {'127.0.0.1'})
    web.ctx.env = {'REMOTE_ADDR': '127.0.0.1', 'HTTP_HOST': 'agent.example',
                   'HTTP_X_FORWARDED_PROTO': 'https', 'HTTP_X_FORWARDED_FOR': '192.0.2.20',
                   'HTTP_ORIGIN': 'https://agent.example'}
    web.ctx.headers = []
    return web.ctx.env


def test_trusted_proxy_source_scheme_and_origin(proxy_env):
    assert auth._request_origin_exact() == 'https://agent.example'
    assert auth._client_source() == '192.0.2.20'
    assert auth._origin_ok()
    proxy_env['HTTP_ORIGIN'] = 'http://agent.example'
    assert not auth._origin_ok()


def test_untrusted_forwarding_cannot_change_source_or_scheme(proxy_env):
    proxy_env['REMOTE_ADDR'] = '192.0.2.99'
    assert auth._request_origin_exact() == 'http://agent.example'
    assert auth._client_source() == '192.0.2.99'
    assert not auth._origin_ok()
    proxy_env['HTTP_ORIGIN'] = 'http://agent.example'
    assert auth._origin_ok()


def test_secure_login_cookie_after_trusted_tls_termination(proxy_env, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(web, 'input', lambda **kw: SimpleNamespace(username='user', password='pass'))
    monkeypatch.setattr(auth, '_read_body', lambda: {'username': 'user', 'password': 'pass'}, raising=False)
    monkeypatch.setattr(auth, '_get_service', lambda: SimpleNamespace(login=lambda *args: SimpleNamespace(token='synthetic-token', must_change_password=False, username='user', display_name='User', is_platform_admin=False, tenants=[])))
    # Cookie construction is checked through the real web.py serializer in
    # existing HTTP tests; here exercise the handler with its actual parser.
    monkeypatch.setattr(web, 'data', lambda: json.dumps({'username': 'user', 'password': 'pass'}).encode())
    auth.DbAuthLoginHandler().POST()
    cookies = [v for k,v in web.ctx.headers if k.lower() == 'set-cookie']
    assert cookies and 'secure' in cookies[0].lower()
    assert 'httponly' in cookies[0].lower() and 'samesite=lax' in cookies[0].lower()
