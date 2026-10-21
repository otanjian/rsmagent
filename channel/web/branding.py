# encoding:utf-8
"""Instance-level Web branding service.

This module owns the single source of truth for the Web console brand
(name, Logo description, Logo and derived favicon) for the *current deployed
instance*. It isolates the load/validate/normalize/versioned-publish logic from
the large route module so the routes only translate HTTP.

Scope and ownership (see ``openspec/changes/add-branding-settings``):

- Brand data lives under ``<get_data_root()>/branding/``; it is the only write
  source and MUST NOT be copied into ``config.json``.
- Branding is always available; no separate feature switch is required.
- This is a single-instance compatibility target only. Enterprise multi-user /
  multi-tenant deployment is gated by the audit-log capability slice plus
  per-request ``branding.manage`` authorization, which this module refuses to fake.

The module deliberately avoids importing the web route module so it stays
unit-testable and does not create import cycles.
"""

import errno
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
from typing import Dict, List, Optional, Tuple

from config import get_data_root

try:
    import fcntl  # POSIX (Linux / macOS)
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

try:
    from PIL import Image, ImageOps
    _HAS_PIL = True
except ImportError:  # pragma: no cover - Pillow is a declared dependency
    _HAS_PIL = False

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCHEMA_VERSION = 1

# Reject control chars (C0 + DEL) and line separators. We deliberately keep
# these as code-point checks rather than a regex on encoded bytes so astral
# (non-BMP) characters are counted as one code point each.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

BRAND_NAME_MIN = 1
BRAND_NAME_MAX = 32
LOGO_DESC_MAX = 100

# Image upload limits as explicitly decided for this slice.
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2 MiB
IMG_MIN_DIM = 32
IMG_MAX_DIM = 4096
IMG_MAX_PIXELS = 16_000_000
# Longest side of the normalized display Logo; favicon is a fixed square.
LOGO_MAX_EXHIBIT_SIDE = 1024
FAVICON_SIZE = 32

# Retained recoverable snapshots and asset retention window.
MAX_SNAPSHOTS = 5
ASSET_RETENTION_DAYS = 7

DEFAULT_BRAND_NAME = "容大AI"
DEFAULT_BRAND_DESC = "业务工作台"
# Bundled trusted resources, served by the existing static AssetsHandler.
DEFAULT_LOGO_URL = "/assets/rongda-ai-mark.svg"
DEFAULT_FAVICON_URL = "/assets/favicon.ico"

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP"}

LOGO_ACTIONS = ("keep", "replace", "default")


