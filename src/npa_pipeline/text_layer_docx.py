"""PDF с полным текстовым слоем (на всех страницах) -> .docx простым механическим
извлечением текста (pypdf), без OCR и без ИИ в рантайме (см. README).

Резервный путь для документов, которые EBPI не покрывает (обычно ведомственные
приказы, см. npa_pipeline.ebpi) — но чей PDF не скан: текст уже лежит в файле,
распознавать нечего, OCR был бы избыточен. Для PDF со сканами (хотя бы одна
страница без текстового слоя) не подходит — это задача внешнего npa-ocr, см.
docs/ocr-integration.md.

Чисто механическое извлечение — переносит текст страниц как есть, без попытки
распознать структуру/таблицы (этим занимается EBPI, когда покрывает документ, см.
docs/ebpi-coverage.md). Для приказов с таблицами в тексте результат будет хуже, чем
EBPI, но лучше, чем полное отсутствие .docx.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from pypdf import PdfReader

from npa_pipeline.ocr_jobs import count_scan_pages


def has_full_text_layer(pdf_path: Path | str) -> bool:
    """True, если у PDF есть хотя бы одна страница и ни одной без текстового слоя
    (тот же порог, что у OCR — см. ocr_jobs.count_scan_pages/DEFAULT_MIN_PAGE_TEXT_CHARS).
    False для пустого PDF или PDF со сканами — там нужен OCR, не это извлечение."""
    total, scan = count_scan_pages(pdf_path)
    return total > 0 and scan == 0


def extract_text_layer_to_docx(pdf_path: Path | str, out_path: Path | str) -> None:
    """Построчно переносит текстовый слой каждой страницы PDF в .docx (разрыв
    страницы между страницами PDF, пустые строки пропускаются)."""
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
