#!/usr/bin/env python
# encoding:utf-8
"""Live evidence through the platform's own agent stack.

Where ``verify_image_input.py`` proves the provider accepts the blocks and reads
the picture, this goes one layer up: it builds the agent the way the platform
does (``AgentInitializer``), runs a real turn with an inbound image, and prints
what the model answered.

What this covers:
  * the agent bootstrap and its real prompt/tool setup,
  * ``run_stream(attachments=...)`` block construction,
  * the provider request and the model's answer.

What it does NOT cover (needs a logged-in console or a real IM client):
  * the HTTP/channel ingress that delivers the image in the first place.
  * the WeCom round trip.

Usage:
    .venv/bin/python scripts/verify_platform_image_turn.py
    .venv/bin/python scripts/verify_platform_image_turn.py --agent sap --image p.png
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GROUND_TRUTH = "RONGDA-42"


def _ground_truth_image(path):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (520, 200), (255, 255, 255))
    ImageDraw.Draw(img).text((30, 60), GROUND_TRUTH, fill=(0, 0, 0))
    img.save(path, format="PNG")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="tax-health-check-test15",
                        help="agent id to run the turn against")
    parser.add_argument("--image", default="")
    args = parser.parse_args()

    from config import load_config

    load_config()

    image_path = args.image or _ground_truth_image("/tmp/verify_platform_image.png")
    expectation = None if args.image else GROUND_TRUTH

    from agent.attachments import attachments_from_context

    class _Ctx:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get(self, key, default=None):
            return self.kwargs.get(key, default)

    # Exactly the record a channel now reports for an inbound image.
    attachments = attachments_from_context(
        _Ctx(attachments=[{"file_path": image_path, "file_type": "image"}])
    )

    print(f"agent            : {args.agent}")
    print(f"image            : {image_path} ({os.path.getsize(image_path)} bytes)")
    print(f"attachments      : {attachments}")

    # The production initializer instance the running platform uses, so this
    # exercises the same bootstrap rather than a hand-built stand-in.
    from bridge.bridge import Bridge

    initializer = Bridge().get_agent_bridge().initializer

    agent = initializer.initialize_agent(
        session_id="verify-image-turn", agent_id=args.agent,
    )
    print(f"model            : {getattr(agent.model, 'model', '?')}")
    print(f"tools loaded     : {len(getattr(agent, 'tools', []) or [])}")

    question = "请看这张图片，只回答图片里的文字内容。"
    answer = agent.run_stream(question, attachments=attachments, clear_history=True)

    print(f"answer           : {str(answer).strip()[:400]}")

    if not str(answer).strip():
        print("FAIL: the platform turn returned nothing")
        return 1
    if expectation and expectation not in str(answer):
        print(f"FAIL: the model did not report the ground truth {expectation!r}")
        return 1

    print("PASS: the platform's own agent stack read the image")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
