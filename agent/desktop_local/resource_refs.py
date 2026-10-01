# encoding:utf-8
"""Typed resource references: three roots, resolved to real local paths (8.4).

A local run reads from two different kinds of place, and writes to a third:

``project``
    The directory the user opened. **The only writable location.** A business
    relative path (``output/报告.docx``) means this root.

``skill``
    A verified skill version in the read-only cache (task 8.2/8.3). A skill's own
    template means *this* root -- not the project, even when a file of the same
    name happens to exist there.

``backend``
    The server's filesystem. It has no local path, so for a local run it is a
    refusal rather than a translation.

One flat "relative path" rule cannot serve both roots, and the failure is
directional: a skill reading ``templates/report.xlsx`` would silently read the
*project's* decoy, and a skill writing ``output/x.docx`` could land inside its own
read-only cache. So a reference states which root it means, and the two are
resolved separately.

The logical form is what the model is given and what it may write::

    project:output/报告.docx
    skill:builtin:excel/templates/report.xlsx
    backend:/srv/data/report.csv

Three rules the spec is explicit about, and which shape the code:

1. **A server path is refused, not translated.** ``backend:`` has no local
   resolution for a local run; it is reported by name. Nothing here guesses that
   ``/srv/data/x.csv`` might be ``./x.csv``.
2. **A missing resource is reported, not substituted.** A skill resource that is
   not in the pinned version raises ``incompatible_skill`` naming the skill and
   the resource. A same-named project file is never handed over instead -- that
   turns "the skill is broken here" into "the skill produced the wrong document".
3. **Nothing is rewritten.** This module *reads* to resolve; it never edits a
   skill's files or text-substitutes a command string. Patching a skill's source
   to paper over an unreachable path would hide the incompatibility rather than
   report it, and rewriting a shell program's text is the "string filter standing
   in for isolation" the design rejects -- it would both miss ``$(cat ...)`` and
   corrupt an ordinary command that merely mentions a path.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from common.log import logger

__all__ = [
    "PROJECT",
    "SKILL",
    "BACKEND",
    "RESOURCE",
    "LogicalRef",
    "ResolvedRef",
    "ResourceRefError",
    "RunSkillSet",
    "parse_ref",
    "format_ref",
    "resolve_ref",
    "resolve_arguments",
    "ensure_readable",
    "ensure_writable",
]

PROJECT = "project"
SKILL = "skill"
BACKEND = "backend"
#: A server-side resource, pinned for this run as ``(resource_id, version,
#: digest)`` (task 8.6). It has no local path *until it is landed*
#: (``resource_landing``), which is why resolving one here refuses: the whole
#: point is that a server resource must never be addressed as a local path.
RESOURCE = "resource"

#: A logical reference starts with ``<kind>:`` and an optional ``//``.
_PREFIX = re.compile(r"^(project|skill|backend|resource):(//)?")

#: Argument names whose values name a resource. Kept in step with
#: ``source_resolver.PATH_ARGUMENTS`` so the two walks cannot disagree.
PATH_ARGUMENTS = ("path", "paths", "file_path", "directory", "dir")


class ResourceRefError(Exception):
    """A refused reference or placement, with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _is_absolute(text: str) -> bool:
    return (os.path.isabs(text) or text.startswith(("\\", "~"))
            or bool(re.match(r"^[A-Za-z]:", text)))


def _clean_relative(raw: str) -> str:
    """Normalise a resource path, refusing anything that could escape a root.

    ``.`` segments are dropped (``a/./b`` is ``a/b``), because that is only
    spelling. ``..`` is **refused** rather than collapsed: accepting it makes
    "inside the root" depend on getting the collapse exactly right in both this
    module and the sandbox, and a single disagreement is an escape. Backslashes
    are refused too, so a Windows-style path cannot mean something different here
    than it does to the caller.
    """
    text = raw.strip()
    if "\\" in text:
        raise ResourceRefError(
            "outside_project", f"{raw!r} is not a forward-slash relative path")
    if text.startswith("/"):
        raise ResourceRefError(
            "invalid_reference", f"a resource path is relative, not absolute: {raw!r}")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ResourceRefError(
            "outside_project", f"{raw!r} is not a normalised relative path")
    return "/".join(parts)


