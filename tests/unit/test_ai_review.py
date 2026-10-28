# tests/unit/test_ai_review.py
"""Полнота планов ИИ, адресные доказательства, повторы и сохранение частичного результата."""

import json
import threading
import time
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from app.application.ai_evidence import merge_reply
from app.application.ai_planning import element_regions, full_plan, unverified_plan
from app.application.ai_review import AILimits, AIReviewCoordinator, public_progress
from app.domain.comparison import compare, statistics
from app.domain.models import (
    AIModes,
    AIObservation,
    AIReply,
    Cancelled,
    Difference,
    VerificationError,
)
from app.domain.visibility import uncertainty_ids
from tests.factories import version


class Reviewer:
    """Регистрирует все операции и реальную максимальную конкуренцию тестового порта."""

    def __init__(self, failing=(), retryable=False, delay=0):
        """Настраивает отказ выбранных стабильных единиц, не подменяя планирование."""
        self.calls, self.active, self.peak = [], 0, 0
        self.failing, self.retryable, self.delay = set(failing), retryable, delay
        self.lock = threading.Lock()

    def review(self, unit, old, new, attempt):
        """Возвращает неопределённость: успешный запрос не означает подтверждение содержания."""
        with self.lock:
            self.calls.append((unit.id, unit.mode, attempt))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            if unit.id in self.failing:
                raise VerificationError("http_error", http_status=503, retryable=self.retryable)
            return AIReply("uncertain", True)
        finally:
            with self.lock:
                self.active -= 1


def uncertain_result(count=3):
    """Создаёт отдельную неопределённость в каждом реальном сопоставленном блоке."""
    result = compare(
        version([f"{i}. Условия поставки без изменений." for i in range(1, count + 1)]),
        version([f"{i}. Условия поставки без изменений." for i in range(1, count + 1)]),
    )
    for i, (a, b) in enumerate(zip(result.old.elements, result.new.elements, strict=True)):
        result.differences.append(
            Difference(
                f"U{i}",
                "unverified_region",
                "Непроверенная область",
                a.text,
                b.text,
                old_element=a.id,
                new_element=b.id,
                old_regions=element_regions([a]),
                new_regions=element_regions([b]),
                status="unverified",
                unverified_reason="mapping_not_found",
            )
        )
    result.coverage.original_unverified = count
    return result


@pytest.mark.parametrize(
    "full,targeted", [(False, False), (True, False), (False, True), (True, True)]
)
def test_modes_are_independent_and_keep_algorithmic_result(full, targeted):
    """Каждая комбинация запускает только выбранные проходы и сохраняет алгоритмы."""
    result = uncertain_result()
    reviewer = Reviewer()
    before = deepcopy(result.differences)
    AIReviewCoordinator(reviewer, AILimits()).verify(
        result, lambda stage: None, modes=AIModes(full, targeted)
    )
    assert {mode for _, mode, _ in reviewer.calls} == ({"full"} if full else set()) | (
        {"unverified"} if targeted else set()
    )
    assert result.differences == before
    assert result.ai_review.full.status == ("completed" if full else "not_requested")
    assert result.ai_review.unverified.status == ("completed" if targeted else "not_requested")


def test_twelve_is_batch_size_all_twenty_five_targets_are_processed():
    """Граничные фрагменты 13, 24 и 25 не теряются; одновременно выполняются не более двух."""
    result = uncertain_result(25)
    reviewer, checkpoints = Reviewer(delay=0.002), []
    AIReviewCoordinator(reviewer, AILimits(batch_size=12, concurrency=2)).verify(
        result, lambda stage: checkpoints.append(stage), modes=AIModes(unverified=True)
    )
    assert len(reviewer.calls) == 25
    assert reviewer.peak == 2
    assert all(unit.status == "completed" for unit in result.ai_review.units)
    assert result.ai_review.unverified.processed == 25
    assert any("пакет 3" in stage for stage in checkpoints)
    assert len(uncertainty_ids(result.differences)) == 25


