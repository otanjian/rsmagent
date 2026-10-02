#!/usr/bin/env python3
# encoding:utf-8
"""Refuse to build a desktop bundle that cannot satisfy its skills' declarations.

Runs before PyInstaller in ``desktop/build/build-backend.sh``. The bundle's
library set is ``requirements-desktop.txt``; the skills declare what they need in
their frontmatter (``requires.python`` / ``install: pip``); this script is the
comparison. Without it the two drift silently, and the drift surfaces as an
import error at output time inside a sandboxed subprocess -- after the model has
reported success. That is not hypothetical: it is how ``xlsxwriter`` was missing
while the representative ``rfq-quote`` skill wrote every deliverable through it.

Three checks, because each catches a different half of the problem:

1. **Declared vs shipped** -- every declared library must be in the lock.
2. **Interpreter window** -- the build Python must be in the supported range;
   the bundle's contents genuinely depend on it (``legacy-cgi`` and a git-sourced
   ``web.py`` on 3.13+, no ``aiohttp<3.10`` wheels above 3.12).
3. **Actually importable** (``--verify-imports``) -- the mapping in step 1 says
   which distribution *should* provide a module; only an import in the real build
   venv says whether it does. This is the check with no assumptions left, so the
   build flow runs it after installing into the isolated venv.

Exit codes: ``0`` clean, ``1`` one or more problems, ``2`` the check itself could
not run (a missing lock or skills directory) -- deliberately distinct, because
"could not check" must never be mistaken for "checked and fine".
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

DEFAULT_LOCK = os.path.join(REPO, "desktop", "build", "requirements-desktop.txt")
DEFAULT_SKILL_DIRS = (
    os.path.join(REPO, "skills"),
    os.path.join(REPO, "workspace", "skills"),
)

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_CANNOT_CHECK = 2


@dataclass
class _Entry:
    """The shape ``agent.skills.dependencies`` needs, without the full loader.

    ``declarations`` reads ``entry.skill.name`` and ``entry.metadata`` and nothing
    else, so the guard can parse frontmatter with the project's own parser
    (``agent.skills.frontmatter``, which needs only ``re``/``json``) instead of
    importing the whole agent stack into a slimmed build venv.
    """

    skill: Any
    metadata: Any


@dataclass
class _SkillName:
    name: str


def collect_entries(skill_dirs: Sequence[str]) -> Tuple[List[_Entry], List[str]]:
    """``(entries, unreadable)``: parse every ``*/SKILL.md`` under ``skill_dirs``.

    An unparsable skill is returned in ``unreadable`` rather than skipped. A skill
    whose declarations cannot be read is exactly the case where a silent skip
    would claim a guarantee it does not have.
    """
    from agent.skills.frontmatter import parse_frontmatter, parse_metadata

    entries: List[_Entry] = []
    unreadable: List[str] = []
    for root in skill_dirs:
        if not root or not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name, "SKILL.md")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    content = handle.read()
                metadata = parse_metadata(parse_frontmatter(content))
            except Exception as e:  # noqa: BLE001 - reported, not swallowed
                unreadable.append(f"{path}: {e}")
                continue
            entries.append(_Entry(skill=_SkillName(name=name), metadata=metadata))
    return entries, unreadable


def verify_imports(declarations: Iterable[Any]) -> List[str]:
    """Modules that are declared and shipped-on-paper but not importable here.

    This is the only check without a mapping assumption in it: ``find_spec``
    either finds the module in *this* interpreter or it does not.
    """
    missing: List[str] = []
    for decl in declarations:
        if decl.kind not in ("python", "pip"):
            continue
        # The import name is the declared module for a `requires.python`; for a
        # `pip` spec the declared name is the distribution, whose import name we
        # do not know, so it is checked by the lock instead.
        if decl.kind != "python":
            continue
        root = str(decl.name).split(".")[0]
        try:
            found = importlib.util.find_spec(root)
        except (ImportError, ValueError, ModuleNotFoundError):
            found = None
        if found is None:
            missing.append(f"{decl.skill}: import {decl.name!r} not found")
    return missing


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", default=DEFAULT_LOCK)
    parser.add_argument("--skills", action="append", default=None,
                        help="skills directory (repeatable; defaults to skills/ and workspace/skills/)")
    parser.add_argument("--platform", default=sys.platform,
                        help=("the platform this bundle targets. PyInstaller produces a "
                              "bundle for the building host, so the default is this "
                              "interpreter's own platform."))
    parser.add_argument("--verify-imports", action="store_true",
                        help="also require each declared module to import in this interpreter")
    parser.add_argument("--skip-interpreter-check", action="store_true",
                        help=("do not require *this* interpreter to be a supported build "
                              "interpreter. For checking declarations on a dev machine; "
                              "the build flow must not use it."))
    args = parser.parse_args(argv)

    from agent.skills import dependencies

    skill_dirs = args.skills or list(DEFAULT_SKILL_DIRS)
    if not os.path.isfile(args.lock):
        print(f"!! cannot check: lock file not found at {args.lock}", file=sys.stderr)
        return EXIT_CANNOT_CHECK
    if not any(os.path.isdir(d) for d in skill_dirs):
        print(f"!! cannot check: no skills directory among {list(skill_dirs)}",
              file=sys.stderr)
        return EXIT_CANNOT_CHECK

    with open(args.lock, encoding="utf-8") as handle:
        lock_text = handle.read()

    entries, unreadable = collect_entries(skill_dirs)
    version = (sys.version_info.major, sys.version_info.minor)
    problems: List[str] = []

    for path in unreadable:
        problems.append(f"unreadable skill (declarations unknown): {path}")

    by_skill = dependencies.check_skills(
        entries, lock_text, platform=args.platform,
        python_version=None if args.skip_interpreter_check else version)
    for skill, found in sorted(by_skill.items()):
        for problem in found:
            problems.append(f"{problem.code} [{skill}] {problem.message}")

    if args.verify_imports:
        all_decls = [d for entry in entries
                     for d in dependencies.declarations(entry)]
        for line in verify_imports(all_decls):
            problems.append(f"dependency_missing {line}")

    print(f"checking {len(entries)} skill(s) against {os.path.relpath(args.lock, REPO)}")
    print(f"build interpreter: Python {version[0]}.{version[1]} "
          f"(supported >={dependencies.MIN_PYTHON[0]}.{dependencies.MIN_PYTHON[1]},"
          f"<{dependencies.MAX_PYTHON_EXCLUSIVE[0]}.{dependencies.MAX_PYTHON_EXCLUSIVE[1]})")

    if not problems:
        print("ok: every declared skill dependency is shipped by this bundle")
        return EXIT_OK

    print("", file=sys.stderr)
    print(f"!! {len(problems)} skill dependency problem(s):", file=sys.stderr)
    for line in problems:
        print(f"   - {line}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Fix by shipping the library (add it to the lock) or by correcting the "
          "skill's frontmatter declaration. Do not ship the bundle with a declared "
          "dependency missing: it fails at output time, inside a sandbox.", file=sys.stderr)
    return EXIT_PROBLEMS


if __name__ == "__main__":
    sys.exit(main())
