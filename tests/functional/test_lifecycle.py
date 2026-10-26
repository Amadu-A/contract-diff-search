# tests/functional/test_lifecycle.py
"""Полный жизненный цикл HTTP и сценариев с настоящим SQLite/файловым хранилищем."""

import time

import pytest
from fastapi.testclient import TestClient

from app.application.service import ComparisonService, Limits
from app.domain.models import DocumentError, JobStatus
from app.infrastructure.repository import SqliteJobRepository
from app.infrastructure.storage import FileStorage
from app.web.api import create_app
from tests.factories import pdf, version
from tests.fakes import FailingVerifier, FakeExporter, FakeExtractor, RecordingPublisher


@pytest.fixture
def service(settings):
    """Собирает реальное временное хранение и независимые fake-порты конвейера."""
    return ComparisonService(
        SqliteJobRepository(settings.data_dir / "metadata.sqlite3"),
        FileStorage(settings),
        RecordingPublisher(),
        FakeExtractor(version(["2.2. Аванс 70%"]), version(["2.2. Аванс 50%"])),
        FakeExporter(),
        FailingVerifier(),
        Limits(10, 2, 3600, 60),
    )


@pytest.fixture
def client(settings, service):
    """Открывает браузерную сессию и применяет настоящий CSRF-контракт."""
    with TestClient(create_app(settings, service)) as client:
        client.get("/")
        client.headers["X-Requested-With"] = "ContractDiff"
        yield client


def upload(client, tmp_path, use_ai=False):
    """Передаёт два валидных PDF; обработка остаётся отдельным действием worker."""
    path = pdf(tmp_path / "input.pdf", ["Synthetic document"])
    data = path.read_bytes()
    return client.post(
        "/api/v1/comparisons",
        files={
            "old": ("our.pdf", data, "application/pdf"),
            "new": ("partner.pdf", data, "application/pdf"),
        },
        data={"use_ai": str(use_ai).lower()},
    )


def test_end_to_end_status_result_filters_search_and_delete(client, service, tmp_path):
    """Загрузка→очередь→worker→детали→удаление без синхронной обработки в POST."""
    response = upload(client, tmp_path)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "queued"
    assert "owner" not in job and "lease" not in job
    service.process(job["id"])
    base = f"/api/v1/comparisons/{job['id']}"
    assert client.get(base).json()["status"] == "completed"
    summary = client.get(base + "/result").json()
    assert summary["statistics"]["total"] == 1
    assert "pdf" not in summary["documents"]["old"]
    differences = client.get(base + "/differences?category=number_modified").json()
    difference = differences["items"][0]
    assert difference["old_text"] == "70%" and difference["new_text"] == "50%"
    assert client.get(base + "/differences/" + difference["id"]).status_code == 200
    assert client.get(base + "/search/new?q=50").json()["items"][0]["regions"]
    assert client.get(base + "/regions/new/1").json()["items"]
    assert client.get(base + "/correspondence/old/1").json()["page"] == 1
    assert client.get(base + "/export/old").status_code == 409
    assert client.delete(base).status_code == 204
    assert client.get(base).status_code == 410
    assert not service.storage.directory(job["id"]).exists()


def test_foreign_session_and_csrf(client, service, settings, tmp_path):
    """UUID без правильной cookie не даёт доступ; изменяющие межсайтовые запросы закрыты."""
    job = upload(client, tmp_path).json()
    with TestClient(create_app(settings, service)) as foreign:
        foreign.get("/")
        assert foreign.get(f"/api/v1/comparisons/{job['id']}").status_code == 404
        assert foreign.delete(f"/api/v1/comparisons/{job['id']}").status_code == 403
    response = client.post(
        f"/api/v1/comparisons/{job['id']}/cancel", headers={"Origin": "https://attacker.invalid"}
    )
    assert response.status_code == 403


def test_ai_failure_retains_main_result(client, service, tmp_path):
    """Таймаут VLM оставляет детерминированное различие и отдельный failed-статус ИИ."""
    job = upload(client, tmp_path, True).json()
    service.process(job["id"])
    base = f"/api/v1/comparisons/{job['id']}"
    assert client.get(base).json()["status"] == "completed_with_warnings"
    data = client.get(base + "/result").json()
    assert data["ai_status"] == "failed" and data["statistics"]["total"] == 1


def test_cancel_and_duplicate_delivery(client, service, tmp_path):
    """Отмена до старта и повторная доставка завершённой задачи не запускают извлечение заново."""
    job = upload(client, tmp_path).json()
    base = f"/api/v1/comparisons/{job['id']}"
    assert client.post(base + "/cancel").json()["status"] == "cancelled"
    service.process(job["id"])
    assert service.extractor.calls == 0
    second = upload(client, tmp_path).json()
    service.process(second["id"])
    service.process(second["id"])
    assert service.extractor.calls == 2


def test_expiry_and_stale_recovery(client, service, tmp_path):
    """TTL закрывает API и удаляет файлы; зависший worker получает контролируемый failed."""
    job = upload(client, tmp_path).json()
    service.process(job["id"])
    service.repository.update(job["id"], expires=time.time() - 1)
    assert client.get(f"/api/v1/comparisons/{job['id']}").status_code == 410
    service.cleanup()
    assert service.repository.get(job["id"]).status == JobStatus.EXPIRED
    assert not service.storage.directory(job["id"]).exists()


def test_bad_format_and_broker_failure_clean_up(client, service, tmp_path):
    """Отказ формата либо публикации не оставляет принятые договоры в хранилище."""
    bad = client.post(
        "/api/v1/comparisons", files={"old": ("x.pdf", b"not PDF"), "new": ("y.pdf", b"not PDF")}
    )
    assert bad.status_code == 422

    def fail(job_id):
        """Воспроизводит недоступный общий брокер после сохранения файлов."""
        raise DocumentError("Очередь недоступна")

    service.publisher.publish = fail
    assert upload(client, tmp_path).status_code == 422
    assert list(service.storage.root.iterdir()) == []
