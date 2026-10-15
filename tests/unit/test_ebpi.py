"""Клиент к EBPI (actual.pravo.gov.ru): разбор ответов, выбор действующей редакции
(решение 2026-10-12 — см. ebpi.select_latest_redaction), graceful-деградация при
отсутствии документа или сбое сети."""

from __future__ import annotations

import json

import httpx

from npa_pipeline import ebpi
from npa_pipeline.http_client import NetworkError


AMENDING_DOCID = 333140
AMENDING_EO = "0001202609230028"


def _handler_for(pnum: str):
    """Фейковый сервер EBPI: один известный документ с двумя редакциями (изменённый,
    действующая редакция ссылается на акт-поправку через reddocrefid), остальные
    pnum -> "не найден"."""

    def handler(request: httpx.Request) -> httpx.Response:
        t = request.url.params.get("t")
        path = request.url.path
        if path == "/api/ebpi/card/":
            params = json.loads(t)
            if params.get("docid") == AMENDING_DOCID:
                return httpx.Response(
                    200,
                    json={
                        "docid": AMENDING_DOCID,
                        "docstate": "Действует без изменений",
                        "docpassing": "Постановление Правительства Российской Федерации от 23.09.2026 № 1221",
                        "docname": "О внесении изменения в постановление ... от 22 мая 2025 г. № 710",
                        "docimagepath": f"http://publication.pravo.gov.ru/Document/View/{AMENDING_EO}",
                        "adoptions": [
                            {"organ": "Правительства", "onumber": "1221", "odate": "23.09.2026",
                             "region": "Российской Федерации", "type": "Постановление"}
                        ],
                        "error": "",
                    },
                )
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
                        {
                            "redid": 497231, "reddocrefid": AMENDING_DOCID, "reddate": "20260923",
                            "redinitial": False, "actual": True,
                        },
                        {
                            "redid": 473307, "reddocrefid": 0, "reddate": "20250901",
                            "redinitial": True, "actual": False,
                        },
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
    # as_of фиксируем: обе даты уже в прошлом относительно отсчёта
    chosen = ebpi.select_latest_redaction(redactions, as_of="20261015")
    assert chosen is not None
    assert chosen["redid"] == 497231  # действующая, с поправками — самая свежая дата

    # порядок в списке не важен — работает по max(reddate), не по позиции
    assert ebpi.select_latest_redaction(list(reversed(redactions)), as_of="20261015")["redid"] == 497231


def test_select_latest_redaction_skips_future_reddate():
    # Регресс: Постановление №255 — max(reddate) брал будущую 01.03.2028 с пустым
    # redtext, UI показывал «DOCX есть», файл открывался пустым листом.
    redactions = [
        {"redid": 1, "reddate": "20230217", "redinitial": True, "actual": False},
        {"redid": 2, "reddate": "20240115", "redinitial": False, "actual": False},
        {"redid": 3, "reddate": "20280301", "redinitial": False, "actual": True},
    ]
    chosen = ebpi.select_latest_redaction(redactions, as_of="20261015")
    assert chosen is not None
    assert chosen["redid"] == 2  # последняя уже вступившая, не будущая заглушка


def test_select_latest_redaction_none_when_all_future():
    only_future = [{"redid": 3, "reddate": "20280301", "redinitial": False}]
    assert ebpi.select_latest_redaction(only_future, as_of="20261015") is None


def test_select_latest_redaction_single_unamended_document():
    # неизменённый документ: одна редакция, actual=False — всё равно выбирается
    redactions = [{"redid": 488318, "reddate": "20260226", "redinitial": True, "actual": False}]
    assert ebpi.select_latest_redaction(redactions, as_of="20261015")["redid"] == 488318


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
    assert result.is_initial is False


def test_fetch_ebpi_document_is_initial_true_when_unamended():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/ebpi/card/":
            return httpx.Response(200, json={"docid": 1, "docstate": "Действует без изменений", "error": ""})
        if path == "/api/ebpi/redactions/":
            return httpx.Response(
                200,
                json={"redactions": [{"redid": 1, "reddocrefid": 0, "reddate": "20260226",
                                       "redinitial": True, "actual": False}], "error": ""},
            )
        if path == "/api/ebpi/redtext":
            return httpx.Response(200, json={"redtext": "<html><body><p>текст</p></body></html>"})
        return httpx.Response(404)

    client = ebpi.create_ebpi_client(transport=httpx.MockTransport(handler))
    with client:
        result = ebpi.fetch_ebpi_document(client, "0001202602260030")
    assert result.is_initial is True
    assert result.amending_acts == []


def test_fetch_ebpi_document_resolves_amending_act():
    client = ebpi.create_ebpi_client(transport=httpx.MockTransport(_handler_for("0001202505230077")))
    with client:
        result = ebpi.fetch_ebpi_document(client, "0001202505230077")
    assert len(result.amending_acts) == 1
    act = result.amending_acts[0]
    assert act.eo_number == AMENDING_EO
    assert act.number == "1221"
    assert act.date == "23.09.2026"
    assert act.effective_date == "20260923"


def test_fetch_card_by_docid_found_and_not_found():
    client = ebpi.create_ebpi_client(transport=httpx.MockTransport(_handler_for("0001202505230077")))
    with client:
        card = ebpi.fetch_card_by_docid(client, AMENDING_DOCID)
        assert card is not None
        assert card["docid"] == AMENDING_DOCID
        assert ebpi.fetch_card_by_docid(client, 999) is None


def test_extract_eo_number_from_docimagepath():
    assert ebpi.extract_eo_number_from_docimagepath(
        "http://publication.pravo.gov.ru/Document/View/0001202609230028"
    ) == "0001202609230028"
    assert ebpi.extract_eo_number_from_docimagepath(None) is None
    assert ebpi.extract_eo_number_from_docimagepath("") is None
    assert ebpi.extract_eo_number_from_docimagepath("garbage, no path") is None


def test_resolve_amending_acts_skips_zero_reddocrefid_and_dedupes():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = json.loads(request.url.params.get("t"))
        calls.append(params["docid"])
        return httpx.Response(
            200,
            json={
                "docid": params["docid"], "docstate": "x", "docpassing": "p", "docname": "n",
                "docimagepath": f"http://publication.pravo.gov.ru/Document/View/000120200101000{params['docid']}",
                "adoptions": [{"onumber": "1", "odate": "01.01.2020"}], "error": "",
            },
        )

    client = ebpi.create_ebpi_client(transport=httpx.MockTransport(handler))
    redactions = [
        {"reddocrefid": 0, "reddate": "20200101"},      # исходная — резолвить нечего
        {"reddocrefid": 5, "reddate": "20210101"},
        {"reddocrefid": 5, "reddate": "20220101"},       # дубль docid — не дёргаем сеть повторно
    ]
    with client:
        acts = ebpi.resolve_amending_acts(client, redactions)
    assert calls == [5]
    assert len(acts) == 1
    assert acts[0].eo_number == "0001202001010005"


def test_format_red_date():
    assert ebpi.format_red_date("20260923") == "23.09.2026"


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


def test_is_negative_cache_expired():
    from datetime import datetime, timedelta, timezone

    fresh = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    stale = (datetime.now(timezone.utc) - timedelta(days=22)).isoformat().replace("+00:00", "Z")
    just_under = (datetime.now(timezone.utc) - timedelta(days=21) + timedelta(hours=1)).isoformat().replace(
        "+00:00", "Z"
    )
    assert ebpi.is_negative_cache_expired(fresh) is False
    assert ebpi.is_negative_cache_expired(stale) is True
    assert ebpi.is_negative_cache_expired(just_under) is False
