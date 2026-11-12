# tests/functional/test_comparison_report.py
"""Реальный PDF-отчёт из сохранённого сравнения: права, Telegram, TTL и многостраничные тексты."""

import io
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pdfplumber
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pypdf import PdfReader

from app.application.queries import comparison_report
from app.domain.models import Difference
from app.infrastructure.exports import DocumentExporter
from app.web.api import INTEGRATION, create_app
from tests.functional.test_lifecycle import upload

pytest_plugins = ("tests.functional.test_lifecycle",)


def report_job(client, service, tmp_path):
    """Сохраняет настоящий результат текущих алгоритмов, затем включает реальный PDF-адаптер."""
    job_id = upload(client, tmp_path).json()["id"]
    service.process(job_id)
    service.exporter = DocumentExporter(None)
    return job_id


def test_report_uses_saved_result_without_restarting_pipeline(client, service, tmp_path):
    """GET скачивает кириллический PDF; повторная загрузка не вызывает ни один порт анализа."""
    job_id = report_job(client, service, tmp_path)
    before = service.extractor.calls

    def forbidden(*args, **kwargs):
        """Любая повторная обработка, публикация или VLM-проверка нарушила бы контракт GET-отчёта."""
        raise AssertionError("Отчёт не должен запускать обработку")

    service.extractor.extract = forbidden
    service.verifier.verify = forbidden
    service.publisher.publish = forbidden
    for _ in range(2):
        response = client.get(f"/api/v1/comparisons/{job_id}/report")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/pdf"
        assert "attachment" in response.headers["content-disposition"]
        text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(response.content)).pages)
        assert "Отчёт по сравнению редакций" in text
        assert "Было (наша" in text and "Стало (версия" in text
        assert "70%" in text and "50%" in text and "Подтверждено" in text
    assert service.extractor.calls == before


@pytest.mark.parametrize("only_excluded", [False, True])
def test_report_omits_formatting_and_table_structure_for_all_sources_and_statuses(
    client, service, tmp_path, only_excluded
):
    """Отчёт и его счётчики исключают два типа; сохранённый результат и остальные изменения доступны."""
    job_id = report_job(client, service, tmp_path)
    result = service.storage.restore_result(job_id)
    result.differences = []
    for category in ("formatting_changed", "table_structure_changed"):
        for source in ("deterministic", "ai"):
            for status in ("confirmed", "unverified", "needs_review"):
                result.differences.append(
                    Difference(
                        f"{category}-{source}-{status}",
                        category,
                        "Исключаемое событие",
                        "ExcludedBeforeMarker",
                        "ExcludedAfterMarker",
                        status=status,
                        source=source,
                    )
                )
    if not only_excluded:
        result.differences += [
            Difference("T", "table_modified", "Содержимое ячейки", "70%", "50%"),
            Difference(
                "S", "structure_changed", "Структура документа", "BeforeMarker", "AfterMarker"
            ),
            Difference(
                "U",
                "text_modified",
                "Непроверенный текст",
                "UncertainMarker",
                "ReviewMarker",
                status="unverified",
            ),
        ]
    service.storage.write_result(job_id, result)
    saved = service.storage.read_result(job_id)
    projection = comparison_report(saved, service.repository.get(job_id))
    assert {item["id"] for item in projection["differences"]} == (
        set() if only_excluded else {"T", "S", "U"}
    )
    assert projection["statistics"]["total"] == (0 if only_excluded else 2)
    assert projection["statistics"]["unverified"] == (0 if only_excluded else 1)

    base = f"/api/v1/comparisons/{job_id}"
    response = client.get(base + "/report")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    reader = PdfReader(io.BytesIO(response.content))
    assert reader.pages
    text = "".join("".join(page.extract_text().split()) for page in reader.pages)
    assert "ExcludedBeforeMarker" not in text and "ExcludedAfterMarker" not in text
    assert "Оформление" not in text and "Структуратаблицы" not in text
    if not only_excluded:
        for expected in (
            "70%",
            "50%",
            "BeforeMarker",
            "AfterMarker",
            "UncertainMarker",
            "ReviewMarker",
        ):
            assert expected in text
    for category in ("formatting_changed", "table_structure_changed"):
        assert client.get(base + "/differences?category=" + category).json()["total"] == 6
    assert service.storage.read_result(job_id) == saved


def test_excluded_inline_changes_have_no_rows_statistics_or_ui_items(client, service, tmp_path):
    """Сохранённое сравнение групп не возвращает ложные записи ни в UI, ни в PDF-отчёт."""
    from dataclasses import replace

    from app.domain.comparison import compare
    from tests.factories import version
    from tests.fakes import FakeExtractor

    service.limits = replace(service.limits, comparison_exclusions=(("-", "—", "–"),))
    service.extractor = FakeExtractor(
        version(["2.2. Санкт-Петербург, далее - Исполнитель"]),
        version(["2.2. Санкт—Петербург, далее – Исполнитель"], "pdf"),
    )
    job_id = report_job(client, service, tmp_path)
    base = f"/api/v1/comparisons/{job_id}"
    assert client.get(base + "/differences").json() == {"total": 0, "items": []}
    stats = client.get(base + "/result").json()["statistics"]
    assert stats["total"] == stats["unverified"] == 0
    response = client.get(base + "/report")
    assert response.status_code == 200
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(response.content)).pages)
    assert "Санкт" not in text
    assert not compare(
        service.extractor.old,
        service.extractor.new,
        exclusions=service.limits.comparison_exclusions,
    ).differences


