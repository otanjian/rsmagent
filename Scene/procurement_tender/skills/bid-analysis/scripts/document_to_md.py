"""
将 PDF/DOC/DOCX 文档转换为 Markdown 文本。

供招投标辅助工作台在上传招标文件后、发送到聊天窗口前调用，
减少大模型侧重复解析文档的开销。
"""
import os
import re
from typing import Dict, Any


def convert_document_to_md(file_path: str, output_dir: str | None = None) -> Dict[str, Any]:
    """
    将 PDF 或 Word 文档转换为 Markdown 文件。

    Args:
        file_path: 文件绝对路径
        output_dir: Markdown 输出目录，默认与源文件同目录

    Returns:
        {"status": "success", "markdown_path": str, "filename": str}
        或
        {"status": "error", "message": str}
    """
    if not file_path or not os.path.isfile(file_path):
        return {"status": "error", "message": f"文件不存在: {file_path}"}

    ext = os.path.splitext(file_path)[1].lower()
    filename = os.path.basename(file_path)

    try:
        if ext == ".docx":
            md = _convert_docx(file_path)
        elif ext == ".doc":
            md = _convert_doc(file_path)
        elif ext == ".pdf":
            md = _convert_pdf(file_path)
        else:
            return {"status": "error", "message": f"不支持的文件格式: {ext}"}

        md = _clean_markdown(md)
        if not md:
            return {"status": "error", "message": "文档内容为空或无法提取文本"}

        out_dir = output_dir if output_dir else os.path.dirname(file_path)
        os.makedirs(out_dir, exist_ok=True)
        base_name = os.path.splitext(filename)[0]
        md_filename = base_name + ".md"
        md_path = os.path.join(out_dir, md_filename)

        # 避免覆盖时冲突，若已存在则追加序号
        counter = 1
        while os.path.exists(md_path):
            md_path = os.path.join(out_dir, f"{base_name}_{counter}.md")
            counter += 1

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)

        # 返回文件路径 + 一段预览文本，供工作台展示，不进入聊天消息。
        preview = md[:2000].strip()
        if len(md) > 2000:
            preview += "\n\n...（内容已截断，完整内容见 Markdown 文件）"

        return {
            "status": "success",
            "markdown_path": md_path,
            "filename": md_filename,
            "markdown_preview": preview,
        }
    except Exception as e:
        return {"status": "error", "message": f"文档转换失败: {str(e)}"}


def _convert_docx(file_path: str) -> str:
    """使用 python-docx 将 DOCX 转为 Markdown。"""
    from docx import Document

    doc = Document(file_path)
    lines = []

    for element in doc.element.body:
        tag = element.tag.split("}")[-1]
        if tag == "p":
            paragraph = next((p for p in doc.paragraphs if p._element is element), None)
            if paragraph is None:
                continue
            text = paragraph.text.strip()
            if not text:
                continue
            style_name = paragraph.style.name.lower()
            if style_name.startswith("heading"):
                level_match = re.search(r"\d+", style_name)
                level = int(level_match.group()) if level_match else 1
                lines.append("#" * level + " " + text)
            elif "list" in style_name:
                if "number" in style_name:
                    lines.append("1. " + text)
                else:
                    lines.append("- " + text)
            else:
                lines.append(text)
        elif tag == "tbl":
            table = next((t for t in doc.tables if t._element is element), None)
            if table is not None:
                lines.append(_table_to_markdown(table))

    return "\n\n".join(lines)


def _convert_doc(file_path: str) -> str:
    """DOC 格式先尝试用 LibreOffice 转成 DOCX 再解析。"""
    import tempfile
    import subprocess

    output_dir = tempfile.mkdtemp(prefix="bid_analysis_doc_")
    try:
        result = subprocess.run(
            ["soffice", "--headless", "--convert-to", "docx", "--outdir", output_dir, file_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice 转换失败: {result.stderr}")

        base_name = os.path.splitext(os.path.basename(file_path))[0]
        converted = os.path.join(output_dir, base_name + ".docx")
        if not os.path.exists(converted):
            # LibreOffice 有时会在文件名后加数字
            candidates = [f for f in os.listdir(output_dir) if f.lower().endswith(".docx")]
            if not candidates:
                raise RuntimeError("LibreOffice 未生成 DOCX 文件")
            converted = os.path.join(output_dir, candidates[0])

        return _convert_docx(converted)
    finally:
        _rm_tree(output_dir)


def _convert_pdf(file_path: str) -> str:
    """将 PDF 转为 Markdown，优先使用 pdfplumber，回退到 pypdf/pdfminer。"""
    errors = []

    try:
        return _convert_pdf_with_pdfplumber(file_path)
    except Exception as e:
        errors.append(f"pdfplumber: {e}")

    try:
        return _convert_pdf_with_pypdf2(file_path)
    except Exception as e:
        errors.append(f"pypdf: {e}")

    try:
        return _convert_pdf_with_pdfminer(file_path)
    except Exception as e:
        errors.append(f"pdfminer: {e}")

    raise RuntimeError("; ".join(errors))


def _convert_pdf_with_pdfplumber(file_path: str) -> str:
    import pdfplumber

    chunks = []
    with pdfplumber.open(file_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            text = text.strip()
            if text:
                chunks.append(f"## 第 {i + 1} 页\n\n{text}")

            tables = page.extract_tables()
            for table in tables:
                if table:
                    chunks.append(_matrix_to_markdown_table(table))

    return "\n\n".join(chunks)


def _convert_pdf_with_pypdf2(file_path: str) -> str:
    import pypdf

    chunks = []
    with open(file_path, "rb") as f:
        reader = pypdf.PdfReader(f)
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            text = text.strip()
            if text:
                chunks.append(f"## 第 {i + 1} 页\n\n{text}")
    return "\n\n".join(chunks)


def _convert_pdf_with_pdfminer(file_path: str) -> str:
    from pdfminer.high_level import extract_text

    text = extract_text(file_path)
    return text.strip()


def _table_to_markdown(table) -> str:
    """将 python-docx Table 转为 Markdown 表格。"""
    rows = []
    for row in table.rows:
        cells = [cell.text.strip().replace("|", "\\|") for cell in row.cells]
        rows.append(cells)
    return _matrix_to_markdown_table(rows)


def _matrix_to_markdown_table(matrix) -> str:
    """将二维数组转为 Markdown 表格。"""
    if not matrix:
        return ""

    # 统一列数
    col_count = max(len(row) for row in matrix)
    normalized = []
    for row in matrix:
        cells = [str(cell).strip().replace("|", "\\|") if cell is not None else "" for cell in row]
        cells += [""] * (col_count - len(cells))
        normalized.append(cells)

    if len(normalized) < 2:
        normalized.append([""] * col_count)

    lines = []
    lines.append("| " + " | ".join(normalized[0]) + " |")
    lines.append("| " + " | ".join(["---"] * col_count) + " |")
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


def _clean_markdown(md: str) -> str:
    """清理 Markdown 文本中的多余空行和异常字符。"""
    # 合并连续空行
    md = re.sub(r"\n{3,}", "\n\n", md)
    # 删除每行末尾空白
    md = "\n".join(line.rstrip() for line in md.split("\n"))
    return md.strip()


def _rm_tree(path: str) -> None:
    import shutil
    try:
        shutil.rmtree(path)
    except Exception:
        pass


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print(json.dumps({"status": "error", "message": "Usage: python document_to_md.py <file_path>"}, ensure_ascii=False))
        sys.exit(1)

    result = convert_document_to_md(sys.argv[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
