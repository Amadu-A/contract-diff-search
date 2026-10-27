# tests/browser/test_ui.py
"""Браузерные сценарии с реальными PDF-адаптерами и локальной тестовой доставкой.

Тестовая очередь выполняет настоящий use-case вне HTTP-потока. Соединение
с общим RabbitMQ проверяется отдельно и этим тестом не подтверждается.
"""

import hashlib
import json
import shutil
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from pypdf import PdfReader

from app.application.ai_review import AILimits, AIReviewCoordinator
from app.application.golden import validate_matrix
from app.bootstrap import build_service
from app.settings import ROOT
from app.web.api import create_app
from tests.factories import pdf
from tests.unit.test_ai_review import Reviewer

pytestmark = pytest.mark.browser


class TestPublisher:
    """Ограниченная тестовая доставка для проверки UI при отсутствии общего брокера."""

    __test__ = False

    def __init__(self, service):
        """Использует один независимый worker-поток, сохраняя асинхронность HTTP."""
        self.service = service
        self.pool = ThreadPoolExecutor(max_workers=1)

    def publish(self, job_id: str) -> None:
        """Запускает тот же production-use-case, подменяя только транспорт доставки."""
        self.pool.submit(self.service.process, job_id)


@pytest.fixture
def live_server(settings):
    """Поднимает изолированный ASGI-сервер на свободном localhost-порту."""
    service = build_service(settings)
    publisher = TestPublisher(service)
    service.publisher = publisher
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, service),
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}", service
    server.should_exit = True
    thread.join(timeout=10)
    publisher.pool.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def browser_page():
    """Создаёт отдельный headless-профиль; пользовательские вкладки и cookies не читаются."""
    playwright = pytest.importorskip("playwright.sync_api")
    executable = shutil.which("chromium") or shutil.which("google-chrome")
    installed = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    if executable is None and installed.exists():
        executable = str(installed)
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(
                headless=True, executable_path=executable, args=["--disable-dev-shm-usage"]
            )
        except playwright.Error as exc:
            pytest.skip(f"Браузер Playwright не установлен: {str(exc)[:120]}")
        page = browser.new_page(viewport={"width": 1672, "height": 960}, accept_downloads=True)
        yield page
        browser.close()


def test_upload_compare_view_search_export_and_reset(live_server, browser_page, tmp_path):
    """Загрузка→worker→две страницы→детали→поиск→экспорт→сброс и адаптивность."""
    from playwright.sync_api import expect

    url, _ = live_server
    page = browser_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    expect(page.locator("[data-start]")).to_be_disabled()
    expect(page.locator("[data-ai]")).not_to_be_checked()
    screenshots = Path(".cache/screenshots")
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / "empty.png"), full_page=True)
    old = pdf(tmp_path / "our.pdf", ["2.2. Advance payment is 70%."])
    new = pdf(tmp_path / "partner.pdf", ["2.2. Advance payment is 50%."])
    page.locator("[data-upload=old] [data-file-input]").set_input_files(str(old))
    page.locator("[data-upload=new] [data-file-input]").set_input_files(str(new))
    expect(page.locator("[data-start]")).to_be_enabled()
    page.locator("[data-start]").click()
    expect(page.locator("[data-stage]")).to_contain_text("Сравнение завершено", timeout=60000)
    expect(page.locator("[data-stat=total]")).to_have_text("1")
    expect(page.locator("[data-detail-old]")).to_have_text("70%")
    expect(page.locator("[data-detail-new]")).to_have_text("50%")
    for image in page.locator("[data-page-image]").all():
        expect(image).to_be_visible()
        image.evaluate("image => image.decode()")
    assert page.locator(".viewer__region--selected").count() == 2
    page.screenshot(path=str(screenshots / "comparison.png"), full_page=True)
    viewer = page.locator("[data-viewer=new]")
    viewer.locator("[data-zoom]").select_option("1.5")
    assert viewer.locator("[data-paper]").evaluate("p => p.style.width") == "150%"
    viewer.locator("[data-rotate]").click()
    assert "90deg" in viewer.locator("[data-paper]").get_attribute("style")
    viewer.locator("[data-search-toggle]").click()
    viewer.locator("[data-search-input]").fill("50")
    viewer.locator("[data-search-form]").get_by_role("button", name="Найти", exact=True).click()
    expect(viewer.locator("[data-search-count]")).to_have_text("1/1")
    page.locator("[data-filter-toggle]").click()
    page.locator("[data-category-filter]").select_option("number_modified")
    expect(page.locator("[data-list-count]")).to_have_text("1")
    page.locator("[data-sync]").click()
    expect(page.locator("[data-sync]")).to_have_attribute("aria-pressed", "false")
    with page.expect_download() as download:
        viewer.locator("[data-export-pdf]").click()
    downloaded = tmp_path / "downloaded.pdf"
    download.value.save_as(str(downloaded))
    assert "50%" in PdfReader(downloaded).pages[0].extract_text()
    page.locator("[data-help]").click()
    expect(page.locator("[data-help-dialog]")).to_be_visible()
    page.get_by_role("button", name="Понятно", exact=True).click()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(screenshots / "mobile.png"), full_page=True)
    page.locator("[data-reset]").click()
    expect(page.locator("[data-start]")).to_be_disabled()
    expect(page.locator("[data-detail-body]")).to_be_hidden()
    assert not errors, errors