def test_partial_failure_retry_and_resume_do_not_repeat_successful_units():
    """Повторы ограничены; поздняя ошибка не стирает успехи; повтор задания не запускает их снова."""
    result = uncertain_result(25)
    failed = unverified_plan(result, 3600)[13].id
    reviewer, snapshots = Reviewer([failed], retryable=True), []
    coordinator = AIReviewCoordinator(reviewer, AILimits(retries=2, retry_delay=0))
    coordinator.verify(
        result,
        lambda stage: None,
        modes=AIModes(unverified=True),
        persist=lambda current: snapshots.append(deepcopy(current.ai_review)),
    )
    assert result.ai_status == "partial"
    assert result.ai_review.unverified.succeeded == 24
    assert result.ai_review.unverified.failed == 1
    assert sum(identifier == failed for identifier, _, _ in reviewer.calls) == 3
    assert len(snapshots) >= 25
    reviewer.failing.clear()
    reviewer.calls.clear()
    coordinator.verify(result, lambda stage: None, modes=AIModes(unverified=True))
    assert [identifier for identifier, _, _ in reviewer.calls] == [failed]
    assert result.ai_status == "completed"
    assert len(result.ai_review.units) == 25
    assert not any(
        warning.startswith("ИИ-проверка выполнена не полностью") for warning in result.warnings
    )


def test_budget_exhaustion_marks_remaining_work_explicitly(monkeypatch):
    """Бюджет времени не превращает необработанный хвост в полную проверку."""
    result = uncertain_result(25)
    clock = iter([0, 10, 10, 10, 10, 10, 10, 10])
    monkeypatch.setattr("app.application.ai_review.time.monotonic", lambda: next(clock, 10))
    reviewer = Reviewer()
    AIReviewCoordinator(reviewer, AILimits(total_seconds=1)).verify(
        result, lambda stage: None, modes=AIModes(unverified=True)
    )
    assert not reviewer.calls
    assert result.ai_status == "failed"
    assert result.ai_review.unverified.remaining == 25
    assert result.ai_review.unverified.error_counts == {"time_budget": 25}


def test_cancel_keeps_prior_checkpoints():
    """Отмена после успешной операции оставляет её для адресного повтора."""
    result, snapshots = uncertain_result(25), []
    reviewer = Reviewer(delay=0.1)

    def checkpoint(stage):
        """Имитирует отмену владельцем после первого сохранённого результата."""
        if "обработано" in stage:
            raise Cancelled()

    with pytest.raises(Cancelled):
        AIReviewCoordinator(reviewer, AILimits()).verify(
            result,
            checkpoint,
            modes=AIModes(unverified=True),
            persist=lambda current: snapshots.append(deepcopy(current)),
        )
    assert snapshots[-1].ai_review.unverified.succeeded == 1
    assert len(reviewer.calls) <= 3


def test_full_plan_covers_both_paginations_long_tail_and_unmatched_page():
    """Страницы вставленной редакции планируются независимо; длинный текст не обрезается."""
    old = version(["1. " + "Условие " * 200, "2. Итог"])
    new = version(["Вставленная страница", "1. " + "Условие " * 200, "2. Итог"], shift=1)
    result = compare(old, new)
    plan = full_plan(result, 200)
    for side, document in (("old", old), ("new", new)):
        page_units = [u for u in plan if u.kind == "page"]
        assert {r.page for u in page_units for r in getattr(u, side + "_regions")} == {
            p.number for p in document.pages
        }
        for page in document.pages:
            regions = sorted(
                [
                    r
                    for u in page_units
                    for r in getattr(u, side + "_regions")
                    if r.page == page.number and r.x0 == 0
                ],
                key=lambda r: r.y0,
            )
            assert regions[0].y0 == 0 and regions[-1].y1 == page.height
            covered = 0
            for region in regions:
                assert region.y0 <= covered
                covered = max(covered, region.y1)
            assert covered == page.height
    assert any("Итог" in u.old_text for u in plan)
    split = [u for u in plan if u.kind == "number" and u.old_elements == [old.elements[0].id]]
    assert "".join(u.old_text for u in split) == old.elements[0].text


def test_only_independent_whole_block_evidence_resolves_uncertainty():
    """Успешный и уверенный VLM-ответ сам по себе не снимает непроверенность."""
    result = uncertain_result(1)
    unit = unverified_plan(result, 3600)[0]
    result.ai_review.original_uncertainties = ["U0"]
    reply = AIReply("same", True)
    merge_reply(result, unit, reply)
    assert statistics(result.differences)["unverified"] == 1
    value = result.old.elements[0].text
    reply.evidence = [AIObservation("unchanged", value, value, "o0", "n0", confidence=1)]
    merge_reply(result, unit, reply)
    assert statistics(result.differences)["unverified"] == 0
    assert result.coverage.resolved_ai == 1

    assert unit.verified_by == "ai_and_independent_text"
    assert result.ai_review.resolutions == {"U0": "same"}


