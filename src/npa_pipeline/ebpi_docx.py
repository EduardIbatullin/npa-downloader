"""Сборка .docx из HTML-текста EBPI (redtext).

redtext — тот же HTML, что сайт actual.pravo.gov.ru отдаёт по кнопке
«Сохранить в Word», заворачивая его в ``data:application/msword``. Здесь из него
собирается настоящий .docx через python-docx (см. docs/ebpi-coverage.md).

Поддерживаются классы абзацев и ячеек redtext, таблицы с объединёнными ячейками
и многоуровневой шапкой. Неизвестные теги верхнего уровня пропускаются, чтобы
один непредвиденный элемент не ронял сборку документа.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.exceptions import InvalidSpanError
from docx.shared import Inches

# p.T/p.Z — жирный заголовок по центру; p.C/p.S — просто по центру; p.R — по правому
# краю; p.J — по ширине явно; p.I/p.K/p.H/p.Y — отступ слева (H — ещё и жирным, это
# «висячий» подзаголовок вроде вводной фразы перед перечислением).
_CENTER_CLASSES = {"T", "Z", "C", "S"}
_RIGHT_CLASSES = {"R"}
_BOLD_CLASSES = {"T", "Z", "H"}
_LEFT_INDENT_CLASSES = {"I", "K", "H", "Y"}

_PX_PER_INCH = 96
_LEFT_INDENT_PX = 45  # margin-left в CSS redtext


def convert_redtext_to_docx(html: str, out_path: Path) -> None:
    """Собирает .docx из HTML redtext EBPI и сохраняет его в out_path.

    Теги верхнего уровня, кроме ``<p>`` и ``<table>``, пропускаются.

    Args:
        html: redtext, как его отдаёт EBPI (см. npa_pipeline.ebpi.fetch_redtext) —
            HTML в стиле Word-экспорта, не настоящий .docx.
        out_path: куда сохранить результат.

    Raises:
        ValueError: html не похож на redtext EBPI (нет тега `<body>`).
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if body is None:
        raise ValueError("В HTML нет <body> — не похоже на redtext EBPI")

    doc = Document()
    for child in body.children:
        if not isinstance(child, Tag):
            continue
        if child.get("id") == "topText":
            continue  # служебная шапка просмотрщика сайта, не часть текста документа
        if child.name == "p":
            _add_paragraph(doc, child)
        elif child.name == "table":
            _add_table(doc, child)
        # прочие теги верхнего уровня пропускаются
    doc.save(str(out_path))


def _classes(tag: Tag) -> set[str]:
    """CSS-классы тега."""
    cls = tag.get("class") or []
    if isinstance(cls, str):
        return {cls}
    return set(cls)


def _add_paragraph(doc: Document, p_tag: Tag) -> None:
    """Добавляет абзац с выравниванием, отступом и начертанием по классу ``<p>``."""
    classes = _classes(p_tag)
    paragraph = doc.add_paragraph()
    if classes & _CENTER_CLASSES:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif classes & _RIGHT_CLASSES:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    elif "J" in classes or not (classes & _LEFT_INDENT_CLASSES):
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if classes & _LEFT_INDENT_CLASSES:
        paragraph.paragraph_format.left_indent = Inches(_LEFT_INDENT_PX / _PX_PER_INCH)
    _add_inline(paragraph, p_tag, bold_default=bool(classes & _BOLD_CLASSES))


def _add_inline(paragraph, node: Tag, *, bold_default: bool = False, superscript: bool = False) -> None:
    """Добавляет в абзац текст узла прогонами с полужирным и надстрочным начертанием.

    Args:
        paragraph: Абзац python-docx.
        node: Узел HTML.
        bold_default: Полужирный шрифт по умолчанию.
        superscript: Надстрочный шрифт.
    """
    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text:
                run = paragraph.add_run(text)
                run.bold = bold_default
                run.font.superscript = superscript
            continue
        if not isinstance(child, Tag):
            continue
        if child.name == "br":
            paragraph.add_run().add_break()
            continue
        child_classes = _classes(child)
        # W9 — надстрочная сноска; W4/W5/W6/W7 — полужирный; курсив и подчёркивание
        # не переносятся
        _add_inline(
            paragraph,
            child,
            bold_default=bold_default or bool(child_classes & {"W4", "W5", "W6", "W7"}),
            superscript=superscript or "W9" in child_classes,
        )


def _add_table(doc: Document, table_tag: Tag) -> None:
    """Добавляет таблицу с объединениями ячеек.

    Непрямоугольное объединение не выполняется: ячейка остаётся отдельной, текст
    не теряется.
    """
    rows = table_tag.find_all("tr", recursive=False)
    tbody = table_tag.find("tbody", recursive=False)
    if not rows and tbody is not None:
        rows = tbody.find_all("tr", recursive=False)
    if not rows:
        return
    n_cols = max(
        sum(int(td.get("colspan", 1)) for td in tr.find_all(["td", "th"], recursive=False))
        for tr in rows
    )
    if n_cols == 0:
        return

    table = doc.add_table(rows=len(rows), cols=n_cols)
    table.style = "Table Grid"
    occupied: set[tuple[int, int]] = set()
    for r, tr in enumerate(rows):
        c = 0
        for td in tr.find_all(["td", "th"], recursive=False):
            while (r, c) in occupied and c < n_cols:
                c += 1
            if c >= n_cols:
                break  # геометрия строки не укладывается в расчётное число столбцов
            colspan = int(td.get("colspan", 1))
            rowspan = int(td.get("rowspan", 1))
            _fill_cell(table.cell(r, c), td)
            r2, c2 = r + rowspan - 1, min(c + colspan, n_cols) - 1
            if (r2, c2) != (r, c):
                try:
                    table.cell(r, c).merge(table.cell(r2, c2))
                except InvalidSpanError:
                    pass  # непрямоугольное объединение: ячейка остаётся отдельной
            for rr in range(r, r2 + 1):
                for cc in range(c, c2 + 1):
                    occupied.add((rr, cc))
            c = c2 + 1


def _fill_cell(cell, td: Tag) -> None:
    """Заполняет ячейку абзацами из ``<td>``."""
    cell.paragraphs[0].text = ""
    inner_paragraphs = td.find_all("p", recursive=False)
    if not inner_paragraphs:
        _add_inline(cell.paragraphs[0], td)
        return
    for i, p in enumerate(inner_paragraphs):
        target = cell.paragraphs[0] if i == 0 else cell.add_paragraph()
        _add_inline(target, p)
