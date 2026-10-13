"""Механическое извлечение текстового слоя PDF в .docx — резервный путь, когда
EBPI документ не покрывает, но PDF не скан (см. npa_pipeline.text_layer_docx)."""

from __future__ import annotations

from pathlib import Path

from docx import Document

from npa_pipeline.text_layer_docx import extract_text_layer_to_docx, has_full_text_layer


def _text_pdf_bytes(*page_texts: str) -> bytes:
    """Хэндкрафченный многостраничный PDF со стандартным шрифтом Helvetica — pypdf
    восстанавливает структуру даже с фиктивным startxref (проверено отдельно)."""
    objects = []
    kids = []
    next_obj = 3
    font_obj = None
    body = ""
    page_objs = []
    content_objs = []
    for i, _ in enumerate(page_texts):
        page_objs.append(next_obj)
        next_obj += 1
        content_objs.append(next_obj)
        next_obj += 1
    font_obj = next_obj

    parts = [
        "%PDF-1.4",
        "1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj",
        f"2 0 obj<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_objs)}] "
        f"/Count {len(page_objs)} >>endobj",
    ]
    for page_obj, content_obj, text in zip(page_objs, content_objs, page_texts):
        parts.append(
            f"{page_obj} 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> /Contents {content_obj} 0 R >>endobj"
        )
        stream = f"BT /F1 12 Tf 10 150 Td ({text}) Tj ET"
        parts.append(f"{content_obj} 0 obj<< /Length {len(stream)} >>\nstream\n{stream}\nendstream\nendobj")
    parts.append(f"{font_obj} 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj")
    parts.append(f"trailer<< /Size {font_obj + 1} /Root 1 0 R >>")
    parts.append("startxref")
    parts.append("0")
    parts.append("%%EOF")
    return ("\n".join(parts) + "\n").encode("latin-1")


def _scan_pdf_bytes() -> bytes:
    """Страница без текстового слоя (как сканы без OCR в проекте)."""
    return b"""%PDF-1.1
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>endobj
trailer<< /Size 4 /Root 1 0 R >>
startxref
0
%%EOF
"""


def test_has_full_text_layer_true_for_text_pdf(tmp_path: Path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_text_pdf_bytes("Page one has enough text content", "Page two has enough text content"))
    assert has_full_text_layer(pdf_path) is True


def test_has_full_text_layer_false_for_scan(tmp_path: Path):
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(_scan_pdf_bytes())
    assert has_full_text_layer(pdf_path) is False


def test_has_full_text_layer_false_when_one_page_is_blank(tmp_path: Path):
    # has_full_text_layer требует текстовый слой на ВСЕХ страницах — одна пустая
    # страница (скан в составе иначе текстового документа) делает его False,
    # это задача OCR, не механического извлечения.
    pdf_path = tmp_path / "mixed.pdf"
    pdf_path.write_bytes(_text_pdf_bytes("Page one has enough text content", ""))
    assert has_full_text_layer(pdf_path) is False


def test_extract_text_layer_to_docx_writes_pages_with_break(tmp_path: Path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_text_pdf_bytes("Page one has enough text content", "Page two has enough text content"))
    out_path = tmp_path / "doc.docx"

    extract_text_layer_to_docx(pdf_path, out_path)

    assert out_path.is_file()
    document = Document(str(out_path))
    full_text = "\n".join(p.text for p in document.paragraphs)
    assert "Page one has enough text content" in full_text
    assert "Page two has enough text content" in full_text


def test_extract_text_layer_to_docx_creates_parent_dir(tmp_path: Path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_text_pdf_bytes("Only page has enough text content"))
    out_path = tmp_path / "nested" / "doc.docx"

    extract_text_layer_to_docx(pdf_path, out_path)

    assert out_path.is_file()
