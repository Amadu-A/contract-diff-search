# tests/regression/test_comparison_exclusions.py
"""Группы эквивалентности сохраняют реальные изменения, исходные диапазоны и адресный план ИИ."""

from dataclasses import asdict, replace
from itertools import product

import pytest

from app.application.ai_evidence import merge_reply
from app.application.ai_planning import full_plan, unverified_plan
from app.bootstrap import build_service, load_exclusions
from app.domain.comparison import compare, statistics
from app.domain.models import AIObservation, AIReply, ReviewUnit, Word
from app.domain.text import comparison_tokens
from tests.factories import version

GROUPS = (("-", "—", "–"),)


@pytest.mark.parametrize(
    "before,after", list(product(("-", "—", "–"), repeat=2)) + [("2.2. -", "2.2. —")]
)
def test_corresponding_placeholders_are_unchanged_despite_low_ocr(before, after):
    """Однозначное целое значение не создаёт ложных различий и низкоуверенных областей."""
    old, new = version([before]), version([after], "pdf")
    for document in (old, new):
        document.elements[0].confidence = 0.2
        document.pages[0].mode = "scanned"
    result = compare(old, new, exclusions=GROUPS)
    assert result.differences == []
    assert set(result.coverage.old.values()) == set(result.coverage.new.values()) == {"unchanged"}
    assert statistics(result.differences)["unverified"] == 0
    assert unverified_plan(result, 3600, 6) == []
    assert not any(
        unit.kind != "page" and (unit.old_elements or unit.new_elements)
        for unit in full_plan(result, 3600, 6)
    )


@pytest.mark.parametrize(
    "before,after",
    [
        ("-", "Срок 30 дней"),
    ],
)
def test_meaningful_text_is_not_excluded(before, after):
    """Группа не удаляет содержательный текст на противоположной стороне."""
    old, new = version([before]), version([after])
    result = compare(old, new, exclusions=GROUPS)
    assert result.differences
    assert not any(match.method == "comparison_exclusion" for match in result.matches)


def test_placeholders_do_not_cross_sections_or_ambiguous_positions():
    """Одинаковый символ не является основанием объединять разные пункты или повторяющиеся ячейки."""
    for before, after in [(["2.1. -"], ["3.1. —"]), (["-", "-"], ["—", "—"])]:
        result = compare(version(before), version(after), exclusions=GROUPS)
        assert not any(match.method == "comparison_exclusion" for match in result.matches)
        assert result.differences


def test_placeholder_between_same_anchors_is_resolved():
    """Пара без номера определяется соседними содержательными опорами, даже при вставке страницы."""
    result = compare(
        version(["Начало раздела", "-", "Конец раздела"]),
        version(["Начало раздела", "—", "Конец раздела"], "pdf", 1),
        exclusions=GROUPS,
    )
    assert result.differences == []
    assert len([match for match in result.matches if match.method == "comparison_exclusion"]) == 1


def test_file_can_extend_and_disable_exclusions_without_python_edits(settings, tmp_path):
    """Настройки передают пользовательский UTF-8-файл в существующий сценарий; пустой файл отключает список."""
    path = tmp_path / "exclusions.txt"
    path.write_text("\ufeff [-, —, –] \n\n [н/д, не применимо] \n", encoding="utf-8")
    settings.comparison_exclusions_file = path
    service = build_service(settings)
    assert service.limits.comparison_exclusions == (*GROUPS, ("н/д", "не применимо"))
    result = compare(
        version(["2.2. н/д"]),
        version(["2.2. не применимо"]),
        exclusions=service.limits.comparison_exclusions,
    )
    assert result.differences == []
    path.write_text("", encoding="utf-8")
    assert build_service(settings).limits.comparison_exclusions == ()


def test_full_page_ai_cannot_restore_the_excluded_placeholder_change():
    """Соседняя страничная проверка не возвращает ложную правку уже разрешённой пары значений."""
    result = compare(version(["-"]), version(["—"], "pdf"), exclusions=GROUPS)
    unit = ReviewUnit(
        "page",
        "full",
        "page",
        old_elements=[result.old.elements[0].id],
        new_elements=[result.new.elements[0].id],
        old_regions=[result.old.elements[0].words[0].region],
        new_regions=[result.new.elements[0].words[0].region],
    )
    reply = AIReply(
        "changed",
        True,
        findings=[
            AIObservation(
                "text_modified", "-", "—", "o0", "n0", old_quote="-", new_quote="—", confidence=1
            )
        ],
    )
    merge_reply(result, unit, reply)
    assert result.differences == []


@pytest.mark.parametrize(
    "before,after",
    [
        ("2.2. Услуги (далее - Работы).", "2.2. Услуги (далее — Работы)."),
        ("2.2. Поле: -", "2.2. Поле: —"),
    ],
)
def test_corresponding_standalone_fragments_inside_same_paragraph(before, after):
    """Исключается отдельный фрагмент текущей пары абзацев, а не весь содержательный пункт."""
    result = compare(version([before]), version([after], "pdf"), exclusions=GROUPS)
    assert result.differences == []
    assert result.comparison_exclusions == [list(GROUPS[0])]
    assert unverified_plan(result, 3600, 6) == []


