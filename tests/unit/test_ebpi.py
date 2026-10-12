"""Клиент к EBPI (actual.pravo.gov.ru): разбор ответов, выбор действующей редакции
(решение 2026-10-12 — см. ebpi.select_latest_redaction), graceful-деградация при
отсутствии документа или сбое сети."""

from __future__ import annotations

import json

import httpx

from npa_pipeline import ebpi
from npa_pipeline.http_client import NetworkError


def _handler_for(pnum: str):
    """Фейковый сервер EBPI: один известный документ с двумя редакциями (изменённый),
    остальные pnum -> "не найден"."""

    def handler(request: httpx.Request) -> httpx.Response:
        t = request.url.params.get("t")
        path = request.url.path
        if path == "/api/ebpi/card/":
            params = json.loads(t)
            if params.get("pnum") != pnum:
                return httpx.Response(200, json={"docid": 0, "error": "Документ не найден"})
            return httpx.Response(
                200,
                json={"docid": 318024, "docstate": "Действует с изменениями", "error": ""},
            )
        if path == "/api/ebpi/redactions/":
            return httpx.Response(
                200,
                json={
                    "docid": 318024,
                    "redactions": [
                        {"redid": 497231, "reddate": "20260923", "redinitial": False, "actual": True},
                        {"redid": 473307, "reddate": "20250901", "redinitial": True, "actual": False},
                    ],
                    "error": "",
                },
            )
        if path == "/api/ebpi/redtext":
            redid = request.url.params.get("t")
            if redid == "473307":
                return httpx.Response(200, json={"redtext": "<html><body><p>исходный текст</p></body></html>"})
            return httpx.Response(200, json={"redtext": "<html><body><p>текст с поправками</p></body></html>"})
        return httpx.Response(404)

    return handler


def test_fetch_card_found():
    client = ebpi.create_ebpi_client(transport=httpx.MockTransport(_handler_for("0001202505230077")))
    with client:
        card = ebpi.fetch_card(client, "0001202505230077")
    assert card is not None
    assert card["docid"] == 318024
    assert card["docstate"] == "Действует с изменениями"


def test_fetch_card_not_found_returns_none():
    client = ebpi.create_ebpi_client(transport=httpx.MockTransport(_handler_for("0001202505230077")))
    with client:
        card = ebpi.fetch_card(client, "0001202605120026")  # приказ — не покрыт
    assert card is None


def test_select_initial_redaction_prefers_redinitial_not_actual():
    redactions = [
        {"redid": 497231, "redinitial": False, "actual": True},
        {"redid": 473307, "redinitial": True, "actual": False},
    ]
    chosen = ebpi.select_initial_redaction(redactions)
    assert chosen is not None
    assert chosen["redid"] == 473307  # исходная, не действующая с поправками


def test_select_initial_redaction_none_when_missing():
    assert ebpi.select_initial_redaction([{"redid": 1, "redinitial": False}]) is None
    assert ebpi.select_initial_redaction([]) is None


def test_select_latest_redaction_picks_max_reddate_not_actual_flag():
    redactions = [
        {"redid": 473307, "reddate": "20250901", "redinitial": True, "actual": False},
        {"redid": 497231, "reddate": "20260923", "redinitial": False, "actual": True},
    ]
    chosen = ebpi.select_latest_redaction(redactions)
    assert chosen is not None
    assert chosen["redid"] == 497231  # действующая, с поправками — самая свежая дата

    # порядок в списке не важен — работает по max(reddate), не по позиции
    assert ebpi.select_latest_redaction(list(reversed(redactions)))["redid"] == 497231


def test_select_latest_redaction_single_unamended_document():
    # неизменённый документ: одна редакция, actual=False — всё равно выбирается
    redactions = [{"redid": 488318, "reddate": "20260226", "redinitial": True, "actual": False}]
    assert ebpi.select_latest_redaction(redactions)["redid"] == 488318


def test_select_latest_redaction_none_when_missing():
    assert ebpi.select_latest_redaction([]) is None


def test_fetch_ebpi_document_picks_latest_redaction_text():
    client = ebpi.create_ebpi_client(transport=httpx.MockTransport(_handler_for("0001202505230077")))
    with client:
        result = ebpi.fetch_ebpi_document(client, "0001202505230077")
    assert result is not None
    assert result.redid == 497231  # действующая редакция, не исходная публикация
    assert "текст с поправками" in result.html
    assert result.docstate == "Действует с изменениями"


def test_fetch_ebpi_document_none_for_uncovered_doc():
    client = ebpi.create_ebpi_client(transport=httpx.MockTransport(_handler_for("0001202505230077")))
    with client:
        result = ebpi.fetch_ebpi_document(client, "0001202605120026")
    assert result is None


def test_fetch_card_network_error_returns_none(monkeypatch):
    monkeypatch.setattr("npa_pipeline.http_client.time.sleep", lambda _: None)  # без реальных пауз retry

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = ebpi.create_ebpi_client(transport=httpx.MockTransport(handler))
    with client:
        card = ebpi.fetch_card(client, "0001202602260030")
    assert card is None


def test_fetch_redtext_missing_field_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "что-то не так"})

    client = ebpi.create_ebpi_client(transport=httpx.MockTransport(handler))
    with client:
        text = ebpi.fetch_redtext(client, 1)
    assert text is None