@pytest.mark.parametrize(
    "failure", ["unreadable", "unverified_ocr", "invented_ref", "partial_quote"]
)
def test_insufficient_evidence_keeps_unverified(failure):
    """Нечитаемое, неуверенный OCR, чужой ref и частичная цитата не подтверждают весь блок."""
    result = uncertain_result(1)
    unit = unverified_plan(result, 3600)[0]
    value = result.old.elements[0].text
    if failure == "unverified_ocr":
        result.new.elements[0].words = [
            replace(w, verified=False) for w in result.new.elements[0].words
        ]
    quote = "Условия" if failure == "partial_quote" else value
    reply = AIReply(
        "same",
        failure != "unreadable",
        evidence=[
            AIObservation(
                "unchanged", quote, quote, "o99" if failure == "invented_ref" else "o0", "n0"
            )
        ],
    )
    merge_reply(result, unit, reply)
    assert statistics(result.differences)["unverified"] == 1


def test_repeated_numeral_must_have_unique_context_and_never_moves_to_other_clause():
    """Дневная ставка не подменяет предел пени; одинаковые значения требуют контекста."""
    result = compare(
        version(["5.2. Ставка 0,01%, предел 5%, резерв 5%"]),
        version(["5.2. Ставка 0,01%, предел 0,01%, резерв 5%"]),
    )
    unit = next(u for u in full_plan(result, 3600) if u.kind == "number")
    reply = AIReply(
        "different",
        True,
        [
            AIObservation(
                "number_modified",
                "5%",
                "0,01%",
                "o0",
                "n0",
                old_quote="предел 5%",
                new_quote="предел 0,01%",
            )
        ],
    )
    merge_reply(result, unit, reply)
    original = next(
        d
        for d in result.differences
        if d.source == "deterministic" and d.category == "number_modified"
    )
    assert original.old_text == "5%" and original.new_text == "0,01%"
    assert statistics(result.differences)["total"] == 1
    merge_reply(
        result,
        unit,
        AIReply(
            "different",
            True,
            [
                AIObservation(
                    "text_modified",
                    "5%",
                    "0,01%",
                    "o0",
                    "n0",
                    old_quote="предел 5%",
                    new_quote="предел 0,01%",
                )
            ],
        ),
    )
    assert statistics(result.differences)["total"] == 1
    merge_reply(result, unit, AIReply("same", True))
    assert original.status == "confirmed" and original.active
    assert any(
        d.category == "ai_contradiction" and d.status == "needs_review" for d in result.differences
    )


def test_progress_contains_no_document_text_or_paths():
    """Публичный прогресс состоит только из технических счётчиков."""
    result = uncertain_result()
    AIReviewCoordinator(Reviewer(), AILimits()).verify(
        result, lambda stage: None, modes=AIModes(unverified=True)
    )
    public = json.dumps(public_progress(result.ai_review), ensure_ascii=False)
    assert "Условия" not in public and "unused.pdf" not in public
    assert json.loads(public)["unverified"]["processed"] == 3
    assert asdict(result.ai_review)["units"]


def test_independent_ocr_can_resolve_a_false_positive_without_trusting_vlm_alone():
    """Совпавшие полные OCR-кропы с достаточной уверенностью снимают только локальный ложный факт."""
    result = uncertain_result(1)
    result.new.elements[0].text += " OCR-error"
    unit = unverified_plan(result, 3600)[0]
    result.ai_review.original_uncertainties = ["U0"]
    words = result.old.elements[0].words
    reply = AIReply("same", True, old_evidence=words, new_evidence=words)
    merge_reply(result, unit, reply)
    assert statistics(result.differences)["unverified"] == 0
    assert result.coverage.resolved_ai == 1
    assert unit.verified_by == "ai_and_independent_text"
    assert any(match.source == "ai" for match in result.matches)


