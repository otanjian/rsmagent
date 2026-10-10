"""Serve an allowlisted scene frontend assembled from untouched source slices."""
import json
import mimetypes
import re
from functools import lru_cache
from pathlib import Path

import web

from Scene.catalog import ROOT

# The console asks for /scene-assets/runtime.js, assembled here from every scene's
# frontend/workbench.js. The assembly is cached -- concatenating a few hundred
# kilobytes on every request is not free -- but a cached copy must not outlive an
# edit: it is served with ``no-cache``, and a scene author expects a reload to show
# their change rather than a process restart. So each cache below is keyed on a
# stamp of the exact files it was built from: unchanged sources reuse the cached
# bytes, and any content, rename or removal rebuilds.
_SHARED_SCRIPTS = ("_shared/frontend/i18n.js", "_shared/frontend/activation.js",
                   "_shared/frontend/greeting.js", "_shared/frontend/ui.js",
                   "_shared/frontend/erp.js", "_shared/frontend/workbench.js")
_ADAPTER = "_shared/frontend/adapter.js"
_MANIFEST = "source-manifest.json"


def _stamp(paths):
    """A hashable fingerprint of ``paths``: each name with its mtime and size.

    Exact rather than aggregated, so two edits that cancel out in a sum still
    differ, and a file that has appeared or vanished changes the key too. The
    path list is short, so the key stays small.
    """
    parts = []
    for path in paths:
        try:
            stat = (ROOT / path).stat()
        except OSError:
            parts.append((path, None))
            continue
        parts.append((path, stat.st_mtime_ns, stat.st_size))
    return tuple(parts)


@lru_cache(maxsize=4)
def _manifest(stamp):
    return json.loads((ROOT / _MANIFEST).read_text(encoding="utf-8"))["files"]


def source_manifest():
    return _manifest(_stamp((_MANIFEST,)))


def _ordered_scripts():
    """The shared slices followed by every scene's own workbench.js."""
    ordered = list(_SHARED_SCRIPTS)
    ordered += [entry["path"] for entry in source_manifest()
                if entry["path"].endswith("/frontend/workbench.js") and not entry["path"].startswith("_shared/")]
    return ordered


@lru_cache(maxsize=4)
def _runtime_script(stamp):
    source = b"\n".join((ROOT / path).read_bytes() for path in _ordered_scripts())
    functions = re.findall(rb"^(?:async )?function (\w+)\(", source, re.M)
    exports = b", ".join(functions)
    adapter = (ROOT / _ADAPTER).read_bytes()
    return b"(function () {\n'use strict';\n" + adapter + b"\n" + source + b"\nObject.assign(window.SceneOriginal, {" + exports + b"});\n" + b"Object.entries(window.SceneOriginal).forEach(([name,fn]) => { if (!['open','activateScene','activateSceneKeepSession'].includes(name)) window[name] = fn; });\n})();\n"


def runtime_script():
    return _runtime_script(_stamp([_MANIFEST, _ADAPTER] + _ordered_scripts()))


def _workbench_pages():
    return [item["path"] for item in source_manifest()
            if "/frontend/" in item["path"] and item["path"].endswith(".html")]


@lru_cache(maxsize=4)
def _workbench_html(stamp):
    return b"\n".join((ROOT / path).read_bytes() for path in _workbench_pages())


def workbench_html():
    return _workbench_html(_stamp([_MANIFEST] + _workbench_pages()))


class SceneAssetHandler:
    def GET(self, path):
        if path == "runtime.js":
            content, mime = runtime_script(), "text/javascript; charset=utf-8"
        elif path == "workbenches.html":
            content, mime = workbench_html(), "text/html; charset=utf-8"
        else:
            allowed = {entry["path"] for entry in source_manifest()
                       if "/frontend/" in entry["path"] or any(part in entry["path"] for part in ("/assets/", "/templates/", "/demo/"))}
            allowed.add("_shared/frontend/host.css")
            file = (ROOT / path).resolve()
            if path not in allowed or not file.is_relative_to(ROOT) or file.suffix.lower() not in {".js", ".css", ".html", ".xlsx", ".csv", ".txt", ".json"} or not file.is_file():
                raise web.notfound()
            content = file.read_bytes()
            mime = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        web.header("Content-Type", mime)
        web.header("X-Content-Type-Options", "nosniff")
        # Assembled runtime must stay fresh; large static libs benefit from cache
        # so the first scene-card click is not a silent multi-megabyte wait.
        if path in ("runtime.js", "workbenches.html"):
            web.header("Cache-Control", "no-cache")
        else:
            web.header("Cache-Control", "public, max-age=86400")
        return content
