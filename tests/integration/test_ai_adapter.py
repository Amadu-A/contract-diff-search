# tests/integration/test_ai_adapter.py
"""HTTP-контракт ИИ: пустые ответы, контекст, JSON и безопасная диагностика без содержания файлов."""

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from app.domain.models import ReviewUnit, VerificationError
from app.infrastructure.verifier import SharedVlmVerifier
from tests.factories import pdf, version


@pytest.mark.parametrize("ocr_fails", [False, True])
def test_visual_request_bounds_images_and_preserves_real_ocr_coordinates(
    settings, tmp_path, caplog, ocr_fails
):
    """Реальные PDF-кропы имеют ref, ограниченный растр и исходную геометрию; отказ OCR не скрывает VLM."""
    from app.domain.models import VisualRegion
    from app.infrastructure.ocr import TesseractOCR
    from app.infrastructure.pdf import PdfDocumentExtractor
    from app.infrastructure.word import LibreOffice

    source = pdf(
        tmp_path / "source.pdf",
        ["2.2. Synthetic document with sufficient native text 70%."],
        pages=3,
    )
    document = PdfDocumentExtractor(
        settings, TesseractOCR(settings), LibreOffice(settings)
    ).extract(source, source.name, tmp_path / "old", False, lambda stage: None)
    unit = ReviewUnit(
        "U-visual",
        "unverified",
        "ocr",
        old_regions=[VisualRegion(p.number, 0, 0, p.width, p.height) for p in document.pages],
        new_regions=[VisualRegion(1, 0, 0, 595, 842)],
        old_text="DO-NOT-LOG",
        error_kind="context_limit",
    )
    calls = []

    class OCR:
        """Возвращает измеренную область либо безопасно проверяемый отказ независимого OCR."""

        def recognize(self, image, directory, checkpoint):
            """Использует пиксели настоящего кропа; не подменяет координаты ответом модели."""
            calls.append(image.size)
            if ocr_fails:
                raise RuntimeError("DO-NOT-LOG")
            return [
                {"text": "Synthetic", "box": (3, 4, 24, 12), "confidence": 0.98, "verified": True}
            ]

    def handler(request):
        """Проверяет фактически отправленные изображения и бюджет после контекстного отказа."""
        payload = json.loads(request.content)
        assert payload["response_format"]["type"] == "json_schema"
        parts = payload["messages"][1]["content"]
        images = [item for item in parts if item["type"] == "image_url"]
        assert len(images) == 2
        for item in images:
            with Image.open(
                io.BytesIO(base64.b64decode(item["image_url"]["url"].split(",", 1)[1]))
            ) as image:
                assert image.width * image.height <= settings.vlm_image_pixels // 2
        context = json.loads(parts[0]["text"])
        assert [item["ref"] for item in context["old_sources"]] == ["o0", "o1", "o2"]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"outcome":"same","readable":true}'}}]}
        )

    reply = SharedVlmVerifier(settings, httpx.MockTransport(handler), OCR()).review(
        unit, document, document, attempt=1
    )
    assert len(calls) == 4
    assert reply.visual_refs == ["o0", "o1", "o2", "n0"]
    if ocr_fails:
        assert not reply.old_evidence
    else:
        assert [word.region.page for word in reply.old_evidence] == [1, 2, 3]
        assert 0.8 < reply.old_evidence[0].region.x0 < 1.2
        assert 1 < reply.old_evidence[0].region.y0 < 1.5
    assert "DO-NOT-LOG" not in caplog.text


