# tests/functional/test_telegram_integration.py
"""Серверная интеграция, атомарные повторы и ограниченный доступ к готовому результату."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.web.api import COOKIE, INTEGRATION, VIEW_COOKIE, create_app
from tests.factories import docx, pdf

pytest_plugins = ("tests.functional.test_lifecycle",)
TOKEN = "synthetic-server-secret-" + "x" * 40
CSRF = {"X-Requested-With": "ContractDiff"}


@pytest.fixture
def bot(settings, service):
    """Использует настоящий HTTP, SQLite и загрузчик с тестовой серверной авторизацией."""
    settings.telegram_integration_token = SecretStr(TOKEN)
    settings.public_base_url = "https://contracts.example.test"
    with TestClient(create_app(settings, service)) as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        yield client


@pytest.fixture
def documents(tmp_path):
    """Создаёт разные DOCX/PDF для проверки неизменяемого порядка сторон."""
    return {
        "old": ("our.docx", docx(tmp_path / "our.docx", ["Our version"]).read_bytes()),
        "new": ("partner.pdf", pdf(tmp_path / "partner.pdf", ["Partner version"]).read_bytes()),
    }


def send(bot, documents, key="request-0001", **flags):
    """Отправляет стандартный multipart-контракт внешнего клиента."""
    return bot.post(INTEGRATION, files=documents, data=flags, headers={"Idempotency-Key": key})


def ticket(bot, service, job_id):
    """Завершает настоящий use-case и получает переносимый одноразовый билет."""
    service.process(job_id)
    response = bot.post(f"{INTEGRATION}/{job_id}/access-link")
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["url"]).fragment)["ticket"][0], response.json()


@pytest.mark.parametrize(
    "full,targeted", [(False, False), (False, True), (True, False), (True, True)]
)
def test_existing_pipeline_and_side_order(bot, service, documents, full, targeted):
    """Бот использует один конвейер, два независимых флага и фиксированные old/new."""
    response = send(bot, documents, ai_full=str(full).lower(), ai_unverified=str(targeted).lower())
    assert response.status_code == 202, response.text
    body = response.json()
    assert (body["ai_full"], body["ai_unverified"]) == (full, targeted)
    assert body["expires_at"] is None and not body["result_ready"]
    job = service.repository.get(body["job_id"])
    assert job.old_name == "our.docx" and job.new_name == "partner.pdf"
    assert service.extractor.calls == 0
    assert (service.storage.directory(job.id) / "old.docx").read_bytes() == documents["old"][1]
    assert (service.storage.directory(job.id) / "new.pdf").read_bytes() == documents["new"][1]
    assert bot.post(f"{INTEGRATION}/{job.id}/access-link").status_code == 409
    service.process(job.id)
    current = bot.get(f"{INTEGRATION}/{job.id}").json()
    assert current["result_ready"] and current["statistics"]["total"] == 1
    assert current["expires_at"] and current["stage"] == "completed"
    assert service.extractor.calls == 2
    assert not {"owner", "old_name", "new_name", "lease", "pdf", "error"} & current.keys()


def test_idempotency_atomic_repeats_and_conflicts(bot, service, documents):
    """Конкурентные повторы публикуют одну задачу; различающиеся параметры получают 409."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: send(bot, documents), range(4)))
    assert {r.status_code for r in replies} == {202}
    assert len({r.json()["job_id"] for r in replies}) == 1
    assert len(service.publisher.ids) == 1
    assert send(bot, documents, ai_full="true").status_code == 409
    changed = dict(documents, old=documents["new"])
    assert send(bot, changed).status_code == 409


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic invalid", b"Bearer \xff"])
def test_authentication_precedes_reading_body(settings, service, authorization):
    """Отказ токена не читает даже поток тела, а не только запрещает публикацию."""
    settings.telegram_integration_token = SecretStr(TOKEN)

    def body():
        """Сигнализирует ошибку, если транспорт попытался прочитать неавторизованную загрузку."""
        raise AssertionError("Тело запроса не должно читаться")
        yield b"unreachable"

    with TestClient(create_app(settings, service)) as client:
        headers = {"Authorization": authorization} if authorization else {}
        assert client.post(INTEGRATION, content=body(), headers=headers).status_code == 401
    assert not service.publisher.ids


