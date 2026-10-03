"""Verify scene source checksums; optionally compare to a OneAgent checkout."""
import argparse
import hashlib
import json
from pathlib import Path


def verify(source=None):
    root = Path(__file__).resolve().parents[1] / "Scene"
    manifest = json.loads((root / "source-manifest.json").read_text(encoding="utf-8"))
    failures = []
    for entry in manifest["files"]:
        actual = (root / entry["path"]).read_bytes()
        if hashlib.sha256(actual).hexdigest() != entry["sha256"]:
            failures.append(entry["path"] + ": checksum mismatch")
            continue
        if source is None:
            continue
        origin = entry["source"].split("#", 1)[0]
        expected = (source / origin).read_bytes()
        if entry.get("format"):
            data = json.loads(expected)
            if entry["path"] == "catalog.json":
                data = {"categories": data["categories"], "scenes": [s["id"] for s in data["scenes"]]}
            else:
                scene_id = entry["path"].split("/", 1)[0]
                data = next(s for s in data["scenes"] if s["id"] == scene_id)
            if json.loads(actual) != data:
                failures.append(entry["path"] + ": JSON values changed")
            continue
        if "lines" in entry:
            start, end = entry["lines"]
            expected = b"".join(expected.splitlines(keepends=True)[start - 1:end])
        elif "offsets" in entry:
            start, end = entry["offsets"]
            expected = expected.decode("utf-8")[start:end].encode("utf-8")
        elif "byte_range" in entry:
            start, end = entry["byte_range"]
            expected = expected[start:end]
        if entry.get("source_sha256"):
            if hashlib.sha256(expected).hexdigest() != entry["source_sha256"]:
                failures.append(entry["path"] + ": upstream source changed; review adaptation")
        elif actual != expected:
            failures.append(entry["path"] + ": source bytes changed")
    return len(manifest["files"]), failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Optional OneAgent repository root")
    count, failures = verify(parser.parse_args().source)
    for failure in failures:
        print(failure)
    print(f"Verified {count} source entries; {len(failures)} differences")
    raise SystemExit(bool(failures))