def test_invalid_file_and_api_error(live_server, browser_page, tmp_path):
    """Ошибки клиента и API остаются видимыми и не блокируют повторный запуск."""
    from playwright.sync_api import expect

    page = browser_page
    page.goto(live_server[0])
    invalid = tmp_path / "bad.exe"
    invalid.write_bytes(b"invalid")
    page.locator("[data-upload=old] [data-file-input]").set_input_files(str(invalid))
    expect(page.locator("[data-upload=old] [data-file-error]")).to_contain_text("DOC")
    valid = pdf(tmp_path / "valid.pdf", ["Safe data"])
    page.locator("[data-upload=old] [data-file-input]").set_input_files(str(valid))
    page.locator("[data-upload=new] [data-file-input]").set_input_files(str(valid))
    page.route(
        "**/api/v1/comparisons",
        lambda route: route.fulfill(status=422, json={"detail": "Тестовый отказ очереди"}),
    )
    page.locator("[data-start]").click()
    expect(page.locator("[data-toast]")).to_contain_text("Тестовый отказ очереди")
    expect(page.locator("[data-start]")).to_be_enabled()


@pytest.mark.parametrize(
    "full,targeted,failure",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, False),
        (True, False, True),
    ],
)
def test_ai_controls_and_visibility_export_without_recomparison(
    live_server, browser_page, tmp_path, full, targeted, failure
):
    """Четыре комбинации флагов и выключение пометок меняют проекции и оба PDF, сохраняя статистику."""
    from playwright.sync_api import expect

    url, service = live_server
    reviewer = Reviewer()
    review = reviewer.review
    failures = {"enabled": failure, "unit": None}

    def review_with_failure(unit, old, new, attempt):
        """Имитирует один адресный отказ при сохранении результатов остальных операций."""
        from app.domain.models import VerificationError

        failures["unit"] = failures["unit"] or unit.id
        if failures["enabled"] and unit.id == failures["unit"]:
            raise VerificationError("timeout")
        return review(unit, old, new, attempt)

    reviewer.review = review_with_failure
    service.verifier = AIReviewCoordinator(reviewer, AILimits())
    extract = service.extractor.extract
    calls = []

    def uncertain_extract(*args, **kwargs):
        """Имитирует локальную неуверенность OCR одного абзаца настоящего PDF."""
        model = extract(*args, **kwargs)
        calls.append(model.name)
        for element in model.elements:
            if "Penalty" in element.text:
                element.words = [
                    replace(word, verified=False, confidence=0.4) for word in element.words
                ]
        return model

    service.extractor.extract = uncertain_extract
    page = browser_page
    page.goto(url)
    page.locator("[data-ai-full]").set_checked(full)
    page.locator("[data-ai-unverified]").set_checked(targeted)
    for side, percent in [("old", "70"), ("new", "50")]:
        file = pdf(
            tmp_path / f"{side}.pdf",
            [f"2.2. Advance payment {percent}%.", f"5.2. Penalty {percent}%."],
        )
        page.locator(f"[data-upload={side}] [data-file-input]").set_input_files(str(file))
    with page.expect_response(
        lambda response: (
            response.url == url + "/api/v1/comparisons" and response.request.method == "POST"
        )
    ) as accepted:
        page.locator("[data-start]").click()
    job = accepted.value.json()
    assert (job["ai_full"], job["ai_unverified"]) == (full, targeted)
    expect(page.locator("[data-stage]")).to_contain_text("Сравнение завершено", timeout=60000)
    expect(page.locator("[data-stat=total]")).to_have_text("1")
    expect(page.locator("[data-stat=unverified]")).to_have_text("1")
    expect(page.locator("[data-list-count]")).to_have_text("2")
    if failure:
        expect(page.locator("[data-ai-retry]")).to_be_visible()
        calls_before_retry = len(calls)
        failures["enabled"] = False
        page.locator("[data-ai-retry]").click()
        expect(page.locator("[data-cancel]")).to_be_visible()
        expect(page.locator("[data-cancel]")).to_be_hidden(timeout=60000)
        expect(page.locator("[data-ai-retry]")).to_be_hidden()
        assert len(calls) == calls_before_retry
    if full or targeted:
        expect(page.locator("[data-ai-progress]")).to_be_visible()
        page.locator("[data-ai-details]").click()
        expect(page.locator("[data-ai-operations] p").first).to_be_visible()
    before_calls = len(calls), len(reviewer.calls)
    page.locator("[data-show-unverified-toggle]").uncheck()
    expect(page.locator("[data-list-count]")).to_have_text("1")
    expect(page.locator("[data-stat=unverified]")).to_have_text("1")
    assert page.locator(".viewer__region--warning").count() == 0
    for side in ("old", "new"):
        link = page.locator(f"[data-viewer={side}] [data-export-pdf]")
        expect(link).to_have_attribute("href", __import__("re").compile(r"show_unverified=false$"))
        with page.expect_download() as download:
            link.click()
        output = tmp_path / f"{side}-clean.pdf"
        download.value.save_as(str(output))
        colors = {
            tuple(float(n) for n in args)
            for args, operator in PdfReader(output).pages[0].get_contents().operations
            if operator in (b"RG", b"rg")
        }
        assert (0.85, 0.5, 0.05) not in colors
        assert (0.42, 0.46, 0.51) in colors
    page.locator("[data-show-unverified-toggle]").check()
    expect(page.locator("[data-list-count]")).to_have_text("2")
    assert (len(calls), len(reviewer.calls)) == before_calls


