"""Локальный кэш органов (SQLite) и резолвинг названия → GUID."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx

from npa_pipeline import api as pravo_api
from npa_pipeline.db import DEFAULT_DB_PATH, connect, init_schema, resolve_db_path
from npa_pipeline.normalize import normalize_name


def _federal_sort_weight(name_norm: str, weight: int | None) -> int:
    """Ключ сортировки: выше = приоритетнее в автодополнении.

    Предпочитаем weight с портала (у федеральных органов он заметно выше).
    Если weight ещё не загружен в кэш — эвристика по окончанию названия.
    """
    if weight is not None and weight > 0:
        return int(weight)

    n = name_norm
    if n.endswith("российской федерации") or n == "российская федерация":
        if (
            "представительство" in n
            or " при президенте " in n
            or " при правительстве " in n
        ):
            return 1_000
        return 85_000
    return 0


class AuthorityCache:
    """Кэш органов в единой SQLite-базе проекта.

    Args:
        db_path: Путь к базе.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = resolve_db_path(db_path)
        with self._connect() as conn:
            init_schema(conn)

    def _connect(self):
        """Соединение с базой."""
        return connect(self.db_path)

    def build(self, client: httpx.Client) -> int:
        """Загружает /api/SignatoryAuthorities и upsert'ит записи в кэш.

        Строки не удаляет — только добавляет/обновляет, так что орган, пропавший
        из нового ответа портала, остаётся резолвимым по старым данным.

        Returns:
            Текущее общее число органов в кэше после upsert'а (не количество
            изменённых за этот вызов строк).
        """
        rows = pravo_api.list_signatory_authorities(client)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self._connect() as conn:
            for row in rows:
                guid = row["id"]
                name = row.get("name") or ""
                weight = row.get("weight")
                conn.execute(
                    """
                    INSERT INTO authorities (guid, name, name_norm, block, category, updated_at, weight)
                    VALUES (?, ?, ?, NULL, NULL, ?, ?)
                    ON CONFLICT(guid) DO UPDATE SET
                        name = excluded.name,
                        name_norm = excluded.name_norm,
                        updated_at = excluded.updated_at,
                        weight = excluded.weight
                    """,
                    (guid, name, normalize_name(name), now, weight),
                )
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM authorities").fetchone()[0]
        return int(count)

    def count(self) -> int:
        """Число органов в кэше."""
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM authorities").fetchone()[0])

    def resolve(
        self,
        *,
        authority_guid: str | None = None,
        authority_name: str | None = None,
    ) -> list[str]:
        """Резолвит орган в набор кандидатов-GUID.

        Args:
            authority_guid: если задан, используется как есть, без обращения к
                БД — authority_name в этом случае игнорируется.
            authority_name: название органа для поиска по кэшу (нормализуется
                через normalize_name). Пустая строка или None — пустой результат.

        Returns:
            Список GUID-кандидатов: пустой — орган не найден; из одного элемента
            — точное совпадение по нормализованному названию; из нескольких —
            точных совпадений не было, вернулись все частичные (LIKE) совпадения.
        """
        if authority_guid:
            return [authority_guid]

        if not authority_name or not authority_name.strip():
            return []

        needle = normalize_name(authority_name)
        with self._connect() as conn:
            exact = conn.execute(
                "SELECT guid FROM authorities WHERE name_norm = ?",
                (needle,),
            ).fetchall()
            if exact:
                return [r["guid"] for r in exact]

            like = f"%{needle}%"
            partial = conn.execute(
                "SELECT guid FROM authorities WHERE name_norm LIKE ?",
                (like,),
            ).fetchall()
            return [r["guid"] for r in partial]

    def suggest(self, query: str, *, limit: int = 15) -> list[dict[str, str]]:
        """Автодополнение по названию органа: федеральные выше региональных,
        затем совпадение-префикс, затем короче название.

        Returns:
            До limit записей вида {"guid": ..., "name": ...}, в порядке
            ранжирования выше. Пустой список для пустого query или limit < 1.
        """
        needle = normalize_name(query)
        if not needle or limit < 1:
            return []

        fetch_limit = max(limit * 8, 80)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT guid, name, name_norm, weight FROM authorities
                WHERE name_norm LIKE ?
                LIMIT ?
                """,
                (f"%{needle}%", fetch_limit),
            ).fetchall()

        ranked = sorted(
            rows,
            key=lambda r: (
                -_federal_sort_weight(r["name_norm"], r["weight"]),
                0 if r["name_norm"].startswith(needle) else 1,
                len(r["name_norm"]),
                r["name"],
            ),
        )
        return [{"guid": r["guid"], "name": r["name"]} for r in ranked[:limit]]
