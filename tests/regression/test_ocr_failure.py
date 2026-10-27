# tests/regression/test_ocr_failure.py
"""Регрессия потери всей страницы при недоступном повторном OCR числа."""

import pytest
from PIL import Image

from app.domain.comparison import compare
from app.domain.models import DocumentError, DocumentVersion, Page
from app.infrastructure.ocr import TesseractOCR
from tests.factories import version


def test_failed_secondary_number_read_preserves_primary_text(settings, tmp_path):
    """Отказ дополнительного чтения понижает доверие только к числу, не стирая страницу."""
    ocr = TesseractOCR(settings)

    def read(image, output, psm, checkpoint, language=None):
        """Возвращает основной OCR и воспроизводит отказ второго языкового прохода."""
        if psm == 7:
            raise DocumentError("Нет языка")
        return [
            {
                "text": "70%",
                "box": (5, 5, 50, 20),
                "confidence": 0.95,
                "verified": True,
                "line": (1, 1, 1),
                "paragraph": (1, 1),
            }
        ]

    ocr._read = read
    with Image.new("RGB", (100, 100), "white") as image:
        rows = ocr.recognize(image, tmp_path / "ocr", lambda stage: None)
    assert rows[0]["text"] == "70%"
    assert not rows[0]["verified"] and rows[0]["confidence"] < 0.65


def test_unread_counterpart_does_not_confirm_deletion():
    """Отсутствующий текст скана не становится доказанным удалением всего договора."""
    old = version(["2.2. Аванс 70%"])
    new = DocumentVersion(
        "scan.pdf", "pdf", "unused.pdf", [], [Page(1, 600, 850, "scanned", 0, ["OCR недоступен"])]
    )
    result = compare(old, new)
    assert not any(d.category == "text_deleted" for d in result.differences)
    assert result.coverage.old["E000000"] == "unverified"


def test_secondary_ocr_uses_same_grid_scale_at_far_right(settings, tmp_path):
    """Совпавший OCR справа в странице не теряется из-за разных масштабов пространственного индекса."""
    ocr = TesseractOCR(settings)

    def read(image, output, psm, checkpoint, language=None):
        """Даёт одинаковые независимые слова в области с большими исходными координатами."""
        return [
            {
                "text": "Договор",
                "box": (900, 600, 1040, 630),
                "confidence": 0.95,
                "verified": True,
                "line": (1, 1, 1),
                "paragraph": (1, 1),
            }
        ]

    ocr._read = read
    with Image.new("RGB", (1400, 900), "white") as image:
        rows = ocr.recognize(image, tmp_path / "far-right", lambda stage: None)
    assert rows[0]["verified"] and rows[0]["confidence"] == 0.95


def test_number_recheck_does_not_overwrite_failed_letter_verification(settings, tmp_path):
    """Совпадение цифр не подтверждает разные буквы в смешанном OCR-токене."""
    ocr = TesseractOCR(settings)

    def read(image, output, psm, checkpoint, language=None):
        """Независимый проход расходится в букве, хотя повтор цифр успешен."""
        return [
            {
                "text": "B12345678" if psm == 6 else "A12345678",
                "box": (10, 10, 100, 30),
                "confidence": 0.95,
                "verified": True,
                "line": (1, 1, 1),
                "paragraph": (1, 1),
            }
        ]

    ocr._read = read
    with Image.new("RGB", (300, 100), "white") as image:
        rows = ocr.recognize(image, tmp_path / "mixed", lambda stage: None)
    assert not rows[0]["verified"]


@pytest.mark.parametrize(
    ("primary", "page_repeat", "local_repeat", "local_confidence", "verified"),
    [
        ("0,01%", "9,99%", "0,01%", 0.90, True),
        ("0,01%", "9,99%", "0,01%", 0.20, False),
        ("10", "11", "10", 0.95, True),
        ("10", "11", "O10", 0.95, False),
        ("70%", "70%", "O70%", 0.95, False),
        ("10", "11", "-10", 0.95, False),
        ("10", "11", "−10", 0.95, False),
        ("10", "11", "10.", 0.95, False),
        ("0,01%", "9,99%", "0.01%", 0.95, False),
        ("A12345678", "A12345678", "12345678", 0.95, True),
    ],
)
def test_local_number_requires_independent_literal_and_quality(
    settings, tmp_path, primary, page_repeat, local_repeat, local_confidence, verified
):
    """Локальное OCR подтверждает число без потери знака, букв и измеренной уверенности."""
    ocr = TesseractOCR(settings)

    def read(image, output, psm, checkpoint, language=None):
        """Разделяет первичное чтение, анализ страницы и независимый числовой фрагмент."""
        return [
            {
                "text": local_repeat if psm == 7 else page_repeat if psm == 6 else primary,
                "box": (10, 10, 100, 30),
                "confidence": local_confidence if psm == 7 else 0.92,
                "verified": True,
                "line": (1, 1, 1),
                "paragraph": (1, 1),
            }
        ]

    ocr._read = read
    with Image.new("RGB", (300, 100), "white") as image:
        rows = ocr.recognize(image, tmp_path / "local-number", lambda stage: None)
    assert rows[0]["text"] == primary
    assert rows[0]["verified"] is verified
    assert rows[0]["confidence"] == (
        min(0.92, local_confidence) if verified else min(0.64, local_confidence)
    )
