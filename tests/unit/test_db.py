"""Тесты DocumentStore и импорта JSON."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from npa_pipeline.db import DocumentStore
from npa_pipeline.download import already_downloaded, save_download, verify_pdf
from npa_pipeline.import_json import import_json_sidecars
from npa_pipeline.models import DocItem, Query


FIXTURE = Path(__file__).parent / "fixtures" / "document_joint_247_04.json"


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


def test_upsert_card_joint_signatories(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    card = json.loads(FIXTURE.read_text(encoding="utf-8"))
    eo = store.upsert_from_card(card)
    assert eo == "0001202306010022"

    row = store.get_document_row(eo)
    assert row is not None
    assert row["number"] == "247/04"
    assert row["raw_card_json"]
    assert len(row["signatories"]) == 2
    mains = [s for s in row["signatories"] if s["is_main"]]
    assert len(mains) == 1
    assert mains[0]["authority_guid"] == "d67a404e-260a-4a5b-b340-d34558da8bd6"


def test_upsert_idempotent(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    card = json.loads(FIXTURE.read_text(encoding="utf-8"))
    store.upsert_from_card(card)
    first = store.get_document_row(card["eoNumber"])["first_seen_at"]
    store.upsert_from_card(card)
    second = store.get_document_row(card["eoNumber"])
    assert second["first_seen_at"] == first
    assert store.count_documents() == 1


def test_save_and_already_downloaded(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    card = json.loads(FIXTURE.read_text(encoding="utf-8"))
    store.upsert_from_card(card)
    data = _minimal_pdf()
    pages = verify_pdf(data, content_length=len(data))
    doc = DocItem(
        eo_number=card["eoNumber"],
        number=card["number"],
        document_date=date(2023, 4, 25),
        signatory_ids=["a", "b"],
        complex_name=card["complexName"],
    )
    path = save_download(
        tmp_path / "dl",
        data=data,
        eo_number=card["eoNumber"],
        document=doc,
        pages=pages,
        store=store,
    )
    assert path.is_file()
    assert not path.with_suffix(".json").exists()
    assert already_downloaded(store, card["eoNumber"]) == path
    assert store.count_files() == 1


def test_import_json_sidecar(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    out = tmp_path / "downloads"
    out.mkdir()
    data = _minimal_pdf()
    pdf = out / "demo.pdf"
    pdf.write_bytes(data)
    meta = {
        "eoNumber": "0001202306010022",
        "filename": "demo.pdf",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "pages": 1,
        "document": {
            "eo_number": "0001202306010022",
            "number": "247/04",
            "document_date": "2023-04-25",
            "signatory_ids": ["a", "b"],
            "complex_name": "Приказ от 25.04.2023 № 247/04",
        },
    }
    (out / "demo.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    stats = import_json_sidecars(out, store, delete_json=True)
    assert stats["imported"] == 1
    assert stats["deleted"] == 1
    assert not (out / "demo.json").exists()
    assert store.get_file("0001202306010022") is not None
    assert already_downloaded(store, "0001202306010022") == pdf.resolve()


def test_get_ebpi_none_when_never_checked(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    card = json.loads(FIXTURE.read_text(encoding="utf-8"))
    store.upsert_from_card(card)
    assert store.get_ebpi(card["eoNumber"]) is None


def test_record_ebpi_covered_roundtrip(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    card = json.loads(FIXTURE.read_text(encoding="utf-8"))
    eo = store.upsert_from_card(card)

    store.record_ebpi(
        eo_number=eo,
        covered=True,
        docid=318024,
        docstate="Действует с изменениями",
        redid=473307,
        red_date="20250901",
        docx_path=tmp_path / "doc.docx",
    )
    rec = store.get_ebpi(eo)
    assert rec is not None
    assert rec.covered is True
    assert rec.docid == 318024
    assert rec.docstate == "Действует с изменениями"
    assert rec.redid == 473307
    assert rec.red_date == "20250901"
    assert rec.docx_path == str(tmp_path / "doc.docx")


def test_record_ebpi_not_covered_roundtrip(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    card = json.loads(FIXTURE.read_text(encoding="utf-8"))
    eo = store.upsert_from_card(card)

    store.record_ebpi(eo_number=eo, covered=False)
    rec = store.get_ebpi(eo)
    assert rec is not None
    assert rec.covered is False
    assert rec.docid is None
    assert rec.docx_path is None


def test_record_ebpi_upsert_overwrites_previous(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    card = json.loads(FIXTURE.read_text(encoding="utf-8"))
    eo = store.upsert_from_card(card)

    store.record_ebpi(eo_number=eo, covered=False)
    assert store.get_ebpi(eo).covered is False

    store.record_ebpi(eo_number=eo, covered=True, docid=1, docstate="x", redid=2, red_date="y", docx_path="d.docx")
    rec = store.get_ebpi(eo)
    assert rec.covered is True
    assert rec.docid == 1


def test_record_ebpi_unknown_document_raises(tmp_path: Path):
    import pytest

    store = DocumentStore(tmp_path / "npa.db")
    with pytest.raises(ValueError, match="не найден"):
        store.record_ebpi(eo_number="нет-такого", covered=False)


def test_record_relation_and_read_back(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    card = json.loads(FIXTURE.read_text(encoding="utf-8"))
    target = store.upsert_from_card(card)

    from datetime import date as date_cls
    from npa_pipeline.models import DocItem

    source = "0001202609230028"
    store.upsert_from_doc_item(
        DocItem(eo_number=source, number="1221", document_date=date_cls(2026, 9, 23), signatory_ids=[])
    )
    store.record_relation(
        source_eo=source, relation_type="amends", target_eo=target,
        effective_date="20260923", origin="ebpi",
    )

    rows = store.get_relations_for_target(target)
    assert len(rows) == 1
    assert rows[0]["source_eo"] == source
    assert rows[0]["relation_type"] == "amends"
    assert rows[0]["effective_date"] == "20260923"
    assert rows[0]["origin"] == "ebpi"


def test_record_relation_idempotent_no_duplicates(tmp_path: Path):
    store = DocumentStore(tmp_path / "npa.db")
    card = json.loads(FIXTURE.read_text(encoding="utf-8"))
    target = store.upsert_from_card(card)

    from datetime import date as date_cls
    from npa_pipeline.models import DocItem

    source = "0001202609230028"
    store.upsert_from_doc_item(
        DocItem(eo_number=source, number="1221", document_date=date_cls(2026, 9, 23), signatory_ids=[])
    )
    for _ in range(3):
        store.record_relation(
            source_eo=source, relation_type="amends", target_eo=target,
            effective_date="20260923", origin="ebpi",
        )
    assert len(store.get_relations_for_target(target)) == 1


def test_record_relation_requires_known_documents(tmp_path: Path):
    import pytest

    store = DocumentStore(tmp_path / "npa.db")
    with pytest.raises(Exception):
        store.record_relation(
            source_eo="нет-такого-источника", relation_type="amends",
            target_eo="нет-такой-цели", origin="ebpi",
        )
