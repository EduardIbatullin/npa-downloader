"""Сборка .docx из текстового слоя PDF (pypdf), без OCR.

Запасной путь для документов, которых нет в EBPI (обычно ведомственные приказы,
см. :mod:`npa_pipeline.ebpi`), но PDF которых не скан. PDF со сканами (хотя бы
одна страница без текстового слоя) распознаёт npa-ocr (см.
docs/ocr-integration.md).

Текст страниц переносится как есть, без структуры и таблиц, поэтому для
документов с таблицами результат хуже, чем у EBPI.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from pypdf import PdfReader

from npa_pipeline.ocr_jobs import count_scan_pages


def has_full_text_layer(pdf_path: Path | str) -> bool:
    """Проверяет, есть ли у PDF текстовый слой на всех страницах.

    Порог тот же, что у npa-ocr (см. :func:`npa_pipeline.ocr_jobs.count_scan_pages`),
    поэтому результат согласован с тем, какие страницы npa-ocr считает сканами.

    Args:
        pdf_path: путь к PDF-файлу.

    Returns:
        True — документ не скан, extract_text_layer_to_docx даст полный текст.
        False — пустой PDF или хотя бы одна страница без текстового слоя; тогда
        нужен OCR (внешний npa-ocr, см. docs/ocr-integration.md), не это извлечение.
    """
    total, scan = count_scan_pages(pdf_path)
    return total > 0 and scan == 0


def extract_text_layer_to_docx(pdf_path: Path | str, out_path: Path | str) -> None:
    """Извлекает текстовый слой PDF и сохраняет его как .docx.

    Каждая непустая строка становится абзацем, между страницами PDF —
    разрыв страницы. Структура и таблицы не распознаются.

    Args:
        pdf_path: путь к исходному PDF. Предполагается, что has_full_text_layer()
            уже вернул True для него — сама функция это не проверяет и не бросает
            исключение, если в PDF встретится страница-скан, просто не добавит
            для неё ни одного абзаца.
        out_path: путь для .docx; родительская директория создаётся при
            необходимости.
    """
    reader = PdfReader(str(pdf_path))
    document = Document()
    last_index = len(reader.pages) - 1
    for index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        for line in text.splitlines():
            if line.strip():
                document.add_paragraph(line.strip())
        if index < last_index:
            document.add_page_break()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(out_path))
