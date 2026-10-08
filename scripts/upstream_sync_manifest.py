"""Validation shared by the upstream-sync gate and its regression tests."""
import json
import re
from pathlib import Path

DISPOSITIONS = {"reuse", "adapt", "equivalent", "product-excluded", "pending"}
VERIFICATIONS = {"pending", "passed", "failed"}


def validate_manifest(data, root: Path):
    errors = []
    if data.get("schema_version") != 1:
        errors.append("unsupported schema_version")
    for field in ("source_sha", "target_base_sha"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(data.get(field, ""))):
            errors.append(f"{field}: fixed commit SHA required")
    entries = data.get("capabilities")
    if not isinstance(entries, list) or not entries:
        return errors + ["nonempty capabilities required"]
    seen = set()
    for item in entries:
        if not isinstance(item, dict):
            errors.append("capability must be an object")
            continue
        ident = item.get("id", "")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Z]+-\d{3,}", ident) or ident in seen:
            errors.append(f"{ident}: invalid or duplicate stable ID")
        if isinstance(ident, str):
            seen.add(ident)
        for field in ("behavior", "identity", "owner", "task"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                errors.append(f"{ident}: {field} required")
        for field in ("entrypoints", "dependencies", "supported_forms", "required_tests", "evidence"):
            values = item.get(field)
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
                errors.append(f"{ident}: nonempty {field} required")
        if not isinstance(item.get("disposition"), str) or item["disposition"] not in DISPOSITIONS:
            errors.append(f"{ident}: invalid disposition")
        if not isinstance(item.get("verification"), str) or item["verification"] not in VERIFICATIONS:
            errors.append(f"{ident}: invalid verification")
        source = item.get("source", {})
        if not isinstance(source, dict) or source.get("sha") != data.get("source_sha") or not source.get("paths"):
            errors.append(f"{ident}: source commit and paths required")
        refs = [item.get("task", "")]
        for field in ("entrypoints", "evidence"):
            if isinstance(item.get(field), list):
                refs += item[field]
        for value in refs:
            relative = str(value).split("#", 1)[0]
            path = (root / relative).resolve()
            if not relative or not path.is_relative_to(root.resolve()) or not path.is_file():
                errors.append(f"{ident}: missing or non-repository reference {relative}")
        if item.get("verification") == "passed":
            if not item.get("candidate") or not item.get("results"):
                errors.append(f"{ident}: passed requires candidate and result evidence")
        if item.get("disposition") == "product-excluded" and not item.get("review"):
            errors.append(f"{ident}: product exclusion requires review evidence")
    return errors


def load_manifest(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
