"""Тесты разбора полного названия НПА → реквизиты."""

from datetime import date

import pytest

from npa_pipeline.parse_citation import parse_citation, query_from_citation


def test_parse_federal_law_219_processed_example():
    """Документ, уже скачанный на стенде (219-ФЗ)."""
    text = (
        'Федеральный закон от 21.07.2014 № 219-ФЗ\n'
        '"О внесении изменений в Федеральный закон "Об охране окружающей среды" '
        'и отдельные законодательные акты Российской Федерации"\n'
    )
    parsed = parse_citation(text)
    assert parsed.number == "219-ФЗ"
    assert parsed.date == date(2014, 7, 21)
    assert parsed.authority_name == "Президент Российской Федерации"
    assert parsed.doc_type == "федеральный закон"


def test_parse_federal_law_310_golden():
    text = (
        'Федеральный закон от 31.07.2025 № 310-ФЗ\n'
        ' "О внесении изменений в Федеральный закон '
        '"Об инновационных научно-технологических центрах…"'
    )
    parsed = parse_citation(text)
    assert parsed.number == "310-ФЗ"
    assert parsed.date == date(2025, 7, 31)
    assert parsed.authority_name == "Президент Российской Федерации"


def test_parse_collapses_accidentally_duplicated_ot():
    """Опечатка «от от» (встретилась на практике при ручном вводе) не должна
    ломать разбор — см. _DUPLICATE_OT в parse_citation.py."""
    parsed = parse_citation("Федеральный закон от от 20.03.2025 № 33-ФЗ")
    assert parsed.number == "33-ФЗ"
    assert parsed.date == date(2025, 3, 20)
    assert parsed.authority_name == "Президент Российской Федерации"
    assert parsed.doc_type == "федеральный закон"


def test_parse_prikaz_genitive_authority():
    text = (
        "Приказ Министерства природных ресурсов и экологии Российской Федерации "
        "от 19.07.2016 № 402\n "
        '"Об утверждении Порядка"'
    )
    parsed = parse_citation(text)
    assert parsed.number == "402"
    assert parsed.date == date(2016, 7, 19)
    assert (
        parsed.authority_name
        == "Министерство природных ресурсов и экологии Российской Федерации"
    )
    assert parsed.doc_type == "приказ"


def test_parse_joint_takes_first_authority():
    text = (
        "Приказ Министерства природных ресурсов и экологии Российской Федерации, "
        "Федерального агентства по недропользованию от 25.04.2023 № 247/04\n"
        ' "Об утверждении Порядка…"'
    )
    parsed = parse_citation(text)
    assert parsed.number == "247/04"
    assert parsed.date == date(2023, 4, 25)
    assert (
        parsed.authority_name
        == "Министерство природных ресурсов и экологии Российской Федерации"
    )


def test_parse_government_resolution():
    text = (
        'Постановление Правительства Российской Федерации от 31.05.2019 № 691\n'
        ' "О чём-то"'
    )
    parsed = parse_citation(text)
    assert parsed.number == "691"
    assert parsed.date == date(2019, 5, 31)
    assert parsed.authority_name == "Правительство Российской Федерации"


def test_parse_presidential_decree():
    text = "Указ Президента Российской Федерации от 01.01.2020 № 1"
    parsed = parse_citation(text)
    assert parsed.number == "1"
    assert parsed.authority_name == "Президент Российской Федерации"
    assert parsed.doc_type == "указ"


def test_parse_date_with_underscores_filename_style():
    text = "Федеральный закон от 21_07_2014 N 219-ФЗ"
    parsed = parse_citation(text)
    assert parsed.date == date(2014, 7, 21)
    assert parsed.number == "219-ФЗ"


def test_parse_html_br_title():
    text = (
        "Федеральный закон от 21.07.2014 № 219-ФЗ<br />"
        '"О внесении изменений…"'
    )
    parsed = parse_citation(text)
    assert parsed.number == "219-ФЗ"
    assert parsed.date == date(2014, 7, 21)


def test_query_from_citation():
    q = query_from_citation(
        "Федеральный закон от 21.07.2014 № 219-ФЗ"
    )
    assert q.number == "219-ФЗ"
    assert q.date == date(2014, 7, 21)
    assert q.authority_name == "Президент Российской Федерации"


def test_parse_consultant_style_with_month_name_and_redaction():
    """Формат консультант/гарант: месяц прописью + «(в ред. …)»."""
    text = (
        "ПРИКАЗ от 29 ноября 2019 г. N 814 "
        "(в ред. Приказа Минприроды России от 28.04.2023 N 265)"
    )
    parsed = parse_citation(text)
    assert parsed.number == "814"
    assert parsed.date == date(2019, 11, 29)
    assert parsed.authority_name == (
        "Министерство природных ресурсов и экологии Российской Федерации"
    )
    assert parsed.doc_type == "приказ"


def test_parse_ignores_redaction_date_when_authority_in_head():
    text = (
        "Приказ Министерства природных ресурсов и экологии Российской Федерации "
        "от 19.07.2016 № 402 (в ред. Приказа Минприроды России от 28.04.2023 N 265)"
    )
    parsed = parse_citation(text)
    assert parsed.number == "402"
    assert parsed.date == date(2016, 7, 19)
    assert "Министерство природных ресурсов" in (parsed.authority_name or "")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "закон об охране окружающей среды",
        "Федеральный закон № 219-ФЗ",
        "от 21.07.2014 № 219-ФЗ",
    ],
)
def test_parse_rejects_free_text_or_incomplete(bad: str):
    with pytest.raises(ValueError):
        parse_citation(bad)
