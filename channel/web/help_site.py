# encoding:utf-8
"""Public product help, served by the main web.py application at /help/."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlsplit

import web

from webhelp.site import HelpView, PAGES, resource_root

DEFAULT_HELP_SITE_URL = '/help/'
SITE_CONFIG_RELATIVE_PATH = ('webhelp', 'config.json')
_ASSET_TYPES = {
    '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.gif': 'image/gif', '.ico': 'image/x-icon', '.mp4': 'video/mp4',
    '.woff': 'font/woff', '.woff2': 'font/woff2',
    '.sh': 'text/plain; charset=utf-8', '.ps1': 'text/plain; charset=utf-8',
    '.yml': 'text/plain; charset=utf-8', '.yaml': 'text/plain; charset=utf-8',
}


def site_config_path() -> str:
    return str(resource_root() / 'config.json')


def read_declared_site_url(path: Optional[str] = None) -> str:
    """Public deployment-command base; it never redirects the help entry."""
    try:
        value = json.loads(Path(path or site_config_path()).read_text(encoding='utf-8'))
        url = value.get('site_url', '')
        return url.strip() if isinstance(url, str) else ''
    except (OSError, ValueError, AttributeError):
        return ''


def normalize_site_url(raw: Optional[str]) -> str:
    value = (raw or '').strip()
    if not value:
        return ''
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return ''
    host = parts.hostname or ''
    if parts.scheme not in ('http', 'https') or not host or 'your-site-domain' in host.lower():
        return ''
    authority = '[%s]' % host if ':' in host else host
    if port:
        authority += ':%d' % port
    path = (parts.path or '/').rstrip('/') + '/'
    return '%s://%s%s' % (parts.scheme, authority, path)


def resolve_help_site_url(path: Optional[str] = None) -> str:
    """The help entry is always on the current service, regardless of config."""
    return DEFAULT_HELP_SITE_URL


def _asset(path):
    root = (resource_root() / 'assets').resolve()
    # The route exposes only explicitly public assets, never templates/config/tools.
    parts = path.split('/')
    if '\\' in path or any(p.startswith('.') or not p for p in parts):
        raise web.notfound()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise web.notfound()
    if not target.is_file() or target.suffix.lower() not in _ASSET_TYPES:
        raise web.notfound()
    stat = target.stat()
    etag = '"%x-%x"' % (stat.st_mtime_ns, stat.st_size)
    web.header('Content-Type', _ASSET_TYPES[target.suffix.lower()])
    web.header('ETag', etag)
    web.header('Cache-Control', 'public, max-age=3600')
    web.header('Accept-Ranges', 'bytes')
    if web.ctx.env.get('HTTP_IF_NONE_MATCH') == etag:
        raise web.HTTPError('304 Not Modified', {}, '')
    start, end = 0, stat.st_size - 1
    requested = web.ctx.env.get('HTTP_RANGE', '')
    if requested:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
        if not match or not any(match.groups()) or not stat.st_size:
            raise web.HTTPError('416 Range Not Satisfiable', {'Content-Range': 'bytes */%d' % stat.st_size}, '')
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), end) if last else end
        else:
            start = max(0, stat.st_size - int(last))
        if start > end or start >= stat.st_size:
            raise web.HTTPError('416 Range Not Satisfiable', {'Content-Range': 'bytes */%d' % stat.st_size}, '')
        web.ctx.status = '206 Partial Content'
        web.header('Content-Range', 'bytes %d-%d/%d' % (start, end, stat.st_size))
    length = max(0, end - start + 1)
    web.header('Content-Length', str(length))

    def chunks():
        with target.open('rb') as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    return chunks()


class HelpSiteHandler:
    def GET(self, path=None):
        web.header('X-Content-Type-Options', 'nosniff')
        web.header('Referrer-Policy', 'strict-origin-when-cross-origin')
        if path is None:
            raise web.HTTPError('301 Moved Permanently', {'Location': '/help/' + web.ctx.query}, '')
        if path.startswith('assets/'):
            return _asset(path[len('assets/'):])
        query = dict(web.input())
        page = path or 'index'
        if page.endswith('.php') and page[:-4] in PAGES:
            page = page[:-4]
            target = '/help/' + ('' if page == 'index' else page)
            raise web.HTTPError('301 Moved Permanently', {'Location': target + ('?' + urlencode(query) if query else '')}, '')
        if page == 'scenario_doc':
            # 参数化页面：/help/scenario_doc 本身没有内容，只有 /help/scenario/<slug>。
            raise web.notfound()
        parts = page.split('/')
        if len(parts) in (2, 3) and parts[0] == 'scenario' and parts[1]:
            # /help/scenario/<slug>[/tasks|/demo]，语言仍由 cookie 与 ?lang= 决定。
            query = {**query, 'p': parts[1], 'k': parts[2] if len(parts) == 3 else ''}
            page = 'scenario_doc'
        if page not in PAGES:
            raise web.notfound()
        lang = query.get('lang', '').lower()
        if lang in ('zh', 'en'):
            web.setcookie('webhelp_lang', lang, expires=31536000, path='/help', samesite='Lax',
                          secure=web.ctx.env.get('wsgi.url_scheme') == 'https', httponly=True)
        else:
            lang = web.cookies(webhelp_lang='zh').webhelp_lang.lower()
        view = HelpView(page, lang, query, origin=web.ctx.home)
        web.header('Content-Type', 'text/html; charset=utf-8')
        # Language depends on a cookie; do not let a shared cache mix languages.
        web.header('Cache-Control', 'private, no-cache')
        web.header('Vary', 'Cookie')
        if not view.ok:
            web.ctx.status = '404 Not Found'
        return view.render('header') + view.render() + view.render('footer')
