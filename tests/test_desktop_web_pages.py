# encoding:utf-8
"""Phase-1 page audit: W01-W18 over the *served* console (tasks 5.3-5.5).

The container changes what a page may assume, and it does so invisibly: a page
that calls ``window.open`` or navigates the top frame still works in a browser,
so nothing fails until a user opens the console inside the desktop client. This
audit is the check that the shipped console keeps behaving in both, and it is
deliberately split into the three parts that can disagree:

* **What the server signs and serves.** Every acceptance row maps onto a real
  page id in ``_SIGNED_CONSOLE_PAGES`` and onto a real view container in the
  assembled shell (``/chat`` is fetched through the app, with its SSI includes
  resolved). A row whose page id was renamed, or whose view left the shell, is
  a page the matrix claims and nobody can open.
* **What the projection answers.** For each role the row is *reachable* or
  *honestly refused*: a refusal carries a reason from the projection's own
  vocabulary, and nothing is reported as available while being refused. That is
  the rule this file holds -- acceptance.md: 能力关闭必须有真实服务端依据，
  不允许为了矩阵通过临时隐藏坏页面.
* **What the container must survive.** The link and navigation call sites are
  enumerated, so a new ``window.open`` (which the container denies and the
  adapter re-routes), or a new same-frame navigation target (which the container
  refuses unless it is a shell route), shows up here with its reason -- or fails.

Rows name both a view id and a page id because the console carries two tables:
the classic monolith's ``VIEW_META`` and the split shell's ``core/nav.js``.
"""

import os
import re
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from _helpers import WebAppHarness  # noqa: E402

from auth.service import BUILTIN_MENU_DEFAULTS, _SIGNED_CONSOLE_PAGES  # noqa: E402
from channel.web.core import template  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WEB = os.path.join(ROOT, "channel", "web")
STATIC = os.path.join(WEB, "static")

#: W01-W18: title, signed page ids, view ids, and the role the row belongs to.
#: The view ids are the containers the shipped shell hosts (``view-<id>``); a
#: row with no signed page -- W14, the account menu -- declares ``None``.
W_ROWS = (
    ("W01", "普通对话", ("workbench.chat",), ("chat",), "member"),
    ("W02", "多智能体", ("workbench.chat",), ("chat",), "member"),
    ("W03", "会话历史", ("workbench.history",), ("history",), "member"),
    ("W04", "智能体使用工作台", ("workbench.agents",), ("agent-workbench",), "member"),
    ("W05", "智能体管理", ("admin.agents",), ("agents",), "tenant_admin"),
    ("W06", "场景应用", ("workbench.scenes",), ("scenes",), "member"),
    ("W07", "知识库", ("workbench.knowledge",), ("knowledge",), "member"),
    ("W08", "待办", ("workbench.todos",), ("todo",), "member"),
    ("W09", "技能与记忆", ("admin.skills", "admin.memory"), ("skills", "memory"), "tenant_admin"),
    ("W10", "模型与配置", ("admin.models",), ("config",), "tenant_admin"),
    ("W11", "消息渠道", ("admin.channels",), ("channels",), "member"),
    ("W12", "系统接入", ("admin.external_connections",), ("external_connections",), "member"),
    ("W13", "定时任务", ("workbench.schedules",), ("tasks",), "member"),
    ("W14", "账户与偏好", (None,), (None,), "member"),
    ("W15", "组织权限", ("admin.members", "admin.roles", "admin.organization"),
     ("system_user", "roles", "org"), "tenant_admin"),
    ("W16", "平台与租户", ("admin.tenants",), ("tenant", "platform"), "platform_admin"),
    ("W17", "运维", ("admin.logs", "admin.audit", "admin.token_usage"),
     ("logs", "audit", "token_usage"), "platform_admin"),
    ("W18", "品牌", ("admin.branding",), ("branding",), "platform_admin"),
)

#: Page ids with no row of their own, and why: the audit is not allowed to grow
#: a silent hole, so each exemption is named here.
UNROUTED_PAGES = {
    "admin.settings": "operator settings, reached from the same 运维 menu group as W17",
}

