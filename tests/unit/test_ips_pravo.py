"""Клиент к ips.pravo.gov.ru: разбор search.json/document_card.json/documenttext,
выбор действующей редакции по status (не actual — см. ips_pravo.select_latest_redaction),
graceful-деградация при отсутствии документа или сбое сети."""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs

import httpx

from npa_pipeline import ips_pravo
from npa_pipeline.http_client import NetworkError

ND = "1000000000102074303"


def _handler_found(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/ips/legislation/search.json":
        body = parse_qs(request.content.decode())
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        if body.get("number_eq") == ["7-ФЗ"] and body.get("date") == ["10.01.2002,10.01.2002"]:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "nd": ND,
                            "adoption": "Федеральный закон от 10.01.2002 № 7-ФЗ",
                            "docdate": "10.01.2002",
                            "state": "Действует с изменениями",
                            "publications": ["\"Российская газета\" от 12.01.2002 г."],
                        }
                    ],
                    "pageSize": 1,
                },
            )
        return httpx.Response(200, json={"pageSize": 0})
    if path == "/api/ips/legislation/document_card.json":
        if request.url.params.get("nd") != ND:
            return httpx.Response(200, json={"error": "not found"})
        return httpx.Response(
            200,
            json={
                "redactions": [
                    {"id": 99, "status": "актуальная", "date": "01.09.2026", "refnd": "ref-99"},
                    {"id": 100, "status": "не вступившая в силу", "date": "01.01.2027", "refnd": "ref-100"},
                    {"id": 98, "status": "недействующая", "date": "01.01.2020", "refnd": None},
                ]
            },
        )
    if path == "/api/ips/legislation/documenttext":
        params = request.url.params
        if params.get("nd") == ND and params.get("rdk") == "99" and params.get("completed") == "true":
            return httpx.Response(
                200,
                json={"docid": ND, "doctext": "<html><body><p>Текст закона</p></body></html>", "rdk": 99},
            )
        if params.get("completed") != "true":
            # ловушка: без completed=true — 200 с пустым doctext, не ошибка
            return httpx.Response(200, json={"docid": ND, "doctext": "", "rdk": params.get("rdk")})
        return httpx.Response(200, json={"docid": ND, "doctext": ""})
    return httpx.Response(404)


def test_search_by_requisites_finds_single_hit():
    client = ips_pravo.create_ips_client(transport=httpx.MockTransport(_handler_found))
    hits = ips_pravo.search_by_requisites(client, number="7-ФЗ", doc_date=date(2002, 1, 10))
    assert len(hits) == 1
    assert hits[0].nd == ND
    assert hits[0].adoption == "Федеральный закон от 10.01.2002 № 7-ФЗ"
    assert hits[0].state == "Действует с изменениями"
    assert hits[0].publications == ['"Российская газета" от 12.01.2002 г.']


def test_search_by_requisites_empty_when_no_match():
    client = ips_pravo.create_ips_client(transport=httpx.MockTransport(_handler_found))
    hits = ips_pravo.search_by_requisites(client, number="999-ФЗ", doc_date=date(1999, 1, 1))
    assert hits == []


def test_search_by_requisites_sends_dot_format_date_not_iso():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"pageSize": 0})

    client = ips_pravo.create_ips_client(transport=httpx.MockTransport(handler))
    ips_pravo.search_by_requisites(client, number="7-ФЗ", doc_date=date(2002, 1, 10))
    assert seen["date"] == "10.01.2002,10.01.2002"


def test_fetch_redactions_parses_list():
    client = ips_pravo.create_ips_client(transport=httpx.MockTransport(_handler_found))
    redactions = ips_pravo.fetch_redactions(client, nd=ND)
    assert [r.rdk for r in redactions] == [99, 100, 98]


def test_fetch_redactions_empty_for_unknown_nd():
    client = ips_pravo.create_ips_client(transport=httpx.MockTransport(_handler_found))
    assert ips_pravo.fetch_redactions(client, nd="unknown") == []


def test_select_latest_redaction_prefers_applied_status_not_future():
    client = ips_pravo.create_ips_client(transport=httpx.MockTransport(_handler_found))
    redactions = ips_pravo.fetch_redactions(client, nd=ND)
    chosen = ips_pravo.select_latest_redaction(redactions)
    assert chosen is not None
    assert chosen.rdk == 99
    assert chosen.status == "актуальная"


def test_select_latest_redaction_falls_back_when_nothing_applied():
    redactions = [
        ips_pravo.IpsRedaction(rdk=1, status="недействующая", date="01.01.2000", ref_nd=None),
        ips_pravo.IpsRedaction(rdk=2, status="не вступившая в силу", date="01.01.2030", ref_nd=None),
    ]
    chosen = ips_pravo.select_latest_redaction(redactions)
    assert chosen is not None
    assert chosen.rdk == 1  # не future, максимальный rdk среди оставшихся


def test_select_latest_redaction_none_when_all_future_or_empty():
    assert ips_pravo.select_latest_redaction([]) is None
    only_future = [ips_pravo.IpsRedaction(rdk=1, status="не вступившая в силу", date=None, ref_nd=None)]
    assert ips_pravo.select_latest_redaction(only_future) is None


def test_fetch_text_requires_completed_true_param():
    client = ips_pravo.create_ips_client(transport=httpx.MockTransport(_handler_found))
    html = ips_pravo.fetch_text(client, nd=ND, rdk=99)
    assert html is not None
    assert "Текст закона" in html


def test_fetch_text_none_for_other_rdk_without_completed_match():
    client = ips_pravo.create_ips_client(transport=httpx.MockTransport(_handler_found))
    assert ips_pravo.fetch_text(client, nd=ND, rdk=100) is None


def test_network_failure_returns_empty_not_raises(monkeypatch):
    monkeypatch.setattr("npa_pipeline.http_client.time.sleep", lambda _: None)

    def _broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = ips_pravo.create_ips_client(transport=httpx.MockTransport(_broken))
    assert ips_pravo.search_by_requisites(client, number="7-ФЗ", doc_date=date(2002, 1, 10)) == []
    assert ips_pravo.fetch_redactions(client, nd=ND) == []
    assert ips_pravo.fetch_text(client, nd=ND, rdk=99) is None
