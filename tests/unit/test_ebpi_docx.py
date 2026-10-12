"""Конвертер HTML redtext (EBPI) -> DOCX. Фрагменты ниже — упрощённые, но по той же
разметке, что реальные документы (см. ebpi_docx.py и docs/ebpi-coverage.md)."""

from __future__ import annotations

from pathlib import Path

from docx import Document

from npa_pipeline.ebpi_docx import convert_redtext_to_docx

SIMPLE_HTML = """
<html><body>
<div id="topText"><div>Шапка просмотрщика — не часть текста</div></div>
<p class="T" id="p1">ПРАВИТЕЛЬСТВО РОССИЙСКОЙ ФЕДЕРАЦИИ</p>
<p id="p2">В соответствии со статьей 31<span class="W9">1</span> Федерального закона
постановляет: <span class="cmd-hide" cmdprm="gohash=abc">"Об охране окружающей среды"</span></p>
<p class="I">1.&nbsp;Утвердить прилагаемые Правила.</p>
<p class="Y" id="p82">Председатель Правительства<br>Российской Федерации М.Мишустин</p>
</body></html>
"""

TABLE_HTML = """
<html><body>
<table id="t1"><tbody>
<tr><td rowspan="2">Категория</td><td colspan="2">Показатели</td></tr>
<tr><td>ХПК</td><td>БПК5</td></tr>
<tr><td><p class="L">Сверхмалые -</p><p class="L">небольшие</p></td><td>10</td><td>5</td></tr>
</tbody></table>
</body></html>
"""


def test_skips_viewer_header_and_keeps_document_text(tmp_path: Path):
    out = tmp_path / "out.docx"
    convert_redtext_to_docx(SIMPLE_HTML, out)
    doc = Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    assert not any("Шапка просмотрщика" in t for t in texts)
    assert "ПРАВИТЕЛЬСТВО РОССИЙСКОЙ ФЕДЕРАЦИИ" in texts
    assert any("Председатель Правительства" in t for t in texts)


def test_superscript_footnote_and_cross_reference_text(tmp_path: Path):
    out = tmp_path / "out.docx"
    convert_redtext_to_docx(SIMPLE_HTML, out)
    doc = Document(str(out))
    p2 = next(p for p in doc.paragraphs if "статьей 31" in p.text)
    # сноска "1" сохранена как текст (надстрочным run'ом), не потеряна
    assert "статьей 311 Федерального закона" in p2.text.replace("\n", "")
    # перекрёстная ссылка (cmd-hide) развёрнута как обычный видимый текст
    assert '"Об охране окружающей среды"' in p2.text
    footnote_run = next(r for r in p2.runs if r.text == "1")
    assert footnote_run.font.superscript is True


def test_paragraph_alignment_by_class(tmp_path: Path):
    out = tmp_path / "out.docx"
    convert_redtext_to_docx(SIMPLE_HTML, out)
    doc = Document(str(out))
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    title = next(p for p in doc.paragraphs if "ПРАВИТЕЛЬСТВО" in p.text)
    assert title.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert title.runs[0].bold is True


def test_table_merges_rowspan_and_colspan_without_losing_text(tmp_path: Path):
    out = tmp_path / "out.docx"
    convert_redtext_to_docx(TABLE_HTML, out)
    doc = Document(str(out))
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert len(table.rows) == 3
    assert len(table.columns) == 3
    # "Категория" (rowspan=2) видна из обеих строк первого столбца — объединена
    assert table.cell(0, 0).text == table.cell(1, 0).text == "Категория"
    # "Показатели" (colspan=2) видна из обоих столбцов второй строки шапки
    assert table.cell(0, 1).text == table.cell(0, 2).text == "Показатели"
    assert table.cell(1, 1).text == "ХПК"
    assert table.cell(1, 2).text == "БПК5"
    # ячейка с двумя <p> — обе строки на месте, текст не потерян
    assert "Сверхмалые -" in table.cell(2, 0).text
    assert "небольшие" in table.cell(2, 0).text
    assert table.cell(2, 1).text == "10"
    assert table.cell(2, 2).text == "5"


def test_empty_table_is_skipped_without_crashing(tmp_path: Path):
    out = tmp_path / "out.docx"
    convert_redtext_to_docx("<html><body><p>текст</p><table></table></body></html>", out)
    doc = Document(str(out))
    assert len(doc.tables) == 0
    assert any("текст" in p.text for p in doc.paragraphs)
