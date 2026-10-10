"""Deployment choices owned only by the SAP workbench scene."""
import os
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit


MCP_ENDPOINTS = MappingProxyType({
    # On this deployment host port 8100 is owned by weknora-lite, so the
    # sap-abap gateway from C:\\rdai\\scripts\\start-sap-mcp.ps1 listens on 8110
    # instead. sap-pyrfc keeps its default 8200.
    "sap-abap": "http://127.0.0.1:8110/mcp",
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


#: Optional trusted HTTPS origin that serves the SAP Web GUI to the browser
#: through a reverse proxy. It exists for hosts whose own certificate a browser
#: rejects (an untrusted/expired one cannot be accepted from inside an iframe),
#: so the visible pane loads the proxy's normally verified certificate instead.
#: Only the browser-facing iframe and its transaction navigation use it; the MCP
#: channel, the credential binding and the backend connections keep the real
#: configured SAP address. Unset (the default) leaves the iframe on the direct
#: SAP URL, so this is inert until a proxy is actually deployed in front.
DISPLAY_ORIGIN_ENV = 'SAP_WORKBENCH_DISPLAY_ORIGIN'


def browser_display_origin():
    """The configured https origin for the browser iframe, or '' when unset.

    A bare origin only: no path, query, fragment or credentials, exactly because
    the SAP path and query must survive the rehosting in :func:`browser_sap_url`.
    """
    raw = (os.environ.get(DISPLAY_ORIGIN_ENV) or '').strip()
    if not raw:
        return ''
    target = urlsplit(raw)
    if (target.scheme != 'https' or not target.hostname or target.username
            or target.password or target.path not in ('', '/') or target.query
            or target.fragment):
        return ''
    return 'https://' + target.netloc


def browser_sap_url(web_gui_url):
    """What the visible iframe loads: the configured SAP URL, or its exact path
    and query rehosted on :func:`browser_display_origin` when a reverse proxy is
    set. Rehosting keeps every backend consumer on the real SAP address."""
    origin = browser_display_origin()
    if not origin:
        return web_gui_url
    parts = urlsplit(web_gui_url)
    return urlunsplit((urlsplit(origin).scheme, urlsplit(origin).netloc,
                       parts.path, parts.query, ''))


def verify_sap_tls(url):
    """User-approved test exception (2026-10-03); all other origins verify TLS.

    This affects the gateway's SAP connection, never the browser, global HTTP
    clients or certificate stores. No Chrome certificate exception is reused.
    """
    target = urlsplit(url)
    return not (target.scheme == "https" and target.hostname == "sap.goodsap.cn"
                and target.port == 44300 and not target.username and not target.password)
