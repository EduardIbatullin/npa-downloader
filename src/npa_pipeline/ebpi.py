"""Клиент к эталонному банку правовой информации (actual.pravo.gov.ru, порт 8000) —
даёт выверенный HTML-текст документа (включая таблицы) независимо от скана PDF.

Покрытие проверено эмпирически на 14 документах (2026-10-12): законы, указы,
постановления/распоряжения Правительства, акты высших судов и Банка России — есть;
ведомственные приказы (министерств, служб, агентств, в т.ч. федеральных) — не
найдены ни разу. Проверять каждый документ через fetch_ebpi_document() и не
полагаться на вид органа — граница не по уровню (федеральный/региональный), а по
виду акта.

API нигде не документирован официально — найден разбором минифицированного JS
страницы actual.pravo.gov.ru/content/content.html (обработчик #pnum= в хэше). Любой
сбой сети, таймаут или неожиданный формат ответа трактуется как «документа здесь
нет», не как ошибка — вызывающий код должен сам решать, переходить ли к OCR.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from npa_pipeline.http_client import NetworkError, create_client, request_json

EBPI_BASE_URL = "http://actual.pravo.gov.ru:8000"
_BPA = "ebpi"  # единственное известное значение этого параметра на сайте


def create_ebpi_client(*, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Клиент к EBPI — тот же create_client, другой base_url."""
    return create_client(base_url=EBPI_BASE_URL, transport=transport)


@dataclass
class EbpiDocument:
    """Текст документа из EBPI — самая свежая (действующая) редакция, см.
    select_latest_redaction. Для изменённого акта это текст с учётом поправок, он
    может не совпадать с тем, что было в исходно опубликованном PDF (решение
    2026-10-12 — нужна действующая версия, не архивная; см. select_initial_redaction,
    если понадобится вернуться к исходной публикации)."""

    pnum: str
    docid: int
    docstate: str | None
    redid: int
    red_date: str | None  # YYYYMMDD, как в ответе API
    html: str  # redtext — HTML в стиле Word-экспорта (xmlns:w=...)


def _request(client: httpx.Client, path: str, params: dict[str, Any]) -> dict | None:
    """JSON-запрос к EBPI; любой сбой сети или структурная ошибка в ответе -> None."""
    try:
        data = request_json(client, "GET", path, params=params)
    except NetworkError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def fetch_card(client: httpx.Client, pnum: str) -> dict | None:
    """Карточка документа: docstate, реквизиты. None — не найден в EBPI или сбой сети."""
    data = _request(client, "/api/ebpi/card/", {"bpa": _BPA, "t": json.dumps({"pnum": pnum})})
    if data is None or not data.get("docid") or data.get("error"):
        return None
    return data


def fetch_redactions(client: httpx.Client, pnum: str) -> list[dict]:
    """Список редакций документа — обычно одна, у изменявшихся актов несколько."""
    data = _request(client, "/api/ebpi/redactions/", {"bpa": _BPA, "t": json.dumps({"pnum": pnum})})
    if data is None or data.get("error"):
        return []
    redactions = data.get("redactions")
    return redactions if isinstance(redactions, list) else []


def select_initial_redaction(redactions: list[dict]) -> dict | None:
    """Редакция с флагом redinitial=true — как документ был опубликован изначально.

    Сейчас не используется в fetch_ebpi_document() (решение 2026-10-12: нужна
    действующая версия текста, не исходная публикация — см. select_latest_redaction),
    но оставлена: пригодится, если понадобится версия, совпадающая с архивным PDF.
    Поле `actual` в ответе API для этого не годится — у неизменённых документов оно
    `false` даже у единственной (и единственно верной) редакции."""
    for red in redactions:
        if red.get("redinitial"):
            return red
    return None


def select_latest_redaction(redactions: list[dict]) -> dict | None:
    """Самая свежая редакция (по дате reddate) — действующая на сегодня версия
    текста, с учётом всех внесённых поправок (решение 2026-10-12: «пока только
    самый актуальный» — не исходная публикация + отдельно акты-поправки).

    Для неизменённого документа это та же единственная редакция, что отдала бы
    select_initial_redaction. Поле `actual` в ответе API для выбора не годится —
    см. его докстринг; `reddate` (формат YYYYMMDD, сравнивается как строка)
    надёжнее и проверено на реальном изменённом документе (Постановление №710:
    редакция от 2026-09-23 выбирается верно, не исходная от 2025-09-01)."""
    if not redactions:
        return None
    return max(redactions, key=lambda r: r.get("reddate") or "")


def fetch_redtext(client: httpx.Client, redid: int) -> str | None:
    """Полный HTML-текст редакции (тот же Word-экспорт, что отдаёт кнопка «Сохранить
    в Word» на сайте — см. docs/ebpi-coverage.md). ttl=0 — режим показа, не влияет
    на содержание текста, только на подсветку элементов интерфейса."""
    data = _request(client, "/api/ebpi/redtext", {"bpa": _BPA, "t": redid, "ttl": 0})
    if data is None:
        return None
    html = data.get("redtext")
    return html if isinstance(html, str) and html.strip() else None


def fetch_ebpi_document(client: httpx.Client, pnum: str) -> EbpiDocument | None:
    """Полный текст документа из EBPI, если он там есть и покрыт (см. докстринг
    модуля) — действующая редакция (см. select_latest_redaction). None — документ
    не найден, не покрыт, или любой сбой сети на любом шаге."""
    card = fetch_card(client, pnum)
    if card is None:
        return None
    redactions = fetch_redactions(client, pnum)
    chosen = select_latest_redaction(redactions)
    if chosen is None:
        return None
    html = fetch_redtext(client, chosen["redid"])
    if html is None:
        return None
    return EbpiDocument(
        pnum=pnum,
        docid=card["docid"],
        docstate=card.get("docstate"),
        redid=chosen["redid"],
        red_date=chosen.get("reddate"),
        html=html,
    )
