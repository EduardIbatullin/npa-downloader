"""Live точечная проверка ips.pravo.gov.ru на одном известном документе — ловит
регрессии в разборе настоящего ответа сайта (не фикстур), в первую очередь в
значениях status у redactions[]: прошлая проверка (до 2026-10-14) прочитала их
неверно из-за искажения кириллицы в выводе curl в терминале (mojibake), что
привело бы к выбору будущей, ещё не действующей редакции вместо текущей — см.
docs/ips-pravo-coverage.md."""

from __future__ import annotations

from datetime import date

import pytest

from npa_pipeline import ips_pravo
from npa_pipeline.ebpi_docx import convert_redtext_to_docx

# Федеральный закон от 10.01.2002 № 7-ФЗ «Об охране окружающей среды» — заведомо
# старше границы охвата publication.pravo.gov.ru, многократно изменялся.
ND = "1000000000102074303"


@pytest.mark.live
def test_known_document_search_and_card():
    with ips_pravo.create_ips_client() as client:
        hits = ips_pravo.search_by_requisites(client, number="7-ФЗ", doc_date=date(2002, 1, 10))
        assert len(hits) == 1
        assert hits[0].nd == ND
        assert hits[0].adoption.startswith("Федеральный закон от 10.01.2002")

        redactions = ips_pravo.fetch_redactions(client, nd=ND)
        assert len(redactions) > 50  # закон многократно менялся, десятки редакций

        chosen = ips_pravo.select_latest_redaction(redactions)
        assert chosen is not None
        assert chosen.status == "актуальная"  # не "применяется" — см. докстринг модуля


@pytest.mark.live
def test_known_document_text_converts_to_real_docx(tmp_path):
    with ips_pravo.create_ips_client() as client:
        redactions = ips_pravo.fetch_redactions(client, nd=ND)
        chosen = ips_pravo.select_latest_redaction(redactions)
        assert chosen is not None
        html = ips_pravo.fetch_text(client, nd=ND, rdk=chosen.rdk)
        assert html is not None
        assert len(html) > 100_000  # закон большой — страховка от «пустого, но 200 OK»

        out = tmp_path / "test.docx"
        convert_redtext_to_docx(html, out)
        assert out.is_file()
        assert out.stat().st_size > 10_000
