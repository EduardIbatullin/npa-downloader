"""/api/docx/{nd}: отдаёт .docx документа, найденного через резерв
ips.pravo.gov.ru (Status.FOUND_TEXT_ONLY), по внутреннему nd — не eoNumber,
см. npa_pipeline.webapp.get_ips_docx."""

from __future__ import annotations

from pathlib import Path

from datetime import date

from docx import Document
from fastapi.testclient import TestClient

from npa_pipeline import webapp
from npa_pipeline.db import DocumentStore
from npa_pipeline.models import DocItem

ND = "1000000000102074303"
EO = "0001202503200023"


def _real_docx(path: Path, text: str = "Текст документа для предпросмотра") -> None:
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))


def _seed_document(store: DocumentStore, eo_number: str = EO) -> None:
    store.upsert_from_doc_item(
        DocItem(
            eo_number=eo_number,
            number="33-ФЗ",
            document_date=date(2025, 3, 20),
            signatory_ids=[],
        )
    )


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    db_path = tmp_path / "npa.db"
    monkeypatch.setattr(webapp, "DEFAULT_DB", db_path)
    return TestClient(webapp.app)


def test_get_docx_404_for_unknown_nd(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get(f"/api/docx/{ND}")
    assert resp.status_code == 404


def test_get_docx_open_returns_html_preview_not_raw_file(tmp_path: Path, monkeypatch):
    """«Открыть» (без download=1) на настоящий .docx — HTML-предпросмотр, не
    файл: браузер не умеет рендерить .docx inline (см. npa_pipeline.docx_preview,
    это и была жалоба — кнопка «Открыть» вызывала диалог сохранения)."""
    db_path = tmp_path / "npa.db"
    monkeypatch.setattr(webapp, "DEFAULT_DB", db_path)
    docx_path = tmp_path / "Федеральный закон от 10_01_2002 N 7-ФЗ.docx"
    _real_docx(docx_path, "Текст закона об охране окружающей среды")

    store = DocumentStore(db_path)
    store.record_ips_document(
        nd=ND,
        adoption="Федеральный закон от 10.01.2002 № 7-ФЗ",
        state="актуальная",
        docx_path=docx_path,
    )

    client = TestClient(webapp.app)
    resp = client.get(f"/api/docx/{ND}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Текст закона об охране окружающей среды" in resp.text
    assert "attachment" not in resp.headers.get("content-disposition", "")


def test_get_docx_download_flag_returns_real_file_unchanged(tmp_path: Path, monkeypatch):
    """«Скачать» (download=1) — настоящий файл .docx как есть, без предпросмотра."""
    db_path = tmp_path / "npa.db"
    monkeypatch.setattr(webapp, "DEFAULT_DB", db_path)
    docx_path = tmp_path / "doc.docx"
    _real_docx(docx_path)
    DocumentStore(db_path).record_ips_document(
        nd=ND, adoption=None, state=None, docx_path=docx_path
    )

    client = TestClient(webapp.app)
    resp = client.get(f"/api/docx/{ND}?download=1")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert resp.content == docx_path.read_bytes()
    assert "attachment" in resp.headers.get("content-disposition", "")


def test_get_docx_open_falls_back_to_raw_file_when_not_a_real_docx(tmp_path: Path, monkeypatch):
    """Файл с расширением .docx, который не парсится (не zip/OOXML) — тихий
    фолбэк на обычную отдачу, а не 500 — см. _office_file_response."""
    db_path = tmp_path / "npa.db"
    monkeypatch.setattr(webapp, "DEFAULT_DB", db_path)
    docx_path = tmp_path / "broken.docx"
    docx_path.write_bytes(b"not a real docx")
    DocumentStore(db_path).record_ips_document(
        nd=ND, adoption=None, state=None, docx_path=docx_path
    )

    client = TestClient(webapp.app)
    resp = client.get(f"/api/docx/{ND}")
    assert resp.status_code == 200
    assert resp.content == b"not a real docx"


def _seed_its_word_file(db_path: Path, *, file_id: int, docx_path: Path) -> None:
    """Прямые INSERT в its_documents/its_files/its_document_files — тот же
    приём, что test_service_ebpi.py._seed_authority, т.к. FK проверяются
    (PRAGMA foreign_keys = ON, см. npa_pipeline.db.connect)."""
    import sqlite3

    from npa_pipeline.db import connect, init_schema

    with connect(db_path) as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO its_documents (url_id, designation, designation_norm, first_seen_at, updated_at) "
            "VALUES (1640, 'ИТС 28-2021', 'итс 28-2021', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO its_files (file_id, url_id, role, updated_at) VALUES (?, 1640, 'order', 'now')",
            (file_id,),
        )
        conn.execute(
            "INSERT INTO its_document_files (file_id, file_path, sha256, size_bytes, downloaded_at) "
            "VALUES (?, ?, 'x', 1, 'now')",
            (file_id, str(docx_path)),
        )
        conn.commit()


def test_its_pdf_endpoint_open_also_previews_docx(tmp_path: Path, monkeypatch):
    """Тот же фикс для /api/its/pdf/{file_id} — общая точка
    _office_file_response, не дублированная отдельно для ИТС."""
    db_path = tmp_path / "npa.db"
    monkeypatch.setattr(webapp, "DEFAULT_DB", db_path)
    docx_path = tmp_path / "its_order.docx"
    _real_docx(docx_path, "Текст приказа Росстандарта")
    _seed_its_word_file(db_path, file_id=555, docx_path=docx_path)

    client = TestClient(webapp.app)

    opened = client.get("/api/its/pdf/555")
    assert opened.status_code == 200
    assert opened.headers["content-type"].startswith("text/html")
    assert "Текст приказа Росстандарта" in opened.text

    downloaded = client.get("/api/its/pdf/555?download=1")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert downloaded.content == docx_path.read_bytes()


def test_get_docx_404_when_file_missing_from_disk(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "npa.db"
    monkeypatch.setattr(webapp, "DEFAULT_DB", db_path)
    missing_path = tmp_path / "gone.docx"  # запись в БД есть, файла на диске нет
    DocumentStore(db_path).record_ips_document(
        nd=ND, adoption=None, state=None, docx_path=missing_path
    )

    client = TestClient(webapp.app)
    resp = client.get(f"/api/docx/{ND}")
    assert resp.status_code == 404


def test_docx_text_prefers_ebpi_over_text_layer(tmp_path: Path, monkeypatch):
    """/api/docx-text/{eo_number}: EBPI в приоритете, как и в service.py
    (_attach_ebpi_text перед _attach_text_layer_docx) — покрывает пробел,
    из-за которого найденный EBPI-текст НПА не был доступен через веб-UI
    вообще (ни кнопки, ни эндпоинта)."""
    db_path = tmp_path / "npa.db"
    monkeypatch.setattr(webapp, "DEFAULT_DB", db_path)
    store = DocumentStore(db_path)
    _seed_document(store)

    ebpi_docx = tmp_path / "ebpi.docx"
    _real_docx(ebpi_docx, "Текст из EBPI")
    store.record_ebpi(eo_number=EO, covered=True, docx_path=ebpi_docx)

    # текстовый слой тоже «есть» (на случай будущей регрессии приоритета)
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    store.record_file(eo_number=EO, file_path=pdf_path, sha256="x", size_bytes=1, pages=1)
    text_layer_docx = pdf_path.with_suffix(".docx")
    _real_docx(text_layer_docx, "Текст из текстового слоя")

    client = TestClient(webapp.app)
    resp = client.get(f"/api/docx-text/{EO}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Текст из EBPI" in resp.text
    assert "Текст из текстового слоя" not in resp.text


def test_docx_text_falls_back_to_text_layer_when_ebpi_not_covered(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "npa.db"
    monkeypatch.setattr(webapp, "DEFAULT_DB", db_path)
    store = DocumentStore(db_path)
    _seed_document(store)
    store.record_ebpi(eo_number=EO, covered=False)  # EBPI проверялся, не покрыл

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    store.record_file(eo_number=EO, file_path=pdf_path, sha256="x", size_bytes=1, pages=1)
    text_layer_docx = pdf_path.with_suffix(".docx")
    _real_docx(text_layer_docx, "Текст из текстового слоя")

    client = TestClient(webapp.app)
    resp = client.get(f"/api/docx-text/{EO}?download=1")
    assert resp.status_code == 200
    assert resp.content == text_layer_docx.read_bytes()


def test_docx_text_404_when_neither_source_available(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "npa.db"
    monkeypatch.setattr(webapp, "DEFAULT_DB", db_path)
    store = DocumentStore(db_path)
    _seed_document(store)

    client = TestClient(webapp.app)
    resp = client.get(f"/api/docx-text/{EO}")
    assert resp.status_code == 404


def test_npa_pdf_label_marks_unamended_original():
    assert webapp._npa_pdf_label() == "PDF (документ без внесенных изменений)"


def test_npa_docx_label_ebpi_with_amendments_includes_source_and_date():
    from npa_pipeline.models import Result, Status

    result = Result(
        status=Status.FOUND,
        ebpi_docx_path="/tmp/x.docx",
        ebpi_docstate="Действует с изменениями",
        ebpi_red_date="20260901",
    )
    assert webapp._npa_docx_label(result) == (
        "DOCX (actual.pravo.gov.ru · Действует с изменениями от 01.09.2026г.)"
    )


def test_npa_docx_label_ebpi_unamended_has_source_without_redaction_date():
    from npa_pipeline.models import Result, Status

    result = Result(
        status=Status.FOUND,
        ebpi_docx_path="/tmp/x.docx",
        ebpi_docstate="Действует без изменений",
        ebpi_red_date="20230217",
    )
    assert webapp._npa_docx_label(result) == (
        "DOCX (actual.pravo.gov.ru · Действует без изменений)"
    )


def test_npa_docx_label_text_layer_and_ips():
    from npa_pipeline.models import Result, Status

    text_layer = Result(status=Status.FOUND, text_layer_docx_path="/tmp/t.docx")
    assert webapp._npa_docx_label(text_layer) == "DOCX (текстовый слой PDF)"

    ips = Result(
        status=Status.FOUND_TEXT_ONLY,
        ips_nd="102000000",
        ips_docx_path="/tmp/i.docx",
        ips_state="Действует с изменениями",
    )
    assert webapp._npa_docx_label(ips) == (
        "DOCX (ips.pravo.gov.ru · Действует с изменениями)"
    )