def test_real_dash_style_can_be_added_without_ignoring_neighboring_word_change():
    """Добавление короткого тире снимает только пунктуационную неопределённость, сохраняя изменение текста."""
    old, new = (
        version(["2.2. Услуги (далее – Работы) на объекте."]),
        version(["2.2. Услуги (далее — Работы) на складе."], "pdf"),
    )
    result = compare(old, new, exclusions=GROUPS)
    assert result.differences
    assert not any(d.old_text == "–" and d.new_text == "—" for d in result.differences)
    assert any("объекте" in d.old_text and "складе" in d.new_text for d in result.differences)


@pytest.mark.parametrize(
    "before,after",
    [
        ("-5", "-6"),
        ("70%", "50%"),
        ("01-02-2026", "01-03-2026"),
    ],
)
def test_ungrouped_numbers_and_dates_are_preserved(before, after):
    """Числа и даты вне группы остаются самостоятельными изменениями."""
    result = compare(version([before]), version([after]), exclusions=GROUPS)
    assert result.differences
    assert not any(match.method == "comparison_exclusion" for match in result.matches)


@pytest.mark.parametrize(
    "before,after",
    [
        ("Санкт-Петербург", "Санкт—Петербург"),
        ("Ростов-на-Дону", "Ростов–на–Дону"),
        ("далее - Исполнитель", "далее — Исполнитель"),
        ("10 - 20", "10 — 20"),
        ("-5", "—5"),
        ("01-02-2026", "01—02—2026"),
        ("Санкт-Петербург", "Санкт — Петербург"),
    ],
)
def test_equivalent_characters_inside_text_are_unchanged(before, after):
    """Настроенные замены и OCR-пробелы не создают событий; исходная модель не изменяется."""
    old, new = version(["2.2. " + before]), version(["2.2. " + after], "pdf")
    new.pages[0].mode = "scanned"
    original = asdict(old), asdict(new)
    result = compare(old, new, exclusions=GROUPS)
    assert result.differences == []
    assert result.coverage.new == {"E000000": "unchanged"}
    assert unverified_plan(result, 3600, 6) == []
    assert original == (asdict(old), asdict(new))


@pytest.mark.parametrize(
    "before,after",
    [
        ("70%", "50%"),
        ("Санкт-Петербург", "Санкт—Петербур"),
        ("срок - 10 дней", "срок — 20 дней"),
        ("-", "н/д"),
        ("н/д", "не применимо"),
    ],
)
def test_meaningful_changes_next_to_equivalent_sign_are_preserved(before, after):
    """Правило замен не распространяется на соседний текст или значения другой группы."""
    result = compare(version(["2.2. " + before]), version(["2.2. " + after]), exclusions=GROUPS)
    assert result.differences
    assert statistics(result.differences)["total"] > 0


def test_split_ocr_tokens_and_low_confidence_sign_keep_exact_number_ranges():
    """Сомнительный эквивалентный знак не загрязняет надёжное изменение числа и его геометрию."""
    old = version(["2.2. Санкт-Петербург: срок - 10 дней"])
    new = version(["2.2. Санкт—Петербург: срок — 20 дней"], "pdf")
    element = new.elements[0]
    city = next(w for w in element.words if "Петербург" in w.text)
    element.words.remove(city)
    for value, start, end in [
        ("Санкт", city.start, city.start + 5),
        ("—", city.start + 5, city.start + 6),
        ("Петербург:", city.start + 6, city.end),
    ]:
        element.words.append(Word(value, start, end, city.region))
    element.words = [
        replace(w, confidence=0.2, verified=False) if w.text == "—" else w for w in element.words
    ]
    element.confidence = 0.4
    new.pages[0].mode = "scanned"
    result = compare(old, new, exclusions=GROUPS)
    assert len(result.differences) == 1
    change = result.differences[0]
    assert (change.old_text, change.new_text, change.status) == ("10", "20", "confirmed")
    for side, text in (("old", "10"), ("new", "20")):
        document = getattr(result, side)
        assert document.elements[0].text[slice(*getattr(change, side + "_range"))] == text[0]
        expected = next(w.region for w in document.elements[0].words if w.text == text)
        assert getattr(change, side + "_regions") == [expected]
    assert statistics(result.differences)["unverified"] == 0


def test_low_confidence_outside_equivalent_signs_remains_unverified():
    """Сомнительное чтение числа сохраняется при эквивалентном дефисе."""
    old, new = version(["2.2. Срок - 10 дней"]), version(["2.2. Срок — 10 дней"], "pdf")
    new.elements[0].confidence = 0.2
    new.elements[0].words = [
        replace(w, confidence=0.2) if w.text == "10" else w for w in new.elements[0].words
    ]
    assert statistics(compare(old, new, exclusions=GROUPS).differences)["unverified"] > 0


