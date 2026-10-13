"""Разбор полного официального названия НПА → реквизиты для Query.

Поддерживаемые виды (не свободный полнотекст):

    Федеральный закон от 21.07.2014 № 219-ФЗ "О внесении…"

    Приказ Министерства природных ресурсов … от 19.07.2016 № 402

    ПРИКАЗ от 29 ноября 2019 г. N 814
    (в ред. Приказа Минприроды России от 28.04.2023 N 265)

Орган в названии часто в родительном падеже — приводим к именительному.
Скобки «(в ред. …)» отрезаются от основных реквизитов; при отсутствии органа
в шапке орган берётся из этой пометки (Минприроды России и т.п.).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from npa_pipeline.models import Query
from npa_pipeline.normalize import normalize_name

# Типы без органа в шапке — на портале подписант обычно Президент РФ.
_DEFAULT_AUTHORITY_BY_TYPE: dict[str, str] = {
    "федеральный закон": "Президент Российской Федерации",
    "федеральный конституционный закон": "Президент Российской Федерации",
}

_DOC_TYPES: tuple[str, ...] = (
    "федеральный конституционный закон",
    "федеральный закон",
    "постановление",
    "распоряжение",
    "приказ",
    "указ",
)

_MONTHS: dict[str, int] = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

_MONTH_ALT = "|".join(_MONTHS)

# Короткие имена органов → канон для Query (как в кэше authorities / aliases).
_SHORT_AUTHORITIES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), name)
    for pat, name in (
        (
            r"\bМинприроды(?:\s+(?:России|РФ|Российской Федерации))?\b",
            "Министерство природных ресурсов и экологии Российской Федерации",
        ),
        (
            r"\bМинфин(?:\s+(?:России|РФ|Российской Федерации))?\b",
            "Министерство финансов Российской Федерации",
        ),
        (
            r"\bРоснедра\b",
            "Федеральное агентство по недропользованию",
        ),
    )
)

_GENITIVE_PREFIXES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), repl)
    for pat, repl in (
        (r"^Министерства\b", "Министерство"),
        (r"^Федерального агентства\b", "Федеральное агентство"),
        (r"^Федеральной службы\b", "Федеральная служба"),
        (r"^Федерального бюро\b", "Федеральное бюро"),
        (r"^Правительства\b", "Правительство"),
        (r"^Президента\b", "Президент"),
        (r"^Государственной Думы\b", "Государственная Дума"),
        (r"^Совета Федерации\b", "Совет Федерации"),
        (r"^Центрального банка\b", "Центральный банк"),
        (r"^Следственного комитета\b", "Следственный комитет"),
        (r"^Генеральной прокуратуры\b", "Генеральная прокуратура"),
        (r"^Администрации\b", "Администрация"),
    )
)

# Дата: 21.07.2014 или 29 ноября 2019 г.
_DATE = rf"""
    (?:
        (?P<d>\d{{1,2}})[.\-_/](?P<m>\d{{1,2}})[.\-_/](?P<y>\d{{4}})
      |
        (?P<d_word>\d{{1,2}})\s+(?P<month>{_MONTH_ALT})\s+(?P<y_word>\d{{4}})\s*\.?г?\.?
    )
