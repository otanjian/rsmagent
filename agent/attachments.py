# encoding:utf-8
"""Turn inbound image attachments into model-consumable content parts.

The single encoding entry point for every channel: a channel only reports where
an image lives, and this module decides whether it can be delivered, normalizes
it to a bounded size, and — when it cannot be delivered — produces a readable
reason instead of dropping it silently.

Why the reasons matter: before this existed, images reached the model as the
literal string ``[图片: <path>]``. A model that never received the picture had no
way to know that, so it either pretended to have read it or asked the user to
retype the contents. Every degradation below is therefore reported into the
turn's text, so the model can always answer honestly.
"""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from common.log import logger

# Image formats we are willing to forward. Anything else is reported.
SUPPORTED_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")

_FORMAT_TO_MEDIA_TYPE = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

# Quality ladder used when a normalized image still exceeds its byte ceiling.
_JPEG_QUALITY_LADDER = (85, 75, 65, 55, 45, 35, 25)
# Progressive shrink factor applied when the quality ladder bottoms out.
_SHRINK_FACTOR = 0.75
# Never scale below this long edge: further shrinking cannot meet the ceiling
# and would only destroy the image before we give up.
_MIN_EDGE = 32


@dataclass(frozen=True)
class AttachmentPolicy:
    """Bounds applied to every inbound image."""

    max_bytes: int = 4 * 1024 * 1024
    max_count: int = 4
    max_edge: int = 1568
    jpeg_quality: int = 85

    @classmethod
    def from_config(cls) -> "AttachmentPolicy":
        """Read overrides from ``conf()["image_input"]``, falling back to defaults.

        Bad values fall back rather than raising: a typo in configuration must
        not take image input (or the whole turn) down.
        """
        try:
            from config import conf

            raw = conf().get("image_input") or {}
            if not isinstance(raw, dict):
                return cls()
        except Exception:
            return cls()

        def _positive_int(key: str, fallback: int, low: int = 1) -> int:
            try:
                value = int(raw.get(key, fallback))
            except (TypeError, ValueError):
                return fallback
            return value if value >= low else fallback

        return cls(
            max_bytes=_positive_int("max_bytes", cls.max_bytes, low=1024),
            max_count=_positive_int("max_count", cls.max_count),
            max_edge=_positive_int("max_edge", cls.max_edge, low=_MIN_EDGE),
            jpeg_quality=_positive_int("jpeg_quality", cls.jpeg_quality, low=1),
        )


@dataclass(frozen=True)
class ImagePart:
    """One image ready to be sent to a provider."""

    media_type: str
    data: bytes
    name: str = ""

    def data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"


def build_image_parts(
    attachments: Sequence[Dict[str, Any]],
    policy: AttachmentPolicy | None = None,
) -> Tuple[List[ImagePart], List[str]]:
    """Encode inbound image attachments into deliverable parts.

    *attachments* is the minimal channel-facing contract: mappings carrying
    ``path`` (required), plus an optional ``media_type`` and ``name``.

    Returns ``(parts, notices)``. Every image that could not be delivered
    contributes exactly one entry to *notices*; those strings are meant to be
    appended to the turn's text (see :func:`append_notices`).
    """
    policy = policy or AttachmentPolicy.from_config()
    parts: List[ImagePart] = []
    notices: List[str] = []
    omitted_for_count = 0

    for attachment in attachments or ():
        path = str((attachment or {}).get("path") or "").strip()
        name = str((attachment or {}).get("name") or "") or os.path.basename(path)
        if not path:
            notices.append("有一张图片未能解析：附件缺少本地路径")
            continue
        if len(parts) >= policy.max_count:
            omitted_for_count += 1
            continue

        try:
            part = _encode_one(path, name, policy)
        except Exception as error:  # defensive: never fail the turn over an image
            logger.warning(f"[Attachments] could not prepare image '{path}': {error}")
            notices.append(f"{name} 未能解析：{error}")
            continue

        if part is None:
            notices.append(f"{name} 未能解析：超过大小上限（{policy.max_bytes} 字节）")
            continue
        parts.append(part)

    if omitted_for_count:
        notices.append(
            f"另有 {omitted_for_count} 张图片未能解析：超过单轮图片数量上限"
            f"（{policy.max_count} 张）"
        )

    return parts, notices


