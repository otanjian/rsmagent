"""Path contracts for the registry / fork assembly shipped by rdai.
Browser tests cover leave guards, tab restoration and history; these exercise
all declared paths through the real WSGI application, including anonymous use.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from channel.web.core import template
from channel.web.route_registry import console_page_metadata, shell_entry_paths

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "channel/web/static/js"

@pytest.fixture(scope="module")
def responses():
    # Isolate web.py from test modules that install web stubs during collection.
    program = r"""
import json
from channel.web.web_channel import build_web_app
from channel.web.route_registry import shell_entry_paths
app = build_web_app()
results = {}
requests = [("GET", path) for path in shell_entry_paths()]
requests += [("GET", path) for path in ["/config", "/api/agents", "/settings/no-such-tab", "/no-such-view"]]
requests += [("POST", "/settings/models"), ("GET", "/knowledge/graph/")]
for method, path in requests:
    r = app.request(path, method=method, headers={"X-Forwarded-Proto": "https"})
    body = r.data.decode("utf-8", "replace")
    results[method + " " + path] = {"status": r.status, "html": "id=\"console-page-routes\"" in body,
        "headers": dict(r.headers), "metadata": "{{COW_CONSOLE_ROUTES}}" in body}
print(json.dumps(results))
"""
    result = subprocess.run([sys.executable, "-B", "-c", program], cwd=ROOT,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_actual_app_serves_every_registered_page(responses):
    for path in shell_entry_paths():
        result = responses["GET " + path]
        assert result["status"].startswith("200"), (path, result)
        assert result["html"] and not result["metadata"], path
    assert responses["GET /knowledge/graph/"]["status"].startswith("200")


def test_page_paths_do_not_shadow_apis(responses):
    for path in ("/config", "/api/agents"):
        result = responses["GET " + path]
        assert result["status"].split()[0] in {"400", "401", "403"}, result
        assert not result["html"], path
    for path in ("/settings/no-such-tab", "/no-such-view"):
        assert responses["GET " + path]["status"].startswith("404")
    assert not responses["POST /settings/models"]["html"]
    assert responses["POST /settings/models"]["status"].split()[0] in {"401", "403", "405"}


def test_tabs_match_the_rendered_page_and_report_changes():
    page = template.render("chat.html")
    sources = {"config": "console.js", "knowledge": "console.js",
               "memory": "fork/views/memory.js", "tasks": "views/tasks.js"}
    for entry in console_page_metadata():
        if not entry["tabs"]:
            continue
        present = set(re.findall(r'id="%s-tab-([a-z]+)"' % entry["view"], page))
        # catalog is the member projection, with no management tab button.
        expected = set(entry["tabs"]) - ({"catalog"} if entry["view"] == "config" else set())
        assert present == expected, (entry, present)
        if entry["view"] == "config":
            assert 'id="config-panel-catalog"' in page
        source = (STATIC / sources[entry["view"]]).read_text()
        assert "routeNoteTab('%s'" % entry["view"] in source or (
            "ConsoleRoutes.note('%s'" % entry["view"]) in source
        for tab, alias in entry["tab_aliases"].items():
            assert tab in entry["tabs"] and alias not in entry["tabs"]
            assert entry["path"] + "/" + alias in shell_entry_paths()
    memory = next(p for p in console_page_metadata() if p["view"] == "memory")
    assert memory["tab_aliases"] == {"dreams": "evolution"}


def test_page_inventory_matches_live_navigation():
    source = (STATIC / "console.js").read_text()
    block = source.split("const VIEW_META = {", 1)[1].split("\n};", 1)[0]
    views = set(re.findall(r"^\s*'?([\w-]+)'?:\s*\{", block, re.M))
    views = {v for v in views if not v.startswith("personal-")}
    assert {p["view"] for p in console_page_metadata()} == views
    assert next(p for p in console_page_metadata() if p["view"] == "agents")["area"] == "admin"


def test_only_one_router_is_loaded_and_boot_is_auth_gated():
    page = template.render("chat.html")
    assert page.count('src="/assets/js/fork/router.js') == 1
    assert 'src="/assets/js/core/router.js' not in page
    assert page.index('src="/assets/js/fork/router.js') < page.index('src="/assets/js/console.js')
    source = (STATIC / "console.js").read_text()
    assert "window.ConsoleRoutes.apply()" in source
    router = (STATIC / "fork/router.js").read_text()
    assert "!_accountAppVisible" in router
    assert "if (ready) apply()" in router


def test_root_and_legacy_chat_are_safe_anonymous_shells(responses):
    for path in ("/", "/chat"):
        result = responses["GET " + path]
        assert result["status"].startswith("200") and result["html"]
        assert "Location" not in result["headers"]
    # A fragment is never sent to the server. /chat must serve the parser so
    # old fragment bookmarks survive without an absolute HTTP redirect.
    assert "/chat" in shell_entry_paths()


def test_deep_shell_does_not_preserve_revoked_platform_data_access(tmp_path):
    from tests._helpers import WebAppHarness
    web = WebAppHarness(tmp_path)
    try:
        user_id = web.member('operator', ['member'])
        def platform(granted):
            row = next(row for row in web.service.list_platform_users() if row['id'] == user_id)
            web.service.set_platform_user_status(actor_user_id=web.admin_id, user_id=user_id,
                active=True, is_platform_admin=granted, expected_version=row['version'],
                recent_password=web.ADMIN_PASSWORD)
        platform(True)
        token = web.login('operator')
        assert web.get('/settings/models', token=token).status.startswith('200')
        assert web.get('/api/models', token=token).status.startswith('200')
        platform(False)
        assert web.get('/settings/models', token=token).status.startswith('200')
        assert web.get('/api/models', token=token).status.startswith('403')
        for path in ('/api/models', '/api/update/status'):
            assert web.get(path).status.split()[0] in {'401', '403'}
        projection = web.json(web.get('/auth/context', token=token))
        assert projection['console_pages']['admin.models']['actions']['manage'] is False
    finally:
        web.close()
