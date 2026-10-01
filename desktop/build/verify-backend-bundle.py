#!/usr/bin/env python3
"""Verify a built ``cowagent-backend`` bundle before it is shipped.

Change ``align-desktop-project-execution-with-master`` (tasks 10.1-10.3).

Why this exists as a build step rather than an assertion in a test suite: the
things that go wrong in a frozen bundle cannot be seen from the source tree. The
unit tests import the worker as a *module* from a checkout where every path and
dependency is present; the shipped artifact is a compiled onedir folder where the
interpreter is the binary itself and the standard library lives inside
``_internal``. Only running the real binary answers the question.

Three properties are checked, each of which has actually been wrong at some point
in this change:

1. **The packaged worker seam answers.** An installed app has no ``python`` on
   ``PATH`` and cannot pass ``-m``; it starts the binary with
   ``--desktop-local-worker`` instead. If the fast dispatch in ``app.py`` is
   reordered behind a heavy import, this is what notices.
2. **The probe describes the *bundle*, not the build machine.** The desktop
   derives its sandbox read paths from ``--probe``; if it reported the build
   interpreter's prefix, every read inside the sandbox would be refused.
3. **The master tools are actually bundled and functional.** A ``hello`` +
   ``describe`` + one ``read`` over the real stdio protocol proves the tool
   classes were collected -- the failure this guards against (an over-eager
   ``excludes``) builds a bundle that starts and then cannot run a single tool.

Exit code is 0 only when all three hold; any failure names what was expected and
what was observed, because this runs in CI where the log is the only evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

# The bundle's own protocol module is imported *by relative path*: this script
# may run before the app's dependencies are installed in some CI legs, so it must
# not require the package to import. The two constants it needs are re-declared
# here and pinned to the source of truth by the check below.
WORKER_FLAG = "--desktop-local-worker"
PROBE_FLAG = "--probe"

#: The port the packaged backend is asked to listen on during verification. A
#: fixed high port (not 0) so the check can address it; the scratch HOME means a
#: collision is the only thing that could interfere, and that fails loudly.
PACKAGED_PORT = "19733"

#: The tools the worker must be able to build. Mirrors
#: ``agent.desktop_local.protocol.ALLOWED_TOOLS``; asserted against that module
#: when it is importable so the two cannot drift.
EXPECTED_TOOLS = ("read", "write", "edit", "bash", "ls", "search_files")


def bundle_executable(distpath: str) -> str:
    """The worker executable inside ``distpath``, for this platform.

    PyInstaller's onedir output puts the executable *inside* a folder named after
    the spec's ``name``, whose contents are ``_internal`` plus the executable.
    """
    name = "cowagent-backend.exe" if os.name == "nt" else "cowagent-backend"
    return os.path.join(distpath, "cowagent-backend", name)


def fail(message: str) -> None:
    sys.stderr.write(f"::error::{message}\n")
    raise SystemExit(1)


def check_constants_against_source() -> None:
    """Refuse to verify with constants that no longer match the app's.

    A guard that quietly checks the wrong flag is worse than no guard, so when
    the package is importable its values win.
    """
    global WORKER_FLAG, PROBE_FLAG, EXPECTED_TOOLS  # noqa: PLW0603
    try:
        from agent.desktop_local import protocol as proto
        from agent.desktop_local import worker as worker_mod
    except Exception:
        return
    mismatches = []
    if worker_mod.WORKER_FLAG != WORKER_FLAG:
        mismatches.append(
            f"worker flag: app has {worker_mod.WORKER_FLAG!r}, verifier has {WORKER_FLAG!r}")
    if worker_mod.PROBE_FLAG != PROBE_FLAG:
        mismatches.append(
            f"probe flag: app has {worker_mod.PROBE_FLAG!r}, verifier has {PROBE_FLAG!r}")
    if tuple(proto.ALLOWED_TOOLS) != tuple(EXPECTED_TOOLS):
        mismatches.append(
            f"tools: app has {tuple(proto.ALLOWED_TOOLS)!r}, verifier has {tuple(EXPECTED_TOOLS)!r}")
    if mismatches:
        fail("verifier constants drifted from the app: " + "; ".join(mismatches))


def check_probe(binary: str) -> dict:
    """Run ``--desktop-local-worker --probe`` and validate the report."""
    proc = subprocess.run(
        [binary, WORKER_FLAG, PROBE_FLAG],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        fail(f"probe exited {proc.returncode}; stderr: {proc.stderr.strip()[:2000]}")
    report = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                report = json.loads(line)
            except Exception:
                continue
    if not isinstance(report, dict):
        fail(f"probe did not print a JSON object; stdout: {proc.stdout.strip()[:2000]}")

    # A bundled worker must say so, and must point at the bundle rather than a
    # build machine's interpreter prefix -- that is the whole reason the probe
    # exists. (`frozen` false means we were handed a source checkout by mistake.)
    if not report.get("frozen"):
        fail(f"probe reports frozen={report.get('frozen')!r}; a shipped bundle must be frozen")
    bundle_root = report.get("bundleRoot") or ""
    if not bundle_root or not os.path.isdir(bundle_root):
        fail(f"probe bundleRoot is not a directory: {bundle_root!r}")
    expected_bin = os.path.realpath(binary)
    if os.path.realpath(report.get("realPath") or "") != expected_bin:
        fail(f"probe realPath {report.get('realPath')!r} is not the bundle {expected_bin!r}")

    # The read list a frozen run needs. PyInstaller keeps the standard library in
    # `_internal/base_library.zip`, so `sysconfig` reports the layout of the
    # machine the bundle was *built* on -- `stdlib`/`purelib` name directories
    # that do not exist in the shipped bundle, and must not be asserted on.
    # What the sandbox is actually granted for a frozen runtime is the bundle root
    # and the executable's own directory (`interpreterReadPaths`), so those are
    # the ones that have to be real: a launch that grants a nonexistent path dies
    # before the handshake and is reported as "the worker crashed".
    read_paths = [bundle_root, os.path.dirname(expected_bin)]
    for value in read_paths:
        if not os.path.isdir(value):
            fail(f"frozen read path does not exist on disk: {value!r}")
    return report


class _WorkerSession:
    """A live bundle process, spoken to over its real stdio protocol."""

    def __init__(self, binary: str) -> None:
        self.proc = subprocess.Popen(
            [binary, WORKER_FLAG],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._stderr: list[str] = []
        for stream, sink in ((self.proc.stdout, self._lines), (self.proc.stderr, None)):
            threading.Thread(target=self._pump, args=(stream, sink), daemon=True).start()
        self._seq = 0

    def _pump(self, stream, sink) -> None:
        try:
            for line in stream:
                if sink is None:
                    self._stderr.append(line)
                else:
                    sink.put(line)
        except Exception:
            return

    def send(self, op: str, **fields) -> None:
        self._seq += 1
        frame = {"id": f"v{self._seq}", "op": op, **fields}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame, ensure_ascii=True) + "\n")
        self.proc.stdin.flush()

    def reply(self, request_id: str, timeout: float = 180.0) -> dict:
        """The reply to ``request_id``, skipping unrelated frames."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fail(f"no reply to {request_id!r} within {timeout}s; stderr: {self.stderr()}")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            try:
                frame = json.loads(line)
            except Exception:
                continue
            if isinstance(frame, dict) and frame.get("id") == request_id:
                return frame

    def stderr(self) -> str:
        return "".join(self._stderr).strip()[:2000]

    def close(self) -> int:
        if self.proc.poll() is None:
            self.proc.kill()
        return self.proc.wait(timeout=30)


