# encoding:utf-8
"""Give every new member their own private "智能办公助理".

Product planning 3.1 gives the *user* level the same standing as the platform
and tenant levels: an individual has a space of their own, and their personal
memory belongs to them alone. A tenant, however, shares exactly one
智能办公助理 — so every member's schedule, todos and drafts land in one
workspace, separated only by conversation ownership, and the persona file names
whoever set it up.

This module closes that gap the same way :mod:`agent.tenant_provisioning` closes
the tenant gap, and for the same reason: the roster (``team.json``) and
``identity.db`` cannot commit together. So the work is orchestrated per member —
clone, personalise, bind, register as that member's default — with per-member
compensation so a failure is reported and retried rather than half-applied.

Three things are deliberately different from the tenant copy flow:

* the clone is bound **private to its owner** instead of tenant-shared, so the
  exclusive-read gate already in place keeps every colleague out;
* it is registered as that member's *personal* default (``memberships.default_agent_id``)
  rather than changing the tenant default, which must stay tenant-shared;
* it does not record ``cloned_from_agent_id``. That column carries a partial
  unique index — one clone per source per tenant — and this flow makes one clone
  per *member*, so recording it would fail the second member with a 409.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent.tenant_provisioning import (
    _MAX_AGENT_ID_LEN,
    _apply_rewrites,
    _rewrite_persona,
    _sanitise_agent_id,
    _whole_token_re,
)
from auth.service import SUPPLIED_ASSISTANT_ORIGINS

logger = logging.getLogger(__name__)

#: The roster name the template is looked up by, when nothing else is configured.
DEFAULT_SOURCE_NAME = "智能办公助理"

#: The owner marker a stock 智能办公助理 persona uses ("只承办 admin 本人的…").
DEFAULT_OWNER_ALIASES = ("admin",)

#: Defaults for the owner-facing lead. See ``config.py`` for the placeholder
#: contract; these mirror it so a provisioner built outside a loaded config
#: (tests, one-off scripts) still personalises.
DEFAULT_DESCRIPTION_TEMPLATE = "{name}的专属办公助理：{source_tail}"
DEFAULT_PERSONA_SUMMARY_TEMPLATE = "{name}的私人智能办公助理：{source_tail}"

#: The fields the templates above drive. Deliberately narrow: ``greeting`` and
#: ``position`` carry no owner wording, and ``name`` is not ours to change.
_OWNER_FIELD_TEMPLATE_KEYS = {
    "description": "personal_assistant_description_template",
    "persona_summary": "personal_assistant_persona_summary_template",
}

#: Separators between an owner-facing lead and the description that follows it:
#: the fullwidth colon the stock template uses, and its ASCII counterpart.
_LEAD_SEPARATORS = ("：", ":")

#: Outcomes of one attempt, as reported to the caller and recorded in audit.
CREATED = "created"
SKIPPED = "skipped"
FAILED = "failed"

#: Audit suffix per outcome, so the log reads ``member.personal_agent.create``.
_AUDIT_SUFFIX = {CREATED: "create", SKIPPED: "skip", FAILED: "fail"}

#: Reason codes. Stable strings: the console and the audit log both read them.
NO_SOURCE = "no_source_agent"
ALREADY_OWNED = "already_has_personal_agent"
ERROR = "error"

#: Outcomes and reasons specific to the operational backfill (see
#: ``personalize_existing``). ``UPDATED`` is deliberately not ``CREATED``: nothing
#: is created here, and an operator reading the summary should not think it was.
UPDATED = "updated"
UP_TO_DATE = "up_to_date"
AGENT_NOT_FOUND = "agent_not_in_roster"
OWNER_UNKNOWN = "owner_has_no_display_name"

#: Persona files the personalisation pass rewrites. ``USER.md`` is not here: it
#: is replaced outright, because it describes the operator and the operator has
#: just changed.
_REWRITTEN_FILES = ("AGENT.md", "RULE.md", "BOOTSTRAP.md")

#: Text metadata on the roster that may also name the source's owner.
_METADATA_FIELDS = ("description", "persona_summary", "greeting", "position")


def _with_suffix(base: str, sequence: int) -> str:
    """``base`` with ``-<sequence>``, kept inside the roster's id contract."""
    tail = "-%d" % sequence
    return base[: _MAX_AGENT_ID_LEN - len(tail)] + tail


