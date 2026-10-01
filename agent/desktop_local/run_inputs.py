# encoding:utf-8
"""Authorized server run inputs as a landing source (task 8.6).

This is the "接通获权服务器附件" half of 8.6: the concrete thing behind a
``resource:<id>`` reference. A landing without a source is a mechanism with nothing
to land, and this is the source.

The server already has the pieces, and this module deliberately does not add a
second copy of any of them:

* ``client_files`` materializes a committed transfer into the Agent's user work dir
  (``<workspace>/user/<id>/work/desktop-inputs/<run_id>/<transfer_id>/<filename>``)
  and records a ``desktop_run_inputs`` row with ``artifact_rel`` and
  ``source_version``;

So the shape of a resource is already ``(id, source_version, path)``, and this
module is the thin adapter that turns it into the ``(version, digest, bytes)`` a
:class:`~agent.desktop_local.resource_landing.ResourceLanding` verifies.

``artifact_rel`` is resolved against the Agent's user work dir, because that is the
base ``client_files`` recorded it against. This is worth stating precisely, since
the same column name appears on ``desktop_publish_ledger`` where it *is* relative
to the staging root (``PublishService.abs_of``). The two are not interchangeable,
and resolving this table's column through ``abs_of`` looks up a different
directory -- which fails closed here (the path is absent, so nothing is landed),
but is still the wrong base to build on.

Two deliberate properties
------------------------

**The digest is computed from the bytes that are actually on disk, never from a
stored column.** The point of a digest check is to catch the case where the file
is not what it is supposed to be -- a truncated copy, a replaced artifact, a
stale path. Reading a *stored* digest and comparing it to itself would verify
nothing at all. So the row supplies the *identity* (id, version) and the file
supplies the *content*.

**Authorization is not re-implemented here.** The lookup is scoped to the caller's
own ``tenant_id``/``user_id`` and, when a run is named, to that run -- and a row
belonging to anyone else is reported as "not found" rather than "forbidden", which
is what ``delete_run_input`` already does and what keeps this from leaking the
existence of another user's attachments. A second, weaker authorization rule is
exactly the failure this module must not introduce.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from common.log import logger

__all__ = [
    "RunInputRef",
    "RunInputFetcher",
    "RunLanding",
    "fetcher_for_identity",
    "landing_for_identity",
]


@dataclass(frozen=True)
class RunInputRef:
    """A run input as the server knows it: identity plus where the bytes live."""

    resource_id: str
    version: str = ""
    artifact_rel: str = ""
    run_id: str = ""
    filename: str = ""


class RunInputFetcher:
    """Resolves ``resource:<id>`` to verified bytes, scoped to one caller.

    Callable, so it can be handed straight to
    :class:`~agent.desktop_local.resource_landing.ResourceLanding`.
    """

    def __init__(self, service: Any = None, *, identity: Any = None,
                 publisher: Any = None, work_root: Any = None):
        self._service = service
        self._identity = identity
        self._publisher_override = publisher
        self._work_root_override = work_root

    def _ident(self) -> Any:
        if self._identity is not None:
            return self._identity
        from common.runtime_identity import current_identity

        return current_identity()

    def _work_root(self):
        """The base ``desktop_run_inputs.artifact_rel`` is relative to.

        See the module docstring: that is the Agent's user work dir, not the
        publish staging root. ``None`` when there is no user to resolve one for,
        which makes the input unresolvable rather than resolved to a guess.
        """
        if self._work_root_override is not None:
            return Path(self._work_root_override)
        from common.state_dir import agent_user_work_dir

        work = agent_user_work_dir(self._ident(), ensure=False)
        return None if work is None else Path(work)

    def _publisher(self):
        # Injected first: reaching into the web auth layer is the production path,
        # not a requirement of this seam, and a test should not have to build a
        # whole publish store to ask what an authorized input is.
        if self._publisher_override is not None:
            return self._publisher_override
        if self._service is None:
            from channel.web.auth_handlers import _get_service
            from integrations.desktop.publish import service_for

            self._service = _get_service()
            return service_for(self._service)
        from integrations.desktop.publish import service_for

        return service_for(self._service)

    def describe(self, resource_id: str) -> Optional[RunInputRef]:
        """The row for ``resource_id``, or ``None`` when the caller may not have it.

        ``None`` covers "no such input", "someone else's input" and "deleted or
        expired" alike. The caller does not need to tell them apart, and telling
        them apart is how an endpoint starts confirming that another user's
        attachment exists.
        """
        ident = self._ident()
        if ident is None or not getattr(ident, "user_id", ""):
            return None
        service = self._publisher()
        store = getattr(getattr(service, "_svc", None), "_store", None)
        if store is None:
            logger.debug("[RunInputs] publish store unavailable; no run inputs")
            return None
        try:
            rows = store.execute(
                "SELECT * FROM desktop_run_inputs WHERE id=?", (str(resource_id),))
        except Exception as e:  # noqa: BLE001 - an unreadable table is "no input"
            logger.warning(f"[RunInputs] lookup for {resource_id!r} failed: {e}")
            return None
        if not rows:
            return None
        row = dict(rows[0])
        if row.get("deleted_at"):
            return None
        if row.get("user_id") != getattr(ident, "user_id", ""):
            return None
        tenant_id = getattr(ident, "tenant_id", "")
        if tenant_id and row.get("tenant_id") and row["tenant_id"] != tenant_id:
            return None
        import time

        retained_until = row.get("retained_until")
        if retained_until and int(retained_until) <= int(time.time()):
            return None
        artifact_rel = str(row.get("artifact_rel") or "")
        if not artifact_rel:
            return None
        return RunInputRef(
            resource_id=str(row.get("id") or resource_id),
            version=str(row.get("source_version") or ""),
            artifact_rel=artifact_rel,
            run_id=str(row.get("run_id") or ""),
            filename=os.path.basename(artifact_rel),
        )

    def pull(self, resource_id: str):
        """``(ref, bytes)`` for an authorized input, or ``None``.

        Separate from :meth:`__call__` so a test can assert the identity/version
        half without reading a file, and so a caller that wants to land under the
        original filename has it.
        """
        ref = self.describe(resource_id)
        if ref is None:
            return None
        root = self._work_root()
        if root is None:
            logger.warning("[RunInputs] no user work dir; cannot resolve run input")
            return None
        # Resolve both sides: the base can itself be reached through a symlink
        # (macOS ``/var`` -> ``/private/var``), and comparing a resolved candidate
        # against an unresolved base would reject every legitimate input.
        root = root.resolve()
        candidate = (root / ref.artifact_rel).resolve()
        # Containment: a row is trusted to name a file under this user's work dir,
        # and nothing else. Without this a stored ``../`` would read outside it.
        if candidate != root and root not in candidate.parents:
            logger.warning(
                f"[RunInputs] run input {ref.resource_id!r} escapes the work dir")
            return None
        path = candidate
        try:
            data = path.read_bytes()
        except OSError as e:
            logger.warning(f"[RunInputs] reading {ref.artifact_rel!r} failed: {e}")
            return None
        return ref, data

    def __call__(self, resource_id: str):
        """The landing fetcher: verified-shape ``FetchedResource`` or ``None``."""
        from agent.desktop_local.resource_landing import FetchedResource, digest_of

        pulled = self.pull(resource_id)
        if pulled is None:
            return None
        ref, data = pulled
        # The digest is of the bytes read just now -- see the module docstring on
        # why a stored digest would verify nothing.
        return FetchedResource(
            version=ref.version,
            digest=digest_of(data),
            data=data,
            name=ref.filename,
        )


def fetcher_for_identity(identity: Any = None) -> RunInputFetcher:
    """A fetcher bound to one identity, for a run's staging seam."""
    return RunInputFetcher(identity=identity)