def test_page_ocr_warning_only_about_resolved_signs_does_not_create_a_target():
    """Общее предупреждение страницы не возвращает уже разрешённую пунктуационную неопределённость."""
    old, new = version(["-"]), version(["—"], "pdf")
    new.elements[0].words = [
        replace(w, confidence=0.2, verified=False) for w in new.elements[0].words
    ]
    new.elements[0].confidence = 0.2
    new.pages[0].warnings = ["Значительная часть OCR имеет низкую достоверность"]
    result = compare(old, new, exclusions=GROUPS)
    assert result.differences == []
    assert unverified_plan(result, 3600, 6) == []
    new.pages[0].warnings.append("OCR не распознал содержимое страницы")
    assert statistics(compare(old, new, exclusions=GROUPS).differences)["unverified"] == 1


def test_equivalent_row_labels_do_not_prevent_corresponding_cell_comparison():
    """Тире в названии строки канонизируется без объединения разных столбцов."""
    old, new = version(["2.2. -"]), version(["2.2. —"], "pdf")
    for document, label in ((old, "Санкт-Петербург"), (new, "Санкт—Петербург")):
        document.elements[0].kind = "cell"
        document.elements[0].attributes = {"column": "1", "row_key": label}
    assert compare(old, new, exclusions=GROUPS).differences == []
    new.elements[0].attributes["column"] = "2"
    assert compare(old, new, exclusions=GROUPS).differences


@pytest.mark.parametrize(
    "line", ["-", "[]", "[-]", "[-,]", "[-, -]", "[-, —", "[-, [—]]", "[-, —]\n[—, –]"]
)
def test_invalid_group_syntax_has_file_and_line_error(settings, tmp_path, line):
    """Ошибочная конфигурация не превращается в молчаливое отключение проверки."""
    path = tmp_path / "exclusions.txt"
    path.write_text("\n" + line, encoding="utf-8")
    settings.comparison_exclusions_file = path
    with pytest.raises(ValueError, match=r"exclusions\.txt: строка [23]: ожидается группа"):
        load_exclusions(settings)


def test_multicharacter_groups_are_independent_and_keep_source_offsets():
    """Группы раздельны; замена переменной длины не сдвигает реальное число после неё."""
    groups = (*GROUPS, ("н/д", "не применимо"))
    old, new = (
        version(["2.2. Поле н/д, срок 10 дней"]),
        version(["2.2. Поле не применимо, срок 25 дней"]),
    )
    result = compare(old, new, exclusions=groups)
    assert len(result.differences) == 1
    change = result.differences[0]
    assert (change.old_text, change.new_text) == ("10", "25")
    assert new.elements[0].text[slice(*change.new_range)] == "25"
    assert comparison_tokens("не применимо", groups)[-1][2] == len("не применимо")
    independent = compare(version(["2.2. -"]), version(["2.2. н/д"]), exclusions=groups)
    assert independent.differences


def test_multicharacter_replacement_does_not_create_false_word_formatting_change():
    """Одинаковый стиль текста не становится изменением оформления из-за длины эквивалентной записи."""
    from app.domain.models import TextRun

    old, new = (
        version(["2.2. Поле н/д, срок 10 дней"]),
        version(["2.2. Поле не применимо, срок 10 дней"]),
    )
    for document in (old, new):
        element = document.elements[0]
        element.runs = [TextRun(0, len(element.text), (("font", "Arial"),))]
    groups = (*GROUPS, ("н/д", "не применимо"))
    assert compare(old, new, exclusions=groups).differences == []


@pytest.mark.parametrize(
    "before,after", [("Санкт-Петербург", "Санкт—Петербург"), ("10 - 20", "10 — 20")]
)
def test_full_page_ai_does_not_restore_excluded_inline_changes_without_anchor(before, after):
    """Полный VLM-проход не возвращает исключённую замену даже при неразрешённой ссылке модели."""
    result = compare(version([before]), version([after], "pdf"), exclusions=GROUPS)
    unit = ReviewUnit("page", "full", "page")
    reply = AIReply(
        "changed",
        True,
        findings=[
            AIObservation("text_modified", before, after, "invalid", "invalid", confidence=1)
        ],
    )
    merge_reply(result, unit, reply)
    assert result.differences == []


def test_saved_result_keeps_groups_and_restores_legacy_snapshot(settings):
    """Возобновление ИИ использует сохранённые группы; старый плоский снимок также читается."""
    import json

    from app.infrastructure.storage import FileStorage

    storage = FileStorage(settings)
    job = "5a7a4969-c058-4c76-bfe1-56a875e75750"
    storage.directory(job).mkdir(parents=True)
    result = compare(version(["-"]), version(["—"]), exclusions=GROUPS)
    storage.write_result(job, result)
    assert storage.restore_result(job).comparison_exclusions == [list(GROUPS[0])]
    data = storage.read_result(job)
    data["comparison_exclusions"] = ["-", "—"]
    (storage.directory(job) / "result.json").write_text(json.dumps(data), encoding="utf-8")
    assert storage.restore_result(job).comparison_exclusions == [["-", "—"]]
