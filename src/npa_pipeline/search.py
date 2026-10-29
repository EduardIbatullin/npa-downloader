"""Поиск документа на publication.pravo.gov.ru по реквизитам."""

from __future__ import annotations

from datetime import date

import httpx

from npa_pipeline import api as pravo_api
from npa_pipeline.authorities import AuthorityCache
from npa_pipeline.models import (
    COVERAGE_START,
    DiagnosticHit,
    DocItem,
    MatchType,
    Query,
    Result,
    Status,
)
from npa_pipeline.normalize import (
    digit_prefix,
    matches_digit_boundary,
    normalize_number,
    numbers_equal,
)


def validate_query(query: Query) -> str | None:
    """Возвращает текст ошибки или None, если вход корректен."""
    if query.eo_number:
        return None
    if not query.number or not str(query.number).strip():
        return "Не указан номер документа"
    if query.date is None:
        return "Не указана дата подписания"
    if not query.authority_guid and not query.authority_name:
        return "Не указан орган (GUID или название)"
    return None


def apply_filters(
    items: list[DocItem],
    *,
    target_date: date,
    candidates: list[str],
    filter_by_authority: bool,
    number_check: str | None = None,
    digit_boundary_prefix: str | None = None,
    doc_type_id: str | None = None,
) -> list[DocItem]:
    """Фильтрует кандидатов по дате, номеру, органу, типу; дедуплицирует по eoNumber.

    Args:
        items: исходный список документов (обычно сырой ответ поиска).
        target_date: документы с другой датой подписания отсекаются.
        candidates: GUID-ы органов-кандидатов — используется и для фильтра (если
            filter_by_authority), и как множество для проверки пересечения с
            item.signatory_ids.
        filter_by_authority: True — оставить только документы, у которых
            signatory_ids пересекаются с candidates; False — пропустить этот
            фильтр (например, когда орган уже учтён на уровне запроса к API).
        number_check: если задан — оставить только документы с этим номером
            (сравнение через normalize.numbers_equal, не точное совпадение строк).
        digit_boundary_prefix: если задан — оставить только документы, чей номер
            начинается с этого цифрового префикса на границе разряда (запасной
            поиск по цифровой части номера, см. normalize.matches_digit_boundary).
        doc_type_id: если задан и у документа непустой document_type_id — должен
            совпадать.

    Returns:
        Отфильтрованный список без дублей eoNumber, в исходном порядке.
    """
    result: list[DocItem] = []
    seen: set[str] = set()
    candidate_set = set(candidates)

    for item in items:
        if item.document_date != target_date:
            continue
        if number_check is not None and not numbers_equal(item.number, number_check):
            continue
        if digit_boundary_prefix is not None and not matches_digit_boundary(
            item.number, digit_boundary_prefix
        ):
            continue
        if filter_by_authority and candidates:
            if not set(item.signatory_ids) & candidate_set:
                continue
        if doc_type_id and item.document_type_id and item.document_type_id != doc_type_id:
            continue
        if item.eo_number in seen:
            continue
        seen.add(item.eo_number)
        result.append(item)
    return result


def decide(
    matched: list[DocItem],
    *,
    match_type: MatchType,
    query_date: date,
    diagnostics: list[DiagnosticHit] | None = None,
) -> Result:
    """Выбирает статус результата по числу совпадений.

    Returns:
        Result: FOUND при ровно одном совпадении; AMBIGUOUS при нескольких
        (все — в candidates); при нуле совпадений — OUT_OF_RANGE, если
        query_date раньше охвата портала (COVERAGE_START), иначе NOT_FOUND
        (с diagnostics, если переданы).
    """
    if len(matched) == 1:
        return Result(
            status=Status.FOUND,
            match_type=match_type,
            document=matched[0],
            eo_number=matched[0].eo_number,
        )
    if len(matched) > 1:
        return Result(
            status=Status.AMBIGUOUS,
            match_type=match_type,
            candidates=matched,
            message=f"Найдено {len(matched)} документов",
        )
    if query_date < COVERAGE_START:
        return Result(
            status=Status.OUT_OF_RANGE,
            message=(
                f"Дата подписания {query_date.isoformat()} раньше границы охвата "
                f"{COVERAGE_START.isoformat()}"
            ),
            diagnostics=diagnostics or [],
        )
    return Result(
        status=Status.NOT_FOUND,
        diagnostics=diagnostics or [],
        message="Документ не найден среди кандидатов органа",
    )


def _diag_from_items(items: list[DocItem], candidates: list[str]) -> list[DiagnosticHit]:
    """Находки для диагностики: документы органов вне списка кандидатов, без повторов."""
    candidate_set = set(candidates)
    hits: list[DiagnosticHit] = []
    seen: set[str] = set()
    for item in items:
        if item.eo_number in seen:
            continue
        if candidate_set and set(item.signatory_ids) & candidate_set:
            continue
        seen.add(item.eo_number)
        hits.append(
            DiagnosticHit(
                eo_number=item.eo_number,
                authority_id=item.signatory_ids[0] if item.signatory_ids else None,
                number=item.number,
                document_date=item.document_date,
            )
        )
    return hits


def _filter_by_date_and_number(
    items: list[DocItem],
    *,
    target_date: date,
    number: str,
    prefix: str,
) -> list[DocItem]:
    """Для диагностики: дата + точный номер или граница цифровой части."""
    out: list[DocItem] = []
    seen: set[str] = set()
    for item in items:
        if item.document_date != target_date:
            continue
        ok = numbers_equal(item.number, number) or (
            bool(prefix) and matches_digit_boundary(item.number, prefix)
        )
        if not ok or item.eo_number in seen:
            continue
        seen.add(item.eo_number)
        out.append(item)
    return out