def test_unsupported_schema_and_thinking_fallbacks_are_not_reenabled(settings, monkeypatch):
    """Два разных отказа совместимости не возвращают уже отключённый флаг на третьем запросе."""
    from app.application.ai_review import AILimits, AIReviewCoordinator
    from app.domain.comparison import compare

    requests = []
    unit = ReviewUnit("U-compat", "full", "text")
    monkeypatch.setattr("app.application.ai_review.full_plan", lambda result, *args: [unit])

    def handler(request):
        """Воспроизводит последовательное отклонение схемы и флага рассуждений сервером."""
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": "unsupported json_schema"})
        if len(requests) == 2:
            return httpx.Response(400, json={"error": "unsupported chat_template_kwargs"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"outcome":"same","readable":true}'}}]}
        )

    result = compare(version([]), version([]))
    AIReviewCoordinator(
        SharedVlmVerifier(settings, httpx.MockTransport(handler)), AILimits(retry_delay=0)
    ).verify(result, lambda stage: None)
    assert result.ai_status == "completed" and len(requests) == 3
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert (
        requests[1]["response_format"]["type"]
        == requests[2]["response_format"]["type"]
        == "json_object"
    )
    assert "chat_template_kwargs" in requests[1] and "chat_template_kwargs" not in requests[2]


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (
            200,
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": None, "reasoning_content": "PRIVATE"},
                    }
                ]
            },
            "output_truncated",
        ),
        (
            200,
            {"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
            "empty_content",
        ),
        (200, {"choices": [{"message": {"content": "not-json PRIVATE"}}]}, "json_validation"),
        (
            200,
            {
                "choices": [
                    {"message": {"content": '{"outcome":"same","readable":true,"bbox":[1,2,3,4]}'}}
                ]
            },
            "json_validation",
        ),
        (429, {"error": "PRIVATE"}, "http_error"),
        (503, {"error": "PRIVATE"}, "http_error"),
        (400, {"error": "maximum context length PRIVATE"}, "context_limit"),
        (400, {"error": "unsupported chat_template_kwargs PRIVATE"}, "unsupported_parameter"),
    ],
)
def test_failures_are_classified_without_response_text(settings, caplog, status, body, expected):
    """Ошибки сервера и схемы дают техническую категорию; содержимое не попадает в журнал."""
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    unit = ReviewUnit("U-test", "full", "text")
    with pytest.raises(VerificationError) as failure:
        SharedVlmVerifier(settings, transport).review(unit, version([]), version([]))
    assert failure.value.kind == expected
    assert "PRIVATE" not in str(failure.value) and "PRIVATE" not in caplog.text


def test_retry_changes_output_limit_and_disables_unsupported_flag(settings):
    """Повтор после отказа параметра совместимости не повторяет неподдерживаемый флаг."""
    requests = []

    def handler(request):
        """Сохраняет только синтетическое тело для проверки управляемых параметров."""
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"outcome":"uncertain","readable":false}'},
                    }
                ]
            },
        )

    reviewer = SharedVlmVerifier(settings, httpx.MockTransport(handler))
    unit = ReviewUnit("U-test", "unverified", "ocr", error_kind="unsupported_parameter")
    reply = reviewer.review(unit, version([]), version([]), attempt=1)
    assert not reply.readable
    assert "chat_template_kwargs" not in requests[0]
    assert requests[0]["max_tokens"] == settings.vlm_max_retry_tokens
    assert requests[0]["model"] == "shared-vlm"


@pytest.mark.parametrize(
    "error,expected",
    [(httpx.ReadTimeout("PRIVATE"), "timeout"), (httpx.ConnectError("PRIVATE"), "network")],
)
def test_transport_errors_are_safe_and_retryable(settings, error, expected):
    """Сетевые исключения не раскрывают текст адреса/запроса и допускают ограниченный повтор."""

    def handler(request):
        """Возвращает детерминированный транспортный отказ без сети."""
        raise error

    with pytest.raises(VerificationError) as failure:
        SharedVlmVerifier(settings, httpx.MockTransport(handler)).review(
            ReviewUnit("U", "full", "text"), version([]), version([])
        )
    assert failure.value.kind == expected and failure.value.retryable
    assert "PRIVATE" not in str(failure.value)


@pytest.mark.parametrize("category", ["unchanged", "number_modified"])
def test_same_response_normalizes_matches_but_rejects_actual_contradiction(settings, category):
    """Совпадения в findings принимаются как evidence, реальные изменения при outcome=same отклоняются."""
    answer = {
        "outcome": "same",
        "readable": True,
        "findings": [
            {
                "category": category,
                "old_value": "70%",
                "new_value": "70%",
                "old_ref": "o0",
                "new_ref": "n0",
            }
        ],
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(answer)}}]}
        )
    )
    reviewer = SharedVlmVerifier(settings, transport)
    if category == "unchanged":
        reply = reviewer.review(ReviewUnit("U", "full", "text"), version([]), version([]))
        assert not reply.findings and len(reply.evidence) == 1
        assert reply.visual_refs == []
    else:
        with pytest.raises(VerificationError, match="invalid_semantic_response"):
            reviewer.review(ReviewUnit("U", "full", "text"), version([]), version([]))
