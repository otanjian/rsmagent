#!/usr/bin/env python3
"""Console inspection over CDP, with deadlines and failure records.

Change ``harden-windows-desktop-reliability``, task 5.3. The 2026-10-13
inspection lost two screenshots to ``Page.captureScreenshot`` never answering
(>60s, report item 5) and the ad-hoc script had to be killed by hand: it had no
deadline, so one unresponsive page could cost the whole run and left no record
of which page did it. This is the repeatable version:

* every CDP operation has a deadline (``--deadline``, default 20s);
* a page that hangs is recorded and skipped -- the remaining pages still run;
* each page gets its own session, so a wedged connection cannot carry over;
* **the report separates what was observed from what the inspection could not
  determine**, and it never claims a fix. A timeout is an inspection limitation,
  not a product defect; a page whose rendered text misses an expectation is a
  finding that still needs confirmation.

Usage::

    python scripts/inspect-console-cdp.py --endpoint http://127.0.0.1:9222 \
        --out .cow-inspect --deadline 20 --pages / --pages /admin

Requires a running Chrome/Chromium with remote debugging (the desktop app's own
inspector, or ``msedge --remote-debugging-port=9222``), and ``aiohttp`` from
``requirements.txt``. Exit code: 0 when at least one page was inspected, 2 when
nothing could be (that is a broken inspection, not a broken product).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

#: Registered console paths, the set the 2026-10-13 inspection walked.
DEFAULT_PAGES = [
    "/", "/history", "/agents", "/settings", "/scheduler", "/knowledge",
    "/memory", "/admin",
]

#: How long to let a page settle after navigation before reading it. Bounded on
#: purpose: waiting for "network idle" is exactly the unbounded wait this script
#: exists to avoid.
DEFAULT_SETTLE_SECONDS = 1.5

OBSERVED = "observed"
INSPECTION_LIMITATION = "inspection_limitation"


class InspectionError(RuntimeError):
    """The inspection itself could not proceed (not a product verdict)."""


# --- the session ------------------------------------------------------------

class CdpSession:
    """One CDP websocket, with a deadline on every call."""

    def __init__(self, ws: Any):
        self._ws = ws
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None

    @classmethod
    async def open(cls, endpoint: str, *, timeout: float = 10.0) -> "CdpSession":
        import aiohttp  # imported here so --help works without the dependency

        url = page_ws_url(endpoint, timeout=timeout)
        session = aiohttp.ClientSession()
        ws = await asyncio.wait_for(session.ws_connect(url, max_msg_size=0), timeout)
        instance = cls(ws)
        instance._reader = asyncio.ensure_future(instance._read_loop())
        return instance

    async def _read_loop(self) -> None:
        try:
            async for message in self._ws:
                if message.type.name != "TEXT":
                    continue
                payload = json.loads(message.data)
                message_id = payload.get("id")
                if message_id is None:
                    continue
                future = self._pending.pop(message_id, None)
                if future is not None and not future.done():
                    future.set_result(payload)
        except Exception:  # noqa: BLE001 - a dead socket is reported per call
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.cancel()
            self._pending.clear()

    async def call(self, method: str, params: Optional[dict] = None,
                   timeout: float = 20.0) -> Any:
        self._next_id += 1
        message_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await asyncio.wait_for(
                self._ws.send_json({"id": message_id, "method": method,
                                    "params": params or {}}),
                timeout)
            answer = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(message_id, None)
        if "error" in answer:
            raise InspectionError(f"{method}: {answer['error'].get('message')}")
        return answer.get("result")

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is not an error
            pass


def page_ws_url(endpoint: str, *, timeout: float = 10.0) -> str:
    """The debugger URL of a page target, creating one if Chrome has none."""
    with urllib.request.urlopen(f"{endpoint.rstrip('/')}/json", timeout=timeout) as response:
        targets = json.load(response)
    for target in targets:
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)["webSocketDebuggerUrl"]


# --- the report -------------------------------------------------------------

@dataclass
class PageRecord:
    """What one page produced, and what it is allowed to claim."""

    page: str
    status: str                       # ok | timeout | error
    verdict: str                      # observed | inspection_limitation
    elapsed_ms: int
    detail: str = ""
    text_chars: int = 0
    screenshot: str = ""
    expectation: str = ""             # missing | met | "" (not asked)
    needs_confirmation: bool = False

    @property
    def is_limitation(self) -> bool:
        return self.verdict == INSPECTION_LIMITATION


@dataclass
class Report:
    endpoint: str = ""
    deadline_seconds: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    pages: List[PageRecord] = field(default_factory=list)

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {"pages": len(self.pages), "observed": 0,
                                  "inspection_limitation": 0, "screenshots": 0}
        for record in self.pages:
            if record.verdict == OBSERVED:
                counts["observed"] += 1
            if record.is_limitation:
                counts["inspection_limitation"] += 1
            if record.screenshot:
                counts["screenshots"] += 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "deadline_seconds": self.deadline_seconds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary(),
            # Stated in the artifact itself, because the temptation to read a
            # failure record as a product verdict is exactly what task 5.3 is
            # about: this file records observations, never fixes.
            "notes": ("Observations only. 'observed' entries are what the page "
                      "rendered; a missing expectation needs confirmation before "
                      "anyone calls it a product defect, and "
                      "'inspection_limitation' entries say nothing about the "
                      "product at all."),
            "pages": [asdict(record) for record in self.pages],
        }


SessionFactory = Callable[[], Awaitable[CdpSession]]


# --- the run ----------------------------------------------------------------

async def inspect(
    *,
    open_session: SessionFactory,
    pages: Sequence[str],
    origin: str,
    deadline: float,
    out_dir: str,
    settle: float = DEFAULT_SETTLE_SECONDS,
    expectations: Optional[Dict[str, str]] = None,
    endpoint: str = "",
) -> Report:
    """Walk ``pages`` with a deadline on every operation, recording each one.

    A page that hangs is recorded as an inspection limitation and the walk
    continues: that is the whole point. Each page opens (and closes) its own
    session, so a wedged connection cannot poison the next page.
    """
    expectations = expectations or {}
    report = Report(endpoint=endpoint, deadline_seconds=deadline,
                    started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    os.makedirs(out_dir, exist_ok=True)

    for page in pages:
        started = time.monotonic()
        session: Optional[CdpSession] = None
        try:
            session = await asyncio.wait_for(open_session(), deadline)
            # Every call is bounded twice on purpose: the session bounds each CDP
            # call, and this bounds the await. A transport that ignored its own
            # timeout, or wedged below it, would otherwise take the whole run
            # down -- which is exactly what happened on 2026-10-13 (report item 5).
            await asyncio.wait_for(
                session.call("Page.navigate", {"url": f"{origin}{page}"}, deadline),
                deadline)
            await asyncio.sleep(settle)
            text = await asyncio.wait_for(_visible_text(session, deadline), deadline)
            shot = await asyncio.wait_for(
                session.call("Page.captureScreenshot", {"format": "png"}, deadline),
                deadline)
            rel = _save_screenshot(out_dir, page, shot)
            expectation = expectations.get(page)
            met = None if expectation is None else (expectation in text)
            report.pages.append(PageRecord(
                page=page, status="ok", verdict=OBSERVED,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                detail=f"rendered {len(text)} characters",
                text_chars=len(text), screenshot=rel,
                expectation="" if met is None else ("met" if met else "missing"),
                needs_confirmation=met is False,
            ))
        except asyncio.TimeoutError:
            report.pages.append(PageRecord(
                page=page, status="timeout", verdict=INSPECTION_LIMITATION,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                detail=(f"no answer within {deadline:g}s; the page was skipped and "
                        "the remaining pages still ran"),
            ))
        except Exception as error:  # noqa: BLE001 - recorded, never fatal
            report.pages.append(PageRecord(
                page=page, status="error", verdict=INSPECTION_LIMITATION,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                detail=f"{type(error).__name__}: {error}",
            ))
        finally:
            if session is not None:
                try:
                    await asyncio.wait_for(session.close(), deadline)
                except Exception:  # noqa: BLE001
                    pass

    report.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write_report(out_dir, report)
    return report


async def _visible_text(session: CdpSession, deadline: float) -> str:
    result = await session.call("Runtime.evaluate", {
        "expression": "document.body ? document.body.innerText : ''",
        "returnByValue": True,
    }, deadline)
    return str((result or {}).get("result", {}).get("value") or "")


def _save_screenshot(out_dir: str, page: str, encoded: Any) -> str:
    if not encoded:
        return ""
    name = (page.strip("/").replace("/", "-") or "root") + ".png"
    path = os.path.join(out_dir, name)
    with open(path, "wb") as handle:
        handle.write(base64.b64decode(encoded))
    return path


def _write_report(out_dir: str, report: Report) -> str:
    path = os.path.join(out_dir, "inspection.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, ensure_ascii=False, indent=2)
    return path


def _load_pages(args: argparse.Namespace) -> List[str]:
    pages: List[str] = list(args.pages or [])
    if args.pages_file:
        with open(args.pages_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        pages.extend(payload.get("pages", payload if isinstance(payload, list) else []))
    if not pages:
        pages = list(DEFAULT_PAGES)
    seen: List[str] = []
    for page in pages:
        if page not in seen:
            seen.append(page)
    return seen


def _load_expectations(args: argparse.Namespace) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in args.expect or []:
        page, _, text = item.partition("=")
        if page and text:
            out[page] = text
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default="http://127.0.0.1:9222",
                        help="CDP HTTP endpoint of a running browser")
    parser.add_argument("--origin", default=None,
                        help="console origin (default: http://127.0.0.1:<port> from --endpoint)")
    parser.add_argument("--out", default=".cow-inspect", help="report and screenshot directory")
    parser.add_argument("--deadline", type=float, default=20.0,
                        help="seconds allowed per CDP operation (default 20)")
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE_SECONDS,
                        help="seconds to let a page settle after navigation")
    parser.add_argument("--pages", action="append", default=[],
                        help="page path to inspect (repeatable)")
    parser.add_argument("--pages-file", default="",
                        help='JSON file holding {"pages": [...]} or a bare list')
    parser.add_argument("--expect", action="append", default=[],
                        help="PATH=SUBSTRING that must appear in the rendered text")
    args = parser.parse_args(argv)

    origin = args.origin or _origin_from_endpoint(args.endpoint)
    pages = _load_pages(args)

    report = asyncio.run(inspect(
        open_session=lambda: CdpSession.open(args.endpoint),
        pages=pages,
        origin=origin,
        deadline=args.deadline,
        out_dir=args.out,
        settle=args.settle,
        expectations=_load_expectations(args),
        endpoint=args.endpoint,
    ))

    summary = report.summary()
    print(json.dumps({"origin": origin, "out": args.out, **summary}, ensure_ascii=False))
    for record in report.pages:
        print(f"  {record.status:8s} {record.verdict:20s} {record.page} — {record.detail}")
    return 0 if summary["observed"] else 2


def _origin_from_endpoint(endpoint: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(endpoint if "//" in endpoint else f"//{endpoint}")
    return f"http://{parsed.hostname or '127.0.0.1'}:9899"


if __name__ == "__main__":
    sys.exit(main())
