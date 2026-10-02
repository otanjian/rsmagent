"""One answer to "where does this reference live?" (change task 3.6).

The file panel, an `@` reference in a message, and the arguments a tool is about
to run with are three surfaces of the same question, and before this module each
answered it on its own -- always against the *server's* idea of the working
directory. With a local project open that is wrong in a specific direction:

* an `@` reference or a tool argument whose relative path also exists under the
  server directory would be read from the server, and
* a reference the server cannot see would be copied up so the server could read
  it -- the "无条件上传" the requirement forbids.

So the requirement ("文件面板、附件工作区引用及 `@` SHALL 使用同一来源；本机引用
不经过服务器普通路径解析或无条件上传") is implemented as one resolver with four
rules:

1. **One source per session.** :func:`source_for_session` and
   :func:`source_for_identity` ask the same trusted registry and the same
   re-authorization rule the run boundary asks
   (``agent.desktop_local.run_context.resolve_target_root``), so "the panel
   listed X" and "the tool read X" cannot disagree. No desktop target means the
   pre-existing server behaviour, untouched.
2. **A desktop source that did not resolve is a refusal, not a fallback.**
   Nothing here returns the server root for a session that is bound to a local
   project: the caller either gets ``source.available`` or a reason to report.
3. **A local reference is never a server path.** Absolute paths are refused
   under a desktop source instead of being parsed, and the only paths resolved
   are relative ids inside the registered root (containment re-checked against
   the real path, so a symlinked component cannot escape it).
4. **Landing is explicit.** :func:`prepare_tool_inputs` refuses a server-side
   input a local run cannot see, naming it, unless the caller injects a landing
   transport. This module never uploads, and never turns "the server has it"
   into "the device has it".

Nothing here is persisted, and no host path is ever derived from a request: the
only absolute paths that exist below come from the trusted registry, and
:meth:`Source.describe` -- the shape a response may carry -- holds identifiers
and a refusal reason, never a directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from common.log import logger
from common import safe_fs


SERVER = "server"
DESKTOP = "desktop"

#: Argument names whose values name a path a tool is about to open. Kept in step
#: with ``agent.desktop_local.worker._PATH_ARGUMENTS`` -- the worker's own guard
#: is the last line, this is the one that can still *explain* the problem.
PATH_ARGUMENTS = ("path", "paths", "file_path", "directory", "dir")

#: Labels for the text marker a reference becomes in the turn.
FILE_LABEL = ("本机项目文件", "Local project file")
DIR_LABEL = ("本机项目目录", "Local project directory")
REFUSED_LABEL = ("本机引用未生效", "Local reference not applied")

REFUSAL_EMPTY = "引用没有给出路径，因此没有生效。"
REFUSAL_SERVER_PATH = (
    "本机项目只接受项目内相对引用，不接受服务器绝对路径（{raw}）。"
    "这次引用没有生效，也没有把它当成服务器文件去读取或上传。"
)
REFUSAL_ESCAPES = (
    "该引用超出了当前本机项目的目录范围（或包含非法分量），已拒绝；"
    "没有改用服务器目录。"
)
REFUSAL_REF_GONE = "引用的内容在当前本机项目中不存在：{relative}"
REFUSAL_NO_LANDING = (
    "本机运行读不到服务器路径（{raw}）：它在本机项目里不存在，也没有自动上传或复制。"
    "如果本机任务确实需要这份输入，请先通过获权传输把它落地，或改为引用本机项目内的文件。"
)
REFUSAL_TRANSFER_OUTSIDE = (
    "落地后的输入（{landed}）不在当前本机项目内，已拒绝，没有把项目外的路径交给本机工具。"
)
#: A ``resource:`` reference with no landing transport available. Distinct from
#: ``REFUSAL_NO_LANDING`` because the answer differs: a server *path* might be a
#: file the project could have, while a pinned ``resource:`` explicitly says the
#: bytes live on the server and must be landed first (task 8.6).
REFUSAL_RESOURCE_NOT_LANDED = (
    "本机运行读不到服务器资源（{raw}）：它还没有被显式落地到本轮输入目录，"
    "也没有被当成项目内路径去读取。"
)


def _label(pair: Tuple[str, str]) -> str:
    from common import i18n

    return i18n.t(pair[0], pair[1])


def _is_desktop_target(target: Any) -> bool:
    return bool(target is not None and getattr(target, "is_desktop", False))


def clean_relative(raw: Any) -> Optional[str]:
    """Normalize a relative reference id, or ``None`` when it is not one.

    Refuses ``..``, ``.``, empty components, absolute paths, drive letters and
    backslashes on every platform (the grammar the picker emits). ``""`` is the
    project root itself, and is returned as ``""`` rather than ``None``.
    """
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if text in ("", ".", "./"):
        return ""
    if "\\" in text:
        return None
    if re_drive(text):
        return None
    try:
        parts = safe_fs.split_relative(text)
    except safe_fs.UnsafePathError:
        return None
    return "/".join(parts)


def re_drive(text: str) -> bool:
    """Whether ``text`` starts with a Windows drive designator (``C:``)."""
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def _absolute_within(root: str, relative: str) -> Optional[str]:
    """``relative`` resolved under ``root``, or ``None`` when it escapes it.

    Containment is judged on the *real* path, so a component (or the target
    itself) swapped for a symlink pointing outside the project is refused rather
    than followed.
    """
    if not relative:
        candidate = root
    else:
        candidate = os.path.join(root, *relative.split("/"))
    real = os.path.realpath(candidate)
    if not safe_fs.contains(root, real):
        return None
    return real


@dataclass(frozen=True)
class Source:
    """Where one session's project files are, on this machine.

    ``kind`` is ``server`` or ``desktop``. ``root`` is an absolute directory and
    is *internal* -- it is never a response field (use :meth:`describe`).
    ``refusal`` is set (with ``root`` None) for a desktop session whose local
    project cannot be honoured here; callers report it and must not fall back.
    """

    kind: str = SERVER
    root: Optional[str] = None
    target: Any = None
    refusal: Optional[str] = None

    @property
    def is_desktop(self) -> bool:
        return self.kind == DESKTOP

    @property
    def available(self) -> bool:
        return self.root is not None and self.refusal is None

    def describe(self) -> Dict[str, Any]:
        """The response-shaped projection: kind, availability, reason. No path."""
        return {
            "kind": self.kind,
            "available": self.available,
            "refusal": self.refusal,
        }


def source_for_identity(identity: Any = None, *, server_root: Optional[str] = None) -> Source:
    """The source for the ambient (or given) run identity.

    A run identity carries its own frozen target and cwd, so the answer is the
    run's own -- a concurrent turn that re-points the session cannot change it.
    """
    if identity is None:
        from common.runtime_identity import current_identity

        identity = current_identity()
    target = getattr(identity, "execution_target", None)
    if not _is_desktop_target(target):
        return Source(kind=SERVER, root=server_root)

    from agent.desktop_local.run_context import run_local_cwd

    root, refusal = run_local_cwd(identity)
    return Source(kind=DESKTOP, root=root, target=target, refusal=refusal)


def source_for_session(session_id: str, agent_id: Optional[str] = None, *,
                       server_root: Optional[str] = None,
                       identity: Any = None) -> Source:
    """The source for a session the caller addressed (panel, `@`, tool staging).

    The target comes from the session's stored execution target -- identifiers
    only -- and is re-authorized against the trusted registry *now*, so a
    revoked grant or a re-picked directory is a refusal rather than a stale
    "the session's files are over there".
    """
    if identity is None:
        from common.runtime_identity import current_identity

        identity = current_identity()
    target = None
    if session_id:
        try:
            from agent.workspace import project_store

            target = project_store.get_execution_target(session_id, agent_id)
        except Exception as e:  # noqa: BLE001 - a bad record is "no target"
            logger.debug(f"[SourceResolver] execution target lookup failed: {e}")
            target = None
    if not _is_desktop_target(target):
        return Source(kind=SERVER, root=server_root)

    from agent.desktop_local.run_context import resolve_target_root

    root, refusal = resolve_target_root(target, identity)
    return Source(kind=DESKTOP, root=root, target=target, refusal=refusal)


@dataclass(frozen=True)
class Reference:
    """A resolved reference, or why it could not be resolved.

    ``relative`` is the canonical id inside the source (``""`` = the root).
    ``absolute`` is filled for a desktop source only -- the local path the tool
    and the panel act on -- and stays ``None`` for a server source, whose
    absolute-path rules belong to the server file surface, not here.
    """

    kind: str
    relative: Optional[str] = None
    absolute: Optional[str] = None
    is_dir: bool = False
    exists: bool = False
    refusal: Optional[str] = None


def resolve_reference(source: Source, raw: Any, *, allow_root: bool = False) -> Reference:
    """Resolve one reference against ``source`` -- or refuse it.

    The refusal cases are the requirement: under a desktop source an absolute
    path is refused *without being parsed* (no server path resolution stands in
    for a local one), an id that escapes the project is refused, and a missing
    entry is reported by name rather than by looking for a same-named server
    file. A server source keeps the caller's own rules: relative ids are
    normalized and checked for existence under the server root when one was
    given, and an absolute value is handed back untouched for the caller's
    existing (tenant-scoped) handling.

    ``allow_root`` admits the empty reference, which is how a file panel names
    the project root itself; an `@` reference (the default) may not be empty.
    """
    if source.kind == DESKTOP:
        if source.refusal:
            return Reference(DESKTOP, refusal=source.refusal)
        text = "" if raw is None else str(raw).strip()
        if not text:
            if not allow_root:
                return Reference(DESKTOP, refusal=REFUSAL_EMPTY)
            if not source.available:
                return Reference(DESKTOP, refusal=source.refusal or REFUSAL_EMPTY)
            return Reference(DESKTOP, relative="", absolute=source.root,
                             is_dir=True, exists=True)
        if os.path.isabs(text) or text.startswith(("\\", "~")):
            # A local reference may not be an absolute path at all: honoring one
            # would mean parsing a server-shaped path for a local project.
            return Reference(DESKTOP, refusal=REFUSAL_SERVER_PATH.format(raw=text))
        relative = clean_relative(text)
        if relative is None:
            return Reference(DESKTOP, refusal=REFUSAL_ESCAPES)
        absolute = _absolute_within(source.root or "", relative)
        if absolute is None:
            return Reference(DESKTOP, refusal=REFUSAL_ESCAPES)
        exists = os.path.exists(absolute)
        if not exists:
            return Reference(
                DESKTOP, relative=relative, absolute=absolute,
                refusal=REFUSAL_REF_GONE.format(relative=relative or "."))
        return Reference(DESKTOP, relative=relative, absolute=absolute,
                         is_dir=os.path.isdir(absolute), exists=True)

    # Server source: the pre-existing behaviour, with relative ids normalized
    # the same way so both branches of every caller agree on what an id is.
    text = "" if raw is None else str(raw).strip()
    if os.path.isabs(text):
        # Absolute paths are the server file surface's business
        # (``_is_path_allowed`` / the tenant-root checks); this module must not
        # become a second, weaker copy of that rule. Existence is only read to
        # label the marker the way this path was always labelled.
        return Reference(SERVER, absolute=text, is_dir=os.path.isdir(text),
                         exists=os.path.exists(text))
    relative = clean_relative(text)
    if relative is None:
        return Reference(SERVER, refusal=REFUSAL_ESCAPES)
    if source.root and relative:
        absolute = _absolute_within(source.root, relative)
        if absolute is None:
            return Reference(SERVER, refusal=REFUSAL_ESCAPES)
        exists = os.path.exists(absolute)
        return Reference(SERVER, relative=relative, absolute=absolute,
                         is_dir=os.path.isdir(absolute), exists=exists)
    return Reference(SERVER, relative=relative)


def reference_line(source: Source, raw: Any, *,
                   file_label: Optional[str] = None,
                   dir_label: Optional[str] = None,
                   refused_label: Optional[str] = None) -> str:
    """The text marker one reference becomes in the turn's prompt.

    Server references keep their existing labels, so nothing about a session
    without a local project changes. A desktop reference says it is local, which
    is what tells the model to read it through the local project tools instead
    of asking the server for a copy; a refused reference becomes a notice saying
    it did not apply (and that nothing was uploaded), never a silent drop.
    """
    resolved = resolve_reference(source, raw)
    if resolved.refusal:
        return f"[{refused_label or _label(REFUSED_LABEL)}: {resolved.refusal}]"
    shown = resolved.relative if resolved.relative is not None else (resolved.absolute or "")
    if source.kind == DESKTOP:
        label = (dir_label or _label(DIR_LABEL)) if resolved.is_dir \
            else (file_label or _label(FILE_LABEL))
    else:
        from common import i18n

        label = i18n.t("工作空间目录", "Workspace directory") if resolved.is_dir \
            else i18n.t("工作空间文件", "Workspace file")
    return f"[{label}: {shown}]"


@dataclass(frozen=True)
class ToolInputs:
    """Prepared tool arguments, plus the inputs that could not be prepared."""

    arguments: Dict[str, Any] = field(default_factory=dict)
    refusals: List[str] = field(default_factory=list)
    rewrites: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refusals

    def message(self) -> str:
        return "\n".join(self.refusals)


def _iter_path_like(value: Any, key: str = "") -> List[Tuple[str, str]]:
    """``(key, path)`` pairs for the path-shaped strings inside ``value``.

    Walks lists and dicts because a tool may take a list of paths (or nested
    parameters). Only argument names in :data:`PATH_ARGUMENTS` are considered:
    a ``bash`` command string can contain any path at all, and rewriting prose
    would be worse than the worker's own guard refusing it.
    """
    found: List[Tuple[str, str]] = []
    if isinstance(value, str):
        if key in PATH_ARGUMENTS and value.strip():
            found.append((key, value.strip()))
        return found
    if isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iter_path_like(item, key))
        return found
    if isinstance(value, Mapping):
        for name, item in value.items():
            found.extend(_iter_path_like(item, str(name)))
    return found


def iter_path_arguments(value: Any) -> List[Tuple[str, str]]:
    """Public alias of :func:`_iter_path_like`.

    The remote (device-delegated) branch of the same change stages path
    arguments too, and it must agree with this side about *which* argument names
    count as paths -- two walks would drift, and a drifted walk silently changes
    what gets refused.
    """
    return _iter_path_like(value)


def replace_path_argument(container: Dict[str, Any], key: str, old: str,
                          new: str) -> bool:
    """Public alias of :func:`_replace_path`, for the same reason as above.

    Discovery (:func:`iter_path_arguments`) and rewriting must not disagree
    about where a path argument lives, and the remote branch rewrites too.
    """
    return _replace_path(container, key, old, new)


def prepare_tool_inputs(source: Source, tool_name: str, arguments: Any, *,
                        transfer: Optional[Callable[[str], Optional[str]]] = None,
                        skills: Any = None,
                        landing: Any = None) -> ToolInputs:
    """Stage a call's path arguments for a local run, or refuse the call.

    Three outcomes per path argument, and none of them uploads anything:

    * **already local** (a relative id inside the project) -- kept, with its
      containment verified so an escaping id is refused before any tool runs;
    * **an absolute path inside the project** -- rewritten to its relative form,
      so the tool addresses the project the same way the panel does;
    * **a path this machine cannot see** (typically a server-side upload or a
      fetched file) -- refused by name, unless the caller injected ``transfer``
      (the explicit, permissioned landing transport) and it hands back a
      directory inside the project.

    ``skills`` (a ``RunSkillSet``, task 8.4) adds the *typed* forms: a ``skill:``
    reference resolves into the read-only version pinned for this run, and a
    ``backend:`` reference is refused because a server path has no local
    equivalent. When ``skills`` is ``None`` a ``skill:`` reference is still
    refused by name rather than being read as a project path -- treating
    ``skill:builtin:excel/templates/x`` as a project-relative path would look for
    a directory literally called ``skill:builtin:excel``, which is how a missing
    skill turns into a confusing "file not found" instead of "the skill is not
    deployed".

    ``landing`` (a ``ResourceLanding``, task 8.6) adds the *server resource* form:
    a ``resource:<id>@<version>#<digest>`` reference is fetched, **verified** and
    published into the run's input directory, and the argument is rewritten to
    that absolute path. This is the only branch that yields a path outside the
    project, and deliberately so: the contract says a server attachment lands in
    the run's temporary input directory ("客户端先下载到本轮临时输入目录并校验")，
    which the sandbox already grants as readable. A relative id cannot express
    that location, so the rewrite is absolute here and project-relative elsewhere.
    Without a ``landing``, the reference is refused by name -- never resolved as a
    project path, and never uploaded.

    A refused call is reported whole rather than partly executed: running the
    tool with the argument silently dropped would look like success.
    """
    from agent.desktop_local import resource_refs

    prepared = dict(arguments or {})
    refusals: List[str] = []
    rewrites: List[Tuple[str, str]] = []

    for key, raw in _iter_path_like(prepared):
        if source.kind != DESKTOP:
            # A server source keeps its own resolution path and refusals: an
            # absolute path stays the server file surface's business, and a
            # relative one is only normalized.
            reference = resolve_reference(source, raw)
            if reference.refusal:
                refusals.append(reference.refusal)
            elif reference.relative is not None and reference.relative != raw:
                _replace_path(prepared, key, raw, reference.relative)
                rewrites.append((raw, reference.relative))
            continue

        if source.refusal:
            refusals.append(source.refusal)
            break

        root = source.root or ""
        original = raw

        # Typed references come first: they say which root they mean, which a
        # bare relative path cannot.
        try:
            parsed = resource_refs.parse_ref(raw)
        except resource_refs.ResourceRefError as err:
            refusals.append(err.message)
            continue

        if parsed is not None and parsed.kind == resource_refs.RESOURCE:
            # A pinned server resource (task 8.6). It has no local path until an
            # explicit, verified landing puts one there, so this is the one branch
            # that may hand a tool a path *outside* the project: the run's own
            # input directory, which the sandbox already grants as readable. A
            # relative id could not express that path, which is why the rewrite is
            # absolute here and project-relative everywhere else.
            if landing is None:
                refusals.append(REFUSAL_RESOURCE_NOT_LANDED.format(raw=original))
                continue
            from agent.desktop_local import resource_landing

            result = landing.land(resource_landing.LandingRequest(
                resource_id=parsed.relative,
                version=parsed.version,
                digest=parsed.digest,
            ))
            if not result.ok:
                refusals.append(result.message)
                continue
            _replace_path(prepared, key, original, result.absolute)
            rewrites.append((original, result.absolute))
            continue

        if parsed is not None and parsed.kind in (
                resource_refs.SKILL, resource_refs.BACKEND):
            try:
                resolved = resource_refs.resolve_ref(
                    raw, project_root=root, skills=skills)
            except resource_refs.ResourceRefError as err:
                refusals.append(err.message)
                continue
            if resolved.absolute and resolved.absolute != original:
                _replace_path(prepared, key, original, resolved.absolute)
                rewrites.append((original, resolved.absolute))
            continue

        if parsed is not None:
            # A project reference: the existing rules, on the resource part.
            raw = parsed.relative

        if os.path.isabs(raw) or raw.startswith(("\\", "~")):
            # An absolute path *inside* the project is the same file the panel
            # would show: rewrite it to its project-relative form so the tool
            # addresses the project the same way every other surface does.
            inside = _inside_project(root, raw)
            if inside is not None:
                _replace_path(prepared, key, raw, inside)
                rewrites.append((raw, inside))
                continue
            # Otherwise this is the "远程输入未落地" case: a path this machine
            # cannot see. Landing is an explicit capability, never something this
            # function does on its own.
            land = _land(source, raw, transfer)
            if land is None:
                refusals.append(REFUSAL_NO_LANDING.format(raw=raw))
                continue
            _replace_path(prepared, key, raw, land)
            rewrites.append((raw, land))
            continue

        relative = clean_relative(raw)
        if relative is None or _absolute_within(root, relative) is None:
            # Escaping the project (or an illegal id) is refused before the tool
            # runs. Existence is deliberately *not* checked here: a `write` to a
            # file that does not exist yet is an ordinary, correct call.
            refusals.append(REFUSAL_ESCAPES)
            continue
        if relative != original:
            # ``original``, not ``raw``: a ``project:`` reference was normalized
            # above, so the value actually sitting in the arguments is the
            # untouched original and rewriting by any other name would miss it.
            _replace_path(prepared, key, original, relative)
            rewrites.append((original, relative))

    return ToolInputs(arguments=prepared, refusals=refusals, rewrites=rewrites)


def _land(source: Source, raw: str, transfer: Optional[Callable[[str], Optional[str]]]
          ) -> Optional[str]:
    """Ask the injected landing transport for a local path, and validate it.

    ``None`` (no transport, a refusal, an exception, or a path outside the
    project) means "not landed" -- the caller refuses the call. The returned
    value is relative to the project, which is what a local tool argument has to
    be.
    """
    if transfer is None:
        return None
    try:
        landed = transfer(raw)
    except Exception as e:  # noqa: BLE001 - a failed landing is a refusal
        logger.warning(f"[SourceResolver] landing {raw!r} failed: {e}")
        return None
    if not landed:
        return None
    root = source.root or ""
    text = str(landed).strip()
    if os.path.isabs(text):
        if not safe_fs.contains(root, text):
            logger.info(f"[SourceResolver] landed input outside the project refused: {text!r}")
            return None
        relative = os.path.relpath(os.path.realpath(text),
                                   os.path.realpath(root)).replace(os.sep, "/")
        if relative in (".", ""):
            return None
    else:
        relative = clean_relative(text)
        if relative is None:
            return None
    if _absolute_within(root, relative) is None:
        logger.info(f"[SourceResolver] landed input outside the project refused: {landed!r}")
        return None
    return relative


def _inside_project(root: str, raw: str) -> Optional[str]:
    """``raw`` as a project-relative id when it already names a project entry.

    Used so an absolute path the *device* handed the model (a tool result, a
    local artefact) is addressed the same way the panel addresses it, instead of
    being mistaken for a server path. Returns ``None`` when the path is not a
    project entry on this machine.
    """
    if not root or not os.path.isabs(raw):
        return None
    if not safe_fs.contains(root, raw):
        return None
    real = os.path.realpath(raw)
    reference = safe_fs.contains(root, real)
    relative = os.path.relpath(real, os.path.realpath(root)).replace(os.sep, "/")
    if relative in (".", "") or not reference:
        return None
    entry = clean_relative(relative)
    if entry is None:
        return None
    return entry if _absolute_within(root, entry) is not None else None


def _rewrite(value: Any, key: str, old: str, new: str) -> Tuple[Any, bool]:
    """``(value', replaced)``: swap ``old`` for ``new`` where ``key`` holds it.

    Mirrors :func:`_iter_path_like`, so discovery and rewriting cannot disagree
    about where a path argument lives.
    """
    replaced = False
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        for name, item in value.items():
            if str(name) == key and item == old:
                out[name] = new
                replaced = True
                continue
            new_item, hit = _rewrite(item, key, old, new)
            out[name] = new_item
            replaced = replaced or hit
        return out, replaced
    if isinstance(value, list):
        items: List[Any] = []
        for item in value:
            if key in PATH_ARGUMENTS and item == old:
                items.append(new)
                replaced = True
                continue
            new_item, hit = _rewrite(item, key, old, new)
            items.append(new_item)
            replaced = replaced or hit
        return items, replaced
    return value, False


def _replace_path(container: Dict[str, Any], key: str, old: str, new: str) -> bool:
    """Swap ``old`` for ``new`` inside ``container``, leaving other keys alone.

    A value that cannot be located is left untouched: the caller then still has
    the verified (unrewritten) argument rather than a corrupt one.
    """
    swapped = False
    for name, value in list(container.items()):
        if str(name) == key and value == old:
            container[name] = new
            swapped = True
            continue
        new_value, hit = _rewrite(value, key, old, new)
        if hit:
            container[name] = new_value
            swapped = True
    return swapped