def test_visual_partitions_are_bounded_and_do_not_drop_regions():
    """Большой контактный лист делится на следующие операции, сохраняя каждую исходную область."""
    source = version([f"Поставка товара номер {i}" for i in range(25)])
    result = compare(source, deepcopy(source))
    plan = full_plan(result, 3600, region_limit=3)
    assert all(len(unit.old_regions) <= 3 and len(unit.new_regions) <= 3 for unit in plan)
    structure = [unit for unit in plan if unit.kind == "correspondence"]
    assert {r.page for unit in structure for r in unit.old_regions} == set(range(1, 26))


def test_unreadable_reply_does_not_mark_page_fully_reviewed():
    """Валидный JSON с нечитаемой областью сохраняет честный незавершённый статус страницы."""

    class Unreadable:
        """Модель технически отвечает, но не может прочитать исходный растр."""

        def review(self, unit, old, new, attempt):
            """Возвращает нечитаемость без выдуманного результата сравнения."""
            return AIReply("uncertain", False)

    result = uncertain_result(1)
    AIReviewCoordinator(Unreadable(), AILimits()).verify(
        result, lambda stage: None, modes=AIModes(unverified=True)
    )
    assert result.ai_status == "failed"
    assert result.ai_review.unverified.pages_processed == {"old": [], "new": []}
    assert result.ai_review.unverified.error_counts == {"unreadable": 1}


def test_ai_cannot_confirm_different_parties_and_can_upgrade_review_after_evidence():
    """Чужая колонка не подтверждается; позднее независимое доказательство обновляет замечание без дубля."""
    result = compare(version(["2.2. Аванс 70%"]), version(["2.2. Аванс 50%"]))
    result.differences = []
    result.old.elements[0].attributes["column"] = "0"
    result.new.elements[0].attributes["column"] = "1"
    unit = next(u for u in full_plan(result, 3600) if u.kind == "number")
    reply = AIReply("different", True, [AIObservation("number_modified", "70%", "50%", "o0", "n0")])
    merge_reply(result, unit, reply)
    assert len(result.differences) == 1 and result.differences[0].status == "needs_review"
    result.new.elements[0].attributes["column"] = "0"
    merge_reply(result, unit, reply)
    assert len(result.differences) == 1 and result.differences[0].status == "confirmed"
    assert not result.ai_review.resolutions


def test_resume_retains_proof_for_already_resolved_targets():
    """После снятия неопределённости повтор не теряет её успешную операцию и доказательства."""

    class Same:
        """Модель подтверждает сравнение проверенных блоков без новых различий."""

        def review(self, unit, old, new, attempt):
            """Возвращает совпадение, которое application обязан независимо проверить."""
            return AIReply("same", True)

    result = uncertain_result(3)
    coordinator = AIReviewCoordinator(Same(), AILimits())
    coordinator.verify(result, lambda stage: None, modes=AIModes(unverified=True))
    assert result.coverage.resolved_ai == 3
    ids = [unit.id for unit in result.ai_review.units]
    coordinator.verify(result, lambda stage: None, modes=AIModes(unverified=True))
    assert [unit.id for unit in result.ai_review.units] == ids
    assert result.ai_review.unverified.succeeded == 3


def test_page_pass_expands_boundaries_and_keeps_counterpart_on_another_page():
    """Полоса не обрезает абзац; контекст второй редакции остаётся целым при изменённой пагинации."""
    old = version(["1. Условия поставки товара"])
    new = version(["1. Условия поставки товара"], shift=1)
    for document in (old, new):
        element = document.elements[0]
        element.words = [
            replace(
                word,
                region=replace(word.region, y0=260 if i < 2 else 300, y1=272 if i < 2 else 312),
            )
            for i, word in enumerate(element.words)
        ]
    result = compare(old, new)
    units = [
        u for u in full_plan(result, 3600) if u.kind == "page" and u.old_elements and u.new_elements
    ]
    assert units
    for unit in units:
        for side, document in (("old", old), ("new", new)):
            for word in document.elements[0].words:
                assert any(
                    r.page == word.region.page and r.y0 <= word.region.y0 and r.y1 >= word.region.y1
                    for r in getattr(unit, side + "_regions")
                )


