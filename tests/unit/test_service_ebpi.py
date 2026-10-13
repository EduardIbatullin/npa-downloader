"""Интеграция EBPI в fetch_document: PDF всегда, EBPI — только если документ там
покрыт, без порчи результата при сбое EBPI или сети."""

from __future__ import annotations

from pathlib import Path

import httpx

from npa_pipeline import ebpi
from npa_pipeline.authorities import AuthorityCache
from npa_pipeline.db import DocumentStore
from npa_pipeline.http_client import create_client
from npa_pipeline.models import Query, Status
from npa_pipeline.normalize import normalize_name
from npa_pipeline.service import fetch_document

EO = "0001202505230077"
PRAVITELSTVO_GUID = "8005d8c9-4b6d-48d3-861a-2a37e69fccb3"


def _seed_authority(db_path: Path) -> None:
    """upsert_from_card проверяет внешний ключ на authorities — без этого
    FOREIGN KEY constraint падает при первом же сохранении карточки (та же
    настройка, что в test_search_flow.py: cache.build() здесь не нужен, пишем
    напрямую)."""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        name = "Правительство Российской Федерации"
        conn.execute(
            "INSERT INTO authorities (guid, name, name_norm, block, category, updated_at, weight) "
            "VALUES (?, ?, ?, NULL, NULL, 'now', 0)",
            (PRAVITELSTVO_GUID, name, normalize_name(name)),
        )
        conn.commit()


def _minimal_pdf() -> bytes:
    return b"""%PDF-1.1
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>endobj
xref
0 4
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
trailer<< /Size 4 /Root 1 0 R >>
startxref
190
%%EOF
"""


def _card_json() -> dict:
    return {
        "eoNumber": EO,
        "number": "710",
        "documentDate": "2025-05-22T00:00:00",
        "signatoryAuthorityId": PRAVITELSTVO_GUID,
        "documentTypeId": None,
        "name": "Об утверждении Правил",
        "complexName": "Постановление Правительства Российской Федерации от 22.05.2025 № 710",
        "pdfFileLength": len(_minimal_pdf()),
    }


def _pravo_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/Document":
        return httpx.Response(200, json=_card_json())
    if request.url.path == "/file/pdf":
        data = _minimal_pdf()
        return httpx.Response(200, content=data, headers={"Content-Length": str(len(data))})
    return httpx.Response(404)


