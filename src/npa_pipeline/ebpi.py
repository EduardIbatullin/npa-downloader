"""Клиент к эталонному банку правовой информации (actual.pravo.gov.ru, порт 8000) —
даёт выверенный HTML-текст документа (включая таблицы) независимо от скана PDF.

Покрытие проверено эмпирически на 23 документах (2026-10-12, добор 2026-10-13):
законы, указы, постановления/распоряжения Правительства, акты Конституционного и
Верховного Суда, акты Банка России — есть; ведомственные приказы (министерств,
служб, агентств, в т.ч. федеральных) — не найдены ни разу. Проверять каждый
документ через fetch_ebpi_document() и не полагаться на вид органа — граница не по
уровню (федеральный/региональный), а по виду акта. Есть и задержка индексации у
самых свежих документов (день-два после публикации) — см. docs/ebpi-coverage.md,
«Задержка индексации».

API нигде не документирован официально — найден разбором минифицированного JS
страницы actual.pravo.gov.ru/content/content.html (обработчик #pnum= в хэше). Любой
сбой сети, таймаут или неожиданный формат ответа трактуется как «документа здесь
нет», не как ошибка — вызывающий код должен сам решать, переходить ли к OCR.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from npa_pipeline.http_client import NetworkError, create_client, request_json

EBPI_BASE_URL = "http://actual.pravo.gov.ru:8000"
_BPA = "ebpi"  # единственное известное значение этого параметра на сайте

# card() по docid (не по pnum/eoNumber) отдаёт docimagepath вида
# "http://publication.pravo.gov.ru/Document/View/0001202609230028" — оттуда eoNumber
# акта. Найдено и проверено 2026-10-12 на реальном документе (см. docs/ebpi-coverage.md).
_DOCIMAGEPATH_EO_RE = re.compile(r"/Document/View/(\d+)")

# Сколько дней держать отрицательный результат проверки EBPI (covered=False) без
# повторного обращения в сеть. Калибровано по наблюдённому лагу индексации EBPI у
# свежих документов (до ~2 недель на одном случае, см. docs/ebpi-coverage.md,
# «Задержка индексации») с запасом — решение 2026-10-13. После истечения окна
# service._attach_ebpi_text пробует сеть ещё раз; если документ снова не найден,
# окно продлевается от нового checked_at, а не снимается навсегда.
NEGATIVE_CACHE_TTL_DAYS = 21


def is_negative_cache_expired(checked_at: str) -> bool:
    """True, если отрицательный результат проверки EBPI старше NEGATIVE_CACHE_TTL_DAYS
    — пора проверить сетью ещё раз, а не бесконечно доверять старому «не покрыт»
    (который мог быть вызван задержкой индексации, а не видом акта)."""
    checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - checked > timedelta(days=NEGATIVE_CACHE_TTL_DAYS)


def create_ebpi_client(*, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Клиент к EBPI — тот же create_client, другой base_url."""
    return create_client(base_url=EBPI_BASE_URL, transport=transport)


@dataclass
class AmendingAct:
    """Акт, которым внесена действующая редакция (см. EbpiDocument.amending_acts) —
    резолвится через card() по docid (reddocrefid), не по eoNumber, см.
    resolve_amending_acts и docs/ebpi-coverage.md."""

    eo_number: str
    number: str | None  # card["adoptions"][0]["onumber"]
    date: str | None  # card["adoptions"][0]["odate"], формат DD.MM.YYYY
    name: str | None  # card["docname"]
    docpassing: str | None  # card["docpassing"] — человекочитаемая строка реквизитов целиком
    effective_date: str | None  # reddate редакции, которую этот акт ввёл (YYYYMMDD)


@dataclass
class EbpiDocument:
    """Текст документа из EBPI — самая свежая (действующая) редакция, см.
    select_latest_redaction. Для изменённого акта это текст с учётом поправок, он
    может не совпадать с тем, что было в исходно опубликованном PDF (решение
    2026-10-12 — нужна действующая версия, не архивная; см. select_initial_redaction,
    если понадобится вернуться к исходной публикации). is_initial=False сигнализирует
    именно это расхождение — вызывающий код (service.py) помечает имя файла."""

    pnum: str
    docid: int
    docstate: str | None
    redid: int
    red_date: str | None  # YYYYMMDD, как в ответе API
    is_initial: bool  # False — показана не исходная публикация, а поздняя редакция
    html: str  # redtext — HTML в стиле Word-экспорта (xmlns:w=...)
    amending_acts: list[AmendingAct] = field(default_factory=list)


def format_red_date(red_date: str) -> str:
    """YYYYMMDD -> DD.MM.YYYY — для отображения и имени файла."""
    return f"{red_date[6:8]}.{red_date[4:6]}.{red_date[:4]}"


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


def fetch_card_by_docid(client: httpx.Client, docid: int) -> dict | None:
    """Карточка документа по внутреннему docid EBPI (не eoNumber/pnum) — нужна,
    чтобы резолвить reddocrefid редакции в сам акт-поправку, см. resolve_amending_acts.
    Тот же эндпоинт, что fetch_card(), другая форма параметра `t`."""
    data = _request(client, "/api/ebpi/card/", {"bpa": _BPA, "t": json.dumps({"docid": docid})})
    if data is None or not data.get("docid") or data.get("error"):
        return None
    return data


def extract_eo_number_from_docimagepath(docimagepath: str | None) -> str | None:
    """eoNumber из docimagepath вида .../Document/View/<eoNumber> в ответе card()."""
    if not docimagepath:
        return None
    m = _DOCIMAGEPATH_EO_RE.search(docimagepath)
    return m.group(1) if m else None


def resolve_amending_acts(client: httpx.Client, redactions: list[dict]) -> list[AmendingAct]:
    """Акты-поправки для всех редакций с ненулевым reddocrefid (обычно один, у
    документа с несколькими раундами изменений — несколько, по одному на каждый).
    reddocrefid=0 — у исходной редакции, резолвить нечего. Любой сбой на отдельном
    акте (сеть, нет docimagepath) просто пропускает этот акт, не рвёт весь список —
    частичные данные лучше, чем ничего."""
    seen: set[int] = set()
    acts: list[AmendingAct] = []
    for red in redactions:
        docid = red.get("reddocrefid")
        if not docid or docid in seen:
            continue
        seen.add(docid)
        card = fetch_card_by_docid(client, docid)
        if card is None:
            continue
        eo_number = extract_eo_number_from_docimagepath(card.get("docimagepath"))
        if eo_number is None:
            continue
        adoptions = card.get("adoptions") or []
        first = adoptions[0] if adoptions else {}
        acts.append(
            AmendingAct(
                eo_number=eo_number,
                number=first.get("onumber"),
                date=first.get("odate"),
                name=card.get("docname"),
                docpassing=card.get("docpassing"),
                effective_date=red.get("reddate"),
            )
        )
    return acts


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
    модуля) — действующая редакция (см. select_latest_redaction), плюс список
    актов-поправок, если редакция не исходная (см. resolve_amending_acts). None —
    документ не найден, не покрыт, или любой сбой сети на любом шаге получения
    самого текста (сбой резолвинга актов-поправок текст не блокирует — список
    просто останется короче)."""
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
        is_initial=bool(chosen.get("redinitial")),
        html=html,
        amending_acts=resolve_amending_acts(client, redactions),
    )
