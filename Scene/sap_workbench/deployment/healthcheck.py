"""Container-only component readiness, never an SAP or model health probe."""
import json
import os
import sys
from pathlib import Path

from entrypoint import RUNTIME, chrome_ready, novnc_ready, rfb_ready, websocket_ready


def healthy():
    state = json.loads((RUNTIME / 'processes.json').read_text())
    if set(state) != {'xvfb', 'chrome', 'x11vnc', 'websockify'}:
        return False
    for pid in state.values():
        if type(pid) is not int or pid < 1:
            return False
        os.kill(pid, 0)
    version = json.loads(Path(__file__).with_name('browser-versions.json').read_text())['chrome']['version']
    return chrome_ready(version) and rfb_ready() and novnc_ready() and websocket_ready()


if __name__ == '__main__':
    try:
        sys.exit(0 if healthy() else 1)
    except (OSError, ValueError):
        sys.exit(1)
