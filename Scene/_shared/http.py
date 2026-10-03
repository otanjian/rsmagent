"""Authenticated HTTP adapters around the original scene handler classes."""
import json
import re
from pathlib import Path

import web

from Scene._shared import host
from Scene._shared.original import load


def validate_body(body):
    if not isinstance(body, dict):
        raise web.badrequest("JSON object required")
    root = Path(host._get_workspace_root()).resolve()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"session_id", "schedule_id"} and item:
                    if not isinstance(item, str) or not re.fullmatch(r"[\w.-]{1,160}", item) or item in {".", ".."}:
                        raise web.badrequest("invalid identifier")
                if key == "filename" and item:
                    if not isinstance(item, str) or "/" in item or "\\" in item or item in {".", ".."}:
                        raise web.badrequest("invalid filename")
                if key in {"data_file", "data_file_path", "file_path", "filepath", "input_file"} and item:
                    candidate = Path(item).resolve()
                    if not candidate.is_relative_to(root):
                        raise web.forbidden("file must belong to the current tenant workspace")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(body)


def make_handler(module_name, class_name, methods):
    def method(verb):
        def handle(self, *args):
            from channel.web.web_channel import _db_scope, _require_chat_use, _build_preview_url
            from channel.web.auth_handlers import require_management_write
            if verb != "GET":
                require_management_write()
            with _db_scope() as ctx:
                _require_chat_use(ctx)
                token = host.request_context.set(ctx)
                try:
                    if class_name == "SchedulingHistoryDetailHandler" and args:
                        if not re.fullmatch(r"[\w-]{1,100}", args[0]):
                            raise web.badrequest("invalid schedule id")
                    if verb != "GET":
                        if class_name in {"ProcurementImportHandler", "SchedulingImportHandler"}:
                            uploaded = web.input(file={}).get("file")
                            if uploaded and getattr(uploaded, "filename", None):
                                validate_body({"filename": uploaded.filename})
                        else:
                            try:
                                body = json.loads(web.data() or b"{}")
                            except (ValueError, UnicodeDecodeError):
                                raise web.badrequest("invalid JSON")
                            validate_body(body)
                    original = getattr(load(module_name), class_name)()
                    result = getattr(original, verb)(*args)
                    if class_name == "WorkbenchUploadHandler":
                        from Scene.catalog import ROOT, read_catalog
                        data = json.loads(result)
                        scene_id = body.get("scene_id")
                        if scene_id in {s["id"] for s in read_catalog()["scenes"]}:
                            data["project_root"] = str(ROOT / scene_id)
                        result = json.dumps(data, ensure_ascii=False)
                    if class_name == "VoucherTemplateHandler":
                        data = json.loads(result)
                        if data.get("status") == "success":
                            data["download_url"] = _build_preview_url(data["filepath"])
                            data["file_url"] = data["download_url"]
                        result = json.dumps(data, ensure_ascii=False)
                    return result
                finally:
                    host.request_context.reset(token)
        return handle
    return type(class_name, (), {verb: method(verb) for verb in methods})


HANDLERS = {}
for name in ["WorkbenchUploadHandler", "WorkbenchParseExcelHandler", "WorkbenchGenerateReportHandler",
             "VoucherTemplateHandler", "ProcurementImportHandler", "ProcurementErpSyncHandler"]:
    HANDLERS[name] = make_handler(name, name, ("POST",))
#: ``ErpConnectionsHandler`` is GET-only (change ``add-external-system-access``,
#: task 6.5). Its POST was a second writable ERP form -- the scene bundle had its
#: own panel, its own field mapping and its own keep/replace/clear secret rules
#: against the same control plane the console page writes through. The panel is
#: gone and the address no longer answers a write, so there is nothing left here
#: to define a POST for; ``require_management_write`` above would have been the
#: only thing standing between an old client and a duplicated write path.
for name, methods in [("ErpConnectionsHandler", ("GET",)), ("ErpConnectionsOptionsHandler", ("GET",))]:
    HANDLERS[name] = make_handler(name, name, methods)
for name in ["Schedule", "Gantt", "MaterialCheck", "Bottleneck", "WhatIf", "Import", "BOMTree", "History", "HistoryDetail"]:
    cls = "Scheduling" + name + "Handler"
    HANDLERS[cls] = make_handler("scheduling", cls, ("GET",) if name.startswith("History") else ("POST",))
HANDLERS["AirbagSchedulingHandler"] = make_handler("airbag", "AirbagSchedulingHandler", ("GET", "POST", "PUT"))
HANDLERS["SceneSapAnalyzeHandler"] = make_handler("sap_api", "SapDataAnalysisHandler", ("POST",))
HANDLERS["SceneSapCsvHandler"] = make_handler("sap_api", "SapDataAnalysisHandler", ("GET",))
globals().update(HANDLERS)


class SceneCapabilitiesHandler:
    def GET(self):
        from channel.web.web_channel import _db_scope, _require_chat_use
        with _db_scope() as ctx:
            _require_chat_use(ctx)
            admin = ctx.is_platform_admin or ctx.is_tenant_admin
            web.header("Content-Type", "application/json; charset=utf-8")
            return json.dumps({"status": "success", "user": {
                "permissions": sorted(ctx.permissions),
                "roles": ["admin"] if admin else [],
            }})
