"""Тонкий CLI поверх библиотеки."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from npa_pipeline import burondt
from npa_pipeline.authorities import AuthorityCache
from npa_pipeline.db import DEFAULT_DB_PATH, DocumentStore
from npa_pipeline.http_client import create_client
from npa_pipeline.import_json import import_json_sidecars
from npa_pipeline.its import ItsCache
from npa_pipeline.its_download import fetch_its
from npa_pipeline.models import Query, parse_user_date
from npa_pipeline.parse_citation import parse_citation
from npa_pipeline.service import fetch_document


def _query_from_args(args: argparse.Namespace) -> Query:
    """Собирает Query из аргументов `fetch`: --title, затем --json, затем отдельные флаги."""
    if getattr(args, "title", None):
        return parse_citation(args.title).to_query()
    if args.json:
        data = json.loads(Path(args.json).read_text(encoding="utf-8"))
        if data.get("title") or data.get("citation"):
            return parse_citation(data.get("title") or data["citation"]).to_query()
        date_val = data.get("date")
        return Query(
            eo_number=data.get("eo_number") or data.get("eoNumber"),
            authority_guid=data.get("authority_guid") or data.get("authorityGuid"),
            authority_name=data.get("authority_name") or data.get("authorityName"),
            number=data.get("number"),
            date=parse_user_date(date_val) if date_val else None,
            doc_type_id=data.get("doc_type_id") or data.get("docTypeId"),
        )
    return Query(
        eo_number=args.eo,
        authority_guid=args.authority_guid,
        authority_name=args.authority,
        number=args.number,
        date=parse_user_date(args.date) if args.date else None,
        doc_type_id=args.doc_type_id,
    )


def cmd_fetch(args: argparse.Namespace) -> int:
    """Обработчик `npa fetch` — печатает JSON результата, код возврата 0/1 по статусу."""
    try:
        query = _query_from_args(args)
    except ValueError as exc:
        print(json.dumps({"status": "invalid_input", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    result = fetch_document(
        query,
        cache_path=args.db,
        out_dir=args.out_dir,
        download=not args.no_download,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    # found_text_only — успех через резерв ips.pravo.gov.ru (нет PDF, но есть
    # текст, см. npa_pipeline.ips_pravo), не ошибка наравне с not_found/ambiguous
    return 0 if result.status.value in ("found", "found_text_only") else 1


def cmd_refresh_authorities(args: argparse.Namespace) -> int:
    """Обработчик `npa refresh-authorities` — полный обход справочника органов."""
    cache = AuthorityCache(args.db)
    with create_client() as client:
        count = cache.build(client)
    print(json.dumps({"authorities": count, "db": str(Path(args.db))}, ensure_ascii=False))
    return 0


def cmd_refresh_its(args: argparse.Namespace) -> int:
    """Обработчик `npa refresh-its` — обновляет кэш карточек ИТС/НДТ с burondt.ru."""
    cache = ItsCache(args.db)
    with burondt.create_burondt_client() as client:
        stats = cache.refresh(
            client,
            full_rescan=args.full_rescan,
            max_requests=args.max_requests,
            confirm_empty_run=args.confirm_empty_run,
        )
    print(json.dumps({"db": str(Path(args.db)), **stats}, ensure_ascii=False, indent=2))
    return 0


def cmd_fetch_its(args: argparse.Namespace) -> int:
    """Обработчик `npa fetch-its` — печатает JSON результата, код возврата 0/1 по статусу."""
    cache = ItsCache(args.db)
    with burondt.create_burondt_client() as client:
        result = fetch_its(
            client,
            designation=args.designation,
            out_dir=Path(args.out_dir),
            cache=cache,
            download=not args.no_download,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status.value == "found" else 1



def cmd_import_json(args: argparse.Namespace) -> int:
    """Обработчик `npa import-json` — переносит старые JSON-sidecar из downloads/ в БД."""
    store = DocumentStore(args.db)
    stats = import_json_sidecars(
        args.out_dir,
        store,
        delete_json=args.delete_json,
    )
    print(json.dumps({"db": str(Path(args.db)), **stats}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Собирает argparse-парсер со всеми подкомандами `npa`."""
    parser = argparse.ArgumentParser(prog="npa", description="Поиск и скачивание НПА")
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Путь к единой SQLite-базе (органы + документы)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="Найти и скачать документ")
    fetch.add_argument(
        "--title",
        help=(
            "Полное официальное название (complexName), например: "
            "«Федеральный закон от 21.07.2014 № 219-ФЗ \"О внесении…\"» — "
            "орган/номер/дата извлекаются автоматически"
        ),
    )
    fetch.add_argument("--eo", help="Известный eoNumber")
    fetch.add_argument("--authority", help="Название органа")
    fetch.add_argument("--authority-guid", help="GUID органа")
    fetch.add_argument("--number", help="Номер документа")
    fetch.add_argument("--date", help="Дата подписания YYYY-MM-DD или DD.MM.YYYY")
    fetch.add_argument("--doc-type-id", help="GUID типа документа (опционально)")
    fetch.add_argument("--json", help="Путь к JSON с полями запроса")
    fetch.add_argument("--out-dir", default="downloads", help="Каталог для PDF")
    fetch.add_argument(
        "--no-download",
        action="store_true",
        help="Только поиск, без скачивания PDF",
    )
    fetch.set_defaults(func=cmd_fetch)

    refresh = sub.add_parser("refresh-authorities", help="Обновить справочник органов")
    refresh.set_defaults(func=cmd_refresh_authorities)

    refresh_its = sub.add_parser("refresh-its", help="Обновить кэш карточек ИТС/НДТ с burondt.ru")
    refresh_its.add_argument(
        "--full-rescan",
        action="store_true",
        help="Полный обход с UrlId=10, а не только новые id",
    )
    refresh_its.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Ограничить число запросов за запуск (для чанкования)",
    )
    refresh_its.add_argument(
        "--confirm-empty-run",
        type=int,
        default=40,
        help="Сколько пустых id подряд считать концом диапазона",
    )
    refresh_its.set_defaults(func=cmd_refresh_its)

    fetch_its_cmd = sub.add_parser(
        "fetch-its",
        help="Скачать файлы ИТС по обозначению, году или номеру без года (ИТС 28-2021 / 2021 / ИТС 53)",
    )
    fetch_its_cmd.add_argument(
        "designation",
        help=(
            "Обозначение (ИТС 28-2021), просто год (2021) или номер без года (ИТС 53) — "
            "в двух последних случаях вернутся все версии, новые сначала"
        ),
    )
    fetch_its_cmd.add_argument("--out-dir", default="downloads/its", help="Каталог для PDF")
    fetch_its_cmd.add_argument(
        "--no-download",
        action="store_true",
        help="Только резолвинг в кэше, без скачивания",
    )
    fetch_its_cmd.set_defaults(func=cmd_fetch_its)

    # Подкоманда `ocr` здесь отсутствует — OCR живёт в отдельном проекте npa-ocr
    # (https://github.com/EduardIbatullin/npa-ocr, команда `npa-ocr <pdf> [опции]`,
    # см. docs/ocr-integration.md). Веб-UI запускает её как внешний процесс, см.
    # ocr_jobs.py.

    imp = sub.add_parser(
        "import-json",
        help="Импортировать старые JSON-sidecar из downloads/ в БД",
    )
    imp.add_argument("--out-dir", default="downloads", help="Каталог с PDF и JSON")
    imp.add_argument(
        "--delete-json",
        action="store_true",
        help="Удалить JSON после успешного импорта",
    )
    imp.set_defaults(func=cmd_import_json)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Точка входа CLI: парсит аргументы и завершается с кодом обработчика подкоманды."""
    parser = build_parser()
    args = parser.parse_args(argv)
    code = args.func(args)
    sys.exit(code)


if __name__ == "__main__":
    main()
