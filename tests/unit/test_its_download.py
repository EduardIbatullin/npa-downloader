"""Тесты скачивания/проверки файлов ИТС и резолвинга fetch_its."""

from __future__ import annotations

from pathlib import Path

import httpx

from npa_pipeline import burondt
from npa_pipeline.http_client import create_client
from npa_pipeline.its import ItsCache
from npa_pipeline.its_download import already_downloaded_its, fetch_its, fetch_its_by_url_id, save_its_file
from npa_pipeline.models import Status

FIXTURES = Path(__file__).parent / "fixtures" / "burondt"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _minimal_pdf() -> bytes:
    return b"""%PDF-1.1
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>endobj
xref
0 4
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
trailer<< /Size 4 /Root 1 0 R >>
startxref
190
%%EOF
"""


def _seeded_cache(tmp_path: Path) -> ItsCache:
    cache = ItsCache(tmp_path / "its.db")
    card = burondt.parse_card(_read("its_card.html"), url_id=1640)
    cache.upsert_card(card)
    return cache


def test_save_its_file_atomic_and_dedup(tmp_path: Path):
    cache = _seeded_cache(tmp_path)
    data = _minimal_pdf()
    path = save_its_file(
        tmp_path / "out",
        data=data,
        designation="ИТС 28-2021",
        file_id=2190,
        role="document",
        pages=1,
        cache=cache,
    )
    assert path.is_file()
    assert not path.name.endswith(".part")
    assert already_downloaded_its(cache, 2190) == path


