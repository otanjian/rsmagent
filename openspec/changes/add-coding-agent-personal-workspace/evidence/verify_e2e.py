"""Live end-to-end verification for change add-coding-agent-personal-workspace.

Tasks 5.1, 5.2, 5.3 and the live half of 4.2, driven against the real OpenCode
service on 127.0.0.1:4096 with the platform's own client code. Temporary P/U only
(the temporary tree is removed at the end).

Prerequisites:

* the RSMCode OpenCode service is running on ``127.0.0.1:4096`` (``bun run --cwd
  packages/opencode src/index.ts serve``) and answers with the platform's own
  credential;
* ``RSM_OPENCODE_PASSWORD`` is exported (the platform and the exerciser share it);
* run it with the platform repository importable, e.g. from that repository::

      python openspec/changes/add-coding-agent-personal-workspace/evidence/verify_e2e.py

Exit code is 0 only when every check passes. Results are recorded in
``evidence/platform-slice.md`` section 11.6.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

sys.path.insert(0, r"C:\rdai\rsmagent")

from agent.coding import CodingError, CodingSettings
from agent.coding.files import CodingRemoteFiles, RemoteSource, output_line, output_relative_dir
from agent.coding.opencode import OpenCodeClient
from channel.web.fork.handlers.workspace import (
    coding_remote_upload_dir,
    write_remote_attachment,
)

API = "http://127.0.0.1:4096"
PASSWORD = os.environ["RSM_OPENCODE_PASSWORD"]
AUTH = ("opencode", PASSWORD)

FAILURES = []


def check(label, condition, detail=""):
    ok = bool(condition)
    print("%-6s %s%s" % ("PASS" if ok else "FAIL", label, ("  -- " + detail) if detail else ""))
    if not ok:
        FAILURES.append(label)


def section(title):
    print("\n=== %s ===" % title)


settings = CodingSettings(
    enabled=True,
    service_id="default",
    api_url=API,
    web_url="https://code.example.com",
    username="opencode",
    password=PASSWORD,
    personal_workspace_enabled=True,
)
client = OpenCodeClient(settings)

root = Path(tempfile.mkdtemp(prefix="rsm_e2e_"))
P = root / "P"
ACCOUNT = "LiveE2E"
U = P / "user" / ACCOUNT
SESSION = "sess-live-1"
REQUEST = "req-live-9"

(P / ".opencode" / "skills" / "p-marker").mkdir(parents=True)
(P / ".opencode" / "skills" / "p-marker" / "SKILL.md").write_text(
    "---\nname: p-marker\ndescription: marker skill from the project root P\n---\nbody\n",
    encoding="utf-8")
(P / "opencode.json").write_text(
    json.dumps({"$schema": "https://opencode.ai/config.json"}), encoding="utf-8")
subprocess.run(["git", "init", "-q", str(P)], check=False)

wid = "ocws_live_e2e_%d" % int(time.time())
print("P       =", P)
print("U       =", U)
print("wid     =", wid)

try:
    # ---- prepare (task 2.2 surface, re-proven here as the live precondition) --
    section("prepare on the live host")
    prepared = client.prepare_workspace(str(P), str(U), wid)
    print(json.dumps(prepared, ensure_ascii=False))
    check("prepare created U", prepared.get("state") == "ready" and prepared.get("created") is True)
    again = client.prepare_workspace(str(P), str(U), wid)
    check("prepare is idempotent on retry", again.get("created") is False)
    check("U exists on the host", U.is_dir())
    exclude = (P / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    check("git exclude carries user/", "user/" in exclude,
          " | ".join(exclude.strip().splitlines()[-2:]))

    files = CodingRemoteFiles(client, str(U), workspace_id=wid)
    source = RemoteSource(str(U), wid)

    # ---------------------------- 5.1 ----------------------------------------
    section("5.1 panel + composer upload -> list -> search -> read -> resolve -> download")
    attachment_dir = coding_remote_upload_dir(SESSION, "att-1")
    payload = "报价单 v1\n金额 1200\n".encode("utf-8")
    written = write_remote_attachment(files, attachment_dir + "/quote.txt", payload)
    print("uploaded:", json.dumps(written, ensure_ascii=False))
    check("composer attachment landed under uploads/<session>/<attachment>",
          written["path"] == attachment_dir + "/quote.txt")

    retry = write_remote_attachment(files, attachment_dir + "/quote.txt", payload)
    check("an identical retry does not write a second copy", retry["path"] == written["path"])
    try:
        write_remote_attachment(files, attachment_dir + "/quote.txt", b"different")
        check("different bytes under the same id are refused", False)
    except Exception as exc:  # noqa: BLE001
        check("different bytes under the same id are refused",
              "different content" in str(exc), type(exc).__name__)

    tree = files.list_dir("")
    names = [e["path"] for e in tree["entries"]]
    print("tree root:", names)
    check("the panel lists the uploads container", "uploads" in names)
    check("the panel exposes no platform-local path",
          all("abs_path" not in e and "raw_url" not in e for e in tree["entries"]))
    check("directories sort first", names.index("uploads") == 0, str(names))

    nested = files.list_dir(attachment_dir)
    check("nested listing reaches the attachment",
          [e["path"] for e in nested["entries"]] == [written["path"]])

    # The remote search index for a location is built asynchronously upstream
    # (its first answer after a cold location can be empty), so poll briefly --
    # the panel's own @ search is user-paced and self-corrects the same way.
    found = {"results": []}
    for attempt in range(15):
        found = files.search("quote", limit=20)
        if found["results"]:
            break
        time.sleep(0.2)
    print("search raw:", [e["path"] for e in found["results"]], "attempts:", attempt + 1)
    check("search finds it by name",
          any(e["path"].endswith("/quote.txt") for e in found["results"]))

    text = files.read_text(written["path"])
    check("read returns the stored text", text["content"] == payload.decode("utf-8"))
    check("read is not editable while the source is read-only", text["editable"] is False)

    reference = files.reference(written["path"])
    print("reference line:", reference["line"])
    check("resolve names the relative id for the turn",
          written["path"] in reference["line"] and reference["is_dir"] is False)

    blob, content_type = files.read_bytes(written["path"])
    check("download returns the exact bytes", blob == payload, content_type)
    check("download sha256 matches",
          hashlib.sha256(blob).hexdigest() == hashlib.sha256(payload).hexdigest())

    for bad in ("../escape.txt", "C:/Windows/system32/drivers/etc/hosts", "/abs.txt",
                "\\\\server\\share\\x"):
        try:
            files.read_text(bad)
            check("escaping reference refused: %s" % bad, False)
        except (ValueError, FileNotFoundError):
            check("escaping reference refused: %s" % bad, True)

    # generated output: the platform only *names* the folder; the session's own
    # tools write it. Here we land the same bytes the tool would.
    output_dir = output_relative_dir(SESSION, REQUEST)
    output_marker = output_line(SESSION, REQUEST)
    generated = ("# 季度报价单\n\n由本会话生成\n").encode("utf-8")
    landed = write_remote_attachment(files, output_dir + "/report.md", generated)
    print("output marker:", output_marker)
    print("output landed:", json.dumps(landed, ensure_ascii=False))
    check("the turn's output marker names the folder the file landed in",
          output_marker.endswith(output_dir + "]"))
    check("generated file is under outputs/<session>/<turn>",
          landed["path"] == "outputs/%s/%s/report.md" % (SESSION, REQUEST))
    listed_outputs = [e["name"] for e in files.list_dir("outputs/%s" % SESSION)["entries"]]
    check("panel lists the turn's output folder", REQUEST in listed_outputs, str(listed_outputs))
    back, _ = files.read_bytes(landed["path"])
    check("generated bytes read back from U", back == generated)

    # ---------------------------- 5.2 ----------------------------------------
    # NOTE: the 128 MiB payload (5.2) is landed *after* the session-deletion check
    # (5.3) on purpose: an OpenCode session takes a working-tree snapshot, and a
    # directory holding 128 MiB makes that snapshot exceed the client's 10s read
    # budget (the retry then sees 404 and finishes idempotently).
    section("5.3 session delete keeps files; the binding is not moved")
    session = requests.post(API + "/api/session", auth=AUTH, timeout=30,
                            json={"location": {"directory": str(U)}})
    session.raise_for_status()
    created = session.json()
    print("session response keys:", list(created))
    record = created.get("data") or created.get("session") or created
    sid = record["id"]
    in_session = write_remote_attachment(files, "uploads/%s/keep.txt" % sid, b"keep me")
    check("session created in the account directory",
          record["location"]["directory"] == str(U), sid)
    for attempt in range(2):
        started = time.time()
        try:
            OpenCodeClient(settings, timeout=(5, 90)).delete_session(sid, str(U))
            print("delete took %.2fs" % (time.time() - started))
            break
        except CodingError as exc:
            print("delete raised after %.2fs: %s" % (time.time() - started, exc))
            if attempt == 1:
                raise
            time.sleep(2)
    gone = requests.get(API + "/api/session/%s" % sid, auth=AUTH, timeout=20)
    check("the upstream session is gone", gone.status_code >= 400, str(gone.status_code))
    check("U still exists after the session is deleted", U.is_dir())
    check("the deleted session's file is still there",
          files.exists(in_session["path"]), in_session["path"])
    check("the binding still resolves to the same U after a second prepare",
          client.prepare_workspace(str(P), str(U), wid).get("directory") == str(U))
    check("the re-prepare kept the file it already had",
          files.exists(in_session["path"]), in_session["path"])

    # ---------------------------- 5.2 ----------------------------------------
    section("5.2 near-limit payload upload / readback")
    # The platform's per-file ceiling is 200 MiB (WS_UPLOAD_MAX_BYTES) and the
    # remote's backstop is 256 MiB, so 128 MiB is a real near-limit transfer: it
    # is read in 1 MiB chunks under the ceiling, base64'd, decoded on the host
    # and published atomically. The over-ceiling *refusal* cannot be driven from
    # here (it happens in the platform's part reader, before any request) and is
    # pinned by tests/test_coding_remote_workspace.py::test_upload_remote_enforces_the_size_ceiling.
    size = 128 * 1024 * 1024
    big = bytes(range(256)) * (size // 256)
    big_dir = coding_remote_upload_dir(SESSION, "att-big")
    started = time.time()
    stored = write_remote_attachment(files, big_dir + "/big.bin", big)
    uploaded = time.time() - started
    check("128 MiB upload reported the stored size", stored["size"] == size, str(stored["size"]))
    started = time.time()
    readback, _ = files.read_bytes(stored["path"])
    read_time = time.time() - started
    check("128 MiB readback matches exactly",
          len(readback) == size and hashlib.sha256(readback).digest() ==
          hashlib.sha256(big).digest())
    print("timings: upload %.2fs, readback %.2fs" % (uploaded, read_time))
    check("the host file really is 128 MiB", (U / stored["path"]).stat().st_size == size)

    # ---------------------------- 4.2 (live) ---------------------------------
    section("4.2 config discovery resolves P from U; tools and permissions kept")

    def get(route, directory):
        return requests.get(API + route, auth=AUTH, timeout=30,
                            params={"location[directory]": str(directory)})

    loc_u = get("/api/location", U).json()
    print("location(U):", json.dumps(loc_u["project"], ensure_ascii=False))
    check("U is inside the project rooted at P",
          os.path.normcase(loc_u["project"]["directory"]) == os.path.normcase(str(P)))

    skills_u = [s["name"] for s in get("/api/skill", U).json()["data"]]
    skills_p = [s["name"] for s in get("/api/skill", P).json()["data"]]
    print("skills(U):", skills_u)
    check("the project skill written at P is discovered from U", "p-marker" in skills_u)
    check("U and P see the same skills", skills_u == skills_p)

    config_u = get("/config", U).json()
    config_p = get("/config", P).json()
    check("the effective config seen from U equals the one at P", config_u == config_p)

    tools_u = get("/experimental/tool/ids", U).json()
    tools_p = get("/experimental/tool/ids", P).json()
    print("tools(U):", tools_u)
    check("the tool set is preserved in the account directory",
          tools_u == tools_p and "bash" in tools_u and "write" in tools_u)

    agents_u = get("/api/agent", U).json()["data"]
    build = [a for a in agents_u if a["id"] == "build"]
    check("agents and their permission rules are preserved",
          bool(build) and any(p["action"] == "*" for p in build[0]["permissions"]))

finally:
    shutil.rmtree(root, ignore_errors=True)

print("\n=== summary ===")
if FAILURES:
    print("FAILED: %d" % len(FAILURES))
    for item in FAILURES:
        print(" -", item)
    sys.exit(1)
print("all checks passed")