#: Views the console builds at runtime instead of shipping a container for, with
#: the module that registers or wraps the loader. The audit still proves those
#: views are reachable rather than exempting them.
RUNTIME_VIEWS = {
    "tasks": "channel/web/static/js/fork/tasks-console.js",
}

#: The projection's refusal vocabulary (auth/service.py, auth/capability_matrix.py,
#: integrations/external/registry.py). A refusal outside this set is a new answer
#: the console has no branch for -- and one this audit must be told about.
REFUSAL_REASONS = frozenset({
    "no_permission", "consumer_closed", "no_resource_grant", "not_implemented",
    "not_accepted", "disabled_by_deployment", "awaiting_acceptance",
    "menu_denied", "deferred",
})

#: Popup call sites the container can serve: the adapter replaces ``window.open``
#: and routes each target (system browser for a foreign link, the isolated
#: content window for a same-origin preview or attachment).
POPUP_SITES = frozenset({
    "js/console.js",        # account "about" link
    "js/core/version.js",   # GitHub releases (foreign)
    "js/workspace.js",      # preview / raw file (same-origin content)
})


@pytest.fixture(scope="module")
def web():
    harness = WebAppHarness(tempfile.mkdtemp())
    harness.member("alice", ["member"])
    harness.member("bob", ["tenant_admin"])
    # The bootstrap root is the platform administrator the W16-W18 rows need; a
    # real deployment has it complete its first password change.
    harness.service.change_password(
        harness.service.login("root", harness.ADMIN_PASSWORD).token,
        harness.ADMIN_PASSWORD, harness.ADMIN_FINAL)
    harness._passwords["root"] = harness.ADMIN_FINAL
    return harness


def _shell(web, username):
    """The assembled shell as the app serves it (SSI includes resolved)."""
    response = web.get("/chat", token=web.login(username))
    assert response.status == "200 OK", response.status
    return response.data.decode("utf-8")


def _pages_for(web, username):
    return web.service.context_for_tenant(
        web.login(username), web.tenant_id)["console_pages"]


def _web_sources():
    for root, dirs, files in os.walk(os.path.join(STATIC, "js")):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in files:
            if name.endswith(".js"):
                yield os.path.join(root, name)


# ---------------------------------------------------------------------------
# The mapping itself
# ---------------------------------------------------------------------------

def test_every_row_names_a_real_signed_page():
    for row, title, pages, _views, _role in W_ROWS:
        for page in pages:
            if page is None:
                continue
            assert page in _SIGNED_CONSOLE_PAGES, (row, title, page)


def test_every_row_declares_a_view_and_a_role():
    for row, _title, pages, views, role in W_ROWS:
        assert role in ("member", "tenant_admin", "platform_admin"), row
        if pages == (None,):
            assert views == (None,), row
            continue
        assert views, row
        assert all(isinstance(view, str) and view for view in views), row


def test_no_signed_business_page_is_left_un_audited():
    """A new signed page needs a row (or an explicit exemption) to be verified."""
    covered = {page for _row, _t, pages, _v, _r in W_ROWS for page in pages if page}
    uncovered = (set(_SIGNED_CONSOLE_PAGES) - covered) - set(UNROUTED_PAGES)
    assert not uncovered, sorted(uncovered)
    for page, reason in UNROUTED_PAGES.items():
        assert page in _SIGNED_CONSOLE_PAGES, page
        assert reason, page


# ---------------------------------------------------------------------------
# The assembled shell the container must host
# ---------------------------------------------------------------------------

def test_the_served_shell_hosts_every_view_a_row_uses(web):
    html = _shell(web, "alice")
    present = set(re.findall(r'id="view-([A-Za-z0-9_-]+)"', html))
    missing = [(row, view) for row, _t, _p, views, _r in W_ROWS for view in views
               if view is not None and view not in RUNTIME_VIEWS and view not in present]
    assert not missing, missing


def test_a_runtime_built_view_still_ships_its_container_and_loader():
    for view, module in RUNTIME_VIEWS.items():
        assert f'id="view-{view}"' in template.render("chat.html"), view
        source = open(os.path.join(ROOT, module), encoding="utf-8").read()
        assert "loadTasksView" in source or f"registerConsoleView" in source, module


