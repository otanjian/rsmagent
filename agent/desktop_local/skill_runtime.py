# encoding:utf-8
"""Deploy a run's authorized skills into the read-only cache (tasks 8.1–8.4).

This is the piece that makes 8.1–8.3 *reachable*. Without it the manifest
builder and the version cache are libraries nothing calls: a skill reference
would either resolve to nothing or -- worse -- resolve to whatever directory the
server happens to keep skills in, which is the "server absolute path handed to a
local script" the spec forbids.

In local mode the client machine and the Python backend are the same machine, so
"deploy to the device" means "publish the already-selected skill into the local
cache". The order matters and is the whole point:

1. **Select** with the existing :class:`~agent.skills.manager.SkillManager`. No
   new selection logic: whatever the manager would have offered the model is what
   gets deployed, so `skill.use`, enable/disable and requirement checks keep
   working exactly as they did.
2. **Build a manifest** per skill (:mod:`agent.skills.manifest`), with the
   *device's* platform and a live authorization check.
3. **Publish** it into the version cache (:mod:`agent.desktop_local.skill_cache`),
   which verifies the bytes and refuses secrets, escaping paths and undeclared
   files before anything is usable.
4. **Pin** the published versions for the run, so a later upgrade lands beside
   them rather than replacing them, and garbage collection cannot take them.

Then the run's tools resolve ``skill:`` references into the pinned directories,
which the sandbox grants read-only.

A skill that cannot be deployed is **skipped with a reason**, not silently
dropped and not faked: a run that asked for a skill it cannot have must be able
to say which one and why (spec: 缺失依赖、平台不兼容和运行时不可用 SHALL 有明确错误).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from common.log import logger
from agent.desktop_local.resource_refs import ResourceRefError, RunSkillSet
from agent.desktop_local.skill_cache import SkillCache, SkillCacheError, SkillScope

__all__ = ["LocalSkillRuntime", "SkillDeployment", "SkillDeploymentProblem"]


@dataclass(frozen=True)
class SkillDeployment:
    """One skill that was deployed and pinned for this run."""

    skill_id: str
    name: str
    digest: str
    path: str
    resource_count: int


@dataclass(frozen=True)
class SkillDeploymentProblem:
    """One authorized skill that could not be deployed, and why.

    Reported rather than raised: one broken skill must not take down a run that
    can still do useful work, but it must not disappear either -- a model that
    silently lost a bound skill produces a confidently wrong answer.
    """

    skill_id: str
    code: str
    message: str


@dataclass
class LocalSkillRuntime:
    """The skills one local run may use, deployed and pinned.

    Use as a context manager, or call :meth:`release` in a ``finally``: the pin
    is what keeps the version alive against collection, so a run that forgets to
    release leaks a version per run and the cache only grows.
    """

    manager: Any
    cache: SkillCache
    scope: SkillScope
    platform: str = "posix"
    #: Optional ``shutil.which``-like probe, for reporting missing binaries.
    which: Optional[Any] = None
    deployments: List[SkillDeployment] = field(default_factory=list)
    problems: List[SkillDeploymentProblem] = field(default_factory=list)
    _skills: Optional[RunSkillSet] = None

    def prepare(self, *, skill_filter: Optional[List[str]] = None) -> RunSkillSet:
        """Select, deploy and pin; never raises for one bad skill.

        Idempotent when called again without a filter: the same pinned set is
        returned rather than redeployed. Redeploying would build a *second*
        :class:`RunSkillSet` and abandon the first one's pins, so the cache count
        for every skill would climb by one per call and never come back down --
        the version could then never be collected.
        """
        if self._skills is not None:
            if skill_filter is None:
                return self._skills
            # A different selection needs a different set; let go of the old pins
            # first so no version is held by two sets at once.
            self.release()

        self.deployments = []
        self.problems = []
        entries = self.manager.filter_skills(skill_filter=skill_filter)

        # Built before pinning so a mid-way failure does not leave half a run's
        # skills held.
        skills = RunSkillSet(self.cache, self.scope, authorized=self._authorize)
        for entry in entries:
            self._deploy_one(entry, skills)
        self._skills = skills
        return skills

    def _authorize(self, skill_id: str) -> bool:
        """The manager's live ``skill.use`` answer, asked per deployment attempt."""
        check = getattr(self.manager, "is_authorized", None)
        if check is None:
            # A manager without the check (an older/stand-in object) is treated as
            # unrestricted, which matches the legacy mode the manager itself
            # reports for "no identity".
            return True
        try:
            return bool(check(skill_id))
        except Exception as e:  # noqa: BLE001 - an unanswerable check is a refusal
            logger.warning(f"[LocalSkillRuntime] authorization check failed: {e}")
            return False

    def _deploy_one(self, entry: Any, skills: RunSkillSet) -> None:
        from agent.skills.manifest import (
            SkillManifestError, build_skill_manifest, read_skill_payloads,
        )

        name = getattr(entry.skill, "name", "") or ""
        try:
            manifest = build_skill_manifest(
                entry, platform=self.platform, is_authorized=self._authorize,
                which=self.which)
        except SkillManifestError as err:
            self._skip(name or err.message, err.code, err.message)
            return
        except Exception as e:  # noqa: BLE001 - one skill must not fail the run
            self._skip(name, "manifest_failed", str(e))
            return

        try:
            payloads = read_skill_payloads(manifest)
            published = self.cache.publish(self.scope, manifest.to_dict(), payloads)
            skills.pin(manifest.skill_id, published.digest)
        except (SkillCacheError, ResourceRefError, SkillManifestError) as err:
            self._skip(manifest.skill_id, getattr(err, "code", "deploy_failed"), str(err))
            return
        except Exception as e:  # noqa: BLE001 - one skill must not fail the run
            self._skip(manifest.skill_id, "deploy_failed", str(e))
            return

        self.deployments.append(SkillDeployment(
            skill_id=manifest.skill_id, name=manifest.name, digest=manifest.digest,
            path=published.path, resource_count=len(manifest.resources)))
        logger.info(
            f"[LocalSkillRuntime] deployed {manifest.skill_id}@{manifest.digest[:19]} "
            f"({len(manifest.resources)} resources)")

    def _skip(self, skill_id: str, code: str, message: str) -> None:
        self.problems.append(SkillDeploymentProblem(
            skill_id=skill_id, code=code, message=message))
        logger.warning(f"[LocalSkillRuntime] {skill_id} not deployed: {message}")

    @property
    def skills(self) -> RunSkillSet:
        """The pinned set. Empty (not ``None``) before :meth:`prepare` runs."""
        if self._skills is None:
            self._skills = RunSkillSet(self.cache, self.scope, authorized=self._authorize)
        return self._skills

    def roots(self) -> List[str]:
        """The read-only directories the sandbox should be granted."""
        return self.skills.roots()

    def pins(self) -> List[Dict[str, str]]:
        """The versions this run is pinned to, in canonical order (task 8.9).

        The portable counterpart of :meth:`roots`: a device that holds no cache
        and cannot see these paths is still told which versions the run
        authorized, and can refuse to run anything else.
        """
        return self.skills.pins()

    def failure_message(self) -> Optional[str]:
        """A model-facing summary of the skills that could not be deployed.

        Only the problems, and only the actionable part: "which skill, why", so
        the model reports a missing capability instead of quietly doing something
        else with a same-named file.
        """
        if not self.problems:
            return None
        lines = [
            f"技能 {p.skill_id} 在本机不可用（{p.code}）：{p.message}"
            for p in self.problems
        ]
        return "\n".join(lines)

    def release(self) -> None:
        """Drop every pin this run held."""
        if self._skills is not None:
            self._skills.release_all()

    def __enter__(self) -> "LocalSkillRuntime":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def default_cache_root(identity: Any = None) -> str:
    """Where the local skill cache lives: tenant/user state, not a shared path.

    Under the caller's own state root, so one user's cached skills are not
    readable as another's, and nothing global is created. The cache still adds
    its own server/tenant/user scoping inside (task 8.2).
    """
    from common import state_dir

    return str(state_dir.state_path("desktop", "skill-cache", identity=identity))


