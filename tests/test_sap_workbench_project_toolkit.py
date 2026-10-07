"""项目目录产物安装：幂等、可升级旧修订、绝不静默覆盖用户改动。

这三条不是风格选择：第一条保证重复保存配置不会堆积第二份产物，第二条保证
旧宿主已经写过的同名插件能被迁移，第三条保证用户的改动不被丢弃。
"""
from hashlib import sha256
import json
from pathlib import Path

import pytest

from Scene.sap_workbench.backend import project_toolkit

SKILL = ".opencode/skills/sap-workbench/SKILL.md"
PLUGIN = ".opencode/plugins/rsm-sap-workbench-navigation.js"


def results(report):
    return {item["path"]: item["result"] for item in report["artifacts"]}


def source_root():
    return Path(project_toolkit._ROOT)


def test_fresh_install_writes_every_artifact(tmp_path):
    report = project_toolkit.install(tmp_path)
    assert report["conflicts"] == []
    assert all(value == "installed" for value in results(report).values())
    # The skill lands where OpenCode discovers config-directory skills, and the
    # plugin where it auto-discovers plugins.
    assert (tmp_path / SKILL).is_file()
    assert (tmp_path / PLUGIN).is_file()
    assert "name: sap-workbench" in (tmp_path / SKILL).read_text(encoding="utf-8")
    references = list((tmp_path / ".opencode/skills/sap-workbench/references").glob("*.md"))
    assert len(references) == 2
    # The project config denies the SAP tool by default, so it is not presented
    # to ordinary coding sessions of the same project. The scene's own sessions
    # re-enable it with a session-level allow from the platform entry.
    assert report["permission"] == "installed"
    config = json.loads((tmp_path / project_toolkit.CONFIG_FILE).read_text(encoding="utf-8"))
    assert config["permission"] == {tool: "deny" for tool in project_toolkit.HIDDEN_TOOLS}


def test_the_permission_rule_is_idempotent(tmp_path):
    project_toolkit.install(tmp_path)
    target = tmp_path / project_toolkit.CONFIG_FILE
    written = target.read_text(encoding="utf-8")
    second = project_toolkit.install(tmp_path)
    assert second["permission"] == "unchanged"
    assert target.read_text(encoding="utf-8") == written


def test_a_user_permission_choice_is_not_overwritten(tmp_path):
    project_toolkit.install(tmp_path)
    target = tmp_path / project_toolkit.CONFIG_FILE
    navigation, *others = project_toolkit.HIDDEN_TOOLS
    choice = {"permission": {navigation: "allow"}, "model": "acme/model"}
    target.write_text(json.dumps(choice), encoding="utf-8")
    report = project_toolkit.install(tmp_path)
    # "A deny may not be in effect" is what the caller has to know, and it is
    # reported even though the tool the user said nothing about still had to be
    # added.
    assert report["permission"] == "user_set"
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert merged["model"] == "acme/model"
    # The explicit choice is preserved verbatim...
    assert merged["permission"][navigation] == "allow"
    # ...while the tool the user never mentioned still gets its default deny.
    assert all(merged["permission"][tool] == "deny" for tool in others)


def test_existing_json_config_is_merged_not_replaced(tmp_path):
    target = tmp_path / project_toolkit.CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"model": "acme/model", "permission": {"bash": "ask"}}), encoding="utf-8")
    report = project_toolkit.install(tmp_path)
    assert report["permission"] == "installed"
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert merged["model"] == "acme/model"
    assert merged["permission"] == {"bash": "ask",
                                    **{tool: "deny" for tool in project_toolkit.HIDDEN_TOOLS}}


def test_a_jsonc_config_is_refused_rather_than_mangled(tmp_path):
    """We may not be able to add the rule to a config with comments.

    A blind rewrite would destroy the user's comments, so the merge is reported
    as ``unmergeable`` and the interface must not claim the tool was narrowed.
    """
    target = tmp_path / project_toolkit.CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    original = '{\n  // keep my comment\n  "model": "acme/model"\n}\n'
    target.write_text(original, encoding="utf-8")
    report = project_toolkit.install(tmp_path)
    assert report["permission"] == "unmergeable"
    assert target.read_text(encoding="utf-8") == original


def test_install_is_idempotent_and_writes_no_duplicate(tmp_path):
    project_toolkit.install(tmp_path)
    before = {path: (tmp_path / path).read_text(encoding="utf-8") for path in results(project_toolkit.install(tmp_path))}
    second = project_toolkit.install(tmp_path)
    assert all(value == "unchanged" for value in results(second).values())
    assert second["conflicts"] == []
    for path, content in before.items():
        assert (tmp_path / path).read_text(encoding="utf-8") == content


def test_a_crlf_copy_is_recognised_as_the_same_revision(tmp_path):
    """A Windows checkout legitimately stores the same revision with CRLF.

    Written with ``newline=""`` on purpose: the default text mode would translate
    the ``\\n`` of an already-CRLF string a second time and produce ``\\r\\r\\n``,
    testing a file that no checkout would ever contain.
    """
    project_toolkit.install(tmp_path)
    target = tmp_path / PLUGIN
    content = target.read_text(encoding="utf-8")
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(content.replace("\n", "\r\n"))
    assert results(project_toolkit.install(tmp_path))[PLUGIN] == "unchanged"