@pytest.mark.real
def test_real_contracts_matrix_and_visual_exports(live_server, browser_page, tmp_path):
    """Проверяет исходные договоры через UI и независимую матрицу, не публикуя их в Git."""
    from playwright.sync_api import expect

    originals = ROOT / "examples"
    old = next(originals.glob("*.docx"), None)
    new = next(originals.glob("*.pdf"), None)
    if old is None or new is None:
        pytest.skip("Локальные реальные договоры отсутствуют")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (old, new)}
    page = browser_page
    url, service = live_server
    page.goto(url)
    page.locator("[data-upload=old] [data-file-input]").set_input_files(str(old))
    page.locator("[data-upload=new] [data-file-input]").set_input_files(str(new))
    with page.expect_response(
        lambda response: (
            response.url == url + "/api/v1/comparisons" and response.request.method == "POST"
        )
    ) as accepted:
        page.locator("[data-start]").click()
    job = accepted.value.json()
    expect(page.locator("[data-stage]")).to_contain_text("Сравнение завершено", timeout=240000)
    result = service.storage.read_result(job["id"])
    matrix = json.loads((originals / "golden-matrix.json").read_text(encoding="utf-8"))
    validation = validate_matrix(matrix, result)
    output = ROOT / ".cache/real-verified"
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "validation.json").write_text(
        json.dumps({"hashes": hashes, **validation}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    matrix_entries = {entry["id"]: entry for entry in matrix["entries"]}
    for entry_id, filename in (
        ("SEED-001", "advance"),
        ("SEED-002", "penalty"),
        ("SEED-004", "account"),
    ):
        entry = matrix_entries[entry_id]
        section, old_value, new_value = entry["old_section"], entry["old_text"], entry["new_text"]
        difference = next(
            d
            for d in result["differences"]
            if d["old_section"] == section
            and d["old_text"] == old_value
            and d["new_text"].replace(" ", "") == new_value
        )
        visible = [d for d in result["differences"] if d.get("active", True)]
        index = next(i for i, d in enumerate(visible) if d["id"] == difference["id"])
        page.locator(f'[data-index="{index}"]').click()
        expect(page.locator("[data-detail-old]")).to_have_text(old_value)
        for image in page.locator("[data-page-image]").all():
            image.evaluate("image => image.decode()")
        page.wait_for_function("""() => [...document.querySelectorAll('[data-viewer]')].every(panel => {
            const viewport = panel.querySelector('[data-viewport]').getBoundingClientRect();
            const selected = panel.querySelector('.viewer__region--selected')?.getBoundingClientRect();
            return selected && selected.top >= viewport.top && selected.bottom <= viewport.bottom;
        })""")
        page.screenshot(path=str(output / f"ui-{filename}.png"), full_page=True)
    for side in ("old", "new"):
        with page.expect_download() as download:
            page.locator(f"[data-viewer={side}] [data-export-pdf]").click()
        download.value.save_as(str(output / f"{side}-highlighted.pdf"))
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (old, new)} == hashes
    assert validation["failed"] == 0, validation
