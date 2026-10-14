"""Тесты человекочитаемых имён файлов и описаний."""

from datetime import date

from npa_pipeline.models import DocItem
from npa_pipeline.naming import (
    format_document_title,
    human_filename_stem,
    human_filename_stem_from_text,
)


def test_filename_from_complex_name_government_style():
    doc = DocItem(
        eo_number="0001201906010001",
        number="691",
        document_date=date(2019, 5, 31),
        signatory_ids=["x"],
        complex_name=(
            'Постановление Правительства Российской Федерации от 31.05.2019 № 691\n'
            ' "О чём-то"'
        ),
    )
    assert human_filename_stem(doc) == "Постановление Правительства РФ от 31_05_2019 N 691"


def test_filename_abbreviates_minprirody():
    doc = DocItem(
        eo_number="0001201609130025",
        number="402",
        document_date=date(2016, 7, 19),
        signatory_ids=["x"],
        complex_name=(
            "Приказ Министерства природных ресурсов и экологии Российской Федерации "
            "от 19.07.2016 № 402\n "
            '"Об утверждении Порядка"'
        ),
    )
    stem = human_filename_stem(doc)
    assert stem == "Приказ Минприроды России от 19_07_2016 N 402"
    assert "Министерства" not in stem
    assert "№" not in stem


def test_filename_fallback_without_complex_name():
    doc = DocItem(
        eo_number="0001",
        number="12-п",
        document_date=date(2020, 1, 15),
        signatory_ids=["x"],
    )
    assert human_filename_stem(doc) == "Документ от 15_01_2020 N 12-п"


def test_filename_strips_forbidden_chars():
    doc = DocItem(
        eo_number="1",
        number="1/2",
        document_date=date(2020, 1, 1),
        signatory_ids=["x"],
        complex_name="Приказ Органа от 01.01.2020 № 1/2",
    )
    stem = human_filename_stem(doc)
    assert stem == "Приказ Органа от 01_01_2020 N 1_2"
    assert "/" not in stem
    assert "№" not in stem


def test_human_filename_stem_from_text_same_sanitizing_as_doc_item():
    """Источник без DocItem (ips.pravo.gov.ru) — та же обработка (сокращения,
    №→N, даты, запрещённые символы), что у human_filename_stem."""
    stem = human_filename_stem_from_text(
        "Федеральный закон от 10.01.2002 № 7-ФЗ", fallback="1000000000102074303"
    )
    assert stem == "Федеральный закон от 10_01_2002 N 7-ФЗ"
    assert "№" not in stem


def test_human_filename_stem_from_text_uses_fallback_when_empty():
    assert human_filename_stem_from_text("", fallback="nd-123") == "nd-123"
    assert human_filename_stem_from_text(None, fallback="nd-123") == "nd-123"


def test_format_document_title_includes_requisites_and_theme():
    text = format_document_title(
        name=(
            "Об утверждении нормативного документа в области охраны окружающей среды "
            "«Технологические показатели…»"
        ),
        complex_name=(
            "Приказ Министерства природных ресурсов и экологии Российской Федерации "
            "от 07.04.2026 № 191\n "
            '"Об утверждении нормативного документа…"'
        ),
        number="191",
        document_date="2026-04-07",
    )
    assert text.startswith(
        "Приказ Министерства природных ресурсов и экологии Российской Федерации "
        "от 07.04.2026 № 191"
    )
    assert "Об утверждении" in text
