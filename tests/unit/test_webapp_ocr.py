"""Маршруты OCR в webapp.py (/api/ocr, /api/ocr/jobs/{id}, .../docx) — через
TestClient, с OcrJobManager на httpx.MockTransport (без реального сервиса/сети).

Регрессия (поймана вручную 2026-10-14, не юнит-тестами): api_ocr_docx читал
job.status напрямую — AttributeError на новом (HTTP-клиентском) OcrJob, который
хранил статус только в приватном _status. Эти тесты гоняют именно маршрут
webapp.py, не только ocr_jobs.OcrJob — чтобы повторная регрессия такого рода
ловилась здесь, а не руками."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from io import BytesIO

import httpx
import pytest
from pypdf import PdfWriter
from fastapi.testclient import TestClient

from npa_pipeline import webapp
from npa_pipeline.db import DocumentStore
from npa_pipeline.models import DocItem
from npa_pipeline.ocr_jobs import OcrJobManager

EO = "0001202610020001"
JOB_ID = "abc123"


def _seed_document_with_pdf(store: DocumentStore, pdf_path: Path, eo_number: str = EO) -> None:
    store.upsert_from_doc_item(
        DocItem(eo_number=eo_number, number="711", document_date=date(2026, 10, 2), signatory_ids=[])
    )
    pdf_path.write_bytes(b"%PDF-fake")
    store.record_file(eo_number=eo_number, file_path=pdf_path, sha256="x", size_bytes=9, pages=1)


def _ocr_handler(*, done: bool):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/jobs" and request.method == "POST":
            return httpx.Response(202, json={"job_id": JOB_ID})
        if path == f"/jobs/{JOB_ID}" and request.method == "GET":
            status = "done" if done else "running"
            return httpx.Response(
                200,
                json={"status": status, "percent": 100.0 if done else 10.0, "page": 1,
                      "total_pages": 1, "page_status": "готово" if done else "распознаётся",
                      "steps": [], "elapsed_seconds": 5, "return_code": 0 if done else None,
                      "error": None},
            )
        if path == f"/jobs/{JOB_ID}/result" and request.method == "GET":
            return httpx.Response(200, content=b"fake docx bytes")
        return httpx.Response(404)

    return handler


def _client(tmp_path: Path, monkeypatch, *, done: bool) -> TestClient:
    db_path = tmp_path / "npa.db"
    monkeypatch.setattr(webapp, "DEFAULT_DB", db_path)
    store = DocumentStore(db_path)
    pdf_path = tmp_path / "doc.pdf"
    _seed_document_with_pdf(store, pdf_path)

    manager = OcrJobManager(
        tmp_path,
        service_url="http://test-ocr",
        client=httpx.Client(transport=httpx.MockTransport(_ocr_handler(done=done))),
    )
    monkeypatch.setattr(webapp, "ocr_jobs", manager)
    return TestClient(webapp.app)


def test_ocr_docx_download_returns_real_file_when_done(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch, done=True)
    started = client.post("/api/ocr", json={"eo_number": EO})
    assert started.status_code == 200
    assert started.json()["status"] == "done"

    resp = client.get(f"/api/ocr/jobs/{started.json()['job_id']}/docx")
    assert resp.status_code == 200
    assert resp.content == b"fake docx bytes"
    assert resp.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_ocr_docx_download_404_while_still_running(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch, done=False)
    started = client.post("/api/ocr", json={"eo_number": EO})
    assert started.json()["status"] == "running"

    resp = client.get(f"/api/ocr/jobs/{started.json()['job_id']}/docx")
    assert resp.status_code == 404


def test_ocr_status_route_reports_running_then_done(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch, done=True)
    started = client.post("/api/ocr", json={"eo_number": EO})
    job_id = started.json()["job_id"]

    status = client.get(f"/api/ocr/jobs/{job_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "done"
    assert status.json()["docx_ready"] is True


def test_authority_from_title_for_ministry_orders():
    """Орган для npa-ocr: у приказа министерства — министерство в именительном падеже (шапка под
    краем штампа Минюста дописывается по нему)."""
    title = ('Приказ Министерства природных ресурсов и экологии Российской Федерации от 31.08.2023 № 565'
             '<br /> "Об утверждении нормативного документа"')
    assert webapp._authority_from_title(title) == "Министерство природных ресурсов и экологии Российской Федерации"
    assert webapp._authority_from_title("Постановление Правительства Российской Федерации от 15.09.2020 № 1430") == (
        "Правительство Российской Федерации")
    assert webapp._authority_from_title("Приказ Росстандарта от 29.09.2025 № 2095") is None


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_upload_pdf_downloads_result_and_cleans_temporary_file(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, done=True)
    manager = webapp.ocr_jobs
    original_start = manager.start
    uploaded = []

    def start(pdf, *, authority_name=None):
        assert pdf.read_bytes() == _pdf_bytes()
        assert authority_name == "Министерство природы"
        uploaded.append(pdf)
        return original_start(pdf, authority_name=authority_name)

    monkeypatch.setattr(manager, "start", start)
    response = client.post(
        "/api/ocr/upload", files={"file": ("документ.pdf", _pdf_bytes(), "application/pdf")},
        data={"authority_name": " Министерство природы "},
    )
    assert response.status_code == 200
    job = response.json()
    assert job["docx_ready"] is True
    assert not uploaded[0].exists()
    assert client.get(f"/api/ocr/jobs/{job['job_id']}/docx").content == b"fake docx bytes"


@pytest.mark.parametrize("name,content", [("file.txt", b"text"), ("fake.pdf", b"not a pdf"), ("empty.pdf", b"")])
def test_upload_rejects_invalid_pdf(tmp_path, monkeypatch, name, content):
    client = _client(tmp_path, monkeypatch, done=False)
    monkeypatch.setattr(webapp.ocr_jobs, "start", lambda *a, **kw: pytest.fail("Invalid PDF reached OCR"))
    assert client.post("/api/ocr/upload", files={"file": (name, content)}).status_code == 400


@pytest.mark.parametrize("code,expected", [(409, 409), (503, 503)])
def test_upload_reports_ocr_unavailable_and_cleans_file(tmp_path, monkeypatch, code, expected):
    from npa_pipeline.ocr_jobs import JobBusyError, OcrServiceUnavailableError

    client = _client(tmp_path, monkeypatch, done=False)
    uploaded = []

    def start(pdf, **kwargs):
        uploaded.append(pdf)
        raise JobBusyError() if code == 409 else OcrServiceUnavailableError("offline")

    monkeypatch.setattr(webapp.ocr_jobs, "start", start)
    response = client.post("/api/ocr/upload", files={"file": ("scan.pdf", _pdf_bytes())})
    assert response.status_code == expected
    assert not uploaded[0].exists()


def test_upload_same_filename_has_unique_results(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, done=True)
    paths = []
    original = webapp.ocr_jobs.start

    def start(pdf, **kwargs):
        job = original(pdf, **kwargs)
        paths.append(job.docx_path)
        return job

    monkeypatch.setattr(webapp.ocr_jobs, "start", start)
    for _ in range(2):
        assert client.post("/api/ocr/upload", files={"file": ("same.pdf", _pdf_bytes())}).status_code == 200
    assert paths[0] != paths[1]


def test_upload_rejects_oversized_pdf(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, done=False)
    monkeypatch.setattr(webapp, "MAX_UPLOAD_BYTES", 10)
    assert client.post("/api/ocr/upload", files={"file": ("scan.pdf", _pdf_bytes())}).status_code == 413


def test_upload_rejects_encrypted_pdf(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, done=False)
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.encrypt("password")
    stream = BytesIO()
    writer.write(stream)
    assert client.post("/api/ocr/upload", files={"file": ("scan.pdf", stream.getvalue())}).status_code == 400
