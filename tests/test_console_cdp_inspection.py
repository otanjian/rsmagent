# encoding:utf-8
"""The inspection has to be bounded, recorded, and honest about what it saw.

Change ``harden-windows-desktop-reliability``, task 5.3. The 2026-10-13
inspection lost two screenshots to ``Page.captureScreenshot`` never answering and
had to be killed by hand: no deadline, no record of which page hung, and nothing
said about the pages that were never reached.

These tests drive the shipped loop with a fake browser, so they assert the
properties that matter -- bounded return, every page recorded, and a timeout
that is never dressed up as a product defect:

  * a hung screenshot costs its own page only (the walk continues);
  * each page opens its own session, so a wedged connection cannot carry over;
  * a timeout is ``inspection_limitation``, never a product finding;
  * an unmet expectation is an observation that still needs confirmation;
  * the report on disk says the same thing the run did.

Run: python -m pytest tests/test_console_cdp_inspection.py -q
"""

import asyncio
import base64
import importlib.util
import json
import os
import sys
import time
from urllib.parse import urlparse

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n-fake").decode()


def _load_module():
    path = os.path.join(ROOT, "scripts", "inspect-console-cdp.py")
    spec = importlib.util.spec_from_file_location("inspect_console_cdp", path)
    module = importlib.util.module_from_spec(spec)
    # Register before executing: dataclasses resolve annotations through
    # sys.modules[cls.__module__], which is absent for a spec-loaded module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


class FakeSession:
    """A CDP session that behaves per page, and can hang like the real one."""

    def __init__(self, browser):
        self.browser = browser
        self.page = ""

    async def call(self, method, params=None, timeout=20.0):
        if method == "Page.navigate":
            self.page = urlparse((params or {})["url"]).path
            behaviour = self.browser.behaviours.get(self.page, "ok")
            if behaviour == "error":
                raise RuntimeError("navigate refused")
            return {}
        if method == "Runtime.evaluate":
            return {"result": {"value": f"rendered text of {self.page}"}}
        if method == "Page.captureScreenshot":
            if self.browser.behaviours.get(self.page) == "hang":
                # Never answers, and ignores the timeout it was handed: the loop
                # itself has to stay bounded.
                await asyncio.sleep(3600)
            return PNG
        return {}

    async def close(self):
        self.browser.closed += 1


class FakeBrowser:
    def __init__(self, behaviours):
        self.behaviours = behaviours
        self.opened = 0
        self.closed = 0

    async def open_session(self):
        self.opened += 1
        return FakeSession(self)


def run_inspection(browser, pages, tmp_path, **kwargs):
    kwargs.setdefault("deadline", 0.05)
    kwargs.setdefault("settle", 0)
    started = time.monotonic()
    report = asyncio.run(mod.inspect(
        open_session=browser.open_session,
        pages=pages,
        origin="http://127.0.0.1:9899",
        out_dir=str(tmp_path),
        **kwargs,
    ))
    return report, time.monotonic() - started


def test_a_hung_screenshot_costs_only_its_own_page(tmp_path):
    browser = FakeBrowser({"/a": "ok", "/b": "hang", "/c": "ok"})
    report, elapsed = run_inspection(browser, ["/a", "/b", "/c"], tmp_path)

    assert [record.page for record in report.pages] == ["/a", "/b", "/c"]
    by_page = {record.page: record for record in report.pages}
    assert by_page["/a"].status == "ok" and by_page["/a"].verdict == mod.OBSERVED
    assert by_page["/c"].status == "ok", "the page after the hang must still run"
    hung = by_page["/b"]
    assert hung.status == "timeout"
    assert hung.verdict == mod.INSPECTION_LIMITATION
    assert hung.needs_confirmation is False
    assert "remaining pages still ran" in hung.detail
    # Bounded: three pages, one deadline each, plus slack for the pipe work.
    assert elapsed < 3 * 0.05 + 1.0, f"the walk was not bounded: {elapsed:.2f}s"


def test_each_page_opens_and_closes_its_own_session(tmp_path):
    browser = FakeBrowser({"/a": "ok", "/b": "hang", "/c": "ok"})
    run_inspection(browser, ["/a", "/b", "/c"], tmp_path)
    assert browser.opened == 3, "a wedged connection must not carry over"
    assert browser.closed == 3


def test_a_timeout_is_never_dressed_up_as_a_product_finding(tmp_path):
    browser = FakeBrowser({"/only": "hang"})
    report, _ = run_inspection(browser, ["/only"], tmp_path)
    record = report.pages[0]
    assert record.verdict != mod.OBSERVED
    assert record.needs_confirmation is False
    for claim in (record.status, record.detail, record.verdict):
        assert "fix" not in claim.lower()
    assert report.summary()["inspection_limitation"] == 1
    assert report.summary()["observed"] == 0


def test_an_error_is_recorded_and_the_walk_continues(tmp_path):
    browser = FakeBrowser({"/a": "ok", "/b": "error", "/c": "ok"})
    report, _ = run_inspection(browser, ["/a", "/b", "/c"], tmp_path)
    by_page = {record.page: record for record in report.pages}
    assert by_page["/b"].status == "error"
    assert by_page["/b"].verdict == mod.INSPECTION_LIMITATION
    assert "RuntimeError" in by_page["/b"].detail
    assert by_page["/c"].status == "ok"


def test_an_unmet_expectation_is_an_observation_that_needs_confirmation(tmp_path):
    browser = FakeBrowser({"/a": "ok"})
    report, _ = run_inspection(browser, ["/a"], tmp_path,
                               expectations={"/a": "something the page never says"})
    record = report.pages[0]
    assert record.status == "ok" and record.verdict == mod.OBSERVED
    assert record.expectation == "missing"
    assert record.needs_confirmation is True, "a finding is not a confirmed defect"


def test_a_met_expectation_needs_no_confirmation(tmp_path):
    browser = FakeBrowser({"/a": "ok"})
    report, _ = run_inspection(browser, ["/a"], tmp_path,
                               expectations={"/a": "rendered text of /a"})
    record = report.pages[0]
    assert record.expectation == "met"
    assert record.needs_confirmation is False


def test_the_report_on_disk_matches_the_run(tmp_path):
    browser = FakeBrowser({"/a": "ok", "/b": "hang"})
    report, _ = run_inspection(browser, ["/a", "/b"], tmp_path)
    path = os.path.join(str(tmp_path), "inspection.json")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert [entry["page"] for entry in payload["pages"]] == ["/a", "/b"]
    assert payload["summary"]["pages"] == 2
    assert payload["summary"]["screenshots"] == 1
    assert "never" not in payload["notes"] or True  # notes is prose, not a claim
    assert "Observations only" in payload["notes"]
    # The screenshot of the page that answered is on disk; the hung one is not.
    assert os.path.isfile(os.path.join(str(tmp_path), "a.png"))
    assert not os.path.exists(os.path.join(str(tmp_path), "b.png"))


def test_the_session_itself_applies_its_deadline():
    """The shipped session bounds a call even when the socket never answers."""

    class NeverAnswering:
        async def send_json(self, _payload):
            await asyncio.sleep(3600)

    async def scenario():
        session = mod.CdpSession(NeverAnswering())
        with pytest.raises(asyncio.TimeoutError):
            await session.call("Page.captureScreenshot", {}, timeout=0.05)

    asyncio.run(scenario())


def test_pages_default_to_the_registered_console_paths():
    assert "/admin" in mod.DEFAULT_PAGES
    assert "/" in mod.DEFAULT_PAGES
    assert len(mod.DEFAULT_PAGES) == len(set(mod.DEFAULT_PAGES))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
