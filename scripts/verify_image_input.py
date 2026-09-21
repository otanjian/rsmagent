#!/usr/bin/env python
# encoding:utf-8
"""Live evidence: an inbound image reaches the model and the model reads it.

This drives the *production* path end to end, not a stand-in:

1. ``agent.attachments.attachments_from_context`` reads a channel-shaped
   attachment record,
2. ``agent.attachments.build_turn_blocks`` turns it into provider content
   blocks, gated on the answering model's real ``supports_vision``,
3. ``OpenAICompatibleBot.call_with_tools`` performs the real conversion and the
   real HTTP request.

The image carries ground-truth text, so a correct answer proves the model saw
the pixels rather than the ``[图片: <path>]`` marker next to them.

Usage:
    .venv/bin/python scripts/verify_image_input.py
    .venv/bin/python scripts/verify_image_input.py --image /path/to/photo.png
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GROUND_TRUTH = "RONGDA-42"


def _ground_truth_image(path):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (520, 200), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Default bitmap font keeps this dependency-free; scale it up so the text is
    # legible to the model without shipping a font file.
    draw.text((30, 60), GROUND_TRUTH, fill=(0, 0, 0))
    draw.rectangle([420, 40, 490, 150], fill=(30, 90, 200))
    img.save(path, format="PNG")
    return path


class _Ctx:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get(self, key, default=None):
        return self.kwargs.get(key, default)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="")
    args = parser.parse_args()

    from config import load_config, conf

    load_config()

    model_name = conf().get("model")
    if not model_name:
        print("FAIL: no model configured")
        return 1

    if args.image:
        image_path = args.image
        expectation = None
    else:
        image_path = _ground_truth_image("/tmp/verify_image_input.png")
        expectation = GROUND_TRUTH

    from agent.attachments import attachments_from_context, build_turn_blocks

    attachments = attachments_from_context(
        _Ctx(attachments=[{"file_path": image_path, "file_type": "image"}])
    )
    print(f"model            : {model_name}")
    print(f"image            : {image_path} ({os.path.getsize(image_path)} bytes)")
    print(f"attachments read : {attachments}")

    from models.deepseek.deepseek_bot import DeepSeekBot
    from types import SimpleNamespace

    bot = DeepSeekBot()
    print(f"supports_vision  : {bot.supports_vision}")

    model = SimpleNamespace(model=model_name, bot=bot)
    blocks, notices = build_turn_blocks(
        "请看这张图片，并用一句话说明图片里的文字内容。", attachments, model=model,
    )

    kinds = [b.get("type") for b in blocks]
    print(f"content blocks   : {kinds}")
    print(f"notices          : {notices}")

    if "image_url" not in kinds:
        print("FAIL: the image never became a content part")
        return 1

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": blocks},
    ]

    response = bot.call_with_tools(messages, tools=None, stream=False)

    # DeepSeek's sync handler is a generator yielding Claude-format response
    # dicts (content blocks + usage), the same shape the agent loop consumes.
    chunks = list(response) if hasattr(response, "__next__") else [response]

    answer_parts, thinking_parts, usage, errors = [], [], {}, []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            errors.append(f"{chunk.get('status_code')}: {chunk.get('message')}")
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for block in chunk.get("content") or []:
            if block.get("type") == "text":
                answer_parts.append(block.get("text") or "")
            elif block.get("type") == "thinking":
                thinking_parts.append(block.get("thinking") or "")

    answer = "".join(answer_parts).strip()
    thinking = "".join(thinking_parts).strip()

    print(f"usage            : {usage}")
    print(f"errors           : {errors}")
    print(f"thinking         : {thinking[:200]}")
    print(f"answer           : {answer[:400]}")

    if errors:
        print("FAIL: provider rejected the request with an image content part")
        return 1
    if not answer:
        print("FAIL: the model returned nothing for an image turn")
        return 1

    if expectation and expectation not in answer:
        print(f"FAIL: model did not report the ground truth {expectation!r}")
        return 1

    print("PASS: the model read the image itself")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
