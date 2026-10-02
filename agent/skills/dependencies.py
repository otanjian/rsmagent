# encoding:utf-8
"""What a skill declares it needs, against what the bundle actually ships (8.5).

Two declarative mechanisms already existed and neither was enforced:

``metadata.requires``
    ``bins`` / ``anyBins`` / ``env`` / ``anyEnv``, plus any other kind the
    frontmatter names. Consumed by *enablement* (``should_include_skill``), which
    is the right behaviour for an optional capability: a skill whose API key is
    unset is hidden from the model. It is the wrong behaviour for a library the
    skill's script imports unconditionally -- hiding the skill does not help when
    the user explicitly asked for it.

``metadata.install``
    :class:`~agent.skills.types.SkillInstallSpec` (``brew`` / ``pip`` / ``npm`` /
    ``download``). Parsed from the frontmatter and then **consumed by nothing**,
    so a skill could declare exactly how to obtain what it needs and no code
    acted on it.

Meanwhile the desktop bundle's library set is hand-maintained in
``desktop/build/requirements-desktop.txt`` and compared against nothing. The
concrete consequence is why this module exists: the representative business skill
``rfq-quote`` writes its deliverables with ``xlsxwriter`` and the desktop bundle
does not carry it. The import is lazy (inside ``Report.__init__``), so the failure
arrives at output time inside a sandboxed subprocess as an opaque
``ModuleNotFoundError`` -- after the model has already reported success.

Three outcomes, and the third is the one that is easy to get wrong:

* **shipped** -- declared and in the lock: nothing to say.
* **missing** -- declared, resolvable to a distribution, absent from the lock:
  ``dependency_missing``, naming the skill, the module, the distribution and the
  file to add it to.
* **undeclared** -- declared, but this module cannot tell which distribution
  provides it: ``dependency_undeclared``. Reporting *nothing* here would be the
  silent-skip failure this repository has hit before (a check that "passes" because
  it never looked). "I cannot tell" must be visible, not read as "fine".

The module deliberately does **not** install anything. Preparing dependencies is
the build flow's job (``desktop/build/build-backend.sh`` builds an isolated venv
from the lock and bundles it with PyInstaller), so this module's contribution is to
say precisely what that flow must carry -- and to fail loudly when it does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "MIN_PYTHON",
    "MODERN_MIN_PYTHON",
    "MAX_PYTHON_EXCLUSIVE",
    "LOCK_PATH_HINT",
    "Declaration",
    "Problem",
    "parse_lock",
    "normalize_distribution",
    "module_distribution",
    "declarations",
    "check_declarations",
    "check_python_version",
    "check_skills",
]

#: The interpreter window the desktop bundle supports, as the *union* of what the
#: release lines actually build with:
#:
#: * ``release.yml`` / ``release-overlay.yml`` -- Python 3.11 (and
#:   ``build-backend.sh`` probes 3.10/3.11/3.12);
#: * ``release-win7.yml`` -- Python 3.8, the last CPython supporting Windows 7,
#:   with ``playwright`` relaxed to the last ``cp38`` wheel.
#:
#: The ceiling is 3.14 because nothing verifies a 3.14 bundle: the requirements
#: carry explicit ``python_version >= "3.13"`` handling (``legacy-cgi`` plus a
#: git-sourced ``web.py``), so 3.13 is intended, and 3.14 is not yet.
#: Declaring the union rather than one number matters: a single ``>=3.10`` floor
#: would make the guard refuse the legacy Windows 7 release, which is a real line
#: in the packaging flow, not a hypothetical.
MIN_PYTHON: Tuple[int, int] = (3, 8)
#: The floor for the modern (non-Windows-7) lines. Recorded separately because the
#: union above is what the guard enforces, while this is what ``build-backend.sh``
#: probes for; the two are different questions.
MODERN_MIN_PYTHON: Tuple[int, int] = (3, 10)
MAX_PYTHON_EXCLUSIVE: Tuple[int, int] = (3, 14)

#: Named in messages so the remediation is a file the reader can open.
LOCK_PATH_HINT = "desktop/build/requirements-desktop.txt"

#: Import name -> distribution name. Only *necessary* entries belong here: a
#: mapping that merely restates the identity (``requests`` -> ``requests``) is
#: listed as an identity row like any other,
#: and padding the table would make it look like the source of truth.
#:
#: There is deliberately **no** generic "same name" fallback. One was tried and
#: removed: it made every unknown import resolve to a distribution of its own
#: name, so an unshipped library passed the check. Being in this table is the
#: claim that this project knows who provides the module, and the identity rows
#: are therefore listed explicitly rather than inferred.
MODULE_DISTRIBUTIONS: Dict[str, str] = {
    # --- the mismatches: import name differs from the distribution ---
    "yaml": "pyyaml",
    "PIL": "pillow",
    "docx": "pythondocx",
    "pptx": "pythonpptx",
    "dotenv": "pythondotenv",
    "Crypto": "pycryptodome",
    "google.generativeai": "googlegenerativeai",
    "json_repair": "jsonrepair",
    "gtts": "gtts",
    "telegram": "pythontelegrambot",
    "slack_bolt": "slackbolt",
    "discord": "discordpy",
    "websocket": "websocketclient",
    "web": "webpy",
    "legacy_cgi": "legacycgi",
    "zai": "zaisdk",
    "lark_oapi": "larkoapi",
    "dingtalk_stream": "dingtalkstream",
    "PyPDF2": "pypdf2",
    # --- the identity cases, listed so "unknown" stays unknown ---
    "numpy": "numpy",
    "aiohttp": "aiohttp",
    "requests": "requests",
    "chardet": "chardet",
    "croniter": "croniter",
    "click": "click",
    "qrcode": "qrcode",
    "dashscope": "dashscope",
    "tenacity": "tenacity",
    "tiktoken": "tiktoken",
    "pydub": "pydub",
    "playwright": "playwright",
    "wechatpy": "wechatpy",
    "pycryptodome": "pycryptodome",
    "certifi": "certifi",
    "openpyxl": "openpyxl",
    "xlsxwriter": "xlsxwriter",
    "xlrd": "xlrd",
    "pandas": "pandas",
    "scipy": "scipy",
    "matplotlib": "matplotlib",
    "jieba": "jieba",
    "pdfplumber": "pdfplumber",
    "pypdf": "pypdf",
}

#: Kinds that name a **Python library** and can therefore be checked against the
#: lock. ``bins`` is separate: a binary is the machine's business, not the
#: bundle's, so it is only checked when the caller supplies a probe.
_LIBRARY_KINDS = frozenset({"python", "pip"})

#: ``bins``-shaped kinds, checked only with a probe.
_BINARY_KINDS = frozenset({"bins", "anyBins"})

#: One requirement line: the name is everything before an extras/version/marker
#: separator. ``web.py @ git+...`` and ``uvicorn[standard]>=0.20`` both reduce to
#: their base name.
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize_distribution(name: str) -> str:
    """Normalize a distribution name the way package metadata compares them.

    Case-insensitive and insensitive to ``-``/``_``/``.``: ``PyYAML``,
    ``py-yaml`` and ``pyyaml`` are one distribution, and a check that treated
    them as three would report a shipped library as missing.
    """
    return re.sub(r"[-_.]+", "", str(name).strip().lower())


def parse_lock(text: str) -> Dict[str, str]:
    """``normalized distribution -> original requirement line`` for one lock file.

    Comments, blank lines and line continuations are dropped. A line with an
    environment marker (``aiohttp>=3.10; python_version >= "3.13"``) is still a
    *shipped* distribution: the marker says which Python uses it, not whether the
    build carries it. Dropping marked lines would report a real library as missing
    on the interpreters the marker excludes.
    """
    lock: Dict[str, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        while line.endswith("\\"):
            line = line[:-1].strip()
        if not line or line.startswith("#"):
            continue
        match = _NAME.match(line)
        if match is None:
            continue
        lock.setdefault(normalize_distribution(match.group(1)), line)
    return lock


def module_distribution(module: str) -> Optional[str]:
    """The distribution providing ``module``, or ``None`` when it is not known.

    ``None`` is a real answer and must stay distinguishable from a guess. An
    earlier version fell back to "the module name is the distribution name", which
    sounds harmless and destroys the check: every unknown import then resolves to
    *some* distribution, so an unshipped library looks verified. The table below
    therefore lists the identity cases explicitly too -- being in the table is the
    claim that this project knows who provides the module.
    """
    if not module:
        return None
    text = str(module).strip()
    root = text.split(".")[0]
    for candidate in (text, root):
        mapped = MODULE_DISTRIBUTIONS.get(candidate)
        if mapped:
            return normalize_distribution(mapped)
    return None


@dataclass(frozen=True)
class Declaration:
    """One thing a skill says it needs, and where it said so."""

    skill: str
    kind: str
    name: str
    #: ``requires`` or ``install`` -- which declarative surface it came from, so a
    #: message can point at the frontmatter the author should edit.
    source: str = "requires"


@dataclass(frozen=True)
class Problem:
    """A declaration this environment cannot satisfy, with a stable code."""

    skill: str
    name: str
    code: str
    message: str
    kind: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.skill}: {self.message}"


def _install_applies(spec: Any, platform: Optional[str]) -> bool:
    """Whether an install spec is meant for ``platform``.

    A spec with no ``os`` applies everywhere. ``platform`` is the caller's
    (``sys.platform``-ish); when it is unknown the spec is kept rather than
    dropped, because dropping it would under-report requirements.
    """
    declared = list(getattr(spec, "os", None) or [])
    if not declared or not platform:
        return True
    normalized = {str(p).strip() for p in declared}
    return platform in normalized or platform.split("-")[0] in normalized


def declarations(entry: Any, *, platform: Optional[str] = None) -> List[Declaration]:
    """Everything one skill declares it needs, from both declarative surfaces.

    ``requires`` keeps its kind as declared (so a ``python`` module stays
    ``python``), while ``install`` specs map to the kind that decides how they are
    checked: a ``pip`` spec is a distribution, a ``download`` spec is a binary.
    """
    name = getattr(getattr(entry, "skill", None), "name", "") or "?"
    metadata = getattr(entry, "metadata", None)
    if metadata is None:
        return []

    found: List[Declaration] = []
    requires = getattr(metadata, "requires", None) or {}
    for kind, names in requires.items():
        for item in names or []:
            text = str(item or "").strip()
            if text:
                found.append(Declaration(skill=name, kind=str(kind), name=text,
                                         source="requires"))

    for spec in getattr(metadata, "install", None) or []:
        if not _install_applies(spec, platform):
            continue
        kind = str(getattr(spec, "kind", "") or "").strip().lower()
        if kind == "pip":
            package = str(getattr(spec, "package", "") or "").strip()
            if package:
                found.append(Declaration(skill=name, kind="pip", name=package,
                                         source="install"))
            continue
        if kind == "download":
            for binary in getattr(spec, "bins", None) or []:
                text = str(binary or "").strip()
                if text:
                    found.append(Declaration(skill=name, kind="bins", name=text,
                                             source="install"))
            continue
        # ``brew`` / ``npm`` and anything else are the machine's business and are
        # not part of "what the bundle ships"; enablement already gates them.
    return found


def check_python_version(version: Tuple[int, int]) -> List[Problem]:
    """Problems with running on ``version``, or ``[]`` when it is supported.

    Reported instead of raising: an unsupported interpreter is one problem among
    several, and the caller wants the whole list before it decides to stop.
    """
    current = (int(version[0]), int(version[1]))
    if current < MIN_PYTHON:
        return [Problem(
            skill="<runtime>", name=".".join(map(str, current)),
            code="python_incompatible", kind="python",
            message=(f"Python {current[0]}.{current[1]} is below the supported floor "
                     f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}; the desktop bundle is built for "
                     f">={MIN_PYTHON[0]}.{MIN_PYTHON[1]},<{MAX_PYTHON_EXCLUSIVE[0]}."
                     f"{MAX_PYTHON_EXCLUSIVE[1]}"))]
    if current >= MAX_PYTHON_EXCLUSIVE:
        return [Problem(
            skill="<runtime>", name=".".join(map(str, current)),
            code="python_incompatible", kind="python",
            message=(f"Python {current[0]}.{current[1]} is not supported; the desktop "
                     f"bundle is built for >={MIN_PYTHON[0]}.{MIN_PYTHON[1]},"
                     f"<{MAX_PYTHON_EXCLUSIVE[0]}.{MAX_PYTHON_EXCLUSIVE[1]}"))]
    return []


def check_declarations(declarations_to_check: Iterable[Declaration],
                       lock: Dict[str, str], *,
                       platform: Optional[str] = None,
                       python_version: Optional[Tuple[int, int]] = None,
                       which: Optional[Callable[[str], Optional[str]]] = None
                       ) -> List[Problem]:
    """Every way these declarations are not satisfiable by this lock.

    ``which`` is optional and its absence means "unknown", not "present": a
    binary is only reported when a probe was supplied, so a caller that cannot
    look does not get a fabricated pass or fail. ``python_version`` adds the
    interpreter-window problems once, not per declaration.
    """
    problems: List[Problem] = []
    seen: set = set()
    if python_version is not None:
        problems.extend(check_python_version(python_version))

    for decl in declarations_to_check:
        if decl.kind in _BINARY_KINDS:
            if which is None:
                continue
            if not which(decl.name):
                problem = Problem(
                    skill=decl.skill, name=decl.name, kind=decl.kind,
                    code="dependency_missing",
                    message=(f"{decl.skill} requires the executable {decl.name!r} and it "
                             f"is not on PATH. Install it, or drop the declaration."))
                if _remember(seen, problem):
                    problems.append(problem)
            continue

        if decl.kind not in _LIBRARY_KINDS:
            continue

        if decl.kind == "pip":
            distribution = normalize_distribution(decl.name)
        else:
            distribution = module_distribution(decl.name)
            if distribution is None:
                # "Cannot tell" is reported, not skipped: a check that silently
                # passes on the cases it cannot evaluate is worse than no check,
                # because it reads as a guarantee.
                problem = Problem(
                    skill=decl.skill, name=decl.name, kind=decl.kind,
                    code="dependency_undeclared",
                    message=(f"{decl.skill} imports {decl.name!r}, but no distribution is "
                             f"known to provide it, so whether the desktop bundle ships it "
                             f"cannot be checked. Add the mapping to "
                             f"agent/skills/dependencies.py, or declare it as an install "
                             f"spec with kind: pip and the distribution name."))
                if _remember(seen, problem):
                    problems.append(problem)
                continue

        if distribution not in lock:
            problem = Problem(
                skill=decl.skill, name=decl.name, kind=decl.kind,
                code="dependency_missing",
                message=(f"{decl.skill} needs {decl.name!r} (distribution "
                         f"{decl.name if decl.kind == 'pip' else distribution!r}) and the "
                         f"desktop bundle does not ship it. Add it to {LOCK_PATH_HINT}, "
                         f"or stop declaring it in the skill's frontmatter."))
            # Deduplicated by name+code, so declaring the same library on both
            # surfaces (`requires.python` for the runtime check, `install: pip`
            # for the distribution name) reports one problem, not one per
            # surface. A doubled message reads like two distinct defects.
            if _remember(seen, problem, key=(decl.skill, problem.code, distribution)):
                problems.append(problem)
    return problems


def _remember(seen: set, problem: Problem, key: Optional[tuple] = None) -> bool:
    """Record ``problem`` and say whether it is new."""
    identity = key if key is not None else (problem.skill, problem.code, problem.name)
    if identity in seen:
        return False
    seen.add(identity)
    return True


def check_skills(entries: Iterable[Any], lock_text: str, *,
                 platform: Optional[str] = None,
                 python_version: Optional[Tuple[int, int]] = None,
                 which: Optional[Callable[[str], Optional[str]]] = None
                 ) -> Dict[str, List[Problem]]:
    """Per-skill problems for a whole set of skills, keyed by skill name.

    Skills with nothing to report are omitted, so an empty dict means "the bundle
    satisfies every declaration in this set".
    """
    lock = parse_lock(lock_text)
    by_skill: Dict[str, List[Problem]] = {}
    runtime_problems = check_python_version(python_version) if python_version else []
    if runtime_problems:
        by_skill["<runtime>"] = list(runtime_problems)
    for entry in entries:
        name = getattr(getattr(entry, "skill", None), "name", "") or "?"
        found = declarations(entry, platform=platform)
        problems = check_declarations(found, lock, platform=platform, which=which)
        if problems:
            by_skill[name] = problems
    return by_skill
