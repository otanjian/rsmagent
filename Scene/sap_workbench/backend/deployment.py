"""Deployment choices owned only by the SAP workbench scene."""
from types import MappingProxyType
from urllib.parse import urlsplit


MCP_ENDPOINTS = MappingProxyType({
    "sap-abap": "http://127.0.0.1:8100/mcp",
    "sap-pyrfc": "http://127.0.0.1:8200/mcp",
})
SAP_TEST_ORIGIN = "https://sap.goodsap.cn:44300"
BROWSER_SERVICE_REF = 'sap-browser-worker'
# Early scene configurations stored this name for the same local worker.
BROWSER_SERVICE_REFS = frozenset({BROWSER_SERVICE_REF, 'local'})


def fixed_mcp_connections():
    return [{"id": name, "transport": "remote", "url": url, "profile_ref": "",
             "enabled": True, "credential_source": "scene_config",
             "transport_auth": "none", "service_credential_ref": ""}
            for name, url in MCP_ENDPOINTS.items()]


def verify_sap_tls(url):
    """User-approved test exception (2026-10-03); all other origins verify TLS.

    This affects the gateway's SAP connection, never the browser, global HTTP
    clients or certificate stores. No Chrome certificate exception is reused.
    """
    target = urlsplit(url)
    return not (target.scheme == "https" and target.hostname == "sap.goodsap.cn"
                and target.port == 44300 and not target.username and not target.password)
