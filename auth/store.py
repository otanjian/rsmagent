# encoding:utf-8
"""SQLite identity store backing the multi-tenant IAM system.

This is the single source of truth for identity, tenancy, membership, RBAC,
department/organization, agent binding and the append-only identity audit. It
owns ``identity.db``. Agent workspaces and business content remain owned by the
existing Agent Registry and per-agent business databases respectively.

Design constraints observed here (repeated from the design.md boundary):

* One database, foreign keys + unique constraints enforced.
* Every mutable object carries an optimistic-concurrency ``version``.
* Password is never stored anywhere but the ``users.password_hash`` column
  (and never in audit).
* The ``schema_migrations`` table drives idempotent, versioned migrations.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from typing import Any, Callable, List, Sequence

from auth.policy import BUILTIN_MENU_DEFAULTS

#: Ordered list of schema migrations. Appending a migration to this list is
#: how the store evolves; ``migration_versions()`` derives the version ids
#: from its length.
_migrations: List[Callable[[sqlite3.Connection], None]] = []


def migration_versions() -> List[int]:
    """Return the ordered list of migration version identifiers."""
    return [index + 1 for index in range(len(_migrations))]


def _new_migration_id() -> str:
    """A unique id for a row a migration inserts.

    Migrations run before the service layer exists, so they cannot use
    ``auth.audit`` (which imports this module). The shape is the same
    ``audit_events.id``/``role_resource_grants.id`` text key: a random token,
    because a migration body may legitimately need more than one row of a kind
    and nothing outside the row's own uniqueness depends on the value.
    """
    return "mig-%s" % secrets.token_hex(12)


def has_migration_signature(db_path: str) -> bool:
    """Return True when ``db_path`` already carries a valid IAM migration marker.

    A database is considered "already migrated" (new-mode data) when the
    ``schema_migrations`` table exists and has at least one recorded version.
    Used by recovery/migration drills to detect that identity.db already carries
    new-mode schema markers.
    """
    if not db_path or not os.path.exists(db_path):
        return False
    try:
        con = sqlite3.connect(db_path)
        try:
            row = con.execute(
                "SELECT COUNT(*) AS c FROM schema_migrations"
            ).fetchone()
            return bool(row and row[0] > 0)
        finally:
            con.close()
    except (sqlite3.Error, Exception):
        return False


def _migration_1(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        -- Global accounts. One row per human principal.
        CREATE TABLE users (
            id            TEXT PRIMARY KEY,
            username      TEXT NOT NULL COLLATE NOCASE,
            display_name  TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            active        INTEGER NOT NULL DEFAULT 1,
            is_platform_admin INTEGER NOT NULL DEFAULT 0,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            temp_password_expires_at INTEGER,
            version       INTEGER NOT NULL DEFAULT 1,
            created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at    INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE UNIQUE INDEX idx_users_username ON users(username);

        -- Tenants. Stable, server-generated identity.
        CREATE TABLE tenants (
            id             TEXT PRIMARY KEY,
            code           TEXT NOT NULL COLLATE NOCASE,
            name           TEXT NOT NULL,
            active         INTEGER NOT NULL DEFAULT 1,
            shared_root    TEXT NOT NULL,
            default_agent_id TEXT,
            version        INTEGER NOT NULL DEFAULT 1,
            created_at     INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at     INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE UNIQUE INDEX idx_tenants_code ON tenants(code);

        -- Memberships: (tenant, user) edges, each with its own display/active/
        -- department/position. A user may belong to many tenants.
        CREATE TABLE memberships (
            id            TEXT PRIMARY KEY,
            tenant_id     TEXT NOT NULL REFERENCES tenants(id),
            user_id       TEXT NOT NULL REFERENCES users(id),
            display_name  TEXT NOT NULL,
            active        INTEGER NOT NULL DEFAULT 1,
            department_id TEXT,
            position_text TEXT,
            version       INTEGER NOT NULL DEFAULT 1,
            created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at    INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE UNIQUE INDEX idx_memberships_tenant_user ON memberships(tenant_id, user_id);
        CREATE INDEX idx_memberships_tenant ON memberships(tenant_id);

        -- Roles are tenant-scoped. Built-ins (tenant_admin/member) are flagged.
        CREATE TABLE roles (
            id               TEXT PRIMARY KEY,
            tenant_id        TEXT NOT NULL REFERENCES tenants(id),
            code             TEXT NOT NULL,
            name             TEXT NOT NULL,
            builtin          INTEGER NOT NULL DEFAULT 0,
            permissions_json TEXT NOT NULL,
            version          INTEGER NOT NULL DEFAULT 1,
            created_at       INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at       INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE UNIQUE INDEX idx_roles_tenant_code ON roles(tenant_id, code);

        -- Membership <-> Role many-to-many. The membership_id alone is the true
        -- foreign key into memberships; tenant scoping stays explicit at the
        -- application layer.
        CREATE TABLE membership_roles (
            membership_id TEXT NOT NULL REFERENCES memberships(id),
            role_id       TEXT NOT NULL REFERENCES roles(id),
            PRIMARY KEY (membership_id, role_id)
        );

        -- Departments: tenant-scoped tree. The virtual root is flagged by
        -- code='__root__' (not deletable, movable or disabled).
        CREATE TABLE departments (
            id          TEXT PRIMARY KEY,
            tenant_id   TEXT NOT NULL REFERENCES tenants(id),
            parent_id   TEXT,
            code        TEXT NOT NULL,
            name        TEXT NOT NULL,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            version     INTEGER NOT NULL DEFAULT 1,
            created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at  INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE UNIQUE INDEX idx_depts_tenant_code ON departments(tenant_id, code);

        -- Database-backed sessions. Only a digest of the opaque token is stored;
        -- no current-tenant or permission snapshot lives here.
        CREATE TABLE auth_sessions (
            id         TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            user_id    TEXT NOT NULL REFERENCES users(id),
            created_at INTEGER NOT NULL DEFAULT (unixepoch()),
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER,
            restricted INTEGER NOT NULL DEFAULT 0
        );

        -- Agent -> tenant binding. A global, unique agent_id maps to exactly one
        -- tenant. NULL private_owner_user_id means "explicitly tenant-shared";
        -- a non-null value records the private owner.
        CREATE TABLE agent_bindings (
            agent_id              TEXT PRIMARY KEY,
            tenant_id             TEXT NOT NULL REFERENCES tenants(id),
            private_owner_user_id TEXT REFERENCES users(id),
            created_at            INTEGER NOT NULL DEFAULT (unixepoch())
        );

        -- Append-only identity audit. Same-transaction commit as the identity
        -- change it describes. redacted_changes never contains secrets.
        CREATE TABLE audit_events (
            id               TEXT PRIMARY KEY,
            time             INTEGER NOT NULL DEFAULT (unixepoch()),
            actor_user_id    TEXT,
            actor_username   TEXT,
            tenant_id        TEXT,
            target_tenant_id TEXT,
            action           TEXT NOT NULL,
            target           TEXT NOT NULL,
            redacted_changes TEXT NOT NULL,
            result           TEXT NOT NULL
        );
        CREATE INDEX idx_audit_tenant ON audit_events(tenant_id);
        CREATE INDEX idx_audit_time ON audit_events(time);

        -- Audit is append-only: block DELETE and UPDATE at the database layer.
        CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
        CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

        """
    )


_migrations.append(_migration_1)


def _migration_2(con: sqlite3.Connection) -> None:
    """Resource-authorization schema (task 1.2).

    Two grant tables plus a per-role default-model field. No separate resource
    catalog or model policy table is created: resource identity is projected from
    the live sources; model defaults are a role column reusing the role version.
    The tenant-level "what may this tenant allocate" limit lives in
    ``tenant_resource_grants``; the per-role "what may a member use" lives in
    ``role_resource_grants``. Both reuse the owning role/tenant ``version`` for
    optimistic concurrency and audit in the same transaction.
    """
    con.executescript(
        """
        -- Platform-to-tenant global resource limits. Only global/model/tool/MCP
        -- resources need an explicit platform grant; tenant-owned resources are
        -- derived from their origin (agent_bindings, skills dir, tools registry).
        CREATE TABLE tenant_resource_grants (
            id            TEXT PRIMARY KEY,
            tenant_id     TEXT NOT NULL REFERENCES tenants(id),
            resource_kind TEXT NOT NULL,
            resource_id   TEXT NOT NULL,
            action        TEXT NOT NULL,
            created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            UNIQUE (tenant_id, resource_kind, resource_id, action)
        );
        CREATE INDEX idx_tenant_resource_grants_tenant ON tenant_resource_grants(tenant_id);

        -- Per-role resource grants. The role's tenant_id (via roles) is the true
        -- tenant scope; the application layer validates the resource belongs to
        -- that tenant before insert. A role's full grant set is versioned with
        -- the parent roles.version.
        CREATE TABLE role_resource_grants (
            id            TEXT PRIMARY KEY,
            role_id       TEXT NOT NULL REFERENCES roles(id),
            resource_kind TEXT NOT NULL,
            resource_id   TEXT NOT NULL,
            action        TEXT NOT NULL,
            created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            UNIQUE (role_id, resource_kind, resource_id, action)
        );
        CREATE INDEX idx_role_resource_grants_role ON role_resource_grants(role_id);

        -- Per-role optional default model per capability: {capability: model_id}.
        -- Stored on the role so it shares the role's version/audit transaction.
        ALTER TABLE roles ADD COLUMN model_defaults_json TEXT;
        """
    )


_migrations.append(_migration_2)


