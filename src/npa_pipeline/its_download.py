"""Скачивание и проверка файлов ИТС/НДТ. Переиспользует download.verify_pdf и
атомарную запись (.part → replace), но со своим ключом (file_id, не eo_number) и
своими таблицами (its_files/its_document_files) — карточка ИТС структурно
несёт 1:N файлов, а не 1:1, как documents/document_files.

Если у того же обозначения есть карточка «в формате Word», её файлы тоже
скачиваются и попадают в результат рядом с PDF (без проверки %PDF).
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path

import httpx

from npa_pipeline import burondt
from npa_pipeline.download import IntegrityError, unique_path, verify_pdf
from npa_pipeline.http_client import NetworkError
from npa_pipeline.its import ItsCache, _is_word_variant
from npa_pipeline.models import ItsFileResult, ItsResult, Status
from npa_pipeline.text_layer_docx import extract_text_layer_to_docx, has_full_text_layer

_WIN_FORBIDDEN = '<>:"/\\|?*'
_SPACES = re.compile(r"\s+")

_ROLE_LABEL = {
    "document": "справочник",
    "document_word": "справочник Word",
    "order": "приказ",
    "cancellation": "отмена",
    "unknown": "файл",
}


def _sanitize_filename(text: str) -> str:
    text = _SPACES.sub(" ", text).strip()
    for ch in _WIN_FORBIDDEN:
        text = text.replace(ch, "_")
    return text.rstrip(" .")


def _detect_file_kind(data: bytes, filename: str | None) -> tuple[str, str]:
    """Возвращает (kind, suffix): kind = pdf | word | unknown."""
    if data.startswith(b"%PDF"):
        return "pdf", ".pdf"
    if data.startswith(b"PK"):
        return "word", ".docx"
    name = (filename or "").casefold()
    if name.endswith(".docx"):
        return "word", ".docx"
    if name.endswith(".doc"):
        return "word", ".doc"
    if name.endswith(".pdf"):
        return "pdf", ".pdf"
    return "unknown", ".bin"


def _effective_role(role: str, kind: str, *, from_word_card: bool, caption: str | None = None) -> str:
    # Подпись надёжнее сохранённой роли (в кэше могли остаться старые «document»).
    if caption:
        role = burondt._classify_role(caption)
    if kind == "word" or from_word_card:
        if role == "document":
            return "document_word"
    return role


def _attach_text_layer_docx(pdf_path: Path) -> str | None:
    """Если PDF не скан (текстовый слой есть на всех страницах) — механически
    извлекает текст в .docx того же stem (см. npa_pipeline.text_layer_docx). ИТС
    структурно не ищутся в EBPI (нет eoNumber) — это не fallback после EBPI, как у
    НПА (npa_pipeline.service), а единственный путь получить .docx без OCR. Скан
    или любой сбой — None, молча, без исключения."""
    try:
        if not has_full_text_layer(pdf_path):
            return None
        docx_path = pdf_path.with_suffix(".docx")
        if not docx_path.is_file():
            extract_text_layer_to_docx(pdf_path, docx_path)
        return str(docx_path)
    except Exception:
        return None


def already_downloaded_its(cache: ItsCache, file_id: int) -> Path | None:
    """Если файл на месте и sha256 совпадает с БД — возвращает путь (аналог already_downloaded)."""
    rec = cache.get_file_record(file_id)
    if rec is None:
        return None
    path = Path(rec["file_path"])
    if not path.is_file():
        return None
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest == rec["sha256"]:
            return path
    except OSError:
        return None
    return None


def save_its_file(
    out_dir: Path,
    *,
    data: bytes,
    designation: str,
    file_id: int,
    role: str,
    pages: int | None,
    cache: ItsCache,
    suffix: str = ".pdf",
) -> Path:
    """Атомарная запись (.part → replace), sha256, регистрация в its_document_files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir.resolve()

    stem = _sanitize_filename(f"{designation} — {_ROLE_LABEL.get(role, role)}")
    path = unique_path(out_dir, stem, suffix)

    sha256 = hashlib.sha256(data).hexdigest()
    with tempfile.NamedTemporaryFile(dir=out_dir, suffix=".part", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)

    cache.record_file(
        file_id=file_id,
        file_path=str(path.resolve()),
        sha256=sha256,
        size_bytes=len(data),
        pages=pages,
    )
    return path


def _collect_card_files(
    cache: ItsCache,
    url_ids: list[int],
) -> list[tuple[dict, dict, bool]]:
    """Список (file_meta, card_row, from_word_card) без дублей file_id."""
    seen: set[int] = set()
    out: list[tuple[dict, dict, bool]] = []
    for uid in url_ids:
        card = cache.get_card(uid)
        if card is None:
            continue
        word_card = _is_word_variant(card.get("designation"))
        for f in cache.list_files(uid):
            fid = f["file_id"]
            if fid in seen:
                continue
            seen.add(fid)
            out.append((f, card, word_card))
    return out


