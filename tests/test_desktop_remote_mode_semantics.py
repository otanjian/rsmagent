# encoding:utf-8
"""A chosen file is a reference; a chosen project is an authorization.

Change ``harden-windows-desktop-reliability``, task 11.1:

    ``readonly-input`` 与项目模式语义复核：确认"选择本机目录"得到的是文件**引用**，
    若要让对话读取本机项目，须以项目模式打开；在 ``agent/desktop_remote/mode.py``
    的行为不变的前提下，用测试固化该区分。

``mode.py`` already refuses to delegate a read-only reference -- the comment there
says why: "``readonly-input`` is a file *reference*, not permission to execute:
delegating it would hand the device a write the user never granted". Nothing
pinned that down, so an edit that dropped the guard would silently turn every
file pick into local project execution.

These tests build *real* ``ExecutionTarget`` values and drive the real decision
function, with the deployment switch stubbed so the platform is not part of what
is being measured. ``mode.py`` itself is not modified.
"""

import types

import pytest

from agent.desktop_remote import mode
from agent.workspace.execution_target import (
    LOCATION_BACKEND,
    MODE_PROJECT_EXECUTION,
    MODE_READONLY_INPUT,
    ExecutionTarget,
)

_IDS = dict(device_id="dev_1", workspace_id="ws_1", binding_id="bnd_1")


def readonly_reference() -> ExecutionTarget:
    return ExecutionTarget(location="desktop", project_mode=MODE_READONLY_INPUT, **_IDS)


def opened_project() -> ExecutionTarget:
    return ExecutionTarget(location="desktop", project_mode=MODE_PROJECT_EXECUTION, **_IDS)


def identity(target, cwd=None):
    return types.SimpleNamespace(execution_target=target, execution_cwd=cwd)


def switch(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(mode, "delegation_enabled", lambda: value)


def test_the_two_modes_are_distinct_predicates():
    assert readonly_reference().is_readonly_input is True
    assert readonly_reference().allows_project_execution is False
    assert opened_project().allows_project_execution is True
    assert opened_project().is_readonly_input is False
    backend = ExecutionTarget(location=LOCATION_BACKEND)
    assert backend.is_readonly_input is False and backend.allows_project_execution is False


def test_a_read_only_reference_never_becomes_local_execution(monkeypatch):
    """The heart of task 11.1: with the deployment *open*, a file pick stays a pick."""
    switch(monkeypatch, True)
    assert mode.remote_mode_for(identity(readonly_reference())) is False


def test_project_mode_is_what_earns_delegation(monkeypatch):
    switch(monkeypatch, True)
    assert mode.remote_mode_for(identity(opened_project())) is True


def test_the_deployment_switch_still_gates_project_mode(monkeypatch):
    switch(monkeypatch, False)
    assert mode.remote_mode_for(identity(opened_project())) is False


def test_a_resolved_local_path_is_not_delegated_again(monkeypatch):
    switch(monkeypatch, True)
    assert mode.remote_mode_for(identity(opened_project(), cwd="/tmp/project")) is False


def test_a_backend_target_never_delegates(monkeypatch):
    switch(monkeypatch, True)
    assert mode.remote_mode_for(identity(ExecutionTarget(location=LOCATION_BACKEND))) is False


def test_an_unreadable_switch_stays_closed(monkeypatch):
    """A deployment that cannot say yes has not said yes."""
    import integrations.desktop.execution_capability as capability

    def explode():
        raise RuntimeError("the switch cannot be read")

    monkeypatch.setattr(capability, "execution_switch_open", explode)
    assert mode.delegation_enabled() is False


def test_no_identity_means_no_desktop_run(monkeypatch):
    switch(monkeypatch, True)
    assert mode.remote_mode_for(types.SimpleNamespace()) is False


def test_the_guard_and_the_docstring_still_say_it():
    """Source-level, because the behaviour above depends on these two sentences."""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "agent", "desktop_remote", "mode.py"), encoding="utf-8") as handle:
        mode_source = handle.read()
    assert 'if not getattr(target, "allows_project_execution", False):' in mode_source, (
        "mode.py must keep the guard that makes a read-only reference non-executable")
    assert "readonly-input" in mode_source and "file *reference*" in mode_source, (
        "and the reason has to stay next to it")
    with open(os.path.join(root, "agent", "workspace", "execution_target.py"),
              encoding="utf-8") as handle:
        target_source = handle.read()
    assert "A read-only reference is not an execution grant." in target_source, (
        "the invariant belongs to the target model itself")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
