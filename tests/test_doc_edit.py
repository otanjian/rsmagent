"""Viewing and editing memory files and skill definitions in the web console."""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.workspace.service import WorkspaceConflictError, WorkspaceService
SKILL_MD = """---
name: {name}
description: {desc}
---

# {name}

Instructions.
"""


def _write(path, text):
    """Write LF-only bytes; Path.write_text would translate to CRLF on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _memory(root):
    from agent.memory.service import MemoryService

    return MemoryService(str(root))


def _skill_dir(root, name, desc="does a thing"):
    _write(root / name / "SKILL.md", SKILL_MD.format(name=name, desc=desc))
    return root / name / "SKILL.md"


def _skills(builtin, custom):
    from agent.skills.manager import SkillManager
    from agent.skills.service import SkillService

    builtin.mkdir(parents=True, exist_ok=True)
    custom.mkdir(parents=True, exist_ok=True)
    return SkillService(SkillManager(builtin_dir=str(builtin), custom_dir=str(custom)))


# ----------------------------------------------------------------------
# Memory: the relative path an editor needs
# ----------------------------------------------------------------------
def test_memory_content_reports_the_path_under_the_workspace(tmp_path):
    _write(tmp_path / "MEMORY.md", "# global\n")
    _write(tmp_path / "memory" / "2026-08-26.md", "# daily\n")
    svc = _memory(tmp_path)

    assert svc.get_content("MEMORY.md")["rel_path"] == "MEMORY.md"
    assert svc.get_content("2026-08-26.md")["rel_path"] == "memory/2026-08-26.md"


def test_memory_content_reports_the_path_of_a_dream_and_an_evolution_file(tmp_path):
    _write(tmp_path / "memory" / "dreams" / "2026-08-26.md", "# dreamt\n")
    _write(tmp_path / "memory" / "evolution" / "2026-08-26.md", "# learned\n")
    svc = _memory(tmp_path)

    assert svc.get_content("2026-08-26.md", category="dream")["rel_path"] \
        == "memory/dreams/2026-08-26.md"
    assert svc.get_content("2026-08-26.md", category="evolution")["rel_path"] \
        == "memory/evolution/2026-08-26.md"


def test_memory_rel_path_is_what_the_workspace_editor_can_open(tmp_path):
    """The console hands this path straight to the workspace read/write API, so
    the two views of the same file have to agree."""
    _write(tmp_path / "memory" / "dreams" / "2026-08-26.md", "# dreamt\n")

    loaded = _memory(tmp_path).get_content("2026-08-26.md", category="dream")
    ws = WorkspaceService(str(tmp_path))

    assert ws.read_text(loaded["rel_path"])["content"] == loaded["content"]
    ws.write_text(loaded["rel_path"], "# edited\n")
    assert _memory(tmp_path).get_content("2026-08-26.md", category="dream")["content"] \
        == "# edited\n"


# ----------------------------------------------------------------------
# Skills: read
# ----------------------------------------------------------------------
def test_read_content_returns_a_workspace_skill_as_editable(tmp_path):
    _skill_dir(tmp_path / "custom", "note-taker")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    result = svc.read_content("note-taker")

    assert result["source"] == "custom"
    assert result["editable"] is True
    assert result["ships_with_install"] is False
    assert result["filename"] == "SKILL.md"
    assert "# note-taker" in result["content"]
    assert result["mtime"] > 0


def test_read_content_marks_a_builtin_skill_read_only(tmp_path):
    """Its file ships with the installation, so an upgrade would drop the edit."""
    _skill_dir(tmp_path / "builtin", "image-maker")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    result = svc.read_content("image-maker")

    assert result["source"] == "builtin"
    assert result["editable"] is False
    assert result["ships_with_install"] is True
    # Still readable: the point is to be able to look at what a skill tells the
    # agent to do, whether or not it can be changed here.
    assert "# image-maker" in result["content"]


def test_read_content_rejects_an_unknown_skill(tmp_path):
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    with pytest.raises(FileNotFoundError):
        svc.read_content("no-such-skill")


def test_read_content_requires_a_name(tmp_path):
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    with pytest.raises(ValueError):
        svc.read_content("   ")


# ----------------------------------------------------------------------
# Skills: the files one is made of
# ----------------------------------------------------------------------
def _bundled_skill(root, name="note-taker"):
    """A skill as an upload installs one: instructions plus its resources."""
    _skill_dir(root, name)
    _write(root / name / "scripts" / "run.py", "print(1)\n")
    _write(root / name / "references" / "api.md", "# api\n")
    return root / name


def test_list_files_reports_the_whole_skill_directory(tmp_path):
    """A skill is a directory, so `scripts/` and `references/` are as much part
    of it as its SKILL.md - and are named nowhere in the skill list."""
    _bundled_skill(tmp_path / "custom")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    listed = svc.list_files("note-taker")

    # Depth first, in the order a tree draws them, with each directory followed
    # by what it holds.
    assert [(f["path"], f["depth"], f["is_dir"]) for f in listed["files"]] == [
        ("references", 0, True),
        ("references/api.md", 1, False),
        ("scripts", 0, True),
        ("scripts/run.py", 1, False),
        ("SKILL.md", 0, False),
    ]
    assert listed["ships_with_install"] is False


def test_list_files_puts_the_skill_md_before_its_sibling_files(tmp_path):
    """It is the skill's entry point, not just another file in an alphabet."""
    _skill_dir(tmp_path / "custom", "note-taker")
    _write(tmp_path / "custom" / "note-taker" / "AGENTS.md", "# read me\n")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    paths = [f["path"] for f in svc.list_files("note-taker")["files"]]

    assert paths == ["SKILL.md", "AGENTS.md"]


