# encoding:utf-8
"""The ``excel`` tool: inspect a workbook, and update one column of it.

The tool exists because the alternative on a headless host was a hand-written
script: no MS Office COM, no OleDb/ACE driver, and a `python` the service's PATH
did not have. These tests pin the two things that made that alternative fail -
learning the column name, and changing cells without disturbing the rest of the
workbook - plus the permission wiring, because a path-carrying tool that is not
classified is confined by nothing.
"""

import os

import pytest

from agent.tools.excel.excel import Excel
from agent.permission.policy import (
    READ_ONLY, WORKSPACE_WRITE, check_tool_call, _written_paths)
from agent.permission import isolation as iso
from agent.permission.isolation import _Boundary, isolation_decision


@pytest.fixture
def workbook(tmp_path):
    """A template shaped like the real ones: title, blank row, header, data."""
    openpyxl = pytest.importorskip("openpyxl")

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Q4 ITGC"
    sheet["A1"] = "XX公司 2026 年 Q4 ITGC 底稿"
    sheet["A3"], sheet["B3"], sheet["C3"], sheet["D3"] = "序号", "索引号", "文档索引", "支持文档"
    for row in range(4, 14):
        sheet.cell(row=row, column=1, value=row - 3)
        sheet.cell(row=row, column=2, value="ITGC-%02d" % (row - 3))
        sheet.cell(row=row, column=3, value="DOC-%02d" % (row - 3))
        sheet.cell(row=row, column=4, value="support-%02d.xlsx" % (row - 3))
    sheet["E4"] = "=SUM(A4:A13)"          # a formula that must survive an update
    sheet["E4"].font = openpyxl.styles.Font(bold=True)

    other = book.create_sheet("说明")
    other["A1"] = "说明页"
    path = tmp_path / "ITGC 底稿.xlsx"
    book.save(path)
    return path


def _tool(tmp_path):
    return Excel({"cwd": str(tmp_path)})


def _text(result):
    assert result.status == "success", result.result
    return result.result["output"]


def test_inspect_reports_sheets_header_rows_and_column_names(tmp_path, workbook):
    output = _text(_tool(tmp_path).execute(
        {"action": "inspect", "path": workbook.name}))

    assert "Q4 ITGC" in output and "说明" in output
    assert "columns named in row 3" in output
    assert "A = 序号" in output
    assert "C = 文档索引" in output


def test_inspect_takes_no_action_on_the_file(tmp_path, workbook):
    before = workbook.read_bytes()
    _tool(tmp_path).execute({"action": "inspect", "path": workbook.name})
    assert workbook.read_bytes() == before


def test_inspect_dumps_a_row_range(tmp_path, workbook):
    output = _text(_tool(tmp_path).execute(
        {"action": "inspect", "path": workbook.name, "sheet": "Q4 ITGC",
         "sample_rows": "3-5"}))
    assert "rows 3-5 (3 shown)" in output
    assert "DOC-01" in output and "DOC-02" in output


def test_inspect_names_a_key_value_sheet_instead_of_guessing_a_header(tmp_path):
    """A metadata block is not a table; claiming row 2 as its header would send
    the model to the wrong column with confidence."""
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "封面"
    for row, (key, value) in enumerate(
            [("公司", "恒信精密"), ("期间", "2026 年 6 月"), ("制表", "财务小张")], start=1):
        sheet.cell(row=row, column=1, value=key)
        sheet.cell(row=row, column=2, value=value)
    path = tmp_path / "keyvalue.xlsx"
    book.save(path)

    output = _text(_tool(tmp_path).execute({"action": "inspect", "path": path.name}))
    assert "no table header row found" in output
    assert "columns named in row" not in output


