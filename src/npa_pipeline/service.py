"""Публичная точка входа библиотеки."""

from __future__ import annotations

from pathlib import Path

import httpx

from npa_pipeline import api as pravo_api
from npa_pipeline import ebpi
from npa_pipeline import ips_pravo
from npa_pipeline.authorities import AuthorityCache
from npa_pipeline.db import DEFAULT_DB_PATH, DocumentStore
from npa_pipeline.download import IntegrityError, download_document, unique_path
from npa_pipeline.ebpi_docx import convert_redtext_to_docx
from npa_pipeline.http_client import NetworkError, create_client
from npa_pipeline.models import DocItem, Query, Result, Status, parse_user_date
from npa_pipeline.naming import human_filename_stem_from_text
from npa_pipeline.search import search_document
from npa_pipeline.text_layer_docx import extract_text_layer_to_docx, has_full_text_layer


def _persist_search_hits(
    client: httpx.Client,
    store: DocumentStore,
    result: Result,
) -> Result:
    """Пишет found/ambiguous в БД (полная карточка).

    Обновляет DocItem из карточки.
    """
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

    Сначала смотрит кэш в БД (document_ebpi) — в сеть идёт только если записи нет,
    сохранённый .docx пропал с диска, или отрицательный результат устарел. Кэшируется
    и отрицательный результат (документ не покрыт EBPI — так для большинства
    приказов, см. npa_pipeline.ebpi): иначе каждый повторный fetch_document бил бы в
    сеть заново за уже известным «нет». У отрицательного результата TTL
    (ebpi.NEGATIVE_CACHE_TTL_DAYS, сейчас 21 день) — EBPI индексирует свежие
    документы не мгновенно (см. docs/ebpi-coverage.md, «Задержка индексации»), и без
    TTL документ, скачанный в день публикации, мог бы навсегда остаться помеченным
    «не покрыт», даже когда EBPI его позже проиндексирует.

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
            if not ebpi.is_negative_cache_expired(cached.checked_at):
                return result  # проверяли недавно — EBPI этот документ не покрывает
            # TTL истёк — пробуем сеть ещё раз: документ мог появиться в EBPI с
            # опозданием (см. docs/ebpi-coverage.md, «Задержка индексации»), не
            # предполагаем «не покрыт» навсегда
        elif cached.docx_path and Path(cached.docx_path).is_file():
            result.ebpi_docx_path = cached.docx_path
            result.ebpi_docstate = cached.docstate
            return result
        # covered, но файла на диске нет (удалили/перенесли) — перекачаем

    try:
        doc = ebpi.fetch_ebpi_document(ebpi_client, eo_number)
        if doc is None:
            store.record_ebpi(eo_number=eo_number, covered=False)
            return result
        docx_path = Path(result.pdf_path).with_suffix(".docx")
        if not doc.is_initial and doc.red_date:
            # PDF — всегда исходная публикация; эта редакция — с поправками, не то
            # же самое. Помечаем в имени файла, чтобы не выглядело как пара
            # «один документ, два формата» (см. docs/ebpi-coverage.md,
            # «PDF и DOCX — не одно и то же»).
            label = ebpi.format_red_date(doc.red_date)
            docx_path = docx_path.with_name(f"{docx_path.stem} (ред. от {label}).docx")
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
        for act in doc.amending_acts:
            _record_amending_act(store, target_eo=eo_number, act=act)
    except Exception:
        # Любой сбой здесь (сеть, неожиданная разметка) не должен портить уже
        # успешный результат — PDF скачан, этого достаточно для статуса FOUND.
        # В кэш ничего не пишем — при следующем вызове попробуем ещё раз, а не
        # застрянем на «не покрыт» из-за временного сбоя сети.
        pass
    return result


def _record_amending_act(store: DocumentStore, *, target_eo: str, act: "ebpi.AmendingAct") -> None:
    """Пишет акт-поправку в documents и связь «изменяет» в document_relations.

    Запись в documents минимальна (не полная карточка портала, см. упоминание FK в
    DocumentStore.record_relation). Сбой здесь не должен портить весь EBPI-шаг —
    вызывающий код уже в try/except, но на случай будущего использования отдельно
    обёрнуто ещё раз, чтобы один плохой акт-поправка не срывал запись остальных.
    """
    try:
        if act.date:
            store.upsert_from_doc_item(
                DocItem(
                    eo_number=act.eo_number,
                    number=act.number or "",
                    document_date=parse_user_date(act.date),
                    signatory_ids=[],
                    name=act.name,
                    complex_name=act.docpassing,
                )
            )
        store.record_relation(
            source_eo=act.eo_number,
            relation_type="amends",
            target_eo=target_eo,
            effective_date=act.effective_date,
            origin="ebpi",
        )
    except Exception:
        pass


