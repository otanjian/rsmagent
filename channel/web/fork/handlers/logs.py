"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from bridge.context import *
from common.log import logger
from common.utils import tail_lines
import json
import os
import time
import web


def _redact_log_line(line: str) -> str:
    """Mask credential-looking values in one log line (task 4.11).

    The console log view is a debugging aid, not an excuse to hand out
    credentials: the obvious ``key: value`` / ``key=value`` forms are replaced
    with ``key=***``. Deliberately conservative — it masks the value of a small
    set of well-known secret names rather than trying to detect entropy, so a
    normal log line is left byte-identical.
    """
    from channel.web.web_channel import _LOG_SECRET_RE
    return _LOG_SECRET_RE.sub(lambda m: "%s%s***" % (m.group(1), m.group(2)), line)


def _redact_log_text(text: str) -> str:
    from channel.web.web_channel import _redact_log_line
    return "\n".join(_redact_log_line(line) for line in text.split("\n"))


class LogsHandler:
    def GET(self):
        # ``run.log`` is the process-global log and mixes every tenant's
        # activity (plus whatever secrets a handler happened to log), so it is
        # platform control-plane data: in database mode only a platform admin
        # may read it. Legacy mode keeps the shared console password.
        from channel.web.web_channel import _redact_log_line
        from channel.web.web_channel import _require_platform_console
        from channel.web.web_channel import get_data_root
        _require_platform_console()
        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')

        log_path = os.path.join(get_data_root(), "run.log")

        def generate():
            if not os.path.isfile(log_path):
                yield b"data: {\"type\": \"error\", \"message\": \"run.log not found\"}\n\n"
                return

            # Read last 200 lines for initial display
            try:
                chunk = _redact_log_text(''.join(tail_lines(log_path, 200)))
                payload = json.dumps({"type": "init", "content": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode('utf-8')
            except Exception as e:
                yield f"data: {{\"type\": \"error\", \"message\": \"{e}\"}}\n\n".encode('utf-8')
                return

            # Tail new lines
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(0, 2)  # seek to end
                    deadline = time.time() + 600  # 10 min max
                    while time.time() < deadline:
                        line = f.readline()
                        if line:
                            payload = json.dumps({"type": "line",
                                                  "content": _redact_log_line(line)},
                                                 ensure_ascii=False)
                            yield f"data: {payload}\n\n".encode('utf-8')
                        else:
                            yield b": keepalive\n\n"
                            time.sleep(1)
            except GeneratorExit:
                return
            except Exception:
                return

        return generate()


class LogsDownloadHandler:
    """Serve the full run.log as a file download for offline troubleshooting.

    The /api/logs stream only replays the last 200 lines; this returns the whole
    file so users can attach it to a bug report. Like the stream, ``run.log`` is
    process-global data (every tenant's activity), so this is a platform-admin
    surface in database mode; the response is redacted line by line.
    """

    def GET(self):
        from channel.web.web_channel import _require_platform_console
        from channel.web.web_channel import get_data_root
        _require_platform_console()
        log_path = os.path.join(get_data_root(), "run.log")
        if not os.path.isfile(log_path):
            raise web.notfound()

        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                text = _redact_log_text(f.read())
            data = text.encode('utf-8')
        except Exception as e:
            logger.error(f"[WebChannel] Log download error: {e}")
            raise web.internalerror()

        # Timestamped name so multiple downloads don't overwrite each other.
        fname = f"rongda-ai-{time.strftime('%Y%m%d-%H%M%S')}.log"
        web.header('Content-Type', 'text/plain; charset=utf-8')
        web.header('Content-Disposition', f'attachment; filename="{fname}"')
        web.header('Content-Length', str(len(data)))
        web.header('Cache-Control', 'no-store')
        return data


