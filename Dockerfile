FROM python:3.12-slim

WORKDIR /app

# Зависимости отдельно — слой кэшируется при неизменном pyproject.toml
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[web]"

# Каталоги рантайма (БД и PDF); при volume mount перекроются
RUN mkdir -p data downloads

EXPOSE 8765

# host 0.0.0.0 — иначе извне контейнера UI недоступен
# (npa-web по умолчанию слушает только 127.0.0.1)
#
# OCR (кнопка «Распознать скан» в веб-UI) — отдельный проект npa-ocr
# (https://github.com/EduardIbatullin/npa-ocr), здесь не установлен и не нужен:
# ocr_jobs.py обращается к нему по HTTP (OCR_SERVICE_URL), не subprocess — свой
# образ, свой контейнер (сервис `ocr` в docker-compose.yml), см.
# docs/ocr-integration.md.
CMD ["uvicorn", "npa_pipeline.webapp:app", "--host", "0.0.0.0", "--port", "8765"]