def to_content_blocks(text: str, parts: Sequence[ImagePart]) -> List[Dict[str, Any]]:
    """Build the provider-facing content blocks: text first, then images."""
    blocks: List[Dict[str, Any]] = [{"type": "text", "text": text or ""}]
    for part in parts or ():
        blocks.append({
            "type": "image_url",
            "image_url": {"url": part.data_url()},
        })
    return blocks


def append_notices(text: str, notices: Sequence[str]) -> str:
    """Append degradation notices to the turn text.

    The model must be able to tell that an image was not delivered; leaving the
    text untouched would let it claim otherwise.
    """
    if not notices:
        return text or ""
    return "\n".join([text or ""] + list(notices)).strip()


# Inbound image entries accepted from a channel. Web attachments use
# ``file_path``/``file_type``; the canonical shape is ``path``/``media_type``.
_IMAGE_FILE_TYPES = ("image", "IMAGE", "图片")


def attachments_from_context(context: Any) -> List[Dict[str, Any]]:
    """Read the structured image attachments a channel reported on its Context.

    Returns the canonical ``{"path", "media_type", "name"}`` shape. Non-image
    entries (files, videos, workspace references) are deliberately dropped:
    they keep their existing textual path-marker behaviour, and turning them
    into image parts would send a document to a vision endpoint.

    Anything malformed is ignored rather than raised — an attachment problem
    must never take down the turn.
    """
    if context is None:
        return []
    try:
        raw = context.get("attachments")
    except Exception:
        return []
    if not isinstance(raw, (list, tuple)):
        return []

    collected: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        file_type = str(entry.get("file_type") or entry.get("type") or "image")
        if file_type not in _IMAGE_FILE_TYPES:
            continue
        path = str(entry.get("path") or entry.get("file_path") or "").strip()
        if not path:
            continue
        media_type = str(entry.get("media_type") or "").strip()
        if not media_type:
            media_type = _media_type_for_path(path)
        item: Dict[str, Any] = {"path": path, "media_type": media_type}
        name = str(entry.get("name") or entry.get("file_name") or "").strip()
        if name:
            item["name"] = name
        collected.append(item)
    return collected


_MEDIA_TYPE_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _media_type_for_path(path: str) -> str:
    return _MEDIA_TYPE_BY_SUFFIX.get(os.path.splitext(path)[1].lower(), "image/png")


def model_accepts_images(model: Any) -> bool:
    """Whether the model answering this turn can receive image content parts.

    The bot's own declaration wins. Bots that do not declare anything fall back
    to the same model-name families the vision tool already relies on, so a
    provider that never bothered to declare must not silently lose image input.

    Anything unresolvable is treated as "cannot": sending an image part to a
    model that rejects it fails the whole turn, which is worse than the model
    reading a path and telling the user the picture did not arrive.
    """
    bot = getattr(model, "bot", None)
    declared = getattr(bot, "supports_vision", None)
    if isinstance(declared, bool):
        return declared
    if declared is not None:
        # A property that raises means we cannot know; stay conservative.
        return False

    name = getattr(model, "model", None)
    if not isinstance(name, str) or not name.strip():
        return False
    return _model_name_is_multimodal(name.strip())


def _model_name_is_multimodal(model_name: str) -> bool:
    """Model-name fallback, reusing the vision tool's single family list."""
    try:
        from agent.tools.vision.vision import _OPENAI_MODEL_PREFIXES, Vision
    except Exception:  # pragma: no cover - import guard
        return False

    lowered = model_name.lower()
    if any(lowered.startswith(prefix) for prefix in _OPENAI_MODEL_PREFIXES):
        return True
    return Vision._infer_provider_from_model(model_name) is not None


