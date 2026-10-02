"""Database identity adapter for the upstream cross-deployment peer protocol.

Only signatures and deployment routing are new here. Accounts, external subject
bindings, membership, Agent grants and storage scope reuse the existing IAM.
The relay forwards opaque ``peer_identity`` proofs; it never grants identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass

from Crypto.Signature import eddsa

REQUEST_FIELDS = (
    "request_id", "source_agent_id", "source_name", "target_agent_id", "task",
    "root_session_id", "trace", "depth", "members", "peers", "timeout", "mode", "history",
)
RESPONSE_FIELDS = ("request_id", "event", "status", "content", "error", "agent_id", "agent_name", "duration")
MAX_TIMEOUT = 600


class PeerIdentityError(ValueError):
    pass


def _settings():
    from config import conf
    value = conf().get("peer_identity") or {}
    if not isinstance(value, dict) or not value.get("deployment_id"):
        raise PeerIdentityError("peer_identity_not_configured")
    return value


def _canonical(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > 2 * 1024 * 1024:
        raise PeerIdentityError("peer_message_too_large")
    return data


def _digest(payload, fields):
    return hashlib.sha256(_canonical({k: payload[k] for k in fields if k in payload})).hexdigest()


def binding_subject(tenant_id, user_id):
    """Collision-free external subject, also usable in the existing admin UI."""
    return json.dumps([tenant_id, user_id], separators=(",", ":"))


def _sign(claims):
    settings = _settings()
    try:
        seed = bytes.fromhex(os.environ.get(settings.get("signing_key_env", ""), ""))
        key = eddsa.import_private_key(seed)
        signature = eddsa.new(key, "rfc8032").sign(_canonical(claims)).hex()
    except (ValueError, TypeError, KeyError) as error:
        raise PeerIdentityError("peer_signing_key_unavailable") from error
    return {"claims": claims, "signature": signature}


def _verify(proof):
    try:
        claims = proof["claims"]
        settings = _settings()
        issuer = claims["issuer"]
        trusted = settings["trusted_deployments"][issuer]
        key = eddsa.import_public_key(bytes.fromhex(trusted["public_key"]))
        eddsa.new(key, "rfc8032").verify(_canonical(claims), bytes.fromhex(proof["signature"]))
        if claims["version"] != 1 or claims["audience"] != settings["deployment_id"]:
            raise ValueError("audience")
        return claims, trusted
    except (ValueError, TypeError, KeyError) as error:
        raise PeerIdentityError("peer_signature_invalid") from error


def _service():
    from auth.service import get_identity_service
    return get_identity_service()


def authorize_actor(user_id, tenant_id, agent_id):
    from auth.runtime import member_context
    from auth.object_scope import ObjectScope, USE
    from agent.registry import get_agent_registry

    svc = _service()
    ctx = member_context(svc, user_id, tenant_id)
    profile = get_agent_registry().get_addressed(agent_id, require_enabled=True)
    binding = svc.get_agent_binding(profile.id)
    if (ctx.must_change_password or not ObjectScope.from_context(ctx).allows_agent(binding, action=USE)
            or not (ctx.is_platform_admin or ctx.is_tenant_admin or "chat.use" in ctx.permissions)
            or not svc.check_resource_action(user_id, tenant_id, "agent", "agent:" + profile.id,
                                             "use", permission="agent.use")):
        raise PeerIdentityError("peer_actor_not_authorized")
    return ctx, profile


def _route(peer_id, identity):
    settings = _settings()
    try:
        route = settings["peers"][peer_id]
        tenant = route["tenants"][identity.tenant_id]
        deployment = route["deployment_id"]
        if not tenant or not settings["trusted_deployments"][deployment]["public_key"]:
            raise KeyError("trust")
        sources = route.get("source_agents")
        if sources is not None and identity.agent_id not in sources:
            raise KeyError("source")
        return deployment, tenant
    except (KeyError, TypeError) as error:
        raise PeerIdentityError("peer_route_not_authorized") from error


def peer_visible(peer_id):
    from common.runtime_identity import current_identity
    try:
        identity = current_identity()
        if not identity.user_id or not identity.tenant_id:
            return False
        _route(peer_id, identity)
        return True
    except PeerIdentityError:
        return False


@dataclass
class Outbound:
    claims: dict
    identity: object
    peer_id: str
    last_sequence: int = -1


def _source_context(identity):
    ctx, source = authorize_actor(identity.user_id, identity.tenant_id, identity.agent_id)
    # A logged-out Web turn cannot mint a fresh peer request from a stale context.
    if identity.web_auth_session_id:
        rows = _service()._store.execute(
            "SELECT * FROM auth_sessions WHERE id=?", (identity.web_auth_session_id,))
        if (not rows or rows[0]["user_id"] != identity.user_id or rows[0]["revoked_at"]
                or rows[0]["restricted"] or not _service()._desktop_child_parent_live(rows[0])
                or (rows[0]["expires_at"] is not None and rows[0]["expires_at"] <= time.time())):
            raise PeerIdentityError("peer_source_session_expired")
    return ctx, source


def sign_request(payload):
    from common.runtime_identity import current_identity
    identity = current_identity()
    ctx, source = _source_context(identity)
    if payload.get("source_agent_id") != source.id:
        raise PeerIdentityError("peer_source_mismatch")
    deployment, tenant = _route(payload.get("target_agent_id"), identity)
    timeout = float(payload.get("timeout", 600))
    if not 0 < timeout <= MAX_TIMEOUT or payload.get("mode") not in ("delegate", "speak", "clear"):
        raise PeerIdentityError("peer_request_invalid")
    if not payload.get("request_id") or not payload.get("root_session_id"):
        raise PeerIdentityError("peer_request_invalid")
    now = int(time.time())
    claims = {
        "version": 1, "kind": "request", "issuer": _settings()["deployment_id"],
        "audience": deployment, "source_tenant": ctx.tenant_id, "subject": ctx.user_id,
        "target_tenant": tenant, "request_id": payload["request_id"],
        "digest": _digest(payload, REQUEST_FIELDS), "nonce": uuid.uuid4().hex,
        "issued_at": now, "expires_at": now + 60, "reply_until": now + int(timeout) + 30,
    }
    payload["peer_identity"] = _sign(claims)
    return Outbound(claims, identity, payload["target_agent_id"])


def verify_request(payload):
    claims, trusted = _verify(payload.get("peer_identity"))
    now = time.time()
    try:
        valid = (claims["kind"] == "request" and claims["request_id"] == payload["request_id"]
                 and claims["digest"] == _digest(payload, REQUEST_FIELDS)
                 and now - 65 <= claims["issued_at"] <= now + 5
                 and now <= claims["expires_at"] <= claims["issued_at"] + 60
                 and now <= claims["reply_until"] <= claims["issued_at"] + MAX_TIMEOUT + 30
                 and trusted["tenants"][claims["source_tenant"]] == claims["target_tenant"]
                 and len(claims["nonce"]) == 32)
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise PeerIdentityError("peer_request_invalid")
    return claims


def _claim_request(claims, user_id):
    # Persist before execution: retries, process restarts and parallel workers
    # cannot run the same signed request twice. No prompt or key is stored.
    try:
        with _service()._store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM peer_identity_nonces WHERE expires_at < ?", (int(time.time()),))
            con.execute("INSERT INTO peer_identity_nonces(issuer, nonce, expires_at, tenant_id, user_id, request_id)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (claims["issuer"], claims["nonce"], claims["reply_until"],
                         claims["target_tenant"], user_id, claims["request_id"]))
            con.commit()
    except sqlite3.IntegrityError as error:
        raise PeerIdentityError("peer_request_replayed") from error


def sign_response(claims, chunk, sequence):
    proof = {
        "version": 1, "kind": chunk["chunk_type"], "issuer": _settings()["deployment_id"],
        "audience": claims["issuer"], "request_id": claims["request_id"],
        "request_nonce": claims["nonce"], "request_digest": claims["digest"],
        "expires_at": claims["reply_until"], "sequence": sequence,
        "digest": _digest(chunk, RESPONSE_FIELDS),
    }
    return {**chunk, "peer_identity": _sign(proof)}


def verify_response(outbound, payload, kind):
    claims, _ = _verify(payload.get("peer_identity"))
    expected = outbound.claims
    try:
        valid = (claims["issuer"] == expected["audience"] and claims["kind"] == kind
                 and claims["request_id"] == expected["request_id"] == payload["request_id"]
                 and claims["request_nonce"] == expected["nonce"]
                 and claims["request_digest"] == expected["digest"]
                 and claims["expires_at"] == expected["reply_until"] >= time.time()
                 and type(claims["sequence"]) is int and claims["sequence"] > outbound.last_sequence
                 and claims["digest"] == _digest(payload, RESPONSE_FIELDS))
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise PeerIdentityError("peer_response_invalid")
    identity = outbound.identity
    _source_context(identity)
    if _route(outbound.peer_id, identity) != (expected["audience"], expected["target_tenant"]):
        raise PeerIdentityError("peer_route_changed")
    outbound.last_sequence = claims["sequence"]


def serve_authenticated(payload, bridge, send_chunk):
    from auth.runtime import to_runtime_identity
    from common.runtime_identity import use_identity
    from agent.multiagent.inbound import serve_invoke

    claims = None
    sequence = 0
    def send(chunk):
        nonlocal sequence
        signed = sign_response(claims, chunk, sequence) if claims else chunk
        sequence += 1
        send_chunk(signed)
    try:
        claims = verify_request(payload)
        # Refuse before executing if this deployment cannot authenticate its
        # replies; a missing local signing key must not produce an unseen run.
        sign_response(claims, {"chunk_type": "result", "request_id": claims["request_id"]}, 0)
        user = _service().find_user_for_external_identity(
            "peer", claims["issuer"], binding_subject(claims["source_tenant"], claims["subject"]))
        if not user:
            raise PeerIdentityError("peer_subject_unbound")
        ctx, target = authorize_actor(user["id"], claims["target_tenant"], payload["target_agent_id"])
        _claim_request(claims, ctx.user_id)
        # Remote session names live in their own namespace, so a request cannot
        # collide with a local conversation or a different deployment's owner.
        root = hashlib.sha256(_canonical([claims["issuer"], claims["source_tenant"], claims["subject"],
                                         payload["root_session_id"]])).hexdigest()
        scoped = {k: payload[k] for k in REQUEST_FIELDS if k in payload}
        scoped.update(root_session_id="peer_" + root, target_agent_id=target.id,
                      target_aliases=[payload["target_agent_id"]])
        identity = to_runtime_identity(ctx, target.id, "peer_" + root)
        with use_identity(identity):
            # A peer roster is descriptive, never a grant to other local Agents.
            members = []
            for member in scoped.get("members", []):
                try:
                    authorize_actor(ctx.user_id, ctx.tenant_id, member)
                except Exception:
                    if not peer_visible(member):
                        continue
                members.append(member)
            scoped["members"] = members
            serve_invoke(scoped, bridge(), send)
    except Exception as error:
        code = str(error) if isinstance(error, PeerIdentityError) else "peer_identity_unavailable"
        failure = {"chunk_type": "result", "request_id": str(payload.get("request_id") or ""),
                   "status": "failed", "error": code}
        try:
            send(failure)
        except PeerIdentityError:
            send_chunk(failure)