def test_the_shell_is_assembled_and_self_contained(web):
    """The includes are resolved, and nothing points at a developer's machine."""
    html = _shell(web, "alice")
    assert "<!--#include" not in html, "the served page must be assembled"
    # A spot check that an included container is present: it comes from
    # templates/views/tasks.html, which chat.html's own body does not carry.
    assert 'id="view-tasks"' in html
    assert 'id="tasks-list"' in html

    # Every *asset* is addressed relatively: the container loads the shell from
    # the server origin, and the same document also has to work under a
    # configured sub-path. Same-origin shell links (``/chat``, ``/admin``) are
    # the nav area's, and are the two routes the container accepts.
    assets = re.findall(r'(?:src|href)="(/assets/[^"]*)"', html)
    assert not assets, assets
    shell_links = {r for r in re.findall(r'href="(/[^"]*)"', html)}
    assert shell_links <= {"/chat", "/admin"}, sorted(shell_links)
    assert "localhost" not in html and "127.0.0.1" not in html


def test_the_desktop_adapter_is_loaded_before_the_console():
    """console.js reads ``CowDesktopHost`` at load, so the order is a contract."""
    html = open(os.path.join(WEB, "chat.html"), encoding="utf-8").read()
    scripts = re.findall(r'<script defer src="assets/(js/[^"?]+)', html)
    assert "js/fork/desktop-host.js" in scripts, "the adapter must be on the page"
    assert scripts.index("js/fork/desktop-host.js") < scripts.index("js/console.js")
    # ...and the served page keeps that order after assembly.
    served = template.render("chat.html")
    assert served.index("assets/js/fork/desktop-host.js") < served.index("assets/js/console.js")


def test_both_navigation_modes_serve_the_adapter(web):
    """Classic and split are one shell, so both must carry the adapter (task 5.1).

    The presentation switch is resolved *server-side* and injected into the same
    document, so a mode that dropped the adapter -- or loaded it after
    ``console.js`` read ``CowDesktopHost`` -- would leave that mode with no
    environment answer in a container. Both values are requested through the app
    rather than assumed from the static file.
    """
    original = web._settings.get("web_navigation_mode")
    try:
        for mode in ("classic", "split"):
            web._settings["web_navigation_mode"] = mode
            html = _shell(web, "alice")
            assert f'"{mode}"' in html or f"'{mode}'" in html, mode
            assert "{{COW_NAVIGATION_MODE}}" not in html, mode
            adapter = html.index("assets/js/fork/desktop-host.js")
            assert adapter < html.index("assets/js/console.js"), mode
    finally:
        if original is None:
            web._settings.pop("web_navigation_mode", None)
        else:
            web._settings["web_navigation_mode"] = original


def test_the_console_asks_the_adapter_and_nothing_else():
    text = open(os.path.join(STATIC, "js", "console.js"), encoding="utf-8").read()
    used = sorted(set(re.findall(r"CowDesktopHost\.(\w+)", text)))
    # ``bindContext`` joined the surface with
    # fix-desktop-local-context-and-tool-calls (a picked directory is only
    # published once the server confirmed the binding); ``localContext`` with
    # align-desktop-project-execution-with-master (task 9.3: a reloaded page
    # asks the host what local project is actually open instead of trusting a
    # name it cached). This list is the console's whole native contract: nothing
    # here may reach for the raw ``window.desktopHost`` bridge, and no other
    # adapter method may appear without this test being updated on purpose.
    assert used == [
        "bindContext",
        "canChooseWorkspace",
        "chooseWorkspace",
        "localContext",
        "suspendLocalContext",
    ], used


# ---------------------------------------------------------------------------
# What the projection answers, per role
# ---------------------------------------------------------------------------

def test_every_role_gets_a_shell_on_both_entry_routes(web):
    for username in ("alice", "bob", "root"):
        token = web.login(username)
        for path in ("/chat", "/admin"):
            response = web.get(path, token=token)
            assert response.status == "200 OK", (username, path, response.status)