def fetch_its(
    client: httpx.Client,
    *,
    designation: str,
    out_dir: Path,
    cache: ItsCache,
    download: bool = True,
) -> ItsResult:
    """Резолвит designation → url_id, скачивает и проверяет файлы карточки.

    Никогда не бросает — сбои превращаются в ItsResult.status (переиспользует
    общий Status: FOUND/NOT_FOUND/AMBIGUOUS/NETWORK_ERROR/INTEGRITY_ERROR).

    При AMBIGUOUS в каждый кандидат добавляется его список файлов (files) — чтобы
    в UI можно было показать «справочник + приказ» и ссылку на скачивание конкретной
    версии без ещё одного резолвинга по обозначению (см. fetch_its_by_url_id) — важно,
    если у двух версий совпадает выводимое обозначение (например, обеим разрешат
    показаться по общему номеру или году).
    """
    if not designation or not designation.strip():
        return ItsResult(status=Status.INVALID_INPUT, designation=designation, message="Пустое обозначение")

    candidates = cache.resolve(designation)
    if not candidates:
        return ItsResult(status=Status.NOT_FOUND, designation=designation)
    if len(candidates) > 1:
        cand_cards = []
        for u in candidates:
            card = cache.get_card(u)
            if card is None:
                continue
            card = dict(card)
            # к кандидату сразу приклеиваем связанные Word/PDF url_id (метаданные файлов)
            related = [u] + cache.find_related_url_ids(u)
            files: list[dict] = []
            seen: set[int] = set()
            for rid in related:
                for f in cache.list_files(rid):
                    if f["file_id"] in seen:
                        continue
                    seen.add(f["file_id"])
                    files.append(f)
            card["files"] = files
            cand_cards.append(card)
        return ItsResult(status=Status.AMBIGUOUS, designation=designation, candidates=cand_cards)

    return fetch_its_by_url_id(
        client,
        url_id=candidates[0],
        out_dir=out_dir,
        cache=cache,
        download=download,
        designation=designation,
    )


def fetch_its_by_url_id(
    client: httpx.Client,
    *,
    url_id: int,
    out_dir: Path,
    cache: ItsCache,
    download: bool = True,
    designation: str | None = None,
) -> ItsResult:
    """Как fetch_its, но по уже известному url_id — без повторного резолвинга.

    Нужен для UI: когда resolve() вернул несколько версий (AMBIGUOUS), пользователь
    выбирает одну по её url_id, а не вводит обозначение заново (обозначение у версий
    может совпадать или быть неполным — см. resolve).

    Дополнительно скачивает файлы связанных карточек того же ИТС (PDF + Word).
    """
    card_row = cache.get_card(url_id)
    if card_row is None:
        return ItsResult(status=Status.NOT_FOUND, designation=designation or str(url_id))
    designation = designation or card_row["designation"]

    if not download:
        return ItsResult(status=Status.FOUND, designation=designation, url_id=url_id)

    url_ids = [url_id] + cache.find_related_url_ids(url_id)
    file_jobs = _collect_card_files(cache, url_ids)

    results: list[ItsFileResult] = []
    to_download: list[tuple[dict, dict, bool]] = []

    for f, card, from_word in file_jobs:
        existing = already_downloaded_its(cache, f["file_id"])
        caption = f.get("caption")
        if existing is not None:
            kind = "word" if existing.suffix.lower() in {".doc", ".docx"} else "pdf"
            role = _effective_role(
                f["role"], kind, from_word_card=from_word, caption=caption
            )
            results.append(
                ItsFileResult(
                    role=role,
                    file_id=f["file_id"],
                    pdf_path=str(existing),
                    caption=caption,
                    order_number_caption=f.get("order_number_caption"),
                    order_date_caption=f.get("order_date_caption"),
                    text_layer_docx_path=_attach_text_layer_docx(existing) if kind == "pdf" else None,
                )
            )
        else:
            to_download.append((f, card, from_word))

    downloaded: list[tuple[dict, dict, bool, bytes, str | None]] = []
    for f, card, from_word in to_download:
        try:
            data, _content_length, filename = burondt.download_file(client, f["file_id"])
        except NetworkError as exc:
            return ItsResult(
                status=Status.NETWORK_ERROR,
                designation=designation,
                url_id=url_id,
                message=str(exc),
            )
        downloaded.append((f, card, from_word, data, filename))

    for f, card, from_word, data, filename in downloaded:
        kind, suffix = _detect_file_kind(data, filename)
        caption = f.get("caption")
        role = _effective_role(
            f["role"], kind, from_word_card=from_word, caption=caption
        )
        pages: int | None
        if kind == "pdf":
            try:
                pages = verify_pdf(data, content_length=None)
            except IntegrityError as exc:
                return ItsResult(
                    status=Status.INTEGRITY_ERROR,
                    designation=designation,
                    url_id=url_id,
                    message=str(exc),
                )
        elif kind == "word":
            pages = None
        else:
            return ItsResult(
                status=Status.INTEGRITY_ERROR,
                designation=designation,
                url_id=url_id,
                message="Неизвестный формат файла (ожидался PDF или Word)",
            )

        path = save_its_file(
            out_dir,
            data=data,
            designation=card["designation"],
            file_id=f["file_id"],
            role=role,
            pages=pages,
            cache=cache,
            suffix=suffix,
        )
        results.append(
            ItsFileResult(
                role=role,
                file_id=f["file_id"],
                pdf_path=str(path),
                pages=pages,
                caption=caption,
                order_number_caption=f.get("order_number_caption"),
                order_date_caption=f.get("order_date_caption"),
                text_layer_docx_path=_attach_text_layer_docx(path) if kind == "pdf" else None,
            )
        )

    # отмена / приказ после справочника; Word рядом со справочником
    role_order = {
        "document": 0,
        "document_word": 1,
        "order": 2,
        "cancellation": 3,
        "unknown": 4,
    }
    results.sort(key=lambda r: (role_order.get(r.role, 9), r.file_id))

    return ItsResult(status=Status.FOUND, designation=designation, url_id=url_id, files=results)
