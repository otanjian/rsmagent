"""Capability projection for traceable source ingestion (task 1.5).

Three different questions must not be conflated, because the console answers
them differently:

- **configuration** -- is the feature switched on for this deployment;
- **permission**    -- may *this* caller write this Agent's knowledge root; and
- **real dependency** -- is the parser/executor actually available here.

A caller with maintenance rights still cannot convert when the deployment has
not enabled conversion or the converter dependency is missing; the page then
shows a concrete reason instead of promising "upload and it will be searchable".

The two new switches default off. Turning them off never disables scanning
filters or managed-path protection: that guarantee lives in ``KnowledgeScope``,
which keys off the registered catalog rather than these flags.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Tuple

UPLOAD_FLAG = "knowledge_source_upload_enabled"
CONVERSION_FLAG = "knowledge_conversion_enabled"

#: Skill that performs the actual extraction (added in phase C). Its presence is
#: the real dependency for conversion; the flag alone is not enough.
DOCUMENT_CONVERT_SKILL = "document-convert"


def conversion_dependency() -> Tuple[bool, str]:
    """Whether the real converter is installed for this deployment."""
    try:
        from common import state_dir

        skill_dir = Path(state_dir.skills_dir()) / DOCUMENT_CONVERT_SKILL
        if not (skill_dir / "SKILL.md").is_file():
            return False, "document conversion skill is not installed"
        return True, ""
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"conversion dependency could not be checked: {exc}"


def knowledge_capabilities(*, can_write: bool = False,
                           dependency: Optional[Callable[[], Tuple[bool, str]]] = None) -> dict:
    """Project configuration, permission and dependency into one shape."""
    from config import conf

    cfg = conf()
    master = bool(cfg.get("knowledge", True))
    upload_configured = bool(cfg.get(UPLOAD_FLAG, False))
    conversion_configured = bool(cfg.get(CONVERSION_FLAG, False))
    dep_ok, dep_reason = (dependency or conversion_dependency)()

    upload_available = master and upload_configured and can_write
    if not master:
        upload_reason = "knowledge is disabled"
    elif not upload_configured:
        upload_reason = "source upload is not enabled on this deployment"
    elif not can_write:
        upload_reason = "no write permission on this knowledge base"
    else:
        upload_reason = ""

    conversion_available = upload_available and conversion_configured and dep_ok
    if not master:
        conversion_reason = "knowledge is disabled"
    elif not upload_configured:
        conversion_reason = "source upload is not enabled on this deployment"
    elif not can_write:
        conversion_reason = "no write permission on this knowledge base"
    elif not conversion_configured:
        conversion_reason = "document conversion is not enabled on this deployment"
    elif not dep_ok:
        conversion_reason = dep_reason or "converter dependency unavailable"
    else:
        conversion_reason = ""

    return {
        "knowledge_enabled": master,
        "can_write": bool(can_write),
        # Agent search is a console-only filter over the already-visible list;
        # it never depends on the ingestion switches.
        "agent_search": {"enabled": True, "independent_of_flags": True},
        "source_upload": {
            "configured": upload_configured,
            "dependency_ready": True,
            "available": upload_available,
            "reason": upload_reason,
        },
        "conversion": {
            "configured": conversion_configured,
            "dependency_ready": dep_ok,
            "available": conversion_available,
            "reason": conversion_reason,
        },
    }