def runtime_for_identity(manager: Any, *, identity: Any = None,
                         platform: str = "posix", cache_root: Optional[str] = None,
                         which: Optional[Any] = None) -> LocalSkillRuntime:
    """A :class:`LocalSkillRuntime` for the ambient (or given) identity."""
    import os

    from common import state_dir

    ident = identity
    if ident is None:
        from common.runtime_identity import current_identity

        ident = current_identity()
    root = cache_root or default_cache_root(ident)
    origin = getattr(ident, "origin", None) or _instance_origin()
    scope = SkillScope(
        origin=str(origin),
        tenant_id=str(getattr(ident, "tenant_id", "") or "local"),
        user_id=str(getattr(ident, "user_id", "") or "local"),
    )
    os.makedirs(root, exist_ok=True)
    return LocalSkillRuntime(
        manager=manager, cache=SkillCache(root), scope=scope,
        platform=platform, which=which)


def _instance_origin() -> str:
    """A stable stand-in when the identity carries no origin.

    The cache scopes by origin so two servers cannot share a tenant's cache. With
    no origin recorded, the instance's own state root is the honest scope: it is
    the same server by construction, and it keeps the path from collapsing to a
    shared constant that would mix deployments.
    """
    try:
        from common import state_dir

        return str(state_dir.state_root())
    except Exception:  # noqa: BLE001 - a scope label must never fail a run
        return "local"