def _migration_3(con: sqlite3.Connection) -> None:
    """External identity bindings (task 1.1, open-database-runtime).

    Maps an external IM identity triple ``(provider, issuer/corp_id, subject)``
    to exactly one global User. Only administrators create bindings; inbound
    channel messages resolve through here before any execution so the runtime
    identity is always a real account, never a channel service principal.
    ``issuer`` is the provider's corp/tenant identifier (empty string means a
    provider without a corp scope, e.g. a personal consumer bot); uniqueness is
    still enforced on the triple as stored.
    """
    con.executescript(
        """
        CREATE TABLE external_identities (
            id            TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL REFERENCES users(id),
            provider      TEXT NOT NULL,
            issuer        TEXT NOT NULL DEFAULT '',
            subject       TEXT NOT NULL,
            created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            last_used_at  INTEGER
        );
        CREATE UNIQUE INDEX idx_external_identities_triple
            ON external_identities(provider, issuer, subject);
        CREATE INDEX idx_external_identities_user
            ON external_identities(user_id);
        """
    )


_migrations.append(_migration_3)


def _migration_4(con: sqlite3.Connection) -> None:
    """Enterprise control-plane tables (open-database-runtime 7.x-9.x).

    Four new stores back the credential-management, action-approval and
    resource-quota slices:

    * ``credentials`` + ``credential_versions`` — external credentials stored
      encrypted (ciphertext here is opaque; key material is deployment
      controlled). Rotation appends a new version row and bumps ``version``;
      the old ciphertext stays for audit but only the current version is ever
      decryptable. ``active`` marks a revoked credential.
    * ``approvals`` — single high-risk external side-effect actions: pending /
      approved / denied / expired / revoked with requester, agent, resource and
      decision trace. Versioning guards concurrent decisions.
    * ``quota_limits`` + ``quota_usage`` — tenant (and optional user) hard
      limits per metric with windowed usage so a limit change applies from the
      next consumption, never retroactively.
    """
    con.executescript(
        """
        CREATE TABLE credentials (
            id            TEXT PRIMARY KEY,
            tenant_id     TEXT NOT NULL REFERENCES tenants(id),
            name          TEXT NOT NULL,
            resource_kind TEXT NOT NULL DEFAULT '',
            resource_id   TEXT NOT NULL DEFAULT '',
            ciphertext    TEXT NOT NULL,
            active        INTEGER NOT NULL DEFAULT 1,
            version       INTEGER NOT NULL DEFAULT 1,
            created_by    TEXT NOT NULL,
            created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at    INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE UNIQUE INDEX idx_credentials_tenant_name
            ON credentials(tenant_id, name);
        CREATE INDEX idx_credentials_resource
            ON credentials(tenant_id, resource_kind, resource_id);

        CREATE TABLE credential_versions (
            credential_id TEXT NOT NULL REFERENCES credentials(id),
            version       INTEGER NOT NULL,
            ciphertext    TEXT NOT NULL,
            action        TEXT NOT NULL,
            changed_by    TEXT NOT NULL,
            changed_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (credential_id, version)
        );

        CREATE TABLE approvals (
            id               TEXT PRIMARY KEY,
            tenant_id        TEXT NOT NULL REFERENCES tenants(id),
            requester_user_id TEXT NOT NULL,
            agent_id         TEXT NOT NULL DEFAULT '',
            action           TEXT NOT NULL,
            payload_json     TEXT NOT NULL DEFAULT '{}',
            status           TEXT NOT NULL DEFAULT 'pending',
            decision_by      TEXT,
            decision_note    TEXT NOT NULL DEFAULT '',
            expires_at       INTEGER,
            created_at       INTEGER NOT NULL DEFAULT (unixepoch()),
            decided_at       INTEGER,
            version          INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX idx_approvals_tenant_status
            ON approvals(tenant_id, status);

        CREATE TABLE quota_limits (
            tenant_id  TEXT NOT NULL,
            user_id    TEXT NOT NULL DEFAULT '',
            metric     TEXT NOT NULL,
            hard_limit INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, user_id, metric)
        );
        CREATE TABLE quota_usage (
            tenant_id    TEXT NOT NULL,
            user_id      TEXT NOT NULL DEFAULT '',
            metric       TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            used         INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, user_id, metric, window_start)
        );
        """
    )


_migrations.append(_migration_4)


def _migration_5(con: sqlite3.Connection) -> None:
    """Per-account avatar token (web console personal-profile edit).

    Adds a single ``avatar`` column to ``users`` holding ``AVATAR_IMAGE_TOKEN``
    (``"image"``) when the account uploaded an avatar, or NULL when none. The
    bytes live on disk under ``shared_root()/avatars`` keyed by ``user-<id>``;
    the column is only a metadata flag so a profile-edit upload never races the
    agent avatar store. Default NULL keeps existing accounts avatar-less.
    """
    con.executescript(
        """
        ALTER TABLE users ADD COLUMN avatar TEXT;
        """
    )


_migrations.append(_migration_5)


def _migration_6(con: sqlite3.Connection) -> None:
    """Platform-scoped built-in role (platform admin as a role binding).

    Converts the instance-wide platform qualification from the raw
    ``users.is_platform_admin`` flag into a platform-scoped built-in role
    binding. The flag is kept as a *derived mirror* (single-writer is the
    service's ``_set_platform_role``); the binding is the sole source of truth.

    * ``platform_roles`` — instance-wide roles with no tenant/owner. The
      ``platform_admin`` built-in is seeded once (idempotent).
    * ``user_platform_roles`` — account -> platform role bindings, keyed by
      (user_id, platform_role_id) so the backfill is idempotent.

    Existing platform admins (``is_platform_admin=1``) are backfilled a
    ``platform_admin`` binding; the mirror column is already consistent so no
    extra write is needed there.
    """
    con.executescript(
        """
        CREATE TABLE platform_roles (
            id               TEXT PRIMARY KEY,
            code             TEXT NOT NULL,
            name             TEXT NOT NULL,
            builtin          INTEGER NOT NULL DEFAULT 0,
            permissions_json TEXT NOT NULL,
            version          INTEGER NOT NULL DEFAULT 1,
            created_at       INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at       INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE UNIQUE INDEX idx_platform_roles_code ON platform_roles(code);

        CREATE TABLE user_platform_roles (
            user_id          TEXT NOT NULL REFERENCES users(id),
            platform_role_id TEXT NOT NULL REFERENCES platform_roles(id),
            created_at       INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (user_id, platform_role_id)
        );
        """
    )
    # Idempotent seed of the built-in platform_admin role. Fixed id + code make
    # a migration replay a no-op.
    con.execute(
        "INSERT OR IGNORE INTO platform_roles(id, code, name, builtin, permissions_json, version)"
        " VALUES (?, ?, ?, 1, ?, 1)",
        ("platform_admin", "platform_admin", "平台管理员", "[]"),
    )
    # Idempotent backfill: bind every flagged platform admin to the built-in
    # role. The mirror column already equals 1 for these rows, so binding and
    # mirror are consistent without further writes.
    con.execute(
        "INSERT OR IGNORE INTO user_platform_roles(user_id, platform_role_id)"
        " SELECT u.id, r.id FROM users u"
        " JOIN platform_roles r ON r.code = 'platform_admin'"
        " WHERE u.is_platform_admin = 1"
    )


_migrations.append(_migration_6)


def _migration_7(con: sqlite3.Connection) -> None:
    """Tenant-owned channel instances (change tenant-owned-message-channels).

    Gives channel configuration a tenant dimension inside the identity domain
    instead of the shared ``team.json`` roster: a tenant administrator owns
    instances scoped to their own tenant, and each instance binds one encrypted
    credential bundle in ``credentials`` (``resource_kind='channel'``).

    ``(tenant_id, display_name)`` is the uniqueness pair, enforced as a
    **partial** unique index over enabled rows only — the display name
    identifies an instance to its tenant's operators, while the same channel
    *type* may legitimately repeat (e.g. two Feishu bots in one tenant), and a
    disabled instance must not hold its name hostage against a replacement.
    ``version`` backs optimistic concurrency for edits; ``active`` is the
    enable/disable switch so disabling is the rollback path. Audit columns
    mirror the other mutable stores. No backfill: ``team.json`` and existing
    ``credentials`` rows are untouched.
    """
    con.executescript(
        """
        CREATE TABLE tenant_channel_instances (
            id            TEXT PRIMARY KEY NOT NULL,
            tenant_id     TEXT NOT NULL REFERENCES tenants(id),
            channel_type  TEXT NOT NULL,
            display_name  TEXT NOT NULL,
            agent_id      TEXT NOT NULL DEFAULT '',
            active        INTEGER NOT NULL DEFAULT 1,
            version       INTEGER NOT NULL DEFAULT 1,
            created_by    TEXT NOT NULL,
            created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at    INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE UNIQUE INDEX idx_tenant_channel_instances_name
            ON tenant_channel_instances(tenant_id, display_name) WHERE active = 1;
        CREATE INDEX idx_tenant_channel_instances_tenant_active
            ON tenant_channel_instances(tenant_id, active);
        """
    )


_migrations.append(_migration_7)


def _migration_8(con: sqlite3.Connection) -> None:
    """Clone provenance for agent bindings (change copy-default-tenant-agents).

    Copying an agent from the default tenant into another tenant creates a new
    agent id, so a repeated copy cannot recognise its own previous work from the
    id alone. Recording the *source* agent id on the binding makes the copy
    idempotent without depending on any naming convention: "has this tenant
    already received a clone of source agent X?" becomes a lookup.

    ``cloned_from_agent_id`` is nullable because an ordinary bind has no source
    agent, and the uniqueness it needs is **partial**: at most one clone of a
    given source per tenant (but the same source may be cloned into many
    tenants), while any number of plain (NULL) bindings may coexist.
    """
    con.executescript(
        """
        ALTER TABLE agent_bindings ADD COLUMN cloned_from_agent_id TEXT;
        CREATE UNIQUE INDEX idx_agent_bindings_clone_source
            ON agent_bindings(tenant_id, cloned_from_agent_id)
            WHERE cloned_from_agent_id IS NOT NULL;
        """
    )


_migrations.append(_migration_8)