def test_link_exchange_is_single_use_read_only_and_scoped(bot, service, settings, documents):
    """Новый браузер получает все проекции одного результата без права управления и повторного OCR."""
    job_id = send(bot, documents).json()["job_id"]
    token, link = ticket(bot, service, job_id)
    assert link["url"].startswith(settings.public_base_url + "/open#ticket=")
    assert link["link_expires_at"] <= link["result_expires_at"]
    with TestClient(create_app(settings, service)) as browser:
        base = f"/api/v1/comparisons/{job_id}"
        assert browser.get("/open").status_code == 200
        assert COOKIE not in browser.cookies
        assert browser.get(base + "/result").status_code == 404
        assert browser.get("/api/v1/result-access").status_code == 405
        exchange = browser.post("/api/v1/result-access", json={"ticket": token}, headers=CSRF)
        assert exchange.status_code == 200
        assert "HttpOnly" in exchange.headers["set-cookie"]
        assert "SameSite=strict" in exchange.headers["set-cookie"]
        assert browser.cookies.get(VIEW_COOKIE)
        assert (
            browser.post("/api/v1/result-access", json={"ticket": token}, headers=CSRF).status_code
            == 410
        )
        for suffix in (
            "",
            "/result",
            "/differences",
            "/regions/new/1",
            "/ai/operations",
            "/correspondence/old/1",
        ):
            assert browser.get(base + suffix).status_code == 200
        for suffix in ("/cancel", "/ai/retry"):
            assert browser.post(base + suffix, headers=CSRF).status_code in {403, 404}
        assert browser.delete(base, headers=CSRF).status_code in {403, 404}
        assert browser.post("/api/v1/comparisons", files=documents, headers=CSRF).status_code == 403
        assert (
            browser.get(
                "/api/v1/comparisons/00000000-0000-0000-0000-000000000000/result"
            ).status_code
            == 404
        )
        assert service.extractor.calls == 2 and len(service.publisher.ids) == 1
        service.repository.update(job_id, expires=time.time() - 1)
        assert browser.get(base + "/result").status_code in {404, 410}
        assert bot.get(f"{INTEGRATION}/{job_id}").json()["status"] == "expired"
        assert bot.post(f"{INTEGRATION}/{job_id}/access-link").status_code == 410


def test_link_expiry_rotation_and_no_secret_storage(bot, service, settings, documents, caplog):
    """БД хранит лишь отпечатки; просроченный билет и старый серверный секрет не работают."""
    job_id = send(bot, documents).json()["job_id"]
    token, _ = ticket(bot, service, job_id)
    with service.repository.connection() as db:
        stored = json.dumps([dict(r) for r in db.execute("SELECT * FROM result_access")])
        assert token not in stored and TOKEN not in stored
        db.execute("UPDATE result_access SET expires=?", (time.time() - 1,))
    assert (
        bot.post("/api/v1/result-access", json={"ticket": token}, headers=CSRF).status_code == 410
    )
    settings.telegram_integration_token = SecretStr("replacement-" + "y" * 48)
    assert bot.get(f"{INTEGRATION}/{job_id}").status_code == 401
    bot.headers["Authorization"] = (
        "Bearer " + settings.telegram_integration_token.get_secret_value()
    )
    assert bot.get(f"{INTEGRATION}/{job_id}").status_code == 200
    assert (
        TOKEN not in caplog.text and token not in caplog.text and "Our version" not in caplog.text
    )


def test_owner_boundary_limits_csrf_and_cleanup(bot, client, service, settings, documents):
    """Интеграция не получает чужие задания; лимиты атомарны и CSRF сайта сохранён."""
    own = client.post("/api/v1/comparisons", files=documents).json()["id"]
    assert bot.get(f"{INTEGRATION}/{own}").status_code == 404
    assert bot.post(f"{INTEGRATION}/{own}/access-link").status_code == 404
    settings.telegram_max_active_jobs = 1
    job_id = send(bot, documents).json()["job_id"]
    assert send(bot, documents, "request-0002").status_code == 429
    assert bot.post("/api/v1/comparisons", files=documents).status_code == 403
    assert (
        bot.post(
            INTEGRATION, files=documents, headers={"Origin": "https://outside.test"}
        ).status_code
        == 403
    )
    settings.telegram_uploads_per_minute = 2
    assert send(bot, documents, "request-0003").status_code == 429
    service.repository.update(
        job_id, status="expired", expires=time.time() - 7200, updated=time.time() - 7200
    )
    with service.repository.connection() as db:
        db.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time() - 7200, job_id))
    service.cleanup()
    assert service.repository.get(job_id) is None


