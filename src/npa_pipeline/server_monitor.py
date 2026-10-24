"""Монитор ресурсов сервера для веб-UI (страница /monitor, эндпоинт /api/monitor).

Контейнер web не изолирован от хоста по /proc: без lxcfs /proc/stat, /proc/meminfo,
/proc/loadavg и /proc/uptime внутри контейнера показывают значения ХОСТА (лимитов
cgroup в docker-compose.yml нет), поэтому CPU/RAM читаем прямо оттуда, без
монтирования /proc и без docker.sock.

Диск — shutil.disk_usage по тому downloads: на nv-02 это bind mount с корневого
раздела хоста, т.е. те самые «/ 95%», которые важно видеть перед загрузкой моделей.

GPU — nvidia-smi: в docker-compose.yml сервису web выдан GPU только с capability
`utility` (NVIDIA runtime кладёт в контейнер nvidia-smi и даёт читать статистику,
но не считать на GPU). Нет nvidia-smi (локальный запуск без GPU) — блок помечается
недоступным, остальная страница работает.

Ollama — какие модели сейчас загружены и сколько VRAM держат (GPU на nv-02 общий:
ollama, neuro_expert, эскалация ИИ в npa-ocr), через /api/ps по OLLAMA_URL.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

PROC = Path(os.environ.get("MONITOR_PROC_DIR", "/proc"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
NVIDIA_SMI_TIMEOUT = 5.0
OLLAMA_TIMEOUT = 2.0
# Первый вызов после старта: предыдущего снимка /proc/stat ещё нет — меряем
# дельту за короткий интервал, иначе получили бы средний % с момента загрузки хоста.
FIRST_CPU_SAMPLE_INTERVAL = 0.25

_GPU_FIELDS = (
    "index",
    "name",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "temperature.gpu",
    "power.draw",
    "power.limit",
)


# --- CPU ---


def parse_proc_stat(text: str) -> dict[str, tuple[int, int]]:
    """/proc/stat -> {"cpu": (busy, total), "cpu0": ..., ...} в тиках с момента загрузки.

    busy — всё, кроме idle и iowait; guest/guest_nice уже входят в user/nice, их не
    суммируем повторно (иначе загрузка от виртуалок считалась бы дважды)."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        name, *vals = line.split()
        nums = [int(v) for v in vals[:8]]  # user nice system idle iowait irq softirq steal
        nums += [0] * (8 - len(nums))
        idle = nums[3] + nums[4]
        total = sum(nums)
        out[name] = (total - idle, total)
    return out


def cpu_percent(prev: dict[str, tuple[int, int]], cur: dict[str, tuple[int, int]]) -> dict[str, float]:
    out = {}
    for name, (busy, total) in cur.items():
        p_busy, p_total = prev.get(name, (0, 0))
        d_total = total - p_total
        out[name] = round(100.0 * (busy - p_busy) / d_total, 1) if d_total > 0 else 0.0
    return out


