# tests/integration/test_storage.py
"""Атомарные checkpoint при конкурентном чтении и кратковременных файловых блокировках."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from app.domain.comparison import compare
from app.infrastructure.storage import FileStorage
from tests.factories import version

pytestmark = pytest.mark.integration


@pytest.fixture
def checkpoint_storage(settings):
    """Создаёт настоящий JSON-файл завершённого базового сравнения."""
    storage = FileStorage(settings)
    job = str(uuid4())
    storage.directory(job).mkdir()
    result = compare(version(["1. Аванс 50%"]), version(["1. Аванс 70%"]))
    storage.write_result(job, result)
    return storage, job, result


@pytest.mark.parametrize("operation", ["read", "replace"])
def test_short_file_lock_keeps_atomic_checkpoint(checkpoint_storage, monkeypatch, operation):
    """После краткого отказа читается/публикуется целый результат, а исходное исключение не маскируется."""
    storage, job, result = checkpoint_storage
    method = "read_text" if operation == "read" else "replace"
    original = getattr(Path, method)
    locked = {"remaining": 2}

    def temporary_lock(path, *args, **kwargs):
        """Имитирует только временный запрет доступа к целевому checkpoint."""
        if path.name in {"result.json", "result.tmp"} and locked["remaining"]:
            locked["remaining"] -= 1
            raise PermissionError("Кратковременная блокировка файла")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, temporary_lock)
    if operation == "replace":
        result.old.name = "updated.docx"
        storage.write_result(job, result)
    data = storage.read_result(job)
    assert data["old"]["name"] == result.old.name
    assert data["differences"][0]["old_text"] == "50%"
    assert data["differences"][0]["new_text"] == "70%"


@pytest.mark.parametrize("operation", ["read", "replace"])
def test_permanent_file_lock_is_not_silenced(checkpoint_storage, monkeypatch, operation):
    """Постоянный запрет доступа заканчивается исходной ошибкой, а не бесконечным ожиданием."""
    storage, job, result = checkpoint_storage

    def denied(*args, **kwargs):
        """Имитирует постоянный отказ файловой системы."""
        raise PermissionError("Постоянная блокировка")

    monkeypatch.setattr(Path, "read_text" if operation == "read" else "replace", denied)
    with pytest.raises(PermissionError, match="Постоянная"):
        if operation == "read":
            storage.read_result(job)
        else:
            storage.write_result(job, result)


def test_concurrent_readers_never_observe_partial_json(checkpoint_storage):
    """Четыре читателя и автор видят только согласованные старую или новую версии JSON."""
    storage, job, result = checkpoint_storage
    result.old.name = result.new.name = "revision-0"
    storage.write_result(job, result)
    ready = Barrier(5)

    def write():
        """Публикует последовательные согласованные снимки через настоящий rename."""
        ready.wait()
        for revision in range(40):
            result.old.name = result.new.name = f"revision-{revision}"
            storage.write_result(job, result)

    def read():
        """Проверяет полное содержимое при каждом конкурентном чтении."""
        ready.wait()
        for _ in range(100):
            data = storage.read_result(job)
            assert data["old"]["name"] == data["new"]["name"]
            assert data["differences"][0]["status"] == "confirmed"

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(write), *(executor.submit(read) for _ in range(4))]
        for future in futures:
            future.result(timeout=15)
