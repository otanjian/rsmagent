"""Apply 使用说明 + 建议问题 from the copy doc onto the test15 tenant roster.

Dry-run by default: prints coverage and whether a byte-identical rewrite is
possible. Pass --apply to write (backup + atomic replace, same layout as
``agent/team.py::write``).
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import shutil
from datetime import datetime

DOC = r"C:\rdai\rsmagent\docs\agent-onboarding-copy.md"
ROSTER = r"C:\rdai\cow\agents\team.json"
DB = r"C:\rdai\rsmagent\identity.db"
TENANT = "tnt_EA3qM-lHPLD8ZPwW"  # code=test15

APPLY = "--apply" in sys.argv


def parse_doc(path):
    entries = {}
    cur = None
    for ln in open(path, encoding="utf-8").read().splitlines():
        if ln.startswith("### "):
            # Heading shape: ``### 名称（`id`）``; some names contain full-width
            # parens themselves, so drop only the trailing id parenthetical.
            name = re.sub(r"（`[^）]*`）\s*$", "", ln[4:].strip()).strip()
            cur = {"name": name, "usage": None, "qs": []}
            entries[name] = cur
        elif cur is not None:
            m = re.match(r"^- \*\*使用说明\*\*：(.*)$", ln)
            if m:
                cur["usage"] = m.group(1).strip()
            m2 = re.match(r"^\s+\d+\.\s+(.*)$", ln)
            if m2:
                cur["qs"].append(m2.group(1).strip())
    return entries


doc = parse_doc(DOC)

raw = open(ROSTER, "rb").read()
text = raw.decode("utf-8-sig")
data = json.loads(text)

# Round-trip check: does our dump reproduce the file we read?
round_trip = json.dumps(data, indent=4, ensure_ascii=False) + "\n"
print("round-trip identical to file:", round_trip == text)
print("BOM present:", raw[:3] == b"\xef\xbb\xbf")

c = sqlite3.connect(DB)
bound = [r[0] for r in c.execute(
    "select agent_id from agent_bindings where tenant_id=?", (TENANT,))]
print("test15 bound agents:", len(bound))

payload_for_compare = {k: data[k] for k in
                       ("agents", "default_agent_id", "channel_instances")
                       if k in data}

by_id = {a.get("id"): a for a in data["agents"]}
matched, unmatched, missing = [], [], []
for aid in bound:
    a = by_id.get(aid)
    if a is None:
        missing.append(aid)
        continue
    e = doc.get(a.get("name"))
    if e is None or not e["usage"] or len(e["qs"]) != 4:
        unmatched.append((aid, a.get("name")))
        continue
    matched.append((aid, a.get("name")))

print("matched:", len(matched))
print("unmatched (no doc entry):", len(unmatched))
for aid, name in unmatched:
    print("   -", aid, "|", name)
print("bound but absent from roster:", missing)

# Which doc entries are never used?
used = {n for _, n in matched}
print("\ndoc entries not applied:", sorted(set(doc) - used))

if APPLY:
    # Safety gate: prove a no-op rewrite reproduces the file byte-for-byte
    # (``team.write`` uses text mode, so CRLF translation is expected).
    probe_fd, probe = tempfile.mkstemp(prefix=".probe.", suffix=".tmp",
                                       dir=os.path.dirname(ROSTER))
    with os.fdopen(probe_fd, "w", encoding="utf-8") as fh:
        json.dump(payload_for_compare, fh, indent=4, ensure_ascii=False)
        fh.write("\n")
    same = open(probe, "rb").read() == raw
    os.unlink(probe)
    print("\nno-op rewrite byte-identical:", same)
    if not same:
        raise SystemExit("refusing to write: layout would not round-trip")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{ROSTER}.bak-onboarding-{stamp}"
    shutil.copy2(ROSTER, backup)
    print("backup ->", backup)

    changed = 0
    for aid, _name in matched:
        a = by_id[aid]
        e = doc[a["name"]]
        if a.get("usage_hint") != e["usage"]:
            a["usage_hint"] = e["usage"]
            changed += 1
        if list(a.get("suggested_questions") or []) != e["qs"]:
            a["suggested_questions"] = list(e["qs"])
            changed += 1

    payload = {k: data[k] for k in ("agents", "default_agent_id", "channel_instances")
               if k in data}
    fd, tmp = tempfile.mkstemp(prefix=".team.json.", suffix=".tmp",
                               dir=os.path.dirname(ROSTER))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=4, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, ROSTER)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print("fields written:", changed, "| agents updated:", len(matched))
