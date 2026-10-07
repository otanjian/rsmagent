# encoding:utf-8
"""Guard the console's asset wiring.

The frontend is plain classic scripts with no bundler, so nothing fails at
build time: a script that is on disk but missing from the page just silently
stops existing, and one listed in the wrong place breaks at load. These are
the invariants the split relies on.
"""

import os
import re

from channel.web.core import template

WEB = os.path.join(os.path.dirname(__file__), "..", "channel", "web")
STATIC = os.path.join(WEB, "static")


def _page():
    return template.render("chat.html")


def _scripts(page):
    # Asset URLs are absolute so they resolve the same from every routed path,
    # and each carries a ?v= stamp, so the name sits between /assets/ and the
    # query rather than between /assets/ and the closing quote.
    return re.findall(r'<script defer src="/assets/(js/[^"?]+)(?:\?[^"]*)?"', page)


def test_every_console_script_is_listed_exactly_once_and_exists():
    scripts = _scripts(_page())

    duplicated = sorted({s for s in scripts if scripts.count(s) > 1})
    assert not duplicated, duplicated

    absent = [s for s in scripts if not os.path.exists(os.path.join(STATIC, s))]
    assert not absent, absent

    assert "js/console.js" in scripts and "js/fork/router.js" in scripts
    # Upstream reference modules on disk are not the delivered implementation.
    assert not set(scripts) & {"js/core/router.js", "js/core/nav.js", "js/chat/state.js", "js/boot.js"}


def test_every_stylesheet_is_listed_and_exists():
    sheets = re.findall(
        r'<link rel="stylesheet" href="/assets/(css/[^"?]+)(?:\?[^"]*)?"', _page())
    absent = [s for s in sheets if not os.path.exists(os.path.join(STATIC, s))]
    assert not absent, absent
    assert sheets == sorted(set(sheets), key=sheets.index), "a sheet is linked twice"


def test_runtime_load_order_preserves_dependencies_and_fork_patches():
    scripts = _scripts(_page())
    boot = scripts.index("js/console.js")
    assert scripts.index("js/fork/router.js") < boot < scripts.index("js/workspace.js")
    assert scripts.index("js/doc-editor.js") < scripts.index("js/fork/views/memory.js") < boot
    assert scripts.index("js/views/tasks.js") < boot < scripts.index("js/fork/tasks-console.js")


def test_agent_roster_and_fetch_wrappers_keep_their_initialization_order():
    source = open(os.path.join(STATIC, "js/console.js"), encoding="utf-8").read()
    assert source.index("let defaultAgentId") < source.index("let sessionId = loadOrCreateSessionId()")
    assert source.index("const _nativeFetch") < source.index("const _originalFetch")


def test_the_handler_actually_serves_the_nested_script_paths():
    """The scripts used to be flat under js/; they are now in js/core/,
    js/chat/ and js/views/. Existing on disk is not the same as being
    reachable, so this goes through the handler that answers /assets/."""
    from unittest.mock import patch

    import channel.web.fork.handlers.pages as pages_api

    sent = []
    with patch.object(pages_api.web, "header",
                      lambda name, value=None: sent.append((name.lower(), value))):
        handler = pages_api.AssetsHandler()
        for script in _scripts(_page()):
            del sent[:]
            body = handler.GET(script)
            assert body, script
            # application/octet-stream is what the handler falls back to, and a
            # browser will refuse to execute a script served as that.
            content_type = dict(sent).get("content-type", "")
            assert "javascript" in content_type, (script, content_type)


def test_the_handler_serves_the_assembled_page():
    """ChatHandler answers with the assembled shell, not the raw file: the raw
    chat.html is include markers, which a browser renders as nothing."""
    from unittest.mock import patch

    import channel.web.fork.handlers.pages as pages_api

    with patch.object(pages_api.web, "header", lambda *a, **k: None):
        html = pages_api.ChatHandler().GET()

    assert 'src="/assets/js/console.js' in html
    assert "<!--#include" not in html


