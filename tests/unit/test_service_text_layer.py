"""Интеграция механического извлечения текстового слоя (text_layer_docx) в
fetch_document — резервный путь, когда EBPI документ не покрывает, но PDF не скан.
См. npa_pipeline.service._attach_text_layer_docx."""

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

EO = "0001202602260030"
MINPRIRODY_GUID = "11111111-1111-1111-1111-111111111111"


def _seed_authority(db_path: Path) -> None:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        name = "Министерство природных ресурсов и экологии Российской Федерации"
        conn.execute(
            "INSERT INTO authorities (guid, name, name_norm, block, category, updated_at, weight) "
            "VALUES (?, ?, ?, NULL, NULL, 'now', 0)",
            (MINPRIRODY_GUID, name, normalize_name(name)),
        )
        conn.commit()


def _text_pdf_bytes(text: str) -> bytes:
    """Один объект-страница с полноценным текстовым слоем (не скан) — тот же
    приём, что в test_text_layer_docx.py: pypdf восстанавливает структуру даже с
    фиктивным startxref."""
    stream = f"BT /F1 12 Tf 10 150 Td ({text}) Tj ET"
    return (
        "%PDF-1.4\n"
        "1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        "2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        "3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>endobj\n"
        "4 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n"
        f"5 0 obj<< /Length {len(stream)} >>\nstream\n{stream}\nendstream\nendobj\n"
        "trailer<< /Size 6 /Root 1 0 R >>\n"
        "startxref\n0\n%%EOF\n"
    ).encode("latin-1")


def _scan_pdf_bytes() -> bytes:
    return b"""%PDF-1.1
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>endobj
trailer<< /Size 4 /Root 1 0 R >>
startxref
0
%%EOF
"""


TEXT_PDF = _text_pdf_bytes("Order text with enough characters to pass the threshold")
SCAN_PDF = _scan_pdf_bytes()


def _card_json() -> dict:
    return {
        "eoNumber": EO,
        "number": "191",
        "documentDate": "2026-02-20T00:00:00",
        "signatoryAuthorityId": MINPRIRODY_GUID,
        "documentTypeId": None,
        "name": "Об утверждении методики",
        "complexName": "Приказ Министерства природных ресурсов и экологии "
        "Российской Федерации от 20.02.2026 № 191",
        "pdfFileLength": len(TEXT_PDF),
    }


def _pravo_handler_text(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/Document":
        return httpx.Response(200, json=_card_json())
    if request.url.path == "/file/pdf":
        return httpx.Response(200, content=TEXT_PDF, headers={"Content-Length": str(len(TEXT_PDF))})
    return httpx.Response(404)


def _pravo_handler_scan(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/Document":
        card = _card_json()
        card["pdfFileLength"] = len(SCAN_PDF)
        return httpx.Response(200, json=card)
    if request.url.path == "/file/pdf":
        return httpx.Response(200, content=SCAN_PDF, headers={"Content-Length": str(len(SCAN_PDF))})
    return httpx.Response(404)


def _ebpi_handler_not_found(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/ebpi/card/":
        return httpx.Response(200, json={"docid": 0, "error": "Документ не найден"})
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
        return httpx.Response(200, json={"redtext": "<html><body><p>Текст из EBPI</p></body></html>"})
    return httpx.Response(404)


def _fetch(tmp_path: Path, *, pravo_handler, ebpi_handler, **kwargs) -> Status:
    db = tmp_path / "npa.db"
    cache = AuthorityCache(db)
    _seed_authority(db)
    store = DocumentStore(db)
    pravo_client = create_client(transport=httpx.MockTransport(pravo_handler))
    ebpi_client = ebpi.create_ebpi_client(transport=httpx.MockTransport(ebpi_handler))
    return fetch_document(
        Query(eo_number=EO),
        client=pravo_client,
        ebpi_client=ebpi_client,
        cache=cache,
        store=store,
        out_dir=tmp_path / "downloads",
        **kwargs,
    )


def test_fallback_used_when_ebpi_does_not_cover_and_pdf_has_text_layer(tmp_path: Path):
    result = _fetch(tmp_path, pravo_handler=_pravo_handler_text, ebpi_handler=_ebpi_handler_not_found)
    assert result.status == Status.FOUND
    assert result.ebpi_docx_path is None
    assert result.text_layer_docx_path is not None
    assert Path(result.text_layer_docx_path).is_file()

    from docx import Document

    text = "\n".join(p.text for p in Document(result.text_layer_docx_path).paragraphs)
    assert "Order text with enough characters to pass the threshold" in text


def test_fallback_skipped_when_ebpi_covers(tmp_path: Path):
    result = _fetch(tmp_path, pravo_handler=_pravo_handler_text, ebpi_handler=_ebpi_handler_found)
    assert result.ebpi_docx_path is not None
    assert result.text_layer_docx_path is None  # EBPI уже дал текст — извлечение не нужно


def test_no_fallback_for_real_scan(tmp_path: Path):
    # Скан (нет текстового слоя) и EBPI не покрыл — ни одного docx, это задача OCR.
    result = _fetch(tmp_path, pravo_handler=_pravo_handler_scan, ebpi_handler=_ebpi_handler_not_found)
    assert result.status == Status.FOUND
    assert result.ebpi_docx_path is None
    assert result.text_layer_docx_path is None


def test_fetch_text_layer_fallback_false_disables_it(tmp_path: Path):
    result = _fetch(
        tmp_path,
        pravo_handler=_pravo_handler_text,
        ebpi_handler=_ebpi_handler_not_found,
        fetch_text_layer_fallback=False,
    )
    assert result.text_layer_docx_path is None


def test_second_fetch_reuses_existing_text_layer_docx(tmp_path: Path):
    db = tmp_path / "npa.db"
    cache = AuthorityCache(db)
    _seed_authority(db)
    store = DocumentStore(db)

    first = fetch_document(
        Query(eo_number=EO),
        client=create_client(transport=httpx.MockTransport(_pravo_handler_text)),
        ebpi_client=ebpi.create_ebpi_client(transport=httpx.MockTransport(_ebpi_handler_not_found)),
        cache=cache,
        store=store,
        out_dir=tmp_path / "downloads",
    )
    assert first.text_layer_docx_path is not None
    mtime_before = Path(first.text_layer_docx_path).stat().st_mtime_ns

    second = fetch_document(
        Query(eo_number=EO),
        client=create_client(transport=httpx.MockTransport(_pravo_handler_text)),
        ebpi_client=ebpi.create_ebpi_client(transport=httpx.MockTransport(_ebpi_handler_not_found)),
        cache=cache,
        store=store,
        out_dir=tmp_path / "downloads",
    )
    assert second.text_layer_docx_path == first.text_layer_docx_path
    assert Path(second.text_layer_docx_path).stat().st_mtime_ns == mtime_before  # не перезаписан
