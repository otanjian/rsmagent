"""Private provider relay: uses configured model, enforces existing IAM quota."""
import json
import calendar
from contextlib import suppress
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from .configuration import WorkbenchError

# SAP page snapshots and ADT discovery results can exceed 60 KB after only a
# few turns. Bound transport bytes separately from the model context window.
MAX_REQUEST_BYTES = 512 * 1024
MAX_OUTPUT_TOKENS = 4096


def validate_request(body, model):
    from aiohttp import web
    if not isinstance(body, dict) or body.get('model') != model:
        raise web.HTTPForbidden()
    messages = body.get('messages')
    if not isinstance(messages, list) or not messages:
        raise web.HTTPBadRequest(reason='Messages are required')
    for message in messages:
        if not isinstance(message, dict):
            raise web.HTTPBadRequest(reason='Invalid message')
        content = message.get('content')
        if content is not None and not isinstance(content, (str, list)):
            raise web.HTTPBadRequest(reason='Only text input is supported')
        if isinstance(content, list) and any(not isinstance(part, dict) or part.get('type') != 'text'
                or not isinstance(part.get('text'), str) for part in content):
            raise web.HTTPBadRequest(reason='Only text input is supported')
    if type(body.get('n', 1)) is not int or body.get('n', 1) != 1:
        raise web.HTTPBadRequest(reason='Only one completion is supported')
    for key in ('max_tokens', 'max_completion_tokens'):
        if key in body and (type(body[key]) is not int or body[key] < 1):
            raise web.HTTPBadRequest(reason='Invalid output token limit')
    body['max_tokens'] = min(body.pop('max_completion_tokens', body.get('max_tokens', MAX_OUTPUT_TOKENS)), MAX_OUTPUT_TOKENS)
    return body


async def configured_model(client):
    from agent.coding import resolve_settings
    settings = resolve_settings()
    async with client.get(settings.api_url.rstrip('/') + '/global/config',
                          headers=settings.auth_headers()) as response:
        if response.status != 200:
            raise WorkbenchError("model_configuration_unavailable", 503)
        config = await response.json()
    selection = config.get("model", "")
    provider, _, model = selection.partition("/")
    entry = config.get("provider", {}).get(provider, {})
    options = entry.get("options", {})
    endpoint = options.get("baseURL")
    if not endpoint or urlsplit(endpoint).scheme != "https" or not model:
        raise WorkbenchError("model_configuration_unsupported", 503)
    key = options.get("apiKey")
    if not key:
        # Read only the selected provider from the same local service's auth
        # store. Never copy auth.json into the scene host or project directory.
        auth_path = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode/auth.json"
        try:
            auth = json.loads(auth_path.read_text()).get(provider, {})
        except (OSError, ValueError):
            auth = {}
        if auth.get("type") == "api":
            key = auth.get("key")
    if not key:
        raise WorkbenchError("model_credentials_missing", 503)
    return {"provider": provider, "model": model, "url": endpoint.rstrip('/') + '/chat/completions', "key": key}


async def relay(runtime, request):
    from aiohttp import web, ClientTimeout
    from auth.service import get_identity_service
    await runtime.authorize()
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise web.HTTPBadRequest(reason='Invalid model request') from None
    body = validate_request(body, runtime.model['model'])
    # Text and the schema are tokenized by a byte-level tokenizer. Reserve a
    # conservative byte bound plus output cap before admitting provider work.
    # Unknown usage keeps the reservation; it is never silently refunded.
    encoded = json.dumps(body, ensure_ascii=False).encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_REQUEST_BYTES, actual_size=len(encoded))
    reserve = len(encoded) + MAX_OUTPUT_TOKENS
    # Parsing can await a body upload. Recheck before any new provider or
    # quota consumption if the platform/configuration changed in that gap.
    await runtime.authorize()
    svc = get_identity_service()
    quota_window = calendar.timegm(time.gmtime())
    try:
        admitted = svc.consume_quota(user_id=runtime.user, tenant_id=runtime.tenant, metric="tokens", amount=reserve)
    except Exception:
        raise WorkbenchError("model_quota_unavailable", 503) from None
    if not admitted:
        raise web.HTTPTooManyRequests(reason="Model token quota exhausted")
    reference = "sap-model-" + uuid.uuid4().hex
    try:
        try:
            runtime.audit("model.reserve", {"reference": reference, "tokens": reserve})
        except Exception:
            raise WorkbenchError("audit_unavailable", 503) from None
        await runtime.authorize()
    except BaseException:
        # Nothing has reached the provider yet. Release this reservation using
        # the existing meter, but never reduce a newer time bucket's usage.
        if calendar.timegm(time.gmtime()) == quota_window:
            with suppress(Exception):
                svc.refund_quota(user_id=runtime.user, tenant_id=runtime.tenant, metric="tokens", amount=reserve,
                                 reference=reference, reason="SAP model refused before dispatch")
        raise
    chunks = bytearray()
    usage_truncated = False
    upstream = None
    try:
        upstream = await runtime.client.post(runtime.model["url"], json=body,
                         headers={"Authorization": "Bearer " + runtime.model["key"]},
                         timeout=ClientTimeout(total=180), allow_redirects=False)
        if upstream.status != 200:
            raise web.HTTPBadGateway(reason="Model request failed")
        result = web.StreamResponse(headers={"Content-Type": upstream.headers.get("Content-Type", "application/json"), "Cache-Control": "no-store"})
        await result.prepare(request)
        async for chunk in upstream.content.iter_any():
            await runtime.authorize()
            if len(chunks) + len(chunk) <= 2 * 1024 * 1024 and not usage_truncated:
                chunks.extend(chunk)
            else:
                usage_truncated = True
                chunks.clear()
            await result.write(chunk)
        await result.write_eof()
        usage = None
        lines = bytes(chunks).splitlines() if 'text/event-stream' in upstream.headers.get('Content-Type', '') else [bytes(chunks)]
        for line in lines:
            if line.startswith(b'data: '):
                line = line[6:]
            try:
                item = json.loads(line)
                candidate = item.get("usage", {}).get("total_tokens")
                if type(candidate) is int and 0 < candidate <= reserve:
                    usage = candidate
            except (ValueError, AttributeError):
                pass
        refunded = 0
        # The shared refund API targets its current time bucket, not a supplied
        # reservation. Never let a delayed reply reduce a newer request's use.
        if usage is not None and usage < reserve and calendar.timegm(time.gmtime()) == quota_window:
            refunded = svc.refund_quota(user_id=runtime.user, tenant_id=runtime.tenant, metric="tokens", amount=reserve - usage,
                                       reference=reference, reason="SAP model actual usage")
        runtime.audit("model.complete", {"reference": reference, "reserved_tokens": reserve,
                                         "charged_tokens": reserve - refunded,
                                         "reported_tokens": usage, "usage_verified": usage is not None})
        return result
    finally:
        if upstream:
            upstream.close()
