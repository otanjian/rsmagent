"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from bridge.context import *
from common import i18n
from common.log import logger
import json
import mimetypes
import os
import web


# Adapted on move (not verbatim, see the emitter): ``__file__``
# now points at the fork package, so asset paths anchor at the web
# package root instead of the monolith's own directory.
_WEB_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class RootHandler:
    def GET(self):
        raise web.seeother('/chat')


class HealthHandler:
    # Unauthenticated liveness probe. The desktop shell polls this to know the
    # backend is up; it must never require a session. Returns no sensitive data.
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        return json.dumps({"status": "ok"})


def _stamp_fork_fragments(html: str) -> str:
    """Stamp the fork fragment URLs the page declares.

    ``fragments.js`` fetches ``static/fragments/*.html`` at runtime, so the URL
    sits in a ``data-fork-fragment`` attribute -- markup the assembler does not
    scan, since it only stamps scripts and stylesheets. Without a stamp an
    upgraded server can keep having a browser mount the previous fork markup.
    The stamp is the same rule the assembler applies to scripts (the file's own
    mtime through ``template.asset_version``), so the URL moves exactly when the
    fragment does. Nothing is stamped that is not a fragment of the page, and a
    fragment directory that cannot be read leaves the page as assembled rather
    than failing the request.
    """
    from channel.web.core import template
    fragments_dir = os.path.join(_WEB_ROOT, 'static', 'fragments')
    try:
        names = sorted(os.listdir(fragments_dir))
    except OSError:
        return html
    for name in names:
        if not name.endswith('.html'):
            continue
        reference = f'assets/fragments/{name}'
        if reference not in html:
            continue
        version = template.asset_version(f'fragments/{name}')
        if version:
            html = html.replace(reference, f'{reference}?v={version}')
    return html


class ChatHandler:
    def GET(self):
        # Content-Type must be explicit: behind a reverse proxy that sends
        # X-Content-Type-Options: nosniff, a missing type makes browsers
        # refuse to sniff and render the page as plain text source.
        from channel.web.web_channel import _web_navigation_mode
        web.header('Content-Type', 'text/html; charset=utf-8')
        web.header('Cache-Control', 'no-cache, no-store, must-revalidate')
        web.header('Pragma', 'no-cache')
        # Assembled from ``templates/`` by the same server-side assembler the
        # upstream shell uses (``<!--#include path-->``), so the fork's page is
        # built out of the same view and modal fragments as upstream's instead
        # of carrying its own copy of each one. Assembly also stamps every
        # first-party asset with its own mtime, which is what replaces the
        # hand-maintained asset list and the wall-clock ``?v=`` below it: a new
        # script, stylesheet or i18n namespace needs no edit here, and an asset
        # that has not changed keeps the URL the browser already has.
        from channel.web.core import template as _template
        html = _stamp_fork_fragments(_template.render('chat.html'))
        # Inject the backend-resolved default language for first-load fallback.
        html = html.replace("{{COW_DEFAULT_LANG}}", i18n.get_language())
        # Inject the validated console navigation presentation switch (layout
        # only): "classic" (single sidebar) or "split" (area switch). Invalid
        # config falls back to "classic"; this never alters authorization or
        # consumer open/closed state.
        html = html.replace(
            "{{COW_NAVIGATION_MODE}}",
            _web_navigation_mode(),
        )
        return html


class AssetsHandler:
    def GET(self, file_path):  # 修改默认参数
        try:
            # 如果请求是/static/，需要处理
            if file_path == '':
                # 返回目录列表...
                pass

            # 获取当前文件的绝对路径
            current_dir = _WEB_ROOT
            static_dir = os.path.join(current_dir, 'static')

            full_path = os.path.normpath(os.path.join(static_dir, file_path))

            # 安全检查：确保请求的文件在static目录内
            if not os.path.abspath(full_path).startswith(os.path.abspath(static_dir)):
                logger.error(f"Security check failed for path: {full_path}")
                raise web.notfound()

            if not os.path.exists(full_path) or not os.path.isfile(full_path):
                # Browsers routinely probe optional asset variants (e.g. a
                # .ttf fallback declared alongside .woff2 in @font-face);
                # logging these as errors floods the console with harmless
                # noise. Keep it at debug level — real misconfigurations
                # will still surface via the network panel.
                logger.debug(f"Static file not found: {full_path}")
                raise web.notfound()

            # 设置正确的Content-Type
            content_type = mimetypes.guess_type(full_path)[0]
            if content_type:
                web.header('Content-Type', content_type)
            else:
                # 默认为二进制流
                web.header('Content-Type', 'application/octet-stream')

            # Without a validator a browser has nothing to cache on, so the
            # console re-downloaded every script, stylesheet, font and logo on
            # every reload. The ETag lets it ask instead, and a hit costs one
            # header rather than the file.
            from channel.web.core import template
            info = os.stat(full_path)
            etag = '"%x-%x"' % (info.st_mtime_ns, info.st_size)
            web.header('ETag', etag)
            # ctx fields are read defensively: this handler is also driven
            # directly, outside a live request, where ctx is empty.
            if template.is_versioned(file_path) and 'v=' in web.ctx.get('query', ''):
                # render() stamps these with the file's own mtime, so the URL
                # cannot outlive the bytes it names: a changed file is a
                # changed URL. That is what makes it safe to promise the copy
                # never goes stale — the promise is about this URL, not about
                # this path.
                web.header('Cache-Control', 'public, max-age=31536000, immutable')
            else:
                # Everything else (vendor bundles, fonts, logos) is served off
                # an unstamped URL, so it has to be revalidated. no-cache means
                # "keep it, but ask" — not "do not keep it".
                web.header('Cache-Control', 'no-cache')
            if web.ctx.get('env', {}).get('HTTP_IF_NONE_MATCH') == etag:
                raise web.notmodified()

            # 读取并返回文件内容
            with open(full_path, 'rb') as f:
                return f.read()

        except web.HTTPError:
            # A 304 or the 404 above, both already handled; re-raise as-is so
            # web.py returns the original status to the client.
            raise
        except Exception as e:
            logger.error(f"Error serving static file: {e}", exc_info=True)
            raise web.notfound()