def test_update_by_header_name_clears_only_that_column(tmp_path, workbook):
    openpyxl = pytest.importorskip("openpyxl")
    output = _text(_tool(tmp_path).execute(
        {"action": "update", "path": workbook.name, "sheet": "Q4 ITGC",
         "column": "文档索引"}))

    assert "10 cell(s) cleared" in output
    assert "header row 3" in output

    sheet = openpyxl.load_workbook(workbook)["Q4 ITGC"]
    assert [sheet.cell(row=row, column=3).value for row in range(4, 14)] == [None] * 10
    # Everything around the cleared column is untouched, formula included.
    assert sheet["A3"].value == "序号"
    assert sheet["D4"].value == "support-01.xlsx"
    assert sheet["E4"].value == "=SUM(A4:A13)"
    assert sheet["E4"].font.bold is True


def test_update_by_letter_writes_a_value_to_a_row_range(tmp_path, workbook):
    openpyxl = pytest.importorskip("openpyxl")
    _text(_tool(tmp_path).execute(
        {"action": "update", "path": workbook.name, "sheet": "Q4 ITGC",
         "column": "D", "rows": "4-6", "value": "N/A"}))

    sheet = openpyxl.load_workbook(workbook)["Q4 ITGC"]
    assert [sheet.cell(row=row, column=4).value for row in (4, 5, 6)] == ["N/A"] * 3
    assert sheet["D7"].value == "support-04.xlsx"


def test_update_can_save_a_copy_and_leave_the_source_alone(tmp_path, workbook):
    openpyxl = pytest.importorskip("openpyxl")
    output = _text(_tool(tmp_path).execute(
        {"action": "update", "path": workbook.name, "sheet": "Q4 ITGC",
         "column": "C", "rows": "4-5", "value": "N/A", "output": "out.xlsx"}))

    assert "to out.xlsx" in output
    assert (tmp_path / "out.xlsx").exists()
    assert openpyxl.load_workbook(tmp_path / "out.xlsx")["Q4 ITGC"]["C4"].value == "N/A"
    assert openpyxl.load_workbook(workbook)["Q4 ITGC"]["C4"].value == "DOC-01"


def test_update_defaults_to_the_data_rows_under_the_header(tmp_path, workbook):
    openpyxl = pytest.importorskip("openpyxl")
    output = _text(_tool(tmp_path).execute(
        {"action": "update", "path": workbook.name, "sheet": "Q4 ITGC",
         "column": "文档索引", "rows": "all", "value": "待补"}))

    assert "rows: 4-13 (10 selected)" in output
    assert openpyxl.load_workbook(workbook)["Q4 ITGC"]["C13"].value == "待补"


def test_update_lists_the_header_rows_when_the_column_is_unknown(tmp_path, workbook):
    result = _tool(tmp_path).execute(
        {"action": "update", "path": workbook.name, "sheet": "Q4 ITGC",
         "column": "不存在的列"})

    assert result.status == "error"
    assert "no column header matched" in result.result
    assert "row 3:" in result.result
    assert "文档索引" in result.result


def test_update_refuses_an_ambiguous_sheet(tmp_path, workbook):
    result = _tool(tmp_path).execute(
        {"action": "update", "path": workbook.name, "column": "C"})
    assert result.status == "error"
    assert "pass sheet=<name>" in result.result


def test_update_requires_a_column(tmp_path, workbook):
    result = _tool(tmp_path).execute(
        {"action": "update", "path": workbook.name, "sheet": "Q4 ITGC"})
    assert result.status == "error"
    assert "needs `column`" in result.result


def test_unknown_tool_action_and_format_are_refused(tmp_path, workbook):
    tool = _tool(tmp_path)
    assert tool.execute({"action": "edit", "path": workbook.name}).status == "error"

    legacy = tmp_path / "old.xls"
    legacy.write_bytes(b"")
    result = tool.execute({"action": "inspect", "path": legacy.name})
    assert result.status == "error"
    assert "save it as .xlsx" in result.result

    plain = tmp_path / "notes.txt"
    plain.write_text("hi", encoding="utf-8")
    assert tool.execute({"action": "inspect", "path": plain.name}).status == "error"


def test_missing_file_explains_relative_path_resolution(tmp_path):
    result = _tool(tmp_path).execute({"action": "inspect", "path": "nope.xlsx"})
    assert result.status == "error"
    assert "path not found" in result.result
    assert "workspace" in result.result


# ---------------------------------------------------------------------------
# permission and isolation wiring
# ---------------------------------------------------------------------------