def visual_reply(
    unit, category, old, new, *, complete=True, readable=True, covers=True, absent=False
):
    """Моделирует ответ порта, действительно получившего подписанные исходные области."""
    observation = AIObservation(
        category,
        old,
        new,
        "o0",
        "n0",
        confidence=0.95,
        old_quote=old,
        new_quote=new,
        mapping_confirmed=True,
        mapping_reason="Один пункт и одна роль стороны на исходных изображениях",
        covers_region=covers,
        absence_confirmed=absent,
    )
    return AIReply(
        "same" if category == "unchanged" else "different",
        readable,
        [] if category == "unchanged" else [observation],
        [observation] if category == "unchanged" else [],
        complete=complete,
        visual_refs=["o0", "n0"],
    )


def test_visual_same_without_algorithmic_match_or_correct_ocr():
    """VLM самостоятельно снимает OCR-неопределённость, сохраняя исходное чтение и историю."""
    result = uncertain_result(1)
    result.matches.clear()
    result.new.elements[0].text = "1. Ошибка OCR 7O%"
    result.new.elements[0].words = [
        replace(w, verified=False, confidence=0.3) for w in result.new.elements[0].words
    ]
    result.differences[0].new_text = result.new.elements[0].text
    result.ai_review.original_uncertainties = ["U0"]
    unit = unverified_plan(result, 3600)[0]
    before = deepcopy(result.new.elements[0])
    reply = visual_reply(
        unit, "unchanged", result.old.elements[0].text, result.old.elements[0].text
    )
    merge_reply(result, unit, reply)
    assert statistics(result.differences)["unverified"] == 0
    assert statistics(result.differences)["total"] == 0
    assert result.coverage.resolved_ai == 1 and not result.differences[0].active
    assert result.matches[0].source == "ai" and result.matches[0].method == "ai_visual_same"
    assert result.new.elements[0] == before
    assert result.differences[0].new_text == "1. Ошибка OCR 7O%"


@pytest.mark.parametrize(
    "category", ["number_modified", "table_modified", "text_moved", "graphic_modified"]
)
def test_visual_new_change_without_existing_mapping(category):
    """Новое визуальное изменение входит в total, геометрию, покрытие и повтор без дубля."""
    result = uncertain_result(1)
    result.matches.clear()
    result.ai_review.original_uncertainties = ["U0"]
    unit = unverified_plan(result, 3600)[0]
    reply = visual_reply(unit, category, "70%", "50%")
    merge_reply(result, unit, reply)
    merge_reply(result, unit, reply)
    confirmed = [d for d in result.differences if d.active and d.status == "confirmed"]
    assert len(confirmed) == 1 and confirmed[0].source == "ai"
    assert confirmed[0].verified_by == "ai_visual"
    assert confirmed[0].old_regions and confirmed[0].new_regions
    assert statistics(result.differences)["total"] == 1
    assert statistics(result.differences)["unverified"] == 0
    assert result.coverage.old[unit.old_elements[0]] == "changed"


@pytest.mark.parametrize(
    "category", ["text_added", "text_deleted", "graphic_added", "graphic_deleted"]
)
def test_visual_absence_uses_real_structural_anchor_without_fake_rectangle(category):
    """Отсутствующая сторона хранит якорь отдельно и не получает фиктивную подсветку."""
    result = uncertain_result(1)
    unit = unverified_plan(result, 3600)[0]
    addition = category.endswith("_added")
    reply = visual_reply(
        unit, category, "" if addition else "Фрагмент", "Фрагмент" if addition else "", absent=True
    )
    merge_reply(result, unit, reply)
    change = next(d for d in result.differences if d.source == "ai")
    assert change.status == "confirmed"
    assert getattr(change, "old_regions" if addition else "new_regions") == []
    assert getattr(change, "old_anchor_regions" if addition else "new_anchor_regions")
    assert statistics(result.differences)["added" if addition else "deleted"] == 1
    assert result.coverage.geometry_missing == 0