class RunLanding:
    """A landing whose directory is this run's input directory, made on first use.

    The indirection exists so that resolving *where* a landing would go has no
    side effect. ``prepare_tool_inputs`` only calls :meth:`land` when it actually
    meets a ``resource:`` reference, so a run that never receives a server
    attachment never causes a directory to appear in the user's project.

    It also keeps ``ResourceLanding`` strict about its own rule -- a missing root is
    a refusal, because the root is granted rather than provisioned. Here the root
    *is* granted (the project root) and this class is the code that owns it, so it
    is the right place to create the run-scoped subdirectory.
    """

    def __init__(self, identity: Any = None, *, fetch: Any = None, root: str = ""):
        self._identity = identity
        self._fetch = fetch
        self._root = root
        self._engine = None

    def _ident(self) -> Any:
        if self._identity is not None:
            return self._identity
        from common.runtime_identity import current_identity

        return current_identity()

    def _engine_for(self):
        from agent.desktop_local.resource_landing import ResourceLanding
        from agent.desktop_local.run_context import ensure_run_input_dir

        if self._root:
            directory, refusal = self._root, None
        else:
            directory, refusal = ensure_run_input_dir(self._ident())
        if directory is None:
            return None, refusal
        fetch = self._fetch or fetcher_for_identity(self._ident())
        return ResourceLanding(fetch, directory), None

    def land(self, request):
        """Land one resource, creating the run input directory if it is needed."""
        from agent.desktop_local.resource_landing import Landing

        if self._engine is None:
            engine, refusal = self._engine_for()
            if engine is None:
                return Landing.refusal(
                    "resource_unavailable",
                    refusal or "the run's input directory is not available")
            self._engine = engine
        return self._engine.land(request)


def landing_for_identity(identity: Any = None) -> RunLanding:
    """The landing a desktop run stages ``resource:`` references through."""
    return RunLanding(identity)
