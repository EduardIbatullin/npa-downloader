"""Клиент к поисковому API ips.pravo.gov.ru — резерв для документов старше границы
охвата publication.pravo.gov.ru (models.COVERAGE_START), см.
docs/ips-pravo-coverage.md.

Недокументированный React-эндпоинт (разобран из main.js минифицированного бандла
сайта), без гарантий стабильности — любой сбой сети или неожиданный формат ответа
трактуется одинаково, как «документа здесь нет»; вызывающий код (service.py) сам
решает, как на это реагировать.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from npa_pipeline.http_client import NetworkError, create_client, request_json

BASE_URL = "http://ips.pravo.gov.ru"
BASEID = "c000000000"  # единственная известная база («Федеральное законодательство»)

_SEARCH_PATH = "/api/ips/legislation/search.json"
_CARD_PATH = "/api/ips/legislation/document_card.json"
_TEXT_PATH = "/api/ips/legislation/documenttext"

_APPLIED_STATUS = "актуальная"
_FUTURE_STATUS = "не вступившая в силу"


def create_ips_client(*, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Клиент к ips.pravo.gov.ru — тот же create_client, другой base_url."""
    return create_client(base_url=BASE_URL, transport=transport)


@dataclass
class IpsHit:
    """Один кандидат из search.json. Реквизиты — одной строкой (adoption), не
    разбиты на поля, в отличие от DocItem с publication.pravo.gov.ru."""

    nd: str
    adoption: str
    docdate: str  # DD.MM.YYYY
    state: str | None
    publications: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nd": self.nd,
            "adoption": self.adoption,
            "docdate": self.docdate,
            "state": self.state,
            "publications": list(self.publications),
        }


@dataclass
class IpsRedaction:
    """Одна запись из document_card.json -> redactions[]."""

    rdk: int  # исходное поле "id" — ключ редакции для documenttext
    status: str | None
    date: str | None  # DD.MM.YYYY, дата начала действия этой редакции
    ref_nd: str | None  # nd акта-поправки, которым введена редакция (см. refnd)


def _date_param(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def search_by_requisites(client: httpx.Client, *, number: str, doc_date: date) -> list[IpsHit]:
    """Ищет документ по точному номеру и дате подписания, без eoNumber.

    Не фильтрует по органу (параметр `organ`) — значение, под которым орган
    хранится в этом API, не выяснено до конца для федеральных законов (см.
    docs/ips-pravo-coverage.md, «Не доведено до конца: фильтр organ»); номер+дата
    уже дают однозначный результат в проверенных случаях.

    Args:
        number: номер документа, как в реквизитах (например «7-ФЗ»).
        doc_date: дата подписания.

    Returns:
        Список найденных документов — 0, 1 или несколько; вызывающий код решает,
        как трактовать каждый случай (см. service._try_ips_fallback). Пустой
        список также при сбое сети или неожиданном формате ответа — не
        различается.
    """
    param = _date_param(doc_date)
    body = {
        "searchType": "attrSearch",
        "number_eq": number,
        "date": f"{param},{param}",
        "bpas": BASEID,
        "page": 1,
    }
    try:
        data = request_json(client, "POST", _SEARCH_PATH, data=body)
    except NetworkError:
        return []
    if not isinstance(data, dict):
        return []
    docs = data.get("docs")
    if not isinstance(docs, list):
        return []

    hits: list[IpsHit] = []
    for doc in docs:
        if not isinstance(doc, dict) or not doc.get("nd"):
            continue
        hits.append(
            IpsHit(
                nd=str(doc["nd"]),
                adoption=doc.get("adoption") or "",
                docdate=doc.get("docdate") or param,
                state=doc.get("state"),
                publications=[p for p in (doc.get("publications") or []) if isinstance(p, str)],
            )
        )
    return hits


def fetch_redactions(client: httpx.Client, *, nd: str) -> list[IpsRedaction]:
    """Список редакций документа из document_card.json.

    Returns:
        Список редакций; пустой — документ не найден по этому nd либо сбой сети
        (не различается).
    """
    try:
        data = request_json(client, "GET", _CARD_PATH, params={"nd": nd, "bpa": BASEID})
    except NetworkError:
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("redactions")
    if not isinstance(raw, list):
        return []

    redactions: list[IpsRedaction] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        redactions.append(
            IpsRedaction(
                rdk=int(item["id"]),
                status=item.get("status"),
                date=item.get("date"),
                ref_nd=item.get("refnd"),
            )
        )
    return redactions


def select_latest_redaction(redactions: list[IpsRedaction]) -> IpsRedaction | None:
    """Возвращает действующую на сегодня редакцию документа.

    Выбирает по полю status, не по actual — actual на этом API системно не
    проверялся на нескольких документах (в отличие от EBPI, где аналогичное поле
    оказалось ловушкой, см. npa_pipeline.ebpi.select_latest_redaction), поэтому не
    считается надёжным без дополнительной проверки (см.
    docs/ips-pravo-coverage.md, «Открытые вопросы»).

    Returns:
        Редакция со status == "актуальная" (обычно ровно одна; при нескольких —
        с максимальным rdk). Если такой нет — редакция с максимальным rdk среди
        тех, что не помечены «не вступившая в силу». None — список пуст или все
        редакции в нём — ещё не вступившие в силу.
    """
    if not redactions:
        return None
    applied = [r for r in redactions if r.status == _APPLIED_STATUS]
    if applied:
        return max(applied, key=lambda r: r.rdk)
    candidates = [r for r in redactions if r.status != _FUTURE_STATUS]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.rdk)


def fetch_text(client: httpx.Client, *, nd: str, rdk: int) -> str | None:
    """Полный HTML-текст редакции (тот же стиль Word-экспорта, что у EBPI redtext,
    см. npa_pipeline.ebpi_docx.convert_redtext_to_docx — предположительно
    применим и здесь, не проверено на реальном прогоне).

    `completed=true` обязателен — без него эндпоинт отдаёт 200 с пустой строкой
    doctext (похоже на пустой ответ по ошибке, а не на отказ — это не «текста
    нет», см. docs/ips-pravo-coverage.md).

    Returns:
        HTML-текст либо None — документ/редакция не найдены, сбой сети, или
        ответ пуст (не различается).
    """
    try:
        data = request_json(
            client,
            "GET",
            _TEXT_PATH,
            params={"nd": nd, "bpa": BASEID, "rdk": rdk, "completed": "true"},
        )
    except NetworkError:
        return None
    if not isinstance(data, dict):
        return None
    html = data.get("doctext")
    return html if isinstance(html, str) and html.strip() else None
