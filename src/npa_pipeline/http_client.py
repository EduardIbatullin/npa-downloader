"""HTTP-клиент с общими retry/backoff (base_url настраивается — используется и для
publication.pravo.gov.ru, и для burondt.ru)."""

from __future__ import annotations

import time
from typing import Any

import httpx

BASE_URL = "http://publication.pravo.gov.ru"

# JSON-запросы короткие; PDF может быть 30+ МБ
JSON_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
PDF_TIMEOUT = httpx.Timeout(180.0, connect=15.0)

# 429 добавлен ради burondt.ru (устойчивость на полном объёме ~2550 запросов не проверена;
# для publication.pravo.gov.ru не мешает — там 429 не наблюдался)
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0)
PAGE_PAUSE_SECONDS = 0.35
BURONDT_PAGE_PAUSE_SECONDS = 0.5  # чуть консервативнее — устойчивость burondt.ru не проверена


class NetworkError(Exception):
    """Сбой сети/сервера после исчерпания retry."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


def create_client(
    *,
    transport: httpx.BaseTransport | None = None,
    base_url: str = BASE_URL,
) -> httpx.Client:
    """Создаёт синхронный httpx.Client (можно подменить transport для тестов и base_url для другого хоста)."""
    kwargs: dict[str, Any] = {
        "base_url": base_url,
        "timeout": JSON_TIMEOUT,
        "follow_redirects": True,
        "headers": {"Accept": "text/html, application/json, */*"},
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> Any:
    """Выполняет GET/POST и возвращает разобранный JSON, с retry на обрывы и 5xx.

    Args:
        data: если задан — отправляется как form-urlencoded тело запроса (не
            query-параметры); нужен для API, принимающих POST с
            application/x-www-form-urlencoded (например, ips.pravo.gov.ru,
            см. npa_pipeline.ips_pravo), а не ?params в URL.

    Raises:
        NetworkError: после MAX_ATTEMPTS неудачных попыток (обрыв соединения,
            таймаут, или код из RETRY_STATUSES) — не возвращает None, а именно
            бросает исключение.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.request(method, path, params=params, data=data, timeout=JSON_TIMEOUT)
            if response.status_code in RETRY_STATUSES:
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt + 1 >= MAX_ATTEMPTS:
                break
            time.sleep(BACKOFF_SECONDS[attempt])
    raise NetworkError(f"Запрос {method} {path} не удался после {MAX_ATTEMPTS} попыток", cause=last_exc)


def request_text(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> str:
    """Выполняет GET/POST и возвращает текст ответа (HTML), с тем же retry, что
    request_json.

    Raises:
        NetworkError: после MAX_ATTEMPTS неудачных попыток — см. request_json.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.request(method, path, params=params, timeout=JSON_TIMEOUT)
            if response.status_code in RETRY_STATUSES:
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response.text
        except (httpx.TransportError, httpx.HTTPStatusError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt + 1 >= MAX_ATTEMPTS:
                break
            time.sleep(BACKOFF_SECONDS[attempt])
    raise NetworkError(f"Запрос {method} {path} не удался после {MAX_ATTEMPTS} попыток", cause=last_exc)


def download_bytes(
    client: httpx.Client,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> tuple[bytes, int | None, httpx.Headers]:
    """Скачивает тело ответа потоком (годится для крупных файлов, см. PDF_TIMEOUT).

    Returns:
        (данные, Content-Length из заголовка или None, заголовки ответа целиком —
        нужны вызывающему коду, например, для Content-Disposition).

    Raises:
        NetworkError: после MAX_ATTEMPTS неудачных попыток — см. request_json.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with client.stream("GET", path, params=params, timeout=PDF_TIMEOUT) as response:
                if response.status_code in RETRY_STATUSES:
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                content_length = None
                cl = response.headers.get("Content-Length")
                if cl and cl.isdigit():
                    content_length = int(cl)
                chunks: list[bytes] = []
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                return b"".join(chunks), content_length, response.headers
        except (httpx.TransportError, httpx.HTTPStatusError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt + 1 >= MAX_ATTEMPTS:
                break
            time.sleep(BACKOFF_SECONDS[attempt])
    raise NetworkError(f"Скачивание {path} не удалось после {MAX_ATTEMPTS} попыток", cause=last_exc)
