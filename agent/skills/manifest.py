# encoding:utf-8
"""The authorized skill manifest (change task 8.1).

A skill that runs against a user's own project is not "the skill folder on the
server". It is *a specific authorized version* of that skill, the exact resources
it needs, and the platform it will actually execute on. This module is what fixes
all three into one object, and it is deliberately built on top of the existing
:mod:`agent.skills.manager` rather than beside it: the manifest describes a skill
the manager has already selected and vetted, so there is no second place where
"which skills may run" gets decided.

Two properties carry the weight:

``digest``
    A content digest over **every** resource, not over the description. A digest
    that stopped at ``SKILL.md`` would let a swapped script keep the same
    identity, which is the one substitution a device cannot detect on its own.
    It is computed over a *sorted* resource list, so two machines mounting the
    same package agree on its identity instead of hashing in ``readdir`` order.

``platform``
    The **device's** platform, taken from the caller. A server that stamped its
    own platform into the manifest would ship Linux-path assumptions to Windows,
    which is exactly the failure the spec calls out.

Nothing here reads a network or a device: it walks a directory that is already on
this machine and returns plain data.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from agent.desktop_local.package_rules import (
    PackageRuleError,
    digest_of,
    refuse_secrets,
    safe_relative,
)
from agent.skills.types import SkillEntry

__all__ = [
    "SkillManifest",
    "SkillManifestError",
    "SkillResource",
    "build_skill_manifest",
    "read_skill_payloads",
]


class SkillManifestError(Exception):
    """A refused manifest, with a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SkillResource:
    """One file in a package, identified by content rather than by name."""

    relative_path: str
    digest: str
    size: int


@dataclass(frozen=True)
class SkillManifest:
    """Everything the device needs to mount exactly the reviewed version."""

    skill_id: str
    name: str
    digest: str
    platform: str
    resources: Tuple[SkillResource, ...]
    dependencies: Tuple[str, ...] = ()
    missing_dependencies: Tuple[str, ...] = ()
    #: Where the resources were read from. Local only -- it is the server's own
    #: path and must never reach the device (``to_dict`` leaves it out).
    base_dir: str = ""
    frontmatter: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """The wire form, which is also what the device-side cache verifies."""
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "digest": self.digest,
            "platform": self.platform,
            "dependencies": list(self.dependencies),
            "resources": [
                {"relative_path": r.relative_path, "digest": r.digest, "size": r.size}
                for r in self.resources
            ],
        }


#: How a frontmatter ``os:`` entry maps onto the contract's two platforms. A
#: package is buildable for a device only when the skill claims that platform.
_PLATFORM_ALIASES: Dict[str, str] = {
    "darwin": "posix", "macos": "posix", "mac": "posix",
    "linux": "posix", "posix": "posix", "unix": "posix",
    "win32": "win32", "windows": "win32", "win": "win32",
}


def _skill_id(entry: SkillEntry) -> str:
    """The ``source:name`` id the rest of the system already uses for grants."""
    source = entry.skill.source if entry.skill.source in ("builtin", "custom") else "builtin"
    return f"{source}:{entry.skill.name}"


def _supported_platforms(entry: SkillEntry) -> set:
    declared = list(getattr(entry.metadata, "os", None) or [])
    return {_PLATFORM_ALIASES.get(str(item).lower(), str(item).lower())
            for item in declared}


def _declared_dependencies(entry: SkillEntry) -> Tuple[str, ...]:
    """Flatten a skill's declared requirements into one ordered list.

    ``requires`` is grouped by kind (``bins``, ``python``, ``env`` ...) because
    that is what the installer needs; a manifest only has to say what has to be
    present, so the grouping is dropped and the names are de-duplicated while
    keeping their declared order.
    """
    requires = getattr(entry.metadata, "requires", None) or {}
    seen: list = []
    for _kind, names in requires.items():
        for name in names or []:
            text = str(name).strip()
            if text and text not in seen:
                seen.append(text)
    return tuple(seen)


def _missing_dependencies(entry: SkillEntry, which: Optional[Callable[[str], Optional[str]]],
                          names: Iterable[str]) -> Tuple[str, ...]:
    """Which declared binaries are not on this machine.

    Only ``bins`` can be answered without importing anything, and importing an
    arbitrary skill's Python module to test it would execute its import-time code
    -- so modules are reported as declared dependencies and left to the run,
    rather than being probed by importing.
    """
    if which is None:
        return ()
    bins = list((getattr(entry.metadata, "requires", None) or {}).get("bins") or [])
    missing = []
    for name in bins:
        text = str(name).strip()
        if text and text in set(names) and not which(text):
            missing.append(text)
    return tuple(missing)


