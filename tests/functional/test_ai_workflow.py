# tests/functional/test_ai_workflow.py
"""Новые режимы API, повтор без OCR и независимое скрытие предупреждений во всех проекциях."""

from copy import deepcopy
from dataclasses import asdict

import pytest

from app.application.ai_review import AILimits, AIReviewCoordinator
from app.application.queries import filter_differences, page_regions, summary
from app.domain.models import Difference
from tests.factories import pdf
from tests.functional.test_lifecycle import upload
from tests.unit.test_ai_review import Reviewer, uncertain_result

pytest_plugins = ("tests.functional.test_lifecycle",)


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
