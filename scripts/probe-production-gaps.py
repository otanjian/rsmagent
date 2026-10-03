#!/usr/bin/env python3
"""Reproduce the historical unassembled tool baseline using synthetic data.

Exit 1 means a gap remains; exit 0 only covers these narrow tool-level probes,
not real identity/HTTP, Linux isolation or production acceptance.
"""

import base64
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-baseline', action='store_true', required=True,
                        help='explicitly bypass the production startup assembly; not a current-candidate test')
    parser.parse_args()
    if os.name != "posix":
        print("This probe requires a POSIX host; Windows testing is deferred.")
        return 2
    with tempfile.TemporaryDirectory(prefix="rsmagent-production-probe-") as temp:
        root = Path(temp).resolve()
        home, workspace, data, other = (root / name for name in ("home", "workspace", "data", "other"))
        for path in (home, workspace, data, other):
            path.mkdir()
        safe_env = {
            "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
            "HOME": str(home), "COW_DATA_DIR": str(data), "LANG": "en_US.UTF-8",
            "COW_CREDENTIAL_MASTER_KEY": "SYNTHETIC_DEPLOYMENT_KEY",
        }
        with patch.dict(os.environ, safe_env, clear=True):
            from agent.permission.isolation import _Boundary, _check_bash
            from agent.tools.bash.bash import Bash
            from agent.tools.bash import background
            from auth.ratelimit import LoginRateLimiter
            from cli.commands.backup import create_backup_archive
            from common.log import logger
            logger.setLevel("WARNING")
            secret = other / "fixture.txt"
            secret.write_text("SYNTHETIC_OTHER_TENANT_DATA", encoding="utf-8")
            boundary = _Boundary(read_roots=[str(workspace)], write_roots=[str(workspace)],
                                 blocked=[str(other), str(data), str(home)],
                                 engineering=str(workspace), tenant_id="synthetic-tenant")
            tool = Bash({"cwd": str(workspace)})
            def python_command(source):
                return shlex.quote(sys.executable) + " -c " + shlex.quote(source)
            encoded = base64.b64encode(str(secret).encode()).decode()
            command = python_command(
                "import base64; print(open(base64.b64decode(" + repr(encoded) + ").decode()).read())")
            direct = _check_bash(boundary, {"command": "cat " + shlex.quote(str(secret))}, str(workspace))
            computed = _check_bash(boundary, {"command": command}, str(workspace))
            executed = tool.execute({"command": command}) if computed.allowed else None
            issues = {
                "direct_cross_tenant_path_denied": not direct.allowed,
                "computed_path_gap": computed.allowed and executed is not None and
                    "SYNTHETIC_OTHER_TENANT_DATA" in str(executed.result),
            }
            env_command = python_command("import os; print(os.environ.get('COW_CREDENTIAL_MASTER_KEY', ''))")
            issues["foreground_environment_gap"] = "SYNTHETIC_DEPLOYMENT_KEY" in str(
                tool.execute({"command": env_command}).result)
            try:
                started = tool.execute({"command": env_command, "run_in_background": True})
                job = started.result["bash_id"]
                output = ""
                deadline = time.monotonic() + 10
                while True:
                    result = tool.execute({"bash_id": job}).result
                    output += result["output"]
                    if not result["running"]:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("synthetic background probe timed out")
                    time.sleep(0.05)
                issues["background_environment_gap"] = "SYNTHETIC_DEPLOYMENT_KEY" in output
            finally:
                background.reset()
            now = [1000]
            limiter = LoginRateLimiter(window_seconds=10, max_capacity=2, clock=lambda: now[0])
            for key in ("first", "second"):
                limiter.record_failure(key, key)
            now[0] += 100
            issues["expired_capacity_gap"] = not limiter.check("new", "new").allowed
            (data / "config.json").write_text(json.dumps({"agent_workspace": str(workspace)}))
            (data / "identity.db").write_bytes(b"SYNTHETIC_IDENTITY_DATA")
            archive = root / "workspace-backup.zip"
            create_backup_archive(archive, data, workspace)
            with zipfile.ZipFile(archive) as bundle:
                issues["workspace_backup_omits_identity"] = not any(
                    name.endswith("identity.db") for name in bundle.namelist())
            print(json.dumps({"scope": "historical unassembled tools (launcher NOT installed); not current production acceptance", **issues}, indent=2))
            # The old workspace backup is deliberately not a full instance mode.
            return int(any(value for name, value in issues.items() if name != "direct_cross_tenant_path_denied")
                       or not issues["direct_cross_tenant_path_denied"])


if __name__ == "__main__":
    raise SystemExit(main())
