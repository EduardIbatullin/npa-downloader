"""Единая SQLite-база проекта: органы, документы, файлы, связи."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from npa_pipeline.models import DocItem, parse_api_date, utc_now_iso
from npa_pipeline.normalize import normalize_number

DEFAULT_DB_PATH = Path("data/npa.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS authorities (
    guid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    block TEXT,
    category TEXT,
    updated_at TEXT NOT NULL,
    weight INTEGER
);
CREATE INDEX IF NOT EXISTS idx_authorities_name_norm ON authorities(name_norm);

CREATE TABLE IF NOT EXISTS document_types (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    weight INTEGER
);

CREATE TABLE IF NOT EXISTS documents (
    eo_number TEXT PRIMARY KEY,
    portal_id TEXT,
    number TEXT NOT NULL,
    number_norm TEXT NOT NULL,
    document_date TEXT NOT NULL,
    publish_date TEXT,
    doc_type_id TEXT,
    name TEXT,
    complex_name TEXT,
    title TEXT,
    jd_reg_number TEXT,
    jd_reg_date TEXT,
    pages_count INTEGER,
    pdf_file_length INTEGER,
    raw_card_json TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (doc_type_id) REFERENCES document_types(id)
);
CREATE INDEX IF NOT EXISTS idx_documents_number_norm ON documents(number_norm);
CREATE INDEX IF NOT EXISTS idx_documents_document_date ON documents(document_date);

CREATE TABLE IF NOT EXISTS document_signatories (
    eo_number TEXT NOT NULL,
    authority_guid TEXT NOT NULL,
    is_main INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (eo_number, authority_guid),
    FOREIGN KEY (eo_number) REFERENCES documents(eo_number)
);
CREATE INDEX IF NOT EXISTS idx_document_signatories_authority
    ON document_signatories(authority_guid);

CREATE TABLE IF NOT EXISTS document_files (
    eo_number TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    pages INTEGER,
    downloaded_at TEXT NOT NULL,
    verified_at TEXT,
    FOREIGN KEY (eo_number) REFERENCES documents(eo_number)
);

CREATE TABLE IF NOT EXISTS document_ebpi (
    eo_number TEXT PRIMARY KEY,
    covered INTEGER NOT NULL,   -- 0/1: найден ли документ в EBPI вообще (кэшируем
                                -- и отрицательный результат — большинство приказов
                                -- там не покрыты, см. npa_pipeline.ebpi)
    docid INTEGER,
    docstate TEXT,
    redid INTEGER,
    red_date TEXT,
    docx_path TEXT,
    checked_at TEXT NOT NULL,
    FOREIGN KEY (eo_number) REFERENCES documents(eo_number)
);

CREATE TABLE IF NOT EXISTS document_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_eo TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    target_eo TEXT,
    target_ref_text TEXT,
    effective_date TEXT,
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_eo) REFERENCES documents(eo_number),
    FOREIGN KEY (target_eo) REFERENCES documents(eo_number)
);
CREATE INDEX IF NOT EXISTS idx_document_relations_source ON document_relations(source_eo);
CREATE INDEX IF NOT EXISTS idx_document_relations_target ON document_relations(target_eo);

CREATE TABLE IF NOT EXISTS its_documents (
    url_id            INTEGER PRIMARY KEY,
    designation       TEXT NOT NULL,
    designation_norm  TEXT NOT NULL,
    title             TEXT,
    raw_card_html     TEXT,
    first_seen_at     TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_its_documents_designation_norm ON its_documents(designation_norm);

CREATE TABLE IF NOT EXISTS its_files (
    file_id               INTEGER PRIMARY KEY,
    url_id                INTEGER NOT NULL,
    role                  TEXT NOT NULL,
    caption               TEXT,
    order_number_caption  TEXT,
    order_date_caption    TEXT,
    updated_at            TEXT NOT NULL,
    FOREIGN KEY (url_id) REFERENCES its_documents(url_id)
);
CREATE INDEX IF NOT EXISTS idx_its_files_url_id ON its_files(url_id);

CREATE TABLE IF NOT EXISTS its_document_files (
    file_id       INTEGER PRIMARY KEY,
    file_path     TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    pages         INTEGER,
    downloaded_at TEXT NOT NULL,
    verified_at   TEXT,
    FOREIGN KEY (file_id) REFERENCES its_files(file_id)
);

CREATE TABLE IF NOT EXISTS its_crawl_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ips_documents (
    nd        TEXT PRIMARY KEY,  -- внутренний id документа в ips.pravo.gov.ru,
                                  -- не eoNumber (см. npa_pipeline.ips_pravo) —
                                  -- без FK на documents, это структурно другой
                                  -- источник (Status.FOUND_TEXT_ONLY)
    adoption  TEXT,
    state     TEXT,
    docx_path TEXT NOT NULL,
    found_at  TEXT NOT NULL
);
"""


