"""Веб-интерфейс для проверки API поиска и скачивания НПА / ИТС."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query as FastQuery
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from npa_pipeline import burondt
from npa_pipeline.authorities import AuthorityCache
from npa_pipeline.db import DEFAULT_DB_PATH, DocumentStore
from npa_pipeline.download import find_pdf_by_eo
from npa_pipeline.ocr_jobs import JobBusyError, OcrJobManager, count_scan_pages
from npa_pipeline.http_client import create_client
from npa_pipeline.its import ItsCache
from npa_pipeline.its_download import fetch_its, fetch_its_by_url_id
from npa_pipeline.models import Query, parse_user_date
from npa_pipeline.naming import format_document_title, human_filename_stem
from npa_pipeline.parse_citation import parse_citation
from npa_pipeline.service import fetch_document

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_DB = ROOT / DEFAULT_DB_PATH
DEFAULT_OUT = ROOT / "downloads"

_ITS_HINT = re.compile(
    r"^\s*(?:итс|ндт)\b",
    re.IGNORECASE,
)
_YEAR_ONLY = re.compile(r"^\s*\d{4}\s*$")

app = FastAPI(title="NPA Pipeline", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
ocr_jobs = OcrJobManager(DEFAULT_OUT)


class FetchRequest(BaseModel):
    title: str | None = None
    eo_number: str | None = None
    authority_guid: str | None = None
    authority_name: str | None = None
    number: str | None = None
    date: str | None = None
    download: bool = True


class ParseTitleRequest(BaseModel):
    title: str


class FetchItsRequest(BaseModel):
    designation: str | None = None
    url_id: int | None = None
    download: bool = True


class OcrStartRequest(BaseModel):
    eo_number: str
    authority_name: str | None = None  # из ответа поиска (ocr.authority_name)


class LookupRequest(BaseModel):
    """Единый вход для проверочного UI: название НПА или обозначение ИТС/НДТ."""

    q: str | None = None
    url_id: int | None = None  # выбор версии ИТС из ambiguous


def _cache() -> AuthorityCache:
    return AuthorityCache(DEFAULT_DB)


def _store() -> DocumentStore:
    return DocumentStore(DEFAULT_DB)


def _its_cache() -> ItsCache:
    return ItsCache(DEFAULT_DB)


def _looks_like_its(q: str) -> bool:
    """Эвристика: ИТС/НДТ или голый год (как в CLI fetch-its)."""
    text = q.strip()
    if _ITS_HINT.match(text):
        return True
    if _YEAR_ONLY.match(text):
        return True
    # «ИТС 53» без года уже покрыто ^итс; «28-2021» без префикса — редкость
    return False


def _its_file_label(role: str, caption: str | None = None) -> str:
    """Подпись для UI: сначала caption с карточки, иначе роль."""
    role_fallback = {
        "document": "Справочник (PDF)",
        "document_word": "Справочник (Word)",
        "order": "Приказ",
        "cancellation": "Отмена",
        "unknown": "Файл",
    }.get(role, role)
    text = (caption or "").strip()
    if not text:
        return role_fallback
    # убрать слишком длинные хвосты
    if len(text) > 90:
        text = text[:87].rstrip() + "…"
    return text


def _pack_its_files(files: list[dict]) -> list[dict]:
    out = []
    for f in files:
        file_id = f.get("file_id")
        if not file_id or not f.get("pdf_path"):
            continue
        role = f.get("role") or "unknown"
        caption = f.get("caption")
        out.append(
            {
                "label": _its_file_label(role, caption),
                "role": role,
                "caption": caption,
                "open_url": f"/api/its/pdf/{file_id}",
                "download_url": f"/api/its/pdf/{file_id}?download=1",
            }
        )
    return out


def _lookup_its(*, designation: str | None = None, url_id: int | None = None) -> dict:
    """Ищет и скачивает ИТС/НДТ, собирает ответ для проверочного UI.

    Args:
        designation: обозначение (см. ItsCache.resolve) — обязательно, если
            url_id не передан.
        url_id: конкретная карточка, если уже выбрана (например, после ambiguous
            из первого запроса по designation) — резолвинг по designation
            пропускается, см. fetch_its_by_url_id.
    """
    cache = _its_cache()
    with burondt.create_burondt_client() as client:
        if url_id is not None:
            result = fetch_its_by_url_id(
                client,
                url_id=url_id,
                out_dir=DEFAULT_OUT / "its",
                cache=cache,
                download=True,
                designation=designation,
            )
        else:
            result = fetch_its(
                client,
                designation=(designation or "").strip(),
                out_dir=DEFAULT_OUT / "its",
                cache=cache,
                download=True,
            )

    payload = result.to_dict()
    for f in payload.get("files", []):
        if f.get("pdf_path"):
            f["pdf_url"] = f"/api/its/pdf/{f['file_id']}"

    description = payload.get("designation") or designation or ""
    if result.status.value == "found" and result.url_id is not None:
        card = cache.get_card(result.url_id)
        if card and card.get("title") and card["title"] != card.get("designation"):
            description = f"{card['designation']} — {card['title']}"
        elif card:
            description = card.get("designation") or description

    return {
        "kind": "its",
        "status": payload["status"],
        "title": payload.get("designation") or designation,
        "description": description,
        "message": payload.get("message"),
        "files": _pack_its_files(payload.get("files") or []),
        "candidates": [
            {
                "kind": "its",
                "url_id": c.get("url_id"),
                "title": c.get("designation"),
                "description": (
                    f"{c.get('designation')} — {c['title']}"
                    if c.get("title") and c.get("title") != c.get("designation")
                    else c.get("designation")
                ),
            }
            for c in (payload.get("candidates") or [])
        ],
        "raw": payload,
    }


def _authority_from_title(title: str | None) -> str | None:
    """Орган-подписант по названию акта — только для тех, где восстановление должности поддержано."""
    if not title:
        return None
    if title.startswith("Постановление Правительства"):
        return "Правительство Российской Федерации"
    if title.startswith("Указ Президента"):
        return "Президент Российской Федерации"
    return None


def _ocr_info(pdf_path: str | None, eo_number: str | None, title: str | None) -> dict | None:
    """Считает страницы-сканы у PDF — для кнопки «Распознать скан» в UI.

    Returns:
        None, если pdf_path или eo_number не заданы (документ не найден/не
        скачан — кнопке нечего показывать). Иначе — словарь со счётчиками страниц
        и authority_name (см. _authority_from_title), который уходит в запрос
        на запуск OCR.
    """
    if not pdf_path or not eo_number:
        return None
    total, scan = count_scan_pages(pdf_path)
    return {
        "eo_number": eo_number,
        "total_pages": total,
        "scan_pages": scan,
        "authority_name": _authority_from_title(title),
    }


def _lookup_npa(q: str) -> dict:
    """Разбирает название НПА, ищет и скачивает документ, собирает ответ для UI.

    Args:
        q: название/реквизиты в произвольном виде — разбирается parse_citation.

    Raises:
        HTTPException: 400, если q не разобрался в реквизиты (см. parse_citation).
    """
    try:
        parsed = parse_citation(q)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = fetch_document(
        parsed.to_query(),
        cache_path=DEFAULT_DB,
        out_dir=DEFAULT_OUT,
        download=True,
    )
    payload = result.to_dict()
    payload["parsed"] = parsed.to_dict()

    doc = payload.get("document") or {}
    description = format_document_title(
        name=doc.get("name"),
        complex_name=doc.get("complex_name"),
        number=doc.get("number") or parsed.number,
        document_date=doc.get("document_date") or parsed.date.isoformat(),
    ).strip() or q
    files: list[dict] = []
    if result.pdf_path and result.eo_number:
        files.append(
            {
                "label": "PDF",
                "role": "document",
                "open_url": f"/api/pdf/{result.eo_number}",
                "download_url": f"/api/pdf/{result.eo_number}?download=1",
            }
        )
        payload["pdf_url"] = f"/api/pdf/{result.eo_number}"

    ocr = _ocr_info(
        result.pdf_path,
        result.eo_number,
        format_document_title(
            name=doc.get("name"),
            complex_name=doc.get("complex_name"),
            number=doc.get("number") or parsed.number,
            document_date=doc.get("document_date") or parsed.date.isoformat(),
        ),
    )

    return {
        "kind": "npa",
        "ocr": ocr,
        "status": payload["status"],
        "title": f"№ {parsed.number} от {parsed.date.isoformat()}" if parsed else None,
        "description": description,
        "message": payload.get("message"),
        "files": files,
        "candidates": [
            {
                "kind": "npa",
                "eo_number": c.get("eo_number"),
                "title": c.get("number"),
                "description": (
                    f"№ {c.get('number')} от {c.get('document_date')}"
                    f" · {c.get('eo_number')}"
                ),
            }
            for c in (payload.get("candidates") or [])
        ],
        "parsed": parsed.to_dict(),
        "raw": payload,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    cache = _cache()
    store = _store()
    its_cache = _its_cache()
    return {
        "ok": True,
        "authorities_cached": cache.count(),
        "documents": store.count_documents(),
        "files": store.count_files(),
        "its_cached": its_cache.count_its_documents(),
        "its_files": its_cache.count_its_files(),
        "its_range_lo": its_cache.range_lower_bound(),
        "its_range_hi_confirmed": its_cache.range_upper_bound_confirmed(),
        "db_path": str(DEFAULT_DB),
        "downloads_dir": str(DEFAULT_OUT),
    }


@app.post("/api/lookup")
def api_lookup(body: LookupRequest) -> dict:
    """Единая точка для проверочного UI: НПА по названию или ИТС/НДТ по обозначению."""
    if body.url_id is not None:
        return _lookup_its(url_id=body.url_id, designation=(body.q or None))

    q = (body.q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Укажите название НПА или обозначение ИТС/НДТ")

    if _looks_like_its(q):
        return _lookup_its(designation=q)

    try:
        return _lookup_npa(q)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — UI должен увидеть причину
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/refresh-authorities")
def refresh_authorities() -> dict:
    cache = _cache()
    with create_client() as client:
        count = cache.build(client)
    return {"authorities": count, "db_path": str(DEFAULT_DB)}


@app.get("/api/resolve")
def resolve_authority(name: str = FastQuery(min_length=1)) -> dict:
    cache = _cache()
    guids = cache.resolve(authority_name=name)
    import sqlite3

    items: list[dict] = []
    if guids:
        with sqlite3.connect(cache.db_path) as conn:
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(guids))
            rows = conn.execute(
                f"SELECT guid, name FROM authorities WHERE guid IN ({placeholders})",
                guids,
            ).fetchall()
            by_id = {r["guid"]: r["name"] for r in rows}
        items = [{"guid": g, "name": by_id.get(g, g)} for g in guids]
    return {"count": len(items), "candidates": items}


@app.get("/api/authorities/suggest")
def suggest_authorities(
    q: str = FastQuery(min_length=1),
    limit: int = FastQuery(default=15, ge=1, le=50),
) -> dict:
    cache = _cache()
    items = cache.suggest(q, limit=limit)
    return {"count": len(items), "items": items}


@app.post("/api/fetch")
def api_fetch(body: FetchRequest) -> dict:
    parsed = None
    title = (body.title or "").strip()
    if title:
        try:
            parsed = parse_citation(title)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        query = parsed.to_query()
    else:
        date_val = None
        if body.date and body.date.strip():
            try:
                date_val = parse_user_date(body.date.strip())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Некорректная дата: {exc}") from exc

        query = Query(
            eo_number=(body.eo_number or "").strip() or None,
            authority_guid=(body.authority_guid or "").strip() or None,
            authority_name=(body.authority_name or "").strip() or None,
            number=(body.number or "").strip() or None,
            date=date_val,
        )
    result = fetch_document(
        query,
        cache_path=DEFAULT_DB,
        out_dir=DEFAULT_OUT,
        download=body.download,
    )
    payload = result.to_dict()
    if parsed is not None:
        payload["parsed"] = parsed.to_dict()
    if result.pdf_path:
        eo = result.eo_number or ""
        payload["pdf_url"] = f"/api/pdf/{eo}" if eo else None
    return payload


@app.post("/api/parse-title")
def api_parse_title(body: ParseTitleRequest) -> dict:
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Укажите title")
    try:
        return parse_citation(title).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/pdf/{eo_number}")
def get_pdf(
    eo_number: str,
    download: bool = FastQuery(default=False),
) -> FileResponse:
    """Отдаёт PDF по eoNumber; при download=1 пересобирает короткое имя файла.

    Имя на диске может быть старым/длинным (например, если naming.py менялся
    после того, как файл был скачан) — при скачивании через браузер пересобираем
    его по текущим правилам (human_filename_stem), а не отдаём как есть, иначе
    пользователь долго получал бы отличающиеся от актуального стиля имена.
    """
    store = _store()
    path = find_pdf_by_eo(store, eo_number)
    if path is None:
        raise HTTPException(status_code=404, detail="PDF не найден на диске")
    filename = path.name
    if download:
        row = store.get_document_row(eo_number)
        if row is not None:
            from datetime import date as date_cls

            from npa_pipeline.models import DocItem

            raw_date = str(row.get("document_date") or "")[:10]
            try:
                doc_date = date_cls.fromisoformat(raw_date)
            except ValueError:
                doc_date = date_cls.today()
            stem = human_filename_stem(
                DocItem(
                    eo_number=eo_number,
                    number=row.get("number") or eo_number,
                    document_date=doc_date,
                    signatory_ids=[],
                    name=row.get("name"),
                    complex_name=row.get("complex_name"),
                )
            )
            filename = f"{stem}.pdf"
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type="attachment" if download else "inline",
    )


@app.post("/api/refresh-its")
def refresh_its(max_requests: int = FastQuery(default=300, ge=1, le=3000)) -> dict:
    cache = _its_cache()
    with burondt.create_burondt_client() as client:
        stats = cache.refresh(client, full_rescan=False, max_requests=max_requests)
    return {**stats, "db_path": str(DEFAULT_DB)}


@app.get("/api/its/search")
def search_its(designation: str = FastQuery(min_length=1)) -> dict:
    cache = _its_cache()
    url_ids = cache.resolve(designation)
    items = [cache.get_card(u) for u in url_ids]
    return {"count": len(items), "candidates": [i for i in items if i is not None]}


@app.post("/api/fetch-its")
def api_fetch_its(body: FetchItsRequest) -> dict:
    if body.url_id is None and not (body.designation and body.designation.strip()):
        raise HTTPException(status_code=400, detail="Укажите designation или url_id")

    cache = _its_cache()
    with burondt.create_burondt_client() as client:
        if body.url_id is not None:
            result = fetch_its_by_url_id(
                client,
                url_id=body.url_id,
                out_dir=DEFAULT_OUT / "its",
                cache=cache,
                download=body.download,
                designation=body.designation,
            )
        else:
            result = fetch_its(
                client,
                designation=body.designation.strip(),
                out_dir=DEFAULT_OUT / "its",
                cache=cache,
                download=body.download,
            )
    payload = result.to_dict()
    for f in payload.get("files", []):
        if f.get("pdf_path"):
            f["pdf_url"] = f"/api/its/pdf/{f['file_id']}"
    return payload


@app.post("/api/ocr")
def api_ocr_start(body: OcrStartRequest) -> dict:
    pdf = find_pdf_by_eo(_store(), body.eo_number.strip())
    if pdf is None:
        raise HTTPException(status_code=404, detail="PDF не найден на диске. Сначала найдите документ.")
    try:
        job = ocr_jobs.start(pdf, authority_name=(body.authority_name or None))
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.to_dict()


def _job_or_404(job_id: str):
    job = ocr_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание не найдено (возможно, сервер перезапускался)")
    return job


@app.get("/api/ocr/jobs/{job_id}")
def api_ocr_status(job_id: str) -> dict:
    return _job_or_404(job_id).to_dict()


@app.post("/api/ocr/jobs/{job_id}/cancel")
def api_ocr_cancel(job_id: str) -> dict:
    job = _job_or_404(job_id)
    ocr_jobs.cancel(job)
    return job.to_dict()


@app.get("/api/ocr/jobs/{job_id}/docx")
def api_ocr_docx(job_id: str) -> FileResponse:
    job = _job_or_404(job_id)
    if job.status != "done" or not job.docx_path.is_file():
        raise HTTPException(status_code=404, detail="Распознавание ещё не завершено")
    return FileResponse(
        job.docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=job.docx_path.name,
        content_disposition_type="attachment",
    )


@app.get("/api/its/pdf/{file_id}")
def get_its_pdf(
    file_id: int,
    download: bool = FastQuery(default=False),
) -> FileResponse:
    cache = _its_cache()
    rec = cache.get_file_record(file_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Файл не найден на диске")
    path = Path(rec["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден на диске")
    suffix = path.suffix.lower()
    media = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
    }.get(suffix, "application/octet-stream")
    return FileResponse(
        path,
        media_type=media,
        filename=path.name,
        content_disposition_type="attachment" if download else "inline",
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        "npa_pipeline.webapp:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
    )


if __name__ == "__main__":
    main()