class BrandingError(Exception):
    """A domain error carrying a stable ``code`` and an optional ``field``.

    ``http_status`` is the recommended HTTP status output by the routes.
    """

    def __init__(self, code: str, message: str, http_status: int = 400, field: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.field = field


# --------------------------------------------------------------------------- #
# Cross-process file lock (POSIX fcntl, Windows msvcrt fallback)
# --------------------------------------------------------------------------- #

class _FileLock:
    """A tiny exclusive advisory lock around a lock file.

    Kept intentionally small: it is a commit-window lock, not a general
    concurrency primitive. Bounded wait avoids wedging a request when another
    writer holds the lock longer than expected.
    """

    def __init__(self, path: str, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout
        self._fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        # Create parent directory (the branding root) up front.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fd = open(self.path, "a+b")
        try:
            if fcntl is None:
                self._fd.seek(0, os.SEEK_END)
                if self._fd.tell() == 0:
                    self._fd.write(b"\0")
                    self._fd.flush()
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:  # Windows locks a byte range from the current offset.
                        import msvcrt
                        self._fd.seek(0)
                        msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except (IOError, OSError):
                    if time.time() > deadline:
                        raise BrandingError("storage_error", "品牌存储忙碌，请稍后重试", 503)
                    time.sleep(0.05)
        except BaseException:
            self._fd.close()
            self._fd = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fd is not None:
                if fcntl is not None:
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows
                    import msvcrt
                    self._fd.seek(0)
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        finally:
            if self._fd is not None:
                self._fd.close()
            self._fd = None
        return False


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #

def _normalize_text(value: str, minimum: int, maximum: int, field: str, label: str) -> str:
    """Trim and validate a single-line, control-char-free text field.

    Returns the trimmed string. Raises ``BrandingError`` on any violation.
    """
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise BrandingError(
            "invalid_" + field, f"{label}必须是纯文本", 400, field
        )
    # Strip leading/trailing whitespace, including full-width spaces.
    trimmed = value.strip()
    # Reject embedded newlines / control chars on the RAW value too: a newline
    # inside leading whitespace should not sneak through via a later slice.
    if _CONTROL_RE.search(value):
        raise BrandingError(
            "invalid_" + field, f"{label}不能包含换行或控制字符", 400, field
        )
    count = len(trimmed)
    if count < minimum or count > maximum:
        if minimum == 0:
            msg = f"{label}不能超过 {maximum} 个字符"
        else:
            msg = f"{label}需要 {minimum}～{maximum} 个字符"
        raise BrandingError("invalid_" + field, msg, 400, field)
    return trimmed


def validate_brand_name(value: str) -> str:
    return _normalize_text(value, BRAND_NAME_MIN, BRAND_NAME_MAX, "brand_name", "品牌名称")


def validate_logo_description(value: str) -> str:
    return _normalize_text(value, 0, LOGO_DESC_MAX, "logo_description", "Logo 描述")


def valid_sha256_asset_id(asset_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}(?:\.png)?", asset_id or ""))


# --------------------------------------------------------------------------- #
# Image validation / normalization
# --------------------------------------------------------------------------- #

def _pixel_count(width: int, height: int) -> int:
    return width * height


def _probe_image(data: bytes) -> Tuple[str, int, int, bool]:
    """Return (format, width, height, animated) or raise BrandingError.

    Uses Pillow to actually decode and inspect rather than trusting the
    extension or declared MIME. ``animated`` is True for a GIF/APNG/Animated
    WebP carrier; we refuse those.
    """
    if not _HAS_PIL:
        raise BrandingError("storage_error", "图片处理依赖不可用", 503)
    try:
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").upper()
        if fmt not in SUPPORTED_IMAGE_FORMATS:
            raise BrandingError(
                "invalid_image_format",
                "仅支持 PNG、JPEG 和静态 WebP 图片",
                415,
            )
        if fmt == "WEBP":
            if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                raise BrandingError("invalid_image_animated", "不支持动画图片", 400)
        if fmt == "PNG" and getattr(img, "is_animated", False):
            raise BrandingError("invalid_image_animated", "不支持动画图片", 400)
        # Force a full decode so a truncated/corrupt body raises here.
        img.load()
        width, height = img.size
        # Composite/verify via a temporary RGBA to catch bad color data.
        img.convert("RGBA")
        return fmt, width, height, False
    except BrandingError:
        raise
    except Exception as exc:
        raise BrandingError(
            "invalid_image_decode", "无法解码图片，文件可能损坏", 400
        ) from exc


def validate_image_file(data: bytes, filename: str) -> Tuple[str, int, int]:
    """Validate an uploaded Logo image and its declared filename.

    Returns ``(format, width, height)``. Raises BrandingError with a stable
    code on any violation (size, format, decode, dimensions, animation).
    """
    if not data:
        raise BrandingError("invalid_image_empty", "图片文件为空", 400)
    if len(data) > MAX_IMAGE_BYTES:
        raise BrandingError(
            "image_too_large", "图片不能超过 2 MiB", 413
        )

    ext = (os.path.splitext(filename or "")[1] or "").lower()
    if ext not in SUPPORTED_IMAGE_EXTS:
        raise BrandingError(
            "invalid_image_format",
            "仅支持 PNG、JPG、JPEG 和 WebP 文件",
            415,
        )

    fmt, width, height, _animated = _probe_image(data)

    # Extension / declared MIME vs decoded format consistency.
    ext_to_format = {
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
    }
    if ext_to_format.get(ext) != fmt:
        raise BrandingError(
            "invalid_image_format",
            "图片内容与文件类型不一致",
            415,
        )

    if width < IMG_MIN_DIM or height < IMG_MIN_DIM or width > IMG_MAX_DIM or height > IMG_MAX_DIM:
        raise BrandingError(
            "invalid_image_dimensions",
            f"图片宽高需在 {IMG_MIN_DIM}～{IMG_MAX_DIM} 像素之间",
            400,
        )
    if _pixel_count(width, height) > IMG_MAX_PIXELS:
        raise BrandingError(
            "invalid_image_pixels", "图片总像素不能超过 16,000,000", 400
        )
    return fmt, width, height


def normalize_image(data: bytes) -> bytes:
    """Normalize a validated image into a safe, metadata-stripped PNG.

    - Applies EXIF orientation (``ImageOps.exif_transpose``).
    - Converts to RGBA and re-encodes as PNG (drops EXIF / ICC / any ancillary
      metadata).
    - Scales the longest side down to ``LOGO_MAX_EXHIBIT_SIDE`` keeping aspect
      ratio (content is preserved, never stretched by the display code which
      uses ``object-fit: contain``).
    - Returns PNG bytes.

    The caller has already validated size/format/dimensions/decoding; this
    function only has to produce a safe normalized asset.
    """
    if not _HAS_PIL:
        raise BrandingError("storage_error", "图片处理依赖不可用", 503)
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img)  # type: ignore[arg-type]
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        if max(img.size) > LOGO_MAX_EXHIBIT_SIDE:
            img.thumbnail((LOGO_MAX_EXHIBIT_SIDE, LOGO_MAX_EXHIBIT_SIDE), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except BrandingError:
        raise
    except Exception as exc:
        raise BrandingError(
            "storage_error", "图片规范化失败", 500
        ) from exc


def derive_favicon(data: bytes) -> bytes:
    """Derive a 32x32 transparent PNG favicon from a normalized Logo PNG.

    Uses ``ImageOps.fit`` (cover) with center alignment so the full glyph is
    preserved on a transparent canvas rather than being letterboxed oddly.
    """
    if not _HAS_PIL:
        raise BrandingError("storage_error", "图片处理依赖不可用", 503)
    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img = ImageOps.fit(img, (FAVICON_SIZE, FAVICON_SIZE), Image.LANCZOS, centering=(0.5, 0.5))
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except BrandingError:
        raise
    except Exception as exc:
        raise BrandingError("storage_error", "favicon 生成失败", 500) from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Snapshot / storage records
# --------------------------------------------------------------------------- #

def _default_snapshot(now_ts: Optional[float] = None) -> dict:
    """The version-0 default record (no custom assets)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "brand_name": DEFAULT_BRAND_NAME,
        "logo_description": DEFAULT_BRAND_DESC,
        "logo_asset_id": None,
        "favicon_asset_id": None,
        "updated_at": (now_ts if now_ts is not None else time.time()),
        "operator": "builtin",
    }


def _is_valid_snapshot(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("schema_version") != SCHEMA_VERSION:
        return False
    if not isinstance(record.get("revision"), int) or record["revision"] < 0:
        return False
    if not isinstance(record.get("brand_name"), str) or not record["brand_name"]:
        return False
    if not isinstance(record.get("logo_description"), str):
        return False
    return True


class BrandingService:
    """Stores, validates, and atomically publishes the instance brand."""

    def __init__(self, data_root: str = None):
        self._data_root = data_root or get_data_root()
        self._root = os.path.join(self._data_root, "branding")
        self._assets_dir = os.path.join(self._root, "assets")
        self._snapshots_dir = os.path.join(self._root, "snapshots")
        self._tmp_dir = os.path.join(self._root, "tmp")
        self._main_path = os.path.join(self._root, "branding.json")
        self._revision_path = os.path.join(self._root, ".revision.json")
        self._lock_path = os.path.join(self._root, ".write.lock")
        self._mem_lock = threading.RLock()

    # ---- helpers ----
    def _ensure_dirs(self) -> None:
        for d in (self._root, self._assets_dir, self._snapshots_dir, self._tmp_dir):
            os.makedirs(d, exist_ok=True)

    def _asset_path(self, asset_id: str) -> str:
        return os.path.join(self._assets_dir, asset_id)

    def _snapshot_path(self, revision: int) -> str:
        return os.path.join(self._snapshots_dir, f"{revision}.json")

    def _asset_is_referenced(self, record: dict) -> bool:
        if not record:
            return False
        for key in ("logo_asset_id", "favicon_asset_id"):
            asset_id = record.get(key)
            if asset_id and os.path.isfile(self._asset_path(asset_id)):
                return True
        return False

    # ---- read ----
    def _read_main_compressed(self) -> dict:
        """Read the current published record lazily, repairing nothing.

        Returns a valid snapshot or ``None``. Corrupt data is preserved on disk
        (never overwritten silently) and surfaced to the caller as a nil result
        so it can fall back to the last good snapshot / defaults.
        """
        if not os.path.isfile(self._main_path):
            return None
        try:
            with open(self._main_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not _is_valid_snapshot(data):
            return None
        return data

    def _last_good_snapshot(self) -> Optional[dict]:
        """Walk retained snapshots newest-first and return the first valid one."""
        snapshots = []
        try:
            for name in os.listdir(self._snapshots_dir):
                if not name.endswith(".json"):
                    continue
                try:
                    rev = int(name.split(".")[0])
                except ValueError:
                    continue
                snapshots.append(rev)
        except OSError:
            return None
        for rev in sorted(snapshots, reverse=True):
            try:
                with open(self._snapshot_path(rev), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if _is_valid_snapshot(data):
                return data
        return None

    def get_published(self) -> dict:
        """Return the current published record, or the default when none exist.

        Prefers the main record; if it is missing/corrupt, uses the last valid
        retained snapshot; otherwise the built-in default. Never mutates storage
        during a read.
        """
        record = self._read_main_compressed()
        if record is not None:
            return record
        record = self._last_good_snapshot()
        if record is not None:
            return record
        return _default_snapshot()

    def _storage_corrupt(self) -> bool:
        if self._read_main_compressed() is not None:
            return False
        # An untouched instance has no durable metadata. A missing/bad main
        # record after a publication must not become an editable default.
        return (os.path.lexists(self._main_path) or os.path.exists(self._revision_path)
                or bool(self._snapshot_revisions()))

    def _snapshot_revisions(self) -> List[int]:
        try:
            return [int(name[:-5]) for name in os.listdir(self._snapshots_dir)
                    if name.endswith(".json") and name[:-5].isdigit()]
        except FileNotFoundError:
            return []

    def _next_revision(self, current: dict) -> int:
        """Allocate under the write lock; failed publications may leave gaps."""
        highest = current.get("revision", 0)
        try:
            with open(self._revision_path, encoding="utf-8") as f:
                reserved = json.load(f).get("revision", 0)
            if isinstance(reserved, int) and reserved >= 0:
                highest = max(highest, reserved)
        except (OSError, ValueError, AttributeError):
            pass
        if self._storage_corrupt():
            # Before the revision ledger existed, snapshots held the previous
            # version. Reserve its successor too: it may be the damaged main.
            revisions = self._snapshot_revisions()
            if revisions:
                highest = max(highest, max(revisions) + 1)
        return highest + 1

    def _preserve_corrupt_main(self) -> None:
        """Explicit recovery copies the original before publishing anything."""
        if not self._storage_corrupt() or not os.path.lexists(self._main_path):
            return
        recovery_dir = os.path.join(self._root, "corrupt")
        os.makedirs(recovery_dir, exist_ok=True)
        path = os.path.join(recovery_dir, f"branding.{uuid.uuid4().hex}.json")
        try:
            with open(self._main_path, "rb") as source, open(path, "xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        except OSError as exc:
            raise BrandingError("storage_error", "无法保留损坏的品牌配置，恢复已取消", 500) from exc

    # ---- public read projection ----
    def public_payload(self) -> dict:
        """Minimal projection safe for unauthenticated callers."""
        record = self.get_published()
        if self._asset_is_referenced(record):
            logo_url = self._asset_url(record.get("logo_asset_id"))
            favicon_url = self._asset_url(record.get("favicon_asset_id"))
        else:
            logo_url = DEFAULT_LOGO_URL
            favicon_url = DEFAULT_FAVICON_URL
        return {
            "enabled": True,
            "revision": record.get("revision", 0),
            "brand_name": record.get("brand_name", DEFAULT_BRAND_NAME),
            "logo_description": record.get("logo_description", DEFAULT_BRAND_DESC),
            "logo_url": logo_url,
            "favicon_url": favicon_url,
        }

    def _asset_url(self, asset_id: Optional[str]) -> str:
        if not asset_id:
            return DEFAULT_LOGO_URL
        # asset_id is already "<sha256>.png"; strip a stray trailing extension
        # if present so the URL never carries a doubled suffix.
        base = asset_id[:-4] if asset_id.endswith(".png") else asset_id
        return f"/api/branding/assets/{base}.png"

    # ---- management read ----
    def management_payload(self, can_manage: bool, readonly_reason: str = "", record=None) -> dict:
        # A write response must describe its own committed record, even if a
        # second writer commits before HTTP serialization finishes.
        corrupt = record is None and self._storage_corrupt()
        record = record if record is not None else self.get_published()
        allowed = bool(can_manage)
        payload = {
            "enabled": True,
            "revision": record.get("revision", 0),
            "brand_name": record.get("brand_name", DEFAULT_BRAND_NAME),
            "logo_description": record.get("logo_description", DEFAULT_BRAND_DESC),
            "can_manage": allowed and not corrupt,
            "can_reset": allowed,
            "storage_error": "branding_storage_corrupt" if corrupt else "",
            "readonly_reason": readonly_reason or ("branding_storage_corrupt" if corrupt else ""),
            "limits": {
                "brand_name_min": BRAND_NAME_MIN,
                "brand_name_max": BRAND_NAME_MAX,
                "logo_desc_max": LOGO_DESC_MAX,
                "max_image_bytes": MAX_IMAGE_BYTES,
                "image_min_dim": IMG_MIN_DIM,
                "image_max_dim": IMG_MAX_DIM,
                "max_pixels": IMG_MAX_PIXELS,
            },
            "defaults": {
                "brand_name": DEFAULT_BRAND_NAME,
                "logo_description": DEFAULT_BRAND_DESC,
                "logo_url": DEFAULT_LOGO_URL,
                "favicon_url": DEFAULT_FAVICON_URL,
            },
        }
        if self._asset_is_referenced(record):
            payload["logo_url"] = self._asset_url(record.get("logo_asset_id"))
            payload["favicon_url"] = self._asset_url(record.get("favicon_asset_id"))
        else:
            payload["logo_url"] = DEFAULT_LOGO_URL
            payload["favicon_url"] = DEFAULT_FAVICON_URL
        return payload

    # ---- asset serving ----
    def resolve_asset(self, asset_id: str) -> Tuple[str, bytes]:
        """Return (mime, bytes) for a referenced custom asset, or raise 404.

        Only assets referenced by the current published record OR by a retained
        snapshot may be served. This is
        the "public asset boundary" required by the spec.
        """
        if not valid_sha256_asset_id(asset_id):
            raise BrandingError("not_found", "资产不存在", 404)
        base = asset_id.split(".", 1)[0]
        full = os.path.join(self._assets_dir, f"{base}.png")

        referenced = False
        record = self._read_main_compressed()
        if record:
            referenced = referenced or record.get("logo_asset_id") == f"{base}.png"
            referenced = referenced or record.get("favicon_asset_id") == f"{base}.png"
        if not referenced:
            for snap in self._snapshots_in_retention():
                # Snapshots embed only revision metadata; assets live in assets/.
                if snap.get("logo_asset_id") == f"{base}.png" or snap.get("favicon_asset_id") == f"{base}.png":
                    referenced = True
                    break

        if not referenced:
            raise BrandingError("not_found", "资产不存在", 404)

        # Confine to the assets dir with a realpath check.
        real = os.path.realpath(full)
        if not real.startswith(os.path.realpath(self._assets_dir) + os.sep):
            raise BrandingError("not_found", "资产不存在", 404)
        if not os.path.isfile(real):
            raise BrandingError("not_found", "资产不存在", 404)
        try:
            with open(real, "rb") as f:
                data = f.read()
        except OSError as exc:
            raise BrandingError("storage_error", "读取资产失败", 500) from exc
        return "image/png", data

    def _snapshots_in_retention(self) -> List[dict]:
        result: List[dict] = []
        try:
            names = os.listdir(self._snapshots_dir)
        except OSError:
            return result
        revs = []
        for name in names:
            if name.endswith(".json"):
                try:
                    revs.append(int(name.split(".")[0]))
                except ValueError:
                    continue
        for rev in sorted(revs, reverse=True)[:MAX_SNAPSHOTS]:
            try:
                with open(self._snapshot_path(rev), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if _is_valid_snapshot(data):
                result.append(data)
        return result

    # ---- write (save) ----
    def save(
        self,
        expected_revision: int,
        brand_name: str,
        logo_description: str,
        logo_action: str,
        logo_file: Optional[Tuple[str, bytes]] = None,
        operator: str = "",
    ) -> dict:
        """Validate + atomically publish a single bundled brand version.

        Returns the newly published snapshot dict. Raises BrandingError with a
        stable code / HTTP status on any failure. On any pre-publish failure the
        previously published brand stays intact.
        """
        # Validate the action contract first (mutually exclusive inputs).
        if logo_action not in LOGO_ACTIONS:
            raise BrandingError("invalid_logo_action", "未知的 Logo 操作", 400)
        if not isinstance(expected_revision, int):
            raise BrandingError("missing_expected_revision", "缺少版本号", 400)

        has_file = logo_file is not None
        if logo_action == "replace" and not has_file:
            raise BrandingError("conflicting_logo_action", "替换 Logo 需要上传图片", 400)
        if logo_action in ("keep", "default") and has_file:
            raise BrandingError("conflicting_logo_action", "当前操作不能携带图片", 400)

        name = validate_brand_name(brand_name)
        desc = validate_logo_description(logo_description)

        # Acquire the commit lock; re-check the version inside the lock so a
        # concurrent writer cannot cause a lost update.
        with _FileLock(self._lock_path):
            if self._storage_corrupt():
                raise BrandingError("branding_storage_corrupt", "品牌配置损坏，请重新读取或明确恢复默认", 503)
            current = self.get_published()
            current_revision = current.get("revision", 0)
            if expected_revision != current_revision:
                raise BrandingError(
                    "version_conflict",
                    "品牌设置已被其他人修改",
                    409,
                )

            new_assets: Dict[str, bytes] = {}
            logo_asset_id = current.get("logo_asset_id")
            favicon_asset_id = current.get("favicon_asset_id")

            if logo_action == "replace":
                if not has_file:
                    raise BrandingError("conflicting_logo_action", "替换 Logo 需要上传图片", 400)
                _filename, data = logo_file
                validate_image_file(data, _filename)
                normalized = normalize_image(data)
                logo_asset_id = f"{sha256_hex(normalized)}.png"
                new_assets[logo_asset_id] = normalized
                favicon = derive_favicon(normalized)
                favicon_asset_id = f"{sha256_hex(favicon)}.png"
                new_assets[favicon_asset_id] = favicon
            elif logo_action == "default":
                logo_asset_id = None
                favicon_asset_id = None

            new_revision = self._next_revision(current)
            now_ts = time.time()
            record = {
                "schema_version": SCHEMA_VERSION,
                "revision": new_revision,
                "brand_name": name,
                "logo_description": desc,
                "logo_asset_id": logo_asset_id,
                "favicon_asset_id": favicon_asset_id,
                "updated_at": now_ts,
                "operator": operator or "console",
            }
            self._publish(record, new_assets)
        return record

    # ---- write (reset to built-in defaults) ----
    def reset(self, expected_revision: int, operator: str = "") -> dict:
        if not isinstance(expected_revision, int):
            raise BrandingError("missing_expected_revision", "缺少版本号", 400)

        with _FileLock(self._lock_path):
            current = self.get_published()
            current_revision = current.get("revision", 0)
            if expected_revision != current_revision:
                raise BrandingError("version_conflict", "品牌设置已被其他人修改", 409)

            new_revision = self._next_revision(current)
            self._preserve_corrupt_main()
            record = {
                "schema_version": SCHEMA_VERSION,
                "revision": new_revision,
                "brand_name": DEFAULT_BRAND_NAME,
                "logo_description": DEFAULT_BRAND_DESC,
                "logo_asset_id": None,
                "favicon_asset_id": None,
                "updated_at": time.time(),
                "operator": operator or "console",
            }
            self._publish(record, {})
        return record

    # ---- atomic publish ----
    def _publish(self, record: dict, new_assets: Dict[str, bytes]) -> None:
        """Publish a new version atomically.

        Ordering matters (see the design doc):
        1. Write every new asset to ``assets/`` (content-addressed, immutable).
        2. Write the pre-publish recovery snapshot (the *previous* record) into
           ``snapshots/`` as a rollback point.
        3. Write the new ``branding.json`` to ``tmp/``, fsync, then
           ``os.replace`` into place (atomic).

        Any failure before step 3 leaves the previously published record and its
        recoverable snapshot intact.
        """
        self._ensure_dirs()
        try:
            # 1) write new assets first
            for asset_id, data in new_assets.items():
                dest = self._asset_path(asset_id)
                tmp = os.path.join(self._tmp_dir, f".asset_{asset_id}.tmp")
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, dest)

            # 2) retain the *pre-publish* record as a recoverable snapshot
            prev = self._read_main_compressed()
            if prev is None:
                # First publish: snapshot the default (version 0) so there is a
                # known rollback point before any custom version exists.
                prev = _default_snapshot()
                self._write_snapshot(prev)
            else:
                self._write_snapshot(prev)
            self._trim_snapshots()

            # Reserve before committing. This counter never exposes a candidate
            # as a published snapshot, but prevents revision reuse after damage.
            self._atomic_write_json(self._revision_path, {"revision": record["revision"]})

            # 3) atomically publish the new record
            self._atomic_write_json(self._main_path, record)

            # 4) opportunistic, best-effort asset cleanup
            self._cleanup_orphan_assets()
        except BrandingError:
            raise
        except OSError as exc:
            raise BrandingError("storage_error", "品牌保存失败", 500) from exc

    def _write_snapshot(self, record: dict) -> None:
        if not record:
            return
        path = self._snapshot_path(record.get("revision", 0))
        self._atomic_write_json(path, record)

    def _trim_snapshots(self) -> None:
        try:
            names = os.listdir(self._snapshots_dir)
        except OSError:
            return
        revs = []
        for name in names:
            if name.endswith(".json"):
                try:
                    revs.append(int(name.split(".")[0]))
                except ValueError:
                    continue
        for rev in sorted(revs)[:-MAX_SNAPSHOTS]:
            try:
                os.remove(self._snapshot_path(rev))
            except OSError:
                pass

    def _atomic_write_json(self, path: str, data: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = os.path.join(self._tmp_dir, ".branding_json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _cleanup_orphan_assets(self) -> None:
        """Remove custom assets not referenced by any retained snapshot and older
        than the retention window. Best-effort and lock-free (called under lock)."""
        try:
            cutoff = time.time() - ASSET_RETENTION_DAYS * 86400
            referenced = set()
            for snap in self._snapshots_in_retention():
                for key in ("logo_asset_id", "favicon_asset_id"):
                    aid = snap.get(key)
                    if aid:
                        referenced.add(aid)
            for name in os.listdir(self._assets_dir):
                if not name.endswith(".png"):
                    continue
                if name in referenced:
                    continue
                path = os.path.join(self._assets_dir, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass

    def cleanup_runtime_tmp(self) -> None:
        """Remove dropped temp files; called opportunistically, never fatal."""
        try:
            for name in os.listdir(self._tmp_dir):
                try:
                    os.remove(os.path.join(self._tmp_dir, name))
                except OSError:
                    pass
        except OSError:
            pass


    # ---- backup / recovery integration ----
    def has_custom_brand(self) -> bool:
        """True when this instance already holds customized brand data.

        Used by ``cli/commands/backup.py`` to decide what an archive without a
        branding segment should do on restore (preserve existing vs default).
        Read-only and never creates storage.
        """
        return self.get_published().get("revision", 0) > 0

    def export_backup(self) -> Optional[dict]:
        """Return a consistent brand bundle for a backup archive.

        The bundle carries the current published snapshot and the raw bytes of
        the assets it references. Returns ``None`` when there is no customized
        brand worth backing up (the published record is still the built-in
        default). Read under the commit lock so the two agree.
        """
        # Avoid creating a brand directory on an untouched instance.
        if not os.path.isdir(self._root) or not os.path.isfile(self._main_path):
            return None
        with _FileLock(self._lock_path):
            record = self.get_published()
            if record.get("revision", 0) <= 0:
                return None
            assets: Dict[str, bytes] = {}
            for key in ("logo_asset_id", "favicon_asset_id"):
                asset_id = record.get(key)
                if asset_id:
                    path = self._asset_path(asset_id)
                    if os.path.isfile(path):
                        try:
                            with open(path, "rb") as f:
                                assets[asset_id] = f.read()
                        except OSError:
                            continue
            return {
                "schema_version": SCHEMA_VERSION,
                "source_revision": record.get("revision", 0),
                "brand_name": record.get("brand_name", DEFAULT_BRAND_NAME),
                "logo_description": record.get("logo_description", DEFAULT_BRAND_DESC),
                "logo_asset_id": record.get("logo_asset_id"),
                "favicon_asset_id": record.get("favicon_asset_id"),
                "assets": assets,
            }

    def restore_backup(self, segment: dict) -> dict:
        """Publish a validated backup brand segment into this instance.

        Allocates a fresh (incremented) revision so no stale browser draft can
        collide with the restored version, and keeps the source revision as
        provenance. Everything is validated before anything is written, so a
        corrupt / incomplete segment is rejected instead of partially restored.
        Does not require the feature to be enabled: restoring while disabled
        preserves the data so re-enabling later shows the custom brand.
        """
        if not isinstance(segment, dict):
            raise BrandingError("invalid_branding_backup", "备份中的品牌数据无效", 400)
        if segment.get("schema_version") != SCHEMA_VERSION:
            raise BrandingError(
                "branding_schema_unsupported", "备份中的品牌数据版本不受支持", 400
            )
        name = validate_brand_name(segment.get("brand_name", DEFAULT_BRAND_NAME))
        desc = validate_logo_description(segment.get("logo_description", DEFAULT_BRAND_DESC))
        source_rev = segment.get("source_revision")
        if not isinstance(source_rev, int) or source_rev < 0:
            raise BrandingError("invalid_branding_backup", "备份中的品牌数据无效", 400)

        logo_asset_id = segment.get("logo_asset_id")
        favicon_asset_id = segment.get("favicon_asset_id")
        assets = segment.get("assets") or {}
        if not isinstance(assets, dict):
            raise BrandingError("invalid_branding_backup", "备份中的品牌资产无效", 400)

        with _FileLock(self._lock_path):
            new_revision = self._next_revision(self.get_published())

            # Validate every referenced asset (presence + content address)
            # before anything is written: no partial restore.
            to_write: Dict[str, bytes] = {}
            for key in ("logo_asset_id", "favicon_asset_id"):
                asset_id = logo_asset_id if key == "logo_asset_id" else favicon_asset_id
                if not asset_id:
                    continue
                if not valid_sha256_asset_id(asset_id):
                    raise BrandingError("invalid_branding_backup", "品牌资产标识无效", 400)
                data = assets.get(asset_id)
                if data is None:
                    raise BrandingError(
                        "branding_backup_missing_asset", "备份缺少品牌资产", 400
                    )
                if sha256_hex(data) != asset_id.split(".", 1)[0]:
                    raise BrandingError(
                        "branding_backup_bad_checksum", "品牌资产摘要不匹配", 400
                    )
                to_write[asset_id] = data

            record = {
                "schema_version": SCHEMA_VERSION,
                "revision": new_revision,
                "brand_name": name,
                "logo_description": desc,
                "logo_asset_id": logo_asset_id,
                "favicon_asset_id": favicon_asset_id,
                "updated_at": time.time(),
                # Provenance: this state came from a backup, not a live edit.
                "source_backup_revision": source_rev,
                "operator": "restore",
            }
            self._preserve_corrupt_main()
            self._publish(record, to_write)
        return record


# --------------------------------------------------------------------------- #
# Convenience factory (kept simple, no caching so tests can isolate instances).
# --------------------------------------------------------------------------- #

def create_service() -> BrandingService:
    return BrandingService()
