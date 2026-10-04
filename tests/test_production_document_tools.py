"""Keep both existing document fallback paths working with maintained pypdf."""
import importlib.util
from pathlib import Path
import sys


def _load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, path,
        submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_existing_pdf_fallbacks_extract_actual_text(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    content = DecodedStreamObject()
    content.set_data(b'BT /F1 12 Tf 20 250 Td (Revenue 12345) Tj ET')
    page[NameObject('/Contents')] = content
    pdf = tmp_path / 'report.pdf'
    writer.write(pdf)
    root = Path(__file__).resolve().parents[1]
    converter = _load('production_document_to_md', root / 'Scene/procurement_tender/skills/bid-analysis/scripts/document_to_md.py')
    assert 'Revenue 12345' in converter._convert_pdf_with_pypdf2(str(pdf))
    directory = root / 'Scene/finance_report_audit/skills/financial-reprot-audit/parsers'
    _load('production_finance_parsers', directory / '__init__.py', package=True)
    parser = _load('production_finance_parsers.pdf_parser', directory / 'pdf_parser.py')
    result = parser.PDFParser()._parse_with_pypdf2(str(pdf))
    assert result.success
