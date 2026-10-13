# npa-pipeline

Библиотека и CLI для **поиска и скачивания** официальных документов РФ:

| Источник | Что качаем | Внешний сайт |
|---|---|---|
| **НПА** | PDF официальной публикации | `publication.pravo.gov.ru` |
| **ИТС/НДТ** | справочник (PDF, иногда Word) + утверждающий приказ | `burondt.ru` |

Без ИИ в рантайме. Основной продукт — **Python API** (и тонкий CLI поверх него). Веб-интерфейс — только для ручной проверки API.

Подробные планы и эмпирика по источникам: [`docs/stage1-plan.md`](docs/stage1-plan.md), [`docs/api-reference.md`](docs/api-reference.md).

---

## Что реализовано (этап 1)

- Поиск НПА по реквизитам (орган + номер + дата) или по `eoNumber`
- Разбор полного официального названия (`complexName`) → реквизиты → тот же поиск
- Скачивание PDF, проверка целостности (`%PDF`, `%%EOF`, число страниц), учёт sha256
- Локальная SQLite `data/npa.db`: органы, карточки документов, файлы
- ИТС/НДТ: кэш карточек с burondt.ru, поиск по обозначению / году / номеру, скачивание файлов (PDF + Word-пара, если есть)
- CLI `npa`, опциональный тестовый UI `npa-web` / Docker

