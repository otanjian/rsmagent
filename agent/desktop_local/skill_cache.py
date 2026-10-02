# encoding:utf-8
"""The device-side skill version cache (change tasks 8.2 / 8.3).

A remote skill is *code and data the user did not write*, arriving over the
network, that will later be read by a sandboxed process. So the cache does three
things and refuses to do anything else:

* **it verifies before it publishes.** A payload is published only if the
  manifest declares it, its size and digest match, its path cannot leave the
  package, and it is not a credential. A truncated or tampered byte string is
  refused rather than written -- a cache that trusts "the file arrived" has no
  way to tell a complete package from a partial one.
* **it publishes atomically.** Staging happens in a sibling directory and the
  version is moved into place with one ``os.replace``, so an interrupted
  transfer leaves either no version or the whole one. There is no state in which
  a half-written directory looks like a usable skill.
* **it addresses versions by digest, and counts references.** A version's
  directory is named by the digest of its own contents, so an upgrade lands
  *beside* the version a run is using instead of replacing it, and garbage
  collection can only reclaim what nothing holds. This is deliberately **not**
  the repository's existing ``sync_skills_to_workspace``, which does
  ``shutil.rmtree`` over the whole target and would delete a directory out from
  under a running skill.

Cache *presence* is never authorization. :meth:`SkillCache.resolve` and
:meth:`SkillCache.acquire` both consult a live ``authorized`` callback, so a
revoked ``skill.use`` stops working even though the bytes are still on disk --
the alternative (bytes on disk means permission) is exactly the bug the spec
calls out as 撤权后不能借缓存继续使用.

No Electron, no server imports: the device worker must stay runnable inside the
sandbox, so this module depends only on the standard library.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from agent.desktop_local.package_rules import (
    PackageRuleError,
    digest_of,
    refuse_secrets,
    safe_relative,
)

__all__ = [
    "SkillCache",
    "SkillCacheError",
    "SkillScope",
    "PublishedSkill",
    "DEFAULT_LIMITS",
]


#: Defaults mirror ``contracts/desktop/v2.json`` ``limits``. They are repeated
#: here rather than imported because the device worker must not pull in the
#: server package; ``test_desktop_skill_cache`` asserts they stay in step.
DEFAULT_LIMITS: Dict[str, int] = {
    "transfer_max_bytes": 67108864,      # skill_package_transfer_max_bytes
    "expanded_max_bytes": 268435456,     # skill_package_expanded_max_bytes
    "files_max": 10000,                  # skill_package_files_max
}

_SLUG_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class SkillCacheError(Exception):
    """A refused package operation, with a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SkillScope:
    """The isolation key of the cache: server, tenant and user.

    All three are part of the path, because the spec requires the cache to be
    scoped by 服务器、租户及用户. Two tenants that happen to use the same skill
    digest must not share a directory: the *content* may be identical while the
    authorization to read it is not.
    """

    origin: str
    tenant_id: str
    user_id: str


@dataclass(frozen=True)
class PublishedSkill:
    """A version that is now on disk, addressed by its own digest."""

    skill_id: str
    digest: str
    path: str


def _digest_of(payload: bytes) -> str:
    """Kept as a local name so the cache reads uniformly; one implementation."""
    return digest_of(payload)


def _component(value: Any, *, what: str) -> str:
    """One path segment for a scope component, safe and collision-free.

    Scope components arrive from the server, so they are *not* trusted to be
    well-formed: an origin is a URL and legitimately contains ``://`` and ``/``,
    while a tenant id is an identifier. Sanitising alone would be wrong in both
    directions -- it can collide (``https://a/b`` and ``https://a_b`` becoming
    one directory, silently mixing two servers' caches) and it cannot express a
    value that has no safe characters at all.

    So the component is a readable prefix *plus* a digest of the full value: the
    prefix is for a human reading the cache, and the digest is what makes the
    mapping injective and traversal-proof. Only an empty value is refused,
    because that means an unset identity and caching under it would mix
    identities together.
    """
    text = str(value or "").strip()
    if not text:
        raise SkillCacheError("invalid_scope", f"{what} is empty")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    # A URL's host is the readable part; for anything else take the value whole.
    readable = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", text)
    readable = re.split(r"[/\\?#]", readable, maxsplit=1)[0]
    prefix = _SLUG_SAFE.sub("_", readable).strip(".-")[:48]
    return f"{prefix}-{digest}" if prefix else digest


def _safe_relative(raw: Any) -> str:
    """The shared rule, translated into this module's error type."""
    try:
        return safe_relative(raw)
    except PackageRuleError as err:
        raise SkillCacheError(err.code, err.message) from None


def _refuse_secrets(relative: str) -> None:
    """The shared rule, translated into this module's error type."""
    try:
        refuse_secrets(relative)
    except PackageRuleError as err:
        raise SkillCacheError(err.code, err.message) from None


