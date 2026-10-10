# encoding:utf-8
"""The batch PDF text CLI shares the read tool's OCR fallback.

``read`` answers one document at a time; matching a directory of scanned
declarations wants one command. These tests pin that the CLI walks
directories, marks recognised pages, writes one file per document with
``--out-dir``, and reports rather than crashes on an unreadable page.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.tools.utils import pdf_scan  # noqa: E402


def _load_script():
    path = os.path.join(ROOT, "scripts", "pdf_text.py")
    spec = importlib.util.spec_from_file_location("pdf_text_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _image_only_pdf(path, pages=1):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=300)
    with open(path, "wb") as f:
        writer.write(f)
    return path


class TestPageRangeParsing(unittest.TestCase):
    def setUp(self):
        self.mod = _load_script()

    def test_single_page(self):
        self.assertEqual(self.mod._parse_pages("3"), (3, 3))

    def test_closed_range(self):
        self.assertEqual(self.mod._parse_pages("1-5"), (1, 5))

    def test_open_ended_range(self):
        self.assertEqual(self.mod._parse_pages("4-"), (4, None))
        self.assertEqual(self.mod._parse_pages("-6"), (1, 6))

    def test_default_is_the_whole_document(self):
        self.assertEqual(self.mod._parse_pages(""), (1, None))


class TestExtraction(unittest.TestCase):
    def setUp(self):
        self.mod = _load_script()
        self.work = tempfile.mkdtemp()
        self._real = pdf_scan.ocr_pages

    def tearDown(self):
        pdf_scan.ocr_pages = self._real

    def _stub(self, mapping, error=None):
        def fake(path, page_numbers, scale=pdf_scan.DEFAULT_SCALE, timeout_pages=pdf_scan.MAX_OCR_PAGES):
            return ({p: mapping[p] for p in page_numbers if p in mapping}, error)
        pdf_scan.ocr_pages = fake

    def test_scanned_page_is_recognised_and_labelled(self):
        path = _image_only_pdf(os.path.join(self.work, "a.pdf"))
        self._stub({1: "报关单号 371320261000065880"})

        record = self.mod.extract_one(path, 1, None, True, 2.0)

        self.assertEqual(record["ocr_pages"], [1])
        self.assertIn("371320261000065880", record["text"])
        self.assertIn("(OCR)", record["text"])
        self.assertIsNone(record["error"])

    def test_no_ocr_reports_pages_instead_of_recognising(self):
        path = _image_only_pdf(os.path.join(self.work, "b.pdf"))

        def explode(*a, **k):
            raise AssertionError("--no-ocr must not call the recogniser")

        pdf_scan.ocr_pages = explode
        record = self.mod.extract_one(path, 1, None, False, 2.0)

        self.assertEqual(record["ocr_pages"], [])
        self.assertEqual(record["pages_without_text"], [1])
        self.assertIn("--no-ocr", record["error"])

    def test_partial_failure_is_reported_not_raised(self):
        path = _image_only_pdf(os.path.join(self.work, "c.pdf"), pages=3)
        self._stub({1: "page one"}, error="page 2 could not be recognised: boom")

        record = self.mod.extract_one(path, 1, 3, True, 2.0)

        self.assertIn("page one", record["text"])
        self.assertEqual(record["ocr_pages"], [1])
        self.assertEqual(record["pages_without_text"], [2, 3])
        self.assertIn("boom", record["error"])

    def test_collect_pdfs_walks_directories(self):
        nested = os.path.join(self.work, "sub")
        os.makedirs(nested)
        _image_only_pdf(os.path.join(self.work, "one.pdf"))
        _image_only_pdf(os.path.join(nested, "two.pdf"))
        with open(os.path.join(self.work, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("not a pdf")

        found = self.mod._collect_pdfs([self.work])

        self.assertEqual(sorted(os.path.basename(p) for p in found), ["one.pdf", "two.pdf"])


class TestCli(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.out = os.path.join(self.work, "out")
        self._real = pdf_scan.ocr_pages

    def tearDown(self):
        pdf_scan.ocr_pages = self._real

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "pdf_text.py"), *args],
            capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
        )

    def test_out_dir_writes_one_file_per_document(self):
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
        content.set_data(b'BT /F1 12 Tf 20 250 Td (Invoice Total 900) Tj ET')
        page[NameObject('/Contents')] = content
        pdf = os.path.join(self.work, "doc.pdf")
        with open(pdf, "wb") as f:
            writer.write(f)

        result = self._run(pdf, "--out-dir", self.out, "--no-ocr")

        self.assertEqual(result.returncode, 0, result.stderr)
        written = os.path.join(self.out, "doc.txt")
        self.assertTrue(os.path.exists(written), os.listdir(self.out))
        with open(written, encoding="utf-8") as f:
            self.assertIn("Invoice Total 900", f.read())

    def test_json_mode_reports_each_document(self):
        path = _image_only_pdf(os.path.join(self.work, "scan.pdf"))

        result = self._run(path, "--json", "--no-ocr")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["pages_without_text"], [1])
        self.assertEqual(payload[0]["ocr_pages"], [])


if __name__ == "__main__":
    unittest.main()
