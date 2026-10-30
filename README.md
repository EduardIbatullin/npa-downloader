# npa-downloader

Поиск и скачивание официальных документов РФ с получением их текста в DOCX:

| Источник | Что скачивается | Сайт |
|---|---|---|
| НПА | PDF официальной публикации и текст действующей редакции | `publication.pravo.gov.ru`, `actual.pravo.gov.ru`, `ips.pravo.gov.ru` |
| Справочники ИТС НДТ | Справочник (PDF, иногда Word) и приказ об утверждении | `burondt.ru` |

Основной интерфейс — Python-библиотека `npa_pipeline` и командная строка `npa`;
веб-приложение служит для ручной проверки и запуска распознавания сканов.
Поиск работает без ИИ; сканы распознаёт отдельный сервис
[npa-ocr](https://github.com/EduardIbatullin/npa-ocr).

## Возможности

- Поиск НПА по реквизитам (орган, номер, дата), по полному официальному
  названию или по `eoNumber`, с учётом совпадающих номеров, совместных
  документов и неточных названий органов.
- Скачивание PDF с проверкой целостности; учёт файлов и sha256 в SQLite.
- Текст документа в DOCX: действующая редакция из EBPI, иначе текстовый слой
  PDF; для изменённого документа — связь с актами-поправками.
- Документы старше 08.11.2011 (вне охвата портала) — текст из ips.pravo.gov.ru.
- Справочники ИТС: кэш карточек burondt.ru, поиск по обозначению, году или
  номеру, скачивание с Word-версией и текстом в DOCX.
- Веб-приложение: единый поиск, предпросмотр DOCX, распознавание сканов через
  npa-ocr, мониторинг ресурсов сервера.

Подробнее: [алгоритм поиска](docs/search-algorithm.md),
[API портала](docs/api-reference.md), [EBPI](docs/ebpi-coverage.md),
[ips.pravo.gov.ru](docs/ips-pravo-coverage.md),
[интеграция с npa-ocr](docs/ocr-integration.md),
[журнал изменений](CHANGELOG.md).

## Установка

Нужен Python 3.11+.

```bash
pip install -e ".[dev]"    # библиотека и тесты
pip install -e ".[web]"    # веб-приложение (FastAPI, uvicorn)
```

Перед первым поиском загрузите справочник органов (около 4000 записей), а для
справочников ИТС — кэш карточек:

```bash
npa refresh-authorities
npa refresh-its --full-rescan        # полный обход, около 2550 запросов
npa refresh-its --max-requests 300   # или частями
```

Данные по умолчанию:

| Путь | Содержимое |
|---|---|
| `data/npa.db` | Органы, документы, ИТС, сведения о файлах |
| `downloads/` | PDF и DOCX НПА |
| `downloads/its/` | Файлы ИТС |
| `downloads/ocr/` | Результаты распознавания |

## Использование

### Python: НПА

```python
from datetime import date
from npa_pipeline import Query, fetch_document, query_from_citation

# по реквизитам
result = fetch_document(
    Query(authority_name="Президент Российской Федерации", number="310-ФЗ",
          date=date(2025, 7, 31)),
    out_dir="downloads",
)

# по полному названию
result = fetch_document(query_from_citation(
    'Федеральный закон от 21.07.2014 № 219-ФЗ "О внесении изменений …"'))

# по eoNumber
result = fetch_document(Query(eo_number="0001202507310060"))

result.status                # Status.FOUND, …
result.pdf_path              # PDF
result.ebpi_docx_path        # текст действующей редакции из EBPI
result.text_layer_docx_path  # текст из текстового слоя PDF, если EBPI документ не покрывает
result.to_dict()             # JSON-совместимый словарь
```

Параметры `fetch_document()`: `download=False` — только поиск;
`fetch_ebpi_text`, `fetch_text_layer_fallback`, `fetch_ips_fallback` — отключить
соответствующий способ получения текста.

Разбор названия без сети:

```python
from npa_pipeline import parse_citation

parsed = parse_citation("Приказ Минприроды России от 07.04.2026 N 191")
parsed.authority_name, parsed.number, parsed.date
```

| `status` | Значение |
|---|---|
| `found` | Один документ; при скачивании есть PDF |
| `found_text_only` | Документ старше границы охвата найден в ips.pravo.gov.ru: есть текст, PDF нет |
| `not_found` | Среди кандидатов органа ничего нет; `diagnostics` — находки у других органов |
| `ambiguous` | Несколько документов в `candidates` (или `ips_candidates`), автовыбора нет |
| `out_of_range` | Дата раньше границы охвата, и запасной поиск не дал одного документа |
| `invalid_input` | Нет номера, даты или органа либо название не разобрано |
| `network_error` | Сбой сети после повторов |
| `integrity_error` | Скачанный PDF не прошёл проверку |

`match_type`: `exact` — точное совпадение номера, `digits_only` — найдено по
цифровой части номера.

Для изменённого документа PDF — исходная публикация, а DOCX из EBPI —
действующая редакция: к имени DOCX добавляется «(ред. от ДД.ММ.ГГГГ)», а
акты-поправки записываются в `document_relations`.

### Python: ИТС

```python
from npa_pipeline import burondt
from npa_pipeline.its import ItsCache
from npa_pipeline.its_download import fetch_its, fetch_its_by_url_id

cache = ItsCache("data/npa.db")
with burondt.create_burondt_client() as client:
    result = fetch_its(client, designation="ИТС 28-2021", out_dir="downloads/its", cache=cache)
    # после ambiguous — выбранная карточка
    result = fetch_its_by_url_id(client, url_id=1640, out_dir="downloads/its", cache=cache)

for f in result.files:
    f.role, f.pdf_path, f.text_layer_docx_path   # role: document | document_word | order | cancellation
```

Запрос `ИТС 28-2021` находит одну карточку, `2021` — все справочники года,
`ИТС 53` — все версии номера (новые первыми).

### Командная строка

Печатает JSON результата.

```bash
npa fetch --authority "Президент Российской Федерации" --number 310-ФЗ --date 2025-07-31
npa fetch --eo 0001202507310060
npa fetch --title 'Федеральный закон от 21.07.2014 № 219-ФЗ "О внесении…"'
npa fetch --authority "..." --number 402 --date 2016-07-19 --no-download

npa fetch-its "ИТС 28-2021"
npa fetch-its 2021

npa import-json --out-dir downloads --delete-json   # перенести JSON-файлы сведений в БД
```

Общие параметры: `--db data/npa.db`; у `fetch` и `fetch-its` — `--out-dir`,
`--no-download`.

### Веб-приложение

```bash
npa-web            # http://127.0.0.1:8765
```

Одна форма принимает название НПА или обозначение ИТС; результат — описание
документа и кнопки «Открыть» и «Скачать» (DOCX открывается HTML-предпросмотром).
У НПА со страницами без текстового слоя есть кнопка «Распознать скан»: она
отправляет PDF в сервис npa-ocr и показывает ход распознавания.
Отдельная форма «Распознать PDF с компьютера» принимает один PDF до 100 МБ:
выберите файл, перетащите его в область загрузки или вставьте скопированный
файл через Ctrl+V. Вставка работает, если браузер получает из буфера именно
файл, а не текстовый путь к нему. После выбора нажмите «Распознать PDF»;
в форме появятся ход обработки, отмена и ссылка на готовый DOCX.
Название органа можно указать вручную для восстановления шапки документа.
Страница
`/monitor` показывает загрузку CPU, памяти, диска, GPU и модели Ollama.

| Метод | Путь | Назначение |
|---|---|---|
| `POST` | `/api/lookup` | Единый поиск: `{"q": "…"}` или `{"url_id": N}` для ИТС |
| `POST` | `/api/fetch` | НПА по реквизитам или `title` |
| `POST` | `/api/parse-title` | Разбор названия НПА |
| `POST` | `/api/fetch-its` | ИТС по `designation` или `url_id` |
| `GET` | `/api/pdf/{eo_number}` | PDF НПА (`?download=1` — скачать) |
| `GET` | `/api/docx-text/{eo_number}` | Текст НПА (EBPI или текстовый слой) |
| `GET` | `/api/docx/{nd}` | Текст документа из ips.pravo.gov.ru |
| `GET` | `/api/its/pdf/{file_id}` | Файл ИТС |
| `POST` | `/api/ocr` | Распознать скан: `{"eo_number": "…", "authority_name": "…"}` |
| `POST` | `/api/ocr/upload` | Распознать PDF с компьютера: multipart `file`, необязательно `authority_name` (до 100 МБ) |
| `GET` | `/api/ocr/jobs/{job_id}` | Ход распознавания |
| `POST` | `/api/ocr/jobs/{job_id}/cancel` | Отменить распознавание |
| `GET` | `/api/ocr/jobs/{job_id}/docx` | Результат распознавания |
| `POST` | `/api/refresh-authorities` | Обновить справочник органов |
| `POST` | `/api/refresh-its` | Продолжить обход burondt.ru |
| `GET` | `/api/health`, `/api/monitor` | Состояние приложения и сервера |

### Docker

`docker-compose.yml` поднимает веб-приложение (`web`), сервис распознавания
(`ocr`), шлюз vLLM (`vlm-gate`) и vLLM с DeepSeek-OCR (`vlm`). Образ
`npa-ocr:latest` собирается в репозитории npa-ocr заранее (см.
[`docs/ocr-integration.md`](docs/ocr-integration.md)).

```bash
docker compose up --build -d     # http://127.0.0.1:8765
```

| Переменная `.env` | Назначение |
|---|---|
| `WEB_PORT` | Порт веб-приложения на хосте (8765) |
| `MONITOR_HOSTNAME` | Имя хоста на странице мониторинга |
| `VLM_IDLE_MIN` | Простой до остановки vLLM, мин (10) |
| `VLM_HF_CACHE` | Каталог кэша Hugging Face с весами DeepSeek-OCR |

`docker-compose.dev.yml` подключает исходный код соседнего репозитория
`../npa-ocr` в контейнер `ocr`, чтобы правки npa-ocr применялись без
пересборки образа:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d ocr
```

## Разработка

```bash
pytest            # юнит-тесты, без сети
pytest -m live    # эталонный набор против publication.pravo.gov.ru
```

Эталонные случаи — [`tests/golden/golden_set.json`](tests/golden/golden_set.json).

Структура пакета:

```
src/npa_pipeline/
  service.py           fetch_document: поиск, скачивание, текст
  search.py            алгоритм поиска НПА
  parse_citation.py    название → реквизиты
  api.py               клиент publication.pravo.gov.ru
  authorities.py       кэш органов
  ebpi.py, ebpi_docx.py      EBPI и сборка DOCX
  ips_pravo.py         ips.pravo.gov.ru
  text_layer_docx.py   DOCX из текстового слоя PDF
  burondt.py, its.py, its_download.py   справочники ИТС
  db.py                схема и доступ к SQLite
  ocr_jobs.py          клиент сервиса npa-ocr
  webapp.py, static/   веб-приложение
  server_monitor.py    мониторинг ресурсов
  cli.py               командная строка
```

## Ограничения

- PDF скачивается через `/file/pdf`, а `robots.txt` портала содержит
  `Disallow: /File`; инструмент рассчитан на разовые запросы, а не на массовый
  сбор.
- Поиск «по названию» — это разбор реквизитов из названия, а не
  полнотекстовый поиск по теме (`ComplexName` портала полнотекстовым не
  является).
- Справочники ИТС ищутся только среди карточек локального кэша.
- EBPI и ips.pravo.gov.ru не документированы и могут измениться; покрытие EBPI
  по видам актов проверено выборочно.
- Для НПА текстовый слой PDF почти не помогает: акты, которых нет в EBPI, как
  правило публикуются сканами. Для справочников ИТС это основной путь.
- К pravo.gov.ru клиент обращается по `http://` (в части окружений `https`
  зависает на уровне TCP); целостность файлов проверяется локально.
- Задания распознавания хранятся в памяти веб-приложения и теряются при его
  перезапуске (сохранённые страницы npa-ocr при повторном запуске
  переиспользуются).
