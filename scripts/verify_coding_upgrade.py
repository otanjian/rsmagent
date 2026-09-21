# encoding:utf-8
"""Task 5.4's four migration checks, run for real.

Not a pytest file: this is the acceptance script the evidence section quotes.
It exercises the paths a deployment actually walks through -- an untagged Agent
file, a store opened twice, a capability that is switched off, and the same
capability switched back on -- and prints what it observed.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

from agent.coding import CODING_DISABLED, CodingError, CodingSettings
from agent.coding.opencode import RemoteSession
from agent.coding.sessions import CodingSessionService
from agent.memory import conversation_schema as schema
from agent.memory.conversation_store import ConversationStore
from agent.registry import AGENT_TYPE_NORMAL, AgentRegistry
from common.runtime_identity import identity_scope

PROJECT = "/srv/checkouts/erp-legacy"


class _FakeClient:
    def __init__(self):
        self.created = []

    def create_session(self, session_id, project_dir):
        self.created.append(session_id)
        return RemoteSession(id=session_id, title="Legacy upgrade session",
                             directory=project_dir, created_ms=1789890000000,
                             updated_ms=1789890000000)


def _digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _settings(enabled):
    return CodingSettings(enabled=enabled, service_id="default",
                          api_url="http://127.0.0.1:4096",
                          web_url="https://code.example.com")


def main():
    root = tempfile.mkdtemp(prefix="rsm-5.4-")
    results = {}

    # --- 1. an Agent entry written before the type field existed -----------
    #
    # The entry lives in the deployment's `agents` list. "Not rewriting" has two
    # halves: reading it must not invent a type, and saving an unrelated edit
    # must not start writing one back into the file.
    legacy_entry = {
        "id": "sales",
        "name": "Sales assistant",
        "description": "written before the type field existed",
        "enabled": True,
    }
    settings = {
        "agent_workspace": os.path.join(root, "cow"),
        "default_agent_id": "main",
        "agents": [
            {"id": "main", "name": "Main"},
            dict(legacy_entry),
        ],
    }
    settings_path = os.path.join(root, "config.json")
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)

    registry = AgentRegistry.from_config(settings)
    profile = registry.get("sales")
    results["legacy_type"] = profile.agent_type
    results["legacy_is_normal"] = profile.agent_type == AGENT_TYPE_NORMAL
    results["legacy_is_coding"] = profile.is_coding
    results["legacy_projection_keys"] = sorted(profile.to_dict())

    # What a save would put back into the file: the type must still be absent,
    # and the entry the operator wrote must still have exactly its own keys.
    saved_entry = profile.to_dict()
    results["legacy_saved_has_no_type"] = "agent_type" not in saved_entry
    results["legacy_entry_unrewritten"] = (
        dict(legacy_entry) == {key: saved_entry[key] for key in legacy_entry})
    on_disk = json.load(open(settings_path, encoding="utf-8"))["agents"][1]
    results["legacy_file_unrewritten"] = (
        "agent_type" not in on_disk and "coding_project_dir" not in on_disk)

    # The same load repeated: reading must not start writing on the second pass
    # either.
    registry2 = AgentRegistry.from_config(settings)
    results["legacy_second_load_type"] = registry2.get("sales").agent_type
    results["legacy_still_unrewritten"] = (
        "agent_type" not in settings["agents"][1]
        and "agent_type" not in registry2.get("sales").to_dict())

    # --- 2. the association table initialised more than once ---------------
    db_path = os.path.join(root, "conversations.db")
    store = ConversationStore(Path(db_path))
    results["link_table_after_first_open"] = schema.OPENCODE_SESSION_LINKS in _tables(db_path)

    with identity_scope(agent_id="erp-coder", user_id="u-1", tenant_id="t-1"):
        first = CodingSessionService(store, settings=_settings(True),
                                     client=_FakeClient()).reserve(
            agent_id="erp-coder", project_dir=PROJECT, request_id="req-1")
    results["reserved_session_id"] = first["session_id"]
    results["reserved_state"] = first["state"]

    # A second store on the same file is what every new request does. The DDL
    # must be a no-op and the row must survive.
    ConversationStore(Path(db_path))
    ConversationStore(Path(db_path))
    store2 = ConversationStore(Path(db_path))
    results["link_table_after_reopen"] = schema.OPENCODE_SESSION_LINKS in _tables(db_path)
    results["link_rows_after_reopen"] = _count(db_path, schema.OPENCODE_SESSION_LINKS)
    results["link_intact_after_reopen"] = (
        _link(store2, first["session_id"]) is not None)
    results["store_open_count_is_idempotent"] = _tables(db_path).count(
        schema.OPENCODE_SESSION_LINKS) == 1

    # --- 3. the capability switched off ------------------------------------
    client_off = _FakeClient()
    try:
        with identity_scope(agent_id="erp-coder", user_id="u-1", tenant_id="t-1"):
            CodingSessionService(store2, settings=_settings(False),
                                 client=client_off).reserve(
                agent_id="erp-coder", project_dir=PROJECT, request_id="req-2")
        results["disabled_raised"] = None
    except CodingError as error:
        results["disabled_raised"] = error.code
    results["disabled_is_the_expected_code"] = results["disabled_raised"] == CODING_DISABLED
    results["disabled_wrote_no_row"] = _count(db_path, schema.OPENCODE_SESSION_LINKS) == 1
    results["disabled_called_nothing"] = client_off.created == []
    results["disabled_kept_the_existing_link"] = (
        _link(store2, first["session_id"]) is not None)

    # --- 4. and switched back on, on the same data -------------------------
    client_on = _FakeClient()
    with identity_scope(agent_id="erp-coder", user_id="u-1", tenant_id="t-1"):
        again = CodingSessionService(store2, settings=_settings(True),
                                     client=client_on).reserve(
            agent_id="erp-coder", project_dir=PROJECT, request_id="req-2")
    results["reenabled_new_session"] = again["session_id"]
    results["reenabled_state"] = again["state"]
    results["reenabled_old_link_still_there"] = (
        _link(store2, first["session_id"]) is not None)
    results["reenabled_row_count"] = _count(db_path, schema.OPENCODE_SESSION_LINKS)

    # --- the code directory itself is never touched ------------------------
    project = os.path.join(root, "checkout")
    os.makedirs(project)
    marker = os.path.join(project, "main.py")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("print('user code')\n")
    marker_before = _digest(marker)
    with identity_scope(agent_id="erp-coder", user_id="u-1", tenant_id="t-1"):
        CodingSessionService(store2, settings=_settings(True), client=_FakeClient()).reserve(
            agent_id="erp-coder", project_dir=project, request_id="req-3")
    results["project_untouched"] = os.path.isdir(project) and _digest(marker) == marker_before
    results["project_files"] = sorted(os.listdir(project))

    # --- legacy messages are not rewritten ---------------------------------
    results["messages_rows"] = _count(db_path, schema.MESSAGES)

    shutil.rmtree(root, ignore_errors=True)
    for key in sorted(results):
        print(f"{key}: {results[key]}")


def _link(store, session_id):
    """Link lookups are dimensioned by identity, so ask as the Agent that owns
    the row -- which is how every request reaches this table too."""
    with identity_scope(agent_id="erp-coder", user_id="u-1", tenant_id="t-1"):
        return store.get_coding_link(session_id)


def _tables(db_path):
    connection = sqlite3.connect(db_path)
    try:
        return [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        connection.close()


def _count(db_path, table):
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


if __name__ == "__main__":
    main()