def _attach_text_layer_docx(result: Result) -> Result:
    """Резервный путь, когда EBPI не дал текста: если PDF не скан (текстовый слой
    есть на всех страницах), механически извлекает текст в .docx того же stem, что
    у PDF (см. npa_pipeline.text_layer_docx) — без OCR, он здесь не нужен.

    Не трогает уже заполненный EBPI-результат (ebpi_docx_path) — EBPI даёт выверенный
    текст с таблицами, механическое извлечение хуже и применяется только как fallback.
    Для сканов (хоть одна страница без текстового слоя) не делает ничего — это
    задача внешнего npa-ocr (см. docs/ocr-integration.md). Любой сбой (повреждённый
    PDF, ошибка записи) не портит уже успешный результат с PDF."""
    if result.ebpi_docx_path is not None:
        return result
    assert result.pdf_path is not None
    try:
        if not has_full_text_layer(result.pdf_path):
            return result  # скан — работа OCR, не механического извлечения
        docx_path = Path(result.pdf_path).with_suffix(".docx")
        if not docx_path.is_file():
            extract_text_layer_to_docx(result.pdf_path, docx_path)
        result.text_layer_docx_path = str(docx_path)
    except Exception:
        pass
    return result


def _try_ips_fallback(
    ips_client: httpx.Client,
    result: Result,
    *,
    query: Query,
    out_dir: Path,
    store: DocumentStore,
) -> Result:
    """Пробует найти документ через ips.pravo.gov.ru, когда он старше границы
    охвата publication.pravo.gov.ru — единственная альтернатива для таких
    документов, PDF у них в принципе не существует (см.
    docs/ips-pravo-coverage.md).

    Args:
        result: уже вычисленный Status.OUT_OF_RANGE от основного поиска.
        query: должен содержать number и date — иначе основной поиск не мог бы
            прийти к out_of_range (см. search.search_document).
        store: нужен, чтобы запомнить nd -> docx_path (ips_documents) — без
            этого GET /api/docx/{nd} в webapp.py не мог бы отдать файл по
            отдельному запросу, и повторный поиск того же документа плодил бы
            дубликаты .docx вместо переиспользования уже готового файла.

    Returns:
        Status.FOUND_TEXT_ONLY с текстом (ips_docx_path), если резерв нашёл ровно
        одного кандидата и получил текст (из кэша в БД или свежим запросом);
        Status.AMBIGUOUS с ips_candidates, если кандидатов несколько; иначе
        исходный result (out_of_range) без изменений — ноль кандидатов, сбой сети
        или структуры ответа трактуются одинаково, не как ошибка всего результата.
    """
    assert query.number is not None and query.date is not None
    hits = ips_pravo.search_by_requisites(ips_client, number=query.number, doc_date=query.date)
    if not hits:
        return result
    if len(hits) > 1:
        return Result(
            status=Status.AMBIGUOUS,
            message=f"Найдено {len(hits)} документов на ips.pravo.gov.ru",
            ips_candidates=[h.to_dict() for h in hits],
        )

    hit = hits[0]
    cached = store.get_ips_document(hit.nd)
    if cached is not None and Path(cached.docx_path).is_file():
        return Result(
            status=Status.FOUND_TEXT_ONLY,
            ips_nd=hit.nd,
            ips_docx_path=cached.docx_path,
            ips_state=cached.state or hit.state,
            ips_adoption=cached.adoption or hit.adoption,
        )

    try:
        redactions = ips_pravo.fetch_redactions(ips_client, nd=hit.nd)
        chosen = ips_pravo.select_latest_redaction(redactions)
        if chosen is None:
            return result
        html = ips_pravo.fetch_text(ips_client, nd=hit.nd, rdk=chosen.rdk)
        if html is None:
            return result
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = human_filename_stem_from_text(hit.adoption, fallback=hit.nd)
        docx_path = unique_path(out_dir.resolve(), stem, ".docx")
        convert_redtext_to_docx(html, docx_path)
    except Exception:
        # Сбой здесь (сеть, неожиданная разметка) не должен превращать out_of_range
        # в ошибку всего результата — просто резерв не сработал в этот раз.
        return result

    ips_state = chosen.status or hit.state
    try:
        store.record_ips_document(nd=hit.nd, adoption=hit.adoption, state=ips_state, docx_path=docx_path)
    except Exception:
        # Сбой записи в БД не должен обнулять уже готовый текст — просто
        # GET /api/docx/{nd} не сработает для этого документа, пока не найдётся
        # заново (запись перезапишется при следующем успешном поиске).
        pass

    return Result(
        status=Status.FOUND_TEXT_ONLY,
        ips_nd=hit.nd,
        ips_docx_path=str(docx_path),
        ips_state=ips_state,
        ips_adoption=hit.adoption,
    )


