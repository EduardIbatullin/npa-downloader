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

import httpx
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
