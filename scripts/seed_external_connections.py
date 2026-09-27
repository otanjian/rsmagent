#!/usr/bin/env python3
# encoding:utf-8
"""Seed the external-connection control plane with OneAgent's own data.

Why this exists
---------------
Task 4.4 of ``add-external-system-access`` needs the version/secret binding
proved against data the product actually meets, not against ``example.com``
shapes invented for the test. The fixtures live in
``tests/_external_oneagent.py`` and are transcribed from a OneAgent deployment;
this script writes the same set into a real ``identity.db`` so a running
instance — and the console page — can be exercised against rows that came from
the system being replaced.

What it writes
--------------
One connection per type, through :class:`ExternalConnectionService`, so every
write goes through the same validation, credential encryption, version bump and
audit event a console request would produce. Nothing is inserted with raw SQL:
a seeder that bypasses the service would not prove the service can accept this
data, which is half the point.

Scopes follow the product's own rules rather than convenience:

* ``erp``, ``oa``, ``mcp`` — tenant scope, in the tenant named by
  ``--tenant-code``;
* ``email`` — personal scope, owned by the member named by ``--username``
  (the schema allows exactly one mailbox per member). Ownership is derived from
  the session by the service, which is why the mailbox is written as that
  member and not as the administrator.

Authorization, and why ``--grant-access`` exists
------------------------------------------------
Built-in ``member`` / ``tenant_admin`` now carry ``external.connections.read``
(and ``manage`` for the admin) so the console entry opens after upgrade. A
seeded *member* still needs ``manage`` to create/update their personal mailbox
through this script, which is why ``--grant-access`` (on by default) ensures a
custom role carrying both permissions and binds it to ``--username``,
preserving the member's existing roles. Pass ``--no-grant-access`` to seed the
data only and leave the member without the manage grant.

Idempotence
-----------
Re-running updates the existing row for the same ``(scope, tenant, owner, kind,
name)`` through the service's compare-and-swap ``expected_version``, or creates
it when absent. It never deletes, and it never touches a row whose name it does
not recognise, so a deployment that already has hand-made connections keeps
them.

Usage
-----
::

    export COW_CREDENTIAL_MASTER_KEY=<the deployment's credential key>
    python scripts/seed_external_connections.py \\
        --tenant-code test15 --username RC001

Add ``--dry-run`` to validate the fixtures and print what would change without
writing anything. Add ``--kind erp`` (repeatable) to seed only some types.

Exit code is 0 when every requested connection is present and valid afterwards.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from integrations.external import registry  # noqa: E402
from integrations.external.errors import ExternalConnectionError  # noqa: E402
from tests import _external_oneagent as oneagent  # noqa: E402

PASS, FAIL = "PASS", "FAIL"

#: The permissions the console page and the routes demand. Mirrors
#: ``auth/capability_matrix.py`` / ``channel/web/route_registry.py``; both are
#: named so a rename there is a one-line change here.
READ_PERMISSION = "external.connections.read"
MANAGE_PERMISSION = "external.connections.manage"

#: The custom role ``--grant-access`` ensures.
ACCESS_ROLE_CODE = "external_connections_access"

_results: List[Tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, PASS if ok else FAIL, detail))
    print("  [%-4s] %s%s" % (PASS if ok else FAIL, name,
                             ("  -- %s" % detail) if detail else ""))
    return ok


# --------------------------------------------------------------------------
# Database / identity resolution
# --------------------------------------------------------------------------

def load_env_file(path: str) -> None:
    """Load ``KEY=VALUE`` lines into the process environment.

    The credential key lives only in the environment (``auth/crypto.py`` refuses
    a fallback), and the application itself does not read a dotenv file — its
    launcher exports the variables. So this reads the same file the launcher
    would source, and it does **not** override an already-exported value: a
    caller who exported the key explicitly is the more specific intent.
    """
    if not path:
        return
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def identity_db_path(data_dir: str, explicit: str) -> str:
    """Resolve the database the way ``get_external_connection_service`` does.

    Not re-invented: the console, the agent and this script must land on the
    same file, and the only way to be sure is to use the same precedence — an
    explicit path, then the configured ``identity_db_path``, then
    ``<data root>/identity.db``.
    """
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    os.environ.setdefault("COW_DATA_DIR",
                          os.path.abspath(os.path.expanduser(data_dir)))
    from config import conf, get_data_root
    configured = conf().get("identity_db_path")
    if configured:
        return os.path.abspath(os.path.expanduser(str(configured)))
    return os.path.join(get_data_root(), "identity.db")


def _query(db_path: str, sql: str, params: Tuple[Any, ...]):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def find_tenant(db_path: str, code: str) -> str:
    rows = _query(db_path, "SELECT id FROM tenants WHERE code=?", (code,))
    if not rows:
        raise SystemExit("no tenant with code %r in %s" % (code, db_path))
    return rows[0]["id"]


def find_user(db_path: str, username: str) -> str:
    rows = _query(db_path, "SELECT id FROM users WHERE username=?", (username,))
    if not rows:
        raise SystemExit("no user with username %r in %s" % (username, db_path))
    return rows[0]["id"]


def find_tenant_admin(db_path: str, tenant_id: str) -> str:
    """A real account to attribute the tenant-scoped writes to.

    The tenant's own administrator, not a synthetic seeder: every audit event
    must name an account that exists, and the tenant admin is the account
    entitled to define the tenant's connections.
    """
    rows = _query(
        db_path,
        "SELECT u.id FROM users u JOIN memberships m ON m.user_id=u.id"
        " JOIN membership_roles mr ON mr.membership_id=m.id"
        " JOIN roles r ON r.id=mr.role_id WHERE m.tenant_id=?"
        " AND r.code='tenant_admin' AND m.active=1 LIMIT 1", (tenant_id,))
    if not rows:
        raise SystemExit("tenant %s has no active tenant_admin to attribute the"
                         " write to" % tenant_id)
    return rows[0]["id"]


def find_membership(db_path: str, tenant_id: str, user_id: str):
    rows = _query(db_path, "SELECT * FROM memberships WHERE tenant_id=? AND"
                           " user_id=?", (tenant_id, user_id))
    return dict(rows[0]) if rows else None


def existing_connection(db_path: str, *, kind: str, scope: str,
                        tenant_id: Optional[str],
                        owner_user_id: Optional[str],
                        name: str) -> Optional[Dict[str, Any]]:
    """The row this seed would update, matched on the identity of a connection.

    ``(kind, scope, tenant, owner, name)`` rather than the generated id: an
    operator re-running the seeder after a restore has the same logical
    connection under a different id, and matching on the id would create a
    duplicate instead.
    """
    rows = _query(
        db_path,
        "SELECT id, version FROM external_connections WHERE kind=? AND scope=?"
        " AND COALESCE(tenant_id,'')=? AND COALESCE(owner_user_id,'')=? AND name=?",
        (kind, scope, tenant_id or "", owner_user_id or "", name))
    return dict(rows[0]) if rows else None


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------

def build_plan(only: List[str]) -> List[Dict[str, Any]]:
    """The connections this run will ensure, in a stable order.

    Built before anything is written so ``--dry-run`` and a real run agree on
    what is being asked for.
    """
    wanted = [str(k) for k in (only or [])]
    plan: List[Dict[str, Any]] = []
    for kind, entry in oneagent.by_kind().items():
        if wanted and kind not in wanted:
            continue
        personal = kind == "email"
        plan.append({
            "kind": kind,
            "entry": entry,
            "scope": registry.SCOPE_PERSONAL if personal else registry.SCOPE_TENANT,
            "personal": personal,
        })
    return plan


def validate_fixtures(plan: List[Dict[str, Any]]) -> bool:
    ok = True
    for item in plan:
        kind = str(item["kind"])
        entry = item["entry"]
        try:
            normalized = registry.validate_config(kind, entry["config"])
        except ExternalConnectionError as error:
            ok = check("%s config validates" % kind, False,
                       "%s: %s" % (error.code, error)) and ok
            continue
        slots = registry.secret_slots(kind)
        unknown = [s for s in entry["secrets"] if s not in slots]
        check("%s config and secret slots valid" % kind, not unknown,
              "unknown slots: %s" % unknown if unknown else
              "slots=%s" % sorted(entry["secrets"]))
        ok = ok and not unknown
        del normalized
    return ok


# --------------------------------------------------------------------------
# Access grant
# --------------------------------------------------------------------------

def grant_access(identity, db_path: str, *, tenant_id: str, admin_user_id: str,
                 member_user_id: str, dry_run: bool) -> None:
    """Ensure a role carrying both permissions and bind it to the member.

    ``manage`` implies the ability to see the page, but the console gate reads
    ``read`` explicitly, so both are granted: a member who could create a
    connection but not list one would be a confusing half-state.
    """
    import json

    rows = _query(db_path, "SELECT * FROM roles WHERE tenant_id=? AND code=?",
                  (tenant_id, ACCESS_ROLE_CODE))
    role = dict(rows[0]) if rows else None
    wanted = sorted({READ_PERMISSION, MANAGE_PERMISSION})

    if dry_run:
        check("access role %r" % ACCESS_ROLE_CODE,
              True, "would %s" % ("update" if role else "create"))
        return

    if role is None:
        created = identity.create_role(
            admin_user_id, tenant_id, ACCESS_ROLE_CODE, "系统接入",
            permissions=wanted)
        check("access role %r created" % ACCESS_ROLE_CODE, True,
              "id=%s" % created["id"])
    else:
        current = json.loads(role["permissions_json"] or "[]")
        merged = sorted(set(current) | set(wanted))
        identity.update_role(
            admin_user_id, tenant_id, role["id"], role["name"], merged,
            expected_version=int(role["version"]))
        check("access role %r updated" % ACCESS_ROLE_CODE, True,
              "permissions=%s" % merged)

    membership = find_membership(db_path, tenant_id, member_user_id)
    if membership is None:
        check("member bound to the access role", False,
              "member is not in the tenant")
        return
    bound = sorted(set(_role_codes(db_path, membership["id"])) |
                   {ACCESS_ROLE_CODE})
    identity.update_member(
        actor_user_id=admin_user_id, tenant_id=tenant_id,
        member_id=membership["id"], display_name=membership["display_name"],
        active=bool(membership["active"]), roles=bound,
        department_id=membership["department_id"],
        position_text=membership["position_text"] or "",
        expected_version=int(membership["version"]))
    check("member bound to the access role", True, "roles=%s" % bound)


def _role_codes(db_path: str, membership_id: str) -> List[str]:
    rows = _query(
        db_path,
        "SELECT r.code FROM roles r JOIN membership_roles mr ON mr.role_id=r.id"
        " WHERE mr.membership_id=?", (membership_id,))
    return [row["code"] for row in rows]


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

def ensure(service, db_path: str, item: Dict[str, Any], *,
           tenant_id: str, member_user_id: Optional[str],
           admin_user_id: str, dry_run: bool) -> bool:
    """Create or update one connection through the service."""
    kind = str(item["kind"])
    scope = str(item["scope"])
    personal = bool(item["personal"])
    entry = item["entry"]
    name = str(entry["name"])
    tenant = None if scope == registry.SCOPE_PLATFORM else tenant_id
    owner = member_user_id if personal else None
    # The service fixes a personal connection's owner from the actor, so the
    # mailbox must be written *as* that member; the tenant connections are
    # written as the tenant's administrator.
    actor = member_user_id if personal else admin_user_id

    found = existing_connection(db_path, kind=kind, scope=scope,
                                tenant_id=tenant, owner_user_id=owner, name=name)
    label = "%s %r" % (kind, name)
    if dry_run:
        check(label, True, "would %s" % ("update" if found else "create"))
        return True
    try:
        if found is None:
            created = service.create_connection(
                actor_user_id=actor, scope=scope, tenant_id=tenant, kind=kind,
                name=name, config=dict(entry["config"]),
                secrets=dict(entry["secrets"]))
            check(label, True, "created id=%s v%s"
                  % (created["id"], created["version"]))
            return True
        updated = service.update_connection(
            actor_user_id=actor, scope=scope, connection_id=found["id"],
            expected_version=int(found["version"]), tenant_id=tenant,
            config=dict(entry["config"]), secrets=dict(entry["secrets"]))
        check(label, True, "updated id=%s v%s"
              % (updated["id"], updated["version"]))
        return True
    except ExternalConnectionError as error:
        check(label, False, "%s: %s" % (error.code, error))
        return False


def run(args) -> int:
    load_env_file(args.env_file)
    db_path = identity_db_path(args.data_dir, args.identity_db)
    print("identity.db: %s" % db_path)

    if not os.environ.get("COW_CREDENTIAL_MASTER_KEY"):
        check("credential key present", False,
              "COW_CREDENTIAL_MASTER_KEY is not set; a credential cannot be"
              " stored without it")
        return 1
    check("credential key present", True)

    tenant_id = find_tenant(db_path, args.tenant_code)
    check("tenant %r resolved" % args.tenant_code, bool(tenant_id), tenant_id)
    admin_user_id = find_tenant_admin(db_path, tenant_id)
    check("tenant admin resolved", bool(admin_user_id), admin_user_id)
    member_user_id = find_user(db_path, args.username)
    check("member %r resolved" % args.username, bool(member_user_id),
          member_user_id)

    plan = build_plan(args.kind)
    check("fixtures planned", bool(plan),
          "kinds=%s" % [i["kind"] for i in plan])
    if not validate_fixtures(plan):
        print("\nrefusing to write: the fixtures did not validate")
        return 1

    if args.grant_access:
        from auth.service import IdentityService as _Identity
        identity = None if args.dry_run else _Identity(db_path)
        grant_access(identity, db_path, tenant_id=tenant_id,
                     admin_user_id=admin_user_id, member_user_id=member_user_id,
                     dry_run=args.dry_run)

    if args.dry_run:
        for item in plan:
            ensure(None, db_path, item, tenant_id=tenant_id,
                   member_user_id=member_user_id, admin_user_id=admin_user_id,
                   dry_run=True)
        print("\ndry run: nothing written")
        return 0

    from auth.service import IdentityService
    from integrations.external.service import ExternalConnectionService

    service = ExternalConnectionService(IdentityService(db_path))
    for item in plan:
        ensure(service, db_path, item, tenant_id=tenant_id,
               member_user_id=member_user_id, admin_user_id=admin_user_id,
               dry_run=False)

    failures = sum(1 for _, verdict, _ in _results if verdict == FAIL)
    print("\n%d checks, %d failed" % (len(_results), failures))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed the external-connection control plane with"
                    " OneAgent's own configuration data.")
    parser.add_argument("--data-dir", default=REPO_ROOT,
                        help="data root holding config.json / identity.db")
    parser.add_argument("--identity-db", default="",
                        help="explicit identity.db path (overrides --data-dir)")
    parser.add_argument("--env-file", default="~/.cow/.env",
                        help="dotenv file to read COW_CREDENTIAL_MASTER_KEY from"
                             " (does not override an exported value)")
    parser.add_argument("--tenant-code", default="test15",
                        help="tenant the tenant-scoped connections belong to")
    parser.add_argument("--username", default="RC001",
                        help="member owning the personal mailbox connection")
    parser.add_argument("--kind", action="append", default=[],
                        help="only this kind; repeatable (erp/oa/mcp/email)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and report without writing")
    parser.add_argument("--grant-access", dest="grant_access",
                        action="store_true", default=True,
                        help="ensure the access role and bind it to --username"
                             " (default)")
    parser.add_argument("--no-grant-access", dest="grant_access",
                        action="store_false",
                        help="seed the data only; leave the member unauthorized")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