def fetch_document(
    query: Query,
    *,
    client: httpx.Client | None = None,
    ebpi_client: httpx.Client | None = None,
    ips_client: httpx.Client | None = None,
    cache: AuthorityCache | None = None,
    store: DocumentStore | None = None,
    cache_path: str | Path = DEFAULT_DB_PATH,
    db_path: str | Path | None = None,
    out_dir: str | Path = "downloads",
    download: bool = True,
    fetch_ebpi_text: bool = True,
    fetch_text_layer_fallback: bool = True,
    fetch_ips_fallback: bool = True,
) -> Result:
    """Ищет документ, скачивает PDF и пробует получить его текст без OCR.

    Порядок: поиск и запись карточки в БД -> если результат out_of_range, резерв
    ips.pravo.gov.ru (npa_pipeline.ips_pravo, см. docs/ips-pravo-coverage.md) ->
    PDF, если download=True -> EBPI (actual.pravo.gov.ru — выверенный текст с
    таблицами; покрывает не все виды актов, см. npa_pipeline.ebpi) ->
    механическое извлечение текстового слоя (npa_pipeline.text_layer_docx), если
    EBPI документ не покрыл, но PDF не скан. OCR сюда не входит — это внешний
    инструмент (см. docs/ocr-integration.md), вызывается отдельно, когда ни
    ebpi_docx_path, ни text_layer_docx_path не заполнились.

    Все сетевые/целостностные исключения превращаются в Result.status, а не
    бросаются наружу. Сбой именно EBPI-, текстового или ips-шага не портит
    результат — ни один не обязателен для успеха (см. _attach_ebpi_text,
    _attach_text_layer_docx, _try_ips_fallback).

    Args:
        query: что искать — реквизиты, eoNumber или разобранная цитата (см.
            parse_citation.query_from_citation).
        fetch_ebpi_text: False — пропустить шаг EBPI полностью.
        fetch_text_layer_fallback: False — пропустить механическое извлечение
            текстового слоя, даже если EBPI не покрыл документ.
        fetch_ips_fallback: False — пропустить резерв ips.pravo.gov.ru, даже если
            основной поиск вернул out_of_range.
        client, ebpi_client, ips_client, cache, store, cache_path, db_path,
            out_dir, download: инфраструктурные параметры (HTTP-клиенты, пути к
            БД/файлам, кэши) — при None создаются с дефолтами; их назначение
            видно из имени и типа, здесь не повторяется.

    Returns:
        Result со статусом поиска/скачивания. При FOUND: pdf_path заполнен всегда
        (если download=True); ebpi_docx_path и text_layer_docx_path — независимо
        друг от друга, в зависимости от того, какой текстовый шаг сработал (оба
        могут остаться None одновременно — например, для скана, который EBPI не
        покрыл, тогда нужен OCR отдельно). При FOUND_TEXT_ONLY: pdf_path всегда
        None (PDF структурно не существует), текст — в ips_docx_path.
    """
    owns_client = client is None
    owns_ebpi_client = ebpi_client is None
    owns_ips_client = ips_client is None
    path = Path(db_path) if db_path is not None else Path(cache_path)
    if client is None:
        client = create_client()
    if ebpi_client is None and fetch_ebpi_text:
        ebpi_client = ebpi.create_ebpi_client()
    if ips_client is None and fetch_ips_fallback:
        ips_client = ips_pravo.create_ips_client()
    if cache is None:
        cache = AuthorityCache(path)
    if store is None:
        store = DocumentStore(path)

    try:
        result = search_document(client, query, cache)
        if result.status in (Status.FOUND, Status.AMBIGUOUS):
            result = _persist_search_hits(client, store, result)
        elif result.status == Status.OUT_OF_RANGE and fetch_ips_fallback and ips_client is not None:
            result = _try_ips_fallback(ips_client, result, query=query, out_dir=Path(out_dir), store=store)

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
            if fetch_text_layer_fallback:
                result = _attach_text_layer_docx(result)
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
        if owns_ips_client and ips_client is not None:
            ips_client.close()
