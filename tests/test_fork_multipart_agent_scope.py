"""Multipart routes must resolve the Agent from the query string, not the body.

Clients deliberately keep ``agent_id`` out of a multipart body: web.py merges
the query string and the form, and a field present in both arrives as a list
that breaks handlers expecting a string (the console's fetch wrapper and the
desktop client's ``postFormData`` both do this). A handler that reads the body
alone — ``_raw_web_input()`` is ``rawinput("post")`` — therefore sees no Agent
and quietly answers as the **default** one: a mic recording is written into the
default Agent's workspace and an imported document builds the default Agent's
knowledge service.

Upstream fixed this in its own handlers and gave the pattern one home
(``channel/web/core/_common.py::_scoped_agent_id``). The fork serves its own
handlers, so the pattern has to exist on this side too, and every body-reading
fork route has to inherit it — which is what these two guards check:

* behaviour: the resolver prefers an explicit id and otherwise reads the query;
* structure: no fork route that reads a raw body resolves the Agent with the
  body-only helper.

The second is the one that survives future merges: a new multipart route that
calls ``_request_agent_id(params)`` on ``_raw_web_input()`` fails here, at the
call site, instead of silently writing into the default Agent months later.
"""

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from _helpers import fork_web_layer_files


ROOT = Path(__file__).resolve().parents[1]


class _FakeCtx:
    """The only part of ``web.ctx`` the resolver reads: ``env``.

    ``web.ctx`` is a thread-local proxy that another suite can leave holding a
    plain ``dict``; patching the module attribute instead of mutating it keeps
    this test independent of that ambient state.
    """

    def __init__(self, query: str = ""):
        self.env = {"QUERY_STRING": query}


class ScopedAgentIdTests(unittest.TestCase):
    """The resolver's contract."""

    def _resolve(self, params, query=""):
        from channel.web.fork import runtime

        with patch.object(runtime.web, "ctx", _FakeCtx(query)):
            return runtime._scoped_agent_id(params)

    def test_reads_the_query_string_when_the_body_has_no_agent(self):
        self.assertEqual(
            self._resolve({}, "agent_id=helper&other=1"), "helper")

    def test_an_explicit_body_id_wins_over_the_query(self):
        self.assertEqual(
            self._resolve({"agent_id": "from-body"}, "agent_id=from-query"),
            "from-body")

    def test_a_field_present_twice_collapses_to_one_id(self):
        """web.py merges query and form, so this arrives as a list."""
        self.assertEqual(self._resolve({"agent_id": ["a", "b"]}), "a")

    def test_no_agent_anywhere_is_none_not_a_string(self):
        self.assertIsNone(self._resolve({}))


class BodyReadingRoutesUseTheScopedResolver(unittest.TestCase):
    """Every fork route that reads a raw body scopes the Agent across query+body."""

    def _functions_reading_a_raw_body(self):
        """``(path, qualified name, called names)`` for each such function."""
        for path in fork_web_layer_files():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                names = set()
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        func = inner.func
                        name = getattr(func, "id", None) or getattr(func, "attr", None)
                        if name:
                            names.add(name)
                if "_raw_web_input" in names:
                    yield path, node.name, names

    def test_no_body_reading_route_uses_the_body_only_resolver(self):
        offenders = []
        for path, name, names in self._functions_reading_a_raw_body():
            if "_request_agent_id" in names and "_scoped_agent_id" not in names:
                offenders.append(f"{path.relative_to(ROOT)}::{name}")
        self.assertEqual(
            offenders, [],
            "these routes read a raw (body-only) input and resolve the Agent "
            "with _request_agent_id, so a request whose agent_id rides the query "
            "string is answered as the default Agent; resolve with "
            "_scoped_agent_id instead: " + ", ".join(offenders),
        )

    def test_the_multipart_routes_are_actually_covered(self):
        """The guard above is only meaningful if it sees the known routes.

        Without this, a refactor that stopped calling ``_raw_web_input()`` (or a
        change to ``fork_web_layer_files``) would turn the assertion above into a
        vacuous pass.
        """
        seen = {name for _, name, _ in self._functions_reading_a_raw_body()}
        for expected in ("POST",):
            self.assertIn(expected, seen)
        # ``/upload`` is deliberately no longer one of these: it follows the
        # master branch's handler interaction and never reads the body, leaving
        # the single parse to ``WebChannel.upload_file`` (``wsgi.input`` is a
        # one-shot stream), so its Agent comes from the query string.
        # ``/api/knowledge/import`` left this family when it moved to
        # ``_multipart_lists`` — that reads the body too, but not through
        # ``_raw_web_input``, so the detector below cannot see it. The remaining
        # expectation is found by class-qualified name rather than by method name
        # alone.
        qualified = set()
        for path, name, names in self._functions_reading_a_raw_body():
            source = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == name:
                            qualified.add(f"{node.name}.{item.name}")
        for expected in ("VoiceAsrHandler.POST",):
            self.assertIn(
                expected, qualified,
                "expected a body-reading multipart route to be covered by the "
                "scoped-Agent guard",
            )


if __name__ == "__main__":
    unittest.main()
