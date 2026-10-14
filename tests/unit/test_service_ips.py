"""Интеграция резерва ips.pravo.gov.ru в fetch_document: срабатывает только при
out_of_range от основного поиска, не портит результат при сбое сети/структуры
ответа (см. docs/ips-pravo-coverage.md)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import httpx

from npa_pipeline import ips_pravo
from npa_pipeline.authorities import AuthorityCache
from npa_pipeline.db import DocumentStore
from npa_pipeline.http_client import create_client
from npa_pipeline.models import Query, Status
from npa_pipeline.normalize import normalize_name
from npa_pipeline.service import fetch_document

PRESIDENT = "225698f1-cfbc-4e42-9caa-32f9f7403211"
ND = "1000000000102074303"


def _cache(tmp_path: Path) -> AuthorityCache:
    db = tmp_path / "npa.db"
    cache = AuthorityCache(db)  # инициализирует схему
    with sqlite3.connect(db) as conn:
        name = "Президент Российской Федерации"
        conn.execute(
            "INSERT INTO authorities (guid, name, name_norm, block, category, updated_at, weight) "
            "VALUES (?, ?, ?, NULL, NULL, 'now', 0)",
            (PRESIDENT, name, normalize_name(name)),
        )
        conn.commit()
    return cache


def _empty_page() -> dict:
    return {"items": [], "currentPage": 1, "pagesTotalCount": 1, "itemsTotalCount": 0, "itemsPerPage": 200}


def _pravo_empty_handler(request: httpx.Request) -> httpx.Response:
    """publication.pravo.gov.ru — ни одного совпадения, документ старше границы охвата."""
    if "/api/Documents" in str(request.url):
        return httpx.Response(200, json=_empty_page())
    return httpx.Response(404)


def _ips_handler_single_hit(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/ips/legislation/search.json":
        return httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "nd": ND,
                        "adoption": "Федеральный закон от 10.01.2002 № 7-ФЗ",
                        "docdate": "10.01.2002",
                        "state": "Действует с изменениями",
                        "publications": [],
                    }
                ]
            },
        )
    if path == "/api/ips/legislation/document_card.json":
        return httpx.Response(200, json={"redactions": [{"id": 99, "status": "актуальная", "date": "01.09.2026"}]})
    if path == "/api/ips/legislation/documenttext":
        return httpx.Response(
            200,
            json={"doctext": "<html><body><p>Текст закона об охране среды</p></body></html>"},
        )
    return httpx.Response(404)


def _ips_handler_no_hits(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/ips/legislation/search.json":
        return httpx.Response(200, json={"pageSize": 0})
    return httpx.Response(404)


def _ips_handler_many_hits(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/ips/legislation/search.json":
        return httpx.Response(
            200,
            json={
                "docs": [
                    {"nd": "1", "adoption": "Закон 1", "docdate": "10.01.2002", "state": None},
                    {"nd": "2", "adoption": "Закон 2", "docdate": "10.01.2002", "state": None},
                ]
            },
        )
    return httpx.Response(404)


def _fetch(tmp_path: Path, ips_handler, **kwargs):
    pravo_client = create_client(transport=httpx.MockTransport(_pravo_empty_handler))
    ips_client = ips_pravo.create_ips_client(transport=httpx.MockTransport(ips_handler))
    cache = _cache(tmp_path)
    store = DocumentStore(cache.db_path)
    return fetch_document(
        Query(authority_guid=PRESIDENT, number="7-ФЗ", date=date(2002, 1, 10)),
        client=pravo_client,
        ips_client=ips_client,
        cache=cache,
        store=store,
        out_dir=tmp_path / "downloads",
        **kwargs,
    )


def test_single_ips_hit_upgrades_out_of_range_to_found_text_only(tmp_path: Path):
    result = _fetch(tmp_path, _ips_handler_single_hit)
    assert result.status == Status.FOUND_TEXT_ONLY
    assert result.pdf_path is None
    assert result.ips_nd == ND
    assert result.ips_adoption == "Федеральный закон от 10.01.2002 № 7-ФЗ"
    assert result.ips_state == "актуальная"
    assert result.ips_docx_path is not None
    assert Path(result.ips_docx_path).is_file()
    assert Path(result.ips_docx_path).suffix == ".docx"


def test_no_ips_hits_stays_out_of_range(tmp_path: Path):
    result = _fetch(tmp_path, _ips_handler_no_hits)
    assert result.status == Status.OUT_OF_RANGE
    assert result.pdf_path is None
    assert result.ips_docx_path is None


def test_several_ips_hits_become_ambiguous_with_ips_candidates(tmp_path: Path):
    result = _fetch(tmp_path, _ips_handler_many_hits)
    assert result.status == Status.AMBIGUOUS
    assert result.candidates == []
    assert len(result.ips_candidates) == 2
    assert {c["nd"] for c in result.ips_candidates} == {"1", "2"}


def test_ips_network_failure_keeps_out_of_range_not_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("npa_pipeline.http_client.time.sleep", lambda _: None)

    def _broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    result = _fetch(tmp_path, _broken)
    assert result.status == Status.OUT_OF_RANGE


def test_ips_fallback_skipped_when_flag_disabled(tmp_path: Path):
    def _should_not_be_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("ips.pravo.gov.ru не должен дёргаться при fetch_ips_fallback=False")

    result = _fetch(tmp_path, _should_not_be_called, fetch_ips_fallback=False)
    assert result.status == Status.OUT_OF_RANGE


def test_ips_fallback_skipped_when_download_requested_but_no_eo_number(tmp_path: Path):
    """found_text_only не пытается скачивать PDF — его структурно не существует."""
    result = _fetch(tmp_path, _ips_handler_single_hit, download=True)
    assert result.status == Status.FOUND_TEXT_ONLY
    assert result.pdf_path is None


def test_to_dict_exposes_ips_fields(tmp_path: Path):
    result = _fetch(tmp_path, _ips_handler_single_hit)
    data = result.to_dict()
    assert data["status"] == "found_text_only"
    assert data["ips_nd"] == ND
    assert data["pdf_path"] is None