@dataclass(frozen=True)
class LogicalRef:
    """A reference as written: which root, and what inside it."""

    kind: str
    relative: str = ""
    #: Only for ``skill``: the ``source:name`` id of the skill.
    skill_id: Optional[str] = None
    #: Only for ``resource``: the version the run pinned. A resource that comes
    #: back at another version is a drift, not an equivalent (task 8.6).
    version: str = ""
    #: Only for ``resource``: the expected content digest. Carried in the
    #: reference so the guarantee travels with the claim instead of depending on
    #: a second lookup that could answer about a different version.
    digest: str = ""
    #: The original text, for error messages that quote what was asked for.
    raw: str = ""


@dataclass(frozen=True)
class ResolvedRef:
    """A reference with a real local path, or a reason there is none."""

    kind: str
    logical: str
    absolute: Optional[str] = None
    read_only: bool = False
    exists: bool = False
    is_dir: bool = False

    @property
    def writable(self) -> bool:
        return self.absolute is not None and not self.read_only


def parse_ref(raw: Any) -> Optional[LogicalRef]:
    """Parse a logical reference, or ``None`` when it is not a typed one.

    ``None`` means "this is the caller's own business" -- an untyped absolute
    path in particular is *not* guessed into ``backend`` here, because deciding
    that is the caller's rule (``source_resolver`` refuses it for a local run)
    and guessing it here would be a second, weaker copy of that decision. A
    bare relative path is unambiguous, and stays a project reference so the
    behaviour a caller already had is unchanged.
    """
    if raw is None:
        raise ResourceRefError("invalid_reference", "empty reference")
    text = str(raw).strip()
    if not text:
        raise ResourceRefError("invalid_reference", "empty reference")

    match = _PREFIX.match(text)
    if match is None:
        if _is_absolute(text):
            return None
        try:
            relative = _clean_relative(text)
        except ResourceRefError:
            # A form this module does not own (a backslash, a ``..`` segment).
            # Returning ``None`` hands it back to the caller's own relative-path
            # rule, which is what decided it before this module existed -- so
            # adding typed references cannot change how a bare path behaves.
            return None
        return LogicalRef(kind=PROJECT, relative=relative, raw=text)

    kind = match.group(1)
    payload = text[match.end():]
    if kind == PROJECT:
        return LogicalRef(kind=PROJECT, relative=_clean_relative(payload), raw=text)
    if kind == BACKEND:
        if not payload:
            raise ResourceRefError("invalid_reference", "backend: names nothing")
        return LogicalRef(kind=BACKEND, relative=payload, raw=text)
    if kind == RESOURCE:
        return _parse_resource(payload, text)

    # ``skill:<source>:<name>/<resource>``. The skill id keeps its own colon, so
    # the resource boundary is the first ``/`` -- which is exactly the grammar
    # ``source:name`` already has.
    if "/" not in payload:
        raise ResourceRefError(
            "invalid_reference",
            f"a skill reference needs a resource after the skill id: {text!r}")
    skill_id, relative = payload.split("/", 1)
    if not skill_id:
        raise ResourceRefError("invalid_reference", "skill: names no skill")
    if not relative:
        raise ResourceRefError(
            "invalid_reference", f"a skill reference needs a resource: {text!r}")
    return LogicalRef(kind=SKILL, skill_id=skill_id,
                      relative=_clean_relative(relative), raw=text)


def _parse_resource(payload: str, text: str) -> LogicalRef:
    """``resource:<id>[@<version>][#<digest>]``, refusing an unusable id.

    The id is run through the same relative-path grammar as everything else, so
    a resource id can name a nested entry (``a/b/c.txt``) but cannot carry ``..``,
    a backslash or a leading ``/``. That matters more here than anywhere else:
    the id comes from the server, and it is about to become a filename on this
    machine, so it is exactly the value that must not be trusted as a path.
    """
    if not payload:
        raise ResourceRefError("invalid_reference", "resource: names nothing")
    digest = ""
    body = payload
    if "#" in body:
        body, _, digest = body.partition("#")
        if not digest:
            raise ResourceRefError(
                "invalid_reference", f"resource: has an empty digest pin: {text!r}")
    version = ""
    if "@" in body:
        body, _, version = body.partition("@")
        if not version:
            raise ResourceRefError(
                "invalid_reference", f"resource: has an empty version pin: {text!r}")
    if not body:
        raise ResourceRefError("invalid_reference", f"resource: names nothing: {text!r}")
    relative = _clean_relative(body)
    if not relative:
        raise ResourceRefError("invalid_reference", f"resource: names nothing: {text!r}")
    return LogicalRef(kind=RESOURCE, relative=relative, version=version,
                      digest=digest, raw=text)


