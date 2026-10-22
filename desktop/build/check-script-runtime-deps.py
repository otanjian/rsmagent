#!/usr/bin/env python3
"""Guard the script-runtime dependency list (task 7.1).

The failure this prevents has already happened here in another form:
``xlsxwriter`` was needed by a skill's delivery path, was absent from the packaged
requirements, and *nothing compared the two*. It surfaced at run time as a
``ModuleNotFoundError`` inside the sandbox -- after the model had reported success.

Four checks, each closing a different way that can recur:

1. **Subset.** A package here must also be in ``requirements-desktop.txt``, unless
   it is a declared transitive addition (``TRANSITIVE``). A second, independently
   chosen version list is only a second thing to drift.
2. **Scope.** Only what the first batch needs. A security property, not tidiness:
   task 7.1 chose a minimal bundled CPython over the 399.5 MB frozen backend so
   that "what a script can import" is a short list a person can read.
3. **First-batch skills satisfiable.** Every third-party module those skills import
   must be provided. Checked by reading the scripts, because that is what would
   have caught ``xlsxwriter``.
4. **Exemptions honest.** A package declared transitive that later appears in the
   desktop list must lose its exemption, or the exemption becomes a way to skip
   check 1.

Exit status is non-zero on any failure, so packaging fails before a release ships
rather than in front of a user.
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(HERE, 'requirements-script-runtime.txt')
DESKTOP = os.path.join(HERE, 'requirements-desktop.txt')

#: The first-batch scope. Adding one is a deliberate act.
ALLOWED = frozenset({
    'openpyxl', 'xlsxwriter', 'python-docx', 'python-pptx',
    'numpy', 'pandas', 'pillow', 'lxml', 'et-xmlfile',
})

#: In the script runtime but legitimately absent from the desktop list, because the
#: delivery mechanisms differ:
#:
#: * PyInstaller pulls transitive dependencies into the backend automatically, so
#:   ``lxml`` and ``et_xmlfile`` are in the 399.5 MB bundle without being named in
#:   its requirements;
#: * an embeddable CPython ships **only** what is listed, so a transitive
#:   dependency left implicit here is a ``ModuleNotFoundError`` in the sandbox.
#:
#: Naming them is the point rather than a loophole: each entry says what pulls it
#: in, so the claim can be checked.
#: Exempt from the subset rule because PyInstaller pulls them into the backend
#: automatically, so they are in the 399.5 MB bundle without being named in its
#: requirements. An embeddable CPython does not, so they must be named here.
#: A package in this map that *appears* in the desktop list has stopped being an
#: automatic inclusion and the exemption must go.
AUTO_INCLUDED = {
    'lxml': 'python-docx / python-pptx parser',
    'et-xmlfile': "openpyxl's XML writer",
}

#: Exempt from the subset rule because the package is required by a first-batch
#: skill and was missing from BOTH lists -- a real packaging gap the guard found by
#: reading the skills' imports. Listed here so the reason survives; the entry should
#: be deleted once the package also appears in requirements-desktop.txt, because
#: then the subset rule holds on its own.
GAP_FILLED: dict = {}

#: Every name exempt from check 1.
TRANSITIVE = {**AUTO_INCLUDED, **GAP_FILLED}

#: Third-party libraries a *first-batch* skill may depend on, i.e. Excel and
#: document handling. A skill needing anything outside this set is not in the first
#: batch by design (design D7: the first batch is the Excel/document skills), so it
#: is excluded from the satisfiability scan rather than allowed to widen the
#: runtime. Data, not a hard-coded skill list, so a new first-batch skill is picked
#: up automatically.
#: Distribution name -> the module a script imports.
IMPORT_NAMES = {
    'openpyxl': 'openpyxl',
    'xlsxwriter': 'xlsxwriter',
    'python-docx': 'docx',
    'python-pptx': 'pptx',
    'numpy': 'numpy',
    'pandas': 'pandas',
    'pillow': 'PIL',
    'lxml': 'lxml',
    'et-xmlfile': 'et_xmlfile',
}

FIRST_BATCH_LIBRARIES = frozenset({
    'openpyxl', 'xlsxwriter', 'docx', 'pptx', 'numpy', 'pandas', 'PIL', 'lxml',
    'et_xmlfile',
})

STDLIB = frozenset("""
abc argparse array ast asyncio base64 bisect builtins calendar cmath codecs
collections concurrent configparser contextlib copy copyreg csv ctypes
dataclasses datetime dbm decimal difflib doctest email enum errno filecmp
fileinput fnmatch fractions functools getpass glob graphlib gzip hashlib heapq
html http importlib inspect io itertools json keyword linecache locale logging
lzma math multiprocessing numbers operator os pathlib pickle pkgutil platform
pprint queue random re reprlib runpy secrets shelve shlex shutil signal site
smtplib socket sqlite3 ssl stat statistics string struct subprocess sys tarfile
tempfile textwrap threading time token tokenize tomllib traceback tracemalloc
types typing unicodedata unittest urllib uuid warnings weakref xml zipfile
zipimport zlib zoneinfo __future__
""".split())


def normalise(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name.strip().lower())


def parse(path: str) -> dict:
    """Read a requirements file into ``{normalised name: requirement line}``."""
    requirements = {}
    with open(path, encoding='utf-8') as handle:
        for raw in handle:
            line = raw.split('#', 1)[0].strip()
            if not line or line.startswith('-'):
                continue
            name = re.split(r'[<>=!;\[]', line, 1)[0].strip()
            if name:
                requirements[normalise(name)] = line
    return requirements


def _imports_of(path: str) -> set:
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            text = handle.read()
    except OSError:
        return set()
    return {
        match.group(1)
        for match in re.finditer(
            r'^\s*(?:import|from)\s+([A-Za-z_][\w]*)', text, re.M)
    }


def _skill_scripts(repo: str) -> dict:
    """``{skill name: (scripts dir, sibling module names)}``.

    Sibling names matter: a skill's scripts import each other by bare module name
    (``data_loader``, ``report_generator``), and those are files beside them, not
    packages the runtime must ship. Counting them as missing dependencies would
    demand the runtime carry a skill's own internals.
    """
    skills = {}
    skip_dirs = {'.git', 'node_modules', '.venv-win', '.venv-build', '__pycache__',
                 'target', 'dist', 'release', 'build'}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if os.path.basename(root) != 'scripts':
            continue
        posix = root.replace('\\', '/')
        if '/skills/' not in posix:
            continue
        skill = os.path.basename(os.path.dirname(root))
        siblings = {name[:-3] for name in files if name.endswith('.py')}
        skills[skill] = (root, siblings)
    return skills


def collect_first_batch_imports(repo: str) -> tuple:
    """``(modules needed, skills excluded)``.

    A skill is in the first batch when everything it imports is the standard
    library, one of its own sibling modules, or an Excel/document library. Skills
    needing a PDF parser, a charting library or a network client are excluded by
    that rule rather than by a hand-maintained list, which would rot.
    """
    needed = set()
    excluded = {}
    for skill, (root, siblings) in sorted(_skill_scripts(repo).items()):
        imports = set()
        for name in os.listdir(root):
            if name.endswith('.py'):
                imports |= _imports_of(os.path.join(root, name))
        external = {
            module for module in imports
            if module not in STDLIB
            and module not in siblings
            and not module.startswith('_')
        }
        outside = {m for m in external if m not in FIRST_BATCH_LIBRARIES}
        if outside:
            excluded[skill] = sorted(outside)
            continue
        needed |= external
    return needed, excluded


def main() -> int:
    for path in (RUNTIME, DESKTOP):
        if not os.path.exists(path):
            print(f'::error::missing {path}')
            return 1

    runtime = parse(RUNTIME)
    desktop = parse(DESKTOP)
    failures = []

    # 1. Subset, with declared transitive additions allowed.
    for name in sorted(runtime):
        if name not in desktop and name not in TRANSITIVE:
            failures.append(
                f'{name} is in the script runtime but neither in '
                f'requirements-desktop.txt nor declared transitive; an undeclared '
                f'dependency is how a minimal runtime stops being one')

    # 2. Scope.
    for name in sorted(runtime):
        if name not in ALLOWED:
            failures.append(
                f'{name} is outside the first-batch scope '
                f'({", ".join(sorted(ALLOWED))}); widening what a script can import '
                f'is a deliberate change, not a side effect')

    # 3. Exemptions stay honest.
    for name in sorted(AUTO_INCLUDED):
        if name in desktop:
            failures.append(
                f'{name} is exempted as auto-included but now appears in '
                f'requirements-desktop.txt; drop the exemption')
    for name in sorted(GAP_FILLED):
        if name in desktop:
            # Not a failure: the gap was closed properly, so the exemption has
            # served its purpose. Reported so the entry is removed rather than
            # lingering as a silent permission.
            print(f'note: {name} is now in requirements-desktop.txt; its '
                  f'GAP_FILLED entry can be deleted')

    # 4. The first-batch skills' imports must all be satisfiable.
    repo = os.path.dirname(os.path.dirname(HERE))
    needed, excluded = collect_first_batch_imports(repo)
    provided = {IMPORT_NAMES.get(name) for name in runtime}
    for module in sorted(needed):
        if module not in provided:
            failures.append(
                f'a first-batch skill imports {module!r}, which the script runtime '
                f'does not provide; it would fail inside the sandbox after the '
                f'model was told the work succeeded')

    if failures:
        for problem in failures:
            print(f'::error::{problem}')
        return 1

    print(f'script runtime: {len(runtime)} package(s)')
    for name in sorted(runtime):
        note = f'   (transitive: {TRANSITIVE[name]})' if name in TRANSITIVE else ''
        print(f'  {runtime[name]}{note}')
    print(f'first-batch imports satisfied: {", ".join(sorted(needed)) or "(none)"}')
    print('skills outside the first batch (not required by this runtime): '
          f'{len(excluded)}')
    for skill, modules in sorted(excluded.items()):
        print(f'  {skill}: needs {", ".join(modules)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
