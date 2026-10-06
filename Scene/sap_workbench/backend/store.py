"""Scene-owned SQLite settings and separately encrypted MCP credentials."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import hashlib
import time
import uuid
from urllib.parse import urlsplit

from .configuration import DEFAULT_CONFIG, WorkbenchError, validate_config

ALLOCATION_ERRORS = frozenset({
    '', 'runtime_unavailable', 'browser_unavailable', 'opencode_session_mismatch',
    'opencode_start_timeout', 'browser_service_unavailable', 'opencode_runtime_missing',
    'opencode_assets_missing', 'project_directory_missing',
})

class WorkbenchStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS "cj-sap_workbench-configs" (
                    tenant_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    config_json TEXT NOT NULL, updated_by TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS "cj-sap_workbench-credentials" (
                    tenant_id TEXT PRIMARY KEY, binding TEXT NOT NULL,
                    ciphertext TEXT NOT NULL,
                    FOREIGN KEY(tenant_id) REFERENCES "cj-sap_workbench-configs"(tenant_id)
                );
                CREATE TABLE IF NOT EXISTS "cj-sap_workbench-session_links" (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    config_version INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
                    coding_session_id TEXT, service_id TEXT, remote_session_id TEXT,
                    browser_id TEXT, target_id TEXT, generation INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL DEFAULT 'creating', control TEXT NOT NULL DEFAULT 'paused',
                    sap_user TEXT, sap_client TEXT, login_generation INTEGER NOT NULL DEFAULT 0,
                    credential_ref TEXT, mcp_connection_id TEXT,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    UNIQUE(tenant_id, user_id, agent_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS "cj-sap_workbench-actions" (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    tool_call_id TEXT, action_kind TEXT NOT NULL,
                    parameter_digest TEXT NOT NULL, approval_ref TEXT,
                    state TEXT NOT NULL, evidence_ref TEXT,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES "cj-sap_workbench-session_links"(id),
                    UNIQUE(session_id, request_id)
                );
            ''')
            # Scene-only additive migration, serialized across request threads.
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info("cj-sap_workbench-session_links")')}
            for name, definition in (
                ('allocation_stage', "TEXT NOT NULL DEFAULT 'reserved'"),
                ('allocation_error', "TEXT NOT NULL DEFAULT ''"),
                ('display_mode', "TEXT NOT NULL DEFAULT 'screen'"),
            ):
                if name not in columns:
                    db.execute(f'ALTER TABLE "cj-sap_workbench-session_links" ADD COLUMN {name} {definition}')
            columns = {row['name'] for row in db.execute('PRAGMA table_info("cj-sap_workbench-actions")')}
            for name, definition in (
                ('page_revision', "TEXT NOT NULL DEFAULT ''"),
                ('context_digest', "TEXT NOT NULL DEFAULT ''"),
                ('target_ref', "TEXT NOT NULL DEFAULT ''"),
                ('control_epoch', 'INTEGER NOT NULL DEFAULT 0'),
                ('config_version', 'INTEGER NOT NULL DEFAULT 0'),
                ('receipt_json', "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in columns:
                    db.execute(f'ALTER TABLE "cj-sap_workbench-actions" ADD COLUMN {name} {definition}')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def read_config(self, tenant_id):
        if not tenant_id:
            raise WorkbenchError("missing_tenant", 403)
        with self._connection() as db:
            row = db.execute('SELECT version, config_json FROM "cj-sap_workbench-configs" '
                             'WHERE tenant_id=?', (tenant_id,)).fetchone()
            credential = db.execute('SELECT binding FROM "cj-sap_workbench-credentials" '
                                    'WHERE tenant_id=?', (tenant_id,)).fetchone()
        if row is None:
            return {"version": 0, "config": deepcopy(DEFAULT_CONFIG), "mcp_password_configured": False}
        clean = validate_config(json.loads(row["config_json"]))
        return {"version": row["version"], "config": clean,
                "mcp_password_configured": bool(credential and credential["binding"] == self._binding(clean))}

    def reserve_session(self, tenant_id, user_id, agent_id, request_id, saved, project, *,
                        display_mode='screen', coding=None):
        """Reserve one workbench binding.

        ``coding`` carries a platform coding session resolved elsewhere:
        ``session_id``/``external_session_id`` and the ``service_id`` that derived
        them. Callers normally omit it, because the scene derives the very ids the
        platform will: same ``derive_ids`` function, same operator-configured
        ``service_id``. That is the whole point -- the id the project plugin
        reports as its OpenCode ``sessionID`` has to be the platform's id, so a
        tool call can resolve back to its owner. An earlier revision used a
        scene-private ``service_id`` and produced ids the platform would never
        derive, which left the bridge with nothing to match.

        The derivation is local on purpose: ``CodingSessionService.reserve`` would
        also create upstream state, and the page reaches the platform's coding
        entry itself for that. ``coding`` therefore exists only as an explicit
        override for a caller or test that has already resolved the ids.
        """
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            raise WorkbenchError("invalid_request_id")
        if display_mode not in {'screen', 'iframe'}:
            raise ValueError('invalid display mode')
        digest = hashlib.sha256(f"{tenant_id}\0{user_id}\0{agent_id}\0{request_id}".encode()).hexdigest()[:32]
        binding = "sap_" + digest
        if coding is None:
            from agent.coding import resolve_settings
            from agent.coding.sessions import derive_ids
            service_id = resolve_settings().service_id
            coding_id, remote_id = derive_ids(service_id=service_id, tenant_id=tenant_id, user_id=user_id,
                                              agent_id=agent_id, request_id=request_id)
        else:
            service_id = str(coding['service_id'])
            coding_id = str(coding['session_id'])
            remote_id = str(coding['external_session_id'])
        now = int(time.time())
        snapshot = {"config": saved["config"], "project": project}
        with self._connection() as db:
            db.execute('INSERT OR IGNORE INTO "cj-sap_workbench-session_links" '
                       '(id,tenant_id,user_id,agent_id,request_id,config_version,snapshot_json,'
                       'coding_session_id,service_id,remote_session_id,browser_id,created_at,updated_at,display_mode) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (binding, tenant_id, user_id, agent_id, request_id, saved["version"],
                        json.dumps(snapshot), coding_id, service_id, remote_id, binding, now, now, display_mode))
        return self.session(tenant_id, user_id, binding)

    def session(self, tenant_id, user_id, binding):
        with self._connection() as db:
            row = db.execute('SELECT * FROM "cj-sap_workbench-session_links" '
                             'WHERE id=? AND tenant_id=? AND user_id=?', (binding, tenant_id, user_id)).fetchone()
        if not row:
            raise WorkbenchError("session_not_found", 404)
        result = dict(row)
        result["snapshot"] = json.loads(result.pop("snapshot_json"))
        return result

    def sessions(self, tenant_id, user_id):
        with self._connection() as db:
            return [dict(row) for row in db.execute('SELECT id,remote_session_id,state,control,allocation_stage,allocation_error,created_at,updated_at '
                    'FROM "cj-sap_workbench-session_links" WHERE tenant_id=? AND user_id=? '
                    'ORDER BY updated_at DESC LIMIT 50', (tenant_id, user_id))]

    def binding_for_session(self, remote_session_id):
        """Resolve an owner binding from an OpenCode session id.

        Trusted runtime only, and the ONLY way a project plugin's tool call can
        name a workbench: the plugin knows nothing but its ``sessionID`` and the
        project directory, so the owner (tenant, user and binding) must be
        looked up here instead of being taken from the caller's parameters.

        The derived session ids embed the tenant, user, agent and request id, so
        a row is unique. An ambiguous match is refused rather than guessed: two
        candidates mean the identifier no longer identifies one binding, and
        picking either would let one owner's call reach another's browser.
        """
        if not isinstance(remote_session_id, str) or not 1 <= len(remote_session_id) <= 128:
            return None
        with self._connection() as db:
            rows = db.execute('SELECT * FROM "cj-sap_workbench-session_links" '
                              'WHERE remote_session_id=? AND state!=\'closed\' '
                              'ORDER BY updated_at DESC LIMIT 2', (remote_session_id,)).fetchall()
        if len(rows) != 1:
            return None
        result = dict(rows[0])
        result['snapshot'] = json.loads(result.pop('snapshot_json'))
        return result

    def active_sessions(self, tenant_id, user_id):
        """All owner bindings, including stale reservations beyond history's page."""
        with self._connection() as db:
            rows = db.execute('SELECT * FROM "cj-sap_workbench-session_links" '
                              'WHERE tenant_id=? AND user_id=? AND state!=\'closed\'',
                              (tenant_id, user_id)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['snapshot'] = json.loads(item.pop('snapshot_json'))
            result.append(item)
        return result

    def update_session(self, tenant_id, user_id, binding, **values):
        if set(values) - {"state", "control", "target_id", "generation", "sap_user", "sap_client", "login_generation", "allocation_stage", "allocation_error", "display_mode"}:
            raise ValueError("invalid session update")
        if 'display_mode' in values and values['display_mode'] not in {'screen', 'iframe'}:
            raise ValueError('invalid display mode')
        if 'allocation_stage' in values and values['allocation_stage'] not in {
            'reserved', 'host_starting', 'conversation_ready', 'browser_starting', 'ready', 'paused',
        }:
            raise ValueError('invalid allocation stage')
        if 'allocation_error' in values and values['allocation_error'] not in ALLOCATION_ERRORS:
            raise ValueError('invalid allocation error')
        self.session(tenant_id, user_id, binding)
        # Superseded bindings are terminal. A late startup/monitor cleanup
        # cannot turn their history back into a resumable reservation.
        values["updated_at"] = int(time.time())
        assignments = ["state=CASE WHEN state='closed' THEN state ELSE ? END" if key == 'state'
                       else f'{key}=?' for key in values]
        with self._connection() as db:
            db.execute('UPDATE "cj-sap_workbench-session_links" SET ' + ','.join(assignments)
                       + ' WHERE id=? AND tenant_id=? AND user_id=?', (*values.values(), binding, tenant_id, user_id))

    def admit_action(self, binding, call_id, generation, kind, arguments):
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        now = int(time.time())
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM "cj-sap_workbench-actions" WHERE session_id=? AND request_id=?',
                             (binding, call_id)).fetchone()
            if old:
                raise WorkbenchError("action_already_dispatched", 409)
            action = "act_" + uuid.uuid4().hex
            db.execute('INSERT INTO "cj-sap_workbench-actions" '
                       '(id,session_id,request_id,generation,tool_call_id,action_kind,parameter_digest,state,created_at,updated_at) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (action, binding, call_id, generation, call_id, kind, digest, "running", now, now))
        return action

    def finish_action(self, action, state):
        if state not in {"succeeded", "failed", "unknown", "cancelled"}:
            raise ValueError("invalid action state")
        with self._connection() as db:
            db.execute('UPDATE "cj-sap_workbench-actions" SET state=?,updated_at=? WHERE id=?',
                       (state, int(time.time()), action))

    def recover_actions(self, binding):
        with self._connection() as db:
            db.execute('UPDATE "cj-sap_workbench-actions" SET state=\'unknown\',updated_at=? '
                       'WHERE session_id=? AND state=\'running\'', (int(time.time()), binding))
            db.execute('UPDATE "cj-sap_workbench-actions" SET state=\'failed\',updated_at=? '
                       'WHERE session_id=? AND state=\'preparing\'', (int(time.time()), binding))

    def reserve_commit(self, tenant, user, binding, call_id, *, generation, kind,
                       digest, context_digest, target, epoch, revision, config_version):
        """One durable preparation per trusted owner/request; never replay it."""
        self.session(tenant, user, binding)
        now = int(time.time())
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM "cj-sap_workbench-actions" WHERE session_id=? AND request_id=?',
                             (binding, call_id)).fetchone()
            if old:
                if old['action_kind'] != kind or old['parameter_digest'] != digest or old['context_digest'] != context_digest:
                    raise WorkbenchError('commit_request_mismatch', 409)
                if old['state'] != 'prepared':
                    raise WorkbenchError('commit_preparing' if old['state'] == 'preparing' else 'action_already_dispatched', 409)
                return self._commit_row(old), False
            action = 'act_' + uuid.uuid4().hex
            db.execute('INSERT INTO "cj-sap_workbench-actions" '
                '(id,session_id,request_id,generation,tool_call_id,action_kind,parameter_digest,state,created_at,updated_at,'
                'page_revision,context_digest,target_ref,control_epoch,config_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (action,binding,call_id,generation,call_id,kind,digest,'preparing',now,now,
                 revision,context_digest,target,epoch,config_version))
        return self.commit_action(tenant,user,binding,action), True

    @staticmethod
    def _commit_row(row):
        result = dict(row)
        result['receipt'] = json.loads(result.pop('receipt_json'))
        return result

    def commit_action(self, tenant, user, binding, action):
        with self._connection() as db:
            row = db.execute('SELECT a.* FROM "cj-sap_workbench-actions" a '
                'JOIN "cj-sap_workbench-session_links" s ON s.id=a.session_id '
                'WHERE a.id=? AND s.id=? AND s.tenant_id=? AND s.user_id=?',
                (action,binding,tenant,user)).fetchone()
        if not row:
            raise WorkbenchError('action_not_found',404)
        return self._commit_row(row)

    def attach_commit_approval(self, action, approval):
        with self._connection() as db:
            changed = db.execute('UPDATE "cj-sap_workbench-actions" SET approval_ref=?,state=\'prepared\',updated_at=? '
                'WHERE id=? AND state=\'preparing\'', (approval,int(time.time()),action)).rowcount
        if changed != 1:
            raise WorkbenchError('action_already_dispatched',409)

    def begin_commit(self, action):
        with self._connection() as db:
            changed = db.execute('UPDATE "cj-sap_workbench-actions" SET state=\'running\',updated_at=? '
                'WHERE id=? AND state=\'prepared\'', (int(time.time()),action)).rowcount
        if changed != 1:
            raise WorkbenchError('action_already_dispatched',409)

    def commit_receipt(self, action, receipt):
        with self._connection() as db:
            changed = db.execute('UPDATE "cj-sap_workbench-actions" SET receipt_json=?,updated_at=? '
                'WHERE id=? AND state=\'running\'',(json.dumps(receipt,ensure_ascii=False),int(time.time()),action)).rowcount
        if changed != 1:
            raise WorkbenchError('action_already_dispatched',409)

    def commit_outcome(self, action, state, *, receipt=None, evidence='', expected=('running',)):
        if state not in {'prepared','succeeded','failed','unknown'} or not expected or set(expected)-{'preparing','prepared','running','unknown'}:
            raise ValueError('invalid commit transition')
        with self._connection() as db:
            changed = db.execute('UPDATE "cj-sap_workbench-actions" SET state=?,receipt_json=?,evidence_ref=?,updated_at=? '
                'WHERE id=? AND state IN ('+','.join('?' for _ in expected)+')',
                (state,json.dumps(receipt or {},ensure_ascii=False),evidence,int(time.time()),action,*expected)).rowcount
        if changed != 1:
            raise WorkbenchError('action_already_dispatched',409)

    @staticmethod
    def _binding(config):
        sap = config["sap"]
        target = urlsplit(sap["web_gui_url"])
        return json.dumps([sap["system_id"], target.scheme, target.hostname, target.port,
                           sap["client"], config["mcp"]["username"]], ensure_ascii=False)

    def resolve_mcp_credentials(self, tenant_id, expected_version):
        """Trusted runtime only: caller must authorize this tenant's scene use.

        No HTTP route exposes this method. Version and target binding prevent
        use of a password after an account/target change or rotation.
        """
        from auth.crypto import CredentialCryptoError, decrypt_secret
        if not tenant_id:
            raise WorkbenchError("missing_tenant", 403)
        with self._connection() as db:
            row = db.execute('SELECT c.version, c.config_json, s.binding, s.ciphertext '
                             'FROM "cj-sap_workbench-configs" c '
                             'LEFT JOIN "cj-sap_workbench-credentials" s ON s.tenant_id=c.tenant_id '
                             'WHERE c.tenant_id=?', (tenant_id,)).fetchone()
        if row is None or row["version"] != expected_version:
            raise WorkbenchError("config_conflict", 409)
        config = validate_config(json.loads(row["config_json"]))
        if not row["ciphertext"] or row["binding"] != self._binding(config):
            raise WorkbenchError("mcp_credentials_required")
        try:
            password = decrypt_secret(row["ciphertext"])
        except CredentialCryptoError:
            raise WorkbenchError("credential_crypto_unavailable", 503) from None
        return config, password

    def save_config(self, tenant_id, user_id, expected_version, config, *, audit,
                    mcp_password=None, clear_mcp_password=False):
        if not tenant_id or not user_id:
            raise WorkbenchError("missing_identity", 403)
        if type(expected_version) is not int or expected_version < 0:
            raise WorkbenchError("invalid_version")
        clean = validate_config(config)
        if mcp_password is not None and (not isinstance(mcp_password, str) or len(mcp_password) > 1024):
            raise WorkbenchError("invalid_config")
        if type(clear_mcp_password) is not bool or (clear_mcp_password and mcp_password):
            raise WorkbenchError("invalid_config")
        ciphertext = None
        if mcp_password:
            if not clean["mcp"]["username"] or not clean["sap"]["web_gui_url"] or not clean["sap"]["client"]:
                raise WorkbenchError("mcp_credentials_required")
            from auth.crypto import CredentialCryptoError, encrypt_secret
            try:
                ciphertext = encrypt_secret(mcp_password)
            except CredentialCryptoError:
                raise WorkbenchError("credential_crypto_unavailable", 503) from None
        binding = self._binding(clean)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute('SELECT version FROM "cj-sap_workbench-configs" WHERE tenant_id=?',
                             (tenant_id,)).fetchone()
            previous = row["version"] if row else 0
            if previous != expected_version:
                raise WorkbenchError("config_conflict", 409)
            version = previous + 1
            db.execute('INSERT INTO "cj-sap_workbench-configs" '
                       '(tenant_id, version, config_json, updated_by, updated_at) '
                       'VALUES (?,?,?,?,strftime(\'%s\',\'now\')) '
                       'ON CONFLICT(tenant_id) DO UPDATE SET version=excluded.version, '
                       'config_json=excluded.config_json, updated_by=excluded.updated_by, '
                       'updated_at=excluded.updated_at',
                       (tenant_id, version, json.dumps(clean, ensure_ascii=False), user_id))
            if clear_mcp_password:
                db.execute('DELETE FROM "cj-sap_workbench-credentials" WHERE tenant_id=?', (tenant_id,))
            elif ciphertext:
                db.execute('INSERT INTO "cj-sap_workbench-credentials" (tenant_id,binding,ciphertext) '
                           'VALUES (?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET '
                           'binding=excluded.binding,ciphertext=excluded.ciphertext',
                           (tenant_id, binding, ciphertext))
            else:
                # Blank means keep, but never reuse a password for a new account or SAP target.
                db.execute('DELETE FROM "cj-sap_workbench-credentials" WHERE tenant_id=? AND binding<>?',
                           (tenant_id, binding))
            password_configured = db.execute('SELECT 1 FROM "cj-sap_workbench-credentials" '
                                             'WHERE tenant_id=?', (tenant_id,)).fetchone() is not None
            # An audit failure rolls back the configuration transaction. No
            # endpoint, user credential, or whole input payload is audited.
            audit({"previous_version": previous, "version": version,
                   "mcp_password_configured": password_configured})
        return {"version": version, "config": clean, "mcp_password_configured": password_configured}
