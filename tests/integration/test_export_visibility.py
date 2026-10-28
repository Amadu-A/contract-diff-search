# tests/integration/test_export_visibility.py
"""Два варианта PDF/DOCX: предупреждения скрываются, подтверждённые ИИ-факты имеют общую заливку."""

import hashlib
import shutil
from pathlib import Path
from zipfile import ZipFile

import pytest
from pypdf import PdfReader

from app.domain.comparison import compare, regions
from app.domain.models import Difference
from app.infrastructure.exports import DocumentExporter
from app.infrastructure.ocr import TesseractOCR
from app.infrastructure.pdf import PdfDocumentExtractor
from app.infrastructure.word import LibreOffice, read_docx
from tests.factories import docx, pdf


@pytest.mark.parametrize("kind", ["pdf", "docx"])
def test_exports_keep_confirmed_and_ai_markers_but_hide_only_warnings(settings, tmp_path, kind):
    """Разные ключи экспорта не смешиваются; исходники, текст и вёрстка остаются проверенными."""
    if kind == "docx" and not shutil.which(settings.soffice_command):
        pytest.skip("Для проверки DOCX нужен LibreOffice")
    create = docx if kind == "docx" else pdf
    paths = [
        create(tmp_path / f"{side}.{kind}", [f"2.2. Advance payment is {value}%."])
        for side, value in [("old", "70"), ("new", "50")]
    ]
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    office = LibreOffice(settings)
    extractor = PdfDocumentExtractor(settings, TesseractOCR(settings), office)
    versions = [
        extractor.extract(p, p.name, tmp_path / side, False, lambda stage: None)
        for p, side in zip(paths, ("old", "new"), strict=True)
    ]
    result = compare(*versions)
    result.differences[0].source = "ai"
    result.differences[0].verified_by = "ai_visual"
    for identifier, status, source, value in [
        ("warning", "unverified", "deterministic", "Advance"),
        ("ai", "needs_review", "ai", "payment"),
    ]:
        a, b = [next(e for e in v.elements if value in e.text) for v in versions]
        spans = [(e.text.index(value), e.text.index(value) + len(value)) for e in (a, b)]
        result.differences.append(
            Difference(
                identifier,
                "unverified_region" if source == "deterministic" else "text_modified",
                "Синтетическая пометка",
                value,
                value,
                old_element=a.id,
                new_element=b.id,
                old_range=spans[0],
                new_range=spans[1],
                old_regions=regions(a, *spans[0]),
                new_regions=regions(b, *spans[1]),
                status=status,
                source=source,
            )
        )
    artifacts = DocumentExporter(office).export(result, tmp_path, lambda stage: None)
    for side in ("old", "new"):
        for show in (True, False):
            artifact = next(
                a for a in artifacts[side] if a.format == "pdf" and a.show_unverified == show
            )
            document = PdfReader(artifact.path)
            colors = {
                tuple(float(n) for n in args)
                for args, operator in document.pages[0].get_contents().operations
                if operator in (b"RG", b"rg")
            }
            assert ((0.85, 0.5, 0.05) in colors) == show
            assert (0.48, 0.25, 0.8) in colors and (0.42, 0.46, 0.51) in colors
            if kind == "docx":
                word = next(
                    a for a in artifacts[side] if a.format == "docx" and a.show_unverified == show
                )
                assert word.path, word.reason
                with ZipFile(word.path) as archive:
                    xml = archive.read("word/document.xml")
                assert (b"FFF0D0" in xml) == show
                assert b"E5D9FF" in xml and b"D4D8DE" in xml
                assert [e.text for e in read_docx(Path(word.path))] == [
                    e.text for e in read_docx(tmp_path / side / "accepted.docx")
                ]
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths] == hashes


@pytest.mark.parametrize("status", ["needs_review", "confirmed"])
def test_ai_pdf_fill_is_reserved_for_independently_confirmed_findings(settings, tmp_path, status):
    """Гипотеза имеет только контур; подтверждённое ИИ-изменение — обычную серую заливку."""
    extractor = PdfDocumentExtractor(settings, TesseractOCR(settings), LibreOffice(settings))
    models = [
        extractor.extract(
            pdf(tmp_path / f"{side}.pdf", [f"2.2. Advance payment is {value}%."]),
            f"{side}.pdf",
            tmp_path / side,
            False,
            lambda stage: None,
        )
        for side, value in [("old", "70"), ("new", "50")]
    ]
    result = compare(*models)
    result.differences = [next(d for d in result.differences if d.category == "number_modified")]
    result.differences[0].source = "ai"
    result.differences[0].status = status
    artifacts = DocumentExporter(LibreOffice(settings)).export(result, tmp_path, lambda stage: None)
    operations = PdfReader(artifacts["new"][0].path).pages[0].get_contents().operations
    operators = [operator for _, operator in operations]
    colors = {tuple(float(n) for n in args) for args, operator in operations if operator == b"rg"}
    assert ((0.42, 0.46, 0.51) in colors) == (status == "confirmed")
    assert (b"f*" in operators or b"f" in operators) == (status == "confirmed")
    assert (b"S" in operators) == (status == "needs_review")
