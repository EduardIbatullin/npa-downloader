"""Клиент burondt.ru: карточки справочников ИТС НДТ и скачивание их файлов.

Справочники ИТС не публикуются на publication.pravo.gov.ru. На burondt.ru нет
поиска по реквизитам: карточки лежат по последовательным числовым ``UrlId`` в
диапазоне, общем с ГОСТами и документами технических рабочих групп. Поэтому
справочники находятся обходом всего диапазона с отбором по полю «Обозначение»
(:func:`is_its`). Диапазон пополняется, и его верхняя граница каждый раз
определяется заново по длинной серии пустых карточек (:func:`find_upper_bound`).

Модуль не обращается к БД.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx

from npa_pipeline.http_client import (
    BURONDT_PAGE_PAUSE_SECONDS,
    create_client,
    download_bytes,
    request_text,
)

BURONDT_BASE_URL = "https://burondt.ru"
CARD_PATH = "/NDT/NDTDocsDetail.php"
FILE_PATH = "/NDT/NDTDocsFileDownload.php"
ITS_PREFIX = "ИТС"

_DESIGNATION_RE = re.compile(
    r"<label>\s*Обозначение\s*</label>\s*</td>\s*<td>([^<]*)</td>", re.IGNORECASE
)
_TITLE_RE = re.compile(
    r"<label>\s*Наименование\s*</label>\s*</td>\s*<td>([^<]*)</td>", re.IGNORECASE
)
_FILES_ROW_RE = re.compile(
    r"<label>\s*Файлы:?\s*</label>\s*</td>\s*<td>(.*?)</td>\s*</tr>",
    re.IGNORECASE | re.DOTALL,
)
_FILE_ITEM_RE = re.compile(
    r"<li>(.*?)<a[^>]*href=\"[^\"]*UrlId=(\d+)\"[^>]*>([^<]*)</a>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_ORDER_WORD_RE = re.compile(r"приказ", re.IGNORECASE)
_CANCEL_WORD_RE = re.compile(r"отмен", re.IGNORECASE)
_ORDER_NUMBER_RE = re.compile(r"№\s*(\d+)")
_ORDER_DATE_RE = re.compile(
    r"(\d{1,2}\s+[а-яА-Я]+\s+\d{4}|\d{2}\.\d{2}\.\d{4})"
)
_CONTENT_DISPOSITION_FILENAME_RE = re.compile(r'filename="?([^";]+)"?')


@dataclass
class ItsCardFile:
    """Один файл карточки burondt.ru (справочник, приказ или отмена)."""

    file_id: int
    caption: str
    role: str  # "document" | "order" | "unknown"


@dataclass
class ItsCard:
    """Карточка ИТС/НДТ (или ГОСТа/протокола — отфильтровывается is_its)."""

    url_id: int
    designation: str
    title: str | None = None
    files: list[ItsCardFile] = field(default_factory=list)


def create_burondt_client(*, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Создаёт HTTP-клиент burondt.ru."""
    return create_client(base_url=BURONDT_BASE_URL, transport=transport)


def fetch_card_html(client: httpx.Client, url_id: int) -> str:
    """Загружает HTML карточки ``NDTDocsDetail.php?UrlId=<url_id>``."""
    return request_text(client, "GET", CARD_PATH, params={"UrlId": url_id})


def _strip_tags(text: str) -> str:
    """Текст без HTML-тегов и ``&nbsp;``."""
    return _TAG_RE.sub("", text).replace("&nbsp;", " ").strip()


def _classify_role(caption: str) -> str:
    """Роль файла по подписи карточки: order / cancellation / document."""
    if _CANCEL_WORD_RE.search(caption):
        return "cancellation"
    if _ORDER_WORD_RE.search(caption):
        return "order"
    return "document"


def extract_order_meta(caption: str) -> tuple[str | None, str | None]:
    """Извлекает номер и дату приказа из подписи файла, если они есть.

    Подпись карточки не авторитетна: номер и дата в ней могут не совпадать с
    самим приказом. Результат годится только для показа, а не как проверенные
    реквизиты.

    Returns:
        (номер, дата) — любой из них None, если не нашлось в тексте подписи.
    """
    number_m = _ORDER_NUMBER_RE.search(caption)
    date_m = _ORDER_DATE_RE.search(caption)
    return (
        number_m.group(1) if number_m else None,
        date_m.group(1) if date_m else None,
    )


