# encoding:utf-8
"""The scheduled-task page must reach the browser assembled and complete.

Change port-upstream-tasks-page. The page is now upstream's: three fragments and
two scripts served as-is, plus the fork's patch layer. None of that lives in
``chat.html`` any more, so the things that can go wrong are the things a text
search of one file cannot see:

    10|* a fragment that is included but not shipped (the desktop bundle) -- a 500
  from the console inside the shipped app, where nobody runs the suite;
* a script referenced without its cache-busting stamp -- a returning user runs
  the previous console against the newer page;
* the patch layer loaded before the halves it overrides -- its own guard bows
  out, so the fork's semantics (per-action gating, per-task verbs, the read-only
  delivery target) silently never install;
* an include marker reaching the browser, which renders as a comment and takes
  every module with it.

tests/test_scheduler_frontend.cjs covers what those modules then *do*;
tests/test_upstream_drift_guards.py pins the upstream bytes they are ported
against.
"""

import os
import re
from unittest.mock import patch

import channel.web.fork.handlers.pages as pages

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "channel", "web")

#: The upstream page, served as-is, and the fork layer that rides on it.
PAGE_ASSETS = (
    "js/views/tasks.js",
    "js/views/tasks-modal.js",
    "js/fork/tasks-console.js",
)

#: Markup that only exists because the fragments are assembled in. Each id is
#: the one the ported scripts bind to at load time.
FRAGMENT_MARKERS = (
    'id="view-tasks"',            # templates/views/tasks.html
    'id="tasks-pane"',
    'id="runs-pane"',
    'id="task-edit-modal-overlay"',   # templates/modals/task-edit.html
    'id="run-detail-modal-overlay"',  # templates/modals/run-detail.html
)


def _served_page():
    # `web.header` needs a request context; the body does not.
    with patch.object(pages.web, "header", lambda *args, **kwargs: None):
        return pages.ChatHandler().GET()


def test_the_served_page_is_assembled_from_the_fragments():
    """The fragments' markup is in the answer, and no marker is."""
    html = _served_page()
    assert "<!--#include" not in html
    for marker in FRAGMENT_MARKERS:
        assert marker in html, marker


def test_the_page_references_the_upstream_scripts_and_the_fork_layer():
    html = _served_page()
    for asset in PAGE_ASSETS:
        assert f"assets/{asset}" in html, asset


def test_every_page_asset_carries_the_cache_bust_stamp():
    html = _served_page()
    for asset in PAGE_ASSETS:
        pattern = r"assets/" + re.escape(asset) + r"\?v=[0-9a-f]+"
        assert re.search(pattern, html), asset


def test_the_fork_layer_is_evaluated_after_both_halves_it_overrides():
    """It captures upstream's functions off the global object and wraps
    console.js's initDropdown, so both must already have run. Loaded first, its
    capture guard finds nothing and the fork semantics never install -- a page
    that looks fine and quietly offers verbs the server will refuse."""
    html = _served_page()
    # Tag order, not text order: the page explains itself in comments that name
    # these paths, and those comments come first.
    order = re.findall(r'<script defer src="assets/(js/[^"?]+)', html)
    patch_at = order.index("js/fork/tasks-console.js")
    for earlier in ("js/views/tasks.js", "js/views/tasks-modal.js", "js/console.js"):
        assert earlier in order, earlier
        assert order.index(earlier) < patch_at, earlier


def test_the_desktop_bundle_ships_the_fragments_the_page_is_assembled_from():
    """PyInstaller ships only what the desktop spec lists, and the assembler
    reads `templates/` at request time -- so a directory missing from the spec
    is not a missing file at build time, it is a 500 in the shipped app."""
    spec_path = os.path.join(WEB, "..", "..", "desktop", "build", "cowagent-backend.spec")
    with open(spec_path, encoding="utf-8") as handle:
        spec = handle.read()
    bundled = set(re.findall(r"rp\('channel', 'web', '([^']+)'\)", spec))
    assert "templates" in bundled, sorted(bundled)