def format_ref(ref: LogicalRef) -> str:
    """The logical text form, which is what the model is given."""
    if ref.kind == SKILL:
        return f"{SKILL}:{ref.skill_id}/{ref.relative}"
    if ref.kind == BACKEND:
        return f"{BACKEND}:{ref.relative}"
    if ref.kind == RESOURCE:
        tail = f"@{ref.version}" if ref.version else ""
        tail += f"#{ref.digest}" if ref.digest else ""
        return f"{RESOURCE}:{ref.relative}{tail}"
    return f"{PROJECT}:{ref.relative}"


class RunSkillSet:
    """The skill versions pinned for one run, as resolvable read-only roots.

    Pinning is what lets resolution be honest: a skill reference resolves only
    into a version that was *acquired* for this run, so a reference cannot reach
    a leftover directory from an earlier run, and the version stays alive against
    garbage collection while the run reads it (task 8.3).

    ``authorized`` is consulted on every :meth:`pin` -- a revocation between two
    turns stops resolving even though the bytes are still on disk.
    """

    def __init__(self, cache: Any, scope: Any, *,
                 authorized: Callable[[str], bool]):
        self._cache = cache
        self._scope = scope
        self._authorized = authorized
        #: skill_id -> digest, in pin order, so roots() is stable for a caller.
        self._pinned: Dict[str, str] = {}

    def pin(self, skill_id: str, digest: str) -> str:
        """Reserve a version for this run and return its directory.

        Idempotent per skill: pinning the same version twice keeps *one*
        reference. Acquiring again would bump the cache's count while
        :meth:`release_all` decrements once, so the count could never reach zero
        and garbage collection would never reclaim the version -- a leak that
        looks like "the cache just grows". Re-pinning a *different* version
        releases the old reference first, so a run holds exactly one version of a
        given skill.
        """
        current = self._pinned.get(skill_id)
        if current == digest:
            return self._cache.version_dir(self._scope, skill_id, digest)
        if current is not None:
            self._release_one(skill_id, current)
            self._pinned.pop(skill_id, None)
        try:
            directory = self._cache.acquire(
                self._scope, skill_id, digest, authorized=self._authorized)
        except Exception as err:  # noqa: BLE001 - translate, do not leak internals
            code = getattr(err, "code", None)
            if code == "not_authorized":
                raise ResourceRefError("not_authorized", str(err)) from None
            raise ResourceRefError("incompatible_skill", str(err)) from None
        self._pinned[skill_id] = digest
        return directory

    def _release_one(self, skill_id: str, digest: str) -> None:
        try:
            self._cache.release(self._scope, skill_id, digest)
        except Exception as e:  # noqa: BLE001 - a failed release must not throw
            logger.warning(f"[ResourceRefs] releasing {skill_id} failed: {e}")

    def release_all(self) -> None:
        """Drop every reference this run held. Safe to call more than once."""
        for skill_id, digest in list(self._pinned.items()):
            self._release_one(skill_id, digest)
        self._pinned.clear()

    def roots(self) -> List[str]:
        """The read-only directories a sandbox should be granted.

        This is the value that has to reach the execution grant's ``skillRoots``:
        the sandbox already treats those as readable and never writable, so the
        read-only rule is enforced by the OS rather than by this module's
        bookkeeping.
        """
        return [self._cache.version_dir(self._scope, skill_id, digest)
                for skill_id, digest in self._pinned.items()]

    def pins(self) -> List[Dict[str, str]]:
        """``[{"skill_id", "digest"}]``: which versions this run is pinned to.

        The portable form of :meth:`roots`. Where ``roots()`` answers "which
        directories may the sandbox read", this answers "which *versions* did the
        run authorize" -- the question a second machine can check, since it has
        neither this cache nor these paths (task 8.9).

        Canonical order (sorted by ``skill_id``, the same form the digest uses),
        so the set a run reports and the set its command digest covers are one
        list and not two orderings of the same thing.
        """
        return sorted(({"skill_id": skill_id, "digest": digest}
                       for skill_id, digest in self._pinned.items()),
                      key=lambda entry: entry["skill_id"])

    def directory(self, skill_id: str) -> Optional[str]:
        digest = self._pinned.get(skill_id)
        if digest is None:
            return None
        return self._cache.version_dir(self._scope, skill_id, digest)


