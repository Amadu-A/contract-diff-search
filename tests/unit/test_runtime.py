# tests/unit/test_runtime.py
"""Постоянные лимиты нативных потоков и лёгкая адресная проверка живого worker."""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app import worker_health
from app.runtime import configure_native_threads


def test_native_limits_override_inherited_excessive_values(settings, monkeypatch):
    """Унаследованные 64 потока не проходят через валидированные настройки приложения."""
    names = (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "BLIS_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "OMP_THREAD_LIMIT",
    )
    for name in names:
        monkeypatch.setenv(name, "64")
    configure_native_threads(settings)
    assert {os.environ[name] for name in names} == {"1"}


@pytest.mark.parametrize(
    "reply,expected",
    [
        ([{"celery@local": {"ok": "pong"}}], 0),
        ([{"celery@other": {"ok": "pong"}}], 1),
        ([], 1),
        ([{"celery@local": {"error": "busy"}}], 1),
    ],
)
def test_health_requires_pong_from_this_container(settings, monkeypatch, reply, expected):
    """Чужой healthy worker не маскирует неработающий worker текущего контейнера."""
    calls = []

    def ping(**kwargs):
        """Регистрирует целевое имя и ограниченное ожидание контрольного запроса."""
        calls.append(kwargs)
        return reply

    monkeypatch.setattr(worker_health, "Settings", lambda: settings)
    monkeypatch.setattr(worker_health, "gethostname", lambda: "local")
    monkeypatch.setattr(
        worker_health,
        "make_celery",
        lambda settings: SimpleNamespace(control=SimpleNamespace(ping=ping)),
    )
    assert worker_health.main() == expected
    assert calls == [
        {"destination": ["celery@local"], "timeout": settings.worker_health_timeout_seconds}
    ]


def test_health_import_does_not_load_native_ocr_libraries():
    """Отдельный процесс healthcheck не импортирует NumPy/SciPy/PDF и не создаёт их пулы."""
    code = "import sys; import app.worker_health; assert not {'numpy','scipy','app.infrastructure.pdf','app.bootstrap','app.infrastructure.ocr'} & sys.modules.keys()"
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15
    )
    assert completed.returncode == 0, completed.stderr


def test_asgi_limits_are_applied_before_importing_pdf_transport():
    """Даже прямой ASGI-импорт применяет лимит до загрузки адаптера с нативными библиотеками."""
    code = """import os, sys, types
module=types.ModuleType('app.web.api')
def attribute(name):
    if name!='create_app': raise AttributeError(name)
    assert os.environ['OPENBLAS_NUM_THREADS']=='1'
    return lambda settings: 'configured-before-pdf'
module.__getattr__=attribute
sys.modules['app.web.api']=module
import app.main
assert app.main.app=='configured-before-pdf'
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        env=dict(os.environ, OPENBLAS_NUM_THREADS="64", NATIVE_THREADS="1"),
    )
    assert completed.returncode == 0, completed.stderr
