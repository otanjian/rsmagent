# encoding:utf-8
"""The console's explicit "make my own folder" write for a shared Agent.

A tenant-shared Agent keeps each member's platform files under
``user/<user id>`` inside the Agent's workspace, and that folder is created by a
write — so a member who has never filed anything has no folder to open. The
console's file panel anchors there and asks for it to be made, once, before it
lists.

What this file pins:

* the folder is the *caller's own* — the user id comes from the verified
  identity and is never taken from the request body;
* it is created under the workspace of the Agent the request names, not of
  whatever Agent the ambient identity happens to carry;
* an identity with no end user makes nothing (and says so), rather than falling
  back to a shared location;
* a ``user`` container a path could be redirected through is refused, not
  silently reinterpreted.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch

import web  # noqa: F401  (real package preferred so HTTPError is authentic)

from channel.web.fork.handlers.workspace import (
    WorkspaceUserDirHandler,
    _ensure_own_user_dir,
)
from common.runtime_identity import RuntimeIdentity

USER = "usr_AbC123"
AGENT = "shared-agent"


def _identity(**overrides):
    base = RuntimeIdentity(agent_id=AGENT, user_id=USER, tenant_id="acme")
    return base.derive(**overrides) if overrides else base


class EnsureOwnUserDirTests(unittest.TestCase):
    """The creation itself: this user, under the addressed Agent's workspace."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="cow-ws-userdir-")
        self.addCleanup(shutil.rmtree, self.ws, True)

    def _patch(self, identity):
        return patch("common.runtime_identity.current_identity",
                     return_value=identity)

    def test_it_creates_the_callers_own_folder_under_the_agent_workspace(self):
        with self._patch(_identity()), \
             patch("common.state_dir.state_root", return_value=Path(self.ws)):
            self.assertTrue(_ensure_own_user_dir(AGENT))
        self.assertTrue(os.path.isdir(os.path.join(self.ws, "user", USER)))

    def test_it_is_idempotent(self):
        with self._patch(_identity()), \
             patch("common.state_dir.state_root", return_value=Path(self.ws)):
            self.assertTrue(_ensure_own_user_dir(AGENT))
            self.assertTrue(_ensure_own_user_dir(AGENT))
        self.assertTrue(os.path.isdir(os.path.join(self.ws, "user", USER)))

    def test_no_end_user_makes_nothing(self):
        identity = _identity(user_id="")
        with self._patch(identity), \
             patch("common.state_dir.state_root", return_value=Path(self.ws)):
            self.assertFalse(_ensure_own_user_dir(AGENT))
        # Not even the container: an anonymous request must not create state.
        self.assertFalse(os.path.exists(os.path.join(self.ws, "user")))

    def test_it_addresses_the_named_agent_not_the_ambient_one(self):
        seen = {}

        def _state_root(identity=None):
            seen["agent_id"] = identity.agent_id
            return Path(self.ws)

        with self._patch(_identity(agent_id="some-other-agent")), \
             patch("common.state_dir.state_root", side_effect=_state_root):
            self.assertTrue(_ensure_own_user_dir(AGENT))

        self.assertEqual(seen["agent_id"], AGENT,
                         "the folder was made for the ambient Agent")

    def test_a_user_container_that_is_a_file_is_refused(self):
        import web as web_module

        with open(os.path.join(self.ws, "user"), "w", encoding="utf-8") as handle:
            handle.write("not a directory")

        with self._patch(_identity()), \
             patch("common.state_dir.state_root", return_value=Path(self.ws)), \
             patch.object(web_module.ctx, "headers", [], create=True):
            with self.assertRaises(web.HTTPError) as caught:
                _ensure_own_user_dir(AGENT)

        self.assertEqual(str(caught.exception), "403 Forbidden")


class WorkspaceUserDirHandlerTests(unittest.TestCase):
    """The route the console calls: it names an Agent, never a user."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="cow-ws-userdir-h-")
        self.addCleanup(shutil.rmtree, self.ws, True)

    def _stack(self, identity):
        """The (legacy) handler body without a request, as the other workspace
        handler tests call it: the tenant gate is a no-op and the origin/CSRF
        gate is stubbed, so what is measured here is the handler's own work."""
        from channel.web import web_channel

        stack = ExitStack()
        stack.enter_context(patch(
            "channel.web.web_channel._db_scope",
            return_value=nullcontext(None)))
        stack.enter_context(patch("channel.web.auth_handlers.require_management_write"))
        stack.enter_context(patch.object(web_channel.web, "header"))
        stack.enter_context(patch.object(
            web_channel.web.ctx, "headers", [], create=True))
        stack.enter_context(patch.object(
            web_channel.web, "data", return_value=b"{}"))
        stack.enter_context(self._identity_patch(identity))
        stack.enter_context(patch(
            "common.state_dir.state_root", return_value=Path(self.ws)))
        return stack

    def _identity_patch(self, identity):
        return patch("common.runtime_identity.current_identity", return_value=identity)

    def _post(self, body, identity):
        from channel.web import web_channel

        with self._stack(identity), \
             patch.object(web_channel.web, "data",
                          return_value=json.dumps(body).encode("utf-8")):
            return json.loads(WorkspaceUserDirHandler().POST())

    def test_the_handler_creates_the_callers_folder(self):
        response = self._post({"agent": AGENT}, _identity())

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["agent"], AGENT)
        self.assertTrue(os.path.isdir(os.path.join(self.ws, "user", USER)))

    def test_the_handler_reports_when_there_is_no_end_user(self):
        response = self._post({"agent": AGENT}, _identity(user_id=""))

        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], "no_user")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "user")))

    def test_the_handler_requires_an_agent(self):
        response = self._post({}, _identity())

        self.assertEqual(response["status"], "error")
        self.assertIn("agent", response["message"])


if __name__ == "__main__":
    unittest.main()