def _within(root: str, relative: str) -> str:
    """``relative`` under ``root``, judged on the real path.

    Containment on the real path means a component swapped for a symlink pointing
    outside the root is refused rather than followed.
    """
    candidate = root if not relative else os.path.join(root, *relative.split("/"))
    real = os.path.realpath(candidate)
    real_root = os.path.realpath(root)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise ResourceRefError(
            "outside_project", f"{relative!r} resolves outside its root")
    return real


def resolve_ref(raw: Any, *, project_root: str,
                skills: Optional[RunSkillSet] = None) -> ResolvedRef:
    """Resolve one logical reference to an actual local path.

    Raises :class:`ResourceRefError` for every case the spec requires be reported
    rather than worked around: an escaping path, a backend path (no local
    equivalent), and a skill resource that is not in the pinned version.
    """
    ref = parse_ref(raw)
    if ref is None:
        text = str(raw).strip()
        if _is_absolute(text):
            # An untyped absolute path. For a local run there is nothing to
            # resolve it against, and treating it as a project path would be the
            # "server path parsed as a local one" bug -- so it is refused by name.
            raise ResourceRefError(
                "server_path_not_local",
                f"{raw!r} is an absolute path with no local root; "
                f"use project:<relative> for this machine or backend:<path> for the server")
        # A relative form this module refuses (a ``..`` segment, a backslash).
        # ``parse_ref`` declines to own it so the caller's own rule can decide,
        # but here the caller *is* this module, so the precise reason is raised
        # rather than being reported as an untyped absolute path.
        _clean_relative(text)
        raise ResourceRefError(
            "invalid_reference", f"{raw!r} is not a usable resource reference")

    if ref.kind == BACKEND:
        raise ResourceRefError(
            "server_path_not_local",
            f"the server path {ref.relative!r} has no local equivalent; "
            f"a local run cannot read it, and it is not copied down automatically")

    if ref.kind == RESOURCE:
        # A server resource has no local path until an explicit landing puts one
        # there (``resource_landing``). Refusing here -- rather than resolving to
        # a directory literally named ``resource:att_1`` -- is what keeps "not
        # landed" from turning into a confusing "file not found".
        raise ResourceRefError(
            "resource_not_landed",
            f"the server resource {ref.relative!r} is not on this machine; "
            f"it must be landed explicitly before a local tool can read it")

    if ref.kind == PROJECT:
        absolute = _within(project_root, ref.relative)
        return ResolvedRef(
            kind=PROJECT, logical=format_ref(ref), absolute=absolute,
            read_only=False, exists=os.path.exists(absolute),
            is_dir=os.path.isdir(absolute))

    # A skill reference: resolvable only into a version pinned for this run.
    if skills is None:
        raise ResourceRefError(
            "incompatible_skill",
            f"{ref.skill_id} is not available to this run")
    directory = skills.directory(ref.skill_id)
    if directory is None:
        raise ResourceRefError(
            "incompatible_skill",
            f"resource {ref.relative!r} is not available: skill "
            f"{ref.skill_id} is not deployed for this run")
    if not os.path.isdir(directory):
        raise ResourceRefError(
            "incompatible_skill",
            f"resource {ref.relative!r} is not available: the deployed version of "
            f"{ref.skill_id} is no longer on this machine")
    absolute = _within(directory, ref.relative)
    if not os.path.exists(absolute):
        # Reported by name. Substituting a same-named project file (or the
        # server's copy) is the failure the spec calls out explicitly.
        raise ResourceRefError(
            "incompatible_skill",
            f"skill {ref.skill_id} does not provide {ref.relative!r} in its deployed "
            f"version; no same-named local or server file is substituted for it")
    return ResolvedRef(
        kind=SKILL, logical=format_ref(ref), absolute=absolute,
        read_only=True, exists=True, is_dir=os.path.isdir(absolute))


