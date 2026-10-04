"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from bridge.context import *
from channel.web.branding import (
    BrandingError,
    BrandingService,
    create_service as create_branding_service,
)
from common.log import logger
import json
import web


def _branding_service() -> BrandingService:
    return create_branding_service()


def _branding_require_platform_admin() -> "RequestContext":
    """Resolve and authorize the context for a database-mode brand write.

    Returns the resolved ``RequestContext`` so callers can attribute the audit
    event to the acting platform admin. Raises 401/403 via ``_require_context``
    / ``_require_platform_admin`` for a missing session or a non-admin.
    """
    from channel.web.auth_handlers import _require_context
    from channel.web.admin_handlers import _require_platform_admin
    ctx = _require_context()
    _require_platform_admin(ctx)
    return ctx


def _branding_origin_ok() -> bool:
    """Verify the Origin / Referer is same-origin for a cookie-authorized write.

    The desktop client renders from a file:// origin and authenticates via the
    Authorization bearer header; it has no browser Origin, which is acceptable
    because bearer writes are not cookie-bound. Browsers send an Origin on
    POST; if present it must match the request host.
    """
    origin = web.ctx.env.get("HTTP_ORIGIN", "") or web.ctx.env.get("HTTP_REFERER", "") or ""
    if not origin:
        return False
    from urllib.parse import urlparse
    try:
        source = urlparse(origin)
        target = urlparse(web.ctx.env.get("wsgi.url_scheme", "http") + "://" + web.ctx.env.get("HTTP_HOST", ""))
        return (source.scheme in ("http", "https")
                and not source.username and not source.password
                and (source.scheme, source.hostname, source.port or (443 if source.scheme == "https" else 80))
                == (target.scheme, target.hostname, target.port or (443 if target.scheme == "https" else 80)))
    except Exception:
        return False


def _branding_require_write():
    """Brand writes require a platform admin (database identity only)."""
    return _branding_require_platform_admin()


def _branding_record_audit(ctx, action: str, record: dict, *, reset: bool = False) -> None:
    """Record a sanitized brand audit event against ``identity.db`` (best-effort).

    Called only in database mode where ``ctx`` is the resolved platform admin;
    a non-None ``ctx`` is required. A failure to record must never roll back the
    committed brand, so it is logged and swallowed.
    """
    if ctx is None:
        return
    try:
        from channel.web.auth_handlers import _get_service
        changes = {
            "brand_name": record.get("brand_name"),
            "logo_description": record.get("logo_description"),
        }
        if reset:
            changes["reset_to_default"] = True
        _get_service()._audit.record(
            actor_user_id=ctx.user_id,
            actor_username=ctx.username,
            tenant_id=None,
            target_tenant_id=None,
            action=action,
            target="brand",
            redacted_changes=changes,
            result="success",
        )
    except Exception:
        logger.exception("[BrandingAudit] failed to record audit event")


def _branding_management_payload(service, record=None, ctx=None):
    allowed = ctx is not None
    payload = service.management_payload(allowed, "", record=record)
    payload["status"] = "success"
    return payload


def _branding_error_response(err: BrandingError) -> str:
    web.header('Content-Type', 'application/json; charset=utf-8')
    from http import HTTPStatus
    web.ctx.status = f"{err.http_status} {HTTPStatus(err.http_status).phrase}"
    web.header('Cache-Control', 'no-store')
    return json.dumps({
        "status": "error",
        "code": err.code,
        "message": err.message,
        "field": err.field,
    }, ensure_ascii=False)


class BrandingPublicHandler:
    """GET /api/branding/public - unauthenticated minimal brand read."""

    def GET(self):
        from channel.web.web_channel import _branding_service
        from channel.web.fork.common import _help_site_url
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        try:
            payload = _branding_service().public_payload()
        except Exception as e:
            logger.exception(f"[BrandingPublicHandler] failed: {e}")
            # Fall back to the built-in default so login/nav never breaks.
            payload = {
                "enabled": False,
                "revision": 0,
                "brand_name": "容大AI",
                "logo_description": "工作台",
                "logo_url": "/assets/rongda-ai-mark.svg",
                "favicon_url": "/assets/favicon.ico",
            }
        # The 「帮助与关于」 target: the site declares its own address, so a read
        # failure lands on the local-development default instead of a dead link.
        payload["help_url"] = _help_site_url()
        return json.dumps(payload, ensure_ascii=False)


