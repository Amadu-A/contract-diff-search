# tests/integration/test_ai_geometry.py
"""Адресная геометрия готового Word-рендера: точная уникальная цитата и отказ при повторении."""

from copy import deepcopy

from app.domain.comparison import compare
from app.infrastructure.ai_geometry import PdfGeometryRefiner
from app.infrastructure.ocr import TesseractOCR
from app.infrastructure.pdf import PdfDocumentExtractor
from app.infrastructure.word import LibreOffice
from tests.factories import pdf


def test_refiner_recovers_only_unique_native_quote_without_ocr_or_comparison(settings, tmp_path):
    """Две одинаковые метки MP не подменяют друг друга; уникальная полная фраза получает реальные координаты."""
    source = pdf(
        tmp_path / "render.pdf", ["12. Unique delivery conditions and address 12345.", "MP", "MP"]
    )
    old = PdfDocumentExtractor(settings, TesseractOCR(settings), LibreOffice(settings)).extract(
        source, source.name, tmp_path / "old", False, lambda stage: None
    )
    new = deepcopy(old)
    old.format = "docx"
    for element in old.elements:
        element.words = []
        element.confidence = 0.64
        element.attributes["geometry_coverage"] = "0"
    result = compare(old, new)
    before = [(d.id, d.status, d.active) for d in result.differences]
    PdfGeometryRefiner().refine(result, lambda stage: None)
    unique = next(e for e in old.elements if "Unique" in e.text)
    assert unique.words and all(word.verified for word in unique.words)
    assert 50 < unique.words[0].region.y0 < 100
    assert all(not e.words for e in old.elements if e.text == "MP")
    assert [(d.id, d.status, d.active) for d in result.differences] == before
    assert any(d.old_element == unique.id and d.old_regions for d in result.differences)
