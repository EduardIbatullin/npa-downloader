"""Модели запроса, результата и документов."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


class Status(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    OUT_OF_RANGE = "out_of_range"
    INVALID_INPUT = "invalid_input"
    NETWORK_ERROR = "network_error"
    INTEGRITY_ERROR = "integrity_error"


class MatchType(str, Enum):
    EXACT = "exact"
    DIGITS_ONLY = "digits_only"


# Граница охвата портала (дата подписания): искать раньше — после неудачи → out_of_range
COVERAGE_START = date(2011, 11, 8)


@dataclass(frozen=True)
class Query:
    """Входной запрос на поиск/скачивание документа."""

    eo_number: str | None = None
    authority_guid: str | None = None
    authority_name: str | None = None
    number: str | None = None
    date: date | None = None
    doc_type_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.date is not None:
            data["date"] = self.date.isoformat()
        return data


@dataclass
class DocItem:
    """Документ из ответа API (список или карточка)."""

    eo_number: str
    number: str
    document_date: date
    signatory_ids: list[str]
    document_type_id: str | None = None
    name: str | None = None
    complex_name: str | None = None
    pdf_file_length: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "eo_number": self.eo_number,
            "number": self.number,
            "document_date": self.document_date.isoformat(),
            "signatory_ids": list(self.signatory_ids),
            "document_type_id": self.document_type_id,
            "name": self.name,
            "complex_name": self.complex_name,
            "pdf_file_length": self.pdf_file_length,
        }


@dataclass
class DiagnosticHit:
    """Документ с тем же номером+датой у органа вне списка кандидатов."""

    eo_number: str
    authority_id: str | None
    number: str
    document_date: date

    def to_dict(self) -> dict[str, Any]:
        return {
            "eo_number": self.eo_number,
            "authority_id": self.authority_id,
            "number": self.number,
            "document_date": self.document_date.isoformat(),
        }


@dataclass
class Result:
    """Контракт результата этапа 1."""

    status: Status
    match_type: MatchType | None = None
    document: DocItem | None = None
    candidates: list[DocItem] = field(default_factory=list)
    diagnostics: list[DiagnosticHit] = field(default_factory=list)
    eo_number: str | None = None
    pdf_path: str | None = None
    message: str | None = None
    # Текст из эталонного банка правовой информации (EBPI, actual.pravo.gov.ru) —
    # заполняется только если документ там покрыт (законы, указы, постановления/
    # распоряжения Правительства, акты высших судов и Банка России; ведомственные
    # приказы не покрыты, см. npa_pipeline.ebpi). None — не покрыт или сбой сети;
    # это не ошибка всего результата, PDF при этом уже скачан и доступен.
    ebpi_docx_path: str | None = None
    ebpi_docstate: str | None = None  # напр. «Действует без изменений» / «Действует с изменениями»
    # Резервный .docx, когда EBPI документ не покрывает, но PDF не скан (текстовый
    # слой есть на всех страницах) — механическое извлечение текста (pypdf), без
    # OCR и без распознавания структуры/таблиц, см. npa_pipeline.text_layer_docx.
    # None — EBPI уже дал текст, либо PDF — скан (там нужен OCR), либо сбой.
    text_layer_docx_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "match_type": self.match_type.value if self.match_type else None,
            "document": self.document.to_dict() if self.document else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "eo_number": self.eo_number,
            "pdf_path": self.pdf_path,
            "message": self.message,
            "ebpi_docx_path": self.ebpi_docx_path,
            "ebpi_docstate": self.ebpi_docstate,
            "text_layer_docx_path": self.text_layer_docx_path,
        }


@dataclass
class ItsFileResult:
    """Один скачанный/найденный файл карточки ИТС (справочник или приказ)."""

    role: str  # document | document_word | order | cancellation | unknown
    file_id: int
    pdf_path: str | None = None
    pages: int | None = None
    caption: str | None = None  # подпись с карточки burondt — для UI
    order_number_caption: str | None = None
    order_date_caption: str | None = None
    # Текст PDF-файла, если он не скан (текстовый слой на всех страницах) —
    # механическое извлечение (pypdf), без OCR. ИТС структурно не ищутся в EBPI
    # (нет eoNumber), поэтому это не fallback после EBPI, как у НПА, а единственный
    # путь получить .docx. None — файл не PDF (уже Word), скан, или сбой.
    text_layer_docx_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "file_id": self.file_id,
            "pdf_path": self.pdf_path,
            "pages": self.pages,
            "caption": self.caption,
            # Подпись карточки burondt.ru не авторитетна — возможны неточности в
            # номере/дате приказа (см. docs/stage1-plan.md, «Несоответствие номера
            # утверждающего приказа»).
            "order_number_caption": self.order_number_caption,
            "order_date_caption": self.order_date_caption,
            "order_meta_verified": False,
            "text_layer_docx_path": self.text_layer_docx_path,
        }


@dataclass
class ItsResult:
    """Контракт результата для скачивания ИТС/НДТ (этап 1, расширение burondt.ru)."""

    status: Status
    designation: str | None = None
    url_id: int | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    files: list[ItsFileResult] = field(default_factory=list)
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "designation": self.designation,
            "url_id": self.url_id,
            "candidates": list(self.candidates),
            "files": [f.to_dict() for f in self.files],
            "message": self.message,
        }


def parse_api_date(value: str | None) -> date | None:
    """Парсит documentDate вида 2023-04-25T00:00:00."""
    if not value:
        return None
    text = value.strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    return date.fromisoformat(text)


def parse_user_date(value: str) -> date:
    """Парсит дату пользователя: YYYY-MM-DD или DD.MM.YYYY."""
    text = value.strip()
    if "." in text:
        day, month, year = text.split(".")
        return date(int(year), int(month), int(day))
    return date.fromisoformat(text)


def utc_now_iso() -> str:
    """Текущее время в UTC как ISO 8601 строка с суффиксом Z (не +00:00)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
