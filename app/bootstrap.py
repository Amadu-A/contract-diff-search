# app/bootstrap.py
"""Единственный composition root для HTTP, worker и плановой очистки."""

from app.application.ai_review import AILimits, AIReviewCoordinator
from app.application.service import ComparisonService, Limits
from app.domain.text import EquivalenceGroups, normalize
from app.runtime import configure_native_threads
from app.settings import Settings


def load_exclusions(settings: Settings) -> EquivalenceGroups:
    """Читает UTF-8-группы; ошибочная строка или пересечение групп останавливает запуск с пояснением."""
    path = settings.comparison_exclusions_file
    groups, seen = [], set()
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        group = tuple(normalize(value.strip()).text for value in line[1:-1].split(","))
        if (
            not line.startswith("[")
            or not line.endswith("]")
            or len(group) < 2
            or any(not value or "[" in value or "]" in value for value in group)
            or len(set(group)) != len(group)
            or seen.intersection(group)
        ):
            raise ValueError(
                f"{path}: строка {number}: ожидается группа [значение, значение]; "
                "значения должны быть непустыми и не повторяться внутри или между группами"
            )
        groups.append(group)
        seen.update(group)
    return tuple(groups)


def build_service(settings: Settings) -> ComparisonService:
    """Собирает адаптеры явно; прикладные сценарии ничего не знают об их классах."""
    configure_native_threads(settings)
    exclusions = load_exclusions(settings)
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
            exclusions,
        ),
        ImageComparer(),
    )
