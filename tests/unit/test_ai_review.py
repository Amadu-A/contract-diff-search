# tests/unit/test_ai_review.py
"""Полнота планов ИИ, адресные доказательства, повторы и сохранение частичного результата."""

import json
import threading
import time
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from app.application.ai_evidence import merge_reply
from app.application.ai_planning import full_plan, unverified_plan
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
                old_regions=[a.words[0].region],
                new_regions=[b.words[0].region],
                status="unverified",
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
