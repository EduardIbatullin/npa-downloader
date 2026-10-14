"""Фоновые задания распознавания — HTTP-клиент к сервису npa-ocr (на
httpx.MockTransport, без реальной сети/модели)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from npa_pipeline.ocr_jobs import (
    JobBusyError,
    OcrJobManager,
    OcrServiceUnavailableError,
    count_scan_pages,
)

JOB_ID = "abc123"
SERVICE_URL = "http://test-ocr"


def test_count_scan_pages_counts_pages_without_text_layer(tmp_path: Path):
    import fitz

    pdf = tmp_path / "mixed.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "This page has a real text layer that is long enough to count.")
    doc.new_page()  # пустая страница — скан без текстового слоя
    doc.save(str(pdf))
    doc.close()
    assert count_scan_pages(pdf) == (2, 1)


def _manager(tmp_path: Path, handler) -> OcrJobManager:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OcrJobManager(tmp_path, service_url=SERVICE_URL, client=client)


def _running_then_done_handler(*, polls_before_done: int = 1):
    state = {"get_calls": 0, "cancel_called": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/jobs" and request.method == "POST":
            return httpx.Response(202, json={"job_id": JOB_ID})
        if path == f"/jobs/{JOB_ID}" and request.method == "GET":
            state["get_calls"] += 1
            if state["cancel_called"]:
                return httpx.Response(
                    200,
                    json={"status": "cancelled", "percent": 50.0, "page": 1, "total_pages": 2,
                          "page_status": None, "steps": [], "elapsed_seconds": 1,
                          "return_code": None, "error": None},
                )
            if state["get_calls"] <= polls_before_done:
                return httpx.Response(
                    200,
                    json={"status": "running", "percent": 10.0, "page": 1, "total_pages": 2,
                          "page_status": "распознаётся", "steps": [], "elapsed_seconds": 1,
                          "return_code": None, "error": None},
                )
            return httpx.Response(
                200,
                json={"status": "done", "percent": 100.0, "page": 2, "total_pages": 2,
                      "page_status": "готово", "steps": ["рендер страницы: 0 с"], "elapsed_seconds": 5,
                      "return_code": 0, "error": None},
            )
        if path == f"/jobs/{JOB_ID}/result" and request.method == "GET":
            return httpx.Response(200, content=b"fake docx content")
        if path == f"/jobs/{JOB_ID}/cancel" and request.method == "POST":
            state["cancel_called"] = True
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(404)

    return handler, state


def test_start_uploads_pdf_and_returns_job_with_status(tmp_path: Path):
    handler, _ = _running_then_done_handler()
    manager = _manager(tmp_path, handler)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")

    job = manager.start(pdf)
    assert job.job_id == JOB_ID
    assert job.to_dict()["status"] == "running"
    assert job.to_dict()["docx_ready"] is False
    # Прямой атрибут .status (не только через to_dict()) — webapp.py,
    # api_ocr_docx, читает именно так; регрессия поймана вручную 2026-10-14
    # (AttributeError: 'OcrJob' object has no attribute 'status').
    assert job.status == "running"


def test_get_polls_and_downloads_result_when_done(tmp_path: Path):
    # polls_before_done=2: 1-й опрос расходуется внутри start() (см. _refresh
    # там же), 2-й — явный manager.get() ниже, оба ещё "running"; 3-й — "done".
    handler, _ = _running_then_done_handler(polls_before_done=2)
    manager = _manager(tmp_path, handler)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")

    job = manager.start(pdf)
    assert manager.get(job.job_id).to_dict()["status"] == "running"

    final = manager.get(job.job_id)
    assert final.to_dict()["status"] == "done"
    assert final.to_dict()["docx_ready"] is True
    assert final.status == "done"  # прямой атрибут — см. webapp.py, api_ocr_docx
    assert final.docx_path.read_bytes() == b"fake docx content"
    assert final.docx_path == tmp_path / "ocr" / "doc.docx"


def test_get_unknown_job_id_returns_none_without_network_call(tmp_path: Path):
    def _should_not_be_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("не должно быть сетевого вызова для незнакомого job_id")

    manager = _manager(tmp_path, _should_not_be_called)
    assert manager.get("unknown") is None


def test_start_raises_job_busy_on_409(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "занято"})

    manager = _manager(tmp_path, handler)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")
    with pytest.raises(JobBusyError):
        manager.start(pdf)


def test_start_raises_service_unavailable_on_network_error(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    manager = _manager(tmp_path, handler)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")
    with pytest.raises(OcrServiceUnavailableError):
        manager.start(pdf)


def test_cancel_calls_service_and_refreshes_status(tmp_path: Path):
    handler, state = _running_then_done_handler(polls_before_done=100)
    manager = _manager(tmp_path, handler)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")
    job = manager.start(pdf)

    manager.cancel(job)
    assert state["cancel_called"] is True
    assert job.to_dict()["status"] == "cancelled"