def parse_card(html: str, url_id: int) -> ItsCard | None:
    """Разбирает HTML карточки NDTDocsDetail.php в ItsCard.

    Returns:
        Карточка или None для пустого ``UrlId`` (пропуски в диапазоне обычны).
    """
    designation_m = _DESIGNATION_RE.search(html)
    designation = designation_m.group(1).strip() if designation_m else ""
    if not designation:
        return None

    title_m = _TITLE_RE.search(html)
    title = title_m.group(1).strip() if title_m and title_m.group(1).strip() else None

    files: list[ItsCardFile] = []
    files_m = _FILES_ROW_RE.search(html)
    if files_m:
        for item_m in _FILE_ITEM_RE.finditer(files_m.group(1)):
            caption_prefix = _strip_tags(item_m.group(1))
            filename = item_m.group(3).strip()
            caption = caption_prefix or filename
            files.append(
                ItsCardFile(
                    file_id=int(item_m.group(2)),
                    caption=caption,
                    role=_classify_role(caption),
                )
            )

    return ItsCard(url_id=url_id, designation=designation, title=title, files=files)


def is_its(card: ItsCard) -> bool:
    """Проверяет, что карточка — справочник ИТС, а не ГОСТ или протокол."""
    return card.designation.strip().upper().startswith(ITS_PREFIX)


def resolve_ambiguous_roles(
    files_with_bytes: list[tuple[ItsCardFile, bytes]]
) -> list[ItsCardFile]:
    """Определяет роли файлов по размеру, когда подпись карточки их не определила.

    Args:
        files_with_bytes: файлы карточки вместе с их содержимым — размер в байтах
            и есть сигнал для разделения: крупнейший файл считается справочником.

    Returns:
        Файлы с пересчитанной ролью: крупнейший — "document", остальные —
        "order". Если файлов меньше двух, роль не трогается (бессмысленно считать
        «крупнейший» среди одного).
    """
    if len(files_with_bytes) < 2:
        return [f for f, _ in files_with_bytes]
    ordered = sorted(files_with_bytes, key=lambda pair: len(pair[1]), reverse=True)
    result: list[ItsCardFile] = []
    for i, (f, _data) in enumerate(ordered):
        role = "document" if i == 0 else "order"
        result.append(ItsCardFile(file_id=f.file_id, caption=f.caption, role=role))
    return result


def download_file(client: httpx.Client, file_id: int) -> tuple[bytes, int | None, str | None]:
    """Скачивает файл карточки: GET NDTDocsFileDownload.php?UrlId=<file_id>.

    Returns:
        (содержимое файла, Content-Length из заголовков или None, имя файла из
        Content-Disposition или None, если заголовка не было или не распарсился).
    """
    data, content_length, headers = download_bytes(client, FILE_PATH, params={"UrlId": file_id})
    filename = None
    disposition = headers.get("Content-Disposition") if headers else None
    if disposition:
        m = _CONTENT_DISPOSITION_FILENAME_RE.search(disposition)
        if m:
            filename = m.group(1).strip()
    return data, content_length, filename


def find_upper_bound(
    client: httpx.Client,
    *,
    from_url_id: int,
    confirm_empty_run: int = 40,
    max_probe: int = 20_000,
    pause_seconds: float = BURONDT_PAGE_PAUSE_SECONDS,
) -> int:
    """Находит верхнюю границу диапазона UrlId без хардкода константы.

    Идёт вперёд от from_url_id, пока не встретит confirm_empty_run пустых карточек
    подряд: такая серия отличает конец диапазона от единичных пропусков внутри
    него.

    Args:
        from_url_id: с какого UrlId начинать обход вперёд.
        confirm_empty_run: сколько пустых подряд считать концом диапазона.
        max_probe: защита от бесконечного обхода, если диапазон никогда не кончится.
        pause_seconds: пауза между запросами (вежливость к серверу).

    Returns:
        Последний непустой url_id, который встретили (не первый пустой после него).
    """
    empty_run = 0
    last_non_empty = from_url_id - 1
    url_id = from_url_id
    while empty_run < confirm_empty_run and url_id < from_url_id + max_probe:
        html = fetch_card_html(client, url_id)
        if pause_seconds:
            time.sleep(pause_seconds)
        card = parse_card(html, url_id)
        if card is None:
            empty_run += 1
        else:
            empty_run = 0
            last_non_empty = url_id
        url_id += 1
    return last_non_empty


def iter_cards(
    client: httpx.Client,
    *,
    lo: int,
    hi: int,
    pause_seconds: float = BURONDT_PAGE_PAUSE_SECONDS,
) -> Iterator[tuple[int, ItsCard | None]]:
    """Построчно обходит диапазон [lo, hi] включительно, с паузой между запросами.

    Единичные пропуски встречаются внутри диапазона — обход построчный, не с шагом.

    Yields:
        (url_id, карточка или None, если слот пустой) для каждого url_id в диапазоне.
    """
    for url_id in range(lo, hi + 1):
        html = fetch_card_html(client, url_id)
        if pause_seconds:
            time.sleep(pause_seconds)
        yield url_id, parse_card(html, url_id)
