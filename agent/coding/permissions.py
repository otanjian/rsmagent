# encoding:utf-8
"""Session permission profiles the platform hands to the shared OpenCode service.

A tool a project installs is visible to *every* session of that project,
including ordinary coding conversations the user starts themselves. OpenCode's
permission rules are the only mechanism that narrows a tool to some sessions:

* a rule with ``action: "deny"`` removes the tool from the model's tool list
  (``resolveTools`` -> ``Permission.disabled`` in ``packages/opencode``);
* the session's own ruleset is merged *after* the Agent's, and the last match
  wins, so a session-level ``allow`` re-enables what a project-level ``deny``
  hid.

The ruleset lives here, on the server. A caller may only **name** a profile; it
never sends rule content. Letting a client send the rules would let it grant
itself permission for any tool, so an unknown name is refused as an invalid
request instead of being passed through.

This is a *visibility* rule, not an authorization one. A session that carries the
allow still has every tool call verified server-side against the session's own
binding; narrowing only reduces accidental calls.
"""

from typing import Dict, List, Optional, Tuple

#: Named profiles -> the OpenCode session permission ruleset they stand for.
#: ``pattern: "*"`` means "the whole tool". A profile is added by the platform
#: module that owns the capability, so a scene cannot invent one.
PROFILES: Dict[str, Tuple[Dict[str, str], ...]] = {
    # The SAP workbench installs ``sap_transaction_open`` and ``sap_data_call``
    # into its project directory. The project config denies both by default; a
    # scene-issued session names this profile and gets the allows that make the
    # tools visible again.
    "sap_workbench": (
        {"permission": "sap_transaction_open", "pattern": "*", "action": "allow"},
        {"permission": "sap_data_call", "pattern": "*", "action": "allow"},
        {"permission": "sap_page_read", "pattern": "*", "action": "allow"},
    ),
}


def ruleset(profile: str) -> Optional[List[Dict[str, str]]]:
    """The server-defined ruleset for a named profile.

    Returns ``None`` for an empty name — no rules to add. An unknown non-empty
    name raises ``KeyError`` on purpose; the caller turns that into
    ``coding_invalid_request`` rather than silently granting nothing.
    """
    name = str(profile or "").strip()
    if not name:
        return None
    rules = PROFILES[name]
    return [dict(rule) for rule in rules]