def test_list_files_leaves_out_caches_and_hidden_entries(tmp_path):
    """Neither is part of the skill anyone wrote, or one they meant to edit."""
    _bundled_skill(tmp_path / "custom")
    _write(tmp_path / "custom" / "note-taker" / "__pycache__" / "run.pyc", "\x00")
    _write(tmp_path / "custom" / "note-taker" / ".DS_Store", "\x00")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    paths = [f["path"] for f in svc.list_files("note-taker")["files"]]

    assert "__pycache__" not in paths
    assert ".DS_Store" not in paths


def test_list_files_says_which_files_can_be_shown_as_text(tmp_path):
    """A bundled asset belongs in the tree - it is part of the skill - but
    opening it in a text editor would only show mojibake."""
    _skill_dir(tmp_path / "custom", "note-taker")
    _write(tmp_path / "custom" / "note-taker" / "logo.png", "\x89PNG\r\n")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    listed = {f["path"]: f for f in svc.list_files("note-taker")["files"]}

    assert listed["SKILL.md"]["text"] is True
    assert listed["logo.png"]["text"] is False
    assert listed["logo.png"]["kind"] == "image"


def test_list_files_stops_before_recursing_through_a_very_deep_tree(tmp_path):
    """The walk is a recursion, and the directories under a skill are not all
    of its own making. Past the cap the listing says it is incomplete rather
    than descending until the stack runs out."""
    _skill_dir(tmp_path / "custom", "note-taker")
    deep = tmp_path / "custom" / "note-taker" / ("/".join("d" * 20))
    _write(deep / "buried.txt", "hi\n")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    listed = svc.list_files("note-taker")

    assert listed["truncated"] is True
    assert max(f["depth"] for f in listed["files"]) == svc.MAX_TREE_DEPTH
    assert not any(f["path"].endswith("buried.txt") for f in listed["files"])


def test_list_files_leaves_out_a_symlink(tmp_path):
    """A link can point anywhere, and reading one is refused for resolving
    outside the skill. Listing it would only offer a row that cannot open."""
    _bundled_skill(tmp_path / "custom")
    link = tmp_path / "custom" / "note-taker" / "elsewhere.md"
    try:
        link.symlink_to(tmp_path / "secret.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need a privilege this platform does not grant")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    paths = [f["path"] for f in svc.list_files("note-taker")["files"]]

    assert "elsewhere.md" not in paths


def test_list_files_rejects_an_unknown_skill(tmp_path):
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    with pytest.raises(FileNotFoundError):
        svc.list_files("no-such-skill")


def test_read_content_reads_a_file_beside_the_skill_md(tmp_path):
    _bundled_skill(tmp_path / "custom")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    result = svc.read_content("note-taker", path="scripts/run.py")

    assert result["content"] == "print(1)\n"
    assert result["filename"] == "scripts/run.py"
    assert result["editable"] is True


def test_read_content_reads_a_bundled_file_of_a_builtin_skill_read_only(tmp_path):
    _bundled_skill(tmp_path / "builtin", "image-maker")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    result = svc.read_content("image-maker", path="scripts/run.py")

    assert result["content"] == "print(1)\n"
    assert result["ships_with_install"] is True
    assert result["editable"] is False


def test_read_content_accepts_a_windows_style_path(tmp_path):
    """A path typed or pasted with backslashes names the same file."""
    _bundled_skill(tmp_path / "custom")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    assert svc.read_content("note-taker", path=r"scripts\run.py")["content"] == "print(1)\n"


def test_read_content_rejects_a_missing_file_and_a_directory(tmp_path):
    _bundled_skill(tmp_path / "custom")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    with pytest.raises(FileNotFoundError):
        svc.read_content("note-taker", path="scripts/nope.py")
    with pytest.raises(ValueError):
        svc.read_content("note-taker", path="scripts")


