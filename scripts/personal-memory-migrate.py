#!/usr/bin/env python3
"""Local operator recovery. Defaults to read-only preview; never infers an owner."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ReadOnlyCatalog:
    """Only the trusted catalog projections needed to resolve owner roots."""
    def __init__(self, db):
        self.db = Path(db).resolve().as_uri() + '?mode=ro'

    def rows(self, sql, params=()):
        with sqlite3.connect(self.db, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only = ON')
            return [dict(row) for row in conn.execute(sql, params)]

    def tenant_shared_root(self, tenant):
        rows = self.rows('SELECT shared_root FROM tenants WHERE id=? AND active=1', (tenant,))
        return rows[0]['shared_root'] if rows else None

    def tenant_shared_roots(self):
        return self.rows('SELECT id,shared_root FROM tenants')

    def list_agent_bindings(self, tenant):
        return self.rows('SELECT * FROM agent_bindings WHERE tenant_id=?', (tenant,))

    def verify_owner(self, tenant, user):
        return bool(self.rows('SELECT m.id FROM memberships m JOIN users u ON u.id=m.user_id '
                              'JOIN tenants t ON t.id=m.tenant_id WHERE m.tenant_id=? AND m.user_id=? '
                              'AND m.active=1 AND u.active=1 AND t.active=1', (tenant, user)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Deployment config.json (read only)')
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--mode', choices=('preview', 'apply', 'rollback'), default='preview')
    parser.add_argument('--plan', help='Previously reviewed JSON preview, required by apply')
    args = parser.parse_args()
    import config
    from auth.service import _catalog_identity, identity_db_path, IdentityService
    from common.runtime_identity import RuntimeIdentity, use_identity
    from agent.memory.personal import PersonalMemoryService
    from agent.memory.migration import PersonalMemoryMigration
    # No load_config startup hooks, credential logging, schema migrations or
    # tenant creation. The normal root validator still applies to this catalog.
    config.config = config.Config(json.loads(Path(args.config).read_text()))
    catalog = ReadOnlyCatalog(identity_db_path())
    if not catalog.verify_owner(args.tenant, args.user):
        parser.error('An active user membership in the selected tenant is required')
    token = _catalog_identity.set(catalog if args.mode == 'preview' else IdentityService(identity_db_path()))
    try:
        with use_identity(RuntimeIdentity(tenant_id=args.tenant, user_id=args.user)):
            migration = PersonalMemoryMigration(PersonalMemoryService())
            if args.mode == 'apply':
                if not args.plan:
                    parser.error('--plan is required by apply')
                result = migration.apply(json.loads(Path(args.plan).read_text()))
            elif args.mode == 'rollback':
                if not args.plan:
                    parser.error('--plan is required by rollback')
                plan = json.loads(Path(args.plan).read_text())
                if (plan.get('tenant_id'), plan.get('user_id')) != (args.tenant, args.user):
                    parser.error('The plan belongs to another owner')
                result = migration.rollback(plan['batch_id'])
            else:
                result = migration.preview()
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        _catalog_identity.reset(token)


if __name__ == '__main__':
    main()
