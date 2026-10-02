"""The channel signature seam (tasks 8.2/8.3).

Upstream's channel methods take no tenant/agent argument. The fork originally
added keyword parameters to ``upload_file``/``post_message``/``cancel_request``/
``poll_response``, which meant every upstream edit to those methods had to be
merged by hand. The seam replaces the parameters with a request-scoped
authorized target:

* signatures stay exactly as upstream wrote them (asserted here, so a future
  edit that re-adds a fork kwarg fails this file instead of a merge), and
* the authorized target still governs: a published ``(agent_id, session_id)``
  is what the channel uses, and nothing is published in legacy mode, where the
  upstream router fallback runs unchanged.
"""
import json
import os
import sys
import threading
import unittest
from inspect import signature
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.runtime import authorized_target, authorized_target_scope  # noqa: E402


def _channel_class():
    from channel.web import web_channel
    return web_channel.WebChannel.__closure__[0].cell_contents


class SignatureTests(unittest.TestCase):
    """8.2: the upstream signature is not rewritten for fork needs."""

    def test_upstream_channel_methods_take_no_fork_parameters(self):
        raw = _channel_class()
        for name in ("upload_file", "post_message", "cancel_request", "poll_response"):
            params = list(signature(getattr(raw, name)).parameters)
            self.assertEqual(params, ["self"], f"{name} must keep upstream's signature")

    def test_no_database_only_keyword_survives_on_the_channel(self):
        from tests._helpers import web_layer_source
        source = web_layer_source()
        for gone in ("def upload_file(self, *", "def post_message(self, *",
                     "def cancel_request(self, *", "def poll_response(self, *"):
            self.assertNotIn(gone, source)


class TargetScopeTests(unittest.TestCase):
    """The seam publishes a request-scoped target and restores it on exit."""

    def test_a_published_target_is_readable_inside_the_scope_only(self):
        self.assertEqual(authorized_target(), {})
        with authorized_target_scope(session=("agent-a", "sess-1")):
            self.assertEqual(authorized_target()["session"], ("agent-a", "sess-1"))
        self.assertEqual(authorized_target(), {})

    def test_a_published_target_is_not_shared_across_threads(self):
        """A pooled worker thread must not inherit the previous request's target."""
        seen = {}

        def worker():
            seen["target"] = authorized_target()

        with authorized_target_scope(session=("agent-a", "sess-1")):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
        self.assertEqual(seen["target"], {})

    def test_a_reader_cannot_mutate_the_published_target(self):
        with authorized_target_scope(agent_id="agent-a"):
            reader = authorized_target()
            reader["agent_id"] = "tampered"
            self.assertEqual(authorized_target()["agent_id"], "agent-a")

    def test_the_scope_is_reentrant(self):
        with authorized_target_scope(agent_id="outer"):
            with authorized_target_scope(agent_id="inner"):
                self.assertEqual(authorized_target()["agent_id"], "inner")
            self.assertEqual(authorized_target()["agent_id"], "outer")


class ConsumptionTests(unittest.TestCase):
    """8.3: the channel consumes the published target instead of a parameter."""

    def _bridge(self):
        bridge = SimpleNamespace(
            steer_session=mock.Mock(return_value=SimpleNamespace(
                status=SimpleNamespace(value="accepted"), message="")),
            route_context=mock.Mock(return_value="default"),
            agent_router=SimpleNamespace(resolve=lambda **kwargs: "router-agent"),
            scoped_session_key=lambda session_id, agent_id=None: session_id,
        )
        return bridge, SimpleNamespace(get_agent_bridge=lambda: bridge)

    def test_post_message_uses_the_published_authorized_session(self):
        from channel.web import web_channel
        bridge, factory = self._bridge()
        with mock.patch("bridge.bridge.Bridge", lambda: factory), \
                mock.patch.object(web_channel.web, "data", lambda: json.dumps({
                    "session_id": "body-session",
                    "message": "/steer change course",
                    "steer": True,
                    "lang": "en",
                }).encode()):
            instance = object.__new__(_channel_class())
            with authorized_target_scope(session=("target-agent", "target-session")):
                web_channel.WebChannel.__closure__[0].cell_contents.post_message(instance)
        # The *authorized* pair won, not the body's session and not the router.
        self.assertEqual(bridge.steer_session.call_args.args[0], "target-session")
        self.assertEqual(bridge.steer_session.call_args.args[2], "target-agent")

    def test_post_message_falls_back_to_upstream_routing_without_a_target(self):
        from channel.web import web_channel
        bridge, factory = self._bridge()
        with mock.patch("bridge.bridge.Bridge", lambda: factory), \
                mock.patch.object(web_channel.web, "data", lambda: json.dumps({
                    "session_id": "body-session",
                    "message": "/steer change course",
                    "steer": True,
                    "lang": "en",
                }).encode()):
            instance = object.__new__(_channel_class())
            web_channel.WebChannel.__closure__[0].cell_contents.post_message(instance)
        # Upstream semantics: the router resolves the Agent (no fork target).
        self.assertEqual(bridge.steer_session.call_args.args[0], "body-session")
        self.assertEqual(bridge.steer_session.call_args.args[2], "router-agent")

    def test_upload_file_reads_the_authorized_agent_from_the_target(self):
        from channel.web import web_channel
        captured = {}

        def fake_upload_dir(agent_id):
            captured["agent_id"] = agent_id
            return "/tmp/nonexistent-uploads"

        with mock.patch.object(web_channel, "_get_upload_dir", fake_upload_dir), \
                mock.patch("channel.web.core._common._raw_web_input", lambda: {
                    "session_id": "", "file": None, "files": None,
                    "relative_path": "", "relative_paths": None,
                    "agent_id": "form-field-agent", "upload_id": "",
                }):
            had_env = hasattr(web_channel.web.ctx, "env")
            web_channel.web.ctx.env = {"CONTENT_LENGTH": "0",
                                       "CONTENT_TYPE": "text/plain"}
            try:
                instance = object.__new__(_channel_class())
                with authorized_target_scope(agent_id="authorized-agent"):
                    web_channel.WebChannel.__closure__[0].cell_contents.upload_file(instance)
            finally:
                if not had_env:
                    del web_channel.web.ctx.env
        self.assertEqual(captured.get("agent_id"), "authorized-agent")
        # The raw form field never steers the write (the target wins).
        self.assertNotEqual(captured.get("agent_id"), "form-field-agent")


if __name__ == "__main__":
    unittest.main()
