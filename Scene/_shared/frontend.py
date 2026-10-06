"""Serve an allowlisted scene frontend assembled from untouched source slices."""
import json
import mimetypes
import re
from functools import lru_cache
from pathlib import Path

import web

from Scene.catalog import ROOT


@lru_cache(maxsize=1)
def source_manifest():
    return json.loads((ROOT / "source-manifest.json").read_text(encoding="utf-8"))["files"]


@lru_cache(maxsize=1)
def runtime_script():
    ordered = ["_shared/frontend/i18n.js", "_shared/frontend/activation.js",
               "_shared/frontend/greeting.js", "_shared/frontend/ui.js", "_shared/frontend/erp.js", "_shared/frontend/workbench.js"]
    ordered += [entry["path"] for entry in source_manifest()
                if entry["path"].endswith("/frontend/workbench.js") and not entry["path"].startswith("_shared/")]
    source = b"\n".join((ROOT / path).read_bytes() for path in ordered)
    functions = re.findall(rb"^(?:async )?function (\w+)\(", source, re.M)
    exports = b", ".join(functions)
    adapter = (ROOT / "_shared/frontend/adapter.js").read_bytes()
    return b"(function () {\n'use strict';\n" + adapter + b"\n" + source + b"\nObject.assign(window.SceneOriginal, {" + exports + b"});\n" + b"Object.entries(window.SceneOriginal).forEach(([name,fn]) => { if (!['open','activateScene','activateSceneKeepSession'].includes(name)) window[name] = fn; });\n})();\n"


@lru_cache(maxsize=1)
def workbench_html():
    return b"\n".join((ROOT / item["path"]).read_bytes() for item in source_manifest()
                       if "/frontend/" in item["path"] and item["path"].endswith(".html"))


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
