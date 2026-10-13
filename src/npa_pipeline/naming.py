"""Имена файлов и отображаемые названия НПА."""

from __future__ import annotations

import re

from npa_pipeline.models import DocItem

_WIN_FORBIDDEN = '<>:"/\\|?*'
_DATE_DOTS = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_SPACES = re.compile(r"\s+")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)

# Длинные первыми. Формы как в complexName (часто родительный падеж).
_AUTHORITY_ABBREVS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(re.escape(src), re.IGNORECASE), dst)
    for src, dst in (
        (
            "Министерства природных ресурсов и экологии Российской Федерации",
            "Минприроды России",
        ),
        (
            "Министерство природных ресурсов и экологии Российской Федерации",
            "Минприроды России",
        ),
        (
            "Министерства финансов Российской Федерации",
            "Минфина России",
        ),
        (
            "Министерство финансов Российской Федерации",
            "Минфин России",
        ),
        (
            "Федерального агентства по недропользованию",
            "Роснедра",
        ),
        (
            "Федеральное агентство по недропользованию",
            "Роснедра",
        ),
        (
            "Правительства Российской Федерации",
            "Правительства РФ",
        ),
        (
            "Правительство Российской Федерации",
            "Правительство РФ",
        ),
        (
            "Президента Российской Федерации",
            "Президента РФ",
        ),
        (
            "Президент Российской Федерации",
            "Президент РФ",
        ),
    )
)


def _requisite_line(complex_name: str | None) -> str:
    """Первая строка complexName без тематического заголовка в кавычках."""
    raw = (complex_name or "").strip()
    if not raw:
        return ""
    raw = _BR.sub("\n", raw)
    raw = raw.split("\n", 1)[0].strip()
    if '"' in raw:
        raw = raw.split('"', 1)[0].strip()
    if "«" in raw:
        raw = raw.split("«", 1)[0].strip()
    return _SPACES.sub(" ", raw).strip()


def abbreviate_authorities(text: str) -> str:
    """Сокращает типичные органы в тексте (для имён файлов)."""
    out = text
    for pat, repl in _AUTHORITY_ABBREVS:
        out = pat.sub(repl, out)
    return out


def format_document_title(
    *,
    name: str | None = None,
    complex_name: str | None = None,
    number: str | None = None,
    document_date: str | None = None,
) -> str:
    """Собирает человекочитаемое описание документа: реквизиты + тема.

    Example:
        Приказ Министерства … от 07.04.2026 № 191
        «Об утверждении…»

    Args:
        name: тема документа (поле «name» с портала) — предпочтительный источник
            темы; если дублирует начало реквизитной строки из complex_name,
            отбрасывается, чтобы не повторять одно и то же дважды.
        complex_name: полное официальное название с портала. Первая строка — это
            реквизиты (head); если name не дал темы, тема достаётся из второй
            строки complex_name или из текста после кавычки.
        number: номер документа — используется только в последнем фолбэке, если
            не нашлось ни реквизитов, ни темы.
        document_date: дата документа строкой — тоже только в этом фолбэке,
            вместе с number.

    Returns:
        Строка в порядке убывания информативности: «реквизиты\\n«тема»» (есть оба),
        только реквизиты, только тема, «Документ № <number> от <document_date>»
        (есть номер и дата, но не реквизиты и не тема), иначе просто number
        (или пустая строка, если вообще ничего не передано).
    """
    head = _requisite_line(complex_name)
    theme = (name or "").strip()
    if theme.startswith('"') or theme.startswith("«"):
        pass
    elif theme and not theme.startswith("Об ") and not theme.startswith("О "):
        # name с портала часто уже без кавычек — оставляем как есть
        pass

    # если name — это весь complexName, не дублируем
    if theme and head and theme.casefold().startswith(head.casefold()[:40]):
        theme = ""
    if not theme and complex_name:
        # вторая строка / хвост после кавычки
        rest = _BR.sub("\n", complex_name)
        parts = rest.split("\n", 1)
        if len(parts) > 1:
            theme = parts[1].strip().strip('"«»').strip()
        elif '"' in rest:
            theme = rest.split('"', 1)[1].strip().strip('"').strip()

    if head and theme:
        if not (theme.startswith("«") or theme.startswith('"')):
            theme_fmt = f"«{theme}»"
        else:
            theme_fmt = theme
        return f"{head}\n{theme_fmt}"
    if head:
        return head
    if theme:
        return theme
    if number and document_date:
        return f"Документ № {number} от {document_date}"
    return number or ""


def human_filename_stem(document: DocItem) -> str:
    """Строит имя файла вида «Приказ Минприроды России от 31_05_2019 N 691».

    Берёт первую строку complex_name, сокращает органы (см. abbreviate_authorities),
    переводит дату в DD_MM_YYYY, «№» → «N».

    Returns:
        Готовый stem без расширения, обрезанный до 180 символов, если вышло
        длиннее. Если у document нет complex_name — собирает «Документ от
        DD_MM_YYYY N <номер или eoNumber>» вместо пустой строки. Если не вышло
        собрать вообще ничего — возвращает eo_number как последний фолбэк.
    """
    raw = _requisite_line(document.complex_name)

    if not raw:
        d = document.document_date.strftime("%d_%m_%Y")
        num = document.number or document.eo_number
        raw = f"Документ от {d} N {num}"

    raw = abbreviate_authorities(raw)
    raw = raw.replace("№", "N").replace("Nº", "N")
    raw = _DATE_DOTS.sub(r"\1_\2_\3", raw)
    raw = _SPACES.sub(" ", raw).strip()

    for ch in _WIN_FORBIDDEN:
        raw = raw.replace(ch, "_")
    raw = raw.rstrip(" .")

    if len(raw) > 180:
        raw = raw[:180].rstrip(" .")

    return raw or document.eo_number
