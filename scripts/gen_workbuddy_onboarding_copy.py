"""Generate the preset-side onboarding copy from the reviewed copy doc.

One-off: writes ``agent/presets/workbuddy/onboarding-copy.json`` (source of
truth for the 62 scenario profiles, keyed by slug) and merges the same copy
into the already-generated ``catalog.json`` so an install needs no rebuild.
"""
import json
import re
from pathlib import Path

ROOT = Path(r"C:\rdai\rsmagent")
DOC = ROOT / "docs" / "agent-onboarding-copy.md"
CATALOG = ROOT / "agent" / "presets" / "workbuddy" / "catalog.json"
OUT = ROOT / "agent" / "presets" / "workbuddy" / "onboarding-copy.json"

MAX_HINT = 200
MAX_QUESTION = 200
MAX_QUESTIONS = 4


def parse_doc(path):
    entries = {}
    cur = None
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.startswith("### "):
            name = re.sub(r"（`[^）]*`）\s*$", "", ln[4:].strip()).strip()
            cur = {"name": name, "usage_hint": None, "suggested_questions": []}
            entries[name] = cur
        elif cur is not None:
            m = re.match(r"^- \*\*使用说明\*\*：(.*)$", ln)
            if m:
                cur["usage_hint"] = m.group(1).strip()
            m2 = re.match(r"^\s+\d+\.\s+(.*)$", ln)
            if m2:
                cur["suggested_questions"].append(m2.group(1).strip())
    return entries


def validate(name, entry):
    hint = entry["usage_hint"]
    if not hint:
        raise SystemExit(f"{name}: missing 使用说明")
    if len(hint) > MAX_HINT:
        raise SystemExit(f"{name}: 使用说明 {len(hint)} chars > {MAX_HINT}")
    if " → " not in hint:
        raise SystemExit(f"{name}: 使用说明 has no ' → ' step separator")
    qs = entry["suggested_questions"]
    if len(qs) != MAX_QUESTIONS:
        raise SystemExit(f"{name}: {len(qs)} questions, expected {MAX_QUESTIONS}")
    for q in qs:
        if len(q) > MAX_QUESTION:
            raise SystemExit(f"{name}: question {len(q)} chars > {MAX_QUESTION}")
        # Mirrors agent/registry.clean_suggested_questions: a question is prose,
        # never a command or an @ routing mark.
        if q.startswith("/") or "@" in q:
            raise SystemExit(f"{name}: forbidden question {q!r}")


doc = parse_doc(DOC)
for name, entry in doc.items():
    validate(name, entry)

catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
scenarios = catalog["scenarios"]

payload = {}
missing = []
for item in scenarios:
    name = item["profile"]["name"]
    entry = doc.get(name)
    if entry is None:
        missing.append((item["slug"], name))
        continue
    payload[item["slug"]] = {
        "name": name,
        "usage_hint": entry["usage_hint"],
        "suggested_questions": list(entry["suggested_questions"]),
    }

print("catalog scenarios:", len(scenarios), "| with copy:", len(payload))
if missing:
    print("MISSING doc entries:")
    for slug, name in missing:
        print("   -", slug, "|", name)
    raise SystemExit("refusing to write an incomplete mapping")

used = {v["name"] for v in payload.values()}
print("doc entries not used by the workbuddy catalog:",
      sorted(set(doc) - used))

OUT.write_text(
    json.dumps({"version": 1, "source": "docs/agent-onboarding-copy.md",
                "note": "使用说明=应用步骤，步骤之间用“ → ”连接；建议问题最多四条，"
                        "由 scripts/build_workbuddy_presets.py 写入场景 profile。",
                "scenarios": payload},
               ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8")
print("wrote", OUT.relative_to(ROOT))

# Merge into the generated catalog in place so an install picks it up now.
changed = 0
for item in scenarios:
    copy = payload[item["slug"]]
    profile = item["profile"]
    if profile.get("usage_hint") != copy["usage_hint"] or \
            list(profile.get("suggested_questions") or []) != copy["suggested_questions"]:
        profile["usage_hint"] = copy["usage_hint"]
        profile["suggested_questions"] = list(copy["suggested_questions"])
        changed += 1
CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
print("catalog profiles updated:", changed)
