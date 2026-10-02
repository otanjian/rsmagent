# encoding:utf-8
"""The local execution worker: real tools, real process, real boundary.

Change ``align-desktop-project-execution-with-master`` (tasks 4.1 / 4.2 / 4.3).

Two things are being protected, and only one of them is about the protocol:

1. **Reuse, not reimplementation.** The worker runs the *same* tool classes the
   server's Agent runs. The tests assert behaviour that only the original
   implementations have (bash writes a real file, ``read`` reports a real line
   count), so swapping in a lookalike would fail them.
2. **The boundary is the process.** The worker gets the project by handshake, its
   environment is stripped by :func:`build_worker_env`, and a call that names a
   location outside the project is refused before any tool runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _worker_env(extra=None):
    from agent.desktop_local.worker import build_worker_env

    env = build_worker_env()
    env.update(extra or {})
    return env


def _capture_signal_handler(worker_mod, worker):
    """Install the worker's stop-signal cleanup and hand back its handler.

    ``_install_signal_cleanup`` registers with ``signal.signal``, which refuses
    to run outside the main thread; capturing the registration instead keeps the
    test on the handler's own behaviour.
    """
    captured = {}

    def _register(number, handler):
        captured[number] = handler

    with mock.patch.object(worker_mod.signal, "signal", side_effect=_register):
        worker_mod._install_signal_cleanup(worker)
    assert captured, "no signal handler was registered"
    return next(iter(captured.values()))


class _WorkerProcess:
    """A real ``python -m agent.desktop_local.worker`` on a pipe."""
    def __init__(self, root, temp=None, env=None):
        self.root = root
        self.temp = temp or os.path.join(root, ".cow-tmp")
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "agent.desktop_local.worker"],
            cwd=str(REPO_ROOT), env=env or _worker_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self._next_id = 0

    def send(self, **frame):
        self._next_id += 1
        frame.setdefault("id", f"req-{self._next_id}")
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        return frame["id"]

    def recv(self):
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise AssertionError(
                f"worker closed the pipe unexpectedly; stderr:\n{stderr}")
        return json.loads(line)

    def call(self, op, **frame):
        request_id = self.send(op=op, **frame)
        reply = self.recv()
        if reply.get("id") != request_id:
            raise AssertionError(
                f"reply id mismatch: expected {request_id!r}, got {reply!r}")
        return reply

    def hello(self, root=None, temp=None):
        return self.call("hello", root=root or self.root,
                         temp=temp or self.temp)

    def run_tool(self, tool, **arguments):
        return self.call("call", tool=tool, arguments=arguments)

    def close(self):
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:
                pass


class WorkerProtocolTests(unittest.TestCase):
    """Pure frame handling: nothing here needs a subprocess."""

    def test_decode_refuses_non_objects(self):
        from agent.desktop_local import protocol as proto

        for bad in ("", "   ", "[1,2]", "not json", '"a string"'):
            with self.assertRaises(proto.WorkerProtocolError) as caught:
                proto.decode(bad)
            self.assertEqual(caught.exception.code, "invalid_request")

    def test_decode_refuses_an_oversized_frame(self):
        from agent.desktop_local import protocol as proto

        with self.assertRaises(proto.WorkerProtocolError) as caught:
            proto.decode("x" * (proto.MAX_FRAME_BYTES + 1))
        self.assertEqual(caught.exception.code, "frame_too_large")

    def test_unknown_codes_collapse_to_internal(self):
        from agent.desktop_local import protocol as proto

        error = proto.WorkerProtocolError("not_a_real_code", "x")
        self.assertEqual(error.code, "internal")

    def test_bounded_result_replaces_rather_than_truncates(self):
        from agent.desktop_local import protocol as proto

        small = {"a": 1}
        self.assertEqual(proto.bounded_result(small), small)
        huge = {"blob": "x" * (proto.MAX_RESULT_BYTES + 100)}
        bounded = proto.bounded_result(huge)
        self.assertTrue(bounded["truncated"])
        self.assertIn("preview", bounded)

    def test_the_allowed_tool_set_is_closed(self):
        from agent.desktop_local import protocol as proto

        self.assertEqual(
            tuple(proto.ALLOWED_TOOLS),
            ("read", "write", "edit", "bash", "ls", "search_files"))


class WorkerEnvTests(unittest.TestCase):
    """No credential, no identity store, no unrelated scope reaches the worker."""

    def test_keeps_only_what_a_tool_needs(self):
        from agent.desktop_local.worker import build_worker_env

        env = build_worker_env({
            "PATH": "/usr/bin", "HOME": "/tmp/home", "LANG": "zh_CN.UTF-8",
            "COW_DESKTOP": "1", "SOME_UNRELATED_THING": "x",
        })
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/tmp/home")
        self.assertEqual(env["COW_DESKTOP"], "1")
        self.assertNotIn("SOME_UNRELATED_THING", env)

    def test_drops_secrets_even_when_the_name_looks_keepable(self):
        from agent.desktop_local.worker import build_worker_env

        env = build_worker_env({
            "PATH": "/usr/bin",
            "COW_SESSION_TOKEN": "bearer-abc",
            "OPENAI_API_KEY": "sk-123",
            "CUSTOM_api_key": "sk-456",
            "DESKTOP_AUTH_SECRET": "s",
            "IDENTITY_DB_PASSWORD": "p",
            "MY_COOKIE": "c",
            "PATH_CREDENTIAL_FILE": "/etc/shadow",
        })
        self.assertEqual(set(env), {"PATH"}, env)

    def test_a_named_database_path_is_not_kept_by_default(self):
        from agent.desktop_local.worker import build_worker_env

        env = build_worker_env({"PATH": "/usr/bin",
                                "COW_IDENTITY_DB": "/data/identity.db"})
        self.assertNotIn("COW_IDENTITY_DB", env)

    def test_the_temp_directory_is_the_granted_one_not_the_inherited_one(self):
        from agent.desktop_local.worker import build_worker_env

        # Inherited TMPDIR points outside the sandbox's writable grant, where
        # `tempfile.gettempdir()` finds nothing usable -- so it is replaced, in
        # every name a tool might consult, rather than kept.
        env = build_worker_env(
            {"PATH": "/usr/bin", "TMPDIR": "/var/folders/unwritable/T",
             "TEMP": "/var/folders/unwritable/T", "TMP": "/var/folders/unwritable/T"},
            temp="/private/tmp/cow/run",
        )
        self.assertEqual(env["TMPDIR"], "/private/tmp/cow/run")
        self.assertEqual(env["TEMP"], "/private/tmp/cow/run")
        self.assertEqual(env["TMP"], "/private/tmp/cow/run")

    def test_no_temp_directory_is_invented_when_none_was_granted(self):
        from agent.desktop_local.worker import build_worker_env

        # Setting it to something unwritable would be worse than omitting it:
        # the failure would move from a clear "no usable temp directory" to an
        # EPERM inside whichever tool happened to touch it.
        env = build_worker_env({"PATH": "/usr/bin", "TMPDIR": "/var/folders/x/T"})
        self.assertEqual(set(env), {"PATH"}, env)


class WorkerTempEnvironmentTests(unittest.TestCase):
    """The worker points itself (and so its tools) at its own granted temp."""

    def test_apply_temp_environment_redirects_os_and_tempfile(self):
        from agent.desktop_local import worker as worker_mod

        with tempfile.TemporaryDirectory(prefix="desk-temp-") as tmp:
            saved = {k: os.environ.get(k) for k in ("TMPDIR", "TEMP", "TMP")}
            saved_file = tempfile.tempdir
            self.addCleanup(self._restore, saved, saved_file)
            tempfile.gettempdir()  # populate the cache that must be invalidated
            resolved = worker_mod.apply_temp_environment(tmp)
            real = os.path.realpath(tmp)
            self.assertEqual(resolved, real)
            for key in ("TMPDIR", "TEMP", "TMP"):
                self.assertEqual(os.environ[key], real)
            self.assertEqual(tempfile.gettempdir(), real)
            # Creating one is the assertion that matters.
            with tempfile.NamedTemporaryFile() as handle:
                handle.write(b"scratch")
                self.assertTrue(handle.name.startswith(real))

    @staticmethod
    def _restore(saved, saved_file):
        import tempfile as tempfile_mod

        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        tempfile_mod.tempdir = saved_file


class ProcessGroupTests(unittest.TestCase):
    """The worker remembers the command trees it creates, so they can be killed."""

    def test_a_recorded_group_is_killed(self):
        from agent.desktop_local.worker import _ProcessGroups

        if os.name != "posix":  # pragma: no cover - the desktop worker is posix
            self.skipTest("process groups are a posix concept")
        groups = _ProcessGroups()
        # The real shape: a shell makes the group, backgrounds a long process and
        # exits. The group outlives the shell, and its id is the only handle to
        # what is left -- which is exactly why the worker remembers it.
        shell = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"],
            start_new_session=True, stdout=subprocess.PIPE,
        )
        leftover = int(shell.stdout.read().strip())
        self.addCleanup(self._reap, leftover)
        self.assertEqual(shell.wait(timeout=5), 0)

        groups.record(shell.pid)
        killed, unreaped = groups.kill_all(settle=1.0)
        self.assertEqual((killed, unreaped), (1, []), "the orphaned tree must be killed")

    @staticmethod
    def _reap(pid: int) -> None:
        """Best-effort cleanup for a leaked process in a failing test."""
        try:
            os.kill(pid, 9)
        except OSError:
            pass

    def test_the_workers_own_group_is_never_tracked(self):
        from agent.desktop_local.worker import _ProcessGroups

        groups = _ProcessGroups()
        # Killing our own group would kill the worker from inside its own
        # handler, before it can answer the caller.
        groups.record(os.getpid())
        groups.record(0)
        self.assertEqual(groups.kill_all(), (0, []))

    def test_a_group_that_is_already_gone_is_not_reported_as_unreaped(self):
        from agent.desktop_local.worker import _ProcessGroups

        if os.name != "posix":  # pragma: no cover - the desktop worker is posix
            self.skipTest("process groups are a posix concept")
        groups = _ProcessGroups()
        child = subprocess.Popen(["true"], start_new_session=True)
        child.wait(timeout=5)
        groups.record(child.pid)
        killed, unreaped = groups.kill_all(settle=0.05)
        self.assertEqual((killed, unreaped), (0, []))
    def test_a_group_that_cannot_be_killed_is_reported(self):
        from agent.desktop_local.worker import _ProcessGroups

        if os.name != "posix":  # pragma: no cover - the desktop worker is posix
            self.skipTest("process groups are a posix concept")
        groups = _ProcessGroups()
        # A group whose members are not our children stands in for the real case
        # (a command that backgrounds a process and exits): killpg reports
        # success member-by-member, so the group must be *checked*, not assumed.
        other = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(other.kill)
        self.addCleanup(other.wait)
        groups.record(other.pid)
        with mock.patch("os.killpg") as killpg:
            killed, unreaped = groups.kill_all(settle=0.1)
        self.assertTrue(killpg.called)
        self.assertEqual(killed, 0)
        self.assertEqual(unreaped, [other.pid])

    def test_a_stop_signal_kills_the_command_trees_and_exits(self):
        from agent.desktop_local import worker as worker_mod

        worker = mock.Mock()
        handler = _capture_signal_handler(worker_mod, worker)
        with mock.patch.object(worker_mod.os, "_exit") as hard_exit:
            handler(15, None)
        self.assertEqual(worker.process_groups.kill_all.call_count, 1,
                         "the command trees are killed before the exit")
        # The shell convention, and an exit that cannot be reported as an ignored
        # exception when the signal lands during interpreter shutdown.
        hard_exit.assert_called_once_with(143)

    def test_a_stop_signal_never_raises(self):
        """Nothing escapes the handler, on any stream or hook."""
        from agent.desktop_local import worker as worker_mod

        worker = mock.Mock()
        handler = _capture_signal_handler(worker_mod, worker)
        for signum, code in ((15, 143), (2, 130), (1, 129)):
            with mock.patch.object(worker_mod.os, "_exit") as hard_exit:
                handler(signum, None)
            hard_exit.assert_called_once_with(code)

    def test_a_stop_signal_survives_a_closed_stream(self):
        """Flushing a stream that is already gone must not change the exit."""
        from agent.desktop_local import worker as worker_mod

        worker = mock.Mock()
        handler = _capture_signal_handler(worker_mod, worker)
        broken = mock.Mock()
        broken.flush.side_effect = ValueError("I/O operation on closed file")
        with mock.patch.object(worker_mod.sys, "stdout", broken):
            with mock.patch.object(worker_mod.os, "_exit") as hard_exit:
                handler(15, None)
        hard_exit.assert_called_once_with(143)

    def test_a_background_job_reports_its_process_group(self):
        from agent.desktop_local.worker import _ProcessGroups
        from agent.tools.bash import background as background_mod

        if os.name != "posix":  # pragma: no cover - the desktop worker is posix
            self.skipTest("process groups are a posix concept")
        groups = _ProcessGroups()
        background_mod.set_process_group_hook(groups.record)
        self.addCleanup(background_mod.set_process_group_hook, None)
        with tempfile.TemporaryDirectory(prefix="desk-bg-") as tmp:
            job = background_mod.start("sleep 30", tmp, dict(os.environ))
            self.addCleanup(background_mod.kill, job)
            # A job spawns its own session too, so without this a cancelled run
            # leaves whatever the job started behind.
            self.assertIn(job, background_mod._jobs)
            pgid = background_mod._jobs[job].process.pid
            self.assertIn(pgid, groups._groups)

    def test_the_worker_installs_the_background_hook_when_it_builds_tools(self):
        from agent.desktop_local import worker as worker_mod
        from agent.tools.bash import background as background_mod

        with tempfile.TemporaryDirectory(prefix="desk-hook-") as tmp:
            worker = worker_mod.LocalToolWorker()
            home = worker_mod._Home(tmp, tmp)
            worker._build_tools(home)
            self.addCleanup(background_mod.set_process_group_hook, None)
            self.assertIsNotNone(background_mod._process_group_hook)

    def test_a_long_session_does_not_accumulate_dead_group_ids(self):
        from agent.desktop_local.worker import _ProcessGroups

        if os.name != "posix":  # pragma: no cover - the desktop worker is posix
            self.skipTest("process groups are a posix concept")
        groups = _ProcessGroups()
        first = subprocess.Popen(["true"], start_new_session=True)
        first.wait(timeout=5)
        groups.record(first.pid)
        second = subprocess.Popen(["true"], start_new_session=True)
        second.wait(timeout=5)
        groups.record(second.pid)
        self.assertNotIn(first.pid, groups._groups)
        self.assertIn(second.pid, groups._groups)


class WorkerProcessTests(unittest.TestCase):
    """A real worker process in a real project directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="desk-worker-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "project")
        os.makedirs(self.root)
        Path(self.root, "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        self.worker = _WorkerProcess(self.root)
        self.addCleanup(self.worker.close)

    def test_hello_reports_the_project_and_the_tool_set(self):
        reply = self.worker.hello()
        self.assertEqual(reply["status"], "success", reply)
        self.assertEqual(reply["root"], os.path.realpath(self.root))
        self.assertEqual(sorted(reply["tools"]),
                         ["bash", "edit", "ls", "read", "search_files", "write"])
        self.assertGreater(reply["pid"], 0)

    def test_a_frame_before_hello_is_refused(self):
        reply = self.worker.run_tool("read", path="notes.txt")
        self.assertEqual(reply["status"], "error")
        self.assertEqual(reply["error"]["code"], "not_hello")

    def test_an_unknown_tool_is_refused_by_name(self):
        self.worker.hello()
        for name in ("subagent", "scheduler", "python", "os.system", ""):
            reply = self.worker.run_tool(name, path="notes.txt")
            self.assertEqual(reply["status"], "error", reply)
            self.assertIn(reply["error"]["code"], ("unknown_tool", "invalid_request"))

    def test_an_unknown_op_is_refused(self):
        self.worker.hello()
        reply = self.worker.call("evaluate", expression="1+1")
        self.assertEqual(reply["error"]["code"], "unknown_op")

    def test_a_bad_frame_does_not_kill_the_worker(self):
        self.worker.hello()
        self.worker.proc.stdin.write("this is not json\n")
        self.worker.proc.stdin.flush()
        bad = self.worker.recv()
        self.assertEqual(bad["status"], "error")
        # ...and the worker still answers a good frame afterwards.
        reply = self.worker.run_tool("read", path="notes.txt")
        self.assertEqual(reply["status"], "success", reply)

    def test_read_runs_the_original_tool(self):
        self.worker.hello()
        reply = self.worker.run_tool("read", path="notes.txt")
        self.assertEqual(reply["status"], "success", reply)
        self.assertIn("alpha", json.dumps(reply["result"]))

    def test_paths_are_relative_to_the_project(self):
        self.worker.hello()
        self.worker.run_tool("write", path="sub/new.txt", content="hi")
        self.assertTrue(Path(self.root, "sub", "new.txt").is_file())

    def test_bash_runs_in_the_project_directory(self):
        self.worker.hello()
        reply = self.worker.run_tool("bash", command="pwd")
        self.assertEqual(reply["status"], "success", reply)
        self.assertIn(os.path.realpath(self.root), json.dumps(reply["result"]))

    def test_bash_actually_changes_the_disk(self):
        """Only the original tool writes a real file with real permissions."""
        self.worker.hello()
        self.worker.run_tool("bash", command="printf 'made\\n' > made.txt")
        made = Path(self.root, "made.txt")
        self.assertTrue(made.is_file(), "the shell did not really run")
        self.assertEqual(made.read_text(encoding="utf-8"), "made\n")

    def test_ls_and_search_files_see_the_project(self):
        self.worker.hello()
        listing = self.worker.run_tool("ls", path=".")
        self.assertIn("notes.txt", json.dumps(listing["result"]))
        found = self.worker.run_tool("search_files", pattern="beta", path=".")
        self.assertIn("notes.txt", json.dumps(found["result"]))

    def test_edit_uses_the_original_tool(self):
        self.worker.hello()
        reply = self.worker.run_tool(
            "edit", path="notes.txt", oldText="beta", newText="gamma")
        self.assertEqual(reply["status"], "success", reply)
        self.assertEqual(Path(self.root, "notes.txt").read_text(encoding="utf-8"),
                         "alpha\ngamma\n")

    def test_a_second_frame_while_busy_is_refused_not_queued(self):
        """Two writers on one project is what the claim exists to prevent."""
        self.worker.hello()
        slow_id = self.worker.send(
            op="call", tool="bash", arguments={"command": "sleep 2; echo done"})
        # Wait until the call is definitely inside the tool before competing.
        import time
        time.sleep(0.6)
        refused_id = self.worker.send(op="call", tool="read",
                                     arguments={"path": "notes.txt"})
        replies = {}
        for _ in range(2):
            reply = self.worker.recv()
            replies[reply["id"]] = reply
        self.assertIn(slow_id, replies, replies)
        self.assertIn(refused_id, replies, replies)
        self.assertEqual(replies[refused_id]["status"], "error", replies)
        self.assertEqual(replies[refused_id]["error"]["code"], "invalid_request")
        self.assertEqual(replies[slow_id]["status"], "success", replies)
        # The reader stayed free while the tool ran: a ping answered in between.
        self.assertEqual(self.worker.call("ping")["pong"], True)

    def test_describe_returns_the_real_schemas(self):
        self.worker.hello()
        reply = self.worker.call("describe")
        self.assertEqual(reply["status"], "success", reply)
        by_name = {t["name"]: t for t in reply["tools"]}
        self.assertIn("bash", by_name)
        self.assertIn("command", by_name["bash"]["parameters"]["properties"])
        self.assertIn("path", by_name["read"]["parameters"]["properties"])

    def test_a_path_outside_the_project_is_refused(self):
        """A mistake becomes a clear refusal, not a permission error to retry."""
        self.worker.hello()
        outside = os.path.join(self._tmp.name, "outside.txt")
        Path(outside).write_text("secret", encoding="utf-8")
        for tool, field in (("read", "path"), ("write", "path"),
                            ("ls", "path")):
            reply = self.worker.run_tool(tool, **{field: outside})
            self.assertEqual(reply["status"], "error", (tool, reply))
            self.assertEqual(reply["error"]["code"], "path_outside_project")
        self.assertFalse(Path(outside).with_suffix(".bak").exists())

    def test_a_traversal_out_of_the_project_is_refused(self):
        self.worker.hello()
        reply = self.worker.run_tool("read", path="../outside.txt")
        self.assertEqual(reply["status"], "error", reply)
        self.assertEqual(reply["error"]["code"], "path_outside_project")

    def test_a_literal_dotdot_in_a_filename_is_still_allowed(self):
        """The guard is about locations, not about the characters in a name."""
        self.worker.hello()
        Path(self.root, "a..b.txt").write_text("ok", encoding="utf-8")
        reply = self.worker.run_tool("read", path="a..b.txt")
        self.assertEqual(reply["status"], "success", reply)

    def test_bash_commands_are_not_string_filtered(self):
        """The sandbox decides what a shell may touch, not a substring check.

        Refusing a command merely because it names an absolute path would break
        ordinary work (``ls /usr/bin``, ``cat /etc/hosts``) while catching
        nothing that ``$(cat ...)`` would not slip past. The guard therefore does
        not inspect ``command`` at all -- the platform sandbox is the boundary.
        """
        self.worker.hello()
        reply = self.worker.run_tool("bash", command="ls /usr >/dev/null; echo ok")
        self.assertEqual(reply["status"], "success", reply)
        self.assertIn("ok", json.dumps(reply["result"]))

    def test_a_hello_for_another_root_is_refused_when_the_launcher_pinned_one(self):
        """The frame cannot widen what the launcher confined."""
        other = os.path.join(self._tmp.name, "elsewhere")
        os.makedirs(other, exist_ok=True)
        worker = _WorkerProcess(self.root, env=_worker_env(
            {"COW_DESKTOP_WORKER_ROOT": os.path.realpath(self.root)}))
        self.addCleanup(worker.close)
        reply = worker.hello(root=other)
        self.assertEqual(reply["status"], "error", reply)
        self.assertEqual(reply["error"]["code"], "invalid_request")
        # The pinned root still works.
        reply = worker.hello(root=self.root)
        self.assertEqual(reply["status"], "success", reply)

    def test_shutdown_stops_the_loop(self):
        self.worker.hello()
        reply = self.worker.call("shutdown")
        self.assertTrue(reply["stopping"])
        self.worker.proc.stdin.close()
        self.assertEqual(self.worker.proc.wait(timeout=10), 0)

    def test_eof_exits_cleanly(self):
        self.worker.hello()
        self.worker.proc.stdin.close()
        self.assertEqual(self.worker.proc.wait(timeout=10), 0)

    def test_no_unsolicited_output_pollutes_stdout(self):
        """stdout is the pipe: one line in, one line out."""
        self.worker.hello()
        reply = self.worker.run_tool("ls", path=".")
        self.assertEqual(reply["status"], "success")
        self.assertEqual(reply["id"], "req-2")


class WorkerModuleTests(unittest.TestCase):
    """Unit-level properties that do not need a process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="desk-worker-unit-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "proj")
        os.makedirs(self.root)

    def test_it_never_calls_chdir(self):
        """A process-wide chdir would be a race across concurrent tool calls."""
        import ast

        tree = ast.parse(
            (REPO_ROOT / "agent" / "desktop_local" / "worker.py").read_text(
                encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(
                    func.value, ast.Name):
                called.add(f"{func.value.id}.{func.attr}")
        self.assertNotIn("os.chdir", called, f"calls found: {sorted(called)}")

    def test_path_guard_accepts_the_project_and_its_temp_dir(self):
        from agent.desktop_local.worker import _Home

        temp = os.path.join(self.root, "tmp")
        os.makedirs(temp, exist_ok=True)
        home = _Home(self.root, temp)
        self.assertTrue(home.allows(self.root))
        self.assertTrue(home.allows(os.path.join(self.root, "a", "b.txt")))
        self.assertTrue(home.allows(temp))
        self.assertFalse(home.allows(os.path.dirname(self.root)))
        self.assertFalse(home.allows("/etc/passwd"))

    def test_path_guard_resolves_symlinks(self):
        from agent.desktop_local.worker import _Home

        outside = os.path.join(self._tmp.name, "outside")
        os.makedirs(outside, exist_ok=True)
        link = os.path.join(self.root, "escape")
        os.symlink(outside, link)
        home = _Home(self.root, os.path.join(self.root, ".tmp"))
        self.assertFalse(home.allows(os.path.join(link, "file.txt")))

    def test_main_refuses_command_line_arguments(self):
        from agent.desktop_local import worker

        self.assertEqual(worker.main(["/some/root"]), 2)

    def test_the_probe_reports_what_the_launcher_must_be_able_to_read(self):
        """The desktop's read list is derived from this, so the keys are a contract.

        A packaged install cannot answer ``python -c '<probe>'`` -- the worker is
        the frozen binary itself -- so the probe has to be part of the worker's
        own entry. Both halves must keep working: the venv form a source checkout
        uses, and the frozen form the bundle uses.
        """
        from agent.desktop_local import worker

        report = worker.interpreter_report()
        for key in ("realPath", "basePrefix", "stdlib", "purelib", "frozen",
                    "bundleRoot"):
            self.assertIn(key, report)
        # `realPath` is the one the read list cannot be derived without; a probe
        # that answered without it would leave the sandbox unable to start.
        self.assertTrue(report["realPath"])
        self.assertFalse(report["frozen"])

    def test_the_probe_runs_as_a_real_process_and_prints_only_json(self):
        proc = subprocess.run(
            [PYTHON, "-m", "agent.desktop_local.worker", "--probe"],
            cwd=str(REPO_ROOT), env=_worker_env(), capture_output=True, text=True,
            timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, proc.stdout)
        report = json.loads(lines[0])
        self.assertEqual(os.path.realpath(report["realPath"]), os.path.realpath(PYTHON))

    def test_the_frozen_entry_flag_reaches_the_worker(self):
        """``app.py --desktop-local-worker`` is how an installed app starts it.

        The packaged bundle is one executable, so `python -m agent...` is not
        available there; the flag has to dispatch *before* app.py's channel and
        plugin imports, or a confined worker would load the whole server.
        """
        from agent.desktop_local import worker

        proc = subprocess.run(
            [PYTHON, "app.py", worker.WORKER_FLAG, worker.PROBE_FLAG],
            cwd=str(REPO_ROOT), env=_worker_env(), capture_output=True, text=True,
            timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(report["realPath"], worker.interpreter_report()["realPath"])

    def test_the_dispatch_is_not_reachable_by_importing_app(self):
        """`import app` must still return the module, not a worker that exits.

        The dispatch is guarded by ``__name__ == "__main__"`` for exactly this
        reason: tooling and tests that import ``app`` would otherwise be killed
        by a stray flag in their own argv. Checked by running it, not by reading
        the source -- a guard that is present but in the wrong place would still
        look right in a text search.
        """
        code = (
            "import sys;"
            "sys.argv = ['app.py', '--desktop-local-worker', '--probe'];"
            "import app;"
            "print('IMPORTED-OK')"
        )
        proc = subprocess.run(
            [PYTHON, "-c", code], cwd=str(REPO_ROOT), env=_worker_env(),
            capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("IMPORTED-OK", proc.stdout)

    def test_the_dispatch_precedes_the_channel_imports(self):
        """Ordering is what keeps the confined worker small.

        A frozen worker dispatched *after* ``from channel import channel_factory``
        would load the whole server (and every channel's dependencies) inside the
        sandbox on each script call. This is the one property the behaviour above
        cannot observe, so it is asserted on the source.
        """
        source = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
        dispatch = source.index('"--desktop-local-worker" in sys.argv')
        self.assertLess(dispatch, source.index("from channel import channel_factory"),
                        "the dispatch must precede the channel imports")
        self.assertIn('__name__ == "__main__"',
                      source[source.rindex('__name__ == "__main__"', 0, dispatch):dispatch + 1])

    def test_the_worker_does_not_import_the_identity_layer(self):
        """Nothing in the worker's import graph may reach auth/或 config."""
        code = (
            "import sys; import agent.desktop_local.worker; "
            "bad = [m for m in sys.modules if m.split('.')[0] in "
            "('auth', 'channel', 'integrations')]; "
            "print('|'.join(sorted(bad)))"
        )
        proc = subprocess.run(
            [PYTHON, "-c", code], cwd=str(REPO_ROOT), env=_worker_env(),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)


if __name__ == "__main__":
    unittest.main()