def _migration_9(con: sqlite3.Connection) -> None:
    """Inbound authors seen but not yet bound to an account.

    Nobody knows another person's IM ``open_id``, so an administrator asked to
    "bind this member" has nothing to type. Recording each denied inbound turns
    that into a list to pick from.

    The row carries the **tenant and channel instance that delivered the
    message**, not just the identity triple. An attempt has no user yet — that
    is the whole point — so the delivering instance is the only thing that can
    say which tenant's administrator should be shown it. Attempts delivered by a
    non-tenant (legacy/platform) channel therefore carry an empty tenant and are
    visible only to platform administrators.

    ``(provider, issuer, subject)`` is unique: a chatty author would otherwise
    fill the list with their own retries. ``attempts`` counts the denials so the
    interface can show "tried 3 times" without storing three rows.
    """
    con.executescript(
        """
        CREATE TABLE external_identity_attempts (
            provider      TEXT NOT NULL,
            issuer        TEXT NOT NULL DEFAULT '',
            subject       TEXT NOT NULL,
            tenant_id     TEXT NOT NULL DEFAULT '',
            channel_type  TEXT NOT NULL DEFAULT '',
            instance_id   TEXT NOT NULL DEFAULT '',
            attempts      INTEGER NOT NULL DEFAULT 1,
            first_seen_at INTEGER NOT NULL DEFAULT (unixepoch()),
            last_seen_at  INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (provider, issuer, subject)
        );
        CREATE INDEX idx_external_identity_attempts_tenant
            ON external_identity_attempts(tenant_id, last_seen_at);
        """
    )


_migrations.append(_migration_9)


def _migration_10(con: sqlite3.Connection) -> None:
    """The evidence an administrator needs to tell *who* an attempt is.

    An identity triple is not an answer to "who messaged the bot?" — nobody
    knows their members' ``open_id`` values by heart, which is exactly why the
    binding screen exists. The sender's display name and a preview of what they
    actually said are what make the pending list actionable; ``is_group`` says
    whether the author spoke in a group, where one ``open_id`` is a single voice
    among many and a preview reads differently.

    All three are best-effort. A channel that cannot name the sender, or a
    message whose content is a local file path rather than text, records an
    empty/neutral value and the attempt is still offered for binding.
    """
    con.executescript(
        """
        ALTER TABLE external_identity_attempts ADD COLUMN sender_name TEXT NOT NULL DEFAULT '';
        ALTER TABLE external_identity_attempts ADD COLUMN message_preview TEXT NOT NULL DEFAULT '';
        ALTER TABLE external_identity_attempts ADD COLUMN is_group INTEGER NOT NULL DEFAULT 0;
        """
    )


_migrations.append(_migration_10)


def _migration_11(con: sqlite3.Connection) -> None:
    """Tenant-consistent RBAC, grant and quota rows (tasks 7.5 + 7.7).

    The application layer has always validated that a role belongs to the
    membership's tenant, and that a quota row names a real tenant. That is a
    convention, not a constraint: a bug (or a hand-written ``INSERT``) could
    write a cross-tenant ``membership_roles`` edge and nothing at the database
    layer would object. This migration makes the invariant structural:

    * ``roles`` and ``memberships`` gain a ``UNIQUE(tenant_id, id)`` index so
      they can be a composite foreign-key parent;
    * ``membership_roles`` gains ``tenant_id`` and is rebuilt with
      ``FOREIGN KEY(tenant_id, membership_id) REFERENCES memberships(tenant_id, id)``
      and ``FOREIGN KEY(tenant_id, role_id) REFERENCES roles(tenant_id, id)``:
      a row whose role and membership disagree on the tenant is now impossible
      to store, whichever code path tries;
    * ``role_resource_grants`` gains ``tenant_id`` with
      ``FOREIGN KEY(tenant_id, role_id) REFERENCES roles(tenant_id, id)``, so a
      grant can never point at another tenant's role;
    * ``quota_limits`` / ``quota_usage`` gain
      ``FOREIGN KEY(tenant_id) REFERENCES tenants(id)``, so a limit or a usage
      bucket can never exist for a tenant that does not.

    Existing rows are *derived*, never invented, exactly as the conversation
    store's tenancy backfill does (task 6.8): ``tenant_id`` comes from the
    parent row. An edge whose parent disagrees is dropped rather than
    re-attributed, because it is precisely the corruption this migration
    exists to prevent. ``PRAGMA foreign_key_check`` on the rebuilt tables is
    the post-condition, so a database that somehow ends up inconsistent fails
    the migration instead of booting with the invariant silently broken.
    """
    # Composite parents first: a foreign key needs its parent's unique index to
    # exist before the child table that references it is created.
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_roles_tenant_id ON roles(tenant_id, id)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memberships_tenant_id"
        " ON memberships(tenant_id, id)"
    )

    def rebuild(table: str, create_sql: str, copy_sql: str, indexes: Sequence[str]) -> None:
        """Swap ``table`` for ``create_sql``, deriving rows with ``copy_sql``."""
        legacy = f"{table}_pre_tenant"
        con.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        con.execute(create_sql)
        con.execute(copy_sql.replace("{legacy}", legacy))
        con.execute(f"DROP TABLE {legacy}")
        for statement in indexes:
            con.execute(statement)

    rebuild(
        "membership_roles",
        """
        CREATE TABLE membership_roles (
            tenant_id     TEXT NOT NULL,
            membership_id TEXT NOT NULL,
            role_id       TEXT NOT NULL,
            PRIMARY KEY (tenant_id, membership_id, role_id),
            FOREIGN KEY (tenant_id, membership_id) REFERENCES memberships(tenant_id, id),
            FOREIGN KEY (tenant_id, role_id) REFERENCES roles(tenant_id, id)
        )
        """,
        """
        INSERT INTO membership_roles(tenant_id, membership_id, role_id)
        SELECT m.tenant_id, edge.membership_id, edge.role_id
          FROM {legacy} edge
          JOIN memberships m ON m.id = edge.membership_id
          JOIN roles r ON r.id = edge.role_id AND r.tenant_id = m.tenant_id
        """,
        [],
    )

    rebuild(
        "role_resource_grants",
        """
        CREATE TABLE role_resource_grants (
            id            TEXT PRIMARY KEY,
            tenant_id     TEXT NOT NULL,
            role_id       TEXT NOT NULL,
            resource_kind TEXT NOT NULL,
            resource_id   TEXT NOT NULL,
            action        TEXT NOT NULL,
            created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            UNIQUE (role_id, resource_kind, resource_id, action),
            FOREIGN KEY (tenant_id, role_id) REFERENCES roles(tenant_id, id)
        )
        """,
        """
        INSERT INTO role_resource_grants(
            id, tenant_id, role_id, resource_kind, resource_id, action, created_at)
        SELECT g.id, r.tenant_id, g.role_id, g.resource_kind, g.resource_id,
               g.action, g.created_at
          FROM {legacy} g
          JOIN roles r ON r.id = g.role_id
        """,
        ["CREATE INDEX idx_role_resource_grants_role ON role_resource_grants(role_id)"],
    )

    # Quota is the other side of the same rule (task 7.7): a metric bucket is
    # only meaningful for a tenant that exists, so a leftover row for a tenant
    # deleted by hand can no longer make a meter answer for a stranger.
    rebuild(
        "quota_limits",
        """
        CREATE TABLE quota_limits (
            tenant_id  TEXT NOT NULL REFERENCES tenants(id),
            user_id    TEXT NOT NULL DEFAULT '',
            metric     TEXT NOT NULL,
            hard_limit INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, user_id, metric)
        )
        """,
        """
        INSERT INTO quota_limits(tenant_id, user_id, metric, hard_limit)
        SELECT q.tenant_id, q.user_id, q.metric, q.hard_limit
          FROM {legacy} q
          JOIN tenants t ON t.id = q.tenant_id
        """,
        [],
    )

    rebuild(
        "quota_usage",
        """
        CREATE TABLE quota_usage (
            tenant_id    TEXT NOT NULL REFERENCES tenants(id),
            user_id      TEXT NOT NULL DEFAULT '',
            metric       TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            used         INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, user_id, metric, window_start)
        )
        """,
        """
        INSERT INTO quota_usage(tenant_id, user_id, metric, window_start, used)
        SELECT q.tenant_id, q.user_id, q.metric, q.window_start, q.used
          FROM {legacy} q
          JOIN tenants t ON t.id = q.tenant_id
        """,
        [],
    )

    for table in ("membership_roles", "role_resource_grants",
                  "quota_limits", "quota_usage"):
        violations = con.execute(f"PRAGMA foreign_key_check({table})").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"{table} still holds {len(violations)} cross-tenant row(s)"
                " after migration; refusing to leave the invariant broken"
            )


_migrations.append(_migration_11)


def _migration_12(con: sqlite3.Connection) -> None:
    """Tenant archive (soft delete): a nullable timestamp on ``tenants``.

    ``archived_at IS NULL`` means "not archived". Archiving also clears
    ``active`` so every existing active-tenant gate already rejects an archived
    tenant; the column only distinguishes "archived" from a plain disable for
    listing, the archive marker and restore. No tenant row or data is deleted.
    """
    con.execute("ALTER TABLE tenants ADD COLUMN archived_at INTEGER")


_migrations.append(_migration_12)


def _migration_13(con: sqlite3.Connection) -> None:
    """Backfill built-in role permission sets to the explicit defaults.

    Before built-in roles became editable, their stored ``permissions_json``
    could only come from the old tenant seed (member: the seven read/todo ids;
    tenant_admin: ``list(PERMISSION_CATALOG)`` at seed time). Re-pointing every
    ``builtin=1`` row at the current explicit default gives ``member`` the
    use/create tier and ``tenant_admin`` the full catalogue, so the console
    display and the enforcement path agree after upgrade.

    Scoped strictly by ``builtin=1`` and code, so custom roles and every other
    tenant's rows are untouched; runs once per database via schema version.
    """
    import json as _json

    from auth.policy import MEMBER_CODE, TENANT_ADMIN_CODE, default_permissions_for

    for code in (TENANT_ADMIN_CODE, MEMBER_CODE):
        stored = _json.dumps(sorted(default_permissions_for(code)))
        con.execute(
            "UPDATE roles SET permissions_json=?, version=version+1"
            " WHERE builtin=1 AND code=?",
            (stored, code),
        )


