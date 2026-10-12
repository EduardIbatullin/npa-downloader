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
# OCR (кнопка «Распознать скан» в веб-UI) вынесен в отдельный проект npa-ocr
# (https://github.com/EduardIbatullin/npa-ocr) и здесь не установлен — ocr_jobs.py
# запускает внешнюю команду `npa-ocr`, которой в этом образе нет. Чтобы она
# заработала внутри контейнера, нужно либо поставить её в этот же образ
# (RUN pip install npa-ocr[models] — несколько ГБ, системный Tesseract и
# torch/torchvision CPU, см. Dockerfile npa-ocr), либо вызывать её в соседнем
# контейнере/на хосте — выбор пока не сделан.
CMD ["uvicorn", "npa_pipeline.webapp:app", "--host", "0.0.0.0", "--port", "8765"]
