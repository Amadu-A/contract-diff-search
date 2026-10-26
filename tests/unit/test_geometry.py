# tests/unit/test_geometry.py
"""Обратные преобразования координат и минимальная посимвольная подсветка."""

import pytest

from app.domain.comparison import compare
from app.domain.models import VisualRegion, Word
from app.infrastructure.ocr import original_box
from tests.factories import version


def test_inverse_quarter_turn_preserves_original_coordinates():
    """Поворот 90° с расширением холста возвращается в исходную систему скана."""
    assert original_box((20, 180, 30, 190), 90, 100, 200, 200, 100) == pytest.approx(
        (10, 20, 20, 30)
    )


def test_native_single_character_change_has_minimal_region():
    """При замене одной буквы нативные границы не расширяются до целого абзаца."""
    a, b = version(["2.2. кот"]), version(["2.2. кит"])
    for version_value in (a, b):
        element = version_value.elements[0]
        element.words[-1] = Word(
            element.text[-3:],
            5,
            8,
            VisualRegion(1, 50, 60, 80, 70),
            characters=tuple(VisualRegion(1, 50 + i * 10, 60, 60 + i * 10, 70) for i in range(3)),
        )
    changes = compare(a, b).differences
    assert changes[0].old_range == (6, 7)
    assert changes[0].old_regions == [VisualRegion(1, 60, 60, 70, 70)]
