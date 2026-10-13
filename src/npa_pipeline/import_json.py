"""Импорт старых JSON-sidecar из downloads/ в единую БД."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from npa_pipeline.db import DocumentStore
from npa_pipeline.models import DocItem, parse_api_date


def _doc_from_sidecar(info: dict[str, Any]) -> DocItem | None:
    eo = info.get("eoNumber") or info.get("eo_number")
    doc = info.get("document") or {}
    if not eo:
        return None
    number = doc.get("number") or info.get("number") or ""
    raw_date = doc.get("document_date") or doc.get("documentDate")
    doc_date = parse_api_date(raw_date) if raw_date else None
    if doc_date is None:
        doc_date = date(1970, 1, 1)
    signatories = doc.get("signatory_ids") or doc.get("signatoryIds") or []
    return DocItem(
        eo_number=str(eo),
        number=str(number),
        document_date=doc_date,
        signatory_ids=list(signatories),
        document_type_id=doc.get("document_type_id") or doc.get("documentTypeId"),
        name=doc.get("name"),
        complex_name=doc.get("complex_name") or doc.get("complexName"),
        pdf_file_length=doc.get("pdf_file_length") or doc.get("pdfFileLength"),
    )


def _resolve_pdf(out: Path, meta_path: Path, info: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []
    filename = info.get("filename")
    if filename:
        p = Path(filename)
        candidates.append(p if p.is_absolute() else out / p)
    candidates.append(out / f"{meta_path.stem}.pdf")
    for path in candidates:
        if path.is_file():
            return path
    return None


def import_json_sidecars(
    out_dir: str | Path,
    store: DocumentStore,
    *,
    delete_json: bool = False,
) -> dict[str, int]:
    """Переносит старые *.json sidecar-файлы из каталога загрузок в БД.

    Args:
        out_dir: каталог со старыми sidecar-файлами и PDF рядом с ними.
        store: БД, куда переносятся карточки документов и записи о файлах.
        delete_json: удалять ли *.json после успешного переноса (PDF не трогается).

    Returns:
        {"imported": N, "skipped": N, "deleted": N} — "skipped" объединяет
        несколько разных причин (нечитаемый JSON, sidecar без eoNumber, не
        нашёлся PDF рядом — см. _resolve_pdf), они не различаются в счётчике.
    """
    out = Path(out_dir)
    imported = 0
    skipped = 0
    deleted = 0
    if not out.is_dir():
        return {"imported": 0, "skipped": 0, "deleted": 0}

    for meta_path in sorted(out.glob("*.json")):
        try:
            info = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue

        doc = _doc_from_sidecar(info)
        if doc is None:
            skipped += 1
            continue

        pdf_path = _resolve_pdf(out, meta_path, info)
        if pdf_path is None:
            skipped += 1
            continue

        store.upsert_from_doc_item(doc)
        sha256 = info.get("sha256") or hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        size = int(info.get("size") or pdf_path.stat().st_size)
        pages = info.get("pages")
        store.record_file(
            eo_number=doc.eo_number,
            file_path=pdf_path.resolve(),
            sha256=sha256,
            size_bytes=size,
            pages=int(pages) if pages is not None else None,
        )
        imported += 1
        if delete_json:
            meta_path.unlink(missing_ok=True)
            deleted += 1

    return {"imported": imported, "skipped": skipped, "deleted": deleted}
