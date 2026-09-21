# encoding:utf-8
"""入站图片 → 图像内容块：编码、上限与**明确降级**。

事故背景：模型只拿到 ``[图片: <路径>]`` 文本，于是要么谎称读过图、要么让用户
自己把图里的字贴出来。本模块是唯一编码入口，渠道只报告图片在哪里。

这里钉住两类行为：
1. 能投递时，产出可被提供方直接消费的 ``data:`` URL 图像块；
2. 不能投递时，产出**可读的降级原因**，使模型没有「不知道图没被解析」的余地。
"""

import base64
import io

import pytest
from PIL import Image


def _write_image(path, *, size=(64, 48), fmt="PNG", color=(200, 30, 30)):
    Image.new("RGB", size, color).save(path, format=fmt)
    return str(path)


def _write_noise_image(path, *, size=(2000, 1500)):
    """高熵图片：重编码后不会被压到极小，便于观测上限行为。"""
    import random

    rnd = random.Random(1234)
    img = Image.new("RGB", size)
    img.putdata([
        (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        for _ in range(size[0] * size[1])
    ])
    img.save(path, format="PNG")
    return str(path)


def _parts(attachments, **policy_kwargs):
    from agent.attachments import AttachmentPolicy, build_image_parts

    return build_image_parts(attachments, AttachmentPolicy(**policy_kwargs))


# --- 正常投递 ---------------------------------------------------------------

def test_a_real_png_becomes_a_data_url_image_part(tmp_path):
    src = _write_image(tmp_path / "shot.png")

    parts, notices = _parts([{"path": src}])

    assert notices == []
    assert len(parts) == 1
    assert parts[0].media_type == "image/png"
    url = parts[0].data_url()
    assert url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(url.split(",", 1)[1])
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n", "data URL must carry the real image bytes"


def test_a_jpeg_is_reported_as_jpeg(tmp_path):
    src = _write_image(tmp_path / "photo.jpg", fmt="JPEG")

    parts, notices = _parts([{"path": src}])

    assert notices == []
    assert parts[0].media_type == "image/jpeg"


def test_a_webp_is_accepted(tmp_path):
    src = _write_image(tmp_path / "pic.webp", fmt="WEBP")

    parts, notices = _parts([{"path": src}])

    assert notices == []
    assert parts[0].media_type == "image/webp"


def test_content_blocks_put_text_first_and_images_after(tmp_path):
    """提供方按顺序消费内容块；文本在前便于附带降级说明。"""
    src = _write_image(tmp_path / "shot.png")
    parts, _ = _parts([{"path": src}])

    from agent.attachments import to_content_blocks

    blocks = to_content_blocks("图片的内容", parts)

    assert blocks[0] == {"type": "text", "text": "图片的内容"}
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/")


def test_no_images_yields_a_plain_text_block(tmp_path):
    from agent.attachments import to_content_blocks

    assert to_content_blocks("你好", []) == [{"type": "text", "text": "你好"}]


# --- 尺寸归一 ---------------------------------------------------------------

def test_an_oversized_image_is_scaled_down_to_the_max_edge(tmp_path):
    src = _write_noise_image(tmp_path / "big.png", size=(2000, 1500))

    parts, notices = _parts([{"path": src}], max_edge=512)

    assert len(parts) == 1
    with Image.open(io.BytesIO(parts[0].data)) as img:
        assert max(img.size) <= 512, f"long edge was not normalized: {img.size}"


def test_normalization_reaches_the_byte_ceiling(tmp_path):
    """最大边归一后仍超字节上限时，必须继续降质以满足上限。"""
    src = _write_noise_image(tmp_path / "huge.png", size=(3000, 2400))

    parts, notices = _parts(
        [{"path": src}], max_edge=1024, max_bytes=60 * 1024,
    )

    assert notices == []
    assert len(parts) == 1
    assert len(parts[0].data) <= 60 * 1024, \
        f"normalized image still exceeds the ceiling: {len(parts[0].data)} bytes"


def test_a_small_image_is_not_upscaled(tmp_path):
    src = _write_image(tmp_path / "small.png", size=(32, 32))

    parts, notices = _parts([{"path": src}], max_edge=1568)

    assert notices == []
    with Image.open(io.BytesIO(parts[0].data)) as img:
        assert img.size == (32, 32), "a smaller image must be passed through as-is"


# --- 明确降级 ---------------------------------------------------------------

def test_a_missing_file_is_reported_not_silently_dropped(tmp_path):
    parts, notices = _parts([{"path": str(tmp_path / "gone.png")}])

    assert parts == []
    assert len(notices) == 1
    assert "gone.png" in notices[0], "the notice must name the offending image"


def test_an_undecodable_file_is_reported(tmp_path):
    fake = tmp_path / "not_an_image.png"
    fake.write_bytes(b"this is plainly not a png" * 20)

    parts, notices = _parts([{"path": str(fake)}])

    assert parts == []
    assert len(notices) == 1
    assert "not_an_image.png" in notices[0]


def test_an_unsupported_format_is_reported(tmp_path):
    src = tmp_path / "scan.bmp"
    Image.new("RGB", (32, 32), (0, 0, 0)).save(src, format="BMP")

    parts, notices = _parts([{"path": str(src)}])

    assert parts == []
    assert len(notices) == 1
    assert "scan.bmp" in notices[0]


def test_an_image_over_the_ceiling_that_cannot_be_shrunk_enough_is_reported(tmp_path):
    """单张上限小到无法满足时，必须明确拒绝而不是投递超限图片。"""
    src = _write_noise_image(tmp_path / "impossible.png", size=(1500, 1200))

    parts, notices = _parts([{"path": src}], max_edge=512, max_bytes=200)

    assert parts == []
    assert len(notices) == 1
    assert "impossible.png" in notices[0]


def test_the_per_turn_count_limit_is_enforced_with_a_counted_notice(tmp_path):
    paths = [_write_image(tmp_path / f"{i}.png", color=(i * 20, 0, 0)) for i in range(5)]

    parts, notices = _parts([{"path": p} for p in paths], max_count=2)

    assert len(parts) == 2, "only the limit's worth of images may be delivered"
    assert len(notices) == 1
    assert "3" in notices[0], "the notice must say how many images were omitted"


def test_degradation_notices_are_appended_to_the_turn_text(tmp_path):
    from agent.attachments import append_notices

    text = append_notices("图片的内容", ["4.png 未能解析：超过大小上限"])

    assert text.startswith("图片的内容")
    assert "4.png 未能解析：超过大小上限" in text


def test_append_notices_is_a_no_op_without_notices():
    from agent.attachments import append_notices

    assert append_notices("你好", []) == "你好"


# --- 空输入 -----------------------------------------------------------------

def test_no_attachments_yields_nothing(tmp_path):
    parts, notices = _parts([])

    assert parts == []
    assert notices == []


def test_attachment_without_a_path_is_reported(tmp_path):
    parts, notices = _parts([{"path": ""}])

    assert parts == []
    assert len(notices) == 1


# --- 从 Context 读取渠道上报的附件 ------------------------------------------

class _Ctx:
    """Minimal stand-in for bridge.context.Context's mapping surface."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get(self, key, default=None):
        return self.kwargs.get(key, default)


def test_context_attachments_are_read_in_the_canonical_shape():
    from agent.attachments import attachments_from_context

    ctx = _Ctx(attachments=[
        {"path": "/ws/tmp/a.png", "media_type": "image/png", "name": "a.png"},
    ])

    assert attachments_from_context(ctx) == [
        {"path": "/ws/tmp/a.png", "media_type": "image/png", "name": "a.png"},
    ]


def test_context_attachments_tolerate_the_web_file_path_key():
    """Web 附件用 ``file_path``；渠道不该被迫改写已有字段。"""
    from agent.attachments import attachments_from_context

    ctx = _Ctx(attachments=[{"file_path": "/ws/tmp/b.png", "file_type": "image"}])

    assert attachments_from_context(ctx) == [
        {"path": "/ws/tmp/b.png", "media_type": "image/png"},
    ]


def test_context_without_attachments_yields_nothing():
    from agent.attachments import attachments_from_context

    assert attachments_from_context(_Ctx()) == []
    assert attachments_from_context(None) == []


def test_context_attachments_skip_entries_without_a_path():
    from agent.attachments import attachments_from_context

    ctx = _Ctx(attachments=[{"file_path": ""}, {"file_type": "image"}])

    assert attachments_from_context(ctx) == []


def test_context_attachments_ignore_non_image_file_types():
    """非图片附件保持既有路径标记行为，不得被当成图像投递。"""
    from agent.attachments import attachments_from_context

    ctx = _Ctx(attachments=[
        {"file_path": "/ws/report.pdf", "file_type": "file"},
        {"file_path": "/ws/clip.mp4", "file_type": "video"},
    ])

    assert attachments_from_context(ctx) == []


def test_a_bad_container_is_ignored_rather_than_raising():
    from agent.attachments import attachments_from_context

    assert attachments_from_context(_Ctx(attachments="not-a-list")) == []


# --- 上限配置 ---------------------------------------------------------------

def _policy_from(config):
    from unittest.mock import patch

    from agent.attachments import AttachmentPolicy

    with patch("config.conf", return_value=config):
        return AttachmentPolicy.from_config()


def test_configured_limits_are_applied():
    policy = _policy_from({"image_input": {
        "max_bytes": 512 * 1024, "max_count": 2, "max_edge": 800, "jpeg_quality": 70,
    }})

    assert policy.max_bytes == 512 * 1024
    assert policy.max_count == 2
    assert policy.max_edge == 800
    assert policy.jpeg_quality == 70


def test_missing_config_falls_back_to_the_documented_defaults():
    from agent.attachments import AttachmentPolicy

    assert _policy_from({}) == AttachmentPolicy()


@pytest.mark.parametrize("bad", [
    {"max_count": 0},
    {"max_count": -3},
    {"max_count": "many"},
    {"max_bytes": 10},          # below the 1 KiB floor
    {"max_edge": 1},            # below the minimum legible edge
    {"max_edge": None},
])
def test_unusable_values_fall_back_instead_of_disabling_image_input(bad):
    """配置写错不得让图片输入整体失效，更不得抛错打断该轮对话。"""
    from agent.attachments import AttachmentPolicy

    policy = _policy_from({"image_input": bad})

    assert policy == AttachmentPolicy(), f"{bad} should have fallen back"
    assert policy.max_count >= 1
    assert policy.max_edge >= 32


def test_a_non_dict_section_falls_back():
    from agent.attachments import AttachmentPolicy

    assert _policy_from({"image_input": "nonsense"}) == AttachmentPolicy()


if __name__ == "__main__":
    import unittest

    unittest.main()
