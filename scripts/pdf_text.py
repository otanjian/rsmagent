#!/usr/bin/env python3
"""Extract text from PDFs, recognising pages that have no text layer.

The ``read`` tool covers one document at a time. Matching a batch of scanned
declarations, invoices or contracts against a list is a different job: it wants
one command over a directory, with the recognised text either on stdout or in
one file per document.

Same engine as ``read`` (see agent.tools.utils.pdf_scan): pypdf for the text
layer, PDFium rasterisation + RapidOCR for pages that are images.

Examples
--------
    # every PDF under a directory, headers on stdout
    python scripts/pdf_text.py "C:/path/uploads"

    # one .txt beside each result, machine-readable summary on stdout
    python scripts/pdf_text.py "C:/path/uploads" --out-dir "C:/path/text"

    # just the first two pages of each file, as JSON
    python scripts/pdf_text.py a.pdf b.pdf --pages 1-2 --json

    # text layer only (no OCR), for a quick "which of these are scans" check
    python scripts/pdf_text.py "C:/path/uploads" --no-ocr --json
"""
import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.tools.utils import pdf_scan  # noqa: E402


def _parse_pages(spec):
    """Parse "3", "1-5" or the open-ended "4-" into (first, last_or_None)."""
    spec = (spec or "").strip()
    if not spec:
        return 1, None
    if "-" in spec:
        head, _, tail = spec.partition("-")
        return (int(head) if head.strip() else 1,
                int(tail) if tail.strip() else None)
    return int(spec), int(spec)


def _collect_pdfs(inputs):
    found = []
    for item in inputs:
        if os.path.isdir(item):
            found.extend(sorted(glob.glob(os.path.join(item, "**", "*.pdf"), recursive=True)))
        elif os.path.isfile(item):
            found.append(item)
        else:
            print(f"skip (not found): {item}", file=sys.stderr)
    return found


def extract_one(path, first, last, ocr, scale):
    """Return {path, pages, ocr_pages, text, error} for one PDF."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pypdf is required: pip install pypdf")

    reader = PdfReader(path)
    total = len(reader.pages)
    last_page = total if last is None else min(last, total)
    first = max(1, min(first, total)) if total else 1

    page_texts, ocr_pages, missing = {}, [], []
    for page_num in range(first, last_page + 1):
        text = reader.pages[page_num - 1].extract_text()
        if text and text.strip():
            page_texts[page_num] = text
        else:
            missing.append(page_num)

    error = None
    if missing and ocr:
        recognised, error = pdf_scan.ocr_pages(path, missing, scale=scale)
        for page_num, text in recognised.items():
            if text and text.strip():
                page_texts[page_num] = text
                ocr_pages.append(page_num)
    elif missing and not ocr:
        error = "OCR disabled (--no-ocr)"

    parts = [
        f"--- Page {page_num}{' (OCR)' if page_num in ocr_pages else ''} ---\n{page_texts[page_num]}"
        for page_num in sorted(page_texts)
    ]
    return {
        "path": os.path.abspath(path),
        "total_pages": total,
        "pages_read": f"{first}-{last_page}",
        "ocr_pages": ocr_pages,
        "pages_without_text": [p for p in missing if p not in ocr_pages],
        "text": "\n\n".join(parts),
        "error": error,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", help="PDF files or directories to walk")
    parser.add_argument("--pages", default="", help='page range, e.g. "1-5" or "3-"')
    parser.add_argument("--scale", type=float, default=pdf_scan.DEFAULT_SCALE,
                        help=f"rasterisation scale (default {pdf_scan.DEFAULT_SCALE})")
    parser.add_argument("--no-ocr", action="store_true",
                        help="text layer only; pages without text are reported, not recognised")
    parser.add_argument("--json", action="store_true", help="emit a JSON array of results")
    parser.add_argument("--out-dir", default="",
                        help="write one .txt per PDF here instead of printing the text")
    args = parser.parse_args()

    first, last = _parse_pages(args.pages)
    ocr = not args.no_ocr

    if ocr:
        ok, reason = pdf_scan.ocr_available()
        if not ok:
            print(f"warning: OCR unavailable ({reason}); continuing with the text layer only",
                  file=sys.stderr)
            ocr = False

    paths = _collect_pdfs(args.inputs)
    if not paths:
        print("no PDFs found", file=sys.stderr)
        return 1

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)

    results = []
    for index, path in enumerate(paths, 1):
        print(f"[{index}/{len(paths)}] {os.path.basename(path)}", file=sys.stderr)
        try:
            record = extract_one(path, first, last, ocr, args.scale)
        except Exception as error:  # noqa: BLE001 - one bad file must not stop the batch
            record = {"path": os.path.abspath(path), "text": "", "error": str(error),
                      "ocr_pages": [], "pages_without_text": []}
        results.append(record)

        if args.out_dir:
            stem = os.path.splitext(os.path.basename(path))[0] + ".txt"
            with open(os.path.join(args.out_dir, stem), "w", encoding="utf-8") as f:
                f.write(record.get("text", ""))
        elif not args.json:
            print(f"\n===== {path} =====")
            if record.get("ocr_pages"):
                print(f"[OCR pages: {record['ocr_pages']}]")
            print(record.get("text") or "[no text extracted]")
            if record.get("error"):
                print(f"[{record['error']}]")

    if args.json:
        # Keeping the recognised text in the JSON lets one call answer a whole
        # batch; --out-dir is the alternative when that would be too large.
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        recognised = sum(len(r.get("ocr_pages") or []) for r in results)
        failed = [r for r in results if r.get("error")]
        print(f"\n{len(results)} PDF(s); {recognised} page(s) read by OCR; "
              f"{len(failed)} with an error", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
