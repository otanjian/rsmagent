# encoding:utf-8
"""The client's v2 contract constants are generated, and are current.

Change ``align-desktop-project-execution-with-master``, task 6.1.

``contracts/desktop/v2.json`` is the source; ``scripts/gen_desktop_execution_types.py``
renders ``desktop/src/main/project-execution/generated-contract.ts``. This file
re-runs the generator and compares bytes, so editing the contract without
regenerating -- or hand-editing the generated file -- fails here rather than
shipping a client whose phase table quietly disagrees with the server's.

It also locks the generator itself: every limit it writes must come from one of
the two documents (never a third literal), and an inherited v1 bound must be
copied rather than restated.
"""

import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import gen_desktop_execution_types as gen  # noqa: E402


class GeneratedTypesTests(unittest.TestCase):
    def test_the_committed_file_is_exactly_what_the_generator_renders(self):
        with open(gen.OUTPUT, "r", encoding="utf-8") as handle:
            committed = handle.read()
        self.assertEqual(committed, gen.build(),
                         "generated-contract.ts is stale: "
                         ".venv/bin/python scripts/gen_desktop_execution_types.py")

    def test_the_generator_check_mode_agrees(self):
        self.assertEqual(gen.main(["--check"]), 0)

    def test_the_file_says_it_is_generated_and_how_to_regenerate(self):
        with open(gen.OUTPUT, "r", encoding="utf-8") as handle:
            head = handle.read(600)
        self.assertIn("GENERATED FILE -- do not edit", head)
        self.assertIn("scripts/gen_desktop_execution_types.py", head)
        self.assertIn("contracts/desktop/v2.json", head)
        self.assertIn("tests/test_desktop_execution_types.py", head)

    def test_every_line_is_lf_and_the_file_ends_with_a_newline(self):
        with open(gen.OUTPUT, "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.decode("utf-8").encode("utf-8"), raw)

    def test_the_phase_names_in_the_file_are_the_documents_phase_names(self):
        import json

        with open(gen.CONTRACT, "r", encoding="utf-8") as handle:
            contract = json.load(handle)
        with open(gen.OUTPUT, "r", encoding="utf-8") as handle:
            text = handle.read()
        for phase in contract["phases"]["names"]:
            self.assertIn("'%s'" % phase, text, phase)
        for state in contract["phases"]["from_state"]:
            self.assertIn("%s: " % state, text, state)

    def test_no_limit_is_written_from_a_third_place(self):
        """Every generated limit equals a value in v1.json or v2.json."""
        import json

        limits = _generated_limits()
        v1 = gen._v1_limits()
        with open(gen.CONTRACT, "r", encoding="utf-8") as handle:
            v2 = json.load(handle)["limits"]
        for name, value in limits.items():
            source = v2.get(name, v1.get(name))
            self.assertIsNotNone(source, name)
            expected = source
            if isinstance(expected, bool):
                expected = "true" if expected else "false"
            else:
                expected = str(expected)
            self.assertEqual(value, expected, name)

    def test_an_inherited_bound_is_reused_rather_than_restated(self):
        """v2.json lists inherited keys by name and leaves the value in v1.json."""
        import json

        with open(gen.CONTRACT, "r", encoding="utf-8") as handle:
            contract = json.load(handle)
        v1 = gen._v1_limits()
        self.assertTrue(contract["limits"]["reuse_v1"])
        for name in contract["limits"]["reuse_v1"]:
            self.assertIn(name, v1, name)
            self.assertNotIn(name, contract["limits"], name)
            self.assertIn(name, _generated_limits(), name)


def _generated_limits():
    """Read the generated ``EXECUTION_LIMITS`` back out of the TypeScript."""
    with open(gen.OUTPUT, "r", encoding="utf-8") as handle:
        text = handle.read()
    block = re.search(r"export const EXECUTION_LIMITS = \{(.*?)\} as const",
                      text, re.S)
    if not block:
        raise AssertionError("generated file has no EXECUTION_LIMITS block")
    limits = {}
    for line in block.group(1).strip().splitlines():
        match = re.match(r"\s*([a-z_0-9]+): (true|false|-?\d+),", line)
        if match:
            limits[match.group(1)] = match.group(2)
    return limits


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