def test_missing_configuration_bad_upload_and_broker_failure(bot, service, settings, documents):
    """Ошибки конфигурации, файлов и очереди имеют безопасные публичные коды."""
    assert send(bot, dict(documents, old=("bad.docx", b"invalid"))).status_code == 422
    settings.public_base_url = ""
    job_id = send(bot, documents, "request-0002").json()["job_id"]
    service.process(job_id)
    assert bot.post(f"{INTEGRATION}/{job_id}/access-link").status_code == 503

    def unavailable(job_id):
        """Имитирует отказ транспорта без включения внутренних данных в HTTP-ответ."""
        raise RuntimeError("private broker diagnostics")

    service.publisher.publish = unavailable
    response = send(bot, documents, "request-0003")
    assert response.status_code == 503 and "private" not in response.text
    settings.telegram_integration_token = SecretStr("")
    assert bot.get(INTEGRATION).status_code == 503


def test_status_reports_checkpoint_while_ai_is_still_processing(bot, service, documents):
    """Готовность и окончательный TTL не подменяют уже доступные фактические счётчики."""
    from app.domain.comparison import compare
    from tests.factories import version

    job_id = send(bot, documents, ai_unverified="true").json()["job_id"]
    assert service.repository.claim(job_id, "test-lease")
    service.storage.write_result(
        job_id, compare(version(["2.2. Аванс 70%"]), version(["2.2. Аванс 50%"]))
    )
    service.repository.update(
        job_id, ai_progress={"unverified": {"status": "running", "planned": 3, "processed": 1}}
    )
    state = bot.get(f"{INTEGRATION}/{job_id}").json()
    assert state["status"] == "processing" and not state["result_ready"]
    assert state["expires_at"] is None and state["statistics"]["total"] == 1
    assert state["ai"]["unverified"]["processed"] == 1
    assert bot.post(f"{INTEGRATION}/{job_id}/access-link").status_code == 409


def test_concurrent_activation_only_creates_one_view_session(bot, service, documents):
    """Один билет не расходуется дважды даже при одновременном POST из разных запросов."""
    job_id = send(bot, documents).json()["job_id"]
    token, _ = ticket(bot, service, job_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(
            pool.map(
                lambda _: bot.post("/api/v1/result-access", json={"ticket": token}, headers=CSRF),
                range(4),
            )
        )
    assert [r.status_code for r in replies].count(200) == 1
    assert [r.status_code for r in replies].count(410) == 3
    with service.repository.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM result_access WHERE kind='session'").fetchone()[0] == 1
        )


def test_validation_openapi_and_upload_limit(bot, service, settings, documents):
    """OpenAPI описывает server Bearer и multipart; неверные поля и большой файл отклоняются штатно."""
    schema = bot.get("/openapi.json").json()
    assert schema["paths"][INTEGRATION]["post"]["security"] == [{"TelegramIntegration": []}]
    assert "multipart/form-data" in schema["paths"][INTEGRATION]["post"]["requestBody"]["content"]
    assert bot.post(INTEGRATION, files=documents).status_code == 422
    assert send(bot, documents, ai_full="not-a-boolean").status_code == 422
    settings.max_file_mb = 1
    large = dict(documents, new=("partner.pdf", documents["new"][1] + bytes(1024**2)))
    assert send(bot, large).status_code == 413
    settings.telegram_requests_per_minute = 1
    assert bot.get(INTEGRATION + "/00000000-0000-0000-0000-000000000000").status_code == 404
    assert bot.get(INTEGRATION + "/00000000-0000-0000-0000-000000000000").status_code == 429