def test_every_row_is_reachable_or_honestly_refused(web):
    """Each row has a real answer for each role -- and never an unexplained one.

    Two flags travel with a page and they answer different questions:
    ``read_allowed`` is this identity's grant, ``available`` is the page's
    backing consumer. Both combinations are legitimate:

    * open: ``read_allowed`` and no reason;
    * unreadable: refused, with the reason it was refused;
    * readable but the deployment has the capability closed (``consumer_closed``):
      still readable, still *explained* -- which is exactly the 能力关闭必须
      明确展示真实原因 case, and the one a fake "everything works" matrix hides.
    """
    for role, username in (("member", "alice"), ("tenant_admin", "bob")):
        pages = _pages_for(web, username)
        for row, title, page_ids, _views, owner in W_ROWS:
            # A member row is also a tenant administrator's row.
            if owner not in (role,) and not (role == "tenant_admin" and owner == "member"):
                continue
            for page_id in page_ids:
                if page_id is None:
                    continue
                entry = pages.get(page_id)
                assert entry is not None, (role, row, page_id)
                assert isinstance(entry["read_allowed"], bool), (role, row, page_id)
                if entry["read_allowed"]:
                    if entry["available"]:
                        # An open page must not also carry a refusal.
                        assert entry["reason"] == "", (role, row, page_id, entry)
                    else:
                        # Readable, but the page states why it is not usable --
                        # the capability is closed, and the answer is named.
                        assert entry["reason"] in REFUSAL_REASONS, (role, row, page_id, entry)
                else:
                    assert entry["available"] is False, (role, row, page_id, entry)
                    assert entry["reason"] in REFUSAL_REASONS, (role, row, page_id, entry)
                # ``states`` exists only where the capability registry owns the
                # page. Its rule is the leak rule: a page serves real access
                # classes or none at all, never a stale "read" left on a page
                # the caller was just refused.
                states = entry.get("states")
                if states is not None:
                    assert set(states) == {"read", "config", "execution"}, (role, row, page_id)
                    if entry["read_allowed"]:
                        assert any(states.values()), (role, row, page_id, entry)
                    else:
                        assert not any(states.values()), (role, row, page_id, entry)


def test_a_refusal_names_its_reason_instead_of_a_generic_failure(web):
    """``consumer_closed`` and ``no_permission`` are different answers.

    The console branches on them (the deployment has the page closed vs this
    identity may not open it); collapsing them into one string is the regression
    this catches, and an empty reason is the "no real basis" case acceptance.md
    forbids.
    """
    pages = _pages_for(web, "alice")
    refusals = {pid: entry["reason"] for pid, entry in pages.items()
                if entry.get("read_allowed") is False}
    assert refusals, "the fixture must refuse something, or this proves nothing"
    for pid, reason in refusals.items():
        assert isinstance(reason, str) and re.fullmatch(r"[a-z_]+", reason), (pid, reason)
    assert len(set(refusals.values())) >= 2, sorted(set(refusals.values()))


def test_a_menu_denial_is_reported_as_denied_and_never_as_available(web):
    pages = _pages_for(web, "alice")
    denied = [pid for pid, entry in pages.items() if entry.get("menu_denied")]
    assert denied, "the member fixture must hold at least one denied menu row"
    for pid in denied:
        assert pages[pid]["read_allowed"] is False, pid
        assert pages[pid]["available"] is False, pid


def test_the_tenant_admin_reaches_the_management_rows_and_the_member_does_not(web):
    member = _pages_for(web, "alice")
    admin = _pages_for(web, "bob")
    for page in ("admin.members", "admin.roles", "admin.organization", "admin.audit",
                 "admin.token_usage"):
        assert admin[page]["read_allowed"] is True, page
        assert member[page]["read_allowed"] is False, page
        # The member is refused for a *reason*, not by hiding the page.
        assert member[page]["reason"] in REFUSAL_REASONS, (page, member[page])


def test_the_tenant_admins_defaults_cover_the_rows_it_owns():
    granted = {grant[len("nav:"):] for grant in BUILTIN_MENU_DEFAULTS["tenant_admin"]
               if grant.startswith("nav:")}
    for row, _t, pages, _v, owner in W_ROWS:
        if owner != "tenant_admin":
            continue
        for page in pages:
            assert page in granted, (row, page)


