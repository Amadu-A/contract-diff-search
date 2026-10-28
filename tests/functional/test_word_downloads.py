# tests/functional/test_word_downloads.py
"""Настоящий HTTP/worker-сценарий скачивания независимых PDF и DOCX во всех девяти парах форматов."""

import io
import shutil
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.bootstrap import build_service
from app.domain.models import DocumentError
from app.infrastructure.word import LibreOffice
from app.web.api import create_app
from tests.factories import docx, pdf
from tests.fakes import RecordingPublisher


@pytest.mark.parametrize(
    "old_format,new_format",
    [(a, b) for a in ("doc", "docx", "pdf") for b in ("doc", "docx", "pdf")],
)
def test_all_format_pairs_provide_pdf_and_independent_editable_docx(
    settings, tmp_path, old_format, new_format
):
    """Для каждого входного Word доступны оба формата; для PDF — только его два варианта видимости."""
    if (old_format != "pdf" or new_format != "pdf") and not shutil.which(settings.soffice_command):
        pytest.skip("Требуется LibreOffice, включённый в штатный Linux-образ")
    office = LibreOffice(settings)
    files = {}
    for side, kind, amount in (("old", old_format, "70"), ("new", new_format, "50")):
        text = f"2.2. Advance payment is {amount}%."
        source = (
            pdf(tmp_path / f"{side}.pdf", [text])
            if kind == "pdf"
            else docx(tmp_path / f"{side}.docx", [text])
        )
        if kind == "doc":
            source = office.convert(source, tmp_path / f"import-{side}", "doc", lambda stage: None)
        files[side] = (f"{side}.{kind}", source.read_bytes())
    service = build_service(settings)
    service.publisher = RecordingPublisher()
    with TestClient(create_app(settings, service)) as client:
        client.get("/")
        client.headers["X-Requested-With"] = "ContractDiff"
        response = client.post("/api/v1/comparisons", files=files)
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        service.process(job_id)
        base = f"/api/v1/comparisons/{job_id}"
        summary = client.get(base + "/result").json()
        for side, kind in (("old", old_format), ("new", new_format)):
            for show in ("true", "false"):
                response = client.get(f"{base}/export/{side}?format=pdf&show_unverified={show}")
                assert response.status_code == 200, response.text
                assert len(PdfReader(io.BytesIO(response.content)).pages) == 1
            words = [e for e in summary["exports"][side] if e["format"] == "docx"]
            if kind == "pdf":
                assert words == []
                assert client.get(f"{base}/export/{side}?format=docx").status_code == 409
                continue
            assert len(words) == 1 and words[0]["available"]
            assert words[0]["marked_changes"] == 1 and words[0]["unmarked_changes"] == 0
            a = client.get(f"{base}/export/{side}?format=docx&show_unverified=true")
            b = client.get(f"{base}/export/{side}?format=docx&show_unverified=false")
            assert a.status_code == b.status_code == 200
            assert a.content == b.content
            assert (
                a.headers["content-type"]
                == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            assert a.headers["x-unmarked-changes"] == "0"
            with ZipFile(io.BytesIO(a.content)) as document:
                xml = document.read("word/document.xml")
                assert b"D4D8DE" in xml and b"FFF0D0" not in xml and b"E5D9FF" not in xml
            assert client.get(f"{base}/export/{side}?format=doc").status_code == 422


def test_docx_download_remains_available_after_pdf_shading_failure(settings, tmp_path, monkeypatch):
    """HTTP-доступность DOCX не зависит от успешности PDF-разметки той же редакции."""
    if not shutil.which(settings.soffice_command):
        pytest.skip("Требуется LibreOffice")

    def fail(*args, **kwargs):
        """Отклоняет только выходной PDF, сохраняя работу извлечения исходного Word."""
        raise DocumentError("Сбой PDF-разметки")

    monkeypatch.setattr("app.infrastructure.exports._pdf_export", fail)
    service = build_service(settings)
    service.publisher = RecordingPublisher()
    files = {
        side: (
            side + ".docx",
            docx(tmp_path / f"{side}.docx", [f"2.2. Advance payment is {amount}%."]).read_bytes(),
        )
        for side, amount in (("old", "70"), ("new", "50"))
    }
    with TestClient(create_app(settings, service)) as client:
        client.get("/")
        client.headers["X-Requested-With"] = "ContractDiff"
        response = client.post("/api/v1/comparisons", files=files)
        job_id = response.json()["id"]
        service.process(job_id)
        for side in ("old", "new"):
            assert (
                client.get(f"/api/v1/comparisons/{job_id}/export/{side}?format=pdf").status_code
                == 409
            )
            assert (
                client.get(f"/api/v1/comparisons/{job_id}/export/{side}?format=docx").status_code
                == 200
            )
