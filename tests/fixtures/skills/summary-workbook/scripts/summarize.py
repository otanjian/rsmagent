# encoding:utf-8
"""Summarize the fixed acceptance workbook (A06).

Reads the three fixed records (100, 200, 50) from an xlsx workbook and writes the
summary workbook plus a machine-readable result into ``--output``.

The reader is deliberately standard-library only: parsing xlsx is a zip plus a
little XML, and pulling in a second spreadsheet dependency to read three cells
would widen the shipped surface for no benefit. Writing uses ``xlsxwriter``, which
is the dependency this skill declares.

Everything this script writes goes under ``--output``. It never writes beside the
input, so the acceptance run can assert the input's digest is unchanged.
"""

import argparse
import json
import re
import zipfile
from pathlib import Path

# The one rule this fixture encodes, stated where it is applied.
SUMMARY_CELL = "汇总"

_ROW = re.compile(r"<row[^>]*>(.*?)</row>", re.S)
# Capture the whole opening tag so *all* attributes are visible: with the
# reference extracted by its own group, a trailing ``t="s"`` would be swallowed
# by the "rest" group and never inspected.
_CELL = re.compile(r"<c\s([^>]*)>(.*?)</c>", re.S)
_REFERENCE = re.compile(r'r="([A-Z]+)(\d+)"')
_VALUE = re.compile(r"<v>(.*?)</v>", re.S)
# A shared-string cell stores an *index* in <v>, not a number. Reading those as
# amounts is the classic way this fixture would quietly produce the wrong sum.
_NOT_A_NUMBER = re.compile(r't="(?:s|str|inlineStr|b|e)"')


def _numbers(path: Path) -> list:
    """The numeric cells of the first sheet, in reading order."""
    with zipfile.ZipFile(path) as archive:
        sheet_name = next(
            name for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        xml = archive.read(sheet_name).decode("utf-8")
    values = []
    for row in _ROW.findall(xml):
        for attributes, body in _CELL.findall(row):
            if not _REFERENCE.search(attributes):
                continue
            if _NOT_A_NUMBER.search(attributes):
                continue
            found = _VALUE.search(body)
            if not found:
                continue
            try:
                values.append(float(found.group(1)))
            except ValueError:
                continue
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import xlsxwriter

    amounts = _numbers(args.input)
    if len(amounts) != 3:
        raise SystemExit(
            f"expected the three fixed records, found {len(amounts)} numeric cells")
    total = sum(amounts)

    args.output.mkdir(parents=True, exist_ok=True)
    book_path = args.output / "汇总报告.xlsx"
    book = xlsxwriter.Workbook(str(book_path))
    sheet = book.add_worksheet("汇总")
    sheet.write(0, 0, "记录数")
    sheet.write_number(0, 1, len(amounts))
    sheet.write(1, 0, SUMMARY_CELL)
    sheet.write_number(1, 1, total)
    for index, amount in enumerate(amounts, start=2):
        sheet.write(index, 0, f"记录{index - 1}")
        sheet.write_number(index, 1, amount)
    book.close()

    result = {
        "records": amounts,
        "summary": total,
        "files": [str(book_path)],
    }
    (args.output / "汇总结果.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