# ----------------------------------------------------------------------
# Skills: write
# ----------------------------------------------------------------------
def test_write_content_saves_a_workspace_skill(tmp_path):
    target = _skill_dir(tmp_path / "custom", "note-taker")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")
    loaded = svc.read_content("note-taker")

    result = svc.write_content(
        "note-taker",
        loaded["content"] + "\nOne more rule.\n",
        expected_mtime=loaded["mtime"],
    )

    assert target.read_text(encoding="utf-8").endswith("One more rule.\n")
    assert result["size"] == target.stat().st_size


def test_write_content_refuses_a_builtin_skill(tmp_path):
    target = _skill_dir(tmp_path / "builtin", "image-maker")
    original = target.read_bytes()
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    with pytest.raises(ValueError):
        svc.write_content("image-maker", "rewritten\n")
    assert target.read_bytes() == original


def test_write_content_reports_a_mid_edit_rewrite(tmp_path):
    """The agent can rewrite a skill while it is open in the editor."""
    import os

    target = _skill_dir(tmp_path / "custom", "note-taker")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")
    stale = svc.read_content("note-taker")["mtime"]
    os.utime(target, (stale + 60, stale + 60))

    with pytest.raises(WorkspaceConflictError):
        svc.write_content("note-taker", "mine\n", expected_mtime=stale)
    assert "# note-taker" in target.read_text(encoding="utf-8")

    # Without a baseline the save is a deliberate overwrite and goes through.
    svc.write_content("note-taker", "mine\n")
    assert target.read_text(encoding="utf-8") == "mine\n"


def test_write_content_cannot_escape_the_skills_directory(tmp_path):
    outside = _write(tmp_path / "outside.md", "secret\n")
    _skill_dir(tmp_path / "custom", "note-taker")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    # A name is only ever resolved through the loader's index, so one that was
    # never discovered has no file to point at.
    for name in ("../outside.md", "note-taker/../../outside.md", str(outside)):
        with pytest.raises((FileNotFoundError, ValueError)):
            svc.write_content(name, "tampered\n")
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_write_content_saves_a_file_beside_the_skill_md(tmp_path):
    _bundled_skill(tmp_path / "custom")
    target = tmp_path / "custom" / "note-taker" / "scripts" / "run.py"
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")
    loaded = svc.read_content("note-taker", path="scripts/run.py")

    svc.write_content("note-taker", "print(2)\n",
                      expected_mtime=loaded["mtime"], path="scripts/run.py")

    assert target.read_text(encoding="utf-8") == "print(2)\n"
    # The SKILL.md beside it is untouched; a path is not a fallback.
    assert "# note-taker" in (target.parent.parent / "SKILL.md").read_text(encoding="utf-8")


def test_a_path_cannot_escape_the_skill_directory(tmp_path):
    """The per-file path is as attacker-controlled as the skill name, so it gets
    the same containment check - here the workspace editor's own."""
    outside = _write(tmp_path / "custom" / "outside.md", "secret\n")
    _bundled_skill(tmp_path / "custom")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    for path in ("../outside.md", "scripts/../../outside.md", str(outside)):
        with pytest.raises((FileNotFoundError, ValueError)):
            svc.read_content("note-taker", path=path)
        with pytest.raises((FileNotFoundError, ValueError)):
            svc.write_content("note-taker", "tampered\n", path=path)
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_a_workspace_copy_of_a_builtin_skill_is_still_read_only(tmp_path):
    """Startup deletes and re-copies every builtin skill directory into the
    workspace (`_sync_builtin_skills` in app.py). The copy the loader resolves is
    therefore a `custom` one that the next restart replaces regardless, so
    `source` alone is the wrong thing to gate the editor on: an edit accepted
    here would vanish with nothing to say so."""
    _skill_dir(tmp_path / "builtin", "helper", desc="the shipped one")
    custom = _skill_dir(tmp_path / "custom", "helper", desc="the local one")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    loaded = svc.read_content("helper")
    assert loaded["source"] == "custom"
    assert loaded["editable"] is False
    # `source` says `custom` here, so it cannot be what the console explains the
    # refusal with - hence the separate flag.
    assert loaded["ships_with_install"] is True

    with pytest.raises(ValueError):
        svc.write_content("helper", SKILL_MD.format(name="helper", desc="edited"))
    assert "the local one" in custom.read_text(encoding="utf-8")


def test_write_content_refreshes_what_the_skill_list_shows(tmp_path):
    """Name and description come from the frontmatter the editor just changed."""
    _skill_dir(tmp_path / "custom", "note-taker", desc="old summary")
    svc = _skills(tmp_path / "builtin", tmp_path / "custom")

    svc.write_content("note-taker", SKILL_MD.format(name="note-taker", desc="new summary"))

    listed = {s["name"]: s for s in svc.query()}
    assert listed["note-taker"]["description"] == "new summary"


