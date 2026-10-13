"""Локальный кэш карточек ИТС/НДТ (SQLite).

Обход диапазона UrlId burondt.ru и резолвинг обозначения → url_id. Мирроринг
AuthorityCache: самоинициализация схемы, чистый upsert (без удаления строк),
контракт resolve() — 0/1/много кандидатов.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import httpx

from npa_pipeline import burondt
from npa_pipeline.db import DEFAULT_DB_PATH, connect, init_schema, resolve_db_path
from npa_pipeline.http_client import BURONDT_PAGE_PAUSE_SECONDS
from npa_pipeline.models import utc_now_iso
from npa_pipeline.normalize import normalize_number

_LOWER_BOUND = 10  # UrlId 1-9 пустые, см. docs/stage1-plan.md «Калиброванный диапазон»
_BACK_MARGIN = 20  # запас назад от сохранённой границы — вдруг дозаполнили старые id

# «НДТ» — необязательное слово в «Обозначении» некоторых карточек (см. resolve),
# поэтому и извлечение базового номера, и распознавание «голого» запроса-номера
# должны его пропускать, а не только префикс «ИТС».
_BASE_NUMBER_RE = re.compile(r"^итс(?:ндт)?(\d+)")
_BARE_NUMBER_QUERY_RE = re.compile(r"^итс(?:ндт)?(\d+)$")
_YEAR_SUFFIX_RE = re.compile(r"-(\d{4})$")
_YEAR_IN_TEXT_RE = re.compile(r"(\d{4})")

# «ИТС 51-2025» / «ИТС НДТ 28-2021» / «ИТС 22.1-2021» — даже если после идёт тема.
_DESIGNATION_IN_TEXT_RE = re.compile(
    r"(?iu)\bитс(?:\s*ндт)?\s*\d+(?:[._]\d+)?(?:\s*[-–—]\s*\d{4})?"
)


def extract_its_designation(text: str) -> str:
    """Достаёт обозначение ИТС из строки вида «ИТС 51-2025 Литейное производство…».

    Returns:
        Найденное обозначение (например «ИТС 51-2025»), если в тексте есть явный
        паттерн. Иначе — исходный текст без изменений (так проходят запросы вида
        «2021» или «ИТС 53», где весь текст уже и есть обозначение).
    """
    if not text or not text.strip():
        return ""
    m = _DESIGNATION_IN_TEXT_RE.search(text)
    if m:
        return m.group(0)
    return text.strip()


def _designation_norm(designation: str) -> str:
    """Нормализация обозначения для сравнения/поиска.

    Подчёркивания → точки (часто в именах файлов: «22_1-2021» ≈ «22.1-2021»),
    хвостовые «._-» срезаем — иначе «ИТС 22.1-2021_» не совпадает с карточкой.
    """
    text = normalize_number(designation).casefold().replace("_", ".")
    return text.strip("._-")


def _like_pattern(needle: str) -> str:
    """Подстрока для SQL LIKE с экранированием % и _ (иначе _ = любой символ)."""
    escaped = (
        needle.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _is_word_variant(designation: str | None) -> bool:
    """Карточки «… в формате Word» — отдельный url_id с .docx, не PDF."""
    if not designation:
        return False
    n = designation.casefold()
    return "word" in n or "в формате" in n


def _canonical_its_key(designation: str) -> str:
    """Ключ «одна и та же ИТС»: без суффикса «в формате Word»."""
    n = _designation_norm(designation)
    for suffix in ("вформатеword", "форматеword", "word"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    return n.strip("._-")


def _base_number(designation_norm: str) -> str | None:
    """Числовая часть номера ИТС без года — 'итс53-2017'/'итсндт53' → '53'."""
    m = _BASE_NUMBER_RE.match(designation_norm)
    return m.group(1) if m else None


def _bare_number_query(needle: str) -> str | None:
    """Если запрос — это ровно 'ИТС <номер>' без года/чего-либо ещё, возвращает номер."""
    m = _BARE_NUMBER_QUERY_RE.match(needle)
    return m.group(1) if m else None


def _extract_year(designation_norm: str, order_dates: list[str | None]) -> str | None:
    """Год документа: из суффикса «Обозначения», иначе из даты приказа (см. resolve)."""
    m = _YEAR_SUFFIX_RE.search(designation_norm)
    if m:
        return m.group(1)
    for date_text in order_dates:
        if not date_text:
            continue
        m2 = _YEAR_IN_TEXT_RE.search(date_text)
        if m2:
            return m2.group(1)
    return None


class ItsCache:
    """Кэш карточек ИТС/НДТ в единой SQLite-базе проекта."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = resolve_db_path(db_path)
        with self._connect() as conn:
            init_schema(conn)

    def _connect(self):
        return connect(self.db_path)

    def upsert_card(self, card: burondt.ItsCard) -> None:
        """Записывает карточку в its_documents + its_files.

        INSERT...ON CONFLICT DO UPDATE — обновляет существующую запись, никогда не
        удаляет строки (даже если файл карточки на burondt.ru пропал между обходами).
        """
        now = utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT first_seen_at FROM its_documents WHERE url_id = ?",
                (card.url_id,),
            ).fetchone()
            first_seen = existing["first_seen_at"] if existing else now

            conn.execute(
                """
                INSERT INTO its_documents (
                    url_id, designation, designation_norm, title, raw_card_html,
                    first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(url_id) DO UPDATE SET
                    designation = excluded.designation,
                    designation_norm = excluded.designation_norm,
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """,
                (
                    card.url_id,
                    card.designation,
                    _designation_norm(card.designation),
                    card.title,
                    first_seen,
                    now,
                ),
            )

            for f in card.files:
                order_number, order_date = burondt.extract_order_meta(f.caption)
                conn.execute(
                    """
                    INSERT INTO its_files (
                        file_id, url_id, role, caption, order_number_caption,
                        order_date_caption, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        url_id = excluded.url_id,
                        role = excluded.role,
                        caption = excluded.caption,
                        order_number_caption = excluded.order_number_caption,
                        order_date_caption = excluded.order_date_caption,
                        updated_at = excluded.updated_at
                    """,
                    (
                        f.file_id,
                        card.url_id,
                        f.role,
                        f.caption,
                        order_number if f.role == "order" else None,
                        order_date if f.role == "order" else None,
                        now,
                    ),
                )
            conn.commit()

    def _get_crawl_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM its_crawl_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def _save_crawl_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO its_crawl_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()

    def refresh(
        self,
        client: httpx.Client,
        *,
        full_rescan: bool = False,
        max_requests: int | None = None,
        confirm_empty_run: int = 40,
        pause_seconds: float = BURONDT_PAGE_PAUSE_SECONDS,
    ) -> dict[str, Any]:
        """Обходит диапазон UrlId burondt.ru, кэширует карточки ИТС.

        Записи с «Обозначением», не начинающимся с ИТС (ГОСТы, протоколы ТРГ и
        т.п. — тот же диапазон UrlId), пропускаются.

        Args:
            full_rescan: True — принудительно с UrlId=10, весь диапазон целиком.
                По умолчанию (False) — инкрементально от (сохранённая верхняя
                граница − _BACK_MARGIN): полный обход — это ~2550 последовательных
                запросов, повторять их все ради нескольких новых карточек
                расточительно и невежливо к чужому серверу.
            max_requests: чанкование первого полного прогона — без этого лимита
                это один непрерывный запуск на ~2550 запросов, который неудобно (и
                рискованно) гонять одной командой; прогресс сохраняется в
                its_crawl_state, следующий вызов продолжает с того же места.
            confirm_empty_run: сколько подряд пустых UrlId подтверждают конец
                диапазона (не просто случайный пропуск), прежде чем обход
                остановится.

        Returns:
            Словарь со счётчиками (scanned, its_found, gaps, non_its_skipped, lo,
            hi) и truncated — True, если обход прервался по max_requests раньше,
            чем нашёл confirm_empty_run пустых UrlId подряд (граница диапазона в
            этом случае не подтверждена, см. range_upper_bound_confirmed).
        """
        if full_rescan:
            lo = _LOWER_BOUND
        else:
            saved = self._get_crawl_state("last_upper_bound")
            lo = max(_LOWER_BOUND, int(saved) - _BACK_MARGIN) if saved else _LOWER_BOUND

        scanned = 0
        its_found = 0
        gaps = 0
        non_its = 0
        empty_run = 0
        last_non_empty = lo - 1
        url_id = lo

        while empty_run < confirm_empty_run:
            if max_requests is not None and scanned >= max_requests:
                self._save_crawl_state("last_upper_bound", str(last_non_empty))
                return {
                    "scanned": scanned,
                    "its_found": its_found,
                    "gaps": gaps,
                    "non_its_skipped": non_its,
                    "lo": lo,
                    "hi": last_non_empty,
                    "truncated": True,
                }

            html = burondt.fetch_card_html(client, url_id)
            if pause_seconds:
                time.sleep(pause_seconds)
            scanned += 1
            card = burondt.parse_card(html, url_id)
            if card is None:
                gaps += 1
                empty_run += 1
            else:
                empty_run = 0
                last_non_empty = url_id
                if burondt.is_its(card):
                    self.upsert_card(card)
                    its_found += 1
                else:
                    non_its += 1
            url_id += 1

        self._save_crawl_state("last_upper_bound", str(last_non_empty))
        # В отличие от last_upper_bound (просто «докуда дошли», пишется и при
        # усечении max_requests), confirmed_upper_bound пишется только здесь — это
        # значит, что long confirm_empty_run реально сработал и граница подтверждена,
        # а не просто оборвана лимитом запросов. Только этому значению можно доверять
        # как знаменателю для процента прогресса (см. webapp.py, /api/health).
        self._save_crawl_state("confirmed_upper_bound", str(last_non_empty))
        return {
            "scanned": scanned,
            "its_found": its_found,
            "gaps": gaps,
            "non_its_skipped": non_its,
            "lo": lo,
            "hi": last_non_empty,
            "truncated": False,
        }

    def range_lower_bound(self) -> int:
        return _LOWER_BOUND

    def range_upper_bound_confirmed(self) -> int | None:
        """Возвращает верхнюю границу диапазона UrlId, подтверждённую полным обходом.

        Returns:
            UrlId, после которого шёл long confirm_empty_run пустых подряд в прошлом
            полном прогоне refresh(). None — такого прогона ещё не было (тогда
            процент прогресса неизвестен, см. webapp.py).
        """
        saved = self._get_crawl_state("confirmed_upper_bound")
        return int(saved) if saved else None

    def resolve(self, designation: str) -> list[int]:
        """Резолвит обозначение ИТС в список кандидатов-url_id.

        Простой LIKE-поиск подстроки здесь не годится: «итс53» как подстрока
        совпал бы и с «итс530-2019», и с «итс531-2018» — другими номерами.

        Фолбэк на подпись приказа: поле «Обозначение» карточки на burondt.ru не
        всегда содержит год (см. docs/stage1-plan.md, «Поле Обозначение не всегда
        содержит год»), поэтому запрос с годом может не совпасть с самим
        «Обозначением», хотя документ в кэше есть. Полное обозначение с годом почти
        всегда всё равно присутствует в подписи файла приказа («... об утверждении
        ИТС NN-YYYY») — если по «Обозначению» ничего не нашлось, ищем совпадение
        там. Сравнение — через ту же нормализацию, что и designation_norm (в
        Python, не SQL LIKE/LOWER), потому что встроенный LOWER() в SQLite не
        приводит кириллицу к нижнему регистру, и сравнение по нему тихо не
        сработало бы.

        Args:
            designation: обозначение или фраза с ним (см. extract_its_designation),
                год («2021») или «голый» номер («ИТС 53») — см. Returns.

        Returns:
            Список url_id — по образцу AuthorityCache.resolve: 0 (не найдено), 1
            (точное совпадение) или много. Если designation — это просто год
            (4 цифры, без букв), это отдельный вид запроса — «покажи все версии
            ИТС за такой-то год», не поиск конкретного документа (см.
            resolve_by_year). Если designation — «ИТС <номер>» без года (например
            «ИТС 53»), это тоже не поиск одного документа, а «покажи все версии
            этого номера по годам» (см. resolve_by_number).
        """
        if not designation or not designation.strip():
            return []
        # Сначала вытащить «ИТС NN-YYYY» из полной строки с названием темы.
        designation = extract_its_designation(designation)
        needle = _designation_norm(designation)
        if needle.isdigit() and len(needle) == 4:
            return self.resolve_by_year(needle)
        bare_number = _bare_number_query(needle)
        if bare_number is not None:
            return self.resolve_by_number(bare_number)
        with self._connect() as conn:
            exact = conn.execute(
                "SELECT url_id FROM its_documents WHERE designation_norm = ?",
                (needle,),
            ).fetchall()
            if exact:
                return self._prefer_pdf_variants([r["url_id"] for r in exact])
            partial = conn.execute(
                "SELECT url_id FROM its_documents WHERE designation_norm LIKE ? ESCAPE '\\'",
                (_like_pattern(needle),),
            ).fetchall()
            if partial:
                return self._prefer_pdf_variants([r["url_id"] for r in partial])

            rows = conn.execute(
                "SELECT DISTINCT url_id, caption FROM its_files WHERE role = 'order' AND caption IS NOT NULL"
            ).fetchall()
            via_order = {r["url_id"] for r in rows if needle in _designation_norm(r["caption"])}
            return self._prefer_pdf_variants(sorted(via_order))

    def _prefer_pdf_variants(self, url_ids: list[int]) -> list[int]:
        """Убирает «в формате Word», если есть обычная PDF-карточка."""
        if len(url_ids) <= 1:
            return url_ids
        with self._connect() as conn:
            placeholders = ",".join("?" * len(url_ids))
            rows = conn.execute(
                f"SELECT url_id, designation FROM its_documents WHERE url_id IN ({placeholders})",
                url_ids,
            ).fetchall()
        by_id = {r["url_id"]: r["designation"] for r in rows}
        non_word = [u for u in url_ids if not _is_word_variant(by_id.get(u))]
        return non_word if non_word else list(url_ids)

    def resolve_by_year(self, year: str) -> list[int]:
        """Возвращает все url_id ИТС за указанный год.

        Год ищем в двух местах, т.к. «Обозначение» не всегда его содержит (см.
        resolve): как суффикс «-YYYY» в designation_norm и как подстроку в дате
        приказа (order_date_caption) — эта дата у карточки есть независимо от
        формата «Обозначения». Подстрочный поиск 4-значного года в дате достаточно
        надёжен — случайное совпадение с другим числом в этом коротком текстовом
        поле маловероятно.

        Args:
            year: год как строка из 4 цифр; любой другой формат даёт пустой список.

        Returns:
            Список url_id — может быть несколько, если за этот год выходило
            несколько версий ИТС (разных номеров).
        """
        if not (year.isdigit() and len(year) == 4):
            return []
        with self._connect() as conn:
            by_designation = conn.execute(
                "SELECT url_id FROM its_documents WHERE designation_norm LIKE ?",
                (f"%-{year}",),
            ).fetchall()
            by_order_date = conn.execute(
                "SELECT DISTINCT url_id FROM its_files"
                " WHERE role = 'order' AND order_date_caption LIKE ?",
                (f"%{year}%",),
            ).fetchall()
            ids = {r["url_id"] for r in by_designation} | {r["url_id"] for r in by_order_date}
            return self._prefer_pdf_variants(sorted(ids))

    def resolve_by_number(self, number: str) -> list[int]:
        """Возвращает все версии ИТС с указанным базовым номером (без года).

        Номер сравнивается точно (после извлечения _base_number), а не подстрокой —
        подстрокой «53» совпал бы и с «530», и с «531».

        Args:
            number: базовый номер ИТС без года, например «53».

        Returns:
            url_id всех версий, новые версии — первыми. Год каждой версии для
            сортировки берётся тем же способом, что и в resolve/resolve_by_year (из
            «Обозначения» или, если там года нет, из даты приказа); версии с
            неизвестным годом уходят в конец списка, а не мешаются между известными.
        """
        with self._connect() as conn:
            docs = conn.execute(
                "SELECT url_id, designation, designation_norm FROM its_documents"
            ).fetchall()
            matches = [r for r in docs if _base_number(r["designation_norm"]) == number]
            if not matches:
                return []

            dated: list[tuple[int, str | None]] = []
            for row in matches:
                if _is_word_variant(row["designation"]):
                    continue
                order_dates = [
                    f["order_date_caption"]
                    for f in conn.execute(
                        "SELECT order_date_caption FROM its_files WHERE url_id = ? AND role = 'order'",
                        (row["url_id"],),
                    ).fetchall()
                ]
                year = _extract_year(row["designation_norm"], order_dates)
                dated.append((row["url_id"], year))
            # если отфильтровали все Word и ничего не осталось — вернём Word как есть
            if not dated:
                for row in matches:
                    order_dates = [
                        f["order_date_caption"]
                        for f in conn.execute(
                            "SELECT order_date_caption FROM its_files WHERE url_id = ? AND role = 'order'",
                            (row["url_id"],),
                        ).fetchall()
                    ]
                    year = _extract_year(row["designation_norm"], order_dates)
                    dated.append((row["url_id"], year))

        def sort_key(item: tuple[int, str | None]) -> tuple[int, int, int]:
            url_id, year = item
            if year is None:
                return (1, 0, url_id)
            return (0, -int(year), url_id)

        dated.sort(key=sort_key)
        return [url_id for url_id, _year in dated]

    def get_card(self, url_id: int) -> dict[str, Any] | None:
        """Возвращает карточку по url_id, либо None, если она не закэширована."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM its_documents WHERE url_id = ?", (url_id,)
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def find_related_url_ids(self, url_id: int) -> list[int]:
        """Тот же ИТС в другом формате (PDF ↔ Word) — другие url_id с тем же ключом."""
        card = self.get_card(url_id)
        if card is None:
            return []
        key = _canonical_its_key(card["designation"])
        if not key:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT url_id, designation FROM its_documents"
            ).fetchall()
        related = [
            r["url_id"]
            for r in rows
            if r["url_id"] != url_id and _canonical_its_key(r["designation"]) == key
        ]
        return sorted(related)

    def list_files(self, url_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM its_files WHERE url_id = ? ORDER BY file_id",
                (url_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_file_record(self, file_id: int) -> dict[str, Any] | None:
        """Возвращает запись о скачанном файле (its_document_files), либо None,
        если этот file_id ещё не скачивался."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM its_document_files WHERE file_id = ?", (file_id,)
            ).fetchone()
            return dict(row) if row else None

    def record_file(
        self,
        *,
        file_id: int,
        file_path: str,
        sha256: str,
        size_bytes: int,
        pages: int | None,
    ) -> None:
        """Регистрирует скачанный файл в its_document_files.

        Как DocumentStore.record_file — тот же паттерн.

        Raises:
            ValueError: file_id не существует в its_files (FK) — значит, карточка
                с этим файлом никогда не проходила upsert_card.
        """
        now = utc_now_iso()
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM its_files WHERE file_id = ?", (file_id,)
            ).fetchone()
            if not exists:
                raise ValueError(f"Файл {file_id} не найден в its_files — сначала upsert_card")
            conn.execute(
                """
                INSERT INTO its_document_files (
                    file_id, file_path, sha256, size_bytes, pages, downloaded_at, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    file_path = excluded.file_path,
                    sha256 = excluded.sha256,
                    size_bytes = excluded.size_bytes,
                    pages = excluded.pages,
                    downloaded_at = excluded.downloaded_at,
                    verified_at = excluded.verified_at
                """,
                (file_id, file_path, sha256, size_bytes, pages, now, now),
            )
            conn.commit()

    def count_its_documents(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM its_documents").fetchone()[0])

    def count_its_files(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM its_files").fetchone()[0])
