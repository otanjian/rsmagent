"""The process lifecycle record: telling a crash and a kill apart.

Every test here runs the real thing in a child interpreter, because the whole
point of the module is what a *dying process* leaves behind. Nothing is
monkeypatched into the suite's own process: ``faulthandler`` and ``atexit`` are
process-global, and a test that installed them here would change how the rest of
the run reports its own failures.
"""

import os
import subprocess
import sys
import time

import pytest

from common import process_watch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INSTALL = "from common import process_watch; process_watch.install()"


def _run_child(code: str, record) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env[process_watch.ENV_OVERRIDE] = str(record)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, env=env, capture_output=True,
        encoding="utf-8", errors="replace", timeout=90,
    )


def _read(record) -> str:
    if not os.path.exists(record):
        return ""
    with open(record, encoding="utf-8", errors="replace") as f:
        return f.read()


def test_a_start_and_a_clean_exit_are_both_recorded(tmp_path):
    record = tmp_path / "app-lifecycle.log"

    done = _run_child(INSTALL, record)

    assert done.returncode == 0
    text = _read(record)
    assert "process started" in text
    assert "python=" in text
    assert "clean interpreter exit" in text
    # The child's own pid, never the suite's: a record that cannot be tied to a
    # process is useless when several are involved.
    assert "pid=%d " % os.getpid() not in text
    assert "pid=" in text


def test_a_killed_process_leaves_no_exit_line(tmp_path):
    """The central claim: silence about the exit means someone else ended it.

    A record with a start line and no exit line at all is the signature of an
    external ``TerminateProcess`` / task kill, which is indistinguishable from a
    clean shutdown in ``run.log``.
    """
    record = tmp_path / "app-lifecycle.log"
    env = dict(os.environ)
    env[process_watch.ENV_OVERRIDE] = str(record)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.Popen(
        [sys.executable, "-c", INSTALL + "\nimport time\ntime.sleep(60)\n"],
        cwd=REPO_ROOT, env=env,
    )
    try:
        deadline = time.time() + 30
        while "process started" not in _read(record) and time.time() < deadline:
            time.sleep(0.1)
        assert "process started" in _read(record)
    finally:
        child.kill()
        child.wait(timeout=30)

    text = _read(record)
    assert "process started" in text
    assert "clean interpreter exit" not in text
    assert "exiting" not in text


def test_a_fatal_native_fault_dumps_the_stack(tmp_path):
    record = tmp_path / "app-lifecycle.log"

    done = _run_child(INSTALL + "\nimport ctypes\nctypes.string_at(0)\n", record)

    assert done.returncode != 0
    assert "fatal" in _read(record).lower()


def test_an_os_exit_path_announces_itself(tmp_path):
    """os._exit skips atexit, so the reason has to be written before it."""
    record = tmp_path / "app-lifecycle.log"

    done = _run_child(
        INSTALL + "\nimport os\n"
        "process_watch.record_exit('web console not serving within 25s')\n"
        "os._exit(1)\n",
        record,
    )

    assert done.returncode == 1
    text = _read(record)
    assert "exiting reason=web console not serving within 25s" in text
    assert "clean interpreter exit" not in text


def test_an_unhandled_exception_records_its_traceback(tmp_path):
    record = tmp_path / "app-lifecycle.log"

    _run_child(INSTALL + "\nraise RuntimeError('startup exploded')\n", record)

    text = _read(record)
    assert "unhandled exception in main thread" in text
    assert "RuntimeError: startup exploded" in text


def test_a_worker_thread_failure_is_recorded_without_killing_the_process(tmp_path):
    """A channel thread dying must be visible even though the app keeps serving."""
    record = tmp_path / "app-lifecycle.log"

    done = _run_child(
        INSTALL + "\nimport threading\n"
        "def boom():\n    raise ValueError('channel thread died')\n"
        "t = threading.Thread(target=boom, name='web')\n"
        "t.start(); t.join()\n",
        record,
    )

    assert done.returncode == 0
    text = _read(record)
    assert "unhandled exception in thread web" in text
    assert "ValueError: channel thread died" in text
    assert "clean interpreter exit" in text


def test_an_unwritable_record_degrades_to_stderr_and_still_starts(tmp_path):
    """Observability may never be the reason a deployment fails to start."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("in the way")
    record = blocker / "nested" / "app-lifecycle.log"

    done = _run_child(INSTALL + "\nprint('started anyway')\n", record)

    assert done.returncode == 0
    assert "started anyway" in done.stdout
    assert "process started" in done.stderr
    assert not os.path.exists(record)


def test_installing_twice_records_one_start(tmp_path):
    record = tmp_path / "app-lifecycle.log"

    done = _run_child(INSTALL + "\n" + INSTALL + "\n", record)

    assert done.returncode == 0
    assert _read(record).count("process started") == 1


def test_the_record_is_rotated_instead_of_growing_without_bound(tmp_path):
    record = tmp_path / "app-lifecycle.log"
    record.write_text("x" * (process_watch.MAX_BYTES + 1))

    done = _run_child(INSTALL, record)

    assert done.returncode == 0
    assert os.path.exists(str(record) + ".1")
    assert os.path.getsize(record) < process_watch.MAX_BYTES


def test_the_record_sits_beside_the_main_log(tmp_path, monkeypatch):
    """One rule for the data root, shared with run.log (see common/log.py)."""
    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(process_watch.ENV_OVERRIDE, raising=False)

    assert process_watch.lifecycle_path() == str(tmp_path / "app-lifecycle.log")

    monkeypatch.setenv(process_watch.ENV_OVERRIDE, str(tmp_path / "elsewhere.log"))
    assert process_watch.lifecycle_path() == str(tmp_path / "elsewhere.log")


def test_the_record_is_not_the_log_the_launcher_rotates(tmp_path):
    """It must survive the rotation that deletes run-console.log.

    That rotation removes the previous run's stdout, which is precisely the
    record the watchdog needs when it finds the app gone.
    """
    assert process_watch.DEFAULT_FILENAME == "app-lifecycle.log"
    assert process_watch.DEFAULT_FILENAME != "run-console.log"
