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
    """Проверяет, есть ли у PDF текстовый слой на всех страницах.

    Использует тот же порог, что OCR (см.
    ocr_jobs.count_scan_pages/DEFAULT_MIN_PAGE_TEXT_CHARS) — так что результат
    согласован с тем, какие страницы npa-ocr посчитал бы сканами.

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

    Переносит текст постранично: внутри страницы — по одному абзацу на строку
    (пустые строки пропускаются), между страницами PDF — разрыв страницы. Не
    распознаёт структуру/таблицы (этим занимается EBPI, когда покрывает документ,
    см. docs/ebpi-coverage.md) — только то, что уже лежит в текстовом слое PDF.

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