@pytest.mark.parametrize(
    "failure", ["partial", "unreadable", "unknown_ref", "no_image", "no_mapping", "no_absence"]
)
def test_visual_acceptance_requires_more_than_confidence(failure):
    """Высокая уверенность не заменяет реальный источник, читаемость, полноту и доказанное отсутствие."""
    result = uncertain_result(1)
    for document in (result.old, result.new):
        document.elements[0].words = [
            replace(w, verified=False) for w in document.elements[0].words
        ]
    unit = unverified_plan(result, 3600)[0]
    reply = visual_reply(
        unit,
        "text_added" if failure == "no_absence" else "number_modified",
        "" if failure == "no_absence" else "70%",
        "50%",
        complete=failure != "partial",
        readable=failure != "unreadable",
    )
    if failure == "unknown_ref":
        reply.findings[0].old_ref = "o99"
    if failure == "no_image":
        reply.visual_refs.clear()
    if failure == "no_mapping":
        reply.findings[0].mapping_confirmed = False
    merge_reply(result, unit, reply)
    assert statistics(result.differences)["total"] == 0
    assert statistics(result.differences)["unverified"] == 1
    assert any(d.status == "needs_review" for d in result.differences)


def test_child_visual_coverage_accumulates_and_retry_only_plans_remainder():
    """Два дочерних запроса разрешают родителя; повтор после первого не включает проверенную половину."""
    result = uncertain_result(1)
    result.ai_review.original_uncertainties = ["U0"]
    original = unverified_plan(result, 3600)[0]
    a, b = original.old_regions[0], original.new_regions[0]
    first = deepcopy(original)
    first.old_regions = [replace(a, x1=(a.x0 + a.x1) / 2)]
    first.new_regions = [replace(b, x1=(b.x0 + b.x1) / 2)]
    merge_reply(result, first, visual_reply(first, "unchanged", "Первая часть", "Первая часть"))
    assert statistics(result.differences)["unverified"] == 1
    remainder = unverified_plan(result, 3600)[0]
    assert all(r.x0 >= first.old_regions[0].x1 for r in remainder.old_regions)
    merge_reply(
        result, remainder, visual_reply(remainder, "unchanged", "Вторая часть", "Вторая часть")
    )
    assert statistics(result.differences)["unverified"] == 0
    assert result.coverage.resolved_ai == 1


def test_unsupported_hyperlink_stays_separate_from_resolved_text():
    """Совпадение видимого текста не доказывает скрытый адрес гиперссылки Word."""
    result = uncertain_result(1)
    hidden = deepcopy(result.differences[0])
    hidden.id, hidden.uncertainty_id = "hidden-link", "hidden-link"
    hidden.unverified_reason = "unsupported_property"
    hidden.reason = "Невозможно проверить адрес гиперссылки по сканированному PDF"
    result.differences.append(hidden)
    unit = unverified_plan(result, 3600)[0]
    merge_reply(result, unit, visual_reply(unit, "unchanged", "Текст", "Текст"))
    assert not result.differences[0].active and hidden.active
    assert statistics(result.differences)["unverified"] == 1
    assert unverified_plan(result, 3600) == []


def test_total_thirty_plus_five_with_two_duplicates_is_thirty_three():
    """Общая статистика включает новые ИИ-факты и отдельно показывает совместное подтверждение."""
    result = compare(
        version([f"{i}. Аванс {i * 100}%" for i in range(1, 34)]),
        version([f"{i}. Аванс {i * 100 + 1}%" for i in range(1, 34)]),
    )
    result.differences = [d for d in result.differences if d.old_section not in {"31", "32", "33"}]
    assert statistics(result.differences)["total"] == 30
    units = [u for u in full_plan(result, 3600) if u.kind == "number"]
    for i in (1, 2, 31, 32, 33):
        unit = next(u for u in units if u.old_elements == [result.old.elements[i - 1].id])
        merge_reply(
            result, unit, visual_reply(unit, "number_modified", f"{i * 100}%", f"{i * 100 + 1}%")
        )
    stats = statistics(result.differences)
    assert stats["total"] == 33 and stats["ai_confirmed"] == 3 and stats["both_confirmed"] == 2


def test_conflict_gets_one_additional_targeted_check_without_erasing_algorithm():
    """При наличии бюджета противоречие перепроверяется адресно, без бесконечного цикла и дубля."""

    class Contradictory:
        """Воспроизводит устойчивое противоречие с известным алгоритмическим фактом."""

        def __init__(self):
            """Создаёт независимый журнал видов проверенных областей."""
            self.kinds = []

        def review(self, unit, old, new, attempt):
            """Сохраняет виды запросов, чтобы проверить дополнительный проход."""
            self.kinds.append(unit.kind)
            return AIReply("same", True)

    result = compare(version(["2.2. Аванс 70%"]), version(["2.2. Аванс 50%"]))
    reviewer = Contradictory()
    AIReviewCoordinator(reviewer, AILimits()).verify(
        result, lambda stage: None, modes=AIModes(full=True)
    )
    assert reviewer.kinds.count("conflict") == 1
    assert statistics(result.differences)["total"] == 1
    assert (
        len([d for d in result.differences if d.active and d.category == "ai_contradiction"]) == 1
    )