@dataclass
class FileRecord:
    """Запись о скачанном PDF — зеркало строки document_files."""

    eo_number: str
    file_path: str
    sha256: str
    size_bytes: int
    pages: int | None
    downloaded_at: str
    verified_at: str | None = None


@dataclass
class EbpiRecord:
    """Результат проверки документа в EBPI — зеркало строки document_ebpi.

    Кэшируется и отрицательный результат (covered=False), чтобы не дёргать сеть
    повторно за уже известным «не покрыт», см. npa_pipeline.ebpi.
    """

    eo_number: str
    covered: bool
    docid: int | None
    docstate: str | None
    redid: int | None
    red_date: str | None
    docx_path: str | None
    checked_at: str


@dataclass
class IpsDocumentRecord:
    """Найденный через резерв ips.pravo.gov.ru документ — зеркало строки
    ips_documents (см. npa_pipeline.service._try_ips_fallback). Нет eo_number —
    это не тот источник."""

    nd: str
    adoption: str | None
    state: str | None
    docx_path: str
    found_at: str


def resolve_db_path(db_path: str | Path = DEFAULT_DB_PATH) -> Path:
    """Путь к БД; при первом запуске копирует старый authorities.db → npa.db."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and path.name == "npa.db":
        legacy = path.parent / "authorities.db"
        if legacy.exists():
            shutil.copy2(legacy, path)
    return path


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Открывает соединение с `PRAGMA foreign_keys = ON` — FK в SCHEMA реально
    проверяются (не просто декларативны), см. record_file/record_ebpi/record_relation."""
    path = resolve_db_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Создаёт таблицы (CREATE TABLE IF NOT EXISTS) и докатывает старые базы.

    Идемпотентно — безопасно вызывать на каждое открытие БД. Миграция ALTER TABLE
    ниже нужна только для баз, созданных до того, как у authorities появилось
    поле weight — на новых базах колонка уже есть из SCHEMA, ветка не сработает.
    """
    conn.executescript(SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(authorities)")}
    if "weight" not in cols:
        conn.execute("ALTER TABLE authorities ADD COLUMN weight INTEGER")
    conn.commit()


class DocumentStore:
    """Запись и чтение документов / файлов в единой БД."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = resolve_db_path(db_path)
        with self._connect() as conn:
            init_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def upsert_from_card(self, card: dict[str, Any]) -> str:
        """Сохраняет полную карточку /api/Document (INSERT...ON CONFLICT DO UPDATE).

        Полностью заменяет document_signatories документа (DELETE + INSERT), а не
        дополняет — список подписантов в карточке портала всегда полный, слияние
        со старым списком было бы неверным при его изменении между вызовами.

        Returns:
            eoNumber (card["eoNumber"]) — тот же, что был передан.

        Raises:
            ValueError: в карточке нет documentDate — без даты документ не
                сохранить (document_date — NOT NULL в схеме).
        """
        eo = card["eoNumber"]
        now = utc_now_iso()
        doc_date = parse_api_date(card.get("documentDate"))
        if doc_date is None:
            raise ValueError(f"Нет documentDate в карточке {eo}")

        number = card.get("number") or ""
        publish = parse_api_date(card.get("publishDateShort"))
        jd_reg = parse_api_date(card.get("jdRegDate"))

        doc_type = card.get("documentType") or {}
        doc_type_id = card.get("documentTypeId") or doc_type.get("id")
        doc_type_name = doc_type.get("name")
        doc_type_weight = doc_type.get("weight")

        with self._connect() as conn:
            if doc_type_id and doc_type_name:
                conn.execute(
                    """
                    INSERT INTO document_types (id, name, weight)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        weight = excluded.weight
                    """,
                    (doc_type_id, doc_type_name, doc_type_weight),
                )

            existing = conn.execute(
                "SELECT first_seen_at FROM documents WHERE eo_number = ?",
                (eo,),
            ).fetchone()
            first_seen = existing["first_seen_at"] if existing else now

            conn.execute(
                """
                INSERT INTO documents (
                    eo_number, portal_id, number, number_norm, document_date, publish_date,
                    doc_type_id, name, complex_name, title, jd_reg_number, jd_reg_date,
                    pages_count, pdf_file_length, raw_card_json, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(eo_number) DO UPDATE SET
                    portal_id = excluded.portal_id,
                    number = excluded.number,
                    number_norm = excluded.number_norm,
                    document_date = excluded.document_date,
                    publish_date = excluded.publish_date,
                    doc_type_id = excluded.doc_type_id,
                    name = excluded.name,
                    complex_name = excluded.complex_name,
                    title = excluded.title,
                    jd_reg_number = excluded.jd_reg_number,
                    jd_reg_date = excluded.jd_reg_date,
                    pages_count = excluded.pages_count,
                    pdf_file_length = excluded.pdf_file_length,
                    raw_card_json = excluded.raw_card_json,
                    updated_at = excluded.updated_at
                """,
                (
                    eo,
                    card.get("id"),
                    number,
                    normalize_number(number),
                    doc_date.isoformat(),
                    publish.isoformat() if publish else None,
                    doc_type_id,
                    card.get("name"),
                    card.get("complexName"),
                    card.get("title"),
                    card.get("jdRegNumber"),
                    jd_reg.isoformat() if jd_reg else None,
                    card.get("pagesCount"),
                    card.get("pdfFileLength"),
                    json.dumps(card, ensure_ascii=False),
                    first_seen,
                    now,
                ),
            )

            conn.execute(
                "DELETE FROM document_signatories WHERE eo_number = ?",
                (eo,),
            )
            authorities = card.get("signatoryAuthorities") or []
            if authorities:
                for auth in authorities:
                    guid = auth.get("id")
                    if not guid:
                        continue
                    conn.execute(
                        """
                        INSERT INTO document_signatories (eo_number, authority_guid, is_main)
                        VALUES (?, ?, ?)
                        """,
                        (eo, guid, 1 if auth.get("isMain") else 0),
                    )
            else:
                sid = card.get("signatoryAuthorityId")
                if sid:
                    conn.execute(
                        """
                        INSERT INTO document_signatories (eo_number, authority_guid, is_main)
                        VALUES (?, ?, 1)
                        """,
                        (eo, sid),
                    )
            conn.commit()
        return eo

    def upsert_from_doc_item(self, document: DocItem) -> str:
        """Пишет минимальную запись без полной карточки портала.

        Используется там, где полной карточки нет и не предполагается (импорт
        старых JSON-сайдкаров, минимальная запись акта-поправки из EBPI — см.
        service._record_amending_act).

        Returns:
            document.eo_number. Если для этого eo_number уже есть полная карточка
            (raw_card_json не пусто) — ничего не перезаписывает и просто
            возвращает eo_number: урезанные данные не должны затирать полные.
        """
        now = utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT first_seen_at, raw_card_json FROM documents WHERE eo_number = ?",
                (document.eo_number,),
            ).fetchone()
            if existing and existing["raw_card_json"]:
                # Полная карточка уже есть — не затираем урезанными данными
                return document.eo_number

            first_seen = existing["first_seen_at"] if existing else now
            conn.execute(
                """
                INSERT INTO documents (
                    eo_number, portal_id, number, number_norm, document_date, publish_date,
                    doc_type_id, name, complex_name, title, jd_reg_number, jd_reg_date,
                    pages_count, pdf_file_length, raw_card_json, first_seen_at, updated_at
                ) VALUES (?, NULL, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, ?, NULL, ?, ?)
                ON CONFLICT(eo_number) DO UPDATE SET
                    number = excluded.number,
                    number_norm = excluded.number_norm,
                    document_date = excluded.document_date,
                    doc_type_id = COALESCE(excluded.doc_type_id, documents.doc_type_id),
                    name = COALESCE(excluded.name, documents.name),
                    complex_name = COALESCE(excluded.complex_name, documents.complex_name),
                    pdf_file_length = COALESCE(excluded.pdf_file_length, documents.pdf_file_length),
                    updated_at = excluded.updated_at
                """,
                (
                    document.eo_number,
                    document.number,
                    normalize_number(document.number),
                    document.document_date.isoformat(),
                    document.document_type_id,
                    document.name,
                    document.complex_name,
                    document.pdf_file_length,
                    first_seen,
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM document_signatories WHERE eo_number = ?",
                (document.eo_number,),
            )
            for i, sid in enumerate(document.signatory_ids):
                conn.execute(
                    """
                    INSERT INTO document_signatories (eo_number, authority_guid, is_main)
                    VALUES (?, ?, ?)
                    """,
                    (document.eo_number, sid, 1 if i == 0 else 0),
                )
            conn.commit()
        return document.eo_number

    def record_file(
        self,
        *,
        eo_number: str,
        file_path: str | Path,
        sha256: str,
        size_bytes: int,
        pages: int | None,
    ) -> FileRecord:
        """Регистрирует скачанный PDF в document_files.

        Raises:
            ValueError: eo_number не найден в documents (FK) — карточка документа
                должна быть сохранена (upsert_from_card/upsert_from_doc_item)
                раньше, чем его файл.
        """
        now = utc_now_iso()
        path_str = str(file_path)
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM documents WHERE eo_number = ?",
                (eo_number,),
            ).fetchone()
            if not exists:
                raise ValueError(f"Документ {eo_number} не найден в БД — сначала upsert")

            conn.execute(
                """
                INSERT INTO document_files (
                    eo_number, file_path, sha256, size_bytes, pages, downloaded_at, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(eo_number) DO UPDATE SET
                    file_path = excluded.file_path,
                    sha256 = excluded.sha256,
                    size_bytes = excluded.size_bytes,
                    pages = excluded.pages,
                    downloaded_at = excluded.downloaded_at,
                    verified_at = excluded.verified_at
                """,
                (eo_number, path_str, sha256, size_bytes, pages, now, now),
            )
            conn.commit()
        return FileRecord(
            eo_number=eo_number,
            file_path=path_str,
            sha256=sha256,
            size_bytes=size_bytes,
            pages=pages,
            downloaded_at=now,
            verified_at=now,
        )

    def get_file(self, eo_number: str) -> FileRecord | None:
        """Возвращает запись о скачанном файле, либо None, если файл не скачивался."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM document_files WHERE eo_number = ?",
                (eo_number,),
            ).fetchone()
        if not row:
            return None
        return FileRecord(
            eo_number=row["eo_number"],
            file_path=row["file_path"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            pages=row["pages"],
            downloaded_at=row["downloaded_at"],
            verified_at=row["verified_at"],
        )

    def record_ebpi(
        self,
        *,
        eo_number: str,
        covered: bool,
        docid: int | None = None,
        docstate: str | None = None,
        redid: int | None = None,
        red_date: str | None = None,
        docx_path: str | Path | None = None,
    ) -> EbpiRecord:
        """Кэширует результат проверки EBPI (включая отрицательный, covered=False).

        См. npa_pipeline.ebpi и docs/ebpi-coverage.md «Кэш в БД» — без негативного
        кэша каждый fetch_document() на непокрытом документе (большинство —
        приказы) бил бы в сеть заново за уже известным «нет».

        Raises:
            ValueError: eo_number не найден в documents (FK) — как у record_file.
        """
        now = utc_now_iso()
        path_str = str(docx_path) if docx_path is not None else None
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM documents WHERE eo_number = ?",
                (eo_number,),
            ).fetchone()
            if not exists:
                raise ValueError(f"Документ {eo_number} не найден в БД — сначала upsert")

            conn.execute(
                """
                INSERT INTO document_ebpi (
                    eo_number, covered, docid, docstate, redid, red_date, docx_path, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(eo_number) DO UPDATE SET
                    covered = excluded.covered,
                    docid = excluded.docid,
                    docstate = excluded.docstate,
                    redid = excluded.redid,
                    red_date = excluded.red_date,
                    docx_path = excluded.docx_path,
                    checked_at = excluded.checked_at
                """,
                (eo_number, int(covered), docid, docstate, redid, red_date, path_str, now),
            )
            conn.commit()
        return EbpiRecord(
            eo_number=eo_number,
            covered=covered,
            docid=docid,
            docstate=docstate,
            redid=redid,
            red_date=red_date,
            docx_path=path_str,
            checked_at=now,
        )

    def get_ebpi(self, eo_number: str) -> EbpiRecord | None:
        """Возвращает закэшированный результат проверки EBPI.

        Returns:
            EbpiRecord, либо None — документ ещё никогда не проверялся (не то же
            самое, что covered=False, см. EbpiRecord.covered — это уже проверено
            и не покрыто).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM document_ebpi WHERE eo_number = ?",
                (eo_number,),
            ).fetchone()
        if not row:
            return None
        return EbpiRecord(
            eo_number=row["eo_number"],
            covered=bool(row["covered"]),
            docid=row["docid"],
            docstate=row["docstate"],
            redid=row["redid"],
            red_date=row["red_date"],
            docx_path=row["docx_path"],
            checked_at=row["checked_at"],
        )

    def record_ips_document(
        self,
        *,
        nd: str,
        adoption: str | None,
        state: str | None,
        docx_path: str | Path,
    ) -> IpsDocumentRecord:
        """Регистрирует документ, найденный через резерв ips.pravo.gov.ru
        (см. npa_pipeline.service._try_ips_fallback) — без этого
        GET /api/docx/{nd} (webapp.py) не смог бы отдать файл по отдельному
        запросу: nd не хранится нигде, кроме Result текущего запроса."""
        now = utc_now_iso()
        path_str = str(docx_path)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ips_documents (nd, adoption, state, docx_path, found_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(nd) DO UPDATE SET
                    adoption = excluded.adoption,
                    state = excluded.state,
                    docx_path = excluded.docx_path,
                    found_at = excluded.found_at
                """,
                (nd, adoption, state, path_str, now),
            )
            conn.commit()
        return IpsDocumentRecord(nd=nd, adoption=adoption, state=state, docx_path=path_str, found_at=now)

    def get_ips_document(self, nd: str) -> IpsDocumentRecord | None:
        """Возвращает запись о документе из резерва ips.pravo.gov.ru по nd, либо
        None — такой nd не регистрировался."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ips_documents WHERE nd = ?", (nd,)).fetchone()
        if not row:
            return None
        return IpsDocumentRecord(
            nd=row["nd"],
            adoption=row["adoption"],
            state=row["state"],
            docx_path=row["docx_path"],
            found_at=row["found_at"],
        )

    def record_relation(
        self,
        *,
        source_eo: str,
        relation_type: str,
        target_eo: str | None = None,
        target_ref_text: str | None = None,
        effective_date: str | None = None,
        origin: str,
    ) -> None:
        """Записывает связь между документами, идемпотентно.

        Пока один вид связи — «source_eo изменяет target_eo» (relation_type=
        'amends', origin='ebpi', см. npa_pipeline.ebpi.resolve_amending_acts).

        Args:
            source_eo: документ-источник связи. Должен уже существовать в
                documents (FK) — для акта-поправки, которого мы отдельно не
                искали на портале, это обычно минимальная запись через
                upsert_from_doc_item на данных из карточки EBPI, не полная
                карточка портала.
            target_eo: документ-цель связи; тоже должен существовать (FK).

        Повторный вызов с теми же source_eo/relation_type/target_eo не плодит
        дубли — проверяем вручную перед INSERT, т.к. в схеме нет unique-ограничения
        на эту комбинацию.

        Raises:
            sqlite3.IntegrityError: source_eo или target_eo не найдены в documents
                (FK — проверяется только потому, что connect() включает
                PRAGMA foreign_keys = ON).
        """
        now = utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT 1 FROM document_relations
                WHERE source_eo = ? AND relation_type = ? AND target_eo IS ?
                """,
                (source_eo, relation_type, target_eo),
            ).fetchone()
            if existing:
                return
            conn.execute(
                """
                INSERT INTO document_relations (
                    source_eo, relation_type, target_eo, target_ref_text,
                    effective_date, origin, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (source_eo, relation_type, target_eo, target_ref_text, effective_date, origin, now),
            )
            conn.commit()

    def get_relations_for_target(self, target_eo: str) -> list[dict[str, Any]]:
        """Возвращает связи, у которых target_eo == данному (чем изменён документ).

        Returns:
            Строки document_relations как словари, отсортированные по
            effective_date (дате вступления в силу) — пустой список, если связей
            нет.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM document_relations
                WHERE target_eo = ?
                ORDER BY effective_date
                """,
                (target_eo,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_document_row(self, eo_number: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE eo_number = ?",
                (eo_number,),
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            sigs = conn.execute(
                """
                SELECT authority_guid, is_main FROM document_signatories
                WHERE eo_number = ? ORDER BY is_main DESC, authority_guid
                """,
                (eo_number,),
            ).fetchall()
            data["signatories"] = [
                {"authority_guid": s["authority_guid"], "is_main": bool(s["is_main"])}
                for s in sigs
            ]
            return data

    def count_documents(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def count_files(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM document_files").fetchone()[0])