def _walk_resources(base_dir: str) -> Tuple[SkillResource, ...]:
    """Every regular file under ``base_dir``, as a digest-addressed resource.

    Symlinks are refused rather than skipped: a link is the one entry whose
    content is not inside the package it appears to be part of, so "package the
    skill directory" would otherwise mean "package whatever it points at".
    """
    resources = []
    for root, dirs, files in os.walk(base_dir):
        for name in list(dirs):
            if os.path.islink(os.path.join(root, name)):
                raise SkillManifestError(
                    "link_refused",
                    f"{os.path.relpath(os.path.join(root, name), base_dir)!r} is a link")
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            relative = os.path.relpath(full, base_dir).replace(os.sep, "/")
            if os.path.islink(full):
                raise SkillManifestError("link_refused", f"{relative!r} is a link")
            try:
                relative = safe_relative(relative)
                refuse_secrets(relative)
            except PackageRuleError as err:
                raise SkillManifestError(err.code, err.message) from None
            with open(full, "rb") as handle:
                body = handle.read()
            resources.append(SkillResource(
                relative_path=relative, digest=digest_of(body), size=len(body)))
    return tuple(sorted(resources, key=lambda r: r.relative_path))


def _content_digest(resources: Iterable[SkillResource]) -> str:
    """One digest over the whole resource set, independent of walk order.

    Each entry contributes its path, its content digest and its size, so the
    result changes if a file is added, removed, renamed, or edited -- the four
    ways a package can differ while still looking like "the same skill".

    The sort is *inside* the function rather than left to the caller: a digest
    that is only stable because the caller happened to sort first is one refactor
    away from making every deploy look like a new version.
    """
    material = "".join(
        f"{r.relative_path}\u0000{r.digest}\u0000{r.size}\u0001"
        for r in sorted(resources, key=lambda r: r.relative_path))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_skill_manifest(entry: SkillEntry, *, platform: str,
                         is_authorized: Callable[[str], bool],
                         dependencies: Iterable[str] = (),
                         which: Optional[Callable[[str], Optional[str]]] = None,
                         ) -> SkillManifest:
    """Describe the version of ``entry`` that may be deployed to ``platform``.

    ``is_authorized`` is a **required** keyword with no default, and is called on
    every build. A default of "yes" would make the one check that matters --
    whether the identity may still use this skill -- something a caller opts into
    by remembering to pass it, which is precisely how a revoked grant keeps
    working. It is called here rather than read from a cached decision so that a
    grant revoked between two turns takes effect on the next manifest.

    :param platform: the *device's* platform, ``"posix"`` or ``"win32"``.
    :param which: optional ``shutil.which``-like probe; when supplied, declared
        binaries that are absent are reported in ``missing_dependencies``.
    """
    skill_id = _skill_id(entry)
    if not is_authorized(skill_id):
        raise SkillManifestError(
            "not_authorized", f"{skill_id} is not authorized for this identity")

    base_dir = getattr(entry.skill, "base_dir", "") or ""
    if not base_dir or not os.path.isdir(base_dir):
        raise SkillManifestError(
            "skill_unavailable", f"{skill_id} has no readable resource directory")

    supported = _supported_platforms(entry)
    if supported and platform not in supported:
        raise SkillManifestError(
            "platform_unsupported",
            f"{skill_id} declares {sorted(supported)} but this device is {platform}")

    resources = _walk_resources(base_dir)
    if not resources:
        raise SkillManifestError(
            "skill_unavailable", f"{skill_id} has no resources to deploy")

    names: list = []
    for name in list(_declared_dependencies(entry)) + [str(d) for d in dependencies]:
        text = str(name).strip()
        if text and text not in names:
            names.append(text)

    return SkillManifest(
        skill_id=skill_id,
        name=entry.skill.name,
        digest=_content_digest(resources),
        platform=platform,
        resources=resources,
        dependencies=tuple(names),
        missing_dependencies=_missing_dependencies(entry, which, names),
        base_dir=base_dir,
        frontmatter=dict(getattr(entry.skill, "frontmatter", None) or {}),
    )


def read_skill_payloads(manifest: SkillManifest) -> Dict[str, bytes]:
    """The bytes a manifest describes, keyed by relative path.

    Read back from the same directory the digest was computed over, so what a
    caller ships is what the digest covers. The device-side cache re-verifies
    every entry against the manifest, which is what makes that a fact rather than
    a convention.
    """
    if not manifest.base_dir:
        raise SkillManifestError(
            "skill_unavailable", f"{manifest.skill_id} has no resource directory")
    payloads: Dict[str, bytes] = {}
    for resource in manifest.resources:
        full = os.path.join(manifest.base_dir, *resource.relative_path.split("/"))
        if os.path.islink(full):
            raise SkillManifestError(
                "link_refused", f"{resource.relative_path!r} is a link")
        try:
            with open(full, "rb") as handle:
                payloads[resource.relative_path] = handle.read()
        except OSError as err:
            raise SkillManifestError(
                "skill_unavailable",
                f"{resource.relative_path!r} could not be read: {err}") from None
    return payloads
