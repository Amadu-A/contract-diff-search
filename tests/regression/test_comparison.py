# tests/regression/test_comparison.py
"""Обезличенные регрессии критичных чисел, вставок, переносов и полноты."""

import pytest

from app.domain.comparison import compare, statistics
from app.domain.models import TextRun
from tests.factories import version


@pytest.mark.parametrize(
    "before,after",
    [
        ("10", "100"),
        ("0,01%", "0,1%"),
        ("31.12.2025", "30.11.2025"),
        ("250000 ₽", "270000 ₽"),
        ("вправе", "не вправе"),
        ("товар", "Товар"),
        ("да.", "да!"),
    ],
)
def test_critical_changes_are_not_similarity_equivalence(before, after):
    """Каждое критичное отличие обнаруживается независимо от высокой похожести."""
    result = compare(
        version([f"2.2. Поставщик {before} передаёт продукцию."]),
        version([f"2.2. Поставщик {after} передаёт продукцию."]),
    )
    assert result.differences
    assert result.coverage.old == {"E000000": "changed"}
    assert all(d.old_regions or d.new_regions for d in result.differences)


def test_two_percent_changes_do_not_touch_repeated_controls():
    """70% другого пункта и первая дневная ставка не подменяют изменённые вхождения."""
    a = version(
        [
            "2.2. Аванс составляет 70% стоимости.",
            "4.3.1. Аванс до 70% (семьдесят) процентов.",
            "5.2. Ставка 0,01% за день. Общая пеня не более 5% стоимости.",
        ]
    )
    b = version(
        [
            "2.2. Аванс составляет 50% стоимости.",
            "4.3.1. Аванс до 70% (пятьдесят) процентов.",
            "5.2. Ставка 0,01% за день. Общая пеня не более 0,01% стоимости.",
        ]
    )
    result = compare(a, b)
    changes = [(d.old_section, d.old_text, d.new_text) for d in result.differences]
    assert ("2.2", "70%", "50%") in changes
    assert ("5.2", "5%", "0,01%") in changes
    assert ("4.3.1", "семьдесят", "пятьдесят") in changes
    assert not any(section == "4.3.1" and "70" in old for section, old, _ in changes)
    cap = next(d for d in result.differences if d.old_section == "5.2")
    assert cap.old_range[0] > a.elements[2].text.index("0,01%")


def test_insertion_of_fifteen_pages_and_single_digit_in_hundred():
    """Вставка большого раздела не превращает хвост документа в замены."""
    texts = [
        f"{i}. Уникальный пункт документа содержит значение {1000 + i}." for i in range(1, 101)
    ]
    changed = texts.copy()
    changed[89] = changed[89].replace("1090", "1091")
    changed[50:50] = [f"Дополнение уникальное номер {i} к текущей редакции" for i in range(15)]
    result = compare(version(texts), version(changed))
    assert len([d for d in result.differences if d.category == "text_added"]) == 15
    assert len([d for d in result.differences if d.category == "number_modified"]) == 1
    assert not any(d.category == "text_deleted" for d in result.differences)


def test_movement_numbering_formatting_and_coverage():
    """Перемещения и нумерация сохраняются; оформление больше не порождает события."""
    a = version(
        [
            "1. Первый уникальный длинный абзац договора.",
            "2. Второй уникальный длинный абзац договора.",
        ]
    )
    b = version(
        [
            "7. Второй уникальный длинный абзац договора.",
            "8. Первый уникальный длинный абзац договора.",
        ]
    )
    a.elements[0].runs = [TextRun(0, len(a.elements[0].text), ())]
    b.elements[1].runs = [TextRun(0, len(b.elements[1].text), (("b", "true"),))]
    result = compare(a, b)
    categories = {d.category for d in result.differences}
    assert {"text_moved", "numbering_changed"} <= categories
    assert "formatting_changed" not in categories
    assert set(result.coverage.old) == {e.id for e in a.elements}
    assert set(result.coverage.new) == {e.id for e in b.elements}


def test_split_merge_is_structural_not_delete_add():
    """Разбиение неизменного текста на два абзаца отражается структурно."""
    a = version(
        ["Первое длинное предложение текста договора. Второе длинное предложение текста договора."]
    )
    b = version(
        [
            "Первое длинное предложение текста договора.",
            "Второе длинное предложение текста договора.",
        ]
    )
    assert [d.category for d in compare(a, b).differences] == ["structure_changed"]


