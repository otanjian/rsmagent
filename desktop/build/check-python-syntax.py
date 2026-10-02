#!/usr/bin/env python3
"""Refuse to ship Python the interpreter we bundle cannot read.

Change ``align-desktop-project-execution-with-master`` (task 10.2).

Why this is a build step and not a test: the artifact it protects is the frozen
backend, and the interpreter inside that artifact is not the one a developer runs
tests with. The desktop ships **Python 3.11** (``requirements-desktop.txt`` pins
3.11, ``build-backend.sh`` prefers 3.11, and the release workflows set
``python-version: 3.11``), while a developer's virtualenv is typically 3.12+.
Syntax that only the newer interpreter accepts therefore passes every local test
and then fails inside the shipped bundle.

That is not hypothetical -- it is how this script came to exist. Two files used
PEP 701 constructs (a backslash inside an f-string expression, and a repeated
quote character inside one):

* ``agent/tools/bash/bash.py`` -- imported by ``agent.tools``, which the server
  imports on startup, so on 3.11 the **entire application** failed to import;
* a ``Scene/`` skill strategy -- shipped as data and executed by the bundled
  interpreter, so the skill was dead on arrival.

PyInstaller did not fail on either. It recorded ``invalid module named ...`` in
its warnings file and built a bundle that starts and then cannot run a tool, so
the failure surfaced as "the desktop cannot run any local command" rather than as
a build error. Byte-compiling with the interpreter that will run the code is the
check that catches it where it is cheap.

The scan covers everything that travels in the bundle, including files that are
data rather than imports (``Scene/`` skills), because those are executed too.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

#: Directories that are not part of the shipped artifact: build outputs, the
#: project's own dev tooling, and dependency trees. `tests` is excluded because
#: tests are not executed by the app -- the spec excludes the repo's `tests`
#: package from the bundle outright, and a skill's `tests/` travels as data but is
#: never run. A file that only a test imports is not a reason to hold a release.
SKIP_DIRS = frozenset({
    '.git', '.github', '.venv', 'venv', 'node_modules', '__pycache__',
    'dist', 'build', 'build-work', '.venv-build', 'site-packages',
    'agent_stores', 'openspec', 'doc', 'docs', 'tests',
    # electron-builder output (`desktop/release/**`). Without this the scan walks
    # the *packaged copy* of the tree inside the `.app`, whose `_internal/` holds a
    # second copy of `agent/`, `channel/`, `Scene/`, ... -- it would double-count
    # files and report on an artifact instead of on the source. `.app` bundles are
    # filtered by suffix in `shipped_files` for the same reason.
    'release',
})

#: The interpreter the standard (non-legacy) desktop bundle is built and run
#: with. Stated here so a scan on a developer's newer interpreter can say what it
#: did *not* prove. The Win7 pipelines build on 3.8 and pass `--floor 3.8`.
DEFAULT_FLOOR = (3, 11)


def shipped_files(root: pathlib.Path) -> list[pathlib.Path]:
    files = []
    for path in sorted(root.rglob('*.py')):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        # A packaging output that is not under a skipped name: an `.app` bundle
        # carries its own copy of the whole tree.
        if any(part.endswith('.app') for part in path.parts):
            continue
        # A virtualenv or site-packages nested deeper than the skip list.
        if 'site-packages' in path.parts:
            continue
        files.append(path)
    return files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', default='.', help='repository root')
    parser.add_argument(
        '--floor', default=None, metavar='X.Y',
        help='minimum interpreter the shipped bundle is built with, e.g. 3.8 for the '
             'legacy Win7 pipeline (default: the interpreter running this script)')
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    running = sys.version_info[:2]
    floor = running
    if args.floor:
        try:
            floor = tuple(int(part) for part in args.floor.split('.')[:2])
        except ValueError:
            print(f'::error::--floor expects a version like 3.8, got {args.floor!r}')
            return 1
        if running < floor:
            # A newer interpreter cannot be made to parse the older grammar here
            # (`ast.parse(feature_version=...)` does not cover tokenizer-level
            # changes such as PEP 701), so the only honest answer is to refuse.
            print(f'::error::this interpreter is {running[0]}.{running[1]}, older than the '
                  f'{floor[0]}.{floor[1]} floor it is being asked to verify; '
                  f'run the check with the build interpreter')
            return 1

    print(f'==> Byte-compiling shipped Python with {sys.version.split()[0]} '
          f'(floor: {floor[0]}.{floor[1]})')
    if running > floor:
        print(f'    note: running on {running[0]}.{running[1]}, newer than the '
              f'{floor[0]}.{floor[1]} floor. `compile()` runs the *running* grammar, so this '
              f'passes syntax that only {running[0]}.{running[1]}+ accepts (PEP 701 f-strings '
              f'are the case that bit us); a green result here is not proof for '
              f'{floor[0]}.{floor[1]}. Run this check on the build interpreter for that.')

    failures = []
    files = shipped_files(root)
    for path in files:
        try:
            compile(path.read_bytes(), str(path), 'exec', 0, True)
        except SyntaxError as exc:
            failures.append(f'{path.relative_to(root)}:{exc.lineno}: {exc.msg}')
        except Exception as exc:  # a null byte, a bad encoding declaration, ...
            failures.append(f'{path.relative_to(root)}: {type(exc).__name__}: {exc}')

    if failures:
        print(f'::error::{len(failures)} shipped file(s) cannot be compiled by '
              f'Python {sys.version.split()[0]}:')
        for failure in failures:
            print(f'  {failure}')
        print('These files would be silently dropped or broken inside the frozen '
              'bundle. Fix the syntax (or the quoting) rather than excluding them.')
        return 1

    print(f'==> ok: {len(files)} shipped Python files compile cleanly')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
