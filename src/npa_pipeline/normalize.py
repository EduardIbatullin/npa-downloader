"""Нормализация номера документа и названия органа."""

from __future__ import annotations

import re

# Типографские и прочие «тире/дефисы» → ASCII '-'
_DASH_CHARS = (
    "\u2010"  # hyphen
    "\u2011"  # non-breaking hyphen
    "\u2012"  # figure dash
    "\u2013"  # en dash
    "\u2014"  # em dash
    "\u2015"  # horizontal bar
    "\u2212"  # minus sign
    "\uFE58"  # small em dash
    "\uFE63"  # small hyphen-minus
    "\uFF0D"  # fullwidth hyphen-minus
)

_DASH_TRANS = str.maketrans({c: "-" for c in _DASH_CHARS})

# Сокращённые названия органов
_NAME_ALIASES: dict[str, str] = {
    "минфин": "министерство финансов",
    "минприроды": "министерство природных ресурсов и экологии",
    "роснедра": "федеральное агентство по недропользованию",
}

_QUOTE_CHARS = "\"'«»„“”"


def normalize_number(value: str) -> str:
    """Приводит номер к форме для запроса/сравнения: тире→'-', без пробелов и №.

    Регистр не меняется — сервер сравнивает регистронезависимо, а в запрос
    можно отправлять как есть.
    """
    text = value.translate(_DASH_TRANS)
    text = text.replace("№", "").replace("Nº", "").replace("No.", "")
    text = re.sub(r"\s+", "", text)
    return text


def numbers_equal(a: str, b: str) -> bool:
    """Сравнение номеров после нормализации, без учёта регистра."""
    return normalize_number(a).casefold() == normalize_number(b).casefold()


def digit_prefix(value: str) -> str:
    """Цифровая часть до первого нецифрового символа; '' если номер не с цифры."""
    normalized = normalize_number(value)
    if not normalized or not normalized[0].isdigit():
        return ""
    i = 0
    while i < len(normalized) and normalized[i].isdigit():
        i += 1
    return normalized[:i]


def matches_digit_boundary(number: str, prefix: str) -> bool:
    """Номер начинается с prefix, дальше — нецифра или конец строки."""
    if not prefix:
        return False
    normalized = normalize_number(number)
    if not normalized.casefold().startswith(prefix.casefold()):
        return False
    rest = normalized[len(prefix) :]
    return not rest or not rest[0].isdigit()


def normalize_name(value: str) -> str:
    """Нормализация названия органа для сопоставления с кэшем."""
    text = value.replace("ё", "е").replace("Ё", "е")
    for q in _QUOTE_CHARS:
        text = text.replace(q, "")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    # Простые сокращения: целое слово/фраза
    if text in _NAME_ALIASES:
        text = _NAME_ALIASES[text]
    return text