def test_missing_text_geometry_still_plans_actual_source_pages():
    """Номер страницы второй редакции не подставляется вместо отсутствующего якоря первой."""
    result = uncertain_result(3)
    result.matches.clear()
    result.old.elements[1].words = []
    result.differences[1].old_regions = []
    units = [u for u in unverified_plan(result, 3600) if "U1" in u.targets]
    assert {r.page for u in units for r in u.old_regions} == {1, 2, 3}
    assert all(len(u.old_regions) == 1 and u.new_regions for u in units)
    assert all(
        0 <= r.x0 < r.x1 <= 600 and 0 <= r.y0 < r.y1 <= 850 for u in units for r in u.old_regions
    )


def test_visual_mapping_can_follow_reordered_columns_and_different_pagination():
    """Визуальное соответствие роли не зависит от индекса колонки или алгоритмической пары."""
    result = uncertain_result(1)
    result.matches.clear()
    result.old.elements[0].attributes["column"] = "0"
    result.new.elements[0].attributes["column"] = "1"
    result.new.pages.append(replace(result.new.pages[0], number=2))
    result.new.elements[0].words = [
        replace(word, region=replace(word.region, page=2)) for word in result.new.elements[0].words
    ]
    result.differences[0].new_regions = element_regions(result.new.elements)
    unit = unverified_plan(result, 3600)[0]
    merge_reply(result, unit, visual_reply(unit, "number_modified", "70%", "50%"))
    assert statistics(result.differences)["ai_confirmed"] == 1
    assert statistics(result.differences)["unverified"] == 0
    from app.application.queries import correspondence

    assert correspondence(asdict(result), "old", 1)["page"] == 2
    result.old.elements[0].words.clear()
    assert any(
        unit.old_regions and unit.new_regions
        for unit in full_plan(result, 3600)
        if unit.kind != "page"
    )


def test_contradictory_visual_number_does_not_become_second_confirmed_fact():
    """Альтернативное число на месте известного факта сохраняется гипотезой, не увеличивая total."""
    result = compare(version(["2.2. Аванс 70%"]), version(["2.2. Аванс 50%"]))
    unit = next(u for u in full_plan(result, 3600) if u.kind == "number")
    merge_reply(result, unit, visual_reply(unit, "number_modified", "70%", "60%"))
    assert statistics(result.differences)["total"] == 1
    assert any(
        d.source == "ai"
        and d.status == "needs_review"
        and d.unverified_reason == "conflicting_evidence"
        for d in result.differences
    )


def test_internal_hyperlink_and_field_code_are_explicit_nonvisual_properties(tmp_path):
    """Реальное OOXML с внутренней ссылкой/полем не получает визуальное подтверждение по PDF."""
    from app.infrastructure.word import read_docx
    from tests.factories import docx

    path = docx(
        tmp_path / "fields.docx",
        ["2.2. Текст договора"],
        extra_xml='<w:p><w:hyperlink w:anchor="contract-target"><w:r><w:t>Ссылка</w:t></w:r></w:hyperlink></w:p><w:p><w:r><w:instrText>DATE</w:instrText><w:t>28.10.2026</w:t></w:r></w:p>',
    )
    logical = read_docx(path)
    old = version([e.text for e in logical])
    for measured, source in zip(old.elements, logical, strict=True):
        measured.attributes = source.attributes
    new = version([e.text for e in logical], format="pdf")
    result = compare(old, new)
    properties = [
        d for d in result.differences if d.active and d.unverified_reason == "unsupported_property"
    ]
    assert len(properties) == 2
    assert any("гиперссылки" in d.reason for d in properties)
    assert any("поля" in d.reason for d in properties)
    assert all(d.category == "unverified_region" for d in properties)
    assert all(
        not ({d.uncertainty_id for d in properties} & set(unit.targets))
        for unit in unverified_plan(result, 3600)
    )
