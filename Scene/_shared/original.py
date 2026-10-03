"""Compatibility names for scene modules; all implementations use normal imports."""
import importlib

MODULES = {
    "scheduling": "production_plan/backend/api.py",
    "scheduler_engine": "production_plan/backend/scheduler_engine.py",
    "gantt_generator": "production_plan/backend/gantt_generator.py",
    "bom_tree": "production_plan/backend/bom_tree.py",
    "airbag": "production_scheduling_airbag/backend/api.py",
    "sap_api": "sap_data_analysis/backend/api.py",
}
for folder, names in {
    "_shared": ["WorkbenchUploadHandler", "WorkbenchParseExcelHandler", "ErpConnectionsHandler", "ErpConnectionsOptionsHandler"],
    "procurement_analysis": ["WorkbenchGenerateReportHandler", "ProcurementImportHandler", "ProcurementErpSyncHandler"],
    "finance_voucher": ["VoucherTemplateHandler"],
}.items():
    MODULES.update({name: f"{folder}/backend/{name}.py" for name in names})


def load(name):
    if name == "sap" or name.startswith("sap."):
        module = "Scene.sap_data_analysis.backend." + name
    else:
        module = "Scene." + MODULES[name].removesuffix(".py").replace("/", ".")
    return importlib.import_module(module)
