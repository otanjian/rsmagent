# encoding:utf-8
"""Write the fixed acceptance workbook for the ``summary-workbook`` skill.

A06 fixes the sample: three synthetic records with amounts 100, 200 and 50, whose
summary is 350. The workbook is *generated* rather than committed so the input
digest the acceptance run checks is reproducible from source, and so the fixture
carries no opaque binary.

Deliberately dependency-light: the sibling reader parses xlsx with the standard
library only, so this skill's whole declared dependency stays ``xlsxwriter``.
"""

import argparse
import json
from pathlib import Path

RECORDS = (("R-001", 100), ("R-002", 200), ("R-003", 50))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import xlsxwriter

    args.output.parent.mkdir(parents=True, exist_ok=True)
    book = xlsxwriter.Workbook(str(args.output))
    sheet = book.add_worksheet("记录")
    sheet.write(0, 0, "记录号")
    sheet.write(0, 1, "金额")
    for index, (record_id, amount) in enumerate(RECORDS, start=1):
        sheet.write(index, 0, record_id)
        sheet.write_number(index, 1, amount)
    book.close()

    print(json.dumps({
        "input": str(args.output),
        "records": [{"id": record_id, "amount": amount}
                    for record_id, amount in RECORDS],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
