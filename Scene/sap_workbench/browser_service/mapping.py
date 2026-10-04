"""Pure translation of pane input into Chrome DevTools Protocol commands.

Every function here is side-effect free and takes plain dicts, so the wire
protocol can be unit-tested without a browser. The gateway feeds decoded pane
messages in and sends the returned commands to Chrome over CDP.

Coordinates: the pane draws a JPEG of the viewport at some CSS size, so a click
must be scaled from canvas pixels back into viewport pixels before it is
dispatched; otherwise a resized pane would click the wrong control.
"""
from __future__ import annotations

#: CDP modifier bitmask (Alt=1, Ctrl=2, Meta=4, Shift=8).
_MODIFIERS = {"alt": 1, "ctrl": 2, "meta": 4, "shift": 8}

#: DOM ``KeyboardEvent.code`` -> (Windows virtual key code, ``key`` value).
#: Printable characters do not rely on this table; they go through
#: :func:`_insert_text`, which is what actually lands CJK and shifted letters in
#: a SAP HTML field. The table covers the keys that must fire a real keydown.
_KEY_CODES: dict[str, tuple[int, str]] = {
    "Enter": (13, "Enter"), "NumpadEnter": (13, "Enter"),
    "Tab": (9, "Tab"), "Backspace": (8, "Backspace"), "Delete": (46, "Delete"),
    "Escape": (27, "Escape"), "Space": (32, " "),
    "Home": (36, "Home"), "End": (35, "End"),
    "PageUp": (33, "PageUp"), "PageDown": (34, "PageDown"),
    "Insert": (45, "Insert"),
    "ArrowLeft": (37, "ArrowLeft"), "ArrowUp": (38, "ArrowUp"),
    "ArrowRight": (39, "ArrowRight"), "ArrowDown": (40, "ArrowDown"),
}
for _i in range(1, 13):
    _KEY_CODES[f"F{_i}"] = (111 + _i, f"F{_i}")
for _i in range(26):
    _KEY_CODES[f"Key{chr(65 + _i)}"] = (65 + _i, chr(97 + _i))
for _i in range(10):
    _KEY_CODES[f"Digit{_i}"] = (48 + _i, str(_i))


def viewport_point(canvas: dict, viewport: dict, x, y) -> tuple[int, int]:
    """Scale a canvas pixel to a viewport pixel, clamped into the viewport.

    A degenerate canvas (not laid out yet) reports ``(0, 0)`` rather than
    dividing by zero or clicking an arbitrary spot.
    """
    cw, ch = _number(canvas.get("w")), _number(canvas.get("h"))
    vw, vh = _number(viewport.get("width")), _number(viewport.get("height"))
    if cw <= 0 or ch <= 0 or vw <= 0 or vh <= 0:
        return (0, 0)
    px = round(_number(x) * vw / cw)
    py = round(_number(y) * vh / ch)
    return (min(max(px, 0), int(vw)), min(max(py, 0), int(vh)))


def resize_viewport(message) -> dict | None:
    """Only bounded CSS pixel dimensions can change the page layout."""
    if not isinstance(message, dict) or message.get('t') != 'resize':
        return None
    width, height = message.get('width'), message.get('height')
    if any(type(value) is not int or not 1 <= value <= 4096 for value in (width, height)):
        return None
    return {'width': width, 'height': height}


def encode_input(message, viewport) -> list[dict]:
    """Decode one pane message into an ordered list of CDP commands."""
    if not isinstance(message, dict):
        return []
    kind = message.get("t")
    if kind == "mouse":
        return _mouse(message, viewport)
    if kind == "text":
        return _insert_text(message.get("text"))
    if kind == "key":
        return _key(message)
    return []


def _mouse(message, viewport) -> list[dict]:
    event = message.get("event")
    x, y = viewport_point(message.get("canvas") or {}, viewport,
                          message.get("x", 0), message.get("y", 0))
    if event == "wheel":
        return [{"method": "Input.dispatchMouseEvent", "params": {
            "type": "mouseWheel", "x": x, "y": y,
            "deltaX": _number(message.get("deltaX")),
            "deltaY": _number(message.get("deltaY"))}}]
    if event == "move":
        return [{"method": "Input.dispatchMouseEvent", "params": {
            "type": "mouseMoved", "x": x, "y": y, "button": "none"}}]
    if event in ("down", "up"):
        return [{"method": "Input.dispatchMouseEvent", "params": {
            "type": "mousePressed" if event == "down" else "mouseReleased",
            "x": x, "y": y, "button": message.get("button") or "left", "clickCount": 1}}]
    return []


def _key(message) -> list[dict]:
    text = message.get("text")
    shortcut = any(message.get(flag) for flag in ("ctrl", "alt", "meta"))
    # Without a shortcut, the text a key produces is what SAP should receive;
    # dispatching only keydown would drop CJK and shifted characters.
    if isinstance(text, str) and len(text) == 1 and not shortcut:
        return _insert_text(text)
    code = message.get("code")
    entry = _KEY_CODES.get(code) if isinstance(code, str) else None
    if entry is None:
        # An unmapped key is ignored rather than guessed at: a wrong keypress
        # in SAP can move a document.
        return []
    vk, key = entry
    base = {"windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "key": key,
            "code": code, "modifiers": _modifier_mask(message)}
    return [{"method": "Input.dispatchKeyEvent", "params": {**base, "type": "rawKeyDown"}},
            {"method": "Input.dispatchKeyEvent", "params": {**base, "type": "keyUp"}}]


def _insert_text(text) -> list[dict]:
    if not isinstance(text, str) or not text:
        return []
    return [{"method": "Input.insertText", "params": {"text": text}}]


def _modifier_mask(message) -> int:
    return sum(bit for flag, bit in _MODIFIERS.items() if message.get(flag))


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