**Ещё не начато:** история изменений и статус документа (этап 2), приёмка в нормативную базу. См. [`docs/stage2-plan.md`](docs/stage2-plan.md). OCR сканов реализован, но вынесен в отдельный проект — [npa-ocr](https://github.com/EduardIbatullin/npa-ocr); здесь он подключается из веб-UI как внешний инструмент (см. «Веб-интерфейс» ниже).

---

## Установка

Требуется Python ≥ 3.11.

```bash
# ядро + тесты
pip install -e ".[dev]"

# + зависимости тестового веб-UI (FastAPI / uvicorn)
pip install -e ".[web]"
```

Перед первым поиском НПА один раз загрузите справочник органов (~4000 записей):

```bash
npa refresh-authorities
```

Для ИТС/НДТ один раз (или периодически) заполните кэш карточек — без него `fetch-its` ищет только уже известные обозначения:

```bash
npa refresh-its --full-rescan          # полный обход, долго
# или чанками:
npa refresh-its --max-requests 300
```

Данные по умолчанию:

| Путь | Содержимое |
|---|---|
| `data/npa.db` | органы, НПА, ИТС, пути и хеши файлов |
| `downloads/` | PDF НПА |
| `downloads/its/` | файлы ИТС/НДТ |

---

## Python API (основной интерфейс)

### НПА — `fetch_document`

```python
from datetime import date
from npa_pipeline import Query, Status, fetch_document, query_from_citation

# 1) По реквизитам
result = fetch_document(
    Query(
        authority_name="Президент Российской Федерации",
        number="310-ФЗ",
        date=date(2025, 7, 31),
    ),
    out_dir="downloads",
    download=True,  # False — только поиск, без PDF
)

# 2) По полному названию (как на портале / в консультанте)
result = fetch_document(
    query_from_citation(
        'Федеральный закон от 21.07.2014 № 219-ФЗ\n'
        '"О внесении изменений в Федеральный закон '
        '"Об охране окружающей среды"…"'
    )
)

# 3) Если уже известен eoNumber портала
result = fetch_document(Query(eo_number="0001202507310060"))

print(result.status)          # Status.FOUND, …
print(result.eo_number)
print(result.pdf_path)        # путь к PDF при download=True — скачивается в любом случае
print(result.ebpi_docx_path)  # текст из EBPI (см. ниже) в .docx рядом с PDF, если документ там есть
print(result.ebpi_docstate)   # напр. «Действует без изменений» / «Действует с изменениями»
print(result.text_layer_docx_path)  # запасной .docx, если EBPI не покрыл, но PDF не скан (см. ниже)
print(result.to_dict())       # JSON-совместимый dict
```

Разбор названия отдельно (без сети):

```python
from npa_pipeline import parse_citation

parsed = parse_citation(
    "Приказ Минприроды России от 07.04.2026 N 191"
)
# parsed.authority_name, parsed.number, parsed.date
query = parsed.to_query()
```

Поддерживаются в том числе месяц прописью и хвост «(в ред. …)» — основные реквизиты берутся из исходного акта, не из редакции.

#### Контракт `Result` (НПА)

| `status` | Смысл |
|---|---|
| `found` | Один документ; при `download=True` PDF на диске |
| `not_found` | Среди кандидатов органа ничего нет (см. `diagnostics`) |
| `ambiguous` | Несколько совпадений — в `candidates`, автовыбор не делается |
| `out_of_range` | Дата раньше охвата портала (~ноябрь 2011), после неудачного поиска |
| `invalid_input` | Нет номера/даты/органа или название не разобралось |
| `network_error` | Сеть / HTTP после retry |
| `integrity_error` | Файл не прошёл проверку PDF |

`match_type`: `exact` (точное совпадение номера) или `digits_only` (запасной поиск по цифровой части).

После успешного скачивания PDF `fetch_document()` пробует найти документ в EBPI
(`actual.pravo.gov.ru`) и при успехе кладёт действующую редакцию текста (с учётом
поправок) в `.docx` рядом с PDF — см. поля `ebpi_docx_path`/`ebpi_docstate` выше.
Покрыты только законы, указы, постановления/распоряжения Правительства, акты
высших судов и Банка России — ведомственные приказы нет; `ebpi_docx_path` тогда
остаётся `None`, это не ошибка. Результат кэшируется в БД (включая отрицательный
— «не покрыт»), повторный `fetch_document()` на том же документе не ходит в сеть
за EBPI заново. Для изменённого документа PDF (исходная публикация) и `.docx`
(действующая редакция) — не одно и то же: имя `.docx` тогда получает суффикс
«(ред. от DD.MM.YYYY)», а акт-поправка записывается в `document_relations`
(`relation_type='amends'`). Подробности — [`docs/ebpi-coverage.md`](docs/ebpi-coverage.md).
Отключить: `fetch_document(..., fetch_ebpi_text=False)`.

Если EBPI документ не покрыл (обычно приказ), но его PDF не скан — текстовый слой
есть на всех страницах — `fetch_document()` сам извлекает текст механически
(`pypdf`, без OCR и без ИИ) в `.docx` того же имени и кладёт путь в
`result.text_layer_docx_path`; `ebpi_docx_path` при этом остаётся `None`. Для
настоящих сканов (хоть одна страница без текстового слоя) это поле тоже `None` —
там нужен OCR (см. ниже). Отключить: `fetch_document(..., fetch_text_layer_fallback=False)`.

Алгоритм поиска (резолвинг органа, фильтр по дате, коллизии номеров, совместные приказы) — в [`docs/stage1-plan.md`](docs/stage1-plan.md). Поведение API портала — в [`docs/api-reference.md`](docs/api-reference.md).

### ИТС/НДТ — `fetch_its`

Отдельный источник и отдельные таблицы БД (у ИТС нет `eoNumber`).

```python
from npa_pipeline import burondt
from npa_pipeline.its import ItsCache
from npa_pipeline.its_download import fetch_its, fetch_its_by_url_id

cache = ItsCache("data/npa.db")

with burondt.create_burondt_client() as client:
    # обозначение; допускается строка с темой: «ИТС 51-2025 Литейное…»
    result = fetch_its(
        client,
        designation="ИТС 28-2021",
        out_dir="downloads/its",
        cache=cache,
        download=True,
    )

    # или точный url_id после ambiguous
    result = fetch_its_by_url_id(
        client,
        url_id=1640,
        out_dir="downloads/its",
        cache=cache,
    )

print(result.status, result.designation, result.url_id)
for f in result.files:
    print(f.role, f.file_id, f.pdf_path)
    print(f.text_layer_docx_path)  # .docx, если этот PDF не скан (см. ниже)
    # role: document | document_word | order | unknown
```

Поиск по кэшу:

- `ИТС 28-2021` — одна карточка (если есть)
- `2021` — все ИТС за год
- `ИТС 53` — все версии номера, новые сначала → часто `ambiguous`

Если на burondt.ru есть парная карточка «в формате Word», при скачивании PDF-версии Word подтягивается рядом (`document_word`). Word есть не у всех ИТС.

У ИТС нет `eoNumber`, поэтому EBPI для них в принципе не применим — но справочники
(в отличие от приказов на publication.pravo.gov.ru, которые почти всегда сканы)
обычно не скан, с полным текстовым слоем. Поэтому для каждого скачанного PDF
`fetch_its()`/`fetch_its_by_url_id()` автоматически пробует механически извлечь
текст (без OCR, см. `text_layer_docx_path` выше) — тот же `text_layer_docx.py`, что
у НПА (см. раздел «НПА» выше). Для сканов (например, у самого приказа об
утверждении ИТС — его подпись и печать почти всегда отсканированы) поле остаётся
`None`, это не ошибка.

Обновление кэша:

```python
with burondt.create_burondt_client() as client:
    stats = cache.refresh(client, full_rescan=True)  # или max_requests=300
```

---

## CLI

Тонкая обёртка над тем же API. Печатает JSON результата в stdout.

```bash
# --- НПА ---
npa refresh-authorities

npa fetch --authority "Президент Российской Федерации" --number 310-ФЗ --date 2025-07-31
npa fetch --eo 0001202507310060
npa fetch --title 'Федеральный закон от 21.07.2014 № 219-ФЗ "О внесении…"'
npa fetch --authority "..." --number 402 --date 2016-07-19 --no-download

npa import-json --out-dir downloads --delete-json   # старые JSON-sidecar → БД

# --- ИТС/НДТ ---
npa refresh-its --full-rescan
npa refresh-its --max-requests 300

npa fetch-its "ИТС 28-2021"
npa fetch-its 2021
npa fetch-its "ИТС 53"
```

Общие флаги: `--db data/npa.db`, у `fetch` / `fetch-its` — `--out-dir`, `--no-download`.

Имена PDF НПА при сохранении сокращают органы (`Минприроды России`, `Правительства РФ`, …).

OCR сканов (этап 3) здесь больше нет — он выделен в отдельный проект
[npa-ocr](https://github.com/EduardIbatullin/npa-ocr), команда `npa-ocr <pdf> ...`.
Этот проект дёргает его только из веб-UI (см. ниже), как внешний процесс.

---

## Веб-интерфейс (только для проверки API)

Не продуктовый UI. Одна форма: наименование НПА или обозначение ИТС → поиск через те же функции библиотеки → описание и кнопки «Открыть» / «Скачать».

```bash
pip install -e ".[web]"
npa-web
# http://127.0.0.1:8765
```

Или Docker (порт 8765, тома `data/` и `downloads/`):

```bash
docker compose up --build -d
# http://127.0.0.1:8765
```

HTTP-эндпоинты веб-слоя (удобны для отладки, не заменяют библиотечный API):

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/health` | счётчики кэша / БД |
| `POST` | `/api/lookup` | единый поиск: `{ "q": "…" }` или `{ "url_id": N }` для ИТС |
| `POST` | `/api/fetch` | НПА (поля реквизитов или `title`) |
| `POST` | `/api/parse-title` | только разбор названия НПА |
| `POST` | `/api/fetch-its` | ИТС по `designation` / `url_id` |
| `GET` | `/api/pdf/{eoNumber}` | PDF НПА (`?download=1` — attachment) |
| `GET` | `/api/its/pdf/{file_id}` | файл ИТС |
| `POST` | `/api/refresh-authorities` | обновить кэш органов |
| `POST` | `/api/refresh-its` | чанк обновления кэша ИТС |
| `POST` | `/api/ocr` | запуск распознавания скана НПА: `{ "eo_number": "…", "authority_name": "…" }` (одно задание за раз, `409` если занято) |
| `GET` | `/api/ocr/jobs/{job_id}` | статус задания: страница, процент, шаги, время |
| `POST` | `/api/ocr/jobs/{job_id}/cancel` | остановить задание (сохранённые страницы остаются) |
| `GET` | `/api/ocr/jobs/{job_id}/docx` | готовый DOCX (`404`, пока не завершено) |

В карточке НПА, у которого есть страницы без текстового слоя, появляется кнопка «Распознать скан». Она запускает внешнюю команду `npa-ocr` отдельным процессом (проект [npa-ocr](https://github.com/EduardIbatullin/npa-ocr) — должен быть установлен отдельно, `pip install npa-ocr[models]`, команда на PATH), прогресс обновляется каждые 5 секунд. Результат — `downloads/ocr/<stem>.docx`. Без установленного `npa-ocr` задание сразу завершается с ошибкой. Подробности границы между проектами — [`docs/ocr-integration.md`](docs/ocr-integration.md).

Кэш ИТС в UI не обновляется полной кнопкой «на десятки минут» — для полного обхода используйте CLI `npa refresh-its`.

---

## Хранение

Одна SQLite `data/npa.db`, **разные таблицы** для НПА и ИТС (намеренно: разная идентификация и структура карточек).

**НПА:** `authorities`, `documents`, `document_signatories`, `document_files`, …

**ИТС:** `its_documents`, `its_files`, `its_document_files`, `its_crawl_state`

Повторное скачивание того же файла не делается, если путь на диске есть и sha256 совпадает.

---

## Тесты

```bash
pytest                 # юнит-тесты, без сети
pytest -m live         # эталонный набор против живого publication.pravo.gov.ru
```

Эталонные кейсы: [`tests/golden/golden_set.json`](tests/golden/golden_set.json).

---

## Ограничения и оговорки

- Портал НПА: поиск по API; скачивание PDF идёт через `/file/pdf` — в `robots.txt` есть `Disallow: /File` (см. обсуждение в stage2).
- `ComplexName` на стороне портала **не** является полнотекстовым поиском; «по названию» у нас = парсер реквизитов из строки, не произвольная фраза вроде «закон об охране окружающей среды».
- ИТС ищутся только среди уже просканированных карточек локального кэша.
- Охват НПА на портале roughly с ноября 2011; раньше → `out_of_range` после неудачного поиска.
- OCR в веб-интерфейсе: одно задание за раз, список заданий хранится в памяти процесса веб-сервера (после перезапуска сервера задание надо запустить заново, но уже сохранённые страницы продолжатся). Для самого распознавания — тайминги, пределы по таблицам/печатям, требования к памяти — см. README и docs/plan.md в [npa-ocr](https://github.com/EduardIbatullin/npa-ocr).
- EBPI (`actual.pravo.gov.ru`) нигде официально не документирован — найден и используется через реверс-инжиниринг минифицированного JS сайта; покрытие по видам актов проверено на 23 документах, не исчерпывающе. Подробности — [`docs/ebpi-coverage.md`](docs/ebpi-coverage.md).
- Клиент ходит на pravo по `http://` (в части окружений `https` зависает на TCP); целостность файла проверяется локально.

---

## Структура репозитория

```
src/npa_pipeline/     # библиотека
  service.py          # fetch_document
  search.py           # алгоритм поиска НПА
  parse_citation.py   # название → Query
  its.py / its_download.py / burondt.py
  ebpi.py             # клиент к actual.pravo.gov.ru — текст действующей редакции
  ebpi_docx.py        # HTML EBPI -> .docx
  text_layer_docx.py  # запасной путь: текстовый слой PDF -> .docx без OCR
  webapp.py + static/ # тестовый UI
  cli.py
  ocr_jobs.py         # фоновое задание веб-UI: запускает npa-ocr отдельным процессом
docs/                 # планы и справочники источников
tests/
Dockerfile            # образ тестового UI
docker-compose.yml
```