def test_the_desktop_bundle_ships_everything_the_page_is_assembled_from():
    """The desktop client freezes the backend with PyInstaller, which ships
    only what the spec lists. chat.html is now a shell that template.py
    assembles from templates/ on every request, so a directory missing from
    the spec is not a missing file at build time -- it is a 500 from the
    console inside the shipped app, where nobody runs the test suite."""
    spec_path = os.path.join(WEB, "..", "..", "desktop", "build",
                             "cowagent-backend.spec")
    with open(spec_path, encoding="utf-8") as f:
        spec = f.read()

    # datas entries are written as rp('channel', 'web', <name>); whole
    # directories travel with everything under them.
    bundled = set(re.findall(r"rp\('channel', 'web', '([^']+)'\)", spec))

    page_source = os.path.join(WEB, "chat.html")
    with open(page_source, encoding="utf-8") as f:
        shell = f.read()

    needed = {ref.split("/")[0]
              for ref in re.findall(r"<!--#include\s+([^\s>]+?)\s*-->", shell)}
    needed.update(ref.split("/")[0]
                  for ref in re.findall(r'assets/((?:js|css)/[^"?]+)', shell))
    needed.add("chat.html")
    # assets/ is served out of static/, which is where the files actually live.
    needed = {"static" if n in ("js", "css") else n for n in needed}

    assert not needed - bundled, sorted(needed - bundled)


def test_an_assets_version_moves_with_the_file_and_not_with_the_clock():
    """The stamp used to be the wall clock, so every asset URL changed on every
    request and the browser re-fetched the whole console on every reload. The
    property that replaced it: a stamp holds still until its own file changes,
    which is also what lets the asset handler promise a versioned URL never
    goes stale."""
    first = _page()
    assert first == _page(), "rendering twice must not move any stamp"

    victim = _scripts(first)[0]
    path = os.path.join(STATIC, victim)
    before = os.stat(path)
    try:
        os.utime(path, (before.st_atime, before.st_mtime + 5))
        after = _page()
    finally:
        os.utime(path, (before.st_atime, before.st_mtime))

    moved = {ref for ref in re.findall(r'/assets/(?:js|css)/[^"\']+', first)}
    moved ^= {ref for ref in re.findall(r'/assets/(?:js|css)/[^"\']+', after)}
    # Exactly one URL differs: the touched file's, in its old and new form.
    assert len(moved) == 2, sorted(moved)
    assert all(ref.startswith("/assets/" + victim + "?v=") for ref in moved), sorted(moved)


def test_only_stamped_assets_are_advertised_as_immutable():
    """The handler hands out a year-long, revalidation-free cache entry for
    anything is_versioned() accepts. That is only safe where render() puts an
    mtime in the URL -- a vendor bundle or the -old snapshot keeps its URL
    across edits, so promising the same would strand a browser on stale code
    with no way to ask."""
    for stamped in ("js/boot.js", "css/console.css", "js/views/chat.js"):
        assert template.is_versioned(stamped), stamped

    for unstamped in ("vendor/tailwind.js", "legacy/js/console.js",
                      "logos/openai.svg", "vendor/fonts/inter.woff2"):
        assert not template.is_versioned(unstamped), unstamped


def test_the_split_scripts_do_not_declare_the_same_global_twice():
    """Every top-level declaration lands on `window`, which is what the inline
    onclick handlers in generated markup reach. Two scripts declaring the same
    const is a SyntaxError that blanks the page."""
    pattern = re.compile(r"^(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)", re.M)

    owners = {}
    clashes = []
    for script in _scripts(_page()):
        with open(os.path.join(STATIC, script), encoding="utf-8") as f:
            for name in pattern.findall(f.read()):
                first = owners.setdefault(name, script)
                if first != script:
                    clashes.append(f"{name}: {first} and {script}")

    assert not clashes, clashes
    # Guard the regex itself: if it stopped matching, the test above would pass
    # for the wrong reason.
    assert "navigateTo" in owners
    assert len(owners) > 600, len(owners)
