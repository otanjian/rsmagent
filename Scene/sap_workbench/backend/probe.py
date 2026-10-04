"""Bounded, read-only connection checks. No conversation or model admission."""
import asyncio
from types import SimpleNamespace

from aiohttp import ClientSession, ClientTimeout, ClientConnectorCertificateError

from .configuration import WorkbenchError
from .mcp_runtime import ConfiguredMcp
from .environment import runtime_paths, local_checks


MCP_CHECK_TIMEOUT = 50


async def probe_connections(store, tenant, user, token, saved, coding):
    from auth.runtime import resolve_context, IdentityContextError
    from auth.service import get_identity_service
    from agent.coding import resolve_settings
    from .http import runtime_coding

    async def authorize():
        try:
            ctx = resolve_context(get_identity_service(), token, tenant)
        except IdentityContextError as error:
            code = 'platform_login_required' if error.status == 401 else 'session_forbidden'
            raise WorkbenchError(code, error.status) from None
        if ctx.user_id != user or store.read_config(tenant)['version'] != saved['version']:
            raise WorkbenchError('config_conflict', 409)
        runtime_coding(ctx, coding['id'])
        return ctx

    await authorize()
    results = local_checks(runtime_paths(coding.get('project_dir')), saved['config']['browser_service_ref'])
    async with ClientSession(timeout=ClientTimeout(total=10), trust_env=False) as client:
        try:
            async with client.get(saved['config']['sap']['web_gui_url'], allow_redirects=False) as response:
                state = 'passed' if response.status in {200, 301, 302, 303, 401} else 'failed'
                check = {'id': 'sap', 'verification': state, 'http_status': response.status}
            results.append(check)
        except ClientConnectorCertificateError:
            results.append({'id': 'sap', 'verification': 'certificate_untrusted'})
        except Exception:
            results.append({'id': 'sap', 'verification': 'failed'})
        settings = resolve_settings()
        try:
            async with client.get(settings.api_url.rstrip('/') + '/global/health', headers=settings.auth_headers()) as response:
                ready = response.status == 200 and (await response.json()).get('healthy') is True
                check = {'id': 'opencode', 'verification': 'passed' if ready else 'failed', 'http_status': response.status}
                if response.status in {401, 403}:
                    check['reason'] = 'opencode_auth_failed'
            results.append(check)
        except Exception:
            results.append({'id': 'opencode', 'verification': 'failed'})
    worker = ConfiguredMcp(SimpleNamespace(authorize=authorize, store=store, tenant=tenant, row={'config_version': saved['version']}))
    try:
        # Covers spawn and pipe drain as well as the worker's initialization.
        # Two HTTP checks (10s each) + 50s MCP + <=14s cleanup fit below the
        # HTTP handler's 90s deadline, including failure paths.
        async with asyncio.timeout(MCP_CHECK_TIMEOUT):
            ready = await worker.start()
        results.extend({'id': name, 'verification': 'passed'} for name in ready['connections'])
    except asyncio.TimeoutError:
        results.append({'id': 'mcp', 'verification': 'failed', 'reason': 'mcp_check_timeout'})
    except WorkbenchError as error:
        safe_codes = {'mcp_runtime_missing', 'mcp_credentials_required', 'credential_crypto_unavailable',
                      'mcp_identity_tools_missing', 'mcp_identity_mismatch', 'mcp_not_configured',
                      'mcp_login_failed', 'mcp_login_protocol_unsupported'}
        results.append({'id': 'mcp', 'verification': 'failed',
                        'reason': error.code if error.code in safe_codes else 'mcp_login_failed'})
    except Exception:
        results.append({'id': 'mcp', 'verification': 'failed', 'reason': 'mcp_login_failed'})
    finally:
        await worker.close()
    await authorize()
    return results