def test_the_platform_administrator_reaches_the_platform_rows(web):
    """A PA reaches the platform surface, and a member does not.

    ``admin.tenants`` is the row that must be reachable for a platform
    administrator -- not merely listed. Whether a *consumer* is open
    (品牌设置's is deployment state, reported as ``consumer_closed``) is a
    separate answer, so it is asserted as a real reason rather than as success.
    """
    platform = _pages_for(web, "root")
    assert platform["admin.tenants"]["read_allowed"] is True, platform["admin.tenants"]
    member = _pages_for(web, "alice")
    assert member["admin.tenants"]["read_allowed"] is False, member["admin.tenants"]
    assert member["admin.tenants"]["reason"] in REFUSAL_REASONS
    branding = platform["admin.branding"]
    assert branding["read_allowed"] or branding["reason"] in REFUSAL_REASONS, branding


def test_a_forced_password_change_is_a_gate_and_not_a_broken_shell(web):
    """W14/U0: the shell loads, the business APIs refuse with the contractual code."""
    created = web.service.create_member(
        actor_user_id=web.admin_id, tenant_id=web.tenant_id,
        operation="create-new", username="carol", display_name="Carol",
        temporary_password="TempPass1234", roles=["member"])
    token = web.service.login("carol", "TempPass1234").token
    # The document itself is served (it carries no tenant data)...
    assert web.get("/chat", token=token).status == "200 OK"
    # ...while a business API reports the specific state the console gates on.
    response = web.get("/api/sessions", token=token)
    assert response.status.startswith("403"), response.status
    body = web.json(response)
    assert body.get("code") == "password_change_required", body
    assert created["user_id"]


# ---------------------------------------------------------------------------
# The container boundary in the page sources
# ---------------------------------------------------------------------------

def test_no_web_asset_reaches_for_the_native_bridge():
    """The bridge belongs to the container; the pages use the HTTP surfaces."""
    offenders = []
    for path in _web_sources():
        text = open(path, encoding="utf-8").read()
        rel = os.path.relpath(path, ROOT)
        for marker in ("window.electronAPI", "electronAPI(",
                       "require('electron')", 'require("electron")', "ipcRenderer"):
            if marker in text:
                offenders.append((rel, marker))
        # ``desktopHost`` is the preload's raw namespace: only the adapter reads it.
        if "desktopHost" in text and os.path.basename(path) != "desktop-host.js":
            offenders.append((rel, "desktopHost"))
    assert not offenders, offenders


def test_every_popup_call_site_is_one_the_container_can_serve():
    """A new ``window.open`` is un-reviewed, not necessarily wrong."""
    found = set()
    for path in _web_sources():
        rel = os.path.relpath(path, STATIC).replace(os.sep, "/")
        for line in open(path, encoding="utf-8"):
            if re.search(r"(?<![\w.])window\.open\s*\(", line):
                found.add(rel)
    assert found == set(POPUP_SITES), sorted(found ^ set(POPUP_SITES))


def test_every_same_frame_navigation_target_is_a_shell_route():
    """A literal top-frame target outside the shell would be a dead link."""
    shell_prefixes = {"chat", "admin"}
    offenders = []
    for path in _web_sources():
        rel = os.path.relpath(path, ROOT)
        for number, line in enumerate(open(path, encoding="utf-8"), 1):
            match = re.search(r"(?:location\.(?:assign|replace|href\s*=)"
                              r"|window\.location\s*=)\s*\(?\s*'([^']*)'", line)
            if not match:
                continue
            target = match.group(1)
            if not target.startswith("/"):
                continue
            first = target.lstrip("/").split("/")[0]
            if first not in shell_prefixes:
                offenders.append((rel, number, target))
    assert not offenders, offenders


def test_blank_target_anchors_are_external_or_downloads():
    """A ``target="_blank"`` anchor needs an off-origin href or a download attr.

    Off-origin goes to the system browser; a same-origin href with ``download``
    is a download the host's save-as policy owns; a same-origin content href is
    sent to the isolated content window by the container's popup handler.
    """
    sources = [os.path.join(WEB, "chat.html")]
    for root, _dirs, files in os.walk(os.path.join(WEB, "templates")):
        sources += [os.path.join(root, name) for name in files if name.endswith(".html")]
    offenders = []
    for path in sources:
        text = open(path, encoding="utf-8").read()
        for tag in re.findall(r"<a\b[^>]*target=\"_blank\"[^>]*>", text, flags=re.S):
            allowed = re.search(r'href="(https?://|/api/|/preview|/uploads)', tag) \
                or "download" in tag
            if not allowed:
                offenders.append((os.path.relpath(path, ROOT), tag.strip()[:120]))
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# W19: attachments and files as the container sees them
# ---------------------------------------------------------------------------

