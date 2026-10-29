"""Клиент API publication.pravo.gov.ru (см. docs/api-reference.md)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from npa_pipeline.http_client import PAGE_PAUSE_SECONDS, download_bytes, request_json
from npa_pipeline.models import DocItem, parse_api_date

PAGE_SIZE = 200


def _doc_from_list_item(item: dict[str, Any]) -> DocItem:
    """Собирает документ из элемента списка ``/api/Documents``.

    Raises:
        ValueError: В элементе нет ``documentDate``.
    """
    doc_date = parse_api_date(item.get("documentDate"))
    if doc_date is None:
        raise ValueError(f"Нет documentDate в элементе {item.get('eoNumber')}")
    sid = item.get("signatoryAuthorityId")
    return DocItem(
        eo_number=item["eoNumber"],
        number=item.get("number") or "",
        document_date=doc_date,
        signatory_ids=[sid] if sid else [],
        document_type_id=item.get("documentTypeId"),
        name=item.get("name"),
        complex_name=item.get("complexName"),
        pdf_file_length=item.get("pdfFileLength"),
    )


def _doc_from_card(card: dict[str, Any]) -> DocItem:
    """Собирает документ из карточки ``/api/Document`` со всеми подписантами.

    Raises:
        ValueError: В карточке нет ``documentDate``.
    """
    doc_date = parse_api_date(card.get("documentDate"))
    if doc_date is None:
        raise ValueError(f"Нет documentDate в карточке {card.get('eoNumber')}")
    authorities = card.get("signatoryAuthorities") or []
    ids = [a["id"] for a in authorities if a.get("id")]
    if not ids:
        sid = card.get("signatoryAuthorityId")
        if sid:
            ids = [sid]
    return DocItem(
        eo_number=card["eoNumber"],
        number=card.get("number") or "",
        document_date=doc_date,
        signatory_ids=ids,
        document_type_id=card.get("documentTypeId"),
        name=card.get("name"),
        complex_name=card.get("complexName"),
        pdf_file_length=card.get("pdfFileLength"),
    )


# Публичный алиас для сборки DocItem из уже загруженной карточки
doc_item_from_card = _doc_from_card


def search_documents(
    client: httpx.Client,
    *,
    number: str,
    search_type: int = 0,
    authority_id: str | None = None,
    page_size: int = PAGE_SIZE,
) -> list[DocItem]:
    """Ищет документы по номеру, проходя по всем страницам ответа (Index с 1).

    Args:
        number: номер документа для поиска.
        search_type: режим сравнения номера (NumberSearchType) — 0 точное
            совпадение, 1 начинается с, 2 заканчивается на, 3 содержит (см.
            docs/api-reference.md).
        authority_id: GUID органа-подписанта. None — без фильтра: сервер ищет
            по документам всех органов.
        page_size: размер страницы ответа.

    Returns:
        Все найденные документы со всех страниц — не только первая (при
        pagesTotalCount > 1 часть результатов иначе терялась бы молча).
    """
    params: dict[str, Any] = {
        "Number": number,
        "NumberSearchType": search_type,
        "PageSize": page_size,
        "Index": 1,
    }
    if authority_id:
        params["SignatoryAuthorityId"] = authority_id

    first = request_json(client, "GET", "/api/Documents", params=params)
    items = list(first.get("items") or [])
    pages_total = int(first.get("pagesTotalCount") or 1)

    for page in range(2, pages_total + 1):
        time.sleep(PAGE_PAUSE_SECONDS)
        params["Index"] = page
        page_data = request_json(client, "GET", "/api/Documents", params=params)
        items.extend(page_data.get("items") or [])

    return [_doc_from_list_item(item) for item in items]


def get_document_card(client: httpx.Client, eo_number: str) -> dict[str, Any]:
    """Возвращает сырую карточку /api/Document (для сохранения в БД).

    Raises:
        ValueError: ответ не похож на карточку документа (не словарь или нет
            eoNumber).
    """
    card = request_json(client, "GET", "/api/Document", params={"eoNumber": eo_number})
    if not isinstance(card, dict) or "eoNumber" not in card:
        raise ValueError(f"Некорректная карточка для {eo_number}")
    return card


def get_document(client: httpx.Client, eo_number: str) -> DocItem:
    """Карточка документа с полным списком подписантов."""
    return _doc_from_card(get_document_card(client, eo_number))


def enrich_signatories(client: httpx.Client, items: list[DocItem]) -> list[DocItem]:
    """Догружает карточки, чтобы получить полный список подписантов."""
    enriched: list[DocItem] = []
    for i, item in enumerate(items):
        if i:
            time.sleep(PAGE_PAUSE_SECONDS)
        card = get_document(client, item.eo_number)
        enriched.append(card)
    return enriched


def list_signatory_authorities(client: httpx.Client) -> list[dict[str, Any]]:
    """Возвращает полный справочник органов (~3986 записей).

    Raises:
        ValueError: ответ /api/SignatoryAuthorities пришёл не списком.
    """
    data = request_json(client, "GET", "/api/SignatoryAuthorities")
    if not isinstance(data, list):
        raise ValueError("Ожидался список от /api/SignatoryAuthorities")
    return data


def download_pdf(client: httpx.Client, eo_number: str) -> tuple[bytes, int | None]:
    """Скачивает PDF: возвращает (bytes, Content-Length или None)."""
    data, content_length, _headers = download_bytes(client, "/file/pdf", params={"eoNumber": eo_number})
    return data, content_length


def pdf_url(eo_number: str) -> str:
    """Адрес PDF документа на портале."""
    from npa_pipeline.http_client import BASE_URL

    return f"{BASE_URL}/file/pdf?eoNumber={eo_number}"