def test_empty_ocr_and_low_confidence_never_prove_equality():
    """Пустое/сомнительное извлечение порождает непроверенную область."""
    a, b = version(["1. Значение 70%"]), version(["1. Значение 70%"])
    b.elements[0].confidence = 0.2
    result = compare(a, b)
    assert statistics(result.differences)["unverified"] > 0
    assert result.coverage.new["E000000"] == "unverified"


@pytest.mark.parametrize("format", ["docx", "doc", "pdf"])
def test_formatting_never_generates_new_differences(format):
    """Шрифт, заливка и отступы при полном равенстве текста не создают событий."""
    a, b = (
        version(["1. Условия поставки без изменений"], format),
        version(["1. Условия поставки без изменений"], format),
    )
    a.elements[0].runs = [TextRun(0, len(a.elements[0].text), (("b", "true"),))]
    b.elements[0].runs = [TextRun(0, len(b.elements[0].text), (("size", "11"),))]
    assert not compare(a, b).differences


def test_changed_clause_split_keeps_percentages_and_original_ranges():
    """Разделённый Word-пункт сопоставляется с PDF целиком, а диапазоны возвращаются в OOXML."""
    a = version(
        [
            "2.3. Условия начала работ:",
            "Предоплата 50% от цены договора в течение десяти рабочих дней после согласования макета.",
        ]
    )
    a.elements[1].section = "2.3"
    b = version(
        [
            "2.3. Условия начала работ: Предоплата 70% от цены договора в течение десяти рабочих дней после согласования макета."
        ],
        "pdf",
    )
    result = compare(a, b)
    assert not any(d.category in {"text_added", "text_deleted"} for d in result.differences)
    change = next(d for d in result.differences if d.category == "number_modified")
    assert (change.old_text, change.new_text) == ("50%", "70%")
    assert change.old_element == a.elements[1].id
    assert a.elements[1].text[slice(*change.old_range)] == "50%"


def test_exact_short_value_in_another_clause_is_not_a_counterpart():
    """Число в постороннем пункте не доказывает присутствие удалённого значения."""
    a, b = version(["1. 1115003009824"]), version(["9. 1115003009824"])
    result = compare(a, b)
    assert not result.matches
    assert {d.category for d in result.differences} == {"text_added", "text_deleted"}


def test_line_wraps_and_more_than_three_fragments_keep_content():
    """Перенос PDF-строки и дробление на пять абзацев не превращают содержание в удаление."""
    text = "Условия присутствия представителя подтверждают работоспособность обеих вывесок после подключения."
    a = version([text])
    b = version(
        [
            "Условия присутствия",
            "представителя подтверждают",
            "работо-\nспособность обеих",
            "вывесок после",
            "подключения.",
        ],
        "pdf",
    )
    result = compare(a, b)
    assert not result.differences
    assert set(result.coverage.new.values()) == {"unchanged"}


def test_requisite_group_and_correspondent_alias_do_not_hide_changed_accounts():
    """Разные записи одного реквизита равны только при полном совпадении всех цифр."""
    a = version(
        ["9. Реквизиты", "ИНН 5040119241/", "КПП 504001001", "кор. счет: 30101810300000000176"]
    )
    b = version(
        ["9. Реквизиты", "ИНН/КПП 5040119241/504001001", "К/счет: 30101810300000000176"], "pdf"
    )
    for doc in (a, b):
        for element in doc.elements[1:]:
            element.section = "9"
    assert not compare(a, b).differences
    b.elements[-1].text = "К/счет: 30101810300000000177"
    result = compare(a, b)
    assert any(d.category == "number_modified" for d in result.differences)


def test_low_confidence_inside_split_group_keeps_explicit_uncertainty():
    """Сомнительный второй фрагмент не становится проверенным за счёт надёжного первого."""
    a = version(["Первый длинный абзац договора. Второй длинный абзац договора."])
    b = version(["Первый длинный абзац договора.", "Второй длинный абзац договора."], "pdf")
    b.elements[1].confidence = 0.3
    result = compare(a, b)
    assert result.coverage.new[b.elements[1].id] == "unverified"
    assert any(d.status == "unverified" for d in result.differences)


@pytest.mark.parametrize("reverse", [False, True])
def test_exact_duplicates_in_either_version_require_context(reverse):
    """Два одинаковых кандидата одного пункта не дают произвольно подтверждённое добавление/удаление."""
    text = "3.1. Поставка продукции выполняется по письменному согласованию сторон договора."
    a, b = version([text, text]), version([text], "pdf")
    if reverse:
        a, b = b, a
    result = compare(a, b)
    assert not result.matches
    assert any(d.status == "ambiguous" for d in result.differences)
    assert not any(d.category in {"text_added", "text_deleted"} for d in result.differences)
