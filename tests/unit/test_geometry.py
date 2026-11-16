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


def test_missing_side_navigation_uses_neighbor_anchors_and_never_invents_coordinates():
    """Удаление на старой странице 6 ведёт к новой 4 по двум опорам; конфликт остаётся явным."""
    from dataclasses import asdict, replace

    from app.application.queries import correspondence
    from app.domain.comparison import compare
    from tests.factories import version

    old = version(
        [
            "8.1. Предыдущий длинный неизменный пункт договора.",
            "8.2. Удаляемое отдельное существенное условие.",
            "8.3. Следующий длинный неизменный пункт договора.",
        ]
    )
    new = version([old.elements[0].text, old.elements[2].text], "pdf")
    for doc, page in ((old, 6), (new, 4)):
        for element in doc.elements:
            element.words = [replace(w, region=replace(w.region, page=page)) for w in element.words]
    data = asdict(compare(old, new))
    difference = next(d for d in data["differences"] if d["category"] == "text_deleted")
    assert difference["new_regions"] == []
    assert correspondence(data, "old", 6, difference["id"]) == {
        "page": 4,
        "method": "neighbor_anchors",
    }
    for word in data["new"]["elements"][1]["words"]:
        word["region"]["page"] = 3
    assert correspondence(data, "old", 6, difference["id"])["page"] is None
    assert correspondence(data, "old", 6, "unknown")["page"] is None


def test_graphic_without_text_id_uses_existing_page_correspondence():
    """Графика с реальной областью использует однозначное соответствие страницы, без фиктивного текста."""
    from dataclasses import asdict

    from app.application.queries import correspondence
    from app.domain.models import Difference

    result = compare(
        version(["1. Надёжный текст соответствующей страницы."]),
        version(["1. Надёжный текст соответствующей страницы."], "pdf"),
    )
    result.differences.append(
        Difference(
            "G",
            "graphic_added",
            "Печать",
            new_regions=[VisualRegion(1, 50, 700, 100, 750)],
            new_element="graphic-1",
        )
    )
    data = asdict(result)
    assert correspondence(data, "new", 1, "G")["page"] == 1
    assert not result.differences[-1].old_regions
    assert correspondence(data, "new", 1, "unknown")["page"] is None