class BrandingManageHandler:
    """GET /api/branding and POST /api/branding (management read + save)."""

    def GET(self):
        from channel.web.web_channel import _branding_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        try:
            ctx = _branding_require_platform_admin()
            payload = _branding_management_payload(_branding_service(), ctx=ctx)
        except web.HTTPError:
            raise
        except BrandingError as e:
            return _branding_error_response(e)
        except Exception as e:
            logger.exception(f"[BrandingManageHandler] GET failed: {e}")
            return _branding_error_response(BrandingError("storage_error", "无法读取品牌设置", 500))
        return json.dumps(payload, ensure_ascii=False)

    def POST(self):
        from channel.web.web_channel import _branding_service
        from channel.web.web_channel import _raw_web_input
        from channel.web.fork.runtime import _read_uploaded_file_bytes_limited
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        try:
            audit_ctx = _branding_require_write()
            params = _raw_web_input()

            def _scalar(value, default=""):
                # web.py merges the query string and form body, so a field
                # present in both arrives as a list. Collapse to a scalar.
                if isinstance(value, (list, tuple)):
                    return value[0] if value else default
                return value if value is not None else default

            expected = _scalar(params.get("expected_revision"))
            brand_name = _scalar(params.get("brand_name", ""))
            logo_description = _scalar(params.get("logo_description", ""))
            logo_action = _scalar(params.get("logo_action", ""), "keep")

            try:
                expected_revision = int(expected)
            except (TypeError, ValueError):
                raise BrandingError("missing_expected_revision", "缺少版本号", 400)

            file_obj = params.get("logo")
            logo_file = None
            if file_obj is not None:
                if isinstance(file_obj, (list, tuple)):
                    raise BrandingError("conflicting_logo_action", "只能上传一个 Logo", 400)
                filename = getattr(file_obj, "filename", "") or "logo.png"
                try:
                    data = _read_uploaded_file_bytes_limited(file_obj, 2 * 1024 * 1024 + 1)
                except ValueError as exc:
                    raise BrandingError("image_too_large", "图片不能超过 2 MiB", 413) from exc
                logo_file = (filename, data)

            service = _branding_service()
            operator = audit_ctx.username if audit_ctx is not None else "console"
            record = service.save(
                expected_revision=expected_revision,
                brand_name=brand_name,
                logo_description=logo_description,
                logo_action=logo_action,
                logo_file=logo_file,
                operator=operator,
            )
            _branding_record_audit(audit_ctx, "branding.update", record)
            payload = _branding_management_payload(service, record=record, ctx=audit_ctx)
            return json.dumps(payload, ensure_ascii=False)
        except BrandingError as e:
            logger.warning(f"[BrandingManageHandler] POST rejected: {e.code}: {e.message}")
            return _branding_error_response(e)
        except Exception as e:
            logger.exception(f"[BrandingManageHandler] POST failed: {e}")
            return _branding_error_response(BrandingError("storage_error", "品牌保存失败", 500))


class BrandingResetHandler:
    """POST /api/branding/reset - reset to built-in defaults (full confirm)."""

    def POST(self):
        from channel.web.web_channel import _branding_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        try:
            audit_ctx = _branding_require_write()
            try:
                data = json.loads(web.data() or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("expected object")
            except ValueError as exc:
                raise BrandingError("invalid_request", "请求 JSON 无效", 400) from exc
            expected = data.get("expected_revision")
            try:
                expected_revision = int(expected)
            except (TypeError, ValueError):
                raise BrandingError("missing_expected_revision", "缺少版本号", 400)
            service = _branding_service()
            operator = audit_ctx.username if audit_ctx is not None else "console"
            record = service.reset(expected_revision, operator=operator)
            _branding_record_audit(audit_ctx, "branding.reset", record, reset=True)
            payload = _branding_management_payload(service, record=record, ctx=audit_ctx)
            return json.dumps(payload, ensure_ascii=False)
        except BrandingError as e:
            logger.warning(f"[BrandingResetHandler] rejected: {e.code}: {e.message}")
            return _branding_error_response(e)
        except Exception as e:
            logger.exception(f"[BrandingResetHandler] failed: {e}")
            return _branding_error_response(BrandingError("storage_error", "品牌重置失败", 500))


class BrandingAssetHandler:
    """GET /api/branding/assets/<asset-id>.png - public referenced asset."""

    def GET(self, asset_id):
        from channel.web.web_channel import _branding_service
        try:
            mime, data = _branding_service().resolve_asset(asset_id)
        except BrandingError as e:
            # Disabled feature or unknown asset -> 404 + built-in fallback is the
            # client's job. Do not leak internals here.
            return _branding_error_response(e)
        except OSError:
            return _branding_error_response(BrandingError("storage_error", "无法读取品牌图片", 500))
        web.header('Content-Type', mime)
        web.header('X-Content-Type-Options', 'nosniff')
        web.header('Cache-Control', 'public, max-age=31536000, immutable')
        return data


