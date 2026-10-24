"""Монитор ресурсов: разбор /proc и nvidia-smi на фикстурах-строках, Ollama на
httpx.MockTransport — без реального хоста/GPU/сети."""

from __future__ import annotations

from pathlib import Path

import httpx

from npa_pipeline import server_monitor as sm

STAT_1 = """cpu  100 0 100 800 0 0 0 0 0 0
cpu0 50 0 50 400 0 0 0 0 0 0
cpu1 50 0 50 400 0 0 0 0 0 0
intr 12345
"""
# +100 тиков на каждом ядре: cpu0 — всё busy, cpu1 — всё idle
STAT_2 = """cpu  200 0 100 900 0 0 0 0 0 0
cpu0 150 0 50 400 0 0 0 0 0 0
cpu1 50 0 50 500 0 0 0 0 0 0
"""

MEMINFO = """MemTotal:       1000000 kB
MemFree:          100000 kB
MemAvailable:     400000 kB
Buffers:           10000 kB
Cached:           200000 kB
SwapTotal:        500000 kB
SwapFree:         450000 kB
"""


def test_cpu_percent_is_delta_between_samples():
    pct = sm.cpu_percent(sm.parse_proc_stat(STAT_1), sm.parse_proc_stat(STAT_2))
    assert pct == {"cpu": 50.0, "cpu0": 100.0, "cpu1": 0.0}


def test_iowait_counts_as_idle():
    busy, total = sm.parse_proc_stat("cpu  10 0 0 50 40 0 0 0\n")["cpu"]
    assert (busy, total) == (10, 100)


def test_parse_meminfo_uses_available_not_free():
    mem = sm.parse_meminfo(MEMINFO)
    assert mem["total"] == 1000000 * 1024
    assert mem["used"] == 600000 * 1024  # total - MemAvailable, кэш не считается занятым
    assert mem["swap_used"] == 50000 * 1024


def test_read_cpu_from_proc_dir(tmp_path: Path, monkeypatch):
    (tmp_path / "stat").write_text(STAT_1)
    (tmp_path / "loadavg").write_text("1.50 2.00 3.25 1/100 42\n")
    monkeypatch.setattr(sm, "PROC", tmp_path)
    monkeypatch.setattr(sm, "FIRST_CPU_SAMPLE_INTERVAL", 0)
    monkeypatch.setattr(sm, "_cpu", sm._CpuSampler())
    sm.read_cpu()  # первый снимок — база
    (tmp_path / "stat").write_text(STAT_2)
    cpu = sm.read_cpu()
    assert cpu["percent"] == 50.0
    assert cpu["per_core"] == [100.0, 0.0]
    assert cpu["count"] == 2
    assert cpu["load"] == [1.5, 2.0, 3.25]


def test_parse_nvidia_smi_handles_not_available_fields():
    text = "0, NVIDIA GeForce RTX 3090, 37, 11329, 24576, 46, 32.10, 370.00\n1, Some GPU, [N/A], 0, 8192, [N/A], [N/A], [N/A]\n"
    gpus = sm.parse_nvidia_smi(text)
    assert gpus[0]["name"] == "NVIDIA GeForce RTX 3090"
    assert gpus[0]["util"] == 37.0
    assert gpus[0]["mem_total"] == 24576 * 1024 * 1024
    assert gpus[0]["power"] == 32.1
    assert gpus[1]["util"] is None and gpus[1]["temp"] is None


def test_read_gpu_without_nvidia_smi(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(sm.subprocess, "run", missing)
    gpu = sm.read_gpu()
    assert gpu["available"] is False


def test_read_ollama_lists_loaded_models():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": [{
            "name": "qwen3-vl:8b", "size": 9_000_000_000, "size_vram": 8_000_000_000,
            "expires_at": "2026-10-24T14:00:00Z",
            "details": {"parameter_size": "8.8B", "quantization_level": "Q4_K_M"},
        }]})

    res = sm.read_ollama(httpx.Client(transport=httpx.MockTransport(handler)))
    assert res["available"] is True
    assert res["models"][0]["name"] == "qwen3-vl:8b"
    assert res["models"][0]["vram"] == 8_000_000_000
    assert res["models"][0]["quant"] == "Q4_K_M"


def test_read_ollama_unreachable_is_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    res = sm.read_ollama(httpx.Client(transport=httpx.MockTransport(handler)))
    assert res["available"] is False
    assert "refused" in res["error"]


def test_check_http_ok_and_down():
    ok = sm.check_http("npa-ocr", "http://ocr/health", httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))))
    down = sm.check_http("npa-ocr", "http://ocr/health", httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    assert ok["ok"] is True
    assert down["ok"] is False


def test_snapshot_services_include_ollama_and_ocr(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sm, "read_gpu", lambda: {"available": False, "error": "x"})
    monkeypatch.setattr(sm, "read_ollama", lambda: {"available": True, "url": "http://ollama", "models": []})
    monkeypatch.setattr(sm, "check_http", lambda name, url: {"name": name, "ok": False, "detail": url})
    snap = sm.snapshot(tmp_path, ocr_url="http://ocr:8000/")
    assert snap["services"] == [
        {"name": "Ollama", "ok": True, "detail": "http://ollama"},
        {"name": "npa-ocr", "ok": False, "detail": "http://ocr:8000/health"},
    ]


def test_snapshot_survives_failing_source(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sm, "PROC", tmp_path / "missing")  # нет /proc — CPU/RAM/аптайм падают
    monkeypatch.setattr(sm, "_cpu", sm._CpuSampler())
    monkeypatch.setattr(sm, "read_gpu", lambda: {"available": False, "error": "x"})
    monkeypatch.setattr(sm, "read_ollama", lambda: {"available": False, "error": "x"})
    snap = sm.snapshot(tmp_path)
    assert snap["cpu"]["available"] is False
    assert snap["memory"]["available"] is False
    assert snap["disk"]["total"] > 0  # диск читается независимо