#: Where the Web puts a file: the dialog that hands the page a ``File``, and the
#: two endpoints the upload path posts to. A native path API here would be a
#: file grant phase 1 must not create.
_FILE_SERVICE_ROOTS = ("/api/file", "/api/workspace", "/api/knowledge",
                       "/api/logs", "/api/artifacts", "/preview", "/uploads")

#: The handle-based File System Access API. Phase 1 reads what the user picked
#: in a dialog for one upload; a *handle* is a persistent grant, which is phase
#: 2's explicit authorization flow -- never something a page may take for itself.
_FILE_HANDLE_APIS = ("showOpenFilePicker", "showSaveFilePicker",
                     "showDirectoryPicker", "getAsFileSystemHandle",
                     "webkitGetAsEntry")


def _download_contract():
    import json
    path = os.path.join(ROOT, "contracts", "desktop", "v1.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["downloads"]


def test_w19_the_web_offers_uploads_through_its_own_file_dialogs():
    """No desktop branch, no path API: the page posts the bytes it was handed."""
    for name in ("js/console.js", "js/chat/state.js"):
        source = open(os.path.join(STATIC, *name.split("/")), encoding="utf-8").read()
        assert "new FormData()" in source, name
        assert "formData.append('file', file)" in source, name
        # A directory keeps its shape: the server is told each file's path
        # relative to the picked root, never an absolute local path.
        assert "formData.append('relative_paths', relPath)" in source, name
        assert "webkitRelativePath" in source, name
        assert "fetch('/upload'" in source, name
    # The dialogs themselves ship in the shell: one for files, one for a
    # directory (``webkitdirectory``), both hidden until the attach menu asks.
    html = template.render("chat.html")
    assert re.search(r'<input type="file" id="file-input"[^>]*multiple', html), "file dialog"
    assert re.search(r'<input type="file" id="folder-input"[^>]*webkitdirectory', html), "folder dialog"
    assert "accept=" in re.search(r'<input type="file" id="file-input"[^>]*>', html).group(0)


def test_w19_no_page_takes_a_file_handle_for_itself():
    offenders = []
    for path in _web_sources():
        text = open(path, encoding="utf-8").read()
        rel = os.path.relpath(path, ROOT)
        for marker in _FILE_HANDLE_APIS:
            if marker in text:
                offenders.append((rel, marker))
    assert not offenders, offenders
    # The adapter reports phase 2 honestly instead of exposing a path API.
    adapter = open(os.path.join(STATIC, "js", "fork", "desktop-host.js"),
                   encoding="utf-8").read()
    assert "localFiles" in adapter and "feature_unavailable" in adapter
    assert not re.search(r"\b(absolutePath|rootPath|fileHandle|directoryHandle)\b", adapter), adapter


def test_w19_a_dropped_file_cannot_replace_the_shell():
    """Drop is upload-only: the document-level handlers cancel the default.

    Chromium's default for a dropped file is to navigate the frame to
    ``file://…``. The container refuses that navigation anyway (A08/W20), and
    this keeps the drop from ever becoming one -- which is also what makes the
    drop work in the container exactly as it does in a browser.
    """
    offenders = []
    for path in _web_sources():
        lines = open(path, encoding="utf-8").read().splitlines()
        rel = os.path.relpath(path, STATIC).replace(os.sep, "/")
        for number, line in enumerate(lines):
            match = re.search(r"addEventListener\(\s*'(dragover|drop)'", line)
            if not match:
                continue
            window = "\n".join(lines[number:number + 8])
            if "preventDefault" not in window:
                offenders.append((rel, number + 1, match.group(1)))
    assert not offenders, offenders
    # The drop handler itself must consume the drop, not just style the overlay.
    console = open(os.path.join(STATIC, "js", "console.js"), encoding="utf-8").read()
    drop = re.search(r"chatView\.addEventListener\('drop',\s*\(e\)\s*=>\s*\{(.*?)\}\);",
                     console, flags=re.S)
    assert drop, "the chat drop handler is gone"
    body = drop.group(1)
    assert "preventDefault" in body and "stopPropagation" in body, body
    assert "handleFileSelect(e.dataTransfer.files)" in body, body


def test_w19_every_file_url_the_pages_build_is_declared():
    """The container only accepts the prefixes the contract declares.

    A page that starts using a new file endpoint would otherwise work in a
    browser and be silently refused (or mis-routed) in the container, so every
    same-origin file URL literal in the shipped assets is checked against the
    contract's document/attachment prefixes.
    """
    contract = _download_contract()
    declared = tuple(contract["document_prefixes"]) + tuple(contract["attachment_prefixes"])
    forbidden = tuple(contract["forbidden_query_keys"])
    seen = {}
    for path in _web_sources():
        text = open(path, encoding="utf-8").read()
        rel = os.path.relpath(path, STATIC).replace(os.sep, "/")
        for root in _FILE_SERVICE_ROOTS:
            for match in re.finditer(re.escape(root) + r"[^'\"`\s<>)]*", text):
                # A mention in prose ("…the /api/file.") must not be read as a
                # URL, and a path that starts the literal is the one the page
                # actually builds.
                url = match.group(0).rstrip(".,;:")
                if url == root:
                    continue
                seen.setdefault(url, set()).add(rel)
    assert seen, "the audit found no file URLs, so it proves nothing"
    undeclared = sorted(url for url in seen
                        if not any(url.startswith(p + "?") or url.startswith(p + "/")
                                   for p in declared))
    assert not undeclared, undeclared
    leaked = sorted(url for url, _where in seen.items()
                    if any(re.search(rf"[?&]{key}=", url) for key in forbidden))
    assert not leaked, leaked


def test_w19_a_download_keeps_the_session_cookie_and_never_a_query_secret():
    """Downloads are same-origin URLs authorized by the paired Cookie.

    The desktop host refuses a download URL that carries a credential key
    (``forbidden_query_keys``), and the pages must not be relying on one: the
    child session is a Cookie, so a download link needs nothing else.
    """
    contract = _download_contract()
    assert "token" in contract["forbidden_query_keys"]
    text = open(os.path.join(STATIC, "js", "console.js"), encoding="utf-8").read()
    for url in re.findall(r"'/api/[^']*'", text):
        assert not any(re.search(rf"[?&]{key}=", url) for key in contract["forbidden_query_keys"]), url
    # The attachment the user saves is fetched by the host (or by an anchor with
    # the Cookie), never by a URL the page had to authenticate itself into.
    host = open(os.path.join(STATIC, "js", "fork", "desktop-host.js"), encoding="utf-8").read()
    assert "'/api/file'" not in host  # the adapter names no server endpoint of its own
    assert "artifact_ref" not in host or "saveArtifact" in host


def test_w25_the_local_file_actions_are_reachable_and_speak_the_hosts_language():
    """A25: open / reveal / copy path / save as, each with a way to be reached.

    A button with no function does nothing, and a function with no button cannot
    be reached, so both halves are checked against the *assembled* page. The
    reveal button is additionally checked to start hidden: it only means
    something for a file on this machine, and offering it for a server file
    would offer an action that cannot work.
    """
    html = template.render("chat.html")
    assert '<!--#include' not in html, "the served page must be assembled"
    workspace = open(os.path.join(STATIC, "js", "workspace.js"), encoding="utf-8").read()
    host = open(os.path.join(STATIC, "js", "fork", "desktop-host.js"), encoding="utf-8").read()
    source = open(os.path.join(STATIC, "js", "fork", "project-source.js"), encoding="utf-8").read()

    for button, handler in [("ws-btn-external", "openPreviewExternally"),
                            ("ws-btn-download", "downloadPreviewFile"),
                            ("ws-btn-copy", "copyPreviewPath"),
                            ("ws-btn-reveal", "revealPreviewFile")]:
        assert f'id="{button}"' in html, button
        assert f'onclick="{handler}()"' in html, handler
        assert f"function {handler}(" in workspace, handler
    reveal = re.search(r'<button id="ws-btn-reveal"[^>]*>', html).group(0)
    assert 'class="workspace-icon-btn hidden"' in reveal, reveal

    # Every action goes through the one adapter call, which adds the live
    # workspace and answers with the host's own refusal code.
    assert "CowProjectSource.act(" in workspace
    for method in ["projectOpenFile", "projectRevealFile", "projectCopyPath",
                   "projectSaveFileAs"]:
        assert f"'{method}'" in host, method   # published by the page's adapter
        assert f'case "{method}"' not in host  # ...as data, not as a second switch
    assert "PROJECT_ACTION_METHODS" in host and "projectAction" in host
    # The adapter refuses an action with no project rather than inventing one.
    assert "actionRefusal" in source and "no_host" in source

    # The four tooltips and toasts exist in both dictionaries, in every
    # language: the console falls back to the key itself when a string is
    # missing, which would show the user `ws_open_system`.
    for path in [os.path.join(STATIC, "js", "i18n", "core.js"),
                 os.path.join(STATIC, "js", "core", "i18n.js")]:
        text = open(path, encoding="utf-8").read()
        for key in ["ws_open_system", "ws_reveal_file", "ws_save_as", "ws_local_copy_path",
                    "ws_local_opened", "ws_local_revealed", "ws_local_copied",
                    "ws_local_saved_as", "ws_local_no_application",
                    "ws_local_save_as_changed", "ws_local_action_failed",
                    "ws_local_action_unavailable"]:
            assert text.count(key) >= 3, (os.path.basename(path), key, text.count(key))


def test_w25_the_two_ends_agree_on_the_four_action_names():
    """A25: the word the button says and the word the shell acts on are one word.

    The page names an action (``open``) and the shell executes a bridge method
    (``projectOpenFile``); the translation happens in the page's adapter, in a
    table that has to agree with the shell's own. When the two disagreed, both
    ends were individually correct -- the adapter asked, the bridge answered --
    and every click was still refused by name before it reached the shell, which
    is invisible to a test that stubs either side. So the table is compared with
    the main process's, and every method it names is checked to be one the
    bridge actually publishes.
    """
    host = open(os.path.join(STATIC, "js", "fork", "desktop-host.js"),
                encoding="utf-8").read()
    native = open(os.path.join(ROOT, "desktop", "src", "main", "project-browser",
                               "native-actions.ts"), encoding="utf-8").read()
    bridge = open(os.path.join(ROOT, "desktop", "src", "main", "remote",
                               "host-bridge.ts"), encoding="utf-8").read()

    def pairs(source, name):
        """`{ open: 'projectOpenFile', … }` -> {'open': 'projectOpenFile'}."""
        block = re.search(rf"{name}[^=]*=\s*\{{(.*?)\}}", source, re.S)
        assert block, f"{name} is declared in {source[:60]}..."
        found = re.findall(r"([A-Za-z]+)\s*:\s*'([A-Za-z]+)'", block.group(1))
        assert found, f"{name} is a table of pairs"
        return dict(found)

    page_table = pairs(host, "PROJECT_ACTIONS")
    shell_table = pairs(native, "NATIVE_METHOD_BY_ACTION")
    assert page_table == shell_table, (
        "the page's action names and the shell's must be one table: "
        f"page={page_table} shell={shell_table}")

    # Same four, in the order the panel draws them.
    assert list(page_table) == ["open", "reveal", "copyPath", "saveAs"]

    # ...and every method named is one the bridge publishes when local files are
    # open. A method that is not published is refused as `feature_unavailable`,
    # however well the two tables agree with each other.
    phase3 = re.search(r"PHASE3_METHODS = \[(.*?)\]", bridge, re.S)
    assert phase3, "the published method list is declared"
    published = set(re.findall(r"'([A-Za-z]+)'", phase3.group(1)))
    for action, method in page_table.items():
        assert method in published, (action, method, sorted(published))

    # The page's table is data, not a second switch: a switch would let one
    # action mean different things in the two halves of the same file. It is
    # read through `hasOwnProperty`, so a prototype key is not an action name.
    assert host.count("hasOwnProperty.call(PROJECT_ACTIONS, action)") == 1
    assert 'case "projectOpenFile"' not in host
