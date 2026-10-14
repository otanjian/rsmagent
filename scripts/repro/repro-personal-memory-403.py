# encoding:utf-8
"""Reproduce the "personal memory is 403" report against a synthetic install.

Change ``harden-windows-desktop-reliability``, task 3.1. The verification report
(docs/design/windows-desktop-verification-2026-10-13.md section 4.3) recorded the
memory console showing

    个人记忆目录被替换 / 请点击刷新重试；登录失效时请重新登录。

for ``GET /api/memory?...&scope=personal`` while the other fourteen console pages
were fine. The page text is a *symptom*: it renders the ``unsafe_path`` code, and
that code is raised from two different places with two different causes
(``user_root()`` refusing a replaced root, and ``scope_transaction`` refusing a
lock it could not take).

This script drives the real route on a **synthetic** installation:

* a fresh data root in a temporary directory (``COW_DATA_DIR``), so nothing of the
  developer's own config.json, identity.db, memory or uploads is read or written;
* the real ``app.py`` process, the real route registry and the real
  ``PersonalMemoryService`` behind it;
* login with the bootstrap administrator password the fresh instance writes.

It prints the HTTP status and machine-readable code for each step and then the
instance's own log tail, which carries the failing stage -- so the conclusion
comes from the code, not from the page's wording.

Usage:
    python scripts/repro/repro-personal-memory-403.py
    python scripts/repro/repro-personal-memory-403.py --keep --port 19899
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _request(url, *, method="GET", body=None, cookie=None, timeout=30, tenant=None, origin=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", cookie)
    if tenant:
        # Tenant-scoped routes resolve their tenant from this header; without it
        # the request is refused as missing_tenant before any memory code runs.
        request.add_header("X-Tenant-ID", str(tenant))
    if origin:
        # Cookie-authenticated writes are origin-checked; a browser always sends
        # this and the replay has to look like the browser it is standing in for.
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace"), response.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace"), error.headers.get("Set-Cookie", "")


def _wait_for_health(port, deadline_seconds):
    """Poll /api/health, reporting progress. A fresh instance bootstraps its
    identity store, agents and builtin skills first, which takes minutes."""
    url = "http://127.0.0.1:%d/api/health" % port
    started = time.time()
    last_note = 0.0
    while time.time() - started < deadline_seconds:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return True, response.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - "not up yet" is the normal case here
            pass
        elapsed = time.time() - started
        if elapsed - last_note >= 15:
            last_note = elapsed
            print("  ...waiting for the instance (%ds)" % int(elapsed), flush=True)
        time.sleep(1.0)
    return False, ""


def _bootstrap_password(data_root):
    path = os.path.join(data_root, ".bootstrap_admin_password")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        raw = handle.read().strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(parsed, dict):
        for key in ("password", "temporary_password", "value"):
            if parsed.get(key):
                return str(parsed[key])
    return raw


def _tail(path, lines=80):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\n") for line in handle.readlines()[-lines:]]


def _report(label, status, payload):
    try:
        body = json.loads(payload)
    except ValueError:
        body = {"raw": payload[:300]}
    code = body.get("code") or body.get("error") or ""
    print("%-48s status=%s code=%s" % (label, status, code), flush=True)
    if status >= 400:
        print("    body: %s" % payload[:300])
    return body


def _cookie_from(set_cookie):
    """The session cookie the login handler set.

    The database login path sets ``cow_session``; the legacy upstream one set
    ``cow_auth_token``. Taking the first cookie pair of the header covers both
    (and any future rename) without this script having to track the name.
    """
    if not set_cookie:
        return ""
    first = set_cookie.split(";")[0].strip()
    return first if "=" in first else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=19899)
    parser.add_argument("--keep", action="store_true", help="keep the synthetic instance")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--health-deadline", type=int, default=300)
    parser.add_argument("--workdir", default="",
                        help="scratch directory for the synthetic instance (default: a "
                             "sibling of this checkout, outside the user's home)")
    args = parser.parse_args()

    # The instance must live outside the user's home **and** outside its own data
    # root: state_dir refuses a tenant shared root that resolves into either, and
    # rightly so -- that guard is what keeps one tenant out of another's files.
    # A reproduction that ignored it would be measuring the guard instead of the
    # memory code, so the scratch tree is placed deliberately and the tenant base
    # is set explicitly.
    workdir = args.workdir or os.path.join(
        os.path.dirname(REPO), "cow-memory-repro-%s" % uuid.uuid4().hex[:8])
    data_root = os.path.join(workdir, "data")
    tenant_base = os.path.join(workdir, "tenant-base")
    os.makedirs(data_root, exist_ok=True)
    os.makedirs(tenant_base, exist_ok=True)

    home = os.path.realpath(os.path.expanduser("~")).lower()
    if os.path.realpath(workdir).lower().startswith(home):
        print("WARNING: %s is inside %s. The instance's own tenant-root guard will "
              "refuse the tenant -- an artefact of this scratch directory, not the "
              "behaviour under test. Pass --workdir outside home." % (workdir, home),
              flush=True)

    child_log = os.path.join(data_root, "child-output.log")
    env = dict(os.environ)
    env.update({
        "COW_DATA_DIR": data_root,
        "COW_TENANT_BASE": tenant_base,
        "COW_DESKTOP": "1",
        "COW_WEB_PORT": str(args.port),
    })
    print("synthetic work dir: %s" % workdir)
    print("data root: %s" % data_root)
    print("tenant base: %s" % tenant_base)
    print("python: %s" % args.python, flush=True)

    # The child's output goes to a file, never to a pipe this script might block
    # on while the child is still alive.
    sink = open(child_log, "w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        [args.python, "app.py"],
        cwd=REPO, env=env,
        stdout=sink, stderr=subprocess.STDOUT,
    )
    try:
        healthy, health = _wait_for_health(args.port, args.health_deadline)
        print("health: %s %s" % (healthy, health), flush=True)
        if not healthy:
            print("instance never became healthy; child output tail:")
            for line in _tail(child_log, 30):
                print("  " + line)
            return 2

        password = _bootstrap_password(data_root)
        if not password:
            print("no bootstrap password was written; cannot log in")
            return 2
        print("bootstrap administrator password read from the instance", flush=True)

        base = "http://127.0.0.1:%d" % args.port
        status, payload, set_cookie = _request(
            base + "/auth/login", method="POST",
            body={"username": "admin", "password": password},
        )
        body = _report("POST /auth/login", status, payload)
        cookie = _cookie_from(set_cookie)
        tenants = body.get("tenants") or []
        tenant = ""
        if tenants:
            first = tenants[0]
            tenant = str(first.get("id") or first.get("tenant_id") or "") if isinstance(first, dict) else str(first)
        print("session cookie: %s; tenant: %s; tenants offered: %s"
              % (bool(cookie), tenant or "(none)", tenants), flush=True)
        if status != 200 or not cookie or not tenant:
            print("login did not yield a tenant-scoped session; the console probes cannot run")
            return 2

        # The bootstrap administrator arrives restricted: until its password is
        # changed, every tenant route answers password_change_required, which is
        # the account gate and not a memory failure. Clear it so the probes below
        # actually reach the memory code, and say so explicitly.
        new_password = "Repro-403-Password-1"
        status, payload, _ = _request(
            base + "/auth/password", method="POST", cookie=cookie, origin=base,
            body={"old_password": password, "new_password": new_password},
        )
        _report("POST /auth/password (clear restricted)", status, payload)
        status, payload, set_cookie = _request(
            base + "/auth/login", method="POST",
            body={"username": "admin", "password": new_password},
        )
        _report("POST /auth/login (after change)", status, payload)
        cookie = _cookie_from(set_cookie) or cookie

        for label, path in (
            ("GET /api/memory scope=personal (reported)", "/api/memory?page=1&page_size=10&category=memory&scope=personal"),
            ("GET /api/memory scope=shared", "/api/memory?page=1&page_size=10&category=memory&scope=shared"),
            ("GET /api/memory no scope", "/api/memory?page=1&page_size=10&category=memory"),
        ):
            status, payload, _ = _request(base + path, cookie=cookie, tenant=tenant)
            _report(label, status, payload)

        status, payload, _ = _request(
            base + "/api/memory/personal", method="POST", cookie=cookie, tenant=tenant, origin=base,
            body={"action": "save", "id": "MEMORY.md", "content": "复现脚本写入的内容"},
        )
        _report("POST /api/memory/personal save", status, payload)

        status, payload, _ = _request(base + "/api/memory/personal", cookie=cookie, tenant=tenant)
        _report("GET /api/memory/personal", status, payload)

        print("\n--- instance log lines mentioning the failure ---", flush=True)
        for line in _tail(os.path.join(data_root, "run.log"), 200):
            if any(token in line for token in ("ERROR", "Traceback", "unsafe", "UnsafePath", "memory", "403", "被替换")):
                print("  " + line)
        print("--- end of log ---", flush=True)

        # The synthetic tenant/user roots, so the *shape* of the path that failed
        # is visible next to the error.
        print("\ntenant roots created by this instance:")
        tenants = os.path.join(os.path.expanduser("~"), ".cow", "tenant-roots", "tenants")
        if os.path.isdir(tenants):
            for name in sorted(os.listdir(tenants))[:10]:
                print("  %s" % os.path.join(tenants, name))
        print("\ndata root: %s" % data_root, flush=True)
        return 0
    finally:
        try:
            process.terminate()
            process.wait(timeout=30)
        except Exception:  # noqa: BLE001
            process.kill()
        sink.close()
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
            print("cleaned up %s" % workdir)
        else:
            print("kept %s" % workdir)


if __name__ == "__main__":
    sys.exit(main())
