"""Personal model choices, shared by the standalone and embedded Web client.

Only display and selection data are stored. This never configures providers,
credentials or access rights. Each write is a small operation applied to the
latest row in a transaction, so concurrent clients do not replace each other.
"""
import json

import web

from agent.coding import resolve_settings
from agent.coding.browser_auth import same_origin
from channel.web.auth_handlers import _get_service, _require_context
from channel.web.fork.handlers.coding import _coding_error, _run_coding, _success


def _invalid():
    _coding_error("invalid model preference", "400 Bad Request", "invalid_request")


def _text(value, maximum=200):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        _invalid()
    return value


def _model(value):
    if not isinstance(value, dict) or set(value) != {"providerID", "modelID"}:
        _invalid()
    return {key: _text(value[key]) for key in ("providerID", "modelID")}


def _empty():
    return {"user": [], "recent": [], "variant": {}, "session": {}}


def _apply(data, operation):
    if not isinstance(operation, dict):
        _invalid()
    kind = operation.get("type")
    if kind in {"visibility", "select"}:
        if set(operation) != ({"type", "model", "visible"} if kind == "visibility" else {"type", "model"}):
            _invalid()
        model = _model(operation.get("model"))
        visible = operation.get("visible", True)
        if not isinstance(visible, bool):
            _invalid()
        found = next((item for item in data["user"] if all(item[key] == model[key] for key in model)), None)
        if found is None:
            found = dict(model)
            data["user"].append(found)
        found["visibility"] = "show" if visible else "hide"
        if kind == "select":
            data["recent"] = [model] + [item for item in data["recent"] if item != model][:4]
    elif kind == "variant":
        if set(operation) != {"type", "model", "value"}:
            _invalid()
        model = _model(operation.get("model"))
        key = model["providerID"] + "/" + model["modelID"]
        if operation["value"] is None:
            data["variant"].pop(key, None)
        else:
            data["variant"][key] = _text(operation["value"])
    elif kind == "session":
        if set(operation) != {"type", "id", "value"}:
            _invalid()
        sid = _text(operation.get("id"))
        value = operation.get("value")
        if not sid.startswith("ses_") or not isinstance(value, dict) or set(value) - {"agent", "model", "variant"}:
            _invalid()
        result = {}
        if "agent" in value:
            result["agent"] = _text(value["agent"])
        if "model" in value:
            result["model"] = _model(value["model"])
        if "variant" in value:
            result["variant"] = None if value["variant"] is None else _text(value["variant"])
        data["session"].pop(sid, None)
        data["session"][sid] = result
        while len(data["session"]) > 500:
            data["session"].pop(next(iter(data["session"])))
    else:
        _invalid()
    if len(data["user"]) > 2000 or len(data["variant"]) > 2000:
        _invalid()


def _initial(value):
    if not isinstance(value, dict) or set(value) - {"user", "recent", "variant"}:
        _invalid()
    data = _empty()
    if not isinstance(value.get("user", []), list) or not isinstance(value.get("recent", []), list):
        _invalid()
    for item in value.get("user", []):
        if not isinstance(item, dict) or item.get("visibility") not in {"show", "hide"}:
            _invalid()
        _apply(data, {"type": "visibility", "model": {key: item.get(key) for key in ("providerID", "modelID")},
                      "visible": item["visibility"] == "show"})
    recent = value.get("recent", [])
    if len(recent) > 5:
        _invalid()
    data["recent"] = [_model(item) for item in recent]
    variants = value.get("variant", {})
    if not isinstance(variants, dict) or len(variants) > 2000:
        _invalid()
    data["variant"] = {_text(key, 401): _text(item) for key, item in variants.items() if item is not None}
    return data


class CodingModelPreferencesHandler:
    def GET(self):
        return _run_coding(lambda: self._request(False))

    def POST(self):
        return _run_coding(lambda: self._request(True))

    def _request(self, write):
        ctx = _require_context()
        settings = resolve_settings()
        if not settings.enabled or not settings.browser_sso:
            _coding_error("shared model preferences unavailable", "404 Not Found", "not_found")
        web.header("Cache-Control", "no-store", unique=True)
        body = None
        if write:
            if not same_origin(settings.web_url, web.ctx.env.get("HTTP_ORIGIN", "")):
                _coding_error("cross-origin request refused", "403 Forbidden", "cross_origin")
            raw = web.data()
            if len(raw) > 256 * 1024:
                _invalid()
            try:
                body = json.loads(raw)
            except (ValueError, TypeError):
                _invalid()
            if not isinstance(body, dict) or set(body) not in ({"initialize"}, {"operations"}):
                _invalid()
        with _get_service()._store.connect() as con:
            if write:
                con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT preferences, revision FROM coding_model_preferences WHERE user_id=? AND service_id=?",
                              (ctx.user_id, settings.service_id)).fetchone()
            data, revision = (json.loads(row["preferences"]), row["revision"]) if row else (None, 0)
            if write:
                if "initialize" in body:
                    initial = _initial(body["initialize"])
                    if data is None:
                        data, revision = initial, 1
                else:
                    operations = body["operations"]
                    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
                        _invalid()
                    data = data or _empty()
                    for operation in operations:
                        _apply(data, operation)
                    revision += 1
                con.execute("""INSERT INTO coding_model_preferences(user_id, service_id, preferences, revision)
                    VALUES (?, ?, ?, ?) ON CONFLICT(user_id, service_id) DO UPDATE
                    SET preferences=excluded.preferences, revision=excluded.revision""",
                    (ctx.user_id, settings.service_id, json.dumps(data), revision))
                con.commit()
        return _success({"preferences": data, "revision": revision})