_migrations.append(_migration_13)


def _migration_14(con: sqlite3.Connection) -> None:
    """A per-member default Agent (change add-user-personal-agent-provisioning).

    A tenant's ``default_agent_id`` is the entry *every* member shares, which is
    why it must stay tenant-shared. But product planning 3.1 also gives each user
    a space of their own, and a member may own a private assistant that is
    theirs alone to anchor on. That needs a default that is scoped to one
    (tenant, user) edge rather than to the tenant.

    ``memberships`` is exactly that edge, so the pointer lives here instead of in
    a new table: one row per (tenant, user), and deactivating or deleting the
    membership takes the registration with it. Nullable and unbackfilled — every
    existing member simply has no personal default, so default resolution
    behaves exactly as it did before the upgrade.
    """
    con.executescript(
        """
        ALTER TABLE memberships ADD COLUMN default_agent_id TEXT;
        """
    )


_migrations.append(_migration_14)


def _migration_15(con: sqlite3.Connection) -> None:
    """Strip the retired ``knowledge.write`` id from every role's permission set.

    The id no longer authorizes any write path (knowledge writes are decided by
    data-root + Agent ownership, see
    ``channel/web/web_channel.py::_knowledge_write_authorized``), so leaving it
    in a stored set would let the role editor display — and a save round-trip
    reject — a dead switch.

    Applied to *all* roles (built-in and custom), because either kind may hold a
    grant from before the id was retired. Only this one id is removed; every
    other id is preserved verbatim, and a row that does not contain it is left
    untouched (no version bump), so the migration is idempotent and can never
    widen a role.
    """
    import json as _json

    retired = "knowledge.write"
    rows = con.execute("SELECT id, permissions_json FROM roles").fetchall()
    for row_id, raw in rows:
        try:
            perms = _json.loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(perms, list) or retired not in perms:
            continue
        con.execute(
            "UPDATE roles SET permissions_json=?, version=version+1 WHERE id=?",
            (_json.dumps([p for p in perms if p != retired]), row_id),
        )


_migrations.append(_migration_15)


def _migration_16(con: sqlite3.Connection) -> None:
    """Give channel instances a tenant/user scope and an optional owner.

    Change ``enable-member-personal-console``: a member may own a *personal*
    channel instance, so ``tenant_channel_instances`` has to hold both kinds
    without a second table (the credential, version history and audit trail are
    already keyed by instance id and must keep working for both).

    ``scope`` is NOT NULL with a ``'tenant'`` default rather than a nullable
    column: every pre-existing row is by definition tenant-owned, so the default
    classifies history correctly with no backfill, and a caller that forgets to
    choose a scope cannot produce a row of ambiguous ownership.

    ``owner_user_id`` stays NULL for tenant instances (the owner is the tenant
    itself); the service layer is what requires it to be set when
    ``scope='user'``, since SQL cannot express a conditional NOT NULL.

    Uniqueness moves from ``(tenant_id, display_name)`` to include scope, owner
    and channel type. Two consequences are deliberate:

    * ``COALESCE(owner_user_id, '')`` — SQLite treats NULLs as distinct, so
      indexing the bare column would let two *tenant* instances (both NULL)
      share a display name, silently losing the old constraint.
    * ``channel_type`` joins the key, so a Feishu and a Slack bot in one tenant
      may share a display name. That widens what is accepted; the pre-existing
      behaviour it replaces was stricter than the product needs.
    """
    con.executescript(
        """
        ALTER TABLE tenant_channel_instances ADD COLUMN scope TEXT NOT NULL
            DEFAULT 'tenant';
        ALTER TABLE tenant_channel_instances ADD COLUMN owner_user_id TEXT;
        DROP INDEX IF EXISTS idx_tenant_channel_instances_name;
        CREATE UNIQUE INDEX idx_tenant_channel_instances_name
            ON tenant_channel_instances(
                tenant_id, scope, COALESCE(owner_user_id, ''), channel_type,
                display_name)
            WHERE active = 1;
        CREATE INDEX idx_tenant_channel_instances_owner
            ON tenant_channel_instances(tenant_id, owner_user_id)
            WHERE scope = 'user';
        """
    )


_migrations.append(_migration_16)


def _migration_17(con: sqlite3.Connection) -> None:
    """Record where a private agent binding came from.

    Change ``enable-member-personal-console``: provisioning must skip when the
    member already has a *system-made* personal assistant, but must NOT skip
    merely because they own some private agent they created themselves. Storage
    has to tell those two apart, so each binding carries an ``origin``.

    ``DEFAULT 'unknown'`` is deliberately not a synonym for
    ``'provisioned_assistant'``. Every row predating this column was written by
    code that did not record its reason, so the honest classification is
    "unknown"; treating history as system-made would let a later pass treat a
    member's own agent as replaceable. The column is added with a default (and
    not as a bare NOT NULL) for the same reason: the upgrade classifies, it does
    not fail and it does not invent.

    Known values are ``provisioned_assistant``, ``user_created`` and
    ``unknown``. No CHECK constraint pins them: the accepted set is owned by the
    service layer, so a future producer can add a value without a migration.
    """
    con.executescript(
        """
        ALTER TABLE agent_bindings ADD COLUMN origin TEXT NOT NULL
            DEFAULT 'unknown';
        """
    )


_migrations.append(_migration_17)


def _migration_18(con: sqlite3.Connection) -> None:
    """Store the self-service binding proof and the personal route it creates.

    Change ``enable-member-personal-console`` lets a member attach a channel
    instance to themselves. That needs two records, and they are deliberately
    two rather than one.

    ``binding_challenges`` is the proof of control. The console mints one for a
    *server-fixed* ``(tenant, user, instance, purpose)`` scope, and it is redeemed
    by the same person demonstrating they can speak to the bot in a private chat.
    Keeping the scope as columns rather than inside an opaque token is what makes
    a challenge unusable against a different target; ``consumed_at`` and
    ``attempts`` make it single-use and bound the guessing of its code.

    ``personal_channel_links`` is the resulting route. It is kept separate from
    ``external_identities`` on purpose: that table holds the *global*
    provider-identity-to-user mapping, which may still be in use by another
    tenant or by the tenant's own public bind flow. Unlinking has to remove only
    this tenant's personal route, so the two facts must not share a row — the
    ``(tenant, user, instance)`` primary key is the route, and the identity is
    referenced, never owned.
    """
    con.executescript(
        """
        CREATE TABLE binding_challenges (
            -- ``NOT NULL`` is spelled out: SQLite only implies it for an
            -- INTEGER PRIMARY KEY, so a bare TEXT primary key would accept NULL.
            id          TEXT PRIMARY KEY NOT NULL,
            tenant_id   TEXT NOT NULL REFERENCES tenants(id),
            user_id     TEXT NOT NULL REFERENCES users(id),
            instance_id TEXT NOT NULL REFERENCES tenant_channel_instances(id),
            purpose     TEXT NOT NULL,
            code_hash   TEXT NOT NULL,
            attempts    INTEGER NOT NULL DEFAULT 0,
            expires_at  INTEGER NOT NULL,
            consumed_at INTEGER,
            created_at  INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE INDEX idx_binding_challenges_scope
            ON binding_challenges(tenant_id, user_id, instance_id, purpose,
                                  created_at);

        CREATE TABLE personal_channel_links (
            tenant_id            TEXT NOT NULL REFERENCES tenants(id),
            user_id              TEXT NOT NULL REFERENCES users(id),
            instance_id          TEXT NOT NULL REFERENCES tenant_channel_instances(id),
            external_identity_id TEXT NOT NULL REFERENCES external_identities(id),
            active               INTEGER NOT NULL DEFAULT 1,
            created_at           INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (tenant_id, user_id, instance_id)
        );
        CREATE INDEX idx_personal_channel_links_identity
            ON personal_channel_links(external_identity_id);
        """
    )


_migrations.append(_migration_18)


def _migration_19(con: sqlite3.Connection) -> None:
    """Store a member's own tool/skill parameters and who a credential belongs to.

    Change ``enable-member-personal-console``: a member may save parameters for a
    resource *for their own use*, and such a configuration must never rewrite the
    public resource. That needs two pieces of storage.

    ``credentials.owner_user_id`` — a credential's ownership has to be a fact of
    the row rather than something encoded in its name. It stays NULL for every
    credential the tenant owns as a whole, so the upgrade classifies history
    correctly with no backfill, and the tenant's existing credential list keeps
    resolving them (a non-NULL owner is what makes a credential personal).

    ``personal_resource_configs`` — keyed by ``(tenant, user, kind, resource)``,
    so a member has at most one parameter set per resource, and two members
    configuring the same resource cannot collide. Sensitive values are *not*
    stored here: the row keeps a ``credential_id`` reference and non-sensitive
    parameters only, which is what keeps secrets out of a plaintext blob.
    """
    con.executescript(
        """
        ALTER TABLE credentials ADD COLUMN owner_user_id TEXT REFERENCES users(id);
        CREATE INDEX idx_credentials_owner
            ON credentials(tenant_id, owner_user_id);

        CREATE TABLE personal_resource_configs (
            tenant_id     TEXT NOT NULL REFERENCES tenants(id),
            user_id       TEXT NOT NULL REFERENCES users(id),
            resource_kind TEXT NOT NULL,
            resource_id   TEXT NOT NULL,
            params_json   TEXT NOT NULL DEFAULT '{}',
            credential_id TEXT REFERENCES credentials(id),
            version       INTEGER NOT NULL DEFAULT 1,
            created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at    INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (tenant_id, user_id, resource_kind, resource_id)
        );
        """
    )


