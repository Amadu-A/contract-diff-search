# tests/regression/test_comparison_exclusions.py
"""Только соответствующие целые значения исключаются до статистики и адресного плана ИИ."""

import pytest

from app.application.ai_evidence import merge_reply
from app.application.ai_planning import full_plan, unverified_plan
from app.bootstrap import build_service
from app.domain.comparison import compare, statistics
from app.domain.models import AIObservation, AIReply, ReviewUnit
from tests.factories import version


@pytest.mark.parametrize("before,after", [("-", "—"), ("-", "-"), ("—", "—"), ("2.2. -", "2.2. —")])
def test_corresponding_placeholders_are_unchanged_despite_low_ocr(before, after):
    """Однозначное целое значение не создаёт ложных различий и низкоуверенных областей."""
    old, new = version([before]), version([after], "pdf")
    for document in (old, new):
        document.elements[0].confidence = 0.2
        document.pages[0].mode = "scanned"
    result = compare(old, new, exclusions=frozenset({"-", "—"}))
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
        ("Санкт-Петербург", "Санкт—Петербург"),
        ("-5", "—5"),
        ("01-02-2026", "01—02—2026"),
        ("10 - 20", "10 — 20"),
    ],
)
def test_meaningful_text_and_internal_dashes_are_not_excluded(before, after):
    """Исключение не удаляет содержательные правки, дефисы, знаки чисел и диапазоны."""
    old, new = version([before]), version([after])
    result = compare(old, new, exclusions=frozenset({"-", "—"}))
    assert result.differences
    assert not any(match.method == "comparison_exclusion" for match in result.matches)


def test_placeholders_do_not_cross_sections_or_ambiguous_positions():
    """Одинаковый символ не является основанием объединять разные пункты или повторяющиеся ячейки."""
    for before, after in [(["2.1. -"], ["3.1. —"]), (["-", "-"], ["—", "—"])]:
        result = compare(version(before), version(after), exclusions=frozenset({"-", "—"}))
        assert not any(match.method == "comparison_exclusion" for match in result.matches)
        assert result.differences


def test_placeholder_between_same_anchors_is_resolved():
    """Пара без номера определяется соседними содержательными опорами, даже при вставке страницы."""
    result = compare(
        version(["Начало раздела", "-", "Конец раздела"]),
        version(["Начало раздела", "—", "Конец раздела"], "pdf", 1),
        exclusions=frozenset({"-", "—"}),
    )
    assert result.differences == []
    assert len([match for match in result.matches if match.method == "comparison_exclusion"]) == 1


def test_file_can_extend_and_disable_exclusions_without_python_edits(settings, tmp_path):
    """Настройки передают пользовательский UTF-8-файл в существующий сценарий; пустой файл отключает список."""
    path = tmp_path / "exclusions.txt"
    path.write_text("\ufeff-\n—\nн/д\nне применимо\n\n", encoding="utf-8")
    settings.comparison_exclusions_file = path
    service = build_service(settings)
    assert service.limits.comparison_exclusions == frozenset({"-", "—", "н/д", "не применимо"})
    result = compare(
        version(["2.2. н/д"]),
        version(["2.2. не применимо"]),
        exclusions=service.limits.comparison_exclusions,
    )
    assert result.differences == []
    path.write_text("", encoding="utf-8")
    assert build_service(settings).limits.comparison_exclusions == frozenset()


def test_full_page_ai_cannot_restore_the_excluded_placeholder_change():
    """Соседняя страничная проверка не возвращает ложную правку уже разрешённой пары значений."""
    result = compare(version(["-"]), version(["—"], "pdf"), exclusions=frozenset({"-", "—"}))
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
    result = compare(version([before]), version([after], "pdf"), exclusions=frozenset({"-", "—"}))
    assert result.differences == []
    assert result.comparison_exclusions == ["-", "—"]
    assert unverified_plan(result, 3600, 6) == []


def test_real_dash_style_can_be_added_without_ignoring_neighboring_word_change():
    """Добавление короткого тире снимает только пунктуационную неопределённость, сохраняя изменение текста."""
    old, new = (
        version(["2.2. Услуги (далее – Работы) на объекте."]),
        version(["2.2. Услуги (далее — Работы) на складе."], "pdf"),
    )
    result = compare(old, new, exclusions=frozenset({"-", "—", "–"}))
    assert result.differences
    assert not any(d.old_text == "–" and d.new_text == "—" for d in result.differences)
    assert any("объекте" in d.old_text and "складе" in d.new_text for d in result.differences)


@pytest.mark.parametrize(
    "before,after",
    [
        ("-5", "-6"),
        ("70%", "50%"),
        ("01-02-2026", "01-03-2026"),
        ("Санкт-Петербург", "Санкт—Петербург"),
        ("10 - 20", "10 — 20"),
    ],
)
def test_expanding_file_cannot_disable_numbers_dates_or_internal_hyphens(before, after):
    """Даже явная строка списка не превращает содержательные числовые значения и составные слова в совпадение."""
    result = compare(version([before]), version([after]), exclusions=frozenset({before, after}))
    assert result.differences
    assert not any(match.method == "comparison_exclusion" for match in result.matches)