# ----------------------------------------------------------------------
# HTTP handlers
# ----------------------------------------------------------------------
def _db_scope_ctx():
    from auth.runtime import RequestContext
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield RequestContext(
            user_id="u_admin", username="admin", display_name="Admin",
            is_platform_admin=True, must_change_password=False,
            tenant_id="tnt_test", membership={"id": "m1"},
            permissions={"skill.read", "skill.edit", "memory.read"},
            is_tenant_admin=True,
        )

    return scope()


def _get(handler_cls, params):
    from channel.web import web_channel

    # The keyword arguments a handler passes to web.input() are the defaults for
    # parameters the request did not send, so it may read one this test never
    # passed. Merged here rather than making every test name every parameter.
    def _input(**defaults):
        return web_channel.web.storage(**dict(defaults, **params))

    with patch.object(web_channel, "_db_scope", _db_scope_ctx), \
         patch.object(web_channel, "_require_catalog_read"), \
         patch.object(web_channel, "_require_read_permission"), \
         patch.object(web_channel, "_require_resource_action"), \
         patch.object(web_channel, "_require_tenant_agent_binding",
                      lambda ctx, agent_id: agent_id or "primary"), \
         patch.object(web_channel, "_require_private_owner"), \
         patch.object(web_channel.web, "header"), \
         patch.object(web_channel.web, "input", side_effect=_input):
        return json.loads(handler_cls().GET())


def _post(handler_cls, body):
    from channel.web import web_channel

    with patch.object(web_channel, "_db_scope", _db_scope_ctx), \
         patch.object(web_channel, "_require_catalog_read"), \
         patch.object(web_channel, "_require_read_permission"), \
         patch.object(web_channel, "_require_resource_action"), \
         patch.object(web_channel, "_require_tenant_agent_binding",
                      lambda ctx, agent_id: agent_id or "primary"), \
         patch.object(web_channel, "_require_private_owner"), \
         patch.object(web_channel.web, "header"), \
         patch.object(web_channel.web, "data", return_value=json.dumps(body).encode()):
        return json.loads(handler_cls().POST())