def test_read_only_allows_inspect_and_refuses_update():
    inspect = check_tool_call(READ_ONLY, "excel",
                              {"action": "inspect", "path": "/any/where.xlsx"})
    assert inspect.allowed

    update = check_tool_call(READ_ONLY, "excel",
                             {"action": "update", "path": "/any/where.xlsx"})
    assert not update.allowed
    assert "changes state" in update.reason


def test_workspace_write_confines_both_the_source_and_the_save_as_target(tmp_path):
    inside = tmp_path / "book.xlsx"
    outside = "/etc/passwd"

    ok = check_tool_call(WORKSPACE_WRITE, "excel",
                         {"action": "update", "path": str(inside)},
                         cwd=str(tmp_path), write_roots=[str(tmp_path)])
    assert ok.allowed

    # The source is in the workspace, the copy is not: the copy is the write
    # that must be refused, which is why both paths are collected.
    for args in (
        {"action": "update", "path": str(inside), "output": outside},
        {"action": "update", "path": outside},
    ):
        decision = check_tool_call(WORKSPACE_WRITE, "excel", args,
                                   cwd=str(tmp_path), write_roots=[str(tmp_path)])
        assert not decision.allowed
        assert outside in decision.reason

    # ...and inspecting is a read, so the same outside path is fine.
    assert check_tool_call(WORKSPACE_WRITE, "excel",
                           {"action": "inspect", "path": outside},
                           cwd=str(tmp_path), write_roots=[str(tmp_path)]).allowed


def test_written_paths_only_reports_destinations():
    assert _written_paths("excel", {"action": "inspect", "path": "a.xlsx"}) == []
    assert _written_paths("excel", {"action": "update", "path": "a.xlsx"}) == ["a.xlsx"]
    assert _written_paths("excel", {"action": "update", "path": "a.xlsx",
                                    "output": "b.xlsx"}) == ["a.xlsx", "b.xlsx"]


class _Ident:
    user_id = "u1"
    tenant_id = "t1"
    agent_id = "alpha"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """A tenant boundary with a writable workspace and a blocked home."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    boundary = _Boundary(read_roots=[str(work)], write_roots=[str(work)],
                         blocked=[str(home)], tenant_id="t1")
    monkeypatch.setattr(iso, "enabled", lambda: True)
    monkeypatch.setattr(iso, "_current_identity", lambda: _Ident())
    monkeypatch.setattr(iso, "resolve_boundary", lambda ident=None: boundary)
    return {"work": work, "home": home}


def test_isolation_confines_excel_by_action(isolated):
    book = isolated["work"] / "book.xlsx"
    secret = isolated["home"] / "secret.xlsx"

    # update writes in place: allowed inside the workspace...
    assert isolation_decision("excel",
                              {"action": "update", "path": str(book)}).allowed
    # ...refused outside it...
    outside = isolation_decision("excel", {"action": "update", "path": str(secret)})
    assert not outside.allowed
    # ...and a read of the same blocked file is refused too.
    assert not isolation_decision("excel",
                                  {"action": "inspect", "path": str(secret)}).allowed


def test_isolation_checks_the_save_as_target_as_well(isolated):
    book = isolated["work"] / "book.xlsx"
    secret = isolated["home"] / "out.xlsx"

    decision = isolation_decision(
        "excel", {"action": "update", "path": str(book), "output": str(secret)})
    assert not decision.allowed
    assert "out.xlsx" in decision.reason


def test_excel_is_registered_and_wire_safe():
    from agent.tools.base_tool import is_wire_safe_name

    assert is_wire_safe_name("excel")
    assert Excel().name == "excel"
    assert set(Excel().params["properties"]["action"]["enum"]) == {"inspect", "update"}


def test_no_dependency_on_microsoft_excel():
    """The whole point: openpyxl does the work, nothing shells out to Excel."""
    source = open(os.path.join(os.path.dirname(__file__), "..", "agent", "tools",
                               "excel", "excel.py"), encoding="utf-8").read()
    assert "subprocess" not in source
    assert "win32com" not in source
