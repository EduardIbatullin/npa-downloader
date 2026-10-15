"""Клиент к эталонному банку правовой информации (actual.pravo.gov.ru, порт 8000).

Даёт выверенный HTML-текст документа (включая таблицы) независимо от того, есть
ли у PDF текстовый слой. Не покрывает все виды актов и не сразу после публикации
документа — точная граница покрытия и калибровка задержки индексации — см.
docs/ebpi-coverage.md; этот модуль только делает запросы, не хранит знание о том,
что считается «покрыто», — проверять нужно через fetch_ebpi_document().

API не документировано официально и не гарантированно стабильно. Любой сбой
сети, таймаут или неожиданный формат ответа трактуется одинаково — «документа
здесь нет» — вызывающий код сам решает, переходить ли к OCR (см. service.py).
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

# Извлекает eoNumber из docimagepath вида
# "http://publication.pravo.gov.ru/Document/View/<eoNumber>" в ответе card() по
# docid (см. extract_eo_number_from_docimagepath, fetch_card_by_docid). Формат
# подтверждён на реальных данных — см. docs/ebpi-coverage.md.
_DOCIMAGEPATH_EO_RE = re.compile(r"/Document/View/(\d+)")

# Сколько дней держать отрицательный результат проверки EBPI (covered=False) без
# повторного обращения в сеть — калибровка см. docs/ebpi-coverage.md, «Задержка
# индексации». После истечения окна service._attach_ebpi_text пробует сеть ещё
# раз; если документ снова не найден, окно продлевается от нового checked_at, а
# не снимается навсегда.
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
    """Текст документа из EBPI — действующая редакция (см. select_latest_redaction),
    не обязательно совпадающая с исходно опубликованным PDF.

    Для изменённого документа это текст с учётом поправок; is_initial=False
    сигнализирует это расхождение — вызывающий код (service.py) помечает имя
    файла. Альтернатива — select_initial_redaction; обоснование выбора между
    ними — docs/ebpi-coverage.md, «Выбор редакции»."""

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
    """Возвращает карточку документа из EBPI: docstate, реквизиты.

    None — документ не найден в EBPI (не покрыт или ещё не проиндексирован, см.
    docs/ebpi-coverage.md) либо произошёл сбой сети; эти два случая не различаются.
    """
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
    """Резолвит акты-поправки для всех редакций документа с ненулевым reddocrefid.

    Args:
        redactions: список редакций (см. fetch_redactions) — проверяются все, не
            только последняя, поэтому у документа с несколькими раундами изменений
            в результате может быть несколько актов.

    Returns:
        Список найденных актов-поправок. Может быть короче, чем число редакций с
        reddocrefid: редакция с reddocrefid=0 (исходная публикация) пропускается —
        резолвить нечего, а сбой на отдельном акте (сеть, нет docimagepath в ответе)
        тихо пропускает именно этот акт, не прерывая весь список — частичные данные
        лучше, чем ничего.
    """
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
    """Возвращает редакцию с флагом redinitial=true — как документ был опубликован
    изначально.

    Не используется в fetch_ebpi_document() (см. select_latest_redaction и
    docs/ebpi-coverage.md, «Выбор редакции»), но оставлена для случаев, когда
    нужна версия, совпадающая с архивным PDF.

    Returns:
        Исходная редакция, либо None, если ни у одной записи не выставлен
        redinitial. Поле `actual` для этого выбора не годится — у неизменённых
        документов оно `false` даже у единственной (и единственно верной)
        редакции.
    """
    for red in redactions:
        if red.get("redinitial"):
            return red
    return None


def select_latest_redaction(
    redactions: list[dict],
    *,
    as_of: str | None = None,
) -> dict | None:
    """Возвращает самую свежую уже вступившую в силу редакцию (по reddate).

    Используется в fetch_ebpi_document() — обоснование выбора между этой функцией
    и select_initial_redaction см. docs/ebpi-coverage.md, «Выбор редакции». Для
    неизменённого документа это та же единственная редакция, что отдала бы
    select_initial_redaction.

    Редакции с reddate позже «сегодня» пропускаются: иначе max(reddate) берёт
    ещё не вступившую правку, а EBPI на таких redid нередко отдаёт пустой
    redtext (валидный, но пустой .docx). Пример — Постановление №255, где
    будущая редакция от 01.03.2028 давала пустой файл при «положительном»
    статусе covered. Тот же класс ловушки уже закрыт в ips_pravo.select_latest_redaction
    по статусу «не вступившая в силу».

    Args:
        redactions: список редакций из fetch_redactions.
        as_of: дата отсчёта в YYYYMMDD; по умолчанию текущая UTC-дата.
            Нужен в тестах, чтобы не зависеть от календаря машины.

    Returns:
        Редакция с максимальным reddate среди уже вступивших (формат YYYYMMDD,
        сравнивается как строка), либо None — список пуст или все редакции ещё
        в будущем. Поле `actual` для этого выбора не годится — см. docstring
        select_initial_redaction.
    """
    if not redactions:
        return None
    today = as_of or datetime.now(timezone.utc).strftime("%Y%m%d")
    effective = [
        r for r in redactions
        if (rd := r.get("reddate") or "") and rd <= today
    ]
    if not effective:
        return None
    return max(effective, key=lambda r: r.get("reddate") or "")


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
    """Возвращает полный текст документа из EBPI, если он там покрыт.

    Главная точка входа модуля — последовательно идёт card -> redactions -> redtext,
    отбирая действующую редакцию (см. select_latest_redaction), и дополняет её
    списком актов-поправок (см. resolve_amending_acts), если редакция не исходная.

    Args:
        client: клиент EBPI (см. create_ebpi_client).
        pnum: eoNumber документа на publication.pravo.gov.ru.

    Returns:
        EbpiDocument с текстом (EbpiDocument.html) и метаданными редакции, либо
        None — документ не найден в EBPI (не покрыт или ещё не проиндексирован, см.
        docs/ebpi-coverage.md) или сбой сети на любом из шагов получения текста.
        Эти случаи не различаются — вызывающий код (service.py) не обязан знать
        причину, только что текста нет. Сбой именно резолвинга актов-поправок
        текст не блокирует — EbpiDocument.amending_acts просто останется короче.
    """
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