def check_tools_run(binary: str) -> None:
    """Hello, describe and one real ``read`` against the frozen master tools."""
    with tempfile.TemporaryDirectory(prefix="cow-bundle-check-") as project:
        temp = os.path.join(project, ".cow-tmp")
        os.makedirs(temp)
        probe_file = os.path.join(project, "bundle-check.txt")
        marker = "bundle-check-marker-2f8a1c"
        with open(probe_file, "w", encoding="utf-8") as handle:
            handle.write(marker + "\n")

        session = _WorkerSession(binary)
        try:
            session.send("hello", root=project, temp=temp)
            hello = session.reply("v1")
            if hello.get("status") != "success":
                fail(f"hello failed inside the bundle: {hello.get('error')} (stderr: {session.stderr()})")
            tools = set(hello.get("tools") or ())
            missing = [name for name in EXPECTED_TOOLS if name not in tools]
            if missing:
                fail(f"bundle built only {sorted(tools)}; missing tools: {missing}")

            session.send("describe")
            describe = session.reply("v2")
            if describe.get("status") != "success":
                fail(f"describe failed inside the bundle: {describe.get('error')}")

            session.send("call", tool="read", arguments={"path": probe_file})
            called = session.reply("v3")
            if called.get("status") != "success":
                fail(f"read failed inside the bundle: {called.get('error')} "
                     f"(stderr: {session.stderr()})")
            serialized = json.dumps(called)
            if marker not in serialized:
                fail("the read result did not contain the file's contents: "
                     f"{serialized[:500]}")

            session.send("shutdown")
            shutdown = session.reply("v4")
            if shutdown.get("status") != "success":
                fail(f"shutdown failed inside the bundle: {shutdown.get('error')}")
            session.proc.stdin.close()
            if session.proc.wait(timeout=30) != 0:
                fail(f"worker exited {session.proc.returncode} after shutdown; "
                     f"stderr: {session.stderr()}")
        finally:
            session.close()


