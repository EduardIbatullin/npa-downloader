"""Предпросмотр .docx в браузере в виде HTML.

Браузеры не показывают .docx во вкладке (в отличие от PDF): с
``Content-Disposition: inline`` файл всё равно скачивается. Поэтому кнопка
«Открыть» показывает HTML, собранный из .docx при каждом запросе; на диске
хранится только .docx.

Это приближённая обратная операция к
:func:`npa_pipeline.ebpi_docx.convert_redtext_to_docx`: сохраняются
выравнивание, полужирный шрифт и таблицы — достаточно для предпросмотра, но не
для повторной публикации.
"""

from __future__ import annotations

import html as html_lib
from pathlib import Path
from typing import Iterator, Union

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

_ALIGN_CSS = {
    WD_ALIGN_PARAGRAPH.CENTER: "center",
    WD_ALIGN_PARAGRAPH.RIGHT: "right",
    WD_ALIGN_PARAGRAPH.JUSTIFY: "justify",
}

_STYLE = """
body { font-family: Georgia, 'Times New Roman', serif; max-width: 860px;
       margin: 2rem auto; padding: 0 1rem; line-height: 1.5; color: #1a1a1a; }
p { margin: 0 0 0.6em 0; }
table { border-collapse: collapse; margin: 1em 0; width: 100%; }
td { border: 1px solid #999; padding: 4px 8px; vertical-align: top; }
"""


def _iter_block_items(document: Document) -> Iterator[Union[Paragraph, Table]]:
    """Абзацы и таблицы в порядке документа.

    python-docx не даёт их вместе, поэтому обходятся дочерние элементы ``body``.
    """
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _paragraph_html(paragraph: Paragraph) -> str:
    """HTML абзаца: текст, полужирный шрифт, выравнивание и левый отступ."""
    text = "".join(html_lib.escape(run.text) for run in paragraph.runs) or html_lib.escape(paragraph.text)
    if not text.strip():
        return "<p>&nbsp;</p>"
    bold = any(run.bold for run in paragraph.runs if run.bold)
    if bold:
        text = f"<strong>{text}</strong>"
    styles = []
    align = _ALIGN_CSS.get(paragraph.alignment)
    if align:
        styles.append(f"text-align:{align}")
    indent = paragraph.paragraph_format.left_indent
    if indent is not None:
        styles.append(f"margin-left:{indent.inches * 96:.0f}px")
    style_attr = f' style="{";".join(styles)}"' if styles else ""
    return f"<p{style_attr}>{text}</p>"


def _table_html(table: Table) -> str:
    """HTML таблицы; объединённые ячейки выводятся один раз."""
    rows_html = []
    for row in table.rows:
        seen: set[int] = set()
        cells_html = []
        for cell in row.cells:
            cell_id = id(cell._tc)  # python-docx отдаёт тот же объект для
            if cell_id in seen:  # объединённых (merge) ячеек — не дублируем
                continue
            seen.add(cell_id)
            inner = "".join(_paragraph_html(p) for p in cell.paragraphs)
            cells_html.append(f"<td>{inner}</td>")
        rows_html.append(f"<tr>{''.join(cells_html)}</tr>")
    return f"<table>{''.join(rows_html)}</table>"


def render_docx_preview_html(path: Path) -> str:
    """Собирает HTML-страницу из .docx для показа в браузере по кнопке «Открыть».

    Сохраняются выравнивание, полужирный шрифт и таблицы (см. описание модуля).

    Args:
        path: Файл .docx.

    Returns:
        HTML-страница.

    Raises:
        Exception: Файл не является .docx (например, старый двоичный .doc);
            вызывающий код отдаёт тогда сам файл.
    """
    document = Document(str(path))
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\">",
        f"<style>{_STYLE}</style></head><body>",
    ]
    for block in _iter_block_items(document):
        if isinstance(block, Paragraph):
            parts.append(_paragraph_html(block))
        elif isinstance(block, Table):
            parts.append(_table_html(block))
    parts.append("</body></html>")
    return "".join(parts)