_migrations.append(_migration_19)


def _migration_20(con: sqlite3.Connection) -> None:
    """Give the built-in roles their default console ``menu`` grants.

    Change ``enable-member-personal-console`` registers five personal pages and
    turns menu gating on for the built-in roles by granting them. Gating is
    *restrictive* — a role holding any ``menu`` grant is bound to that set — so
    this cannot be a two-line "insert the new pages". Built-in roles held no menu
    grants and were therefore governed by functional permissions alone; adding
    just the five personal pages would have hidden every page a member could
    already open.

    So each built-in role is given its default set as a set of ids (see
    ``BUILTIN_MENU_DEFAULTS``): the pages it could already reach, plus the new
    personal ones. The statement is a guarded insert, which makes the backfill
    idempotent and, more importantly, non-destructive: an existing grant is never
    replaced and a grant an administrator removed is never restored by a re-run.
    Only the two built-in roles are touched — custom roles keep exactly the
    grants they were given, and no non-``menu`` grant is read or written.
    """
    rows = con.execute(
        "SELECT id, code, tenant_id FROM roles WHERE builtin=1"
        " AND code IN ('member', 'tenant_admin')").fetchall()
    for row in rows:
        for grant in BUILTIN_MENU_DEFAULTS.get(row["code"], ()):
            page = grant[len("nav:"):]
            con.execute(
                "INSERT INTO role_resource_grants(id, tenant_id, role_id,"
                " resource_kind, resource_id, action)"
                " SELECT ?, ?, ?, 'menu', ?, 'view'"
                " WHERE NOT EXISTS (SELECT 1 FROM role_resource_grants"
                "  WHERE role_id=? AND resource_kind='menu' AND resource_id=?)",
                ("grant-menu-%s-%s" % (row["id"], page), row["tenant_id"],
                 row["id"], grant, row["id"], grant),
            )


_migrations.append(_migration_20)


def _migration_21(con: sqlite3.Connection) -> None:
    """Tenant policy for personal access, plus per-instance governance stop.

    Two tenant controls from change task 2.5:

    * ``tenant_channel_policies`` — whether personal access is open at all, which
      channel types it may use, and how many personal instances an owner and the
      tenant as a whole may hold. Absence of a row means "nothing narrowed yet",
      never "denied", so an upgraded tenant behaves exactly as before.
    * ``governance_disabled_at`` / ``governance_disabled_by`` on the instance —
      a stop an administrator applies that the owner cannot clear by re-saving or
      re-enabling. It is deliberately *not* the same column as ``active``: the
      owner's own switch and the tenant's governance decision have to be
      independently visible, or lifting governance could not leave the instance
      stopped (which the spec requires: 解除限制后须由本人明确启用).

    Existing instances keep running: both new columns default to NULL.
    """
    con.execute("ALTER TABLE tenant_channel_instances"
                " ADD COLUMN governance_disabled_at INTEGER")
    con.execute("ALTER TABLE tenant_channel_instances"
                " ADD COLUMN governance_disabled_by TEXT")
    con.executescript(
        """
        CREATE TABLE tenant_channel_policies (
            tenant_id                     TEXT PRIMARY KEY NOT NULL
                                          REFERENCES tenants(id),
            personal_enabled              INTEGER NOT NULL DEFAULT 1,
            allowed_types_json            TEXT NOT NULL DEFAULT '[]',
            personal_instance_limit       INTEGER NOT NULL DEFAULT -1,
            tenant_personal_instance_limit INTEGER NOT NULL DEFAULT -1,
            updated_by                    TEXT,
            updated_at                    INTEGER NOT NULL DEFAULT (unixepoch())
        );
        """
    )


_migrations.append(_migration_21)


def _migration_22(con: sqlite3.Connection) -> None:
    """Tenant policy for member-created private Agents (task 4.2).

    A separate table from ``tenant_channel_policies`` because the two control
    different resources: a tenant may well allow a member ten personal Agents and
    one personal WeChat account, and collapsing them into one row would make
    every future control a guess about which resource it meant.

    Same rule as 2.5: **no row means nothing narrowed yet**, never "denied" — so a
    tenant upgraded to this schema keeps creating exactly as it did before, and
    only an explicit ``set_private_agent_policy`` narrows anything.

    Limits count *objects*, not usage windows, so they are not
    ``quota_limits`` metrics (which meter tokens/tool calls/messages/storage).
    ``-1`` means unlimited.
    """
    con.executescript(
        """
        CREATE TABLE tenant_private_agent_policies (
            tenant_id              TEXT PRIMARY KEY NOT NULL
                                   REFERENCES tenants(id),
            personal_enabled       INTEGER NOT NULL DEFAULT 1,
            member_agent_limit     INTEGER NOT NULL DEFAULT -1,
            tenant_agent_limit     INTEGER NOT NULL DEFAULT -1,
            updated_by             TEXT,
            updated_at             INTEGER NOT NULL DEFAULT (unixepoch())
        );
        """
    )


_migrations.append(_migration_22)


def _migration_23(con: sqlite3.Connection) -> None:
    """External-application fingerprint for channel instances (task 6.3).

    Two instances configured with the *same* vendor application would fight over
    one connection: the second long-connection steals the first one's events, or
    the vendor refuses it outright. Detecting that needs an equality test across
    instances — including across two members' personal instances — but the
    bundles are separately encrypted and must not be decrypted to compare.

    So each instance stores a **keyed digest** of the credential fields that name
    its application (``channel.channel_instances.app_identity``). It is
    non-reversible, comparable, and says nothing about the secret itself; the
    unique index below then makes "one application, one active instance per
    tenant" a storage invariant rather than a check someone can forget.

    Existing rows default to ``''`` (unknown) and are *not* indexed, so this
    migration cannot fail on data that predates it. They acquire a fingerprint
    the next time they are written, and until then the write-time check simply
    cannot see them.

    The unique index is therefore scoped to personal rows (``scope='user'``):
    it is the race backstop for "two of my own instances, one application",
    while the public/personal boundary is decided by the service check, which
    deliberate asymmetry is documented there.
    """
    con.executescript(
        """
        ALTER TABLE tenant_channel_instances
            ADD COLUMN app_fingerprint TEXT NOT NULL DEFAULT '';
        CREATE UNIQUE INDEX idx_tenant_channel_instances_app
            ON tenant_channel_instances(tenant_id, app_fingerprint)
            WHERE active = 1 AND scope = 'user' AND app_fingerprint <> '';
        """
    )


_migrations.append(_migration_23)


def _migration_24(con: sqlite3.Connection) -> None:
    """Personal route *targets* for shared channel instances (task 7.2).

    A personal instance already knows its target: the instance row's ``agent_id``
    is the owner's private Agent. A route carried by a *shared* instance has no
    such row to read the target from — the member chooses their own private Agent
    at binding time, and every inbound on that shared instance must be routed to
    the choice the member made then, not to a target re-derived later (which the
    public instance's configuration could have moved in the meantime).

    So the target travels with the binding: the challenge records it at mint time
    (server-side, from the member's own instance of the chosen Agent), and the
    resulting link keeps it. Both columns default to ``''`` so existing personal
    rows — which never needed it — keep meaning exactly what they meant.

    Deliberately not a foreign key: an Agent can be deleted, and a route whose
    target no longer resolves must still be *readable* so the inbound path can
    refuse it explicitly instead of treating the row as absent and falling back
    to the shared Agent. The refusal is re-derived on every message.
    """
    con.executescript(
        """
        ALTER TABLE binding_challenges
            ADD COLUMN target_agent_id TEXT NOT NULL DEFAULT '';
        ALTER TABLE personal_channel_links
            ADD COLUMN target_agent_id TEXT NOT NULL DEFAULT '';
        """
    )


_migrations.append(_migration_24)