def test_long_fragments_ai_status_and_deduplication(client, service, tmp_path):
    """Длинная строка переносится между страницами; AI-факт и гипотеза имеют разные статусы."""
    job_id = report_job(client, service, tmp_path)
    result = service.storage.restore_result(job_id)
    old = "\n".join(f"СтарыйФрагмент{i:04d} полный текст условия" for i in range(160))
    new = "\n".join(f"НовыйФрагмент{i:04d} полный текст условия" for i in range(160))
    fact = Difference(
        "AI-1", "text_modified", "Факт ИИ", old, new, "12", "12", source="ai", ai_mode="full"
    )
    result.differences += [
        fact,
        replace(fact, id="AI-DUP"),
        Difference(
            "H",
            "text_modified",
            "Гипотеза",
            "ГипотезаБыло",
            "ГипотезаСтало",
            status="needs_review",
            source="ai",
        ),
        Difference(
            "U",
            "unverified_region",
            "Непроверено",
            "ОбластьБыло",
            "ОбластьСтало",
            status="unverified",
        ),
    ]
    service.storage.write_result(job_id, result)
    projection = comparison_report(
        service.storage.read_result(job_id), service.repository.get(job_id)
    )
    assert projection["statistics"]["total"] == 2
    response = client.get(f"/api/v1/comparisons/{job_id}/report")
    assert response.status_code == 200
    reader = PdfReader(io.BytesIO(response.content))
    assert len(reader.pages) >= 3
    text = "\n".join(page.extract_text() for page in reader.pages)
    compact = "".join(text.split())
    for index in range(160):
        assert compact.count(f"СтарыйФрагмент{index:04d}") == 1
        assert compact.count(f"НовыйФрагмент{index:04d}") == 1
    assert "Требуетпроверки" in compact and "Непроверено" in compact
    assert "полнаяпроверка" in compact and "Непроверенныеобласти" in compact
    assert f"из {len(reader.pages)}" in text
    with pdfplumber.open(io.BytesIO(response.content)) as document:
        assert all(
            0 <= char["x0"] < char["x1"] <= page.width
            and 0 <= char["top"] < char["bottom"] <= page.height
            for page in document.pages
            for char in page.chars
        )


def test_ambiguous_category_is_never_labeled_confirmed(client, service, tmp_path):
    """Исторический статус confirmed у неоднозначной категории не становится подтверждением в отчёте."""
    job_id = report_job(client, service, tmp_path)
    result = service.storage.restore_result(job_id)
    result.differences = [
        Difference(
            "A", "ambiguous_match", "Спорная область", "Старое", "Новое", status="confirmed"
        ),
        Difference(
            "C",
            "ai_contradiction",
            "Противоречие",
            "Было",
            "Стало",
            status="needs_review",
            source="ai",
        ),
    ]
    service.storage.write_result(job_id, result)
    response = client.get(f"/api/v1/comparisons/{job_id}/report")
    text = "".join(
        "".join(page.extract_text().split())
        for page in PdfReader(io.BytesIO(response.content)).pages
    )
    assert "Неоднозначно" in text
    assert "Подтверждено" not in text
    assert "ПротиворечиеИИ" in text


def test_report_access_uses_owner_and_ttl(client, service, settings, tmp_path):
    """Другой браузер и истёкшее задание не получают PDF независимо от знания UUID."""
    job_id = report_job(client, service, tmp_path)
    path = f"/api/v1/comparisons/{job_id}/report"
    with TestClient(create_app(settings, service)) as stranger:
        assert stranger.get(path).status_code in {403, 404}
    service.repository.update(job_id, expires=time.time() - 1)
    assert client.get(path).status_code == 410


def test_report_download_from_telegram_read_only_session(settings, service, tmp_path):
    """Билет внешнего бота открывает ту же защищённую выдачу отчёта без новых OCR/VLM-запусков."""
    token = "synthetic-integration-token-" + "x" * 32
    settings.telegram_integration_token = SecretStr(token)
    settings.public_base_url = "http://192.168.55.3:8082"
    with TestClient(create_app(settings, service)) as bot:
        bot.headers["Authorization"] = "Bearer " + token
        from tests.factories import pdf

        data = pdf(tmp_path / "source.pdf", ["Source"]).read_bytes()
        response = bot.post(
            INTEGRATION,
            files={side: (side + ".pdf", data) for side in ("old", "new")},
            headers={"Idempotency-Key": "report-test-01"},
        )
        job_id = response.json()["job_id"]
        service.process(job_id)
        service.exporter = DocumentExporter(None)
        link = bot.post(f"{INTEGRATION}/{job_id}/access-link").json()["url"]
    ticket = parse_qs(urlsplit(link).fragment)["ticket"][0]
    before = service.extractor.calls
    with TestClient(create_app(settings, service)) as viewer:
        viewer.get("/open")
        response = viewer.post(
            "/api/v1/result-access",
            json={"ticket": ticket},
            headers={"X-Requested-With": "ContractDiff"},
        )
        assert response.status_code == 200, response.text
        downloaded = viewer.get(f"/api/v1/comparisons/{job_id}/report")
        assert downloaded.status_code == 200
        assert "70%" in PdfReader(io.BytesIO(downloaded.content)).pages[0].extract_text()
    assert service.extractor.calls == before
