"""Guardrails for the architecture a macOS desktop artifact is built for.

electron-builder resolves the architectures it will build from two places, and
the package config beats the command line:

  * the CLI flags the release workflow passes per matrix job
    (``--mac --arm64`` / ``--mac --x64``), which arrive as an
    ``arch -> targets`` map with *empty* target lists;
  * ``build.mac.target`` in ``desktop/package.json``.

``computeArchToTargetNamesMap`` in ``app-builder-lib`` hands that CLI map back
untouched only while every target list stays empty; the first ``mac.target``
entry carrying an ``arch`` array is merged in, and the merge only ever *adds*.
So a pinned ``arch`` array makes every macOS job build every listed arch, while
the CLI flag is silently ignored.

That is worse than wasted time. PyInstaller builds the backend for the *host*
arch of each job, and the per-job backend check in the workflow only inspects
``desktop/build/dist`` -- not what ends up inside each ``.app``. A job whose
config pinned both arches therefore produces one correct bundle and one bundle
whose shell arch and backend arch disagree: a shell that cannot spawn its own
backend. ``desktop/electron-builder.js`` states the rule in its header
("Never pin ``arch`` on mac.target in package.json"), and the workflow honours
it by giving each job its own runner and flag, so this file keeps the promise
honest from the outside.

Deliberately dumb on purpose: these are packaging-config assertions, not a
reimplementation of electron-builder.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON_PATH = REPO_ROOT / "desktop" / "package.json"
ELECTRON_BUILDER_CONFIG_PATH = REPO_ROOT / "desktop" / "electron-builder.js"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"

# The arch each CLI flag selects, as electron-builder spells them.
ARCH_FLAGS = {"arm64": "--arm64", "x64": "--x64"}

# macOS targets the desktop change registered for phase 1.
REGISTERED_MAC_ARCHES = {"arm64", "x64"}


def _load_mac_config() -> dict:
    assert PACKAGE_JSON_PATH.is_file(), f"missing {PACKAGE_JSON_PATH}"
    package = json.loads(PACKAGE_JSON_PATH.read_text(encoding="utf-8"))
    return package["build"]["mac"]


def _load_mac_jobs() -> list:
    """Return the release workflow's matrix entries, one per build job."""
    yaml = pytest.importorskip("yaml")
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = []
    for job in (workflow.get("jobs") or {}).values():
        matrix = ((job or {}).get("strategy") or {}).get("matrix") or {}
        for entry in matrix.get("include") or []:
            jobs.append(entry)
    assert jobs, f"no matrix jobs found in {WORKFLOW_PATH}"
    return jobs


def _assert_no_arch_pin(targets, source: str) -> None:
    entries = targets if isinstance(targets, list) else [targets]
    for entry in entries:
        if isinstance(entry, dict):
            assert "arch" not in entry, (
                f"{source} pins arch={entry.get('arch')!r} for "
                f"{entry.get('target')!r}; electron-builder merges that in on top "
                "of --arm64/--x64, so every macOS job would build every arch and "
                "ship a foreign-arch backend inside one of them"
            )
        else:
            # The "dmg:arm64" shorthand pins an arch just as explicitly.
            assert ":" not in str(entry), (
                f"{source} entry {entry!r} pins an arch suffix; leave the arch to "
                "the release workflow's per-job CLI flag"
            )


def test_mac_target_entries_do_not_pin_an_architecture():
    """A pinned ``arch`` in the config overrides the per-job CLI flag."""
    _assert_no_arch_pin(_load_mac_config()["target"], "desktop/package.json build.mac.target")


def test_the_resolved_electron_builder_config_pins_no_architecture():
    """``electron-builder.js`` may add binaries/notarize, never an arch pin."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to resolve desktop/electron-builder.js")
    script = (
        "const config = require('./desktop/electron-builder.js');"
        "process.stdout.write('__TARGETS__' + JSON.stringify(config.mac.target));"
    )
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # The config logs its injected binaries while it loads, so look for the marker.
    assert "__TARGETS__" in result.stdout, result.stdout + result.stderr
    targets = json.loads(result.stdout.rsplit("__TARGETS__", 1)[1])
    _assert_no_arch_pin(targets, "desktop/electron-builder.js (resolved mac.target)")


def test_every_mac_job_asks_for_exactly_one_architecture():
    mac_jobs = [job for job in _load_mac_jobs() if job.get("platform") == "mac"]
    assert mac_jobs, "the release workflow no longer builds macOS artifacts"
    for job in mac_jobs:
        flags = job.get("eb_flags", "")
        wanted = job.get("arch")
        assert wanted in ARCH_FLAGS, (
            f"macOS job {job.get('name')!r} declares arch={wanted!r}; expected one "
            f"of {sorted(ARCH_FLAGS)}"
        )
        present = [flag for flag in ARCH_FLAGS.values() if flag in flags]
        assert present == [ARCH_FLAGS[wanted]], (
            f"macOS job {job.get('name')!r} passes {flags!r}; it must select exactly "
            f"{ARCH_FLAGS[wanted]} so one job builds one architecture"
        )


def test_the_registered_mac_arches_are_all_built():
    built = {job.get("arch") for job in _load_mac_jobs() if job.get("platform") == "mac"}
    assert built == REGISTERED_MAC_ARCHES, (
        f"registered macOS arches {sorted(REGISTERED_MAC_ARCHES)} are no longer all "
        f"covered by the release workflow: {sorted(built)}"
    )