def _migration_25(con: sqlite3.Connection) -> None:
    """An independently versioned member default, and a one-time repair of the
    default pointers that could never have resolved (tasks 2.3, 4.4-4.6).

    ``memberships.default_agent_id`` was versioned by ``memberships.version``,
    which *every* display-name / department / position edit also bumps. A "set
    my default" write and an unrelated profile edit therefore conflict with each
    other for no reason, and a client holding one revision cannot tell which of
    the two it is holding. The pointer gets its own columns — a revision and an
    origin — leaving ``memberships.version`` to the editor draft it was meant
    for:

    * ``default_agent_revision`` — the member-default's own optimistic
      concurrency value (``set_user_default`` writes against it);
    * ``default_agent_origin`` — ``'user'`` when the member chose the Agent,
      ``'provisioned'`` when system provisioning registered it. NULL means "no
      registered preference", which is what lets provisioning initialise an
      empty one *without* ever competing with a member's own choice (task 4.6).

    The same migration repairs pointers that could never have resolved, and it
    repairs them **without touching ownership**:

    * a tenant default may not name an Agent that is unbound, bound to another
      tenant, or **private**. ``private_owner_user_id`` is an exclusive read
      gate, so a private tenant default locks every other member out of the
      entry the console offers them. The pointer is cleared (new conversations
      fall back to a tenant-shared Agent); ``private_owner_user_id`` is left
      exactly as it was — sharing an Agent stays an explicit, audited act
      (``make_agent_tenant_shared``), never a side effect of a default.
    * a member default may not name an Agent that is unbound, bound to another
      tenant, or private to a *different* member. Same repair, same rule about
      ownership.

    Every repair and every origin backfill appends an audit event in this same
    transaction, so the correction ships with its record. Backfilled legal
    pointers are marked ``'user'``: the pre-upgrade code could not distinguish
    the two origins, and treating an existing preference as the member's own is
    the only choice that cannot silently overwrite it later.
    """
    con.execute("ALTER TABLE memberships ADD COLUMN default_agent_revision"
                " INTEGER NOT NULL DEFAULT 1")
    con.execute("ALTER TABLE memberships ADD COLUMN default_agent_origin TEXT")

    def _audit(action: str, tenant_id: str, target: str, changes: dict) -> None:
        con.execute(
            "INSERT INTO audit_events(id, actor_user_id, actor_username,"
            " tenant_id, target_tenant_id, action, target, redacted_changes, result)"
            " VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, 'success')",
            (_new_migration_id(), tenant_id, tenant_id, action, target,
             json.dumps(changes, ensure_ascii=False)),
        )

    # -- tenant defaults -----------------------------------------------------
    for row in con.execute(
            "SELECT t.id AS tenant_id, t.default_agent_id AS agent_id,"
            " b.tenant_id AS bound_tenant, b.private_owner_user_id AS owner"
            " FROM tenants t LEFT JOIN agent_bindings b"
            " ON b.agent_id = t.default_agent_id"
            " WHERE t.default_agent_id IS NOT NULL"
            "  AND t.default_agent_id != ''").fetchall():
        reason = None
        if row["bound_tenant"] is None:
            reason = "unbound"
        elif row["bound_tenant"] != row["tenant_id"]:
            reason = "foreign_tenant"
        elif row["owner"] is not None:
            reason = "private_agent"
        if reason is None:
            continue
        con.execute(
            "UPDATE tenants SET default_agent_id=NULL, updated_at=unixepoch(),"
            " version=version+1 WHERE id=?", (row["tenant_id"],))
        _audit("tenant.default_agent.repaired", row["tenant_id"],
               "tenant:%s" % row["tenant_id"],
               {"default_agent_id": None, "repaired_from": row["agent_id"],
                "reason": reason, "private_owner_preserved": row["owner"]})

    # -- member defaults -----------------------------------------------------
    for row in con.execute(
            "SELECT m.tenant_id AS tenant_id, m.user_id AS user_id,"
            " m.default_agent_id AS agent_id, b.tenant_id AS bound_tenant,"
            " b.private_owner_user_id AS owner, m.id AS membership_id"
            " FROM memberships m LEFT JOIN agent_bindings b"
            " ON b.agent_id = m.default_agent_id"
            " WHERE m.default_agent_id IS NOT NULL"
            "  AND m.default_agent_id != ''").fetchall():
        reason = None
        if row["bound_tenant"] is None:
            reason = "unbound"
        elif row["bound_tenant"] != row["tenant_id"]:
            reason = "foreign_tenant"
        elif (row["owner"] is not None and row["owner"] != row["user_id"]):
            reason = "owned_by_another_member"
        if reason is None:
            # Legal: keep the pointer, and record it as the member's own so
            # provisioning can never overwrite it.
            con.execute(
                "UPDATE memberships SET default_agent_origin='user'"
                " WHERE id=? AND default_agent_origin IS NULL",
                (row["membership_id"],))
            continue
        con.execute(
            "UPDATE memberships SET default_agent_id=NULL,"
            " default_agent_origin=NULL, updated_at=unixepoch() WHERE id=?",
            (row["membership_id"],))
        _audit("member.default_agent.repaired", row["tenant_id"],
               "membership:%s" % row["membership_id"],
               {"default_agent_id": None, "repaired_from": row["agent_id"],
                "reason": reason, "user_id": row["user_id"],
                "private_owner_preserved": row["owner"]})


_migrations.append(_migration_25)


def _migration_26(con: sqlite3.Connection) -> None:
    """Map the legacy personal menu grants onto the formal console pages (task 2.4).

    The removal of the ``我的资源`` menu is a *grant* change, not only a UI
    change: menu gating is restrictive, so a role still carrying
    ``nav:personal.agents`` would be left holding an id that no longer resolves
    to a page — and, worse, one whose page the console no longer offers.

    For every role that holds a legacy personal id this migration:

    1. inserts the mapped formal id (see ``LEGACY_PERSONAL_MENU_MAP``), guarded so
       a role that already has it is untouched;
    2. deletes the legacy id from that role;
    3. bumps ``roles.version`` and appends an audit event naming exactly what was
       added and removed.

    What it deliberately does **not** do: touch any other grant (a custom role's
    other menu ids and every non-``menu`` grant survive verbatim), infer that a
    missing id was *revoked* and re-add it — only ids actually present are mapped,
    so an administrator's explicit withdrawal is not undone — or widen a role
    beyond the mapped pages. The many-to-one case is intentional: the console has
    one 工具与技能 page, so ``personal.tools`` and ``personal.skills`` both map to
    ``admin.skills`` and a role holding both gains it once.

    Idempotent: a re-run finds no legacy id to map. The migration runner already
    wraps this in one transaction, so the adds, deletes, version bumps and audit
    events commit together or not at all.
    """
    from auth.policy import LEGACY_PERSONAL_MENU_MAP

    legacy_ids = {"nav:%s" % pid for pid in LEGACY_PERSONAL_MENU_MAP}
    rows = con.execute(
        "SELECT id, code, tenant_id, version FROM roles"
        " WHERE id IN (SELECT role_id FROM role_resource_grants"
        "              WHERE resource_kind='menu')").fetchall()
    for role in rows:
        held = {
            r["resource_id"]
            for r in con.execute(
                "SELECT resource_id FROM role_resource_grants"
                " WHERE role_id=? AND resource_kind='menu'", (role["id"],)).fetchall()
        }
        present = sorted(held & legacy_ids)
        if not present:
            continue
        added = sorted({LEGACY_PERSONAL_MENU_MAP[gid[len("nav:"):]]
                        for gid in present})
        for page in added:
            grant = "nav:%s" % page
            con.execute(
                "INSERT INTO role_resource_grants(id, tenant_id, role_id,"
                " resource_kind, resource_id, action)"
                " SELECT ?, ?, ?, 'menu', ?, 'view'"
                " WHERE NOT EXISTS (SELECT 1 FROM role_resource_grants"
                "  WHERE role_id=? AND resource_kind='menu' AND resource_id=?)",
                ("grant-menu-%s-%s" % (role["id"], page), role["tenant_id"],
                 role["id"], grant, role["id"], grant),
            )
        con.execute(
            "DELETE FROM role_resource_grants WHERE role_id=?"
            " AND resource_kind='menu' AND resource_id IN (%s)"
            % ",".join("?" * len(present)),
            (role["id"],) + tuple(present),
        )
        con.execute("UPDATE roles SET version=version+1, updated_at=unixepoch()"
                    " WHERE id=?", (role["id"],))
        con.execute(
            "INSERT INTO audit_events(id, actor_user_id, actor_username,"
            " tenant_id, target_tenant_id, action, target, redacted_changes, result)"
            " VALUES (?, NULL, NULL, ?, ?, 'role.menu.legacy_personal_mapped',"
            " ?, ?, 'success')",
            (_new_migration_id(), role["tenant_id"], role["tenant_id"],
             "role:%s" % role["id"],
             json.dumps({"removed": present,
                         "added": ["nav:%s" % p for p in added],
                         "version": int(role["version"]) + 1},
                        ensure_ascii=False)),
        )


_migrations.append(_migration_26)


def _migration_27(con: sqlite3.Connection) -> None:
    """Grant the newly shared 模型与接入 page to the built-in roles (task 5.4).

    ``admin.models`` joins ``BUILTIN_MENU_DEFAULTS`` because the page became the
    member's model catalog: the public vendor address and key stay platform-only,
    while everyone else reads the models they are authorized for. A tenant
    created from now on is seeded with it by ``_seed_tenant_defaults``; a tenant
    that already exists was seeded (``_migration_20``) from the older table,
    where the page was still ``platform``-scoped — menu gating is restrictive, so
    without this backfill the page would stay hidden for exactly the roles that
    are supposed to reach it, on every deployment that is not brand new.

    Only the two built-in roles are considered, and only when they already carry
    a ``menu`` grant: a role with none is still governed by functional
    permissions alone (the compat rule), and adding a grant to it would *switch
    gating on* and hide the rest of its console. The insert is guarded, so a
    re-run adds nothing.

    Like ``_migration_20``, this cannot tell "never held" from "withdrawn" — the
    page was not in the table at all until now, so there is no withdrawal to
    honour — and it is a *one-time* classification: the table is read once here,
    never re-asserted per request, which is what lets an administrator take the
    page away afterwards without it coming back.

    No version bump: unlike ``_migration_26`` this only *adds* a grant, so no
    cached decision becomes wrong — the same reasoning ``_migration_20`` follows.
    """
    for row in con.execute(
            "SELECT id, code, tenant_id FROM roles WHERE builtin=1"
            " AND code IN ('member', 'tenant_admin')").fetchall():
        grant = "nav:admin.models"
        if not con.execute(
                "SELECT 1 FROM role_resource_grants WHERE role_id=?"
                " AND resource_kind='menu'", (row["id"],)).fetchone():
            continue
        con.execute(
            "INSERT INTO role_resource_grants(id, tenant_id, role_id,"
            " resource_kind, resource_id, action)"
            " SELECT ?, ?, ?, 'menu', ?, 'view'"
            " WHERE NOT EXISTS (SELECT 1 FROM role_resource_grants"
            "  WHERE role_id=? AND resource_kind='menu' AND resource_id=?)",
            ("grant-menu-%s-admin.models" % row["id"], row["tenant_id"],
             row["id"], grant, row["id"], grant),
        )


_migrations.append(_migration_27)