class SkillCache:
    """A per-installation cache of verified skill versions.

    :param root: the cache root. Everything this class writes stays inside it.
    :param limits: overrides for :data:`DEFAULT_LIMITS`.
    :param authorized: a *live* check, ``skill_id -> bool``. When supplied, both
        :meth:`resolve` and :meth:`acquire` consult it on every call, so revoking
        a skill takes effect immediately even though its bytes remain cached.
        Left as ``None`` only by callers that gate authorization themselves --
        the tests pass an explicit callback precisely so no test can accidentally
        rely on "the bytes are here".
    """

    def __init__(self, root: str, *,
                 limits: Optional[Dict[str, int]] = None,
                 authorized: Optional[Callable[[str], bool]] = None):
        self.root = os.path.abspath(root)
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self._authorized = authorized

    # -- layout ------------------------------------------------------------

    def scope_root(self, scope: SkillScope) -> str:
        """``<root>/<origin>/<tenant>/<user>``, every segment sanitised."""
        return os.path.join(
            self.root,
            _component(scope.origin, what="origin"),
            _component(scope.tenant_id, what="tenant id"),
            _component(scope.user_id, what="user id"),
        )

    def skill_dir(self, scope: SkillScope, skill_id: str) -> str:
        return os.path.join(self.scope_root(scope), "skills",
                            _component(skill_id, what="skill id"))

    def version_dir(self, scope: SkillScope, skill_id: str, digest: str) -> str:
        return os.path.join(self.skill_dir(scope, skill_id),
                            _component(digest, what="digest"))

    def _index_path(self, scope: SkillScope) -> str:
        return os.path.join(self.scope_root(scope), "index.json")

    # -- index -------------------------------------------------------------

    @staticmethod
    def _entry_key(skill_id: str, digest: str) -> str:
        return f"{skill_id}\u0000{digest}"

    def _load_index(self, scope: SkillScope) -> Dict[str, Any]:
        path = self._index_path(scope)
        if not os.path.exists(path):
            return {"versions": {}}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            # A corrupt index means refcounts are unknown. Guessing (treating
            # everything as unreferenced) would let gc delete a version a run is
            # using, so an unreadable index is treated as "nothing is known to
            # be reclaimable": the file is preserved for an operator and gc
            # declines to act.
            return {"versions": {}, "degraded": True}
        if not isinstance(raw, dict) or not isinstance(raw.get("versions"), dict):
            return {"versions": {}, "degraded": True}
        return raw

    def _save_index(self, scope: SkillScope, index: Dict[str, Any]) -> None:
        path = self._index_path(scope)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)

    # -- publish -----------------------------------------------------------

    def publish(self, scope: SkillScope, manifest: Dict[str, Any],
                payloads: Dict[str, bytes]) -> PublishedSkill:
        """Verify ``payloads`` against ``manifest`` and publish them atomically.

        The manifest is the *allowlist*: a payload it does not declare is refused
        rather than written (包外文件), and a declared resource that is missing is
        refused rather than published short (否则会只复制说明文件而假报技能可用).
        """
        skill_id = str(manifest.get("skill_id") or "").strip()
        if not skill_id:
            raise SkillCacheError("invalid_manifest", "manifest has no skill_id")
        digest = str(manifest.get("digest") or "").strip()
        if not digest:
            raise SkillCacheError("invalid_manifest", "manifest has no digest")
        declared = self._declared_resources(manifest)

        self._check_budgets(payloads)
        self._check_payloads(declared, payloads)

        target = self.version_dir(scope, skill_id, digest)
        if os.path.isdir(target):
            # Same digest means same content: the version is already published
            # and re-publishing must not rewrite it in place, because a run may
            # be reading it right now.
            self._record_published(scope, skill_id, digest, manifest)
            return PublishedSkill(skill_id=skill_id, digest=digest, path=target)

        staging = tempfile.mkdtemp(prefix=".partial-", dir=self._staging_dir(scope))
        try:
            for relative, body in payloads.items():
                destination = os.path.join(staging, *_safe_relative(relative).split("/"))
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with open(destination, "wb") as handle:
                    handle.write(body)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        self._record_published(scope, skill_id, digest, manifest)
        return PublishedSkill(skill_id=skill_id, digest=digest, path=target)

    def _staging_dir(self, scope: SkillScope) -> str:
        path = os.path.join(self.scope_root(scope), "staging")
        os.makedirs(path, exist_ok=True)
        return path

    def _declared_resources(self, manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        raw = manifest.get("resources")
        if not isinstance(raw, list) or not raw:
            raise SkillCacheError("invalid_manifest", "manifest declares no resources")
        out: Dict[str, Dict[str, Any]] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                raise SkillCacheError("invalid_manifest", "resource is not an object")
            relative = _safe_relative(entry.get("relative_path"))
            _refuse_secrets(relative)
            if relative in out:
                raise SkillCacheError(
                    "invalid_manifest", f"{relative!r} is declared twice")
            declared_digest = entry.get("digest")
            if not isinstance(declared_digest, str) or not declared_digest:
                raise SkillCacheError(
                    "invalid_manifest", f"{relative!r} has no digest")
            try:
                size = int(entry.get("size"))
            except (TypeError, ValueError):
                raise SkillCacheError(
                    "invalid_manifest", f"{relative!r} has no size")
            out[relative] = {"digest": declared_digest, "size": size}
        return out

    def _check_budgets(self, payloads: Dict[str, bytes]) -> None:
        transfer = sum(len(body) for body in payloads.values())
        if transfer > self.limits["transfer_max_bytes"]:
            raise SkillCacheError(
                "budget_exceeded",
                f"{transfer} bytes transferred exceeds "
                f"{self.limits['transfer_max_bytes']}")
        if len(payloads) > self.limits["files_max"]:
            raise SkillCacheError(
                "budget_exceeded",
                f"{len(payloads)} files exceeds {self.limits['files_max']}")
        # Expanded size is what unpacking would cost. These payloads are plain
        # files (an archive would be expanded by the caller before reaching
        # here), so expanded == transferred today; the check is kept separate so
        # an archive path cannot silently bypass the larger budget.
        expanded = transfer
        if expanded > self.limits["expanded_max_bytes"]:
            raise SkillCacheError(
                "budget_exceeded",
                f"{expanded} bytes expanded exceeds "
                f"{self.limits['expanded_max_bytes']}")

    def _check_payloads(self, declared: Dict[str, Dict[str, Any]],
                        payloads: Dict[str, bytes]) -> None:
        for relative, body in payloads.items():
            normalized = _safe_relative(relative)
            _refuse_secrets(normalized)
            if normalized not in declared:
                raise SkillCacheError(
                    "undeclared_file",
                    f"{normalized!r} is not declared by the manifest")
            expected = declared[normalized]
            if not isinstance(body, (bytes, bytearray)):
                raise SkillCacheError(
                    "invalid_payload", f"{normalized!r} is not bytes")
            if len(body) != expected["size"]:
                raise SkillCacheError(
                    "size_mismatch",
                    f"{normalized!r} is {len(body)} bytes, manifest says "
                    f"{expected['size']}")
            if _digest_of(bytes(body)) != expected["digest"]:
                raise SkillCacheError(
                    "digest_mismatch",
                    f"{normalized!r} does not match the manifest digest")
        missing = sorted(set(declared) - set(payloads))
        if missing:
            raise SkillCacheError(
                "missing_file",
                "manifest declares files that were not delivered: "
                + ", ".join(missing))

    def _record_published(self, scope: SkillScope, skill_id: str, digest: str,
                          manifest: Dict[str, Any]) -> None:
        index = self._load_index(scope)
        key = self._entry_key(skill_id, digest)
        entry = index["versions"].get(key) or {}
        index["versions"][key] = {
            "skill_id": skill_id,
            "digest": digest,
            "resources": [
                {"relative_path": r["relative_path"], "digest": r["digest"],
                 "size": r["size"]}
                for r in manifest.get("resources", [])
            ],
            # A re-publish must not reset a live reference count.
            "refcount": int(entry.get("refcount", 0) or 0),
            "platform": manifest.get("platform"),
            "dependencies": list(manifest.get("dependencies") or []),
        }
        self._save_index(scope, index)

    # -- read --------------------------------------------------------------

    def list_versions(self, scope: SkillScope, skill_id: str) -> List[str]:
        index = self._load_index(scope)
        return sorted(
            entry["digest"] for key, entry in index["versions"].items()
            if entry.get("skill_id") == skill_id
        )

    def refcount(self, scope: SkillScope, skill_id: str, digest: str) -> int:
        entry = self._load_index(scope)["versions"].get(
            self._entry_key(skill_id, digest))
        return int(entry.get("refcount", 0) or 0) if entry else 0

    def verify(self, scope: SkillScope, skill_id: str, digest: str) -> bool:
        """Whether a published version is still usable.

        A predicate, so a caller asking "may I use this?" gets a yes or a no
        rather than having to catch. :meth:`acquire` asks the same question but
        needs to say *why* it said no, so both go through
        :meth:`_verify_or_raise`.
        """
        try:
            self._verify_or_raise(scope, skill_id, digest)
        except SkillCacheError:
            return False
        return True

    def _verify_or_raise(self, scope: SkillScope, skill_id: str,
                         digest: str) -> None:
        """Re-check a published version against its own recorded manifest.

        Called before a version is handed to a run: the bytes were verified when
        they arrived, and this is the check that they are still *those* bytes.
        A link anywhere inside is refused, because a link is the one entry that
        can point outside the version it appears to be part of -- the directory
        can be complete, correctly hashed and still hand a run ``/etc/passwd``.
        """
        directory = self.version_dir(scope, skill_id, digest)
        entry = self._load_index(scope)["versions"].get(
            self._entry_key(skill_id, digest))
        if not entry or not os.path.isdir(directory):
            raise SkillCacheError(
                "not_published", f"{skill_id}@{digest} is not a cached version")
        try:
            for root, dirs, files in os.walk(directory):
                for name in list(dirs) + list(files):
                    if os.path.islink(os.path.join(root, name)):
                        raise SkillCacheError("link_refused", f"{name!r} is a link")
            for resource in entry.get("resources", []):
                relative = _safe_relative(resource.get("relative_path"))
                full = os.path.join(directory, *relative.split("/"))
                if not os.path.isfile(full):
                    raise SkillCacheError(
                        "not_published", f"{relative!r} is missing from the version")
                with open(full, "rb") as handle:
                    if _digest_of(handle.read()) != resource.get("digest"):
                        raise SkillCacheError(
                            "digest_mismatch",
                            f"{relative!r} no longer matches its manifest digest")
        except SkillCacheError:
            raise
        except OSError as err:
            raise SkillCacheError(
                "not_published", f"the version could not be read: {err}")

    # -- references --------------------------------------------------------

    def _authorization_gate(self, skill_id: str) -> None:
        if self._authorized is None:
            return
        if not self._authorized(skill_id):
            raise SkillCacheError(
                "not_authorized",
                f"{skill_id} is no longer authorized; the cached copy is not a grant")

    def resolve(self, scope: SkillScope, skill_id: str, digest: str, *,
                authorized: Optional[Callable[[str], bool]] = None) -> Optional[str]:
        """The path to a cached version, or ``None``.

        ``None`` -- never a substitute. A caller that cannot have exactly the
        version it asked for must report that, because silently running an older
        or different version is how a revoked grant becomes a working one.
        ``authorized`` defaults to the cache's own check.
        """
        check = authorized if authorized is not None else self._authorized
        if check is not None and not check(skill_id):
            return None
        directory = self.version_dir(scope, skill_id, digest)
        if not os.path.isdir(directory):
            return None
        return directory

    def acquire(self, scope: SkillScope, skill_id: str, digest: str, *,
                authorized: Optional[Callable[[str], bool]] = None) -> str:
        """Pin a version for a run and return its path.

        The pin is what makes "a run keeps the version it started with" true:
        while it is held, :meth:`gc` may not reclaim this directory even after an
        upgrade has published a newer one.
        """
        check = authorized if authorized is not None else self._authorized
        if check is not None and not check(skill_id):
            raise SkillCacheError(
                "not_authorized",
                f"{skill_id} is no longer authorized; the cached copy is not a grant")
        # The specific reason matters to the caller ("a link was found" is
        # actionable; "not published" is not), so the raising form is used.
        self._verify_or_raise(scope, skill_id, digest)
        index = self._load_index(scope)
        key = self._entry_key(skill_id, digest)
        entry = index["versions"][key]
        entry["refcount"] = int(entry.get("refcount", 0) or 0) + 1
        self._save_index(scope, index)
        return self.version_dir(scope, skill_id, digest)

    def release(self, scope: SkillScope, skill_id: str, digest: str) -> int:
        """Drop one reference. Never goes below zero."""
        index = self._load_index(scope)
        key = self._entry_key(skill_id, digest)
        entry = index["versions"].get(key)
        if not entry:
            return 0
        entry["refcount"] = max(0, int(entry.get("refcount", 0) or 0) - 1)
        self._save_index(scope, index)
        return entry["refcount"]

    # -- retention ---------------------------------------------------------

    def gc(self, scope: SkillScope, *, protect: Iterable[str] = ()) -> List[str]:
        """Reclaim versions nothing references, and only those.

        ``protect`` names digests that must survive this pass regardless of the
        recorded count -- the caller's own in-flight runs -- so a mis-recorded
        count cannot cost a run its resources.
        """
        protected = set(protect)
        index = self._load_index(scope)
        if index.get("degraded"):
            # The counts are unknown, so nothing is provably unreferenced.
            return []
        removed: List[str] = []
        for key in list(index["versions"]):
            entry = index["versions"][key]
            if int(entry.get("refcount", 0) or 0) > 0:
                continue
            if entry.get("digest") in protected:
                continue
            directory = self.version_dir(
                scope, entry["skill_id"], entry["digest"])
            # Only after the count says nothing holds it.
            if os.path.isdir(directory):
                shutil.rmtree(directory, ignore_errors=True)
            index["versions"].pop(key)
            removed.append(entry["digest"])
        if removed:
            self._save_index(scope, index)
        return sorted(removed)