def source_tail(text: Optional[str]) -> str:
    """The part of an owner-facing field that follows its lead.

    The stock template splits "归属短语：实质描述", and only the phrase names the
    original owner. Keeping the remainder verbatim is what stops this from
    discarding what the operator actually wrote about the assistant. A field
    with no separator is all lead, so it has no tail.
    """
    if not text:
        return ""
    positions = [index for index in (text.find(sep) for sep in _LEAD_SEPARATORS)
                 if index >= 0]
    return text[min(positions) + 1:].strip() if positions else ""


def render_owner_field(template: Optional[str], *, name: str,
                       source_text: Optional[str]) -> Optional[str]:
    """Author one owner-facing field, or ``None`` when it must be left alone.

    ``None`` means "do not write this field": either the template is blank (the
    documented opt-out) or the source never had the field, in which case
    inventing one would be guessing. Rendering happens *before* the alias pass,
    so ``{source_tail}`` is fed the source's own text and any owner marker still
    inside it is rewritten by the normal whole-word pass afterwards.

    Placeholders are substituted literally rather than via ``str.format``: a
    deployment's template may legitimately contain braces, and a bad placeholder
    should leave a visible literal instead of raising mid-provision.
    """
    if not (template or "").strip() or not source_text:
        return None
    return (template
            .replace("{name}", name)
            .replace("{source_tail}", source_tail(source_text)))


def _member_user_profile(*, display_name: str, username: str, position_text: str,
                         department_id: Optional[str]) -> str:
    """A ``USER.md`` naming the member this assistant now serves.

    Same shape as the workspace template (``agent.prompt.workspace``) so anything
    that reads the file keeps working; the fields are simply filled in from the
    membership instead of asking in the first conversation.
    """
    lines = [
        "# USER.md - 用户基本信息",
        "",
        "*这个文件只存放不会变的基本身份信息。爱好、偏好、计划等动态信息请写入 MEMORY.md。*",
        "",
        "## 基本信息",
        "",
        "- **姓名**: %s" % display_name,
        "- **称呼**: %s" % display_name,
        "- **用户名**: %s" % (username or "-"),
        "- **职业**: %s" % (position_text or "-"),
        "- **部门**: %s" % (department_id or "-"),
        "",
        "## 联系方式",
        "",
        "- **微信**: ",
        "- **邮箱**: ",
        "- **其他**: ",
        "",
        "## 说明",
        "",
        "本智能体为该用户私有的智能办公助理，只服务上述用户本人。",
        "",
    ]
    return "\n".join(lines)