def search_document(
    client: httpx.Client,
    query: Query,
    cache: AuthorityCache,
) -> Result:
    """Выполняет полный алгоритм поиска документа, без скачивания PDF.

    Сначала ищется точный номер, затем, если совпадений нет, — номер по
    цифровой части (при нескольких органах-кандидатах — только для префикса от
    трёх цифр, иначе выборка слишком широка). Если название органа
    соответствует нескольким GUID, отбор по подписанту выполняется после
    загрузки карточек (:func:`npa_pipeline.api.enrich_signatories`), а не в
    запросе к API. Если документ не найден, собирается диагностика: документы
    с той же датой и номером у других органов. Сбой при сборе диагностики
    оставляет её пустой и не меняет статус.

    Args:
        query: должен содержать либо eo_number, либо number+date+(authority_guid
            или authority_name) — иначе возвращается INVALID_INPUT, см.
            validate_query.
        cache: резолвер органа в один или несколько GUID (см. AuthorityCache).

    Returns:
        Result. FOUND/AMBIGUOUS/NOT_FOUND/OUT_OF_RANGE — см. decide(); INVALID_INPUT
        при невалидном query. Сетевые ошибки от client не перехватываются здесь —
        это ответственность вызывающего кода (service.fetch_document).
    """
    err = validate_query(query)
    if err:
        return Result(status=Status.INVALID_INPUT, message=err)

    # известен eoNumber
    if query.eo_number:
        doc = pravo_api.get_document(client, query.eo_number)
        return Result(
            status=Status.FOUND,
            match_type=MatchType.EXACT,
            document=doc,
            eo_number=doc.eo_number,
        )

    assert query.number is not None and query.date is not None

    # органы-кандидаты
    candidates = cache.resolve(
        authority_guid=query.authority_guid,
        authority_name=query.authority_name,
    )
    number_for_request = normalize_number(query.number)
    prefix = digit_prefix(query.number)
    single = len(candidates) == 1

    # точный номер
    raw_exact = pravo_api.search_documents(
        client,
        number=number_for_request,
        search_type=0,
        authority_id=candidates[0] if single else None,
    )

    if single:
        # сервер уже отобрал документы органа
        matched = apply_filters(
            raw_exact,
            target_date=query.date,
            candidates=candidates,
            filter_by_authority=False,
            number_check=query.number,
            doc_type_id=query.doc_type_id,
        )
    else:
        date_only = apply_filters(
            raw_exact,
            target_date=query.date,
            candidates=candidates,
            filter_by_authority=False,
            number_check=query.number,
        )
        if date_only and candidates:
            date_only = pravo_api.enrich_signatories(client, date_only)
        matched = apply_filters(
            date_only,
            target_date=query.date,
            candidates=candidates,
            filter_by_authority=bool(candidates),
            number_check=query.number,
            doc_type_id=query.doc_type_id,
        )

    if matched:
        return decide(matched, match_type=MatchType.EXACT, query_date=query.date)

    # запасной поиск по цифровой части номера
    fallback_raw: list[DocItem] = []
    fallback_ran = False
    if prefix and (single or len(prefix) >= 3):
        fallback_raw = pravo_api.search_documents(
            client,
            number=prefix,
            search_type=1,
            authority_id=candidates[0] if single else None,
        )
        fallback_ran = True

        if single:
            matched = apply_filters(
                fallback_raw,
                target_date=query.date,
                candidates=candidates,
                filter_by_authority=False,
                digit_boundary_prefix=prefix,
                doc_type_id=query.doc_type_id,
            )
        else:
            date_fb = apply_filters(
                fallback_raw,
                target_date=query.date,
                candidates=candidates,
                filter_by_authority=False,
                digit_boundary_prefix=prefix,
            )
            if date_fb and candidates:
                date_fb = pravo_api.enrich_signatories(client, date_fb)
            matched = apply_filters(
                date_fb,
                target_date=query.date,
                candidates=candidates,
                filter_by_authority=bool(candidates),
                digit_boundary_prefix=prefix,
                doc_type_id=query.doc_type_id,
            )

        if matched:
            return decide(matched, match_type=MatchType.DIGITS_ONLY, query_date=query.date)

    # диагностика; сбой сети здесь не должен менять not_found/out_of_range
    diagnostics: list[DiagnosticHit] = []
    if query.authority_name or query.authority_guid:
        try:
            if not single:
                source = fallback_raw if fallback_ran else raw_exact
                by_date = _filter_by_date_and_number(
                    source, target_date=query.date, number=query.number, prefix=prefix
                )
                if by_date:
                    try:
                        by_date = pravo_api.enrich_signatories(client, by_date)
                    except Exception:
                        pass
                diagnostics = _diag_from_items(by_date, candidates)
            else:
                diag_raw = pravo_api.search_documents(
                    client,
                    number=number_for_request,
                    search_type=0,
                    authority_id=None,
                )
                by_date = apply_filters(
                    diag_raw,
                    target_date=query.date,
                    candidates=[],
                    filter_by_authority=False,
                    number_check=query.number,
                )
                diagnostics = _diag_from_items(by_date, candidates)
        except Exception:
            diagnostics = []

    return decide(
        [],
        match_type=MatchType.EXACT,
        query_date=query.date,
        diagnostics=diagnostics,
    )