def ensure_readable(resolved: ResolvedRef) -> None:
    """Refuse a read that has no local path."""
    if resolved.absolute is None:
        raise ResourceRefError(
            "server_path_not_local", f"{resolved.logical} has no local path")
    if not os.path.exists(resolved.absolute):
        raise ResourceRefError(
            "missing_resource", f"{resolved.logical} does not exist locally")


def ensure_writable(resolved: ResolvedRef) -> None:
    """Refuse a write that would leave the project.

    The skill cache is read-only (spec: 技能缓存 SHALL 只读，项目产出不进入技能缓存),
    so this is the check that stops a business artefact -- or a script the model
    wrote -- from landing in the shared resource directory.
    """
    if resolved.absolute is None:
        raise ResourceRefError(
            "server_path_not_local", f"{resolved.logical} has no local path to write")
    if resolved.read_only:
        raise ResourceRefError(
            "skill_cache_read_only",
            f"{resolved.logical} is a skill resource; the skill cache is read-only "
            f"and project output must not be written into it")


def resolve_arguments(arguments: Any, *, project_root: str,
                      skills: Optional[RunSkillSet] = None) -> Dict[str, Any]:
    """Rewrite a call's path arguments to real local paths, or refuse the call.

    Only arguments named in :data:`PATH_ARGUMENTS` are considered, and only their
    values are replaced -- a ``bash`` command string is left exactly as written,
    because it is a program rather than a path and text-substituting it would be
    a string filter pretending to be isolation. A reference that cannot be
    resolved raises for the whole call: running the tool with the argument
    silently dropped would look like success.
    """
    prepared = dict(arguments or {})
    for key, raw in _iter_paths(prepared):
        resolved = resolve_ref(raw, project_root=project_root, skills=skills)
        _replace(prepared, key, raw, resolved.absolute or raw)
    return prepared


def _iter_paths(value: Any, key: str = "") -> List[tuple]:
    """``(key, text)`` for the path-shaped strings inside ``value``.

    Mirrors ``source_resolver._iter_path_like``: lists and nested mappings are
    walked, and only argument names in :data:`PATH_ARGUMENTS` count as paths.
    """
    found: List[tuple] = []
    if isinstance(value, str):
        if key in PATH_ARGUMENTS and value.strip():
            found.append((key, value.strip()))
        return found
    if isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iter_paths(item, key))
        return found
    if isinstance(value, Mapping):
        for name, item in value.items():
            found.extend(_iter_paths(item, str(name)))
    return found


def _replace(container: Dict[str, Any], key: str, old: str, new: str) -> bool:
    """Swap ``old`` for ``new`` where ``key`` holds it, leaving other keys alone."""
    swapped = False
    for name, value in list(container.items()):
        if str(name) == key and value == old:
            container[name] = new
            swapped = True
            continue
        new_value, hit = _replace_in(value, key, old, new)
        if hit:
            container[name] = new_value
            swapped = True
    return swapped


def _replace_in(value: Any, key: str, old: str, new: str):
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        replaced = False
        for name, item in value.items():
            if str(name) == key and item == old:
                out[name] = new
                replaced = True
                continue
            new_item, hit = _replace_in(item, key, old, new)
            out[name] = new_item
            replaced = replaced or hit
        return out, replaced
    if isinstance(value, list):
        items: List[Any] = []
        replaced = False
        for item in value:
            if key in PATH_ARGUMENTS and item == old:
                items.append(new)
                replaced = True
                continue
            new_item, hit = _replace_in(item, key, old, new)
            items.append(new_item)
            replaced = replaced or hit
        return items, replaced
    return value, False
