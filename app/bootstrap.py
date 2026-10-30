# app/bootstrap.py
"""Единственный composition root для HTTP, worker и плановой очистки."""

from app.application.ai_review import AILimits, AIReviewCoordinator
from app.application.service import ComparisonService, Limits
from app.runtime import configure_native_threads
from app.settings import Settings


def build_service(settings: Settings) -> ComparisonService:
    """Собирает адаптеры явно; прикладные сценарии ничего не знают об их классах."""
    configure_native_threads(settings)
    from app.infrastructure.ai_geometry import PdfGeometryRefiner
    from app.infrastructure.exports import DocumentExporter
    from app.infrastructure.ocr import TesseractOCR
    from app.infrastructure.pdf import PdfDocumentExtractor
    from app.infrastructure.queue import CeleryPublisher, make_celery
    from app.infrastructure.repository import SqliteJobRepository
    from app.infrastructure.storage import FileStorage
    from app.infrastructure.verifier import SharedVlmVerifier
    from app.infrastructure.visual_compare import ImageComparer
    from app.infrastructure.word import LibreOffice

    storage = FileStorage(settings)
    repository = SqliteJobRepository(settings.data_dir.resolve() / "jobs.sqlite3")
    office, ocr = LibreOffice(settings), TesseractOCR(settings)
    return ComparisonService(
        repository,
        storage,
        CeleryPublisher(make_celery(settings)),
        PdfDocumentExtractor(settings, ocr, office),
        DocumentExporter(office),
        AIReviewCoordinator(
            SharedVlmVerifier(settings, ocr=ocr),
            AILimits(
                settings.vlm_batch_size,
                settings.vlm_concurrency,
                settings.vlm_retries,
                settings.vlm_total_seconds,
                settings.vlm_retry_delay_seconds,
                settings.vlm_context_chars,
                settings.vlm_regions_per_request,
            ),
            PdfGeometryRefiner(),
        ),
        Limits(
            settings.max_jobs,
            settings.max_session_jobs,
            settings.retention_seconds,
            settings.job_timeout_seconds,
            frozenset(
                line.strip()
                for line in settings.comparison_exclusions_file.read_text(
                    encoding="utf-8-sig"
                ).splitlines()
                if line.strip()
            ),
        ),
        ImageComparer(),
    )
