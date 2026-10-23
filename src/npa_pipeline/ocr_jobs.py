"""Фоновые задания распознавания для веб-приложения — HTTP-клиент к сервису
npa-ocr (отдельный проект, свой образ, свой контейнер; адрес — OCR_SERVICE_URL,
см. docs/ocr-integration.md).

Сервис сам управляет тяжёлыми моделями и ограничением «одно задание
одновременно» (вернёт 409, если занят) — здесь только: отправить PDF
(multipart), опросить прогресс (`GET /jobs/{id}`), скачать готовый .docx
(`GET /jobs/{id}/result`) в out_root/ocr, когда статус стал "done".

count_scan_pages() — отдельная, не часть этого клиента: механическая проверка
текстового слоя (pypdf) для надписи «N страниц без текстового слоя» в карточке
НПА, не требует обращения к сервису.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DEFAULT_SERVICE_URL = "http://localhost:8000"

# Должно совпадать с DEFAULT_MIN_PAGE_TEXT_CHARS в npa-ocr (src/npa_ocr/ocr.py) —
# именно по этому порогу npa-ocr решает, распознавать страницу или взять текстовый
# слой как есть. Это текстовая константа внешнего контракта, не импорт: npa-downloader
# намеренно не зависит от пакета npa-ocr.
DEFAULT_MIN_PAGE_TEXT_CHARS = 20


class JobBusyError(RuntimeError):
    """Сервис npa-ocr уже выполняет другое задание (разрешено только одно
    одновременно — тяжёлые модели в памяти, см. его README)."""

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
    """Локальное отражение состояния удалённого задания — то, что видит веб-UI.

    _status — сырой JSON последнего опроса сервиса (см. OcrJobManager._refresh);
    хранится как есть, не разбирается на отдельные поля датакласса, чтобы формат
    не дублировался и не расходился с тем, что реально отдаёт сервис.
    """

    job_id: str
    docx_path: Path
    _status: dict[str, Any] = field(default_factory=dict)
    downloaded: bool = False
    """Результат ЭТОГО задания уже скачан (см. OcrJobManager._download_result).
    Не путать с docx_path.is_file() — по тому же имени документа на диске может
    лежать .docx от ПРЕДЫДУЩЕГО запуска распознавания (тот же eo_number,
    перезапустили после фикса) ещё до того, как текущее задание его перезапишет;
    тогда is_file() истинно, а этот флаг — ещё нет. Баг в проверке именно по
    is_file() приводил к тому, что новый, уже готовый результат не скачивался
    и не перезаписывал старый файл — пользователь получал устаревший .docx,
    хотя сервис распознал документ заново и правильно (обнаружено 2026-10-23
    на Приказе №191 после фикса TATR-детекции)."""

    @property
    def status(self) -> str:
        """Прямой доступ к статусу (как у старого, subprocess-based OcrJob) —
        для кода вне этого модуля, который не ходит через to_dict() целиком
        (см. webapp.py, api_ocr_docx)."""
        return self._status.get("status", "running")

    def to_dict(self) -> dict[str, Any]:
        """Снимок состояния для JSON-ответа веб-API (/api/ocr/jobs/{job_id}) —
        та же форма, что была у старого (subprocess-based) OcrJob, фронтенд не
        меняется."""
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
        out_root: корень для скачанных результатов; сами .docx пишутся в
            out_root/ocr (как и раньше).
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
        """Возвращает задание с уже обновлённым статусом (см. _refresh), либо
        None — такой job_id этот процесс не запускал (например, сервер
        перезапускался — задания хранятся только в памяти)."""
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
        """Просит сервис остановить задание; сбой сети здесь не считается
        ошибкой — просто задание продолжит идти, следующий опрос статуса
        покажет его актуальное состояние."""
        try:
            self._client.post(f"{self.service_url}/jobs/{job.job_id}/cancel")
        except httpx.TransportError:
            pass
        self._refresh(job)

    def _refresh(self, job: OcrJob) -> None:
        """Опрашивает статус задания в сервисе; если он стал "done" — сразу же
        скачивает результат (см. _download_result), если ещё не скачан именно
        для этого задания (job.downloaded — см. его докстринг; не проверка
        docx_path.is_file(), на диске может лежать файл от предыдущего запуска)."""
        try:
            resp = self._client.get(f"{self.service_url}/jobs/{job.job_id}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OcrServiceUnavailableError(str(exc)) from exc
        job._status = resp.json()
        if job._status.get("status") == "done" and not job.downloaded:
            self._download_result(job)

    def _download_result(self, job: OcrJob) -> None:
        resp = self._client.get(f"{self.service_url}/jobs/{job.job_id}/result")
        resp.raise_for_status()
        job.docx_path.write_bytes(resp.content)
        job.downloaded = True

    def close(self) -> None:
        """Закрывает HTTP-клиент, если им владеет этот менеджер (не передан
        извне в конструктор — тогда закрытие не наша ответственность)."""
        if self._owns_client:
            self._client.close()