class _CpuSampler:
    """Загрузка CPU как дельта между двумя чтениями /proc/stat — между соседними
    запросами страницы (опрос раз в несколько секунд), без фонового потока."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prev: dict[str, tuple[int, int]] | None = None

    def sample(self) -> dict[str, float]:
        with self._lock:
            if self._prev is None:
                self._prev = parse_proc_stat((PROC / "stat").read_text())
                time.sleep(FIRST_CPU_SAMPLE_INTERVAL)
            cur = parse_proc_stat((PROC / "stat").read_text())
            pct = cpu_percent(self._prev, cur)
            self._prev = cur
            return pct


_cpu = _CpuSampler()


def read_cpu() -> dict:
    pct = _cpu.sample()
    load = (PROC / "loadavg").read_text().split()
    cores = sorted((k for k in pct if k != "cpu"), key=lambda k: int(k[3:]))
    return {
        "percent": pct.get("cpu", 0.0),
        "per_core": [pct[k] for k in cores],
        "count": len(cores) or os.cpu_count(),
        "load": [float(x) for x in load[:3]],
    }


# --- Память, диск, аптайм ---


def parse_meminfo(text: str) -> dict:
    kb = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            kb[key] = int(parts[0])
    total = kb.get("MemTotal", 0) * 1024
    available = kb.get("MemAvailable", kb.get("MemFree", 0)) * 1024
    swap_total = kb.get("SwapTotal", 0) * 1024
    swap_free = kb.get("SwapFree", 0) * 1024
    return {
        "total": total,
        "used": total - available,
        "available": available,
        "cached": (kb.get("Cached", 0) + kb.get("Buffers", 0)) * 1024,
        "swap_total": swap_total,
        "swap_used": swap_total - swap_free,
    }


def read_memory() -> dict:
    return parse_meminfo((PROC / "meminfo").read_text())


def read_disk(path: Path) -> dict:
    usage = shutil.disk_usage(path)
    return {"path": str(path), "total": usage.total, "used": usage.used, "free": usage.free}


def read_uptime() -> float:
    return float((PROC / "uptime").read_text().split()[0])


# --- GPU ---


def _num(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:  # «[N/A]», «[Not Supported]»
        return None


def parse_nvidia_smi(text: str) -> list[dict]:
    """CSV от `nvidia-smi --query-gpu=<_GPU_FIELDS> --format=csv,noheader,nounits`."""
    gpus = []
    for line in text.strip().splitlines():
        vals = [v.strip() for v in line.split(",")]
        if len(vals) != len(_GPU_FIELDS):
            continue
        row = dict(zip(_GPU_FIELDS, vals))
        gpus.append({
            "index": int(row["index"]),
            "name": row["name"],
            "util": _num(row["utilization.gpu"]),
            "mem_used": _mib(row["memory.used"]),
            "mem_total": _mib(row["memory.total"]),
            "temp": _num(row["temperature.gpu"]),
            "power": _num(row["power.draw"]),
            "power_limit": _num(row["power.limit"]),
        })
    return gpus


def _mib(value: str) -> int | None:
    n = _num(value)
    return None if n is None else int(n * 1024 * 1024)


def read_gpu() -> dict:
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(_GPU_FIELDS)}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT,
            check=True,
        )
    except FileNotFoundError:
        return {"available": False, "error": "nvidia-smi не найден (контейнеру не выдан GPU)"}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": f"nvidia-smi: {exc}"}
    return {"available": True, "gpus": parse_nvidia_smi(proc.stdout)}


# --- Ollama ---


def read_ollama(client: httpx.Client | None = None) -> dict:
    try:
        if client is None:
            with httpx.Client(timeout=OLLAMA_TIMEOUT) as c:
                resp = c.get(f"{OLLAMA_URL}/api/ps")
        else:
            resp = client.get(f"{OLLAMA_URL}/api/ps")
        resp.raise_for_status()
        models = resp.json().get("models") or []
    except (httpx.HTTPError, ValueError) as exc:
        return {"available": False, "url": OLLAMA_URL, "error": str(exc) or type(exc).__name__}
    return {
        "available": True,
        "url": OLLAMA_URL,
        "models": [
            {
                "name": m.get("name"),
                "size": m.get("size"),
                "vram": m.get("size_vram"),
                "expires_at": m.get("expires_at"),
                "params": (m.get("details") or {}).get("parameter_size"),
                "quant": (m.get("details") or {}).get("quantization_level"),
            }
            for m in models
        ],
    }


# --- Сервисы ---

SERVICE_TIMEOUT = 2.0


def check_http(name: str, url: str, client: httpx.Client | None = None) -> dict:
    """Сервис жив, если GET url отвечает 2xx (для npa-ocr — его /health)."""
    try:
        if client is None:
            with httpx.Client(timeout=SERVICE_TIMEOUT) as c:
                resp = c.get(url)
        else:
            resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return {"name": name, "ok": False, "detail": str(exc) or type(exc).__name__}
    return {"name": name, "ok": True, "detail": url}


# --- Снимок целиком ---


def snapshot(disk_path: Path, ocr_url: str | None = None) -> dict:
    """Все метрики разом; сбой одного источника не ломает остальные.

    services — для мини-монитора на главной: Ollama (по уже полученному /api/ps,
    без второго запроса) и npa-ocr (его /health), если передан ocr_url."""
    out: dict = {"time": time.time()}
    for key, fn in (
        ("cpu", read_cpu),
        ("memory", read_memory),
        ("disk", lambda: read_disk(disk_path)),
        ("uptime", read_uptime),
        ("gpu", read_gpu),
        ("ollama", read_ollama),
    ):
        try:
            out[key] = fn()
        except OSError as exc:
            out[key] = {"available": False, "error": str(exc)}
    ollama = out["ollama"]
    services = [{
        "name": "Ollama",
        "ok": bool(ollama.get("available")),
        "detail": ollama.get("url") if ollama.get("available") else ollama.get("error"),
    }]
    if ocr_url:
        services.append(check_http("npa-ocr", f"{ocr_url.rstrip('/')}/health"))
    out["services"] = services
    out["hostname"] = os.environ.get("MONITOR_HOSTNAME") or os.uname().nodename
    return out
