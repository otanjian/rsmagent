"""The personal coding workspace: one account directory under project root P.

change ``add-coding-agent-personal-workspace``. A coding Agent is configured
with one remote project root ``P`` -- the directory that holds its trusted
configuration, project tools and public resources. Personal business files
(uploads, generated results, working scripts) must not land loose in ``P``: they
belong in a per-account subtree

    U = P / user / <account>

so that every input file, the default session working directory and every output
share one place, while the project's own configuration keeps being read from
``P``.

This module owns the *resolution* of that location, not the file operations
themselves:

* the account name comes from the identity database, keyed by the immutable
  ``user_id`` -- the request never supplies an owner, a name or a path, and an
  unsafe name is refused rather than rewritten;
* the binding is durable (``coding_user_workspaces`` in the identity database)
  and unique on both the subject and the canonical directory, so a retry reuses
  one directory while an account alias or a different subject cannot silently
  share a tree;
* preparation is idempotent and its failure is retryable: a successful remote
  mkdir whose response was lost is resumed by the same reservation, and a
  failure never falls back to ``P``, the platform's own filesystem, or another
  Agent.

Deliberately absent: a second directory index, a background reconciler, or any
local ``os.path`` operation on the remote path. ``P`` names a directory on the
*OpenCode host*; the platform never creates a same-named local directory and
never resolves the remote path locally.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Dict, Mapping, Optional

from agent.coding import (
    CODING_INVALID_REQUEST,
    CODING_WORKSPACE_CONFLICT,
    CODING_WORKSPACE_UNSAFE,
    CodingError,
    CodingSettings,
    coding_disabled,
    resolve_settings,
)
from agent.coding.opencode import OpenCodeClient

#: The container directory under the project root. Its name is part of the
#: user-visible layout (``P/user/<account>``), so it is a constant rather than
#: a configuration knob: a per-deployment name would make two deployments
#: disagree about where the same account's files are.
USER_CONTAINER = "user"

#: Table holding the binding, in the identity database.
CODING_USER_WORKSPACES = "coding_user_workspaces"

#: One plain path segment: letters, digits, dot, underscore, hyphen. This is
#: the same grammar the account rules already allow; anything else (a
#: separator, a space, ``.``/``..``) cannot be represented and is refused
#: rather than sanitised, because sanitising would let two distinct accounts
#: collide on one directory.
_ALLOWED_NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789._-"
)

_FIELD_SEP = "\x1f"
_WORKSPACE_PREFIX = "ocws_"
_DIGEST_CHARS = 32


def safe_username_segment(username: Any) -> Optional[str]:
    """The account name as one safe path segment, or ``None`` when it is not.

    The canonical spelling is returned unchanged: login case is *not* folded
    into a new directory, so a user who types their name in a different case
    keeps the directory the binding already froze. ``None`` means the stored
    value cannot be one unambiguous segment -- an unknown account, an empty
    name, or a legacy value carrying a separator -- and the caller must refuse
    rather than guess a replacement.
    """
    if username is None:
        return None
    text = username if isinstance(username, str) else str(username)
    if text in ("", ".", ".."):
        return None
    if any(char not in _ALLOWED_NAME_CHARS for char in text):
        return None
    return text


def personal_root(base_project_dir: str, username: str) -> str:
    """``U = P/user/<username>`` as a path on the OpenCode host.

    Pure string composition: the separators are the service's, and no local
    canonicalisation happens here (the project is a directory on the *remote*
    host, so ``os.path.realpath`` would be meaningless and wrong whenever the
    two run apart). A trailing slash on ``P`` is normalised so a retry with a
    differently-spelled configured root still names one directory.
    """
    base = str(base_project_dir or "").rstrip("/")
    return f"{base}/{USER_CONTAINER}/{username}"


def workspace_id_for(
    *,
    service_id: str,
    tenant_id: str,
    base_project_dir: str,
    user_id: str,
) -> str:
    """A stable binding id derived from the verified identity only.

    Like the session ids, this is a pure function of data that survives a
    restart, so the reservation in the database is the only state that matters:
    the same subject resolves to the same id on every process, and a different
    service, tenant, project or user derives a different one.
    """
    parts = []
    for value in (service_id, tenant_id, base_project_dir, user_id):
        text = "" if value is None else str(value)
        parts.append(f"{len(text)}:{text}")
    digest = hashlib.sha256(_FIELD_SEP.join(parts).encode("utf-8")).hexdigest()
    return f"{_WORKSPACE_PREFIX}{digest[:_DIGEST_CHARS]}"


class CodingWorkspaceService:
    """Resolve, reserve and prepare the caller's personal coding directory.

    ``identity`` is the identity service (the source of the canonical account
    name and of the protected binding table). ``client`` is injectable so the
    whole preparation path runs against a scripted remote without a socket.
    """

    def __init__(
        self,
        identity: Any,
        *,
        settings: Optional[CodingSettings] = None,
        client: Any = None,
    ):
        self._identity = identity
        self.settings = settings or resolve_settings()
        self._client = client

    # -- wiring ------------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = OpenCodeClient(self.settings)
        return self._client

    @property
    def _store(self) -> Any:
        return self._identity._store

    def _require_enabled(self) -> CodingSettings:
        """Refuse the capability, never the user: 503 and a stable code."""
        if not (self.settings.enabled and self.settings.configured
                and self.settings.personal_workspace_enabled):
            raise coding_disabled()
        return self.settings

    # -- identity ----------------------------------------------------------

    def canonical_username(self, user_id: str) -> str:
        """The frozen account name for a verified ``user_id``, or a refusal.

        The user is read from the identity database rather than trusted from
        the request, and the stored name must be representable as one segment.
        An unknown account and an unrepresentable legacy name are the same
        refusal to the caller: this request cannot be placed in a personal
        directory, and nothing is created for it.
        """
        if not user_id:
            raise CodingError(
                CODING_WORKSPACE_UNSAFE,
                "a personal coding workspace needs a verified account",
                403,
            )
        try:
            user = self._identity._find_user_by_id(str(user_id))
        except Exception as exc:  # noqa: BLE001 - a read failure is a refusal
            raise CodingError(
                CODING_WORKSPACE_UNSAFE,
                f"the account could not be read: {exc}",
                503,
            ) from exc
        segment = safe_username_segment((user or {}).get("username"))
        if segment is None:
            raise CodingError(
                CODING_WORKSPACE_UNSAFE,
                "this account cannot be represented as a single directory name",
                403,
            )
        return segment

    # -- bindings ----------------------------------------------------------

    def _row(self, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def _by_subject(self, *, service_id, tenant_id, base, user_id
                    ) -> Optional[Dict[str, Any]]:
        rows = self._store.execute(
            f"SELECT * FROM {CODING_USER_WORKSPACES}"
            " WHERE service_id=? AND tenant_id=? AND base_project_dir=?"
            " AND user_id=?",
            (service_id, tenant_id, base, user_id),
        )
        return self._row(rows[0]) if rows else None

    def _by_directory(self, *, service_id, directory) -> Optional[Dict[str, Any]]:
        rows = self._store.execute(
            f"SELECT * FROM {CODING_USER_WORKSPACES}"
            " WHERE service_id=? AND effective_directory=?",
            (service_id, directory),
        )
        return self._row(rows[0]) if rows else None

    def _reserve(self, *, workspace_id, service_id, tenant_id, user_id,
                 username, base, directory) -> Dict[str, Any]:
        """Write the reservation, or adopt the row a concurrent request wrote.

        The two unique constraints are the arbiter. A losing insert is read back
        as one of three answers: the winner's own row (a plain concurrent
        retry), another subject on the same canonical directory (a conflict the
        caller must not be given), or nothing at all (the id collided on some
        other dimension, which is also a conflict).
        """
        try:
            with self._store.connect() as con:
                con.execute(
                    f"INSERT INTO {CODING_USER_WORKSPACES}"
                    " (workspace_id, service_id, tenant_id, user_id,"
                    "  username_at_creation, base_project_dir,"
                    "  effective_directory, state)"
                    " VALUES (?,?,?,?,?,?,?, 'creating')",
                    (workspace_id, service_id, tenant_id, user_id, username,
                     base, directory),
                )
        except sqlite3.IntegrityError:
            existing = self._by_subject(
                service_id=service_id, tenant_id=tenant_id, base=base,
                user_id=user_id)
            if existing is not None:
                if existing["effective_directory"] != directory:
                    raise CodingError(
                        CODING_WORKSPACE_CONFLICT,
                        "this account already has a workspace in another directory",
                        409,
                    )
                return existing
            raise CodingError(
                CODING_WORKSPACE_CONFLICT,
                "this personal directory already belongs to another account",
                409,
            ) from None
        adopted = self._by_subject(
            service_id=service_id, tenant_id=tenant_id, base=base, user_id=user_id)
        return adopted or {
            "workspace_id": workspace_id,
            "service_id": service_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "username_at_creation": username,
            "base_project_dir": base,
            "effective_directory": directory,
            "state": "creating",
        }

    def _mark_ready(self, workspace_id: str) -> None:
        with self._store.connect() as con:
            con.execute(
                f"UPDATE {CODING_USER_WORKSPACES} SET state='ready'"
                " WHERE workspace_id=?",
                (workspace_id,),
            )

    # -- open / prepare ----------------------------------------------------

    def open(
        self,
        *,
        agent_id: str = "",
        base_project_dir: str,
        user_id: str = "",
        tenant_id: str = "",
        session_id: str = "",
    ) -> Dict[str, Any]:
        """Prepare (or reuse) the caller's personal directory and describe it.

        Idempotent by construction: the same subject, project and service
        resolve to the same reservation, so two windows opening at once produce
        one directory, and a retry after a lost response resumes the same one.
        """
        settings = self._require_enabled()
        base = str(base_project_dir or "").strip().rstrip("/")
        if not base:
            raise CodingError(
                CODING_INVALID_REQUEST,
                "a personal coding workspace needs the agent's project directory",
                400,
            )
        username = self.canonical_username(user_id)
        directory = personal_root(base, username)

        binding = self._by_subject(
            service_id=settings.service_id, tenant_id=tenant_id, base=base,
            user_id=user_id)
        if binding is None:
            occupied = self._by_directory(
                service_id=settings.service_id, directory=directory)
            if occupied is not None and occupied["user_id"] != user_id:
                raise CodingError(
                    CODING_WORKSPACE_CONFLICT,
                    "this personal directory already belongs to another account",
                    409,
                )
            binding = self._reserve(
                workspace_id=workspace_id_for(
                    service_id=settings.service_id, tenant_id=tenant_id,
                    base_project_dir=base, user_id=user_id),
                service_id=settings.service_id, tenant_id=tenant_id,
                user_id=user_id, username=username, base=base,
                directory=directory)
        elif binding["effective_directory"] != directory:
            # The stored spelling and the freshly derived one disagree: the
            # account name changed since the binding was frozen, or the project
            # root was reconfigured. Both are conflicts rather than a silent
            # move, because moving would strand the files already in the old U.
            raise CodingError(
                CODING_WORKSPACE_CONFLICT,
                "this account's personal directory no longer matches its binding",
                409,
            )

        if binding["state"] != "ready":
            # Remote first, then ready: a failure leaves ``creating`` so a retry
            # resumes the same reservation instead of creating a second tree.
            self.client.prepare_workspace(
                binding["base_project_dir"], binding["effective_directory"],
                binding["workspace_id"])
            self._mark_ready(binding["workspace_id"])
            binding["state"] = "ready"

        return self.describe(binding)

    @staticmethod
    def describe(binding: Mapping[str, Any]) -> Dict[str, Any]:
        """The response-shaped projection of a binding.

        Carries identifiers and the *remote* directory, never a platform-local
        path and never a credential. ``source`` is the file-panel origin the
        Web layer dispatches on.
        """
        return {
            "source": "coding_remote",
            "workspace_id": binding["workspace_id"],
            "service_id": binding["service_id"],
            "tenant_id": binding.get("tenant_id", ""),
            "user_id": binding.get("user_id", ""),
            "username": binding.get("username_at_creation", ""),
            "base_project_dir": binding.get("base_project_dir", ""),
            "effective_directory": binding.get("effective_directory", ""),
            "state": binding.get("state", "creating"),
        }
