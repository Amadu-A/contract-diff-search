# tests/functional/test_ai_workflow.py
"""Новые режимы API, повтор без OCR и независимое скрытие предупреждений во всех проекциях."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from app.application.ai_review import AILimits, AIReviewCoordinator
from app.application.queries import filter_differences, page_regions, summary
from app.domain.models import Difference
from tests.factories import pdf
from tests.fakes import FakeExporter
from tests.functional.test_lifecycle import upload
from tests.unit.test_ai_review import Reviewer, uncertain_result, visual_reply

pytest_plugins = ("tests.functional.test_lifecycle",)


@pytest.mark.parametrize("ai_enabled", [False, True])
def test_local_ocr_runs_before_optional_vlm_and_resolves_short_changes(
    client, service, tmp_path, ai_enabled
):
    """Повторное OCR обязательно при короткой неопределённости; VLM вызывается только выбранным режимом."""
    from app.domain.comparison import resolve_ocr
    from tests.fakes import FakeExtractor
    from tests.regression.test_ocr_recheck import short_result

    initial = short_result()
    calls = []

    class Extractor(FakeExtractor):
        """Передаёт сценарий локальной проверки через действующий порт извлечения."""

        def recheck(self, result, checkpoint):
            """Разрешает локально либо оставляет цель для независимо включённой VLM."""
            calls.append("ocr")
            if not ai_enabled:
                resolve_ocr(result, result.differences[0], "не", "т", confidence=0.95)

    class Visual:
        """Контролирует отсутствие принудительных запросов к модели."""

        def review(self, unit, old, new, attempt):
            """Возвращает проверяемое визуальное доказательство только при явном выборе ИИ."""
            assert ai_enabled
            calls.append("vlm")
            return visual_reply(unit, "text_modified", "не", "т")

    service.extractor = Extractor(initial.old, initial.new)
    service.verifier = AIReviewCoordinator(Visual(), AILimits())
    path = pdf(tmp_path / "input-short.pdf", ["Synthetic"])
    job = client.post(
        "/api/v1/comparisons",
        files={
            side: (side + ".pdf", path.read_bytes(), "application/pdf") for side in ("old", "new")
        },
        data={"ai_unverified": str(ai_enabled).lower()},
    ).json()
    service.process(job["id"])
    result = service.storage.restore_result(job["id"])
    assert calls[0] == "ocr"
    assert ("vlm" in calls) == ai_enabled
    assert any(
        d.active and d.status == "confirmed" and d.old_text == "не" and d.new_text == "т"
        for d in result.differences
    )
    assert (
        client.get(f"/api/v1/comparisons/{job['id']}/result").json()["statistics"]["unverified"]
        == 0
    )


def test_accepted_visual_checkpoint_updates_statistics_and_pdf_before_final_word(
    client, service, tmp_path
):
    """Принятие ИИ обновляет экспорт на checkpoint без повторного извлечения и без частых Word-конвертаций."""
    from app.domain.comparison import statistics

    observations = []

    class Exporter(FakeExporter):
        """Наблюдает содержимое действующего порта экспорта, не заменяя сценарий обработки."""

        def export(self, result, output, checkpoint, *, include_word=True):
            """Записывает фактическую матрицу, поступившую при промежуточном и окончательном экспорте."""
            observations.append((include_word, statistics(result.differences)))
            return super().export(result, output, checkpoint, include_word=include_word)

    class Visual:
        """Отвечает как порт, получивший исходные изображения неопределённого числового фрагмента."""

        def review(self, unit, old, new, attempt):
            """Возвращает локализованное визуальное изменение с независимым установлением соответствия."""
            return visual_reply(unit, "number_modified", "70%", "50%")

    service.extractor.new.elements[0].words = [
        replace(word, verified=False, confidence=0.3)
        for word in service.extractor.new.elements[0].words
    ]
    service.exporter = Exporter()
    service.verifier = AIReviewCoordinator(Visual(), AILimits())
    job = upload(client, tmp_path, True).json()
    service.process(job["id"])
    result = client.get(f"/api/v1/comparisons/{job['id']}/result").json()
    assert observations[0][1]["total"] == 0
    assert any(
        not word and stats["total"] == 1 and stats["unverified"] == 0
        for word, stats in observations
    )
    assert observations[-1][0] is True
    assert result["statistics"]["ai_confirmed"] == 1
    # Страничный fake также возвращает одну непривязанную гипотезу: общий фильтр теперь честно учитывает её.
    assert result["statistics"]["unverified"] == result["statistics"]["ai_needs_review"] == 1
    unverified = client.get(f"/api/v1/comparisons/{job['id']}/differences?status=unverified").json()
    assert len(unverified["items"]) == 1
    assert unverified["items"][0]["status"] == "needs_review"
    assert service.extractor.calls == 2


@pytest.mark.parametrize(
    "full,targeted", [(False, False), (True, False), (False, True), (True, True)]
)
def test_new_modes_are_saved_and_sent_independently(client, service, tmp_path, full, targeted):
    """POST сохраняет оба флага, а worker выполняет алгоритмы при любой комбинации."""
    data = pdf(tmp_path / "input.pdf", ["Synthetic input"]).read_bytes()
    response = client.post(
        "/api/v1/comparisons",
        files={
            "old": ("our.pdf", data, "application/pdf"),
            "new": ("partner.pdf", data, "application/pdf"),
        },
        data={"ai_full": str(full).lower(), "ai_unverified": str(targeted).lower()},
    )
    assert response.status_code == 202
    job = response.json()
    assert (job["ai_full"], job["ai_unverified"]) == (full, targeted)
    reviewer = Reviewer()
    service.verifier = AIReviewCoordinator(reviewer, AILimits())
    service.process(job["id"])
    result = client.get(f"/api/v1/comparisons/{job['id']}/result").json()
    assert result["statistics"]["total"] == 1
    assert result["ai_progress"]["full"]["status"] == ("completed" if full else "not_requested")


def test_retry_keeps_checkpoint_and_does_not_extract_again(client, service, tmp_path):
    """После отказа повтор использует сохранённые модели и не вызывает повторное извлечение."""
    job = upload(client, tmp_path, True).json()
    service.process(job["id"])
    assert service.extractor.calls == 2
    base = f"/api/v1/comparisons/{job['id']}"
    assert client.post(base + "/ai/retry").status_code == 202
    service.verifier = AIReviewCoordinator(Reviewer(), AILimits())
    service.process(job["id"])
    assert service.extractor.calls == 2
    assert client.get(base + "/result").json()["ai_status"] == "completed"
    operations = client.get(base + "/ai/operations?limit=1").json()
    assert operations["total"] > 0 and len(operations["items"]) == 1
    assert (
        not {"old_text", "new_text", "old_regions", "new_regions"} & operations["items"][0].keys()
    )


def test_visibility_does_not_change_verification_statistics_or_confirmed_same_element():
    """Скрытие убирает только непроверенные пометки, даже если факт и предупреждение имеют один якорь."""
    result = uncertain_result(1)
    warning = result.differences[0]
    result.differences.append(
        Difference(
            "confirmed",
            "text_modified",
            "Подтверждённое изменение",
            "старое",
            "новое",
            old_element=warning.old_element,
            new_element=warning.new_element,
            old_regions=warning.old_regions,
            new_regions=warning.new_regions,
        )
    )
    data = asdict(result)
    original = deepcopy(data)
    stats = summary(data)["statistics"]
    assert filter_differences(data, "", "", 0, 100, True)["total"] == 2
    assert [d["id"] for d in filter_differences(data, "", "", 0, 100, False)["items"]] == [
        "confirmed"
    ]
    for side in ("old", "new"):
        assert [d["id"] for d in page_regions(data, side, 1, False)["items"]] == ["confirmed"]
    assert summary(data)["statistics"] == stats and stats["unverified"] == 1
    assert data == original


def test_cancel_retains_partial_result_and_retry_skips_extraction(client, service, tmp_path):
    """Отмена закрывает текущий проход, сохраняет checkpoint и разрешает продолжение без OCR."""
    from app.application.ai_review import public_progress
    from app.domain.models import ReviewUnit

    job = upload(client, tmp_path, True).json()
    base = f"/api/v1/comparisons/{job['id']}"

    class Cancelling:
        """Имитирует остановку владельцем после одного сохранённого успешного запроса."""

        def verify(self, result, checkpoint, *, persist, **kwargs):
            """Сохраняет реальный базовый результат и инициирует HTTP-отмену через защищённый API."""
            result.ai_review.full.status = "running"
            result.ai_review.full.planned = 2
            result.ai_review.full.succeeded = 1
            result.ai_review.full.processed = 1
            result.ai_review.full.remaining = 1
            result.ai_review.units = [ReviewUnit("saved", "full", "text", status="completed")]
            persist(result)
            assert public_progress(result.ai_review)["full"]["succeeded"] == 1
            assert client.post(base + "/cancel").status_code == 200
            checkpoint("Проверка отмены")

    service.verifier = Cancelling()
    service.process(job["id"])
    assert client.get(base).json()["status"] == "cancelled"
    summary = client.get(base + "/result").json()
    assert summary["ai_status"] == "partial"
    assert summary["ai_progress"]["full"]["status"] == "cancelled"
    assert summary["statistics"]["total"] == 1
    assert client.get(base + "/export/old").status_code == 409
    assert client.post(base + "/ai/retry").status_code == 202
    service.verifier = AIReviewCoordinator(Reviewer(), AILimits())
    service.process(job["id"])
    assert service.extractor.calls == 2
    assert client.get(base + "/result").json()["ai_status"] == "completed"
