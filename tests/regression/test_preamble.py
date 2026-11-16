# tests/regression/test_preamble.py
"""Преамбула и другие ненумерованные блоки при разной сегментации и изменённом окружении."""

import pytest

from app.domain.comparison import compare, statistics
from tests.factories import version

COMMON = "совместно именуемые «Стороны», а по отдельности — «Сторона», заключили настоящий договор о нижеследующем:"
INTRO = "Общество «Пример», именуемое Подрядчиком, в лице директора Иванова, действующего на основании устава,"
REORDERED = "Общество «Пример», в лице директора Иванова, действующего на основании устава, именуемое Подрядчиком,"


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("blocks", [1, 3])
def test_unnumbered_common_tail_survives_changed_context_and_split_merge(reverse, blocks):
    """Общая часть не получает диапазонов изменений даже при перестановке вводных слов."""
    pieces = COMMON.split()
    tail = (
        [COMMON]
        if blocks == 1
        else [" ".join(pieces[:5]), " ".join(pieces[5:10]), " ".join(pieces[10:])]
    )
    a = version(["Договор сторон", INTRO, *tail, "1. Предмет договора", "1.1. Аванс 50% стоимости"])
    b = version(
        [
            "Договор сторон",
            REORDERED + " " + COMMON,
            "1. Предмет договора",
            "1.1. Аванс 70% стоимости",
        ],
        "pdf",
    )
    if reverse:
        a, b = b, a
    result = compare(a, b, exclusions=(("-", "—", "–"),))
    assert any(m.method == "context_split_merge" for m in result.matches)
    for side, doc in (("old", a), ("new", b)):
        for element in doc.elements:
            start = element.text.find("совместно")
            if element.text in tail:
                start = 0
            if start < 0:
                continue
            for d in result.differences:
                if getattr(d, side + "_element") == element.id:
                    span = getattr(d, side + "_range")
                    assert span and span[1] <= start, (side, d)
    assert any(
        d.category == "number_modified" and {d.old_text, d.new_text} == {"50%", "70%"}
        for d in result.differences
    )
    assert not any(COMMON in d.old_text + d.new_text for d in result.differences)
    assert statistics(result.differences)["unverified"] == 0


def test_same_phrase_in_another_section_does_not_cancel_real_deletion():
    """Копия в другом пункте не подменяет удалённую преамбулу."""
    a = version([COMMON, "1. Предмет договора", "2. " + COMMON])
    b = version(["1. Предмет договора", "2. " + COMMON], "pdf")
    result = compare(a, b)
    deletion = next(d for d in result.differences if d.category == "text_deleted")
    assert deletion.old_element == a.elements[0].id and deletion.old_text == COMMON
    assert not any(d.category == "text_added" for d in result.differences)


def test_real_addition_inside_merged_preamble_is_retained():
    """Сопоставление общей части не скрывает новое обязательство в том же блоке."""
    a = version([INTRO, COMMON])
    b = version(
        [INTRO + " " + COMMON + " Стороны также оплачивают доставку товара отдельно."], "pdf"
    )
    result = compare(a, b)
    assert any(
        "оплачивают доставку" in d.new_text and d.category == "text_added"
        for d in result.differences
    )
    assert not any(COMMON in d.old_text + d.new_text for d in result.differences)
