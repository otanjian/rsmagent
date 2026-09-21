# encoding:utf-8
"""The coding console modules must reach the browser, not just the disk.

Change add-opencode-coding-agents, task 4.5. The frontend is plain classic
scripts with no bundler, so a module that is on disk but not referenced by the
page -- or referenced without the cache-busting stamp -- simply does not exist
for a returning user: the browser keeps running the previous console against a
newer backend.

The Node frontend suite reads `chat.html` and `pages.py` as text; this is the
part that can only be checked against what the handler actually answers with,
so the two together cover "is it referenced" and "is it served".
"""

import re
from unittest.mock import patch

import channel.web.fork.handlers.pages as pages

#: The coding feature's own files: the module, its styles and its strings.
CODING_ASSETS = ('js/coding.js', 'css/coding.css', 'js/i18n/coding.js')


def _served_page():
    # `web.header` needs a request context; the body does not.
    with patch.object(pages.web, 'header', lambda *args, **kwargs: None):
        return pages.ChatHandler().GET()


def test_the_served_page_references_every_coding_asset():
    html = _served_page()
    for asset in CODING_ASSETS:
        assert f'assets/{asset}' in html, asset


def test_every_coding_asset_carries_the_cache_bust_stamp():
    """A stale console loading a new backend is exactly the upgrade the stamp
    exists to prevent, so an unstamped reference is a bug even though the file
    resolves.

    The stamp's *shape* is the assembler's business, not this test's: it used to
    be the request's wall clock (all digits) and is now the file's own mtime in
    hex (see channel/web/core/template.py), which is what makes a stamp hold
    still until its asset changes. What must not regress is that a first-party
    asset is referenced with a stamp at all.
    """
    html = _served_page()
    for asset in CODING_ASSETS:
        pattern = r'assets/' + re.escape(asset) + r'\?v=[0-9a-f]+'
        assert re.search(pattern, html), asset
    # The module the page has always loaded is stamped by the same mechanism, so
    # this is a regression check on the mechanism, not on one asset.
    assert re.search(r'assets/js/console\.js\?v=[0-9a-f]+', html)


def test_the_coding_module_is_evaluated_before_the_console_consumes_it():
    """console.js wires `window.CodingChat` into the new-chat menu, the Agent
    form and every open-by-type call while it initialises, so a later script tag
    would leave the feature undefined for the whole page."""
    html = _served_page()
    assert html.index('assets/js/coding.js') < html.index('assets/js/console.js')


def test_the_served_page_no_longer_contains_include_markers():
    """The shell is assembled before it is answered; a marker reaching the
    browser renders as a comment and takes every module with it."""
    html = _served_page()
    assert '<!--#include' not in html