def _ebpi_handler_found(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/ebpi/card/":
        return httpx.Response(200, json={"docid": 1, "docstate": "Действует без изменений", "error": ""})
    if path == "/api/ebpi/redactions/":
        return httpx.Response(
            200, json={"redactions": [{"redid": 1, "redinitial": True, "actual": False}], "error": ""}
        )
    if path == "/api/ebpi/redtext":
        return httpx.Response(200, json={"redtext": "<html><body><p>Текст постановления</p></body></html>"})
    return httpx.Response(404)


def _ebpi_handler_not_found(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/ebpi/card/":
        return httpx.Response(200, json={"docid": 0, "error": "Документ не найден"})
    return httpx.Response(404)


AMENDING_DOCID = 333140
AMENDING_EO = "0001202609230028"


def _ebpi_handler_amended(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/ebpi/card/":
        import json as _json

        parsed = _json.loads(request.url.params.get("t"))
        if parsed.get("docid") == AMENDING_DOCID:
            return httpx.Response(
                200,
                json={
                    "docid": AMENDING_DOCID,
                    "docstate": "Действует без изменений",
                    "docpassing": "Постановление Правительства Российской Федерации от 23.09.2026 № 1221",
                    "docname": "О внесении изменения в постановление ... № 710",
                    "docimagepath": f"http://publication.pravo.gov.ru/Document/View/{AMENDING_EO}",
                    "adoptions": [{"onumber": "1221", "odate": "23.09.2026"}],
                    "error": "",
                },
            )
        return httpx.Response(200, json={"docid": 1, "docstate": "Действует с изменениями", "error": ""})
    if path == "/api/ebpi/redactions/":
        return httpx.Response(
            200,
            json={
                "redactions": [
                    {"redid": 497231, "reddocrefid": AMENDING_DOCID, "reddate": "20260923",
                     "redinitial": False, "actual": True},
                    {"redid": 473307, "reddocrefid": 0, "reddate": "20250901",
                     "redinitial": True, "actual": False},
                ],
                "error": "",
            },
        )
    if path == "/api/ebpi/redtext":
        return httpx.Response(200, json={"redtext": "<html><body><p>Текст с поправками</p></body></html>"})
    return httpx.Response(404)


def _fetch(tmp_path: Path, ebpi_handler, **kwargs) -> Status:
    pravo_client = create_client(transport=httpx.MockTransport(_pravo_handler))
    ebpi_client = ebpi.create_ebpi_client(transport=httpx.MockTransport(ebpi_handler))
    db = tmp_path / "npa.db"
    cache = AuthorityCache(db)  # инициализирует схему
    _seed_authority(db)
    store = DocumentStore(db)
    return fetch_document(
        Query(eo_number=EO),
        client=pravo_client,
        ebpi_client=ebpi_client,
        cache=cache,
        store=store,
        out_dir=tmp_path / "downloads",
        **kwargs,
    )


def test_attaches_ebpi_docx_next_to_pdf_when_covered(tmp_path: Path):
    result = _fetch(tmp_path, _ebpi_handler_found)
    assert result.status == Status.FOUND
    assert result.pdf_path is not None
    assert result.ebpi_docx_path is not None
    assert Path(result.ebpi_docx_path).is_file()
    # тот же stem, что у PDF — не unique_path с суффиксом _2
    assert Path(result.ebpi_docx_path).with_suffix("") == Path(result.pdf_path).with_suffix("")
    assert result.ebpi_docstate == "Действует без изменений"


def test_pdf_result_stays_found_when_ebpi_does_not_cover_document(tmp_path: Path):
    result = _fetch(tmp_path, _ebpi_handler_not_found)
    assert result.status == Status.FOUND
    assert result.pdf_path is not None
    assert Path(result.pdf_path).is_file()
    assert result.ebpi_docx_path is None
    assert result.ebpi_docstate is None


def test_ebpi_network_failure_does_not_break_pdf_result(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("npa_pipeline.http_client.time.sleep", lambda _: None)  # без реальных пауз retry

    def _broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    result = _fetch(tmp_path, _broken)
    assert result.status == Status.FOUND
    assert result.pdf_path is not None
    assert result.ebpi_docx_path is None


def test_ebpi_skipped_when_download_false(tmp_path: Path):
    calls: list[str] = []

    def _tracking_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"docid": 0, "error": "не важно"})

    result = _fetch(tmp_path, _tracking_handler, download=False)

    assert result.status == Status.FOUND
    assert result.pdf_path is None
    assert calls == []  # EBPI вообще не дёргался — скачивание PDF пропущено


def test_ebpi_skipped_when_flag_disabled(tmp_path: Path):
    def _should_not_be_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("EBPI не должен дёргаться при fetch_ebpi_text=False")

    result = _fetch(tmp_path, _should_not_be_called, fetch_ebpi_text=False)
    assert result.status == Status.FOUND
    assert result.pdf_path is not None
    assert result.ebpi_docx_path is None


def test_fetch_document_creates_own_ebpi_client_when_not_given(tmp_path: Path):
    """Без явного ebpi_client fetch_document создаёт свой и закрывает сам — здесь
    просто проверяем, что реального сетевого похода не падает сборка вызова (сеть
    не нужна для самой проверки: создание клиента ленивое и офлайн)."""
    pravo_client = create_client(transport=httpx.MockTransport(_pravo_handler))
    db = tmp_path / "npa.db"
    cache = AuthorityCache(db)
    _seed_authority(db)
    store = DocumentStore(db)
    # fetch_ebpi_text=False — чтобы не ходить в реальную сеть за EBPI в юнит-тесте,
    # но ebpi_client всё равно не создаётся лениво при выключенном флаге
    result = fetch_document(
        Query(eo_number=EO),
        client=pravo_client,
        cache=cache,
        store=store,
        out_dir=tmp_path / "downloads",
        fetch_ebpi_text=False,
    )
    assert result.status == Status.FOUND
    assert result.ebpi_docx_path is None


def _fetch_twice(tmp_path: Path, first_handler, second_handler, **kwargs):
    """Два вызова fetch_document на одной БД — второй раз должен брать EBPI из
    кэша (document_ebpi), а не снова ходить в сеть."""
    db = tmp_path / "npa.db"
    cache = AuthorityCache(db)  # инициализирует схему
    _seed_authority(db)

    pravo_client1 = create_client(transport=httpx.MockTransport(_pravo_handler))
    ebpi_client1 = ebpi.create_ebpi_client(transport=httpx.MockTransport(first_handler))
    first = fetch_document(
        Query(eo_number=EO),
        client=pravo_client1,
        ebpi_client=ebpi_client1,
        cache=cache,
        store=DocumentStore(db),
        out_dir=tmp_path / "downloads",
        **kwargs,
    )

    pravo_client2 = create_client(transport=httpx.MockTransport(_pravo_handler))
    ebpi_client2 = ebpi.create_ebpi_client(transport=httpx.MockTransport(second_handler))
    second = fetch_document(
        Query(eo_number=EO),
        client=pravo_client2,
        ebpi_client=ebpi_client2,
        cache=cache,
        store=DocumentStore(db),
        out_dir=tmp_path / "downloads",
        **kwargs,
    )
    return first, second


def _exploding_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"EBPI не должен дёргаться повторно: {request.url}")


def test_second_fetch_reuses_cached_docx_without_network(tmp_path: Path):
    first, second = _fetch_twice(tmp_path, _ebpi_handler_found, _exploding_handler)
    assert first.ebpi_docx_path is not None
    assert second.status == Status.FOUND
    assert second.ebpi_docx_path == first.ebpi_docx_path
    assert Path(second.ebpi_docx_path).is_file()
    assert second.ebpi_docstate == "Действует без изменений"


def test_second_fetch_skips_network_when_not_covered(tmp_path: Path):
    first, second = _fetch_twice(tmp_path, _ebpi_handler_not_found, _exploding_handler)
    assert first.ebpi_docx_path is None
    assert second.status == Status.FOUND
    assert second.ebpi_docx_path is None  # кэш «не покрыт» — второй раз сеть не дёргали


def test_refetches_when_cached_docx_file_was_deleted(tmp_path: Path):
    db = tmp_path / "npa.db"
    cache = AuthorityCache(db)
    _seed_authority(db)

    pravo_client1 = create_client(transport=httpx.MockTransport(_pravo_handler))
    ebpi_client1 = ebpi.create_ebpi_client(transport=httpx.MockTransport(_ebpi_handler_found))
    first = fetch_document(
        Query(eo_number=EO),
        client=pravo_client1,
        ebpi_client=ebpi_client1,
        cache=cache,
        store=DocumentStore(db),
        out_dir=tmp_path / "downloads",
    )
    assert first.ebpi_docx_path is not None
    Path(first.ebpi_docx_path).unlink()  # файл пропал с диска, запись в БД осталась

    pravo_client2 = create_client(transport=httpx.MockTransport(_pravo_handler))
    ebpi_client2 = ebpi.create_ebpi_client(transport=httpx.MockTransport(_ebpi_handler_found))
    second = fetch_document(
        Query(eo_number=EO),
        client=pravo_client2,
        ebpi_client=ebpi_client2,
        cache=cache,
        store=DocumentStore(db),
        out_dir=tmp_path / "downloads",
    )
    assert second.ebpi_docx_path is not None
    assert Path(second.ebpi_docx_path).is_file()  # перекачали заново


def test_amended_document_gets_labeled_docx_filename(tmp_path: Path):
    result = _fetch(tmp_path, _ebpi_handler_amended)
    assert result.ebpi_docx_path is not None
    name = Path(result.ebpi_docx_path).name
    assert "(ред. от 23.09.2026)" in name
    assert Path(result.ebpi_docx_path).is_file()


def test_unamended_document_docx_filename_has_no_label(tmp_path: Path):
    result = _fetch(tmp_path, _ebpi_handler_found)
    assert result.ebpi_docx_path is not None
    assert "(ред." not in Path(result.ebpi_docx_path).name
    assert Path(result.ebpi_docx_path).with_suffix("") == Path(result.pdf_path).with_suffix("")


def test_amending_act_recorded_in_document_relations(tmp_path: Path):
    db = tmp_path / "npa.db"
    cache = AuthorityCache(db)
    _seed_authority(db)
    pravo_client = create_client(transport=httpx.MockTransport(_pravo_handler))
    ebpi_client = ebpi.create_ebpi_client(transport=httpx.MockTransport(_ebpi_handler_amended))
    store = DocumentStore(db)

    result = fetch_document(
        Query(eo_number=EO),
        client=pravo_client,
        ebpi_client=ebpi_client,
        cache=cache,
        store=store,
        out_dir=tmp_path / "downloads",
    )
    assert result.ebpi_docx_path is not None

    rows = store.get_relations_for_target(EO)
    assert len(rows) == 1
    assert rows[0]["source_eo"] == AMENDING_EO
    assert rows[0]["relation_type"] == "amends"
    assert rows[0]["effective_date"] == "20260923"
    assert rows[0]["origin"] == "ebpi"

    # сам акт-поправка тоже стал известным документом (минимальная запись)
    amending_row = store.get_document_row(AMENDING_EO)
    assert amending_row is not None
    assert amending_row["number"] == "1221"


def test_stale_negative_cache_retries_network(tmp_path: Path):
    """covered=0 старше NEGATIVE_CACHE_TTL_DAYS — EBPI пробуется повторно, а не
    навсегда доверяет старому «не покрыт» (решение 2026-10-13, задержка индексации)."""
    import sqlite3
    from datetime import datetime, timedelta, timezone

    db = tmp_path / "npa.db"
    cache = AuthorityCache(db)
    _seed_authority(db)

    pravo_client1 = create_client(transport=httpx.MockTransport(_pravo_handler))
    ebpi_client1 = ebpi.create_ebpi_client(transport=httpx.MockTransport(_ebpi_handler_not_found))
    first = fetch_document(
        Query(eo_number=EO),
        client=pravo_client1,
        ebpi_client=ebpi_client1,
        cache=cache,
        store=DocumentStore(db),
        out_dir=tmp_path / "downloads",
    )
    assert first.ebpi_docx_path is None

    stale = (datetime.now(timezone.utc) - timedelta(days=22)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE document_ebpi SET checked_at = ? WHERE eo_number = ?", (stale, EO))
        conn.commit()

    pravo_client2 = create_client(transport=httpx.MockTransport(_pravo_handler))
    ebpi_client2 = ebpi.create_ebpi_client(transport=httpx.MockTransport(_ebpi_handler_found))
    second = fetch_document(
        Query(eo_number=EO),
        client=pravo_client2,
        ebpi_client=ebpi_client2,
        cache=cache,
        store=DocumentStore(db),
        out_dir=tmp_path / "downloads",
    )
    assert second.ebpi_docx_path is not None  # TTL истёк -> сеть дёрнули заново, документ нашёлся


def test_fresh_negative_cache_still_skips_network_within_ttl(tmp_path: Path):
    first, second = _fetch_twice(tmp_path, _ebpi_handler_not_found, _exploding_handler)
    assert first.ebpi_docx_path is None
    assert second.ebpi_docx_path is None  # checked_at только что создан — внутри TTL, сеть не дёрнули