def check_build_warnings(workpath: str) -> None:
    """Refuse a bundle whose warnings name a module we could not compile.

    PyInstaller does not fail when a module in the graph fails to compile -- it
    records `invalid module named X` in its warnings file and builds anyway, so
    the bundle starts and then cannot run the tool. Naming the modules here is
    what turns "the desktop cannot run any local command" into a build error with
    a file to fix.

    `missing module named` is deliberately not an error: optional dependencies
    (lark_oapi, playwright's extras, per-platform packages) are expected to be
    absent and the code that uses them already degrades.
    """
    warn_file = os.path.join(workpath, "warn-cowagent-backend.txt")
    if not os.path.isfile(warn_file):
        print(f"==> note: no PyInstaller warnings file at {warn_file}; skipping that check")
        return
    invalid = []
    with open(warn_file, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("invalid module named"):
                invalid.append(line.strip())
    if invalid:
        for line in invalid:
            sys.stderr.write(f"::error::{line}\n")
        fail(f"{len(invalid)} module(s) in the bundle could not be compiled: fix the syntax "
             f"(see the lines above) rather than excluding them")
    print("==> ok: no uncompilable modules in the PyInstaller graph")


def check_shipped_data(binary: str) -> None:
    """Refuse a bundle whose import-time data files are missing.

    PyInstaller collects *Python* automatically; data files it only ships if the
    spec lists them. A missing one does not fail the build -- it fails the first
    request that reads it, inside an installed app, as a 500. That is exactly how
    `contracts/desktop/v1.json` shipped missing: `auth/desktop_contracts.py` reads
    it at import time, so `/api/desktop/meta` answered `FileNotFoundError` while
    everything else looked healthy.

    The list is of *files the shipped code opens by a repo-relative path*, so it is
    short and has to name a real consumer for each entry.
    """
    internal = os.path.join(os.path.dirname(os.path.realpath(binary)), "_internal")
    required = [
        # auth/desktop_contracts.py::load_contract (module scope) and
        # auth/desktop_contracts_v2.py -- read by the /api/desktop/meta handler.
        os.path.join("contracts", "desktop", "v1.json"),
        os.path.join("contracts", "desktop", "v2.json"),
        # The console page is assembled per request from these.
        os.path.join("channel", "web", "chat.html"),
        os.path.join("channel", "web", "templates"),
        os.path.join("channel", "web", "static"),
        # Skills are executed by the packaged interpreter.
        "skills",
    ]
    missing = [rel for rel in required if not os.path.exists(os.path.join(internal, rel))]
    if missing:
        fail("the bundle is missing data its own code reads: " + ", ".join(missing)
             + f" (looked under {internal})")
    print(f"==> ok: {len(required)} shipped data paths present")


def check_meta_endpoint(binary: str) -> None:
    """Boot the frozen backend as a server and read `/api/desktop/meta`.

    The worker protocol check above proves the *tools* work; this proves the
    **application** does: that the packaged Python starts every channel, finds its
    templates and contracts, and answers the one endpoint the desktop negotiates
    against. It is the closest a build step can get to "the installed app works"
    without a signed artifact, and it is the check that found the missing
    `contracts/` entry.

    Everything is pointed at a throwaway directory (`COW_DATA_DIR`, `HOME`) so a
    verify run cannot read or write the operator's own data.
    """
    import urllib.error
    import urllib.request

    with tempfile.TemporaryDirectory(prefix="cow-appcheck-") as scratch:
        home = os.path.join(scratch, "home")
        data = os.path.join(scratch, "data")
        os.makedirs(home)
        os.makedirs(data)
        # The listen port comes from `web_port` in the data dir's config.json; the
        # app does not read it from the environment, so a scratch config is the
        # only way to pick a port without touching the operator's own config.
        with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as handle:
            json.dump({"web_port": int(PACKAGED_PORT)}, handle)
        env = {
            **os.environ,
            "HOME": home,
            "COW_DATA_DIR": data,
            "COW_DESKTOP": "1",
        }
        proc = subprocess.Popen(
            [binary], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, cwd=scratch,
        )
        lines: "queue.Queue[str]" = queue.Queue()
        collected: list[str] = []

        def _pump() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    collected.append(line)
                    lines.put(line)
            except Exception:
                return

        threading.Thread(target=_pump, daemon=True).start()

        url = f"http://127.0.0.1:{PACKAGED_PORT}/api/desktop/meta"
        deadline = time.monotonic() + 180.0
        payload = None
        try:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    fail("the packaged backend exited before answering "
                         f"(code {proc.returncode}):\n" + "".join(collected)[-3000:])
                try:
                    with urllib.request.urlopen(url, timeout=5) as response:
                        body = response.read().decode("utf-8", "replace")
                        if response.status == 200:
                            payload = json.loads(body)
                            break
                except (urllib.error.URLError, ConnectionError, OSError, ValueError):
                    pass
                time.sleep(1.0)
            if payload is None:
                fail("the packaged backend never answered /api/desktop/meta within 180s; "
                     "tail of output:\n" + "".join(collected)[-3000:])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()

    # The response wraps the payload in the app's standard envelope
    # (`{"status": "success", "data": {...}}`), so unwrap it before checking the
    # contract's required keys.
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    for key in ("protocols", "features", "entry_path"):
        if key not in payload:
            fail(f"/api/desktop/meta answered without {key!r}: {json.dumps(payload)[:500]}")
    # The v2 protocol must be *declared* even when its switch is off: the desktop
    # decides what it may attempt from this block, and an absent block reads as
    # "this server has no v2" rather than "v2 exists but is disabled here".
    if "project_execution" not in (payload.get("protocols") or {}):
        fail("the packaged backend did not declare the project_execution protocol: "
             f"{json.dumps(payload.get('protocols'))[:500]}")
    print(f"==> ok: packaged backend served /api/desktop/meta "
          f"(protocols={sorted(payload.get('protocols') or {})}, "
          f"features={sorted(payload.get('features') or {})})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distpath", default=os.path.join("desktop", "build", "dist"),
                        help="directory containing cowagent-backend/ (default: desktop/build/dist)")
    parser.add_argument("--workpath", default=os.path.join("desktop", "build", "build-work", "cowagent-backend"),
                        help="PyInstaller work directory holding warn-cowagent-backend.txt")
    args = parser.parse_args(argv)

    check_build_warnings(args.workpath)
    check_constants_against_source()
    binary = os.path.realpath(bundle_executable(args.distpath))
    if not os.path.isfile(binary):
        fail(f"no bundle executable at {binary}; run PyInstaller first")

    report = check_probe(binary)
    print(f"==> probe ok: frozen={report.get('frozen')} bundleRoot={report.get('bundleRoot')}")
    check_tools_run(binary)
    print(f"==> bundle ok: {'/'.join(EXPECTED_TOOLS)} all answered over stdio")
    check_shipped_data(binary)
    if os.environ.get("COW_SKIP_APP_CHECK") == "1":
        print("==> note: COW_SKIP_APP_CHECK=1, skipping the packaged-server check")
    else:
        check_meta_endpoint(binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