def test_a_previous_released_revision_is_upgraded(tmp_path):
    """Migration: the old per-session host wrote this same file name."""
    project_toolkit.install(tmp_path)
    legacy = "// legacy revision shipped by the retired scene-owned engine host\n"
    target = tmp_path / PLUGIN
    target.write_text(legacy, encoding="utf-8")
    previous = project_toolkit.UPGRADEABLE_REVISIONS
    try:
        project_toolkit.UPGRADEABLE_REVISIONS = frozenset(
            {*previous, sha256(project_toolkit.normalise(legacy).encode("utf-8")).hexdigest()})
        assert results(project_toolkit.install(tmp_path))[PLUGIN] == "upgraded"
        assert "legacy revision" not in target.read_text(encoding="utf-8")
    finally:
        project_toolkit.UPGRADEABLE_REVISIONS = previous


def test_a_user_edit_is_reported_and_not_overwritten(tmp_path):
    project_toolkit.install(tmp_path)
    target = tmp_path / SKILL
    edited = target.read_text(encoding="utf-8") + "\n## 本地补充\n\n这是我们自己加的流程。\n"
    target.write_text(edited, encoding="utf-8")
    report = project_toolkit.install(tmp_path)
    assert results(report)[SKILL] == "conflict"
    assert report["conflicts"] == [SKILL]
    assert target.read_text(encoding="utf-8") == edited
    # The untouched artifacts still reconcile on the same pass.
    assert results(report)[PLUGIN] == "unchanged"


def test_a_missing_project_directory_is_refused(tmp_path):
    with pytest.raises(ValueError, match="project_directory_missing"):
        project_toolkit.install(tmp_path / "absent")


def test_nested_directories_are_created(tmp_path):
    project_toolkit.install(tmp_path)
    assert (tmp_path / ".opencode/skills/sap-workbench/references").is_dir()


def test_every_declared_artifact_has_a_shipped_source():
    """A declared artifact with no shipped source must be reported, never skipped
    as success -- the scene tree is the single source of what gets installed."""
    for relative, _ in project_toolkit.ARTIFACTS:
        assert (project_toolkit._ROOT / relative).is_file(), relative


def test_the_plugin_the_retired_host_installs_can_be_replaced(tmp_path):
    """The common migration case, and the one that is easy to get wrong.

    A project that ran the per-session host holds *that host's* plugin file, which
    is neither our new content nor a user edit. The two digests recorded here from
    the host are earlier revisions; the file a deployed host actually has on disk
    is the current `navigation-guidance.js`. If it is not recorded, migration reads
    the host's own file as a foreign edit and refuses to replace it, leaving a
    host-mode plugin behind.
    """
    legacy = (Path(__file__).resolve().parents[1]
              / "Scene/sap_workbench/opencode_adapter/navigation-guidance.js")
    if not legacy.is_file():
        pytest.skip("the retired host plugin is already removed")
    # Pinned as a literal in the module, so it survives the legacy file's removal.
    assert "0412f4efe704df384f677e8705aa7b228c88b0e3263607554d1facce7ff4ade7" \
        in project_toolkit.UPGRADEABLE_REVISIONS
    project_toolkit.install(tmp_path)
    target = tmp_path / PLUGIN
    target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
    assert results(project_toolkit.install(tmp_path))[PLUGIN] == "upgraded"
    assert "sap_transaction_open" in target.read_text(encoding="utf-8")


def test_the_shipped_plugin_gates_on_the_variable_the_launcher_exports():
    """The plugin's gate and the launcher's environment must name the same variable.

    This is the defect that made a saved, enabled workbench offer no navigation at
    all: the retired per-session host exported `RSM_SAP_WORKBENCH_NAVIGATION` and
    the plugin it installed gated on that, so in the shared service -- which never
    exports it -- the plugin returned no tool while everything still looked
    configured. Two halves that must agree are worth pinning together.
    """
    plugin = (source_root() / "plugins"
              / "rsm-sap-workbench-navigation.js").read_text(encoding="utf-8")
    # The launcher lives beside the checkout rather than inside it.
    candidates = [source_root().parents[offset] / "scripts" / "start-opencode-web.ps1"
                  for offset in (3, 2, 1)]
    launcher = next((path for path in candidates if path.is_file()), None)
    if launcher is None:
        pytest.skip("the launcher ships outside this checkout")
    script = launcher.read_text(encoding="utf-8")

    # The gate the plugin reads, and the variable the launcher exports, must match.
    assert "process.env.RSM_SAP_WORKBENCH_BRIDGE_URL" in plugin
    assert "$env:RSM_SAP_WORKBENCH_BRIDGE_URL" in script
    # The retired host's gate must not survive on either side: a plugin gated on it
    # registers nothing under the shared service, which is silent, not loud.
    assert "RSM_SAP_WORKBENCH_NAVIGATION" not in plugin
    assert "RSM_SAP_NAVIGATION_HOST" not in plugin
