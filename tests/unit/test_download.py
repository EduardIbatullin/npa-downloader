"""Тесты проверки целостности PDF."""

from datetime import date
from pathlib import Path

import pytest

from npa_pipeline.db import DocumentStore
from npa_pipeline.download import IntegrityError, already_downloaded, save_download, verify_pdf
from npa_pipeline.models import DocItem


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


def test_verify_ok():
    data = _minimal_pdf()
    pages = verify_pdf(data, content_length=len(data))
    assert pages >= 1


def test_verify_bad_signature():
    with pytest.raises(IntegrityError, match="%PDF"):
        verify_pdf(b"not a pdf %%EOF", content_length=None)


def test_verify_truncated():
    data = _minimal_pdf().replace(b"%%EOF", b"")
    with pytest.raises(IntegrityError, match="%%EOF"):
        verify_pdf(data, content_length=None)


def test_verify_content_length_mismatch():
    data = _minimal_pdf()
    with pytest.raises(IntegrityError, match="Content-Length"):
        verify_pdf(data, content_length=len(data) - 1)


def test_skip_redownload(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    data = _minimal_pdf()
    doc = DocItem(
        eo_number="0001",
        number="1",
        document_date=date(2020, 1, 1),
        signatory_ids=["x"],
        complex_name="Указ Президента Российской Федерации от 01.01.2020 № 1",
    )
    store.upsert_from_doc_item(doc)
    path = save_download(
        tmp_path,
        data=data,
        eo_number="0001",
        document=doc,
        pages=1,
        store=store,
    )
    # «Президента Российской Федерации» -> «Президента РФ»: сокращение органов
    # в имени файла намеренное (см. naming.py, README «Имена PDF НПА …»)
    assert path.name == "Указ Президента РФ от 01_01_2020 N 1.pdf"
    assert path.exists()
    assert already_downloaded(store, "0001") == path
    assert not path.with_suffix(".json").exists()
