# app/bootstrap.py
"""Единственный composition root для HTTP, worker и плановой очистки."""

from app.application.service import ComparisonService, Limits
from app.infrastructure.exports import DocumentExporter
from app.infrastructure.ocr import TesseractOCR
from app.infrastructure.pdf import PdfDocumentExtractor
from app.infrastructure.queue import CeleryPublisher, make_celery
from app.infrastructure.repository import SqliteJobRepository
from app.infrastructure.storage import FileStorage
from app.infrastructure.verifier import SharedVlmVerifier
from app.infrastructure.visual_compare import ImageComparer
from app.infrastructure.word import LibreOffice
from app.settings import Settings


def build_service(settings: Settings) -> ComparisonService:
    """Собирает адаптеры явно; прикладные сценарии ничего не знают об их классах."""
    storage = FileStorage(settings)
    repository = SqliteJobRepository(settings.data_dir.resolve() / "jobs.sqlite3")
    office, ocr = LibreOffice(settings), TesseractOCR(settings)
    return ComparisonService(
        repository,
        storage,
        CeleryPublisher(make_celery(settings)),
        PdfDocumentExtractor(settings, ocr, office),
        DocumentExporter(office),
        SharedVlmVerifier(settings),
        Limits(
            settings.max_jobs,
            settings.max_session_jobs,
            settings.retention_seconds,
            settings.job_timeout_seconds,
        ),
        ImageComparer(),
    )