def _migration_28(con: sqlite3.Connection) -> None:
    """External-system connection control store (change
    ``add-external-system-access``, tasks 2.1/2.2).

    The identity database is the *single* authority for a connection, its
    non-secret configuration, its secret **references** and its lifecycle, so a
    connection has one owner and every consumer (console, scene, runtime) reads
    the same row instead of a per-surface copy. What lives here is deliberately
    only metadata:

    * ``external_connections`` — one row per connection. ``config_json`` holds
      non-secret configuration *only*; secrets are stored in the existing
      ``credentials``/``credential_versions`` tables (tenant and personal) or in
      the platform-owned pair below, and referenced by ``id``, never copied.
      ``kind``/``scope``/``tenant_id``/``owner_user_id`` are fixed at creation by
      the service (``CHECK`` refuses a client that tries to move them).
    * ``external_connection_catalog_versions`` — the per-scope catalog revision
      and the ERP default pointer. The revision is the CAS token for
      concurrent default switches; ``default_connection_id`` is a real FK so a
      deleted connection cannot leave a dangling default.
    * ``external_connection_tenant_access`` — the explicit list of tenants a
      platform MCP connection is usable by. Default is *no* tenant, so granting
      is a deliberate row rather than an implied "everyone".
    * ``external_connection_tests`` — redacted test summaries bound to the
      config and secret versions they were run against.
    * ``external_connection_migrations`` — the idempotency ledger for the
      legacy-data migration (source hash + mapping), never a copy of a secret.
    * ``platform_connection_secrets`` + ``..._versions`` — platform-owned
      service secrets for shared MCP connections. A separate pair on purpose:
      reusing ``credentials`` would require a fabricated tenant (or a
      tenant=NULL generalization that weakens the existing tenant/personal
      isolation), and the platform secret must never be resolvable through the
      tenant credential API.

    The singletons are expressed as **partial** unique indexes over live rows,
    so a soft-deleted connection frees its slot while its audit trail survives:
    one OA per tenant, one email per *(tenant, owner)*, one override per
    *(tenant, platform row)*.
    """
    con.execute(
        """
        CREATE TABLE external_connections (
            id                 TEXT PRIMARY KEY,
            kind               TEXT NOT NULL,
            scope              TEXT NOT NULL,
            tenant_id          TEXT REFERENCES tenants(id),
            owner_user_id      TEXT REFERENCES users(id),
            name               TEXT NOT NULL,
            config_json        TEXT NOT NULL DEFAULT '{}',
            enabled            INTEGER NOT NULL DEFAULT 1,
            version            INTEGER NOT NULL DEFAULT 1,
            base_connection_id TEXT REFERENCES external_connections(id),
            source             TEXT NOT NULL DEFAULT 'created',
            deleted_at         INTEGER,
            created_by         TEXT NOT NULL,
            created_at         INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at         INTEGER NOT NULL DEFAULT (unixepoch()),
            CHECK (kind IN ('mcp', 'erp', 'oa', 'email')),
            CHECK (scope IN ('platform', 'tenant', 'personal')),
            CHECK (
                (scope = 'platform' AND kind = 'mcp'
                 AND tenant_id IS NULL AND owner_user_id IS NULL)
                OR (scope = 'tenant' AND kind IN ('mcp', 'erp', 'oa')
                    AND tenant_id IS NOT NULL AND owner_user_id IS NULL)
                OR (scope = 'personal' AND kind = 'email'
                    AND tenant_id IS NOT NULL AND owner_user_id IS NOT NULL)
            ),
            CHECK (base_connection_id IS NULL
                   OR (kind = 'mcp' AND scope = 'tenant')),
            CHECK (base_connection_id IS NULL OR base_connection_id <> id)
        )
        """
    )
    con.execute(
        "CREATE INDEX idx_ext_conn_scope"
        " ON external_connections(scope, tenant_id, owner_user_id, kind)"
    )
    con.execute(
        "CREATE INDEX idx_ext_conn_base"
        " ON external_connections(base_connection_id)"
    )
    con.execute(
        "CREATE UNIQUE INDEX idx_ext_conn_oa_singleton"
        " ON external_connections(tenant_id)"
        " WHERE kind='oa' AND deleted_at IS NULL"
    )
    con.execute(
        "CREATE UNIQUE INDEX idx_ext_conn_email_singleton"
        " ON external_connections(tenant_id, owner_user_id)"
        " WHERE kind='email' AND deleted_at IS NULL"
    )
    con.execute(
        "CREATE UNIQUE INDEX idx_ext_conn_override_singleton"
        " ON external_connections(tenant_id, base_connection_id)"
        " WHERE kind='mcp' AND scope='tenant' AND base_connection_id IS NOT NULL"
        " AND deleted_at IS NULL"
    )
    con.execute(
        """
        CREATE TABLE platform_connection_secrets (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL UNIQUE,
            ciphertext TEXT NOT NULL,
            active     INTEGER NOT NULL DEFAULT 1,
            version    INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at INTEGER NOT NULL DEFAULT (unixepoch())
        )
        """
    )
    con.execute(
        """
        CREATE TABLE platform_connection_secret_versions (
            platform_secret_id TEXT NOT NULL
                REFERENCES platform_connection_secrets(id),
            version            INTEGER NOT NULL,
            ciphertext         TEXT NOT NULL,
            action             TEXT NOT NULL,
            changed_by         TEXT NOT NULL,
            changed_at         INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (platform_secret_id, version)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE external_connection_secret_refs (
            connection_id      TEXT NOT NULL
                REFERENCES external_connections(id),
            slot               TEXT NOT NULL,
            credential_id      TEXT REFERENCES credentials(id),
            platform_secret_id TEXT REFERENCES platform_connection_secrets(id),
            secret_version     INTEGER NOT NULL,
            updated_at         INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (connection_id, slot),
            CHECK ((credential_id IS NULL) <> (platform_secret_id IS NULL))
        )
        """
    )
    con.execute(
        """
        CREATE TABLE external_connection_catalog_versions (
            scope_key             TEXT NOT NULL,
            kind                  TEXT NOT NULL,
            revision              INTEGER NOT NULL DEFAULT 1,
            default_connection_id TEXT REFERENCES external_connections(id),
            updated_at            INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (scope_key, kind)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE external_connection_tenant_access (
            platform_connection_id TEXT NOT NULL
                REFERENCES external_connections(id),
            tenant_id              TEXT NOT NULL REFERENCES tenants(id),
            enabled                INTEGER NOT NULL DEFAULT 1,
            revision               INTEGER NOT NULL DEFAULT 1,
            updated_at             INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (platform_connection_id, tenant_id)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE external_connection_tests (
            test_id             TEXT PRIMARY KEY,
            connection_id       TEXT NOT NULL
                REFERENCES external_connections(id),
            actor_user_id       TEXT NOT NULL,
            scope               TEXT NOT NULL,
            tenant_id           TEXT,
            config_version      INTEGER NOT NULL,
            secret_versions_json TEXT NOT NULL DEFAULT '{}',
            stage               TEXT NOT NULL DEFAULT '',
            result              TEXT NOT NULL,
            code                TEXT NOT NULL DEFAULT '',
            detail_json         TEXT NOT NULL DEFAULT '{}',
            created_at          INTEGER NOT NULL DEFAULT (unixepoch())
        )
        """
    )
    con.execute(
        "CREATE INDEX idx_ext_conn_tests"
        " ON external_connection_tests(connection_id, created_at)"
    )
    con.execute(
        """
        CREATE TABLE external_connection_migrations (
            batch_id       TEXT PRIMARY KEY,
            source_locator TEXT NOT NULL,
            source_hash    TEXT NOT NULL,
            scope_key      TEXT NOT NULL,
            mapping_json   TEXT NOT NULL DEFAULT '{}',
            result         TEXT NOT NULL,
            detail_json    TEXT NOT NULL DEFAULT '{}',
            created_at     INTEGER NOT NULL DEFAULT (unixepoch())
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX idx_ext_conn_migrations_source"
        " ON external_connection_migrations(source_hash, scope_key)"
    )
    # Short-lived create-idempotency ledger. The payload fingerprint is keyed
    # (HMAC with the deployment's credential key), so a stored row can prove
    # "same body" without being reversible to a password; ``result_json`` holds
    # the *redacted* projection only.
    con.execute(
        """
        CREATE TABLE external_connection_idempotency (
            key_id              TEXT NOT NULL,
            actor_user_id       TEXT NOT NULL,
            scope_key           TEXT NOT NULL,
            endpoint            TEXT NOT NULL,
            payload_fingerprint TEXT NOT NULL,
            result_json         TEXT NOT NULL,
            created_at          INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (actor_user_id, scope_key, endpoint, key_id)
        )
        """
    )


_migrations.append(_migration_28)


def _migration_29(con: sqlite3.Connection) -> None:
    """Per-scope maintenance windows (change ``add-external-system-access``,
    task 12.2).

    The migration design is 分范围维护窗口迁移: a window pauses configuration
    writes and new executions for **one scope** — platform, one tenant, or one
    member's personal connections — so the cutover is not an instance-wide
    freeze and a tenant that has finished is not held by a tenant that has not.

    ``scope_key`` is the same string the catalogue revisions and the migration
    ledger key on (``integrations.external.migration.scope_key``), so "which
    connections does this window hold" is one lookup and cannot disagree with
    "which connections does this ledger row belong to".

    History is kept rather than overwritten: a closed row is how an operator
    proves *when* writes were stopped and by whom, which is exactly the kind of
    statement the cutover record has to make. ``expires_at`` is not optional in
    practice — :func:`integrations.external.maintenance.begin_window` always
    sets one — so a crash or a forgotten ``close`` cannot leave a scope paused
    forever; an expired row simply stops being *active*.

    ``batch_id`` links the window to the migration batch it was opened for, so
    the import report and the window report can be read together.
    """
    con.execute(
        """
        CREATE TABLE external_connection_maintenance_windows (
            window_id  TEXT PRIMARY KEY,
            scope_key  TEXT NOT NULL,
            scope      TEXT NOT NULL,
            tenant_id  TEXT REFERENCES tenants(id),
            reason     TEXT NOT NULL DEFAULT '',
            batch_id   TEXT NOT NULL DEFAULT '',
            opened_by  TEXT NOT NULL,
            opened_at  INTEGER NOT NULL DEFAULT (unixepoch()),
            expires_at INTEGER NOT NULL,
            closed_by  TEXT,
            closed_at  INTEGER
        )
        """
    )
    # The hot lookup is "is there an open window for this scope_key", so the
    # index leads with scope_key and carries the open/expiry columns.
    con.execute(
        "CREATE INDEX idx_ext_conn_windows_scope"
        " ON external_connection_maintenance_windows(scope_key, closed_at,"
        " expires_at)"
    )


_migrations.append(_migration_29)


def _migration_30(con: sqlite3.Connection) -> None:
    """Open the 外部系统接入 console entry for built-in roles.

    Change ``add-external-system-access`` registers ``admin.external_connections``
    and the ``external.connections.read`` / ``manage`` catalogue ids, but leaves
    both the menu grant and the functional permissions *off* the built-in roles
    so the surface stays fail-closed until configuration is ready. Configuration
    is now accepted: new tenants pick the page up from ``BUILTIN_MENU_DEFAULTS``
    and the explicit default permission sets, and an already-provisioned tenant
    needs this one-shot backfill or the sidebar entry stays hidden forever.

    Two halves, both scoped to the built-in ``member`` / ``tenant_admin`` rows:

    * **Menu grant** — same discipline as ``_migration_27``: only roles that
      already carry a ``menu`` grant receive ``nav:admin.external_connections``.
      A role with none is still governed by functional permissions alone; adding
      a grant would switch gating *on* and hide the rest of its console.
    * **Permissions** — merge ``external.connections.read`` into both roles and
      ``external.connections.manage`` into ``tenant_admin`` only. Existing custom
      edits are preserved (union, never replace); a role that already holds an
      id is left untouched so a re-run is a no-op. ``external.connections.test``
      stays off — test/execute remain closed until readiness opens them.

    Custom roles are never touched: an administrator who never granted the page
    keeps that decision.
    """
    import json as _json

    page_grant = "nav:admin.external_connections"
    perms_by_code = {
        "member": ("external.connections.read",),
        "tenant_admin": (
            "external.connections.read",
            "external.connections.manage",
        ),
    }

    for row in con.execute(
            "SELECT id, code, tenant_id, permissions_json FROM roles"
            " WHERE builtin=1 AND code IN ('member', 'tenant_admin')"
            ).fetchall():
        if con.execute(
                "SELECT 1 FROM role_resource_grants WHERE role_id=?"
                " AND resource_kind='menu'", (row["id"],)).fetchone():
            con.execute(
                "INSERT INTO role_resource_grants(id, tenant_id, role_id,"
                " resource_kind, resource_id, action)"
                " SELECT ?, ?, ?, 'menu', ?, 'view'"
                " WHERE NOT EXISTS (SELECT 1 FROM role_resource_grants"
                "  WHERE role_id=? AND resource_kind='menu' AND resource_id=?)",
                ("grant-menu-%s-admin.external_connections" % row["id"],
                 row["tenant_id"], row["id"], page_grant, row["id"],
                 page_grant),
            )

        try:
            perms = _json.loads(row["permissions_json"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(perms, list):
            continue
        needed = perms_by_code.get(row["code"], ())
        missing = [p for p in needed if p not in perms]
        if not missing:
            continue
        con.execute(
            "UPDATE roles SET permissions_json=?, version=version+1"
            " WHERE id=?",
            (_json.dumps(sorted(set(perms) | set(missing))), row["id"]),
        )


_migrations.append(_migration_30)


def _migration_31(con: sqlite3.Connection) -> None:
    """Backfill ``todo.assign`` onto the built-in roles.

    Change ``add-todo-delegation`` adds the catalogue id, and new tenants pick it
    up from ``MEMBER_DEFAULT_PERMISSIONS`` / ``TENANT_ADMIN_DEFAULT_PERMISSIONS``.
    An already-provisioned tenant needs this one-shot backfill or its members can
    never hand a todo to a colleague — the delegation entry stays refused for
    every existing role.

    Scope is deliberately narrow:

    * only the built-in ``member`` / ``tenant_admin`` rows;
    * union, never replace — an administrator's existing edits survive;
    * a role that already holds the id is skipped, so a re-run is a no-op and
      does not keep bumping ``version``;
    * **nothing else is granted.** ``tenant.members.read`` in particular stays
      off ``member``: the delegation receiver picker reads its own narrow
      projection, so this migration must not double as a member-directory
      widening.

    Custom roles are never touched: an administrator who never granted
    delegation keeps that decision.
    """
    import json as _json

    needed = ("todo.assign",)
    for row in con.execute(
            "SELECT id, code, permissions_json FROM roles"
            " WHERE builtin=1 AND code IN ('member', 'tenant_admin')"
            ).fetchall():
        try:
            perms = _json.loads(row["permissions_json"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(perms, list):
            continue
        missing = [p for p in needed if p not in perms]
        if not missing:
            continue
        con.execute(
            "UPDATE roles SET permissions_json=?, version=version+1 WHERE id=?",
            (_json.dumps(sorted(set(perms) | set(missing))), row["id"]),
        )


_migrations.append(_migration_31)


def _migration_32(con: sqlite3.Connection) -> None:
    """Record when a channel instance first established its sender identity.

    Change ``auto-bind-channel-sender``: an instance that has never been bound
    lets its **first private-chat sender** claim it — that is what makes "the
    account that set the channel up just works" true without a binding code.

    "First" has to survive an unlink, or the rule becomes a takeover window:
    whoever messages next after the owner unbinds would claim the instance. So
    the moment *any* route is established (a redeemed binding code, a console
    link, a scanner-identity bind at creation, or the automatic claim itself),
    the instance is stamped here and can never be claimed again. The binding
    code / console flow stays available as the recovery path.

    Nullable with no default, and ``NULL`` is the claimable state. Rows that
    predate this column are therefore claimable, which is deliberate: the
    deliverable this implements is "an existing, never-bound channel starts
    working on the first message", and a deployment that had to re-create its
    channels first would not get it. What the backfill does close is the case
    the column can still *see*: an instance that currently holds a route has
    been bound, so it is stamped and can never be re-claimed by a stranger.
    An instance unlinked *before* this upgrade is indistinguishable from one
    that was never bound, and stays claimable — a documented residual, bounded
    by the audit row every claim writes and by the console's unlink.
    """
    con.executescript(
        """
        ALTER TABLE tenant_channel_instances ADD COLUMN sender_binding_at INTEGER;
        UPDATE tenant_channel_instances SET sender_binding_at = unixepoch()
         WHERE id IN (SELECT instance_id FROM personal_channel_links);
        """
    )


_migrations.append(_migration_32)


class IdentityStoreError(RuntimeError):
    """Raised when the identity store cannot be opened or migrated."""


class ConnGuard:
    """Context-manager wrapper that owns a ``sqlite3.Connection``.

    A bare ``sqlite3.Connection`` used as a context manager only commits or
    rolls back on ``__exit__``; it does **not** close the connection. That means
    every ``with conn:`` block leaks a file descriptor until the process runs
    out of open handles, which shows up as a wedge (``identity_db_unavailable``
    / 503) after a few hundred requests.

    ``ConnGuard`` mirrors the wrapped connection for normal use (``execute``,
    ``commit``, ``row_factory``, ``executescript``, ``fetchone``, ...) and
    closes the underlying connection in ``__exit__``, so connection lifetime is
    bounded. On error it rolls back before closing to avoid leaving a dangling
    write lock.
    """

    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def __getattr__(self, name):
        # Delegate any sqlite3.Connection attribute/method transparently.
        return getattr(self._con, name)

    def __enter__(self) -> "ConnGuard":
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._con is None:
                return False
            try:
                # Match the semantics of a bare ``sqlite3.Connection`` used as a
                # context manager: commit on success, rollback on error. We
                # additionally CLOSE the connection. (Most callers commit
                # explicitly, but some rely on the default-commit behaviour, so
                # we must not change it.)
                if exc_type is None:
                    self._con.commit()
                else:
                    self._con.rollback()
            except sqlite3.Error:
                pass
        finally:
            self._con.close()
            self._con = None
        return False


class IdentityStore:
    """Thin, thread-safe wrapper over a SQLite ``identity.db``.

    Ownership of the connection and of transaction boundaries is left to the
    caller via ``connect()`` (a context manager) and ``execute()``. The store
    never opens its own long-lived connection so the service layer can bundle
    identity writes + audit in a single transaction.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._migrate()

    @property
    def db_path(self) -> str:
        return self._db_path

    def _migrate(self) -> None:
        if not self._db_path:
            raise IdentityStoreError("identity.db path is empty")
        parent = os.path.dirname(os.path.abspath(self._db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self.connect() as con:
            cursor = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            exists = {row["name"] for row in cursor.fetchall()}
            if "schema_migrations" not in exists:
                con.executescript(
                    "CREATE TABLE schema_migrations("
                    " version INTEGER NOT NULL,"
                    " applied_at INTEGER NOT NULL DEFAULT (unixepoch()))"
                )
            applied = {
                row["version"]
                for row in con.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for version in migration_versions():
                if version not in applied:
                    con.execute("BEGIN")
                    _migrations[version - 1](con)
                    con.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)",
                        (version,),
                    )
                    con.commit()

    def connect(self) -> "ConnGuard":
        """Open a connection with foreign keys enabled. Use as a context manager.

        SQLite connections are cheap here and thread-local usage is the norm;
        a fresh connection is created per call. The caller controls the
        transaction with explicit BEGIN/COMMIT.

        Returns a :class:`ConnGuard` (a context manager) instead of a raw
        ``sqlite3.Connection``. A raw ``sqlite3.Connection`` only commits or
        rolls back on ``__exit__`` and NEVER closes the connection, so using it
        with ``with`` leaks one connection per call. ``ConnGuard`` closes the
        underlying connection on exit, preventing unbounded connection growth
        (which would otherwise wedge the identity store under sustained load).
        """
        con = sqlite3.connect(self._db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        return ConnGuard(con)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self.connect() as con:
            cursor = con.execute(sql, params)
            rows = cursor.fetchall()
            con.commit()  # read-only SELECTs commit as a no-op
            return rows