class PersonalAssistantProvisioner:
    """Clone a tenant's assistant into one member's own private Agent."""

    def __init__(self, identity_service, admin_service=None, *,
                 source_name: Optional[str] = None,
                 source_agent_id: Optional[str] = None,
                 owner_aliases: Optional[Sequence[str]] = None):
        self._svc = identity_service
        self._admin_service = admin_service
        self._source_name = source_name
        self._source_agent_id = source_agent_id
        self._owner_aliases = list(owner_aliases) if owner_aliases is not None else None

    # --- collaborators ---------------------------------------------------

    @property
    def admin(self):
        if self._admin_service is None:
            from agent.admin import get_agent_admin_service

            self._admin_service = get_agent_admin_service()
        return self._admin_service

    def _setting(self, key: str, default: Any) -> Any:
        """Deployment setting, with a constructor override winning over config."""
        try:
            from config import conf

            return (conf() or {}).get(key, default)
        except Exception:
            return default

    def _configured_source_name(self) -> str:
        if self._source_name is not None:
            return self._source_name
        return self._setting("personal_assistant_agent_name", DEFAULT_SOURCE_NAME) \
            or DEFAULT_SOURCE_NAME

    def _configured_source_id(self) -> str:
        if self._source_agent_id is not None:
            return self._source_agent_id
        return (self._setting("personal_assistant_agent_id", "") or "").strip()

    def _configured_aliases(self) -> List[str]:
        if self._owner_aliases is not None:
            return list(self._owner_aliases)
        configured = self._setting("personal_assistant_owner_aliases", None)
        if configured is None:
            return list(DEFAULT_OWNER_ALIASES)
        if isinstance(configured, str):
            return [item for item in (part.strip() for part in configured.split(",")) if item]
        return [str(item) for item in configured if str(item).strip()]

    def _alias_rewrites(self, name: str) -> List[Tuple]:
        """Whole-word rewrites from the configured owner markers to ``name``.

        Shared by provisioning and the backfill so both decide "what is an owner
        reference" the same way -- an install that changed the marker must not
        get one behaviour at creation and another when re-running over old copies.
        """
        rewrites = []
        for alias in self._configured_aliases():
            alias = (alias or "").strip()
            if alias and alias != name:
                rewrites.append((_whole_token_re(alias), None, name))
        return rewrites

    def _owner_field_templates(self) -> Dict[str, str]:
        """``{field: template}`` from deployment settings; blank means opt out."""
        defaults = {
            "description": DEFAULT_DESCRIPTION_TEMPLATE,
            "persona_summary": DEFAULT_PERSONA_SUMMARY_TEMPLATE,
        }
        return {
            field: str(self._setting(key, defaults[field]) or "").strip()
            for field, key in _OWNER_FIELD_TEMPLATE_KEYS.items()
        }

    def _owner_metadata(self, agent_id: str, *, name: str, rewrites,
                        profile: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """The roster field values this member's copy should have, where they differ.

        Two mechanisms meet here. The owner-facing lead is *authored* from a
        template, because the source's wording names whoever the source was
        written for -- possibly as a role word no alias table can match. Every
        other part of these fields is still only ever touched by the alias pass,
        so unrelated prose survives byte-identical.
        """
        if profile is None:
            profile = self._roster().get(agent_id) or {}
        templates = self._owner_field_templates()
        updates: Dict[str, str] = {}
        for field in _METADATA_FIELDS:
            current = profile.get(field)
            if not current:
                continue
            generated = None
            if field in templates:
                # ``None`` keeps the source text for this field, which is both
                # the blank-template opt-out and the "field was never there" case.
                generated = render_owner_field(
                    templates[field], name=name, source_text=current)
            updated = _apply_rewrites(
                current if generated is None else generated, rewrites)
            if updated != current:
                updates[field] = updated
        return updates

    # --- reading ---------------------------------------------------------

    def _roster(self) -> Dict[str, Dict[str, Any]]:
        return {a["id"]: a for a in (self.admin.snapshot().get("agents") or [])}

    def owned_agent_id(self, tenant_id: str, user_id: str) -> Optional[str]:
        """The system-supplied assistant this member already has, if any.

        Idempotency is by *provenance*, not mere ownership: a private agent the
        member created for themselves is not the system's assistant and must not
        make provisioning skip. A binding of unknown origin still counts as
        supplied — those predate the origin column and were written by this
        provisioner, so re-provisioning on them would hand existing members a
        duplicate. See ``SUPPLIED_ASSISTANT_ORIGINS``.
        """
        for binding in self._svc.agents_for_tenant(tenant_id):
            if (binding.get("private_owner_user_id") == user_id
                    and binding.get("origin", "unknown")
                    in SUPPLIED_ASSISTANT_ORIGINS):
                return binding["agent_id"]
        return None

    def resolve_source(self, tenant_id: str) -> Tuple[Optional[str], Optional[str]]:
        """The template Agent to clone, or ``(None, reason)``.

        Bounded to the tenant's own bindings on purpose: another tenant's
        identically-named Agent is not this tenant's to copy, and the
        process-global default may belong to someone else entirely.
        """
        bound = set(self._svc.tenant_agent_ids(tenant_id))
        roster = self._roster()

        explicit = self._configured_source_id()
        if explicit:
            profile = roster.get(explicit)
            if explicit not in bound or profile is None:
                return None, NO_SOURCE
            # A coding Agent has no persona to clone and no normal runtime to
            # answer with; it is never a personal-assistant template.
            if profile.get("agent_type") == "coding":
                return None, NO_SOURCE
            return (explicit, None) if profile.get("enabled", True) else (None, NO_SOURCE)

        wanted = self._configured_source_name()
        matches = sorted(
            agent_id for agent_id in bound
            if roster.get(agent_id, {}).get("name") == wanted
            and roster[agent_id].get("enabled", True)
            and roster[agent_id].get("agent_type") != "coding"
        )
        return (matches[0], None) if matches else (None, NO_SOURCE)

    # --- writing ---------------------------------------------------------

    def _plan(self, tenant_id: str, source_agent_id: str,
              username: str) -> Tuple[str, str, bool]:
        """``(agent_id, workspace, adopt)`` for this member's clone.

        ``adopt`` is True when a previous attempt already wrote the roster entry
        and its workspace but never got as far as binding: reusing it is how a
        retry after a crash completes instead of piling up ``-2`` suffixes.
        """
        root = self._svc.tenant_shared_root(tenant_id) or ""
        if not root:
            raise ValueError("tenant has no shared root to place the Agent in")
        roster = self._roster()
        base = _sanitise_agent_id("%s-%s" % (source_agent_id, username))
        base = (base[:_MAX_AGENT_ID_LEN] or "personal-assistant")
        candidate = base
        suffix = 2
        while True:
            workspace = os.path.join(root, "agents", candidate)
            if candidate not in roster:
                return candidate, workspace, False
            if self._svc.get_agent_binding(candidate) is None and \
                    os.path.realpath(roster[candidate].get("workspace") or "") \
                    == os.path.realpath(workspace):
                return candidate, workspace, True
            candidate = _with_suffix(base, suffix)
            suffix += 1

    def _compensate(self, agent_id: Optional[str]) -> None:
        """Remove what a failed attempt created, so a retry starts clean.

        Best-effort by design: reporting the failure matters more than a perfect
        cleanup, and the next attempt adopts an orphaned roster entry anyway.
        """
        if not agent_id:
            return
        try:
            self._svc.release_deleted_agent(agent_id=agent_id, actor_user_id=None)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[PersonalAssistant] could not release %s: %s", agent_id, exc)
        try:
            profile = self._roster().get(agent_id)
            self.admin.delete_agent(agent_id)
            workspace = Path((profile or {}).get("workspace") or "")
            if workspace.is_dir():
                shutil.rmtree(workspace, ignore_errors=True)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[PersonalAssistant] could not remove %s: %s", agent_id, exc)

    def _personalise(self, agent_id: str, *, display_name: str, username: str,
                     position_text: str, department_id: Optional[str]) -> None:
        """Point the clone at its owner: ``USER.md``, persona text, metadata.

        A clone keeps the source's persona verbatim, which for this template means
        a document about whoever it was written for ("只承办 admin 本人的日程").
        The aliases are configured rather than guessed: substituting a token that
        was never an owner reference would corrupt a hand-written persona, and
        the spec asks for exactly the opposite boundedness.
        """
        name = (display_name or "").strip() or username
        rewrites = self._alias_rewrites(name)

        profile = self._roster()[agent_id]
        workspace = profile["workspace"]
        if rewrites:
            _rewrite_persona(workspace, rewrites)
        user_md = Path(workspace) / "USER.md"
        user_md.write_text(
            _member_user_profile(
                display_name=name, username=username,
                position_text=position_text, department_id=department_id),
            encoding="utf-8")

        updates = self._owner_metadata(agent_id, name=name, rewrites=rewrites,
                                       profile=profile)
        if updates:
            self.admin.update_agent(agent_id, **updates)

    def _record(self, *, status: str, tenant_id: str, user_id: str,
                agent_id: Optional[str] = None, source_agent_id: Optional[str] = None,
                reason: Optional[str] = None, message: Optional[str] = None,
                actor_user_id: Optional[str] = None,
                extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = {"status": status}
        if agent_id:
            result["agent_id"] = agent_id
        if source_agent_id:
            result["source_agent_id"] = source_agent_id
        if reason:
            result["reason"] = reason
        if message:
            result["message"] = message
        if extra:
            # Additive detail for the caller (task 4.6): the assistant was made,
            # but *whether it became the member's default* is a separate outcome
            # the caller is entitled to see. Deliberately not part of the audit
            # payload below — that one stays a fixed vocabulary of ids and one
            # reason code.
            result.update(extra)
        try:
            self._svc.record_personal_agent_event(
                action="member.personal_agent.%s" % _AUDIT_SUFFIX[status],
                tenant_id=tenant_id, user_id=user_id,
                result="failure" if status == FAILED else "success",
                agent_id=agent_id, source_agent_id=source_agent_id,
                reason=reason or message, actor_user_id=actor_user_id)
        except Exception as exc:  # pragma: no cover - audit must not break the flow
            logger.warning("[PersonalAssistant] audit for %s failed: %s", user_id, exc)
        return result

    def initialize_member_default(self, *, tenant_id: str, user_id: str,
                                  agent_id: str,
                                  actor_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Register an Agent as a member's default **only if they have none**.

        System provisioning's half of ``memberships.default_agent_id`` (task
        4.6). It is deliberately not :meth:`IdentityService.set_member_default_agent`:
        that writer is used by operations and by tests to *place* a registration,
        while this one must never displace a choice a human already made. The old
        provisioning path did displace it — the guard was "does this member
        already own a personal assistant", which does not cover a member who owns
        no assistant yet but has already chosen a default.

        The competition is settled inside the service's transaction on
        ``default_agent_origin IS NULL`` (see
        :meth:`IdentityService.initialize_member_default_agent`), so "is anything
        registered" and "write mine" cannot interleave with a user action.

        Returns ``{"status": "created"|"skipped", "reason": ...}``; a skip is a
        normal outcome, not an error, because the member's own preference is a
        legitimate reason for provisioning to stand down.
        """
        return self._svc.initialize_member_default_agent(
            tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
            actor_user_id=actor_user_id)

    def provision(self, *, tenant_id: str, user_id: str, username: str,
                  display_name: str = "", position_text: str = "",
                  department_id: Optional[str] = None,
                  actor_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Stand up this member's private assistant and register it as their default.

        Never raises for a provisioning problem: the caller is creating a member,
        and a missing template or an unwritable roster must not turn into a
        failed user creation. The returned ``status`` says what happened.
        """
        if not tenant_id or not user_id:
            raise ValueError("tenant_id and user_id are required")

        owned = self.owned_agent_id(tenant_id, user_id)
        if owned:
            return self._record(
                status=SKIPPED, reason=ALREADY_OWNED, tenant_id=tenant_id,
                user_id=user_id, agent_id=owned, actor_user_id=actor_user_id)

        source_agent_id, reason = self.resolve_source(tenant_id)
        if not source_agent_id:
            return self._record(
                status=SKIPPED, reason=reason or NO_SOURCE, tenant_id=tenant_id,
                user_id=user_id, actor_user_id=actor_user_id)

        agent_id = None
        try:
            agent_id, workspace, adopt = self._plan(tenant_id, source_agent_id, username)
            if not adopt:
                self.admin.clone_agent(
                    source_agent_id, agent_id, name=None, workspace=workspace,
                    knowledge_mode="own")
            self._personalise(
                agent_id, display_name=display_name, username=username,
                position_text=position_text, department_id=department_id)
            # Bind before registering: the registration refuses an Agent the
            # tenant does not hold, which is the check that keeps a personal
            # default inside its own tenant.
            self._svc.bind_agent(
                tenant_id=tenant_id, agent_id=agent_id,
                private_owner_user_id=user_id, actor_user_id=actor_user_id,
                origin="provisioned_assistant")
            # Initialise, never impose (task 4.6): a member who already chose a
            # default keeps it, and this Agent simply is not registered for them.
            # Overwriting here was the competition the origin column exists to
            # settle, so the outcome is reported rather than assumed.
            registration = self.initialize_member_default(
                tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                actor_user_id=actor_user_id)
        except Exception as exc:
            logger.warning(
                "[PersonalAssistant] provisioning for member %s in tenant %s"
                " failed: %s", user_id, tenant_id, exc)
            self._compensate(agent_id)
            return self._record(
                status=FAILED, reason=ERROR, message=str(exc), tenant_id=tenant_id,
                user_id=user_id, agent_id=agent_id,
                source_agent_id=source_agent_id, actor_user_id=actor_user_id)

        return self._record(
            status=CREATED, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
            source_agent_id=source_agent_id, actor_user_id=actor_user_id,
            extra={"default_registration": registration["status"],
                   "default_registration_reason": registration.get("reason")})

    # --- operational backfill --------------------------------------------

    def personalize_existing(self, *, tenant_id: Optional[str] = None,
                             dry_run: bool = False,
                             actor_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Re-author the owner-facing fields of personal assistants already made.

        A copy created before the owner-facing templates existed still describes
        its owner as whichever role the *source* template was written for, and
        nothing re-runs provisioning for an existing member -- so those copies
        need an explicit pass.

        Scope is taken from the binding, not from names or id shapes: an Agent
        is a personal assistant exactly when its binding names a private owner,
        which is the same record ``owned_agent_id`` trusts. The source template,
        the tenant's shared default and every ordinary Agent are therefore out of
        reach without anything having to guess.

        Idempotent: a field already equal to its target is reported as skipped, so
        re-running after a template change is safe and a second run reports
        nothing to do. ``dry_run`` computes the same report without writing --
        including without auditing, since an audit row is itself a write.
        """
        changed: List[Tuple[str, str, str]] = []
        skipped: List[Tuple[str, str, str]] = []
        failed: List[Tuple[str, str, str]] = []
        buckets = {UPDATED: changed, SKIPPED: skipped, FAILED: failed}
        # One snapshot for the whole pass: each Agent is visited once and its
        # target text is derived from its own entry, so nothing needs a re-read.
        roster = self._roster()
        for tenant in self._tenants_to_walk(tenant_id):
            tenant_key = tenant["id"]
            for binding in self._svc.agents_for_tenant(tenant_key):
                owner_user_id = binding.get("private_owner_user_id")
                if not owner_user_id:
                    continue
                if binding.get("origin", "unknown") not in SUPPLIED_ASSISTANT_ORIGINS:
                    # A member's own agent is theirs; this pass re-authors only
                    # the copies the system supplied.
                    continue
                agent_id = binding["agent_id"]
                status, detail, name = self._personalize_one(
                    tenant_key, agent_id, owner_user_id, roster.get(agent_id),
                    dry_run=dry_run, actor_user_id=actor_user_id)
                buckets[status].append((tenant_key, agent_id, detail or name or ""))

        return {"dry_run": dry_run, "changed": changed,
                "skipped": skipped, "failed": failed}

    def _tenants_to_walk(self, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
        """The tenants to visit: one by id (missing means none), or all of them."""
        tenants = self._svc.list_tenants(status="all")
        if not tenant_id:
            return tenants
        return [t for t in tenants if t["id"] == tenant_id]

    def _personalize_one(self, tenant_id: str, agent_id: str, owner_user_id: str,
                         profile: Optional[Dict[str, Any]], *,
                         dry_run: bool, actor_user_id: Optional[str]
                         ) -> Tuple[str, Optional[str], Optional[str]]:
        """``(status, reason, name)`` for one existing personal assistant."""
        if profile is None:
            # A binding without a roster entry: surfaced rather than silently
            # counted as "already fine", because it means the two stores disagree.
            return FAILED, AGENT_NOT_FOUND, None
        membership = self._svc.get_membership(owner_user_id, tenant_id) or {}
        name = (membership.get("display_name") or "").strip()
        if not name:
            # The owner is gone, so there is no name to author the lead from.
            return FAILED, OWNER_UNKNOWN, None

        updates = self._owner_metadata(
            agent_id, name=name, rewrites=self._alias_rewrites(name),
            profile=profile)
        if not updates:
            return SKIPPED, UP_TO_DATE, name
        if dry_run:
            return UPDATED, None, name

        self.admin.update_agent(agent_id, **updates)
        try:
            self._svc.record_personal_agent_event(
                action="member.personal_agent.personalize", tenant_id=tenant_id,
                user_id=owner_user_id, result="success", agent_id=agent_id,
                reason=",".join(sorted(updates)), actor_user_id=actor_user_id)
        except Exception as exc:  # pragma: no cover - audit must not break the pass
            logger.warning("[PersonalAssistant] audit for %s failed: %s", agent_id, exc)
        return UPDATED, None, name


def get_personal_assistant_provisioner() -> PersonalAssistantProvisioner:
    """Build a provisioner wired to the process's identity service and roster."""
    from auth.service import get_identity_service

    return PersonalAssistantProvisioner(get_identity_service())


__all__ = [
    "DEFAULT_DESCRIPTION_TEMPLATE",
    "DEFAULT_OWNER_ALIASES",
    "DEFAULT_PERSONA_SUMMARY_TEMPLATE",
    "DEFAULT_SOURCE_NAME",
    "PersonalAssistantProvisioner",
    "get_personal_assistant_provisioner",
    "render_owner_field",
    "source_tail",
]