"""

# Первое вхождение «от <дата> №/N <номер>» — основные реквизиты.
_REQUISITES = re.compile(
    rf"""
    ^(?P<head>.+?)\s+
    от\s+
    {_DATE}\s+
    (?:№|Nº|N\.?|No\.?)\s*
    (?P<number>[^\s\"«»„“”\)]+)
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

# Пометка об редакции: орган часто только здесь.
_REDACTION = re.compile(
    r"""
    \(\s*в\s+ред\.?\s+
    (?:Приказа|Постановления|Распоряжения|Указа)\s+
    (?P<authority>.+?)\s+
    от\s+
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

_PARENS_REDACTION = re.compile(
    r"\(\s*в\s+ред\..*?\)",
    re.IGNORECASE | re.DOTALL,
)

_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class ParsedCitation:
    """Разобранные реквизиты из полного названия."""

    number: str
    date: date
    authority_name: str | None
    doc_type: str | None = None

    def to_query(self) -> Query:
        return Query(
            authority_name=self.authority_name,
            number=self.number,
            date=self.date,
        )

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "date": self.date.isoformat(),
            "authority_name": self.authority_name,
            "doc_type": self.doc_type,
        }


def _normalize_spaces(text: str) -> str:
    cleaned = _BR.sub("\n", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    return _SPACES.sub(" ", cleaned).strip()


def _parse_date_from_match(match: re.Match[str]) -> date:
    if match.groupdict().get("month"):
        month = _MONTHS[match.group("month").casefold()]
        return date(int(match.group("y_word")), month, int(match.group("d_word")))
    return date(int(match.group("y")), int(match.group("m")), int(match.group("d")))


def _strip_doc_type(head: str) -> tuple[str | None, str]:
    folded = head.strip()
    low = folded.casefold()
    for dtype in _DOC_TYPES:
        if low == dtype:
            return dtype, ""
        prefix = dtype + " "
        if low.startswith(prefix):
            return dtype, folded[len(prefix) :].strip()
    return None, folded


def _expand_short_authority(text: str) -> str | None:
    """Минприроды России / Роснедра → полное имя из справочника."""
    for pat, name in _SHORT_AUTHORITIES:
        if pat.search(text):
            return name
    return None


def _genitive_to_nominative(authority: str) -> str:
    text = authority.strip()
    short = _expand_short_authority(text)
    if short:
        return short
    text = re.sub(r"\bРФ\b", "Российской Федерации", text)
    text = re.sub(r"\bРоссии\b", "Российской Федерации", text)
    for pat, repl in _GENITIVE_PREFIXES:
        text, n = pat.subn(repl, text, count=1)
        if n:
            break
    return _SPACES.sub(" ", text).strip()


def _authority_from_head(head: str, doc_type: str | None) -> str | None:
    rest = head.strip()
    if not rest:
        if doc_type and doc_type in _DEFAULT_AUTHORITY_BY_TYPE:
            return _DEFAULT_AUTHORITY_BY_TYPE[doc_type]
        return None
    first = rest.split(",", 1)[0].strip()
    return _genitive_to_nominative(first) or None


def _authority_from_redaction(original: str) -> str | None:
    """Орган из «(в ред. Приказа Минприроды России от …)»."""
    match = _REDACTION.search(original)
    if not match:
        return None
    raw = match.group("authority").strip()
    return _genitive_to_nominative(raw) or None


def _clean_number(raw: str) -> str:
    number = raw.strip().rstrip(".,;)]}")
    for q in "\"«»„“”":
        if q in number:
            number = number.split(q, 1)[0].strip()
    return number


def parse_citation(text: str) -> ParsedCitation:
    """Разбирает полное официальное название НПА в реквизиты.

    Поддерживаемые формы названия — см. докстринг модуля. Орган берётся из шапки
    названия; если там его нет (например, у федеральных законов, которые обычно
    подписывает Президент — см. _DEFAULT_AUTHORITY_BY_TYPE) или не распознан,
    пробует взять его из пометки «(в ред. …)».

    Args:
        text: полное название документа, как оно приходит с портала (`complexName`)
            или вставлено пользователем.

    Returns:
        Разобранные реквизиты (число, дата, орган, вид акта).

    Raises:
        ValueError: название пустое, не содержит узнаваемой формы «… от ДД.ММ.ГГГГ
            № номер», в дате есть ошибка, номер или орган не извлеклись, либо
            извлечённый орган не проходит нормализацию (см. normalize_name) — во
            всех случаях с человекочитаемым объяснением, что не удалось разобрать.
    """
    if not text or not str(text).strip():
        raise ValueError("Пустое название документа")

    original = _normalize_spaces(text)
    # Основные реквизиты — без хвоста «(в ред. …)», иначе цепляется дата правки.
    main = _PARENS_REDACTION.sub(" ", original)
    main = _SPACES.sub(" ", main).strip()

    match = _REQUISITES.search(main)
    if not match:
        raise ValueError(
            "Не удалось разобрать название: ожидается вид "
            "«… от ДД.ММ.ГГГГ № номер» или «… от 29 ноября 2019 г. N номер»"
        )

    try:
        doc_date = _parse_date_from_match(match)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Некорректная дата в названии: {exc}") from exc

    number = _clean_number(match.group("number"))
    if not number:
        raise ValueError("В названии не найден номер документа")

    head = match.group("head").strip()
    doc_type, authority_raw = _strip_doc_type(head)
    authority = _authority_from_head(authority_raw, doc_type)
    if not authority:
        authority = _authority_from_redaction(original)

    if not authority:
        raise ValueError(
            "Не удалось определить орган из названия "
            "(укажите орган явно или используйте полный complexName с портала)"
        )

    # sanity: нормализация не должна обнулять строку
    if not normalize_name(authority):
        raise ValueError("Некорректное название органа после разбора")

    return ParsedCitation(
        number=number,
        date=doc_date,
        authority_name=authority,
        doc_type=doc_type,
    )


def query_from_citation(text: str) -> Query:
    """Удобная обёртка: название → Query для fetch_document."""
    return parse_citation(text).to_query()
