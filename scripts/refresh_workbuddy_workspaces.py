"""Refresh the installed WorkBuddy skill files for the test15 tenant.

The 62 workspaces were provisioned from an older copy of the presets, so their
``skills/wb-*`` trees lag the reviewed files under ``agent/presets/workbuddy``.
This mirrors the preset tree onto every workspace (same layout the installer
creates), then rewrites each ``workbuddy-preset.json`` so the installer's
fingerprint check matches again and a later run reports ``already_installed``.

Only the skill tree and the marker are touched: ``AGENT.md``, ``RULE.md``,
``USER.md``, knowledge, memory and any tenant output files are left alone.
Nothing is deleted.
"""
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(r"C:\rdai\rsmagent")
PRESET = ROOT / "agent/presets/workbuddy"
SHARED = PRESET / "shared"
SKILLS_SRC = PRESET / "skills"
TENANT_ID = "tnt_EA3qM-lHPLD8ZPwW"
TENANT_CODE = "test15"
AGENTS = Path(r"C:\Users\Administrator\.cow\tenant-roots\tenants") / TENANT_CODE / "agents"

APPLY = "--apply" in sys.argv

sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "iwa", ROOT / "scripts/install_workbuddy_agents.py")
iwa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iwa)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files(skill):
    """Exactly what the installer places under ``skills/<skill>``."""
    base = SKILLS_SRC / skill
    out = {}
    for path in base.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        out[str(path.relative_to(base)).replace("\\", "/")] = path
    out["scripts/scenario_io.py"] = SHARED / "scenario_io.py"
    out["references/local-io.md"] = SHARED / "local-io.md"
    return out


catalog = json.loads((PRESET / "catalog.json").read_text(encoding="utf-8"))
print("scenarios:", len(catalog["scenarios"]))

problems = []
plan = []
histogram = {}
extra_total = 0
bytes_to_write = 0

for entry in catalog["scenarios"]:
    skill, slug = entry["skill"], entry["slug"]
    agent_id = skill + "-" + TENANT_CODE
    workspace = AGENTS / agent_id
    dest = workspace / "skills" / skill
    marker = workspace / "workbuddy-preset.json"

    if not workspace.is_dir():
        problems.append(f"{agent_id}: workspace missing")
        continue
    if dest.resolve() != dest:
        problems.append(f"{agent_id}: skill dir resolves through a symlink")
        continue
    if not marker.is_file():
        problems.append(f"{agent_id}: marker missing")
        continue
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    if recorded.get("slug") != slug or recorded.get("tenant_id") != TENANT_ID:
        problems.append(f"{agent_id}: marker mismatch ({recorded.get('slug')})")
        continue

    sources = source_files(skill)
    stale, gone = [], []
    for rel, src in sorted(sources.items()):
        target = dest / rel
        if not target.is_file():
            gone.append(rel)
        elif sha(target) != sha(src):
            stale.append(rel)
        bytes_to_write += src.stat().st_size
        histogram[rel] = histogram.get(rel, 0) + (0 if target.is_file() and sha(target) == sha(src) else 1)

    tracked = set(sources)
    extras = [str(p.relative_to(dest)).replace("\\", "/")
              for p in dest.rglob("*")
              if p.is_file() and "__pycache__" not in p.parts
              and str(p.relative_to(dest)).replace("\\", "/") not in tracked]
    extra_total += len(extras)

    plan.append({"entry": entry, "agent_id": agent_id, "workspace": workspace,
                 "skill": skill, "dest": dest, "marker": marker, "recorded": recorded,
                 "stale": stale, "gone": gone, "extras": extras})

print("problems:", problems if problems else "(none)")
print("workspaces to refresh:", len(plan))
print("files differing from the preset:", sum(len(p["stale"]) for p in plan))
print("files missing in a workspace   :", sum(len(p["gone"]) for p in plan))
print("extra files already in a workspace (kept):", extra_total)
print("payload to write: %.1f MB" % (bytes_to_write / 1e6))

print("\nmost commonly stale files:")
for rel, n in sorted(histogram.items(), key=lambda kv: -kv[1])[:12]:
    if n:
        print(f"   {n:3}  {rel}")

print("\nworkspaces with extras:")
for row in plan:
    if row["extras"]:
        print(f"   {row['agent_id']}: {row['extras'][:4]}")

if APPLY:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = Path(tempfile.gettempdir()) / f"workbuddy-workspace-refresh-{stamp}"
    backup.mkdir(parents=True)
    print("\nbacking up ->", backup)
    for row in plan:
        target = backup / row["agent_id"]
        target.mkdir(parents=True)
        shutil.copytree(row["dest"], target / "skills" / row["skill"])
        shutil.copy2(row["marker"], target / "workbuddy-preset.json")
    before = sum(1 for _ in backup.rglob("*") if _.is_file())
    print("backup files:", before)

    written = 0
    for row in plan:
        sources = source_files(row["skill"])
        for rel, src in sources.items():
            dest_file = row["dest"] / rel
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_file)
            written += 1
        expected = {"preset": "workbuddy", "version": 1, "slug": row["entry"]["slug"],
                    "tenant_id": TENANT_ID,
                    "bundle_sha256": iwa.validate_bundle(row["entry"], PRESET)}
        row["marker"].write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
    print("files copied:", written, "| markers rewritten:", len(plan))

    # Verify: every bundle now matches its marker, and required files exist.
    bad = []
    for entry in catalog["scenarios"]:
        skill = entry["skill"]
        workspace = AGENTS / (skill + "-" + TENANT_CODE)
        recorded = json.loads((workspace / "workbuddy-preset.json").read_text(encoding="utf-8"))
        if recorded["bundle_sha256"] != iwa.validate_bundle(entry, PRESET):
            bad.append(skill + ": marker")
        required = [workspace / "AGENT.md", workspace / "RULE.md",
                    workspace / "skills" / skill / "SKILL.md",
                    workspace / "skills" / skill / "scripts/scenario_io.py",
                    workspace / "skills" / skill / "references" / "local-io.md"]
        for path in required:
            if not path.is_file():
                bad.append(f"{skill}: missing {path.name}")
    print("verification problems:", bad if bad else "(none)")
