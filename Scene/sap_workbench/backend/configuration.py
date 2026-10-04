"""Non-secret, scene-owned connection settings; runtime gates are separate."""
from copy import deepcopy
import re
from urllib.parse import parse_qsl, urlsplit

from .deployment import fixed_mcp_connections, BROWSER_SERVICE_REFS


class WorkbenchError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


DEFAULT_CONFIG = {
    "enabled": False,
    "automation_enabled": False,
    "commit_enabled": False,
    "coding_agent_id": "",
    "sap": {"system_id": "", "web_gui_url": "", "client": "", "language": "ZH",
            "allowed_origins": [], "login_mode": "manual"},
    "mcp": {"username": "", "connections": fixed_mcp_connections()},
    "browser_service_ref": "",
    "max_sessions": 4,
    "idle_seconds": 900,
}


def _object(value, keys):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise WorkbenchError("invalid_config")
    return value


def _string(value, maximum=256):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise WorkbenchError("invalid_config")
    return value.strip()


def identifier(value, allow_empty=True):
    value = _string(value, 160)
    if (not value and not allow_empty) or (value and not re.fullmatch(r"[A-Za-z0-9_.:-]+", value)):
        raise WorkbenchError("invalid_identifier")
    if value in {".", ".."}:
        raise WorkbenchError("invalid_identifier")
    return value


def _url(value, *, sap=False, origin=False):
    value = _string(value, 2048)
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and not parsed.username and not parsed.password and not parsed.fragment
                 and "\\" not in value and " " not in value)
        if not valid or port == 0:
            raise ValueError()
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if query and (not sap or any(key not in {"sap-client", "sap-language", "~transaction"}
                                    for key, _ in query)):
            raise ValueError()
        if origin and (parsed.path not in {"", "/"} or parsed.query):
            raise ValueError()
    except ValueError:
        raise WorkbenchError("invalid_url") from None
    return value.rstrip("/") if origin else value


def validate_config(value):
    """Accept only named non-secret fields. A draft can omit connection values."""
    _object(value, DEFAULT_CONFIG)
    result = deepcopy(DEFAULT_CONFIG)
    for key in ("enabled", "automation_enabled", "commit_enabled"):
        flag = value.get(key, False)
        if not isinstance(flag, bool):
            raise WorkbenchError("invalid_config")
        result[key] = flag
    if result["commit_enabled"] and not result["automation_enabled"]:
        raise WorkbenchError("invalid_flags")
    if result["automation_enabled"] and not result["enabled"]:
        raise WorkbenchError("invalid_flags")
    for key in ("coding_agent_id", "browser_service_ref"):
        result[key] = identifier(value.get(key, ""))
    for key, lower, upper in (("max_sessions", 1, 100), ("idle_seconds", 60, 86400)):
        number = value.get(key, result[key])
        if type(number) is not int or not lower <= number <= upper:
            raise WorkbenchError("invalid_config")
        result[key] = number
    sap = _object(value.get("sap", {}), result["sap"])
    result["sap"].update(sap)
    sap = result["sap"]
    sap["system_id"] = identifier(sap["system_id"])
    sap["web_gui_url"] = _url(sap["web_gui_url"], sap=True)
    sap["client"] = _string(sap["client"], 3)
    if sap["client"] and not re.fullmatch(r"[0-9]{3}", sap["client"]):
        raise WorkbenchError("invalid_client")
    sap["language"] = _string(sap["language"], 5).upper()
    if sap["language"] and not re.fullmatch(r"[A-Z]{1,2}(?:-[A-Z]{2})?", sap["language"]):
        raise WorkbenchError("invalid_language")
    sap["login_mode"] = _string(sap["login_mode"])
    if sap["login_mode"] not in {"password", "manual", "sso"}:
        raise WorkbenchError("invalid_config")
    # Legacy password mode predates the explicit decision to maintain MCP
    # credentials separately. It must not advertise shared Web GUI sign-in.
    if sap['login_mode'] == 'password':
        sap['login_mode'] = 'manual'
    for key, field in (("sap-client", "client"), ("sap-language", "language")):
        values = [v.upper() for k, v in parse_qsl(urlsplit(sap["web_gui_url"]).query) if k == key]
        if len(values) > 1 or (values and sap[field] and values[0] != sap[field]):
            raise WorkbenchError("sap_parameter_conflict")
    origins = sap["allowed_origins"]
    if not isinstance(origins, list) or len(origins) > 20:
        raise WorkbenchError("invalid_config")
    sap["allowed_origins"] = list(dict.fromkeys(_url(v, origin=True) for v in origins))
    if "" in sap["allowed_origins"]:
        raise WorkbenchError("invalid_url")
    mcp = _object(value.get("mcp", {}), {"username", "connections"})
    result["mcp"]["username"] = _string(mcp.get("username", ""), 64)
    connections = mcp.get("connections", [])
    if not isinstance(connections, list) or len(connections) > 20:
        raise WorkbenchError("invalid_config")
    fixed = {entry["id"]: entry for entry in result["mcp"]["connections"]}
    ids = set()
    for raw in connections:
        defaults = {"id": "", "transport": "remote", "url": "", "profile_ref": "",
                    "enabled": True, "credential_source": "scene_config",
                    "transport_auth": "none", "service_credential_ref": ""}
        _object(raw, defaults)
        entry = {**defaults, **raw}
        entry["id"] = identifier(entry["id"], allow_empty=False)
        if entry["id"] in ids:
            raise WorkbenchError("duplicate_mcp")
        ids.add(entry["id"])
        # Upgrade earlier scene drafts to the user-requested configured account.
        if entry["credential_source"] not in ("sap_session", "scene_config"):
            raise WorkbenchError("mcp_credentials_required")
        raw = {**raw, "credential_source": "scene_config"}
        if type(entry["enabled"]) is not bool:
            raise WorkbenchError("invalid_config")
        preset = fixed.get(entry["id"])
        if preset is None or any(value != preset[key] for key, value in raw.items() if key != "enabled"):
            raise WorkbenchError("mcp_connection_fixed")
        preset["enabled"] = entry["enabled"]
    return result


def configuration_checks(config, coding=None, opencode=None, *, mcp_password_configured=False):
    """Configuration inspection only. Never imply remote connection success."""
    coding, opencode = coding or {}, opencode or {}
    configured = {
        "sap": bool(config["sap"]["system_id"] and config["sap"]["web_gui_url"]),
        "opencode": bool(opencode.get("configured") and opencode.get("web_url")),
        "project": bool(coding.get("project_dir")),
        "mcp": any(c["enabled"] for c in config["mcp"]["connections"]),
        "mcp_credentials": bool(config["mcp"]["username"] and mcp_password_configured),
        "browser": config["browser_service_ref"] in BROWSER_SERVICE_REFS,
    }
    return [{"id": key, "configuration": "configured" if present else "missing",
             "verification": "not_run"} for key, present in configured.items()]


def capabilities(config):
    # This is the implemented feature set. Individual sessions still verify
    # the real browser, login state and control lease before page operations.
    blockers = ["business_submission_unavailable"]
    if not config["enabled"]:
        blockers.insert(0, "disabled")
    node_ready = config['browser_service_ref'] in BROWSER_SERVICE_REFS
    if not node_ready:
        blockers.insert(0, 'browser_service_unavailable')
    # Node selection only identifies the implementation. Allocation verifies
    # the executable, project and assets before starting either side.
    return {"configuration": True, "visual": bool(config["enabled"] and node_ready),
            "automation": bool(config["enabled"] and node_ready and config["automation_enabled"]),
            "commit": False, "blockers": blockers}
