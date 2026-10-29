"""Фоновые задания распознавания: HTTP-клиент сервиса npa-ocr.

npa-ocr — отдельный сервис (адрес — ``OCR_SERVICE_URL``, см.
docs/ocr-integration.md). Модели и ограничение «одно задание одновременно» —
на его стороне (занятый сервис отвечает 409). Клиент отправляет PDF, опрашивает
ход задания и скачивает готовый .docx в ``out_root/ocr``.

:func:`count_scan_pages` проверяет текстовый слой PDF локально, без обращения к
сервису.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DEFAULT_SERVICE_URL = "http://localhost:8000"

# Совпадает с DEFAULT_MIN_PAGE_TEXT_CHARS npa-ocr: по этому порогу он решает, распознавать
# страницу или взять текстовый слой. Константа повторена, потому что npa-downloader не
# зависит от пакета npa-ocr.
DEFAULT_MIN_PAGE_TEXT_CHARS = 20


class JobBusyError(RuntimeError):
    """Сервис npa-ocr уже выполняет другое задание; одновременно разрешено одно."""

    def __init__(self) -> None:
        super().__init__("Уже выполняется распознавание. Дождитесь окончания.")


class OcrServiceUnavailableError(RuntimeError):
    """Сервис npa-ocr недоступен по сети (не поднят, не отвечает, таймаут)."""


def count_scan_pages(pdf_path: Path | str) -> tuple[int, int]:
    """Считает страницы PDF без текстового слоя — тем же порогом, что npa-ocr.

    Returns:
        (всего страниц, страниц без текстового слоя). Второй элемент 0 означает,
        что документ не скан — текст уже есть на всех страницах, OCR не нужен.
    """
    import pypdf

    reader = pypdf.PdfReader(str(pdf_path))
    scan = 0
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if len(text) < DEFAULT_MIN_PAGE_TEXT_CHARS:
            scan += 1
    return len(reader.pages), scan


@dataclass
class OcrJob:
    """Состояние задания сервиса npa-ocr.

    ``_status`` — JSON последнего опроса сервиса как есть, чтобы не дублировать
    его формат.
    """

    job_id: str
    docx_path: Path
    _status: dict[str, Any] = field(default_factory=dict)
    downloaded: bool = False
    """Результат этого задания уже скачан.

    Это не то же, что ``docx_path.is_file()``: под тем же именем может лежать
    .docx предыдущего распознавания того же документа, и проверка по файлу
    оставила бы его вместо нового результата.
    """

    @property
    def status(self) -> str:
        """Статус задания: ``running``, ``done``, ``failed`` или ``cancelled``."""
        return self._status.get("status", "running")

    def to_dict(self) -> dict[str, Any]:
        """Состояние задания для ответа ``GET /api/ocr/jobs/{job_id}``."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "docx_ready": self.status == "done" and self.downloaded,
            "percent": self._status.get("percent"),
            "page": self._status.get("page"),
            "total_pages": self._status.get("total_pages"),
            "page_status": self._status.get("page_status"),
            "steps": self._status.get("steps", []),
            "elapsed_seconds": self._status.get("elapsed_seconds"),
            "return_code": self._status.get("return_code"),
            "error": self._status.get("error"),
        }


class OcrJobManager:
    """Отправляет PDF в сервис npa-ocr и отслеживает задания через HTTP.

    Args:
        out_root: Корень для скачанных результатов; .docx пишутся в
            ``out_root/ocr``.
        service_url: адрес сервиса npa-ocr; по умолчанию — переменная окружения
            OCR_SERVICE_URL (для docker-compose — имя сервиса в общей сети,
            например "http://ocr:8000"), иначе DEFAULT_SERVICE_URL (локальная
            разработка без compose).
        client: httpx.Client — подменяется в тестах (MockTransport); по
            умолчанию создаётся и владеется этим менеджером (закрывается в close()).
    """

    def __init__(
        self,
        out_root: Path,
        *,
        service_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.out_dir = Path(out_root) / "ocr"
        self.service_url = (
            service_url or os.environ.get("OCR_SERVICE_URL") or DEFAULT_SERVICE_URL
        ).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, read=120.0))
        self._jobs: dict[str, OcrJob] = {}

    def get(self, job_id: str) -> OcrJob | None:
        """Возвращает задание с обновлённым статусом.

        Returns:
            Задание или None, если этот процесс его не запускал (задания
            хранятся только в памяти и теряются при перезапуске).
        """
        job = self._jobs.get(job_id)
        if job is not None:
            self._refresh(job)
        return job

    def start(self, pdf_path: Path, *, authority_name: str | None = None) -> OcrJob:
        """Отправляет pdf_path в сервис npa-ocr (multipart) и начинает отслеживать задание.

        Returns:
            Созданный OcrJob — статус уже опрошен один раз сразу после отправки
            (см. _refresh), не обязательно ещё "running" (быстрый документ мог
            успеть завершиться).

        Raises:
            JobBusyError: сервис уже выполняет другое задание (409).
            OcrServiceUnavailableError: сервис недоступен по сети.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        data = {"authority_name": authority_name} if authority_name else {}
        try:
            with open(pdf_path, "rb") as f:
                resp = self._client.post(
                    f"{self.service_url}/jobs",
                    files={"file": (pdf_path.name, f, "application/pdf")},
                    data=data,
                )
        except httpx.TransportError as exc:
            raise OcrServiceUnavailableError(str(exc)) from exc
        if resp.status_code == 409:
            raise JobBusyError()
        resp.raise_for_status()
        job = OcrJob(job_id=resp.json()["job_id"], docx_path=self.out_dir / f"{pdf_path.stem}.docx")
        self._jobs[job.job_id] = job
        self._refresh(job)
        return job

    def cancel(self, job: OcrJob) -> None:
        """Просит сервис остановить задание.

        Сбой сети при отмене не считается ошибкой: следующий опрос покажет
        состояние задания.
        """
        try:
            self._client.post(f"{self.service_url}/jobs/{job.job_id}/cancel")
        except httpx.TransportError:
            pass
        self._refresh(job)

    def _refresh(self, job: OcrJob) -> None:
        """Опрашивает статус задания; готовый результат скачивает один раз.

        Raises:
            OcrServiceUnavailableError: Сервис недоступен или ответил ошибкой.
        """
        try:
            resp = self._client.get(f"{self.service_url}/jobs/{job.job_id}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OcrServiceUnavailableError(str(exc)) from exc
        job._status = resp.json()
        if job._status.get("status") == "done" and not job.downloaded:
            self._download_result(job)

    def _download_result(self, job: OcrJob) -> None:
        """Скачивает .docx готового задания в ``job.docx_path``."""
        resp = self._client.get(f"{self.service_url}/jobs/{job.job_id}/result")
        resp.raise_for_status()
        job.docx_path.write_bytes(resp.content)
        job.downloaded = True

    def close(self) -> None:
        """Закрывает HTTP-клиент, если он создан менеджером, а не передан извне."""
        if self._owns_client:
            self._client.close()
