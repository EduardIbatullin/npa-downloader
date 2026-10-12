"""Фоновое распознавание скана для веб-приложения.

OCR живёт в отдельном проекте (npa-ocr, https://github.com/EduardIbatullin/npa-ocr)
и запускается здесь как внешний процесс — установленная команда `npa-ocr` (см. её
README, раздел «Внешний контракт»), а не импорт модуля. Так тяжёлые модели (PaddleOCR,
torch) не живут в процессе веб-сервера, а падение или остановка защитой памяти не
роняет приложение. Одновременно выполняется не больше одного задания — на машине
с 16 ГБ RAM параллельные прогоны уже приводили к нехватке памяти.

Команда `npa-ocr` должна быть на PATH того окружения, где запущен веб-сервер
(`pip install npa-ocr[models]` в тот же venv, или отдельно и путь передан через
command_builder). Без неё задания будут падать сразу с кодом возврата, отличным от 0.

Прогресс берётся из журнала stderr дочернего процесса (строки «[  x%] страница N из M»).
Результат — DOCX, промежуточные страницы лежат в <stem>.pages: повторный запуск того
же PDF продолжает с места остановки.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Должно совпадать с DEFAULT_MIN_PAGE_TEXT_CHARS в npa-ocr (src/npa_ocr/ocr.py) —
# именно по этому порогу npa-ocr решает, распознавать страницу или взять текстовый
# слой как есть. Это текстовая константа внешнего контракта, не импорт: npa-downloader
# намеренно не зависит от пакета npa-ocr (см. докстринг модуля).
DEFAULT_MIN_PAGE_TEXT_CHARS = 20

PROGRESS_RE = re.compile(r"\[\s*([\d.]+)%\]\s*страница\s+(\d+)\s+из\s+(\d+):\s*(.+)")
STEP_RE = re.compile(r"^\s*\[стр\.\s*\d+\]\s*(.+?):\s*([\d.]+)\s*(с|мин)")


class JobBusyError(RuntimeError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"Уже выполняется распознавание (задание {job_id}). Дождитесь окончания.")
        self.job_id = job_id


def count_scan_pages(pdf_path: Path | str) -> tuple[int, int]:
    """(всего страниц, страниц без текстового слоя) — тем же порогом, что ocr_document."""
    import pypdf

    reader = pypdf.PdfReader(str(pdf_path))
    scan = 0
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if len(text) < DEFAULT_MIN_PAGE_TEXT_CHARS:
            scan += 1
    return len(reader.pages), scan


def read_progress(progress_path: Path) -> dict:
    """Последний прогресс и последние завершённые шаги из журнала дочернего процесса."""
    result = {"percent": None, "page": None, "total": None, "page_status": None, "steps": []}
    if not progress_path.is_file():
        return result
    try:
        text = progress_path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return result
    for line in text.splitlines():
        m = PROGRESS_RE.search(line)
        if m:
            result.update(
                percent=float(m.group(1)),
                page=int(m.group(2)),
                total=int(m.group(3)),
                page_status=m.group(4).strip(),
            )
            continue
        s = STEP_RE.match(line)
        if s:
            result["steps"].append(f"{s.group(1)}: {s.group(2)} {s.group(3)}")
    result["steps"] = result["steps"][-6:]
    return result


@dataclass
class OcrJob:
    job_id: str
    pdf_path: Path
    docx_path: Path
    progress_path: Path
    log_path: Path
    process: subprocess.Popen | None = None
    status: str = "running"  # running | done | failed | cancelled
    return_code: int | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        progress = read_progress(self.progress_path)
        return {
            "job_id": self.job_id,
            "status": self.status,
            "pdf": self.pdf_path.name,
            "docx_ready": self.status == "done" and self.docx_path.is_file(),
            "percent": progress["percent"],
            "page": progress["page"],
            "total_pages": progress["total"],
            "page_status": progress["page_status"],
            "steps": progress["steps"],
            "elapsed_seconds": round((self.finished_at or time.time()) - self.started_at),
            "return_code": self.return_code,
            "error": self.error,
        }


def _default_command(pdf_path: Path, docx_path: Path, authority_name: str | None) -> list[str]:
    """Внешняя команда npa-ocr (отдельный проект, должна быть на PATH) — не вызов
    модуля в этом процессе. См. докстринг модуля и README npa-ocr, «Внешний контракт»."""
    cmd = ["npa-ocr", str(pdf_path), "--out", str(docx_path)]
    if authority_name:
        cmd += ["--authority-name", authority_name]
    return cmd


class OcrJobManager:
    def __init__(
        self,
        out_root: Path,
        *,
        command_builder: Callable[[Path, Path, str | None], list[str]] = _default_command,
    ) -> None:
        self.out_dir = Path(out_root) / "ocr"
        self._command_builder = command_builder
        self._lock = threading.Lock()
        self._jobs: dict[str, OcrJob] = {}
        self._current: OcrJob | None = None

    def get(self, job_id: str) -> OcrJob | None:
        return self._jobs.get(job_id)

    def start(self, pdf_path: Path, *, authority_name: str | None = None) -> OcrJob:
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise JobBusyError(self._current.job_id)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            stem = pdf_path.stem
            docx_path = self.out_dir / f"{stem}.docx"
            progress_path = self.out_dir / f"{stem}.progress.log"
            job = OcrJob(
                job_id=uuid.uuid4().hex[:12],
                pdf_path=pdf_path,
                docx_path=docx_path,
                progress_path=progress_path,
                log_path=self.out_dir / f"{stem}.log",
            )
            env = {
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "FLAGS_use_mkldnn": "false",
            }
            cmd = self._command_builder(pdf_path, docx_path, authority_name)
            with open(job.log_path, "wb") as stdout_file, open(progress_path, "wb") as stderr_file:
                job.process = subprocess.Popen(cmd, stdout=stdout_file, stderr=stderr_file, env=env)
            self._jobs[job.job_id] = job
            self._current = job
        threading.Thread(target=self._watch, args=(job,), daemon=True).start()
        return job

    def cancel(self, job: OcrJob) -> None:
        with self._lock:
            if job.status != "running" or job.process is None:
                return
            job.status = "cancelled"
            job.process.kill()

    def _watch(self, job: OcrJob) -> None:
        code = job.process.wait()
        with self._lock:
            job.return_code = code
            job.finished_at = time.time()
            if job.status == "cancelled":
                return
            if code == 0 and job.docx_path.is_file():
                job.status = "done"
            else:
                job.status = "failed"
                job.error = self._failure_reason(job)

    @staticmethod
    def _failure_reason(job: OcrJob) -> str:
        # CLI при нехватке памяти печатает JSON со статусом в stdout
        try:
            out = json.loads(job.log_path.read_text(encoding="utf-8", errors="replace"))
            if out.get("status") == "insufficient_memory":
                return out.get("message", "Недостаточно памяти")
        except (OSError, ValueError):
            pass
        return f"Распознавание завершилось с кодом {job.return_code}. Журнал: {job.progress_path.name}"
