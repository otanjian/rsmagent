# encoding:utf-8
"""Render the fixed acceptance document (A07).

Template-driven, standard-library only. The template is mandatory: a run without
``assets/template.md`` fails rather than emitting a report-shaped file, which is
what makes "模板资源齐全" observable.

Every file this script reads is named relative to the *skill*, so the skill works
wherever its pinned version happens to be mounted.
"""

import argparse
import json
import re
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = SKILL_ROOT / "assets" / "template.md"
FIELDS = SKILL_ROOT / "references" / "fields.md"

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

REQUIRED = ("title", "customer", "project_code", "summary")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not TEMPLATE.is_file():
        raise SystemExit(f"the skill's template is missing: {TEMPLATE}")
    template = TEMPLATE.read_text(encoding="utf-8")
    fields = json.loads(args.input.read_text(encoding="utf-8"))

    missing = [name for name in REQUIRED if not str(fields.get(name) or "").strip()]
    values = {name: str(fields.get(name) or "") for name in _PLACEHOLDER.findall(template)}
    # A placeholder with no value is left empty rather than invented.
    body = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template)

    args.output.mkdir(parents=True, exist_ok=True)
    report = args.output / "报告.md"
    report.write_text(body, encoding="utf-8")
    result = {
        "fields_used": sorted(values),
        "missing": missing,
        "template": str(TEMPLATE),
        "auxiliary_present": FIELDS.is_file(),
        "files": [str(report)],
    }
    (args.output / "渲染结果.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