def test_fetch_its_not_found(tmp_path: Path):
    cache = ItsCache(tmp_path / "its.db")

    def handler(request: httpx.Request) -> httpx.Response:  # не должен вызываться
        raise AssertionError("сеть не должна вызываться для пустого кэша")

    with create_client(base_url=burondt.BURONDT_BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = fetch_its(client, designation="ИТС 999-2099", out_dir=tmp_path / "out", cache=cache)

    assert result.status == Status.NOT_FOUND


def test_fetch_its_downloads_and_verifies(tmp_path: Path):
    cache = _seeded_cache(tmp_path)
    data = _minimal_pdf()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=data,
            headers={"Content-Disposition": 'attachment; filename="file.pdf"'},
        )

    with create_client(base_url=burondt.BURONDT_BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = fetch_its(
            client, designation="ИТС 28-2021", out_dir=tmp_path / "out", cache=cache
        )

    assert result.status == Status.FOUND
    assert len(result.files) == 2
    order = next(f for f in result.files if f.role == "order")
    assert order.order_number_caption == "835"
    assert order.pdf_path is not None
    assert Path(order.pdf_path).is_file()


def test_fetch_its_includes_related_word_file(tmp_path: Path):
    cache = ItsCache(tmp_path / "its.db")
    cache.upsert_card(
        burondt.ItsCard(
            url_id=1647,
            designation="ИТС 22.1-2021",
            title="ИТС 22.1-2021",
            files=[
                burondt.ItsCardFile(file_id=2196, caption="ИТС НДТ 22.1-2021", role="document"),
                burondt.ItsCardFile(
                    file_id=2500,
                    caption="Приказ об утверждении ИТС 22.1-2021",
                    role="order",
                ),
            ],
        )
    )
    cache.upsert_card(
        burondt.ItsCard(
            url_id=2552,
            designation="ИТС 22.1-2021 в формате Word",
            title="ИТС 22.1-2021 в формате Word",
            files=[
                burondt.ItsCardFile(file_id=3687, caption="ИТС 22.1-2021", role="document"),
            ],
        )
    )
    pdf = _minimal_pdf()
    docx = b"PK\x03\x04" + b"word-content-placeholder"

    def handler(request: httpx.Request) -> httpx.Response:
        fid = request.url.params.get("UrlId")
        if fid == "3687":
            return httpx.Response(
                200,
                content=docx,
                headers={"Content-Disposition": 'attachment; filename="its.docx"'},
            )
        return httpx.Response(
            200,
            content=pdf,
            headers={"Content-Disposition": 'attachment; filename="file.pdf"'},
        )

    with create_client(base_url=burondt.BURONDT_BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = fetch_its(
            client, designation="ИТС 22_1-2021_", out_dir=tmp_path / "out", cache=cache
        )

    assert result.status == Status.FOUND
    roles = {f.role for f in result.files}
    assert roles == {"document", "document_word", "order"}
    word = next(f for f in result.files if f.role == "document_word")
    assert word.pdf_path is not None
    assert Path(word.pdf_path).suffix.lower() == ".docx"


def test_fetch_its_skips_already_downloaded(tmp_path: Path):
    cache = _seeded_cache(tmp_path)
    data = _minimal_pdf()
    save_its_file(
        tmp_path / "out",
        data=data,
        designation="ИТС 28-2021",
        file_id=2190,
        role="document",
        pages=1,
        cache=cache,
    )

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=data)

    with create_client(base_url=burondt.BURONDT_BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = fetch_its(
            client, designation="ИТС 28-2021", out_dir=tmp_path / "out", cache=cache
        )

    assert result.status == Status.FOUND
    # только file_id 2504 (приказ) должен реально скачиваться — 2190 уже на диске
    assert calls["n"] == 1


def test_fetch_its_ambiguous_includes_files_per_candidate(tmp_path: Path):
    # Две версии номера «47» — запрос по номеру без года должен вернуть AMBIGUOUS
    # с файлами (роль + подпись) у каждого кандидата, чтобы UI мог показать
    # «справочник + приказ» и дать скачать конкретную версию без нового резолвинга.
    cache = ItsCache(tmp_path / "its.db")
    cache.upsert_card(
        burondt.ItsCard(
            url_id=1150,
            designation="ИТС НДТ 47",
            title="ИТС НДТ 47",
            files=[
                burondt.ItsCardFile(file_id=1420, caption="ИТС 47", role="document"),
                burondt.ItsCardFile(
                    file_id=2476,
                    caption="Приказ №2846 от 15.12.2017 об утверждении ИТС 47-2017",
                    role="order",
                ),
            ],
        )
    )
    cache.upsert_card(
        burondt.ItsCard(
            url_id=2100,
            designation="ИТС 47-2023",
            title="ИТС 47-2023",
            files=[
                burondt.ItsCardFile(file_id=2983, caption="ИТС 47-2023", role="document"),
                burondt.ItsCardFile(
                    file_id=2982,
                    caption="Приказ 21 декабря 2023 г. № 2759 об утверждении ИТС 47-2023",
                    role="order",
                ),
            ],
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:  # не должен вызываться
        raise AssertionError("AMBIGUOUS не должен скачивать файлы")

    with create_client(base_url=burondt.BURONDT_BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = fetch_its(client, designation="ИТС 47", out_dir=tmp_path / "out", cache=cache)

    assert result.status == Status.AMBIGUOUS
    assert [c["url_id"] for c in result.candidates] == [2100, 1150]  # новые сначала
    for c in result.candidates:
        roles = {f["role"] for f in c["files"]}
        assert roles == {"document", "order"}


def test_fetch_its_by_url_id_bypasses_resolve(tmp_path: Path):
    cache = _seeded_cache(tmp_path)
    data = _minimal_pdf()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data, headers={"Content-Disposition": 'attachment; filename="file.pdf"'})

    with create_client(base_url=burondt.BURONDT_BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = fetch_its_by_url_id(client, url_id=1640, out_dir=tmp_path / "out", cache=cache)

    assert result.status == Status.FOUND
    assert result.url_id == 1640
    assert len(result.files) == 2
    assert all(Path(f.pdf_path).is_file() for f in result.files)


def _text_pdf_bytes(text: str) -> bytes:
    """PDF с полноценным текстовым слоем (не скан) — см. test_text_layer_docx.py."""
    stream = f"BT /F1 12 Tf 10 150 Td ({text}) Tj ET"
    return (
        "%PDF-1.4\n"
        "1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        "2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        "3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>endobj\n"
        "4 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n"
        f"5 0 obj<< /Length {len(stream)} >>\nstream\n{stream}\nendstream\nendobj\n"
        "trailer<< /Size 6 /Root 1 0 R >>\n"
        "startxref\n0\n%%EOF\n"
    ).encode("latin-1")


def test_fetch_its_attaches_text_layer_docx_for_text_pdf_only(tmp_path: Path):
    """Справочник (file_id 2190) не скан — получает .docx; приказ (2504) скан —
    не получает (см. npa_pipeline.its_download._attach_text_layer_docx)."""
    cache = _seeded_cache(tmp_path)
    text_pdf = _text_pdf_bytes("Document has enough text content to pass the threshold")
    scan_pdf = _minimal_pdf()

    def handler(request: httpx.Request) -> httpx.Response:
        url_id = request.url.params.get("UrlId")
        data = text_pdf if url_id == "2190" else scan_pdf
        return httpx.Response(200, content=data, headers={"Content-Disposition": 'attachment; filename="file.pdf"'})

    with create_client(base_url=burondt.BURONDT_BASE_URL, transport=httpx.MockTransport(handler)) as client:
        result = fetch_its(client, designation="ИТС 28-2021", out_dir=tmp_path / "out", cache=cache)

    assert result.status == Status.FOUND
    document = next(f for f in result.files if f.role == "document")
    order = next(f for f in result.files if f.role == "order")

    assert document.text_layer_docx_path is not None
    assert Path(document.text_layer_docx_path).is_file()
    assert order.text_layer_docx_path is None  # скан — задача OCR, не извлечения


def test_fetch_its_second_call_reuses_existing_file_still_has_docx(tmp_path: Path):
    """Ветка already_downloaded_its (файл уже на диске) тоже прикрепляет .docx."""
    cache = _seeded_cache(tmp_path)
    text_pdf = _text_pdf_bytes("Document has enough text content to pass the threshold")
    scan_pdf = _minimal_pdf()

    def handler(request: httpx.Request) -> httpx.Response:
        url_id = request.url.params.get("UrlId")
        data = text_pdf if url_id == "2190" else scan_pdf
        return httpx.Response(200, content=data, headers={"Content-Disposition": 'attachment; filename="file.pdf"'})

    with create_client(base_url=burondt.BURONDT_BASE_URL, transport=httpx.MockTransport(handler)) as client:
        first = fetch_its(client, designation="ИТС 28-2021", out_dir=tmp_path / "out", cache=cache)
        second = fetch_its(client, designation="ИТС 28-2021", out_dir=tmp_path / "out", cache=cache)

    first_doc = next(f for f in first.files if f.role == "document")
    second_doc = next(f for f in second.files if f.role == "document")
    assert first_doc.text_layer_docx_path is not None
    assert second_doc.text_layer_docx_path == first_doc.text_layer_docx_path
