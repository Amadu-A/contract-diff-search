# tests/conftest.py
"""Общие тестовые настройки; локальные инструменты подставляются только через Settings."""

import shutil

import pytest

from app.settings import ROOT, Settings


def pytest_addoption(parser):
    """Включает реальные конфиденциальные файлы только явным флагом локального прогона."""
    parser.addoption(
        "--real-documents",
        action="store_true",
        help="Проверить локальные договоры examples; не применять в публичном CI",
    )


def pytest_collection_modifyitems(config, items):
    """Не подменяет отсутствие реальных договоров успешной интеграцией."""
    if not config.getoption("--real-documents"):
        marker = pytest.mark.skip(
            reason="Реальные договоры проверяются отдельно с --real-documents"
        )
        for item in items:
            if item.get_closest_marker("real"):
                item.add_marker(marker)


@pytest.fixture
def settings(tmp_path):
    """Использует изолированный каталог и обнаруженные OCR/Office, не читая секреты .env."""
    tesseract = shutil.which("tesseract")
    office = shutil.which("soffice")
    local_tesseract = ROOT / ".cache/tesseract/tesseract.exe"
    if not tesseract and local_tesseract.exists():
        tesseract = str(local_tesseract)
    local_office = ROOT / ".cache/libreoffice-runtime/LibreOffice/program/soffice.exe"
    if not office and local_office.exists():
        office = str(local_office)
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        tesseract_command=tesseract or "missing-tesseract",
        soffice_command=office or "missing-soffice",
        process_timeout_seconds=60,
    )