def build_turn_blocks(
    text: str,
    attachments: Sequence[Dict[str, Any]] | None,
    *,
    model: Any,
    policy: AttachmentPolicy | None = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Content blocks for one user turn.

    Images are attached only when the turn's own model can accept them. Every
    image that is not delivered contributes a readable notice, and those
    notices are appended to the text so the model can never be in the dark
    about a picture it did not receive.
    """
    attachments = list(attachments or ())
    if not attachments:
        return to_content_blocks(text or "", []), []

    if not model_accepts_images(model):
        notices = [
            _unsupported_notice(attachments),
        ]
        return to_content_blocks(append_notices(text, notices), []), notices

    parts, notices = build_image_parts(attachments, policy)
    return to_content_blocks(append_notices(text, notices), parts), notices


def _unsupported_notice(attachments: Sequence[Dict[str, Any]]) -> str:
    names = [
        str((a or {}).get("name") or os.path.basename(str((a or {}).get("path") or "")))
        for a in attachments
    ]
    names = [n for n in names if n]
    subject = "、".join(names) if names else "图片"
    count = f"（共 {len(names)} 张）" if len(names) > 1 else ""
    return f"{subject}{count} 未能解析：当前模型不支持图像输入，图片内容未送达"



def _encode_one(path: str, name: str, policy: AttachmentPolicy) -> ImagePart | None:
    """Encode a single image, or return ``None`` when the ceiling is unreachable.

    Raises for everything the caller should report verbatim (missing file,
    undecodable bytes, unsupported format).
    """
    from PIL import Image

    if not os.path.isfile(path):
        raise ValueError("图片文件不存在或不可读")

    try:
        with Image.open(path) as probe:
            source_format = (probe.format or "").upper()
            probe.load()
            image = probe.convert("RGBA") if probe.mode in ("P", "LA") else probe.copy()
    except Exception as error:
        raise ValueError(f"无法解码为图片（{type(error).__name__}）") from error

    if source_format not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的图片格式（{source_format or '未知'}）")

    # GIF (and any palette source) is flattened to RGB: providers accept static
    # images, and the first frame is what a single image content part means.
    if image.mode not in ("RGB", "L"):
        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1])
            image = background
        else:
            image = image.convert("RGB")

    if max(image.size) > policy.max_edge:
        image = _resize_to_edge(image, policy.max_edge)

    media_type = _FORMAT_TO_MEDIA_TYPE[source_format]
    data = _encode_native(image, source_format, policy.jpeg_quality)

    if len(data) > policy.max_bytes:
        shrunk = _shrink_to_ceiling(image, policy)
        if shrunk is None:
            return None
        data, media_type = shrunk

    return ImagePart(media_type=media_type, data=data, name=name)


def _resize_to_edge(image, max_edge: int):
    from PIL import Image

    width, height = image.size
    scale = max_edge / float(max(width, height))
    target = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(target, Image.LANCZOS)


def _encode_native(image, source_format: str, quality: int) -> bytes:
    """Encode without changing the image's own container where possible."""
    from PIL import Image

    buffer = io.BytesIO()
    if source_format == "JPEG":
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
    elif source_format == "WEBP":
        image.save(buffer, format="WEBP", quality=quality)
    elif source_format == "GIF":
        # A GIF frame re-encoded as GIF stays tiny but palette-bound; PNG keeps
        # the content lossless and is universally accepted.
        image.save(buffer, format="PNG", optimize=True)
    else:
        image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _encode_jpeg(image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _shrink_to_ceiling(image, policy: AttachmentPolicy):
    """Walk quality and size down until the ceiling is met, or give up.

    Returns ``(data, media_type)`` or ``None`` when the ceiling is unreachable
    without destroying the image — in which case the caller reports instead of
    delivering an oversized payload.
    """
    from PIL import Image

    current = image.convert("RGB") if image.mode != "RGB" else image
    # JPEG is the only format with a real quality knob here, so the ladder runs
    # on a JPEG copy; PNG output would keep exceeding the ceiling unchanged.
    while True:
        for quality in _JPEG_QUALITY_LADDER:
            data = _encode_jpeg(current, quality)
            if len(data) <= policy.max_bytes:
                return data, "image/jpeg"

        width, height = current.size
        if max(width, height) <= _MIN_EDGE:
            return None
        target = (
            max(_MIN_EDGE, int(width * _SHRINK_FACTOR)),
            max(_MIN_EDGE, int(height * _SHRINK_FACTOR)),
        )
        if target == (width, height):
            return None
        current = current.resize(target, Image.LANCZOS)