def test_skill_content_handler_serves_and_saves(tmp_path):
    from channel.web.web_channel import SkillContentHandler

    target = _skill_dir(tmp_path / "skills", "console-editable")

    with patch("channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)):
        loaded = _get(SkillContentHandler, {"name": "console-editable"})
        assert loaded["status"] == "success"
        assert loaded["editable"] is True

        saved = _post(SkillContentHandler, {
            "name": "console-editable",
            "content": "# rewritten\n",
            "expected_mtime": loaded["mtime"],
        })

    assert saved["status"] == "success"
    assert target.read_text(encoding="utf-8") == "# rewritten\n"


def test_skill_content_handler_serves_and_saves_a_bundled_file(tmp_path):
    """The console can reach the files beside a skill's SKILL.md, which is the
    only way a skill installed as a folder is editable at all."""
    from channel.web.web_channel import SkillContentHandler

    _bundled_skill(tmp_path / "skills", "console-editable")
    target = tmp_path / "skills" / "console-editable" / "scripts" / "run.py"

    with patch("channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)):
        loaded = _get(SkillContentHandler,
                      {"name": "console-editable", "path": "scripts/run.py"})
        assert loaded["status"] == "success"
        assert loaded["content"] == "print(1)\n"

        saved = _post(SkillContentHandler, {
            "name": "console-editable",
            "path": "scripts/run.py",
            "content": "print(2)\n",
            "expected_mtime": loaded["mtime"],
        })

    assert saved["status"] == "success"
    assert target.read_text(encoding="utf-8") == "print(2)\n"


def test_skill_files_handler_lists_what_a_skill_holds(tmp_path):
    from channel.web.web_channel import SkillFilesHandler

    _bundled_skill(tmp_path / "skills", "console-editable")

    with patch("channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)):
        listed = _get(SkillFilesHandler, {"name": "console-editable"})
        missing = _get(SkillFilesHandler, {"name": "no-such-skill"})
        unnamed = _get(SkillFilesHandler, {"name": ""})

    assert listed["status"] == "success"
    assert [f["path"] for f in listed["files"]] == [
        "references", "references/api.md", "scripts", "scripts/run.py", "SKILL.md",
    ]
    assert missing["status"] == "error"
    assert unnamed["status"] == "error"


def test_skill_content_handler_reports_a_conflict_code(tmp_path):
    from channel.web.web_channel import SkillContentHandler

    target = _skill_dir(tmp_path / "skills", "console-editable")
    original = target.read_text(encoding="utf-8")

    with patch("channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)):
        response = _post(SkillContentHandler, {
            "name": "console-editable",
            "content": "mine\n",
            "expected_mtime": target.stat().st_mtime - 60,
        })

    assert response["code"] == "conflict"
    assert target.read_text(encoding="utf-8") == original


def test_the_skill_viewer_routes_are_wired_into_the_url_table():
    from channel.web import web_channel

    urls = web_channel.URLS
    assert urls[urls.index('/api/skills/content') + 1] == 'SkillContentHandler'
    assert urls[urls.index('/api/skills/files') + 1] == 'SkillFilesHandler'


def test_skill_content_handler_requires_a_name_and_string_content(tmp_path):
    from channel.web.web_channel import SkillContentHandler

    with patch("channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)):
        assert _get(SkillContentHandler, {"name": ""})["status"] == "error"
        assert _post(SkillContentHandler, {"name": "x", "content": None})["status"] == "error"


def test_memory_content_handler_includes_the_editable_path(tmp_path):
    """The compat read reports the workspace-relative path the editor writes to.

    The target has to be named (task 5.1/5.2 of
    ``complete-database-capability-parity``): a request with neither ``scope``
    nor ``agent_id`` is the caller's *personal* memory, whose root is not the
    workspace, so it has no workspace-relative path to report. Only an
    Agent-addressed entry does, and that is what this test is about.
    """
    from channel.web.web_channel import MemoryContentHandler

    _write(tmp_path / "MEMORY.md", "# global\n")

    with patch("channel.web.memory_console._agent_binding",
               return_value={"tenant_id": "tnt_test",
                             "private_owner_user_id": None}), \
         patch("channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)):
        response = _get(MemoryContentHandler, {"filename": "MEMORY.md",
                                               "category": "memory",
                                               "agent_id": "primary"})

    assert response["status"] == "success"
    assert response["rel_path"] == "MEMORY.md"
    assert response["scope"] == "shared", "an unowned Agent is the shared scope"


# ----------------------------------------------------------------------
# Frontend contract
# ----------------------------------------------------------------------
def _read(rel):
    return (Path(__file__).parents[1] / rel).read_text(encoding="utf-8")


def _web(rel):
    return _read(f"channel/web/{rel}")


def test_document_editor_is_loaded_before_its_users():
    """console.js builds both editors at load time, so the factory has to exist
    by then; `defer` keeps the scripts in document order."""
    html = _web("chat.html")

    assert html.index("assets/js/doc-editor.js") < min(html.index("assets/js/views/doc-viewers.js"), html.index("assets/js/views/skills.js"), html.index("assets/js/fork/views/memory.js"))
    # A cached copy of the old page would ask for a script that has since been
    # renamed, so the page has to serve the new file and stamp it. The stamp is
    # applied to every first-party asset at assembly time
    # (channel/web/core/template.py), so this reads the answer the browser gets
    # instead of a list of names a handler used to maintain by hand.
    from channel.web import web_channel

    with patch("web.header"):
        served = web_channel.ChatHandler().GET()
    assert re.search(r'assets/js/doc-editor\.js\?v=[0-9a-f]+', served)


def test_console_scripts_are_served_cache_busted():
    """Every first-party asset the page pulls in gets a version stamp at serve
    time. An asset left off that list keeps running from the browser cache,
    which is how a previous per-tab-save identity editor can outlive the
    upgrade that replaced it with the unified-save one."""
    from channel.web import web_channel

    with patch("web.header"):
        html = web_channel.ChatHandler().GET()

    for asset in ("js/core/nav.js", "js/fork/extensions.js", "js/core/auth.js", "js/identity-admin.js", "js/todos.js",
                  "js/scenes/index.js", "css/base.css", "css/fork/extensions.css"):
        assert f'assets/{asset}?v=' in html, f"{asset} is not version-stamped"
        # No unversioned reference may survive, or the browser reloads the
        # cached copy through that one instead.
        assert f'assets/{asset}"' not in html, f"{asset} still served unversioned"

    # The fork fragments are fetched at runtime by fragments.js through a
    # markup attribute the assembler does not scan, so they are stamped by the
    # handler after assembly. Missed, a browser keeps mounting the previous
    # dialog until its cache entry expires.
    assert re.search(r'assets/fragments/appearance-dialog\.html\?v=[0-9a-f]+', html), \
        "the fork fragment markup is not version-stamped"
    assert re.search(r'assets/js/fragments\.js\?v=[0-9a-f]+', html), \
        "the fragment loader is not version-stamped"


def test_document_editor_contract():
    editor = _web("static/js/doc-editor.js")

    assert "function createDocEditor(" in editor
    # A text area normalizes CRLF to LF in its value, so the dirty baseline has
    # to come from the mounted element rather than the response text - otherwise
    # a CRLF document looks edited the moment it opens.
    assert "baseline = mount(body, data.content, ratio).value;" in editor
    # Assigning `value` parks the caret at the end of the text, so focusing the
    # text area used to scroll a long document to its last line. Both the caret
    # and the scroll follow how far down the reader already was.
    assert "const ratio = _docScrollRatio(body);" in editor
    assert "ta.setSelectionRange(offset, offset);" in editor
    assert "_docApplyScrollRatio(ta, ratio || 0);" in editor
    # And the same on the way back, or saving would jump to the top.
    assert "_docApplyScrollRatio(body, ratio);" in editor
    # The guard clears the editing flag before retrying, so discarding has to
    # retry through exit(); retrying through cancel() would hit its own
    # `if (!editing) return` and leave the text area on screen.
    assert "if (!guard(exit)) return;" in editor
    # An untouched document must not be rewritten, and Ctrl+S must not be able
    # to race a second write against the first one's mtime.
    assert "if (saving) return;" in editor
    assert "if (!force && !isDirty())" in editor
    assert "data.code === 'conflict'" in editor
    # Whatever was read last is what leaving the editor redraws, or a save shows
    # the copy from before the agent's rewrite.
    assert "target.content = data.content;" in editor


def _i18n_count(root, needle):
    """Occurrences of ``needle`` in the split locale namespaces (task 8.5).

    The dictionaries now live in ``channel/web/static/js/i18n/*.js`` and are
    merged by the console at load time, so a key present in all three locales
    shows up three times across those files.
    """
    # Source owns shared keys; fork namespaces fill missing keys at startup.
    shared = (root / "channel/web/static/js/core/i18n.js").read_text(encoding="utf-8")
    count = len(re.findall(r"(?m)^\s*" + re.escape(needle) + r"\s*:", shared))
    if count:
        return count
    return sum(path.read_text(encoding="utf-8").count('"%s":' % needle)
               for path in (root / "channel/web/static/js/i18n").glob("*.js"))


def test_memory_and_skill_editor_wiring():
    root = Path(__file__).parents[1]
    from channel.web.core.template import render
    from conftest import console_js
    html = render('chat.html')
    console = console_js()
    css = _web("static/css/console.css")

    for ident in ("memory-btn-edit", "memory-btn-save", "memory-btn-cancel",
                  "skills-panel-viewer", "skill-viewer-content", "skill-viewer-title",
                  "skill-viewer-readonly", "skill-btn-edit", "skill-btn-save",
                  "skill-btn-cancel", "skill-files-panel", "skill-files-tree",
                  "skill-files-toggle"):
        assert f'id="{ident}"' in html, ident
    assert 'onclick="memoryEditor.start()"' in html
    assert 'onclick="skillEditor.start()"' in html
    assert 'onclick="closeSkillViewer()"' in html

    assert "const memoryEditor = createDocEditor({" in console
    assert "const skillEditor = createDocEditor({" in console
    assert "textarea.doc-editor" in css

    # Memory files stay in the agent's state root. Passing the chat session
    # would resolve the same relative path inside whatever project that session
    # has open, and edit or create the wrong file.
    for fn in ("docReadFile", "docWriteFile"):
        body = re.search(rf"async function {fn}\(.*?\n\}}", console, re.S).group(0)
        assert "session" not in body, fn

    # A skill is addressed by name: which directory a name resolves to is the
    # loader's business, and a builtin one sits outside the workspace. `path`
    # then names one file inside it, so the files beside a skill's SKILL.md are
    # reachable at all.
    assert "name=${encodeURIComponent(name)}&resource_id=" in console
    assert "`/api/skills/content?${query}`" in console
    assert "fetch('/api/skills/content'" in console
    assert "`/api/skills/files?name=${encodeURIComponent(name)}&resource_id=" in console

    # A directory in the tree folds rather than opening anything, and a fold
    # takes everything below it - nested directories included.
    assert "function toggleSkillDir(" in console
    assert "if (path.startsWith(`${dir}/`)) return true;" in console
    # The size comes from the listing rather than from a second request per row.
    assert "formatSkillFileSize(file.size)" in console

    # The whole list folds away for a full-width document. The switch has to be
    # outside the panel it hides, or folding it would take the way back with it.
    assert 'onclick="toggleSkillFilesPanel()"' in html
    assert 'id="skill-files-toggle"' in html.split('id="skill-files-panel"')[0]
    assert "localStorage.setItem(SKILL_FILES_PANEL_KEY" in console

    # The reason shown for a read-only skill comes from the server's flag. The
    # workspace copy of a builtin reads back as `custom`, so keying off `source`
    # would explain those - the common case - as an unsupported file type.
    reason = re.search(r"function skillReadonlyReason\(.*?\n\}", console, re.S).group(0)
    assert "data.ships_with_install" in reason
    assert "data.source" not in reason

    # Leaving the page, going back to the list or closing the tab all discard an
    # open editor, so each has to ask first.
    assert "if (!docGuardUnsaved(() => navigateTo(viewId, onArrive || tab, routeOptions))) return false;" in console
    assert "if (!memoryEditor.guard(closeMemoryViewer)) return;" in console
    assert "if (!skillEditor.guard(closeSkillViewer)) return;" in console
    assert "if (!memoryEditor.isDirty() && !skillEditor.isDirty() && !knowledgeEditor.isDirty() && !brandingDirty) return;" in console
    # Including another file of the same skill taking the text area over.
    assert "if (!skillEditor.guard(() => selectSkillFile(path))) return;" in console
    assert ("if (!memoryEditor.isDirty() && !skillEditor.isDirty() "
            "&& !knowledgeEditor.isDirty()) return;") in console

    # Every string these views show must exist in all three locales. Task 8.5
    # moved the dictionaries into per-domain namespace files, so the count runs
    # over the merged layer (console.js + static/js/i18n/*.js).
    for key in ("skill_back", "skill_open_hint", "skill_load_failed",
                "skill_builtin_readonly", "skill_files_title", "skill_file_not_text"):
        assert _i18n_count(root, key) == 3, key


# ----------------------------------------------------------------------
# Desktop client
# ----------------------------------------------------------------------
def _desktop(rel):
    return _read(f"desktop/src/renderer/src/{rel}")


def test_desktop_doc_editor_shares_one_implementation():
    """Memory and skills differ only in how a document is addressed, so the
    editing rules live in one factory rather than a copy per page."""
    store = _desktop("store/docEditorStore.ts")

    # The same safeguards the workspace panel and the web console carry.
    assert "if (res.code === 'conflict') {" in store
    assert "if (!force && !edit.dirty) {" in store
    assert "if (!edit || edit.saving) return" in store
    # A text area reports CRLF as LF, so the baseline has to be normalized or
    # every CRLF file looks edited the moment it opens.
    assert r"res.content.replace(/\r\n/g, '\n')" in store
    # A response for a document the user has already left must not overwrite
    # what is on screen.
    assert "if (mine !== seq) return" in store

    # Built at module scope: an unsaved edit has to outlive its page being
    # unmounted by a route change, which is also what lets the guard find it.
    for rel in ("pages/MemoryPage.tsx", "pages/SkillsPage.tsx", "pages/KnowledgePage.tsx"):
        src = _desktop(rel)
        assert re.search(r"^const \w+Editor = createDocEditorStore", src, re.M), rel


def test_desktop_memory_editing_stays_in_the_state_root():
    """Memory files are anchored to the agent's state root. Passing a session
    would resolve the same relative path inside whatever project that session has
    open, and edit or create the wrong file."""
    page = _desktop("pages/MemoryPage.tsx")
    client = _desktop("api/client.ts")

    factory = re.search(r"createDocEditorStore<MemoryRef.*?\n\}\)", page, re.S).group(0)
    assert "workspaceRead(doc.relPath)" in factory
    assert "path: doc.relPath" in factory
    assert "session" not in factory

    # The path has to come from the backend: the list only knows a file's name
    # and category, while the read and write endpoints take a path.
    assert "async getMemoryDoc(" in client
    assert "meta.rel_path" in page


def test_desktop_skill_editing_is_addressed_by_name():
    """Which directory a skill name resolves to is the loader's business, and a
    builtin skill sits outside the workspace. `path` then names one file inside
    it, so the files beside a skill's SKILL.md are reachable at all."""
    page = _desktop("pages/SkillsPage.tsx")
    client = _desktop("api/client.ts")

    assert "name=${encodeURIComponent(name)}" in client
    assert "`/api/skills/content?${query}`" in client
    assert "this.request('/api/skills/content'" in client
    assert "expected_mtime: args.expectedMtime ?? null," in client
    assert '`/api/skills/files?name=${encodeURIComponent(name)}&resource_id=${encodeURIComponent(resourceId || "")}`' in client

    # Switching files goes through `open`, which is what asks about an unsaved
    # edit before another file's contents take the text area over.
    assert "await skillEditor.getState().open({ ...doc, path })" in page
    # The file, not just the skill: two files of one skill have to read as two
    # documents, or a late response would be dropped as a duplicate.
    assert "keyOf: (doc) => `${doc.resourceId || doc.name}/${doc.path || ''}`" in page

    # A directory in the tree folds rather than opening anything, and the folds
    # belong to the skill whose tree they were made in - so they are held beside
    # the listing, and cleared with it rather than by unmounting the tree.
    assert "if (path.startsWith(`${dir}/`)) return true" in page
    assert "setFoldedDirs(new Set())" in page
    # The size comes from the listing rather than from a second request per row.
    assert "formatSkillFileSize(file.size)" in page
    # The whole list folds away, remembered under the key the web console uses.
    assert "localStorage.setItem(SKILL_FILES_PANEL_KEY" in page
    assert "const SKILL_FILES_PANEL_KEY = 'cow_skill_files_panel'" in page

    # The reason shown for a read-only skill comes from the server's flag: the
    # workspace copy of a builtin reads back as `custom`, so keying off `source`
    # would explain those - the common case - as an unsupported file type.
    factory = re.search(r"createDocEditorStore<SkillRef.*?\n\}\)", page, re.S).group(0)
    assert "data.ships_with_install" in factory
    assert "data.source" not in factory

    # Returning to the list has to re-read it: a saved edit can change the name
    # and description the cards show, which live in the file's frontmatter.
    assert "void loadData()" in page


def test_desktop_knowledge_editing_saves_through_the_reindexing_endpoint():
    """A knowledge page is answered from the vector index, so a save that went
    through the workspace endpoint would leave search quoting the old text."""
    page = _desktop("pages/KnowledgePage.tsx")

    factory = re.search(r"createDocEditorStore<KnowledgeRef.*?\n\}\)", page, re.S).group(0)
    assert "action: 'update_document'" in factory
    assert "workspaceWrite" not in factory
    assert "expected_mtime: expectedMtime" in factory
    # That endpoint reports a conflict inside its payload; `code` there is the
    # HTTP status, which the editor would read as "no conflict".
    assert "payload.conflict ? 'conflict' : undefined" in factory

    # A saved H1 is the page's title in the tree, and its size feeds the stats
    # line, so the list has to be re-read once the write lands.
    assert "state.content !== prev.content) void refresh()" in page


def test_desktop_editor_opens_where_the_reader_was():
    """The text area replaces the rendered document rather than scrolling it,
    so a reader halfway down a long page would land back at the top - and,
    because focusing follows the caret, on the web at the very end."""
    editor = _desktop("components/DocEditor.tsx")

    assert "export function DocView<D>(" in editor
    assert "el.setSelectionRange(offset, offset)" in editor
    # Definition, then one restore on each side of the swap, and one hand-off
    # from each as it is unmounted.
    assert editor.count("applyScrollRatio(") == 3
    assert editor.count("scrollMemory.set(") == 2

    # Both halves of the swap have to be wired, or the position has nowhere to
    # come from on one side of it.
    for rel in ("pages/MemoryPage.tsx", "pages/SkillsPage.tsx", "pages/KnowledgePage.tsx"):
        src = _desktop(rel)
        assert re.search(r"<DocView store=\{\w+Editor\}>", src), rel
        assert "<DocEditor" in src, rel


def test_desktop_doc_editors_are_guarded_on_the_way_out():
    """Leaving the page unmounts the text area, so every exit has to ask first -
    and ask *before* it commits, or declining cannot call it off."""
    assert "if (!(await guardDocEditors())) return" in _desktop("layout/NavRail.tsx")
    assert "if (!(await guardDocEditors())) return" in _desktop("App.tsx")

    # Back to the list, and switching the memory tab, both drop the editor too.
    assert "memoryEditor.getState().close()" in _desktop("pages/MemoryPage.tsx")
    assert "if (!(await memoryEditor.getState().guard())) return" in _desktop("pages/MemoryPage.tsx")
    assert "skillEditor.getState().close()" in _desktop("pages/SkillsPage.tsx")
    # On the knowledge page, the Agent picker and the graph tab both replace
    # what the text area is editing.
    assert (_desktop("pages/KnowledgePage.tsx")
            .count("if (!(await knowledgeEditor.getState().guard())) return") == 2)


def test_desktop_doc_editor_seeds_declaratively():
    """An imperative `el.value =` in a mount-only effect is not reliable under
    StrictMode's double invoke, and writing '' for a missing text area would
    empty the file."""
    editor = _desktop("components/DocEditor.tsx")

    assert "defaultValue={edit.loaded}" in editor
    assert "autoFocus" in editor
    assert "stashText(el.value)" in editor
    # Saving falls back to nothing when the text area is missing, rather than to
    # '' - which would write an empty file over the user's content.
    assert "if (el) void save(el.value)" in editor
    assert "save(ref.current?.value ?? '')" not in editor

    # Every string these pages show must exist in both locales.
    i18n = _desktop("i18n.ts")
    for key in ("doc_edit", "doc_edit_save", "skill_back", "skill_open_hint",
                "skill_builtin_readonly", "knowledge_doc_readonly",
                "skill_files_title", "skill_file_not_text"):
        assert i18n.count(f"{key}:") == 2, key
