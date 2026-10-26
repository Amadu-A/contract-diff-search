# tests/regression/test_ocr_failure.py
"""Регрессия потери всей страницы при недоступном повторном OCR числа."""

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
