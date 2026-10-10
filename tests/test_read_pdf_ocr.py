# encoding:utf-8
"""A PDF with no text layer must be recognised, not reported as unreadable.

Business documents on this deployment (customs declarations, invoices,
scanned contracts) are frequently page images. Before the OCR fallback,
``read`` answered them with "no text content could be extracted", which ended
the turn: the model had nothing to reason over.

These tests pin the fallback's contract without depending on the recogniser
being installed -- the OCR call itself is stubbed, so they run in CI and on a
deployment that skipped ``requirements-optional.txt``.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.tools.read.read import Read
from agent.tools.utils import pdf_scan


def _image_only_pdf(path, pages=1):
    """A PDF whose pages carry no text at all (a blank page stands in for a scan)."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=300)
    with open(path, "wb") as f:
        writer.write(f)
    return path


def _text_pdf(path, text="Revenue 12345"):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject(
        {NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    content = DecodedStreamObject()
    content.set_data(b'BT /F1 12 Tf 20 250 Td (' + text.encode('latin-1') + b') Tj ET')
    page[NameObject('/Contents')] = content
    with open(path, "wb") as f:
        writer.write(f)
    return path


class TestReadPdfOcr(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.tool = Read({"cwd": self.work})
        self._real_ocr_pages = pdf_scan.ocr_pages

    def tearDown(self):
        pdf_scan.ocr_pages = self._real_ocr_pages

    def _stub_ocr(self, mapping, error=None, calls=None):
        def fake(path, page_numbers, scale=pdf_scan.DEFAULT_SCALE, timeout_pages=pdf_scan.MAX_OCR_PAGES):
            if calls is not None:
                calls.append(list(page_numbers))
            return ({p: mapping[p] for p in page_numbers if p in mapping}, error)
        pdf_scan.ocr_pages = fake

    def test_scanned_pdf_is_recognised_and_marked(self):
        path = _image_only_pdf(os.path.join(self.work, "scan.pdf"))
        self._stub_ocr({1: "中华人民共和国海关进口货物报关单\n报关单号 371320261000065880"})

        result = self.tool.execute({"path": path})

        self.assertEqual(result.status, "success", result.result)
        content = result.result["content"]
        self.assertIn("中华人民共和国海关进口货物报关单", content)
        self.assertIn("371320261000065880", content)
        # The page is labelled so a recognised value is not quoted as exact.
        self.assertIn("(OCR)", content)
        self.assertEqual(result.result["ocr_pages"], [1])
        self.assertIn("OCR", content.split("no text layer")[-1])

    def test_ocr_is_only_asked_for_pages_without_text(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject(
            {NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        content = DecodedStreamObject()
        content.set_data(b'BT /F1 12 Tf 20 250 Td (has text) Tj ET')
        page[NameObject('/Contents')] = content
        writer.add_blank_page(width=300, height=300)          # page 2: no text
        path = os.path.join(self.work, "mixed.pdf")
        with open(path, "wb") as f:
            writer.write(f)

        calls = []
        self._stub_ocr({2: "recognised page two"}, calls=calls)

        result = self.tool.execute({"path": path})

        self.assertEqual(result.status, "success", result.result)
        self.assertEqual(calls, [[2]])
        content = result.result["content"]
        self.assertIn("has text", content)
        self.assertIn("recognised page two", content)
        self.assertNotIn("Page 1 (OCR)", content)
        self.assertIn("Page 2 (OCR)", content)

    def test_text_pdf_never_invokes_ocr(self):
        path = _text_pdf(os.path.join(self.work, "plain.pdf"))

        def explode(*a, **k):
            raise AssertionError("OCR must not run on a PDF that has a text layer")

        pdf_scan.ocr_pages = explode
        result = self.tool.execute({"path": path})
        self.assertEqual(result.status, "success", result.result)
        self.assertIn("Revenue 12345", result.result["content"])

    def test_ocr_can_be_disabled(self):
        tool = Read({"cwd": self.work, "pdf_ocr": False})
        path = _image_only_pdf(os.path.join(self.work, "scan2.pdf"))
        self._stub_ocr({1: "should not be used"})

        result = tool.execute({"path": path})

        self.assertEqual(result.status, "success", result.result)
        self.assertNotIn("should not be used", result.result["content"])
        self.assertIn("OCR", result.result["content"])
        self.assertIn("disabled", result.result["content"])

    def test_empty_recognition_reports_ocr_produced_nothing(self):
        path = _image_only_pdf(os.path.join(self.work, "blank.pdf"))
        self._stub_ocr({})

        result = self.tool.execute({"path": path})

        self.assertEqual(result.status, "success", result.result)
        content = result.result["content"]
        self.assertIn("OCR produced nothing", content)
        self.assertEqual(result.result["pages_without_text"], [1])

    def test_partial_recognition_surfaces_the_reason(self):
        path = _image_only_pdf(os.path.join(self.work, "p3.pdf"), pages=3)
        self._stub_ocr({1: "page one text"},
                       error="page 2 could not be recognised: boom")

        result = self.tool.execute({"path": path, "pages": "1-3"})

        self.assertEqual(result.status, "success", result.result)
        content = result.result["content"]
        self.assertIn("page one text", content)
        self.assertIn("OCR stopped early", content)
        self.assertIn("boom", content)


class TestPdfOcrConfig(unittest.TestCase):
    def test_enabled_by_default(self):
        self.assertTrue(Read({"cwd": "."})._pdf_ocr_enabled())

    def test_accepts_string_falsey_values(self):
        for value in ("0", "false", "False", "no", "off"):
            tool = Read({"cwd": ".", "pdf_ocr": value})
            self.assertFalse(tool._pdf_ocr_enabled(), value)

    def test_blank_value_keeps_the_default(self):
        # An empty string is "not configured", not "off": clearing the field in
        # a config file should not silently disable scanning support.
        self.assertTrue(Read({"cwd": ".", "pdf_ocr": ""})._pdf_ocr_enabled())

    def test_scale_falls_back_when_out_of_range_or_unparsable(self):
        self.assertEqual(Read({"cwd": ".", "pdf_ocr_scale": 999})._pdf_ocr_scale(),
                         pdf_scan.DEFAULT_SCALE)
        self.assertEqual(Read({"cwd": ".", "pdf_ocr_scale": "abc"})._pdf_ocr_scale(),
                         pdf_scan.DEFAULT_SCALE)
        self.assertEqual(Read({"cwd": ".", "pdf_ocr_scale": 1.5})._pdf_ocr_scale(), 1.5)


class TestOcrLineOrdering(unittest.TestCase):
    def test_fragments_on_one_line_are_joined_left_to_right(self):
        items = [(100.0, 300.0, "VALUE"), (100.5, 20.0, "LABEL")]
        self.assertEqual(pdf_scan._order_lines(items), ["LABEL VALUE"])

    def test_separate_lines_keep_their_order(self):
        items = [(200.0, 20.0, "second"), (100.0, 20.0, "first")]
        self.assertEqual(pdf_scan._order_lines(items), ["first", "second"])

    def test_a_large_vertical_jump_is_not_merged(self):
        items = [(100.0, 20.0, "top"), (400.0, 20.0, "bottom")]
        self.assertEqual(pdf_scan._order_lines(items), ["top", "bottom"])


#: Column x-ranges of the customs-form detail table, matching the real form:
#: 项号 is a sliver, the name column is by far the widest.
_COLUMNS = {
    "项号": (20.0, 60.0),
    "商品编号": (70.0, 200.0),
    "商品名称及规格型号": (210.0, 640.0),
    "数量及单位": (660.0, 830.0),
    "单价/总价/币制": (840.0, 1050.0),
    "原产国（地区）": (1060.0, 1200.0),
    "征免": (1210.0, 1400.0),
}


def _row(y, cells):
    """One physical text line: (y, [(column, text), ...]) as detector output."""
    boxes = []
    for offset, (column, text) in enumerate(cells):
        left, right = _COLUMNS[column]
        # Real recognition never reports two boxes at exactly the same y.
        boxes.append((y + offset * 0.1, left, y + 14.0 + offset * 0.1, right, text))
    return boxes


def _customs_table_boxes():
    """A two-entry detail table of the shape the real declarations produce."""
    boxes = []
    boxes += _row(100.0, [(name, name) for name in _COLUMNS])
    # Entry 1: the name cell is three lines tall, so its quantities and prices
    # land on rows the name column also occupies.
    boxes += _row(150.0, [("商品名称及规格型号", "5903909000过胶尼龙布"),
                          ("数量及单位", "7608千克"),
                          ("单价/总价/币制", "3.8300"),
                          ("原产国（地区）", "越南"),
                          ("征免", "照章征税")])
    boxes += _row(200.0, [("项号", "1|3"),
                          ("商品名称及规格型号", "处理材质：硅胶|成分含量：尼龙87.8%"),
                          ("单价/总价/币制", "74091.35")])
    boxes += _row(250.0, [("商品名称及规格型号", "处理工艺：涂布|非双面涂层"),
                          ("数量及单位", "19345米"),
                          ("单价/总价/币制", "美元")])
    # Entry 2.
    boxes += _row(300.0, [("商品名称及规格型号", "5407101000尼龙布"),
                          ("数量及单位", "8828千克"),
                          ("单价/总价/币制", "3.1800"),
                          ("原产国（地区）", "越南"),
                          ("征免", "照章征税")])
    boxes += _row(350.0, [("项号", "2|3"),
                          ("商品名称及规格型号", "织造方法：机织|染整方法：未漂白未染色"),
                          ("单价/总价/币制", "58035.00")])
    boxes += _row(400.0, [("商品名称及规格型号", "结构：平纹|成分含量：尼龙100"),
                          ("数量及单位", "18250米"),
                          ("单价/总价/币制", "美元")])
    boxes += _row(470.0, [("项号", "特殊关系确认：是 价格影响确认：否")])
    return boxes


class TestGoodsTableReconstruction(unittest.TestCase):
    """Reading order scatters one entry's cells; the table view must not."""

    def setUp(self):
        self._real_detect = pdf_scan._detect

    def tearDown(self):
        pdf_scan._detect = self._real_detect

    def _table(self):
        rows = pdf_scan._reconstruct_goods_table(_customs_table_boxes())
        self.assertTrue(rows, "the customs layout should be recognised as a table")
        return [row.split("\t") for row in rows]

    def test_header_is_rendered_as_the_first_line(self):
        header = self._table()[0]
        self.assertEqual(header[:5], ["项号", "商品编号", "商品名称及规格型号",
                                      "数量及单位", "单价/总价/币制"])

    def test_each_entry_becomes_one_line(self):
        self.assertEqual(len(self._table()), 3)          # header + two entries

    def test_a_cell_spanning_several_lines_is_joined_into_one(self):
        first = self._table()[1]
        # 数量及单位 sat on the entry's first and third rows.
        self.assertEqual(first[3], "7608千克 19345米")
        # 单价/总价/币制 sat on all three.
        self.assertEqual(first[4], "3.8300 74091.35 美元")
        self.assertIn("处理材质：硅胶", first[2])
        self.assertIn("处理工艺：涂布", first[2])

    def test_commodity_code_is_separated_from_its_name(self):
        first = self._table()[1]
        self.assertEqual(first[1], "5903909000")
        self.assertTrue(first[2].startswith("过胶尼龙布"), first[2])

    def test_the_second_entry_does_not_inherit_the_first_ones_numbers(self):
        first, second = self._table()[1], self._table()[2]
        self.assertEqual(second[0], "2|3")
        self.assertEqual(second[1], "5407101000")
        self.assertEqual(second[3], "8828千克 18250米")
        self.assertEqual(second[4], "3.1800 58035.00 美元")
        self.assertNotIn("7608", second[3])
        self.assertNotIn("74091.35", " ".join(second))

    def test_a_section_after_the_table_is_not_absorbed(self):
        joined = "\t".join(cell for row in self._table() for cell in row)
        self.assertNotIn("特殊关系确认", joined)

    def test_a_page_without_a_detail_table_is_left_alone(self):
        prose = [(100.0, 20.0, 120.0, 600.0, "兹证明下列货物已办理出口手续"),
                 (160.0, 20.0, 180.0, 600.0, "申报单位（签章）")]
        self.assertEqual(pdf_scan._reconstruct_goods_table(prose), [])

    def test_ocr_image_appends_the_table_beside_the_reading_order_text(self):
        pdf_scan._detect = lambda image: _customs_table_boxes()

        text = pdf_scan.ocr_image(object())

        # The reading-order text still comes first; the table is only added.
        self.assertIn("表格还原", text)
        self.assertLess(text.index("特殊关系确认"), text.index("表格还原"))
        self.assertLess(text.index("5903909000过胶尼龙布"),
                        text.index("表格还原"))

    def test_no_table_section_when_there_is_no_table(self):
        pdf_scan._detect = lambda image: [(100.0, 20.0, 120.0, 600.0, "只有一段说明文字")]

        self.assertNotIn("表格还原", pdf_scan.ocr_image(object()))


class TestQuantityWiderThanItsHeader(unittest.TestCase):
    """The 成交数量 often sticks out past the narrow 「数量及单位」 label.

    Real declarations put the 法定重量 above the 成交数量 in that cell, and the
    label is only as wide as its four characters, so the 成交数量 box starts a
    few pixels to the *right* of the label's edge. Matching on the label span
    alone dropped it and left the weight behind as if it were the quantity —
    ``2400个`` vanished and ``3480千克`` became the 数量, which then failed the
    单价 × 数量 = 金额 check downstream.
    """

    #: The label ends at 765; the quantity underneath starts at 770.
    COLUMNS = {
        "项号": (79.0, 116.0),
        "商品编号": (135.0, 200.0),
        "商品名称及规格型号": (348.0, 484.0),
        "数量及单位": (684.0, 765.0),
        "单价/总价/币制": (857.0, 966.0),
        "原产国（地区）": (1017.0, 1112.0),
        "征免": (1538.0, 1575.0),
    }

    def _boxes(self):
        boxes = []
        for name, (left, right) in self.COLUMNS.items():
            boxes.append((100.0, left, 114.0, right, name))
        boxes.append((575.0, 135.0, 599.0, 269.0, "8708950000气囊袋"))
        boxes.append((575.0, 752.0, 600.0, 831.0, "3480千克"))
        boxes.append((576.0, 857.0, 598.0, 1000.0, "12.3500"))
        # Wider than the label it belongs to: x starts past the label's edge.
        boxes.append((616.0, 770.0, 641.0, 830.0, "2400个"))
        boxes.append((618.0, 857.0, 641.0, 999.0, "29640.00"))
        boxes.append((641.0, 1538.0, 665.0, 1594.0, "特殊关系确认：是"))
        return boxes

    def test_the_quantity_is_kept_even_though_it_overflows_its_label(self):
        row = pdf_scan._reconstruct_goods_table(self._boxes())[1].split("\t")
        self.assertIn("2400个", row[3])
        self.assertIn("3480千克", row[3])
        self.assertIn("29640.00", row[4])

    def test_the_price_and_amount_stay_together(self):
        row = pdf_scan._reconstruct_goods_table(self._boxes())[1].split("\t")
        self.assertEqual(row[4], "12.3500 29640.00")

    def test_overflowing_text_still_lands_in_the_right_column(self):
        columns = [(left, right, label)
                   for label, (left, right) in self.COLUMNS.items()]
        bounds = pdf_scan._column_bounds(columns)
        quantity = list(self.COLUMNS).index("数量及单位")
        price = list(self.COLUMNS).index("单价/总价/币制")
        self.assertEqual(
            pdf_scan._column_for(columns, (616.0, 770.0, 641.0, 830.0, "2400个"),
                                 bounds),
            quantity)
        self.assertEqual(
            pdf_scan._column_for(columns, (618.0, 857.0, 641.0, 999.0, "29640.00"),
                                 bounds),
            price)

    def test_a_stamp_in_the_margin_is_still_outside_every_column(self):
        columns = [(left, right, label)
                   for label, (left, right) in self.COLUMNS.items()]
        bounds = pdf_scan._column_bounds(columns)
        stamp = (5.0, 3.0, 30.0, 40.0, "口领口")
        self.assertEqual(pdf_scan._column_for(columns, stamp, bounds), -1)


class TestCommoditySplit(unittest.TestCase):
    def test_code_glued_to_name_is_split(self):
        self.assertEqual(pdf_scan._split_commodity("5407101000尼龙布"),
                         [("5407101000", "尼龙布")])

    def test_a_name_without_a_code_is_left_intact(self):
        self.assertEqual(pdf_scan._split_commodity("处理材质：硅胶|成分含量：尼龙87.8%"), [])

    def test_several_codes_in_one_box_are_all_recovered(self):
        text = "5407102000涤纶布 5903909000过胶涤纶布"
        self.assertEqual(pdf_scan._split_commodity(text),
                         [("5407102000", "涤纶布"), ("5903909000", "过胶涤纶布")])


class _FakeImage:
    """Stands in for a PIL image, recording which quarter turns were applied."""

    def __init__(self, name, size=(842, 596)):
        self.name = name
        self.size = size
        self.rotations = []

    def rotate(self, degrees, expand=True):  # noqa: ARG002 - mirrors PIL
        self.rotations.append(degrees)
        return _FakeImage(f"{self.name}+{degrees}", self.size)


class TestPageOrientation(unittest.TestCase):
    """A form stored a quarter turn off must be turned before it is read.

    Reading sideways glyphs does not merely misorder them, it corrupts them,
    so the page has to be turned the right way up before recognition, not
    repaired afterwards.
    """

    #: A title, tall enough that the glyphs are unmistakably on their side.
    TITLE = "中华人民共和国海关进口货物报关单"

    def setUp(self):
        self._real_detect = pdf_scan._detect

    def tearDown(self):
        pdf_scan._detect = self._real_detect

    def _sideways(self):
        """Tall, thin boxes: the text runs bottom-to-top."""
        return [(20.0, 500.0, 620.0, 520.0, self.TITLE)]

    def _upright(self, title_top=20.0):
        """Wide boxes with the title lying along the top edge."""
        return [(title_top, 100.0, title_top + 20.0, 700.0, self.TITLE),
                (60.0, 100.0, 80.0, 700.0, "项号 商品编号 商品名称及规格型号")]

    def _install(self, layouts):
        pdf_scan._detect = lambda image: layouts[image.name]

    def test_a_page_of_wide_boxes_is_not_treated_as_rotated(self):
        self.assertFalse(pdf_scan._looks_rotated(self._upright()))

    def test_a_page_of_tall_boxes_is_treated_as_rotated(self):
        self.assertTrue(pdf_scan._looks_rotated(self._sideways()))

    def test_a_stray_tall_box_does_not_make_an_upright_page_look_rotated(self):
        boxes = self._upright() + [(60.0, 100.0, 200.0, 110.0, "竖排")]
        self.assertFalse(pdf_scan._looks_rotated(boxes))

    def test_an_empty_page_is_left_alone(self):
        self.assertFalse(pdf_scan._looks_rotated([]))

    def test_an_upright_page_is_returned_untouched_without_a_second_pass(self):
        calls = []

        def detect(image):
            calls.append(image.name)
            return self._upright()

        pdf_scan._detect = detect
        image = _FakeImage("base")

        kept, boxes = pdf_scan._upright(image)

        self.assertIs(kept, image)                 # same object, not a copy
        self.assertEqual(image.rotations, [])      # no rotation attempted
        self.assertEqual(calls, ["base"])          # recognised exactly once
        self.assertEqual(boxes, self._upright())

    def test_a_sideways_page_is_turned_so_the_title_lands_at_the_top(self):
        self._install({
            "base": self._sideways(),
            "base+90": self._upright(),                        # title at the top
            "base+270": self._upright(title_top=520.0),        # title at the end
        })
        image = _FakeImage("base")

        turned, boxes = pdf_scan._upright(image)

        self.assertEqual(image.rotations, [90, 270])   # both turns considered
        self.assertEqual(turned.name, "base+90")
        self.assertEqual(boxes, self._upright())

    def test_turning_the_other_way_is_chosen_when_that_is_what_fits(self):
        self._install({
            "base": self._sideways(),
            "base+90": self._upright(title_top=520.0),
            "base+270": self._upright(),                   # title at the top
        })
        turned, _boxes = pdf_scan._upright(_FakeImage("base"))
        self.assertEqual(turned.name, "base+270")

    def test_a_page_whose_title_is_never_read_still_returns_a_usable_guess(self):
        self._install({"base": self._sideways(), "base+90": [], "base+270": []})
        turned, boxes = pdf_scan._upright(_FakeImage("base"))
        self.assertEqual(turned.name, "base+90")
        self.assertEqual(boxes, [])

    def test_anchor_ratio_reports_where_the_title_starts(self):
        self.assertAlmostEqual(pdf_scan._anchor_ratio(self._upright(20.0), 596.0),
                               20.0 / 596.0)

    def test_anchor_ratio_ignores_a_page_without_a_title(self):
        boxes = [(20.0, 100.0, 40.0, 700.0, "一段没有标题的说明文字")]
        self.assertEqual(pdf_scan._anchor_ratio(boxes, 596.0), 1.0)


if __name__ == "__main__":
    unittest.main()
