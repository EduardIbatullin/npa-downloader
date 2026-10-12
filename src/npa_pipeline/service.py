"""Публичная точка входа библиотеки."""

from __future__ import annotations

from pathlib import Path

import httpx

from npa_pipeline import api as pravo_api
from npa_pipeline import ebpi
from npa_pipeline.authorities import AuthorityCache
from npa_pipeline.db import DEFAULT_DB_PATH, DocumentStore
from npa_pipeline.download import IntegrityError, download_document
from npa_pipeline.ebpi_docx import convert_redtext_to_docx
from npa_pipeline.http_client import NetworkError, create_client
from npa_pipeline.models import DocItem, Query, Result, Status
from npa_pipeline.search import search_document


def _persist_search_hits(
    client: httpx.Client,
    store: DocumentStore,
    result: Result,
) -> Result:
    """Пишет found / ambiguous в БД (полная карточка). Обновляет DocItem из карточки."""
    docs: list[DocItem] = []
    if result.status == Status.FOUND and result.document is not None:
        docs = [result.document]
    elif result.status == Status.AMBIGUOUS and result.candidates:
        docs = list(result.candidates)
    else:
        return result

    enriched: list[DocItem] = []
    for doc in docs:
        try:
            card = pravo_api.get_document_card(client, doc.eo_number)
            store.upsert_from_card(card)
            enriched.append(pravo_api.doc_item_from_card(card))
        except Exception:
            store.upsert_from_doc_item(doc)
            enriched.append(doc)

    if result.status == Status.FOUND:
        result.document = enriched[0]
        result.eo_number = enriched[0].eo_number
    else:
        result.candidates = enriched
    return result


def _attach_ebpi_text(ebpi_client: httpx.Client, store: DocumentStore, result: Result) -> Result:
    """Пробует найти документ в EBPI и сохранить его текст рядом с уже скачанным PDF.

    Сначала смотрит кэш в БД (document_ebpi) — в сеть идёт только если записи нет
    или сохранённый .docx пропал с диска. Кэшируется и отрицательный результат
    (документ не покрыт EBPI — так для большинства приказов, см. npa_pipeline.ebpi):
    иначе каждый повторный fetch_document бил бы в сеть заново за уже известным
    «нет».

    Молчаливо не меняет result, если документ не покрыт EBPI или что-то пошло не
    так (сеть, неожиданный HTML) — отсутствие «ворда» не должно портить уже
    успешный результат с PDF. Путь к .docx выводится из пути к PDF (тот же stem),
    а не через unique_path: так повторный вызов на том же документе перезаписывает
    файл, а не плодит дубликаты — текст EBPI детерминирован для данной редакции."""
    assert result.pdf_path is not None
    eo_number = result.eo_number
    assert eo_number is not None

    cached = store.get_ebpi(eo_number)
    if cached is not None:
        if not cached.covered:
            return result  # уже проверяли — EBPI этот документ не покрывает
        if cached.docx_path and Path(cached.docx_path).is_file():
            result.ebpi_docx_path = cached.docx_path
            result.ebpi_docstate = cached.docstate
            return result
        # запись есть, но файла на диске нет (удалили/перенесли) — перекачаем

    try:
        doc = ebpi.fetch_ebpi_document(ebpi_client, eo_number)
        if doc is None:
            store.record_ebpi(eo_number=eo_number, covered=False)
            return result
        docx_path = Path(result.pdf_path).with_suffix(".docx")
        convert_redtext_to_docx(doc.html, docx_path)
        result.ebpi_docx_path = str(docx_path)
        result.ebpi_docstate = doc.docstate
        store.record_ebpi(
            eo_number=eo_number,
            covered=True,
            docid=doc.docid,
            docstate=doc.docstate,
            redid=doc.redid,
            red_date=doc.red_date,
            docx_path=docx_path,
        )
    except Exception:
        # Любой сбой здесь (сеть, неожиданная разметка) не должен портить уже
        # успешный результат — PDF скачан, этого достаточно для статуса FOUND.
        # В кэш ничего не пишем — при следующем вызове попробуем ещё раз, а не
        # застрянем на «не покрыт» из-за временного сбоя сети.
        pass
    return result


def fetch_document(
    query: Query,
    *,
    client: httpx.Client | None = None,
    ebpi_client: httpx.Client | None = None,
    cache: AuthorityCache | None = None,
    store: DocumentStore | None = None,
    cache_path: str | Path = DEFAULT_DB_PATH,
    db_path: str | Path | None = None,
    out_dir: str | Path = "downloads",
    download: bool = True,
    fetch_ebpi_text: bool = True,
) -> Result:
    """Ищет документ, сохраняет в БД, скачивает PDF и при успехе пробует найти
    текст в EBPI (actual.pravo.gov.ru — независимый от скана, с таблицами; есть не
    у всех видов актов, см. npa_pipeline.ebpi). Порядок: PDF — в любом случае;
    EBPI — если PDF скачан и документ там покрыт; OCR сюда не входит, это внешний
    инструмент (см. docs/ocr-integration.md), вызывается отдельно, когда
    ebpi_docx_path не заполнился.

    Все сетевые/целостностные исключения превращаются в статусы контракта. Сбой
    именно EBPI-шага не портит результат — он не обязателен для успеха (см.
    _attach_ebpi_text).
    """
    owns_client = client is None
    owns_ebpi_client = ebpi_client is None
    path = Path(db_path) if db_path is not None else Path(cache_path)
    if client is None:
        client = create_client()
    if ebpi_client is None and fetch_ebpi_text:
        ebpi_client = ebpi.create_ebpi_client()
    if cache is None:
        cache = AuthorityCache(path)
    if store is None:
        store = DocumentStore(path)

    try:
        result = search_document(client, query, cache)
        if result.status in (Status.FOUND, Status.AMBIGUOUS):
            result = _persist_search_hits(client, store, result)

        if result.status != Status.FOUND or not download:
            return result

        assert result.document is not None and result.eo_number is not None
        try:
            file_path = download_document(
                client,
                eo_number=result.eo_number,
                query=query,
                document=result.document,
                out_dir=Path(out_dir),
                store=store,
            )
            result.pdf_path = str(file_path)
            if fetch_ebpi_text and ebpi_client is not None:
                result = _attach_ebpi_text(ebpi_client, store, result)
            return result
        except IntegrityError as exc:
            return Result(
                status=Status.INTEGRITY_ERROR,
                eo_number=result.eo_number,
                document=result.document,
                match_type=result.match_type,
                message=str(exc),
            )
        except NetworkError as exc:
            return Result(
                status=Status.NETWORK_ERROR,
                eo_number=result.eo_number,
                document=result.document,
                match_type=result.match_type,
                message=str(exc),
            )
    except NetworkError as exc:
        return Result(status=Status.NETWORK_ERROR, message=str(exc))
    except Exception as exc:
        return Result(status=Status.NETWORK_ERROR, message=f"Неожиданная ошибка: {exc}")
    finally:
        if owns_client:
            client.close()
        if owns_ebpi_client and ebpi_client is not None:
            ebpi_client.close()
