# app/application/service.py
"""Сценарии запуска, обработки, отмены и удаления временного сравнения.

Транзакционная граница метаданных находится в репозитории. Результат
публикуется только после проверки аренды; повторная доставка безопасна.
"""

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from app.application.ai_review import public_progress
from app.application.ports import (
    DocumentExtractor,
    Exporter,
    JobRepository,
    JobStorage,
    TaskPublisher,
    Verifier,
    VisualComparator,
)
from app.domain.comparison import compare
from app.domain.models import (
    TERMINAL,
    AccessDenied,
    AIModes,
    Cancelled,
    ComparisonJob,
    DocumentError,
    JobStatus,
)
from app.observability import timed


@dataclass(frozen=True)
class Limits:
    """Прикладные ограничения, не связанные с механизмом загрузки settings."""

    max_jobs: int
    max_session_jobs: int
    retention: int
    timeout: int


def owner_hash(capability: str) -> str:
    """Хранит односторонний отпечаток браузерного capability, а не секрет cookie."""
    return hashlib.sha256(capability.encode()).hexdigest()


class ComparisonService:
    """Оркестрирует жизненный цикл через порты без SQL, FastAPI и PDF-библиотек."""

    def __init__(
        self,
        repository: JobRepository,
        storage: JobStorage,
        publisher: TaskPublisher,
        extractor: DocumentExtractor,
        exporter: Exporter,
        verifier: Verifier,
        limits: Limits,
        visual_comparator: VisualComparator | None = None,
    ):
        """Получает готовые адаптеры из composition root; сам их не конструирует."""
        self.repository, self.storage, self.publisher = repository, storage, publisher
        self.visual_comparator = visual_comparator
        self.extractor, self.exporter, self.verifier, self.limits = (
            extractor,
            exporter,
            verifier,
            limits,
        )

    @timed("comparison_submit")
    def submit(
        self,
        capability: str,
        old_name: str,
        old_stream: BinaryIO,
        new_name: str,
        new_stream: BinaryIO,
        use_ai: bool = False,
        *,
        ai_full: bool | None = None,
        ai_unverified: bool = False,
    ) -> ComparisonJob:
        """Резервирует квоту, сохраняет документы и публикует долговечную задачу."""
        now, job_id = time.time(), str(uuid4())
        full = use_ai if ai_full is None and not ai_unverified else bool(ai_full)
        job = ComparisonJob(
            job_id,
            owner_hash(capability),
            JobStatus.QUEUED,
            "В очереди",
            now,
            now,
            now + self.limits.timeout + self.limits.retention,
            full or ai_unverified,
            Path(old_name.replace("\\", "/")).name[:240],
            Path(new_name.replace("\\", "/")).name[:240],
            ai_full=full,
            ai_unverified=ai_unverified,
        )
        self.repository.create(job, self.limits.max_jobs, self.limits.max_session_jobs)
        try:
            self.storage.save_upload(job_id, "old", job.old_name, old_stream)
            self.storage.save_upload(job_id, "new", job.new_name, new_stream)
            self.publisher.publish(job_id)
            return job
        except Exception:
            self.storage.delete(job_id)
            self.repository.update(
                job_id, status=JobStatus.EXPIRED, expires=time.time(), stage="Не принято"
            )
            raise

    def owned_job(self, job_id: str, capability: str) -> ComparisonJob:
        """Проверяет capability и срок действия; один UUID права доступа не даёт."""
        job = self.repository.get(job_id)
        if job is None or not secrets.compare_digest(job.owner, owner_hash(capability)):
            raise AccessDenied()
        if job.status == JobStatus.EXPIRED or job.expires <= time.time():
            raise DocumentError("Срок хранения истёк; загрузите документы повторно")
        if job.status in TERMINAL:
            for mode in ("full", "unverified"):
                report = job.ai_progress.get(mode, {})
                if report.get("status") in {"running", "queued"}:
                    report["status"] = (
                        "cancelled"
                        if job.status == JobStatus.CANCELLED
                        else "partial"
                        if report.get("succeeded")
                        else "failed"
                    )
        return job

    def result(self, job_id: str, capability: str) -> dict:
        """Выдаёт результат только завершённого задания его владельцу."""
        job = self.owned_job(job_id, capability)
        if job.status not in {JobStatus.COMPLETED, JobStatus.WARNINGS} and not (
            job.status
            in {JobStatus.PROCESSING, JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.QUEUED}
            and self.storage.has_result(job_id)
        ):
            raise DocumentError("Результат ещё не готов")
        data = self.storage.read_result(job_id)
        if job.status in TERMINAL and data.get("ai_status") == "running":
            data["ai_status"] = (
                "partial"
                if any(
                    u.get("status") == "completed"
                    for u in data.get("ai_review", {}).get("units", [])
                )
                else "failed"
            )
            for mode in ("full", "unverified"):
                data.get("ai_review", {}).get(mode, {}).update(job.ai_progress.get(mode, {}))
        return data

    def retry_ai(self, job_id: str, capability: str) -> ComparisonJob:
        """Повторяет неудачные единицы по сохранённому плану без повторного OCR и сравнения."""
        job = self.owned_job(job_id, capability)
        if not job.use_ai or not self.storage.has_result(job_id):
            raise DocumentError("Нет сохранённого результата для повтора ИИ")
        if not self.repository.requeue_ai(
            job_id,
            time.time() + self.limits.timeout + self.limits.retention,
            self.limits.max_session_jobs,
        ):
            raise DocumentError("Проверка уже выполняется")
        try:
            self.publisher.publish(job_id)
        except Exception:
            self.repository.update(
                job_id, status=JobStatus.WARNINGS, stage="Повтор не поставлен в очередь"
            )
            raise
        return self.owned_job(job_id, capability)

    def cancel(self, job_id: str, capability: str) -> ComparisonJob:
        """Ставит флаг отмены; worker завершает дочерние процессы через checkpoint."""
        job = self.owned_job(job_id, capability)
        if job.status not in TERMINAL:
            self.repository.update(
                job_id,
                status=JobStatus.CANCELLED,
                stage="Отменено",
                expires=time.time() + self.limits.retention,
            )
        return self.owned_job(job_id, capability)

    def delete(self, job_id: str, capability: str) -> None:
        """Закрывает доступ немедленно; очистка активного каталога выполняется после остановки worker."""
        job = self.owned_job(job_id, capability)
        self.repository.update(
            job_id, status=JobStatus.EXPIRED, stage="Удалено", expires=time.time()
        )
        if job.status != JobStatus.PROCESSING:
            self.storage.delete(job_id)

    @timed("comparison_process")
    def process(self, job_id: str) -> None:
        """Сохраняет алгоритмический checkpoint перед ИИ и публикует только при действующей аренде."""
        lease = uuid4().hex
        if not self.repository.claim(job_id, lease):
            return
        job = self.repository.get(job_id)
        directory = self.storage.directory(job_id)
        started, last_stage, last_write = time.monotonic(), "", 0.0
        result = None
        enforce_timeout = True

        def checkpoint(stage: str) -> None:
            """Проверяет отмену и аренду между дорогими операциями и сохраняет текущую стадию."""
            nonlocal last_stage, last_write
            current = self.repository.get(job_id)
            if current is None or current.status != JobStatus.PROCESSING or current.lease != lease:
                raise Cancelled()
            if enforce_timeout and time.monotonic() - started > self.limits.timeout:
                raise DocumentError("Превышено общее время сравнения")
            if stage != last_stage or time.monotonic() - last_write > 10:
                self.repository.update(job_id, stage=stage)
                last_stage, last_write = stage, time.monotonic()

        def persist(current) -> None:
            """Проверяет аренду до каждого атомарного checkpoint и сохраняет только безопасные счётчики в SQLite."""
            checkpoint(last_stage or "ИИ-проверка")
            self.storage.write_result(job_id, current)
            self.repository.update(job_id, ai_progress=public_progress(current.ai_review))

        try:
            if job.resume_ai and self.storage.has_result(job_id):
                result = self.storage.restore_result(job_id)
                result.warnings = [w for w in result.warnings if not w.startswith("ИИ-проверка")]
            else:
                old_path = next(
                    p for p in directory.glob("old.*") if p.suffix in {".doc", ".docx", ".pdf"}
                )
                new_path = next(
                    p for p in directory.glob("new.*") if p.suffix in {".doc", ".docx", ".pdf"}
                )
                visual_only = self.extractor.is_signed(old_path) or self.extractor.is_signed(
                    new_path
                )
                old = self.extractor.extract(
                    old_path, job.old_name, directory / "old", visual_only, checkpoint
                )
                new = self.extractor.extract(
                    new_path, job.new_name, directory / "new", visual_only, checkpoint
                )
                checkpoint("Структурное сопоставление и сравнение")
                scores = self.visual_comparator.measure(old, new) if self.visual_comparator else {}
                result = compare(old, new, scores)
                checkpoint("Сохранение базового результата и экспорта")
                result.exports = self.exporter.export(result, directory, checkpoint)
                self.storage.write_result(job_id, result)
            if job.use_ai:
                result.ai_status = "running"
                result.exports_current = False
                checkpoint("Планирование ИИ-проверки")
                try:
                    self.verifier.verify(
                        result,
                        checkpoint,
                        modes=AIModes(bool(job.ai_full), bool(job.ai_unverified)),
                        job_id=job_id,
                        persist=persist,
                    )
                except Cancelled:
                    raise
                except Exception as exc:
                    result.ai_status = (
                        "partial"
                        if any(u.status == "completed" for u in result.ai_review.units)
                        else "failed"
                    )
                    result.warnings.append(
                        "ИИ-проверка не завершена; алгоритмический результат сохранён"
                    )
                    for mode in ("full", "unverified"):
                        progress = getattr(result.ai_review, mode)
                        if getattr(job, "ai_" + mode) and progress.status not in {
                            "completed",
                            "partial",
                            "failed",
                        }:
                            progress.status = "failed"
                            progress.error_counts[type(exc).__name__] = 1
                    logging.getLogger(__name__).error(
                        "ai_pipeline_failed",
                        extra={
                            "job_id": job_id,
                            "exception_class": type(exc).__name__,
                            "error_kind": "job_time_budget"
                            if isinstance(exc, DocumentError)
                            else "internal",
                            "status": result.ai_status,
                        },
                    )
                enforce_timeout = False
                checkpoint("Экспорт с результатами ИИ")
                result.exports = self.exporter.export(result, directory, checkpoint)
                result.exports_current = True
            checkpoint("Сохранение результата")
            persist(result)
            warning = bool(
                result.warnings
                or result.ai_status in {"failed", "partial"}
                or any(
                    d.active
                    and (
                        d.status in {"unverified", "needs_review"}
                        or d.category in {"ambiguous_match", "unverified_region"}
                    )
                    for d in result.differences
                )
            )
            self.repository.finish(
                job_id,
                lease,
                JobStatus.WARNINGS if warning else JobStatus.COMPLETED,
                time.time() + self.limits.retention,
            )
        except Cancelled:
            current = self.repository.get(job_id)
            # Не удаляем файлы нового владельца аренды или сохранённый базовый результат.
            if (
                current is None
                or current.status == JobStatus.EXPIRED
                or not self.storage.has_result(job_id)
            ):
                self.storage.delete(job_id)
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, DocumentError)
                else "Ошибка обработки; проверьте формат документа и доступность инструментов"
            )
            if self.storage.has_result(job_id):
                self.repository.finish(
                    job_id,
                    lease,
                    JobStatus.WARNINGS,
                    time.time() + self.limits.retention,
                    "Обработка завершена частично; базовый результат сохранён",
                )
            else:
                self.storage.delete(job_id)
                self.repository.finish(
                    job_id, lease, JobStatus.FAILED, time.time() + self.limits.retention, message
                )
            logging.getLogger(__name__).error(
                "comparison_failed",
                extra={
                    "job_id": job_id,
                    "status": "failed",
                    "exception_class": type(exc).__name__,
                    "error_kind": "processing",
                },
            )

    def cleanup(self) -> None:
        """Закрывает доступ по TTL и завершает зависшие задания после перезапуска."""
        now = time.time()
        self.storage.cleanup_transients(now - self.limits.retention)
        for job in self.repository.due(now, now - self.limits.timeout):
            if job.status in {JobStatus.QUEUED, JobStatus.PROCESSING}:
                if self.storage.has_result(job.id):
                    self.repository.update(
                        job.id,
                        status=JobStatus.WARNINGS,
                        stage="ИИ-проверка прервана; можно повторить",
                        expires=now + self.limits.retention,
                        error="Обработчик прерван; базовый результат сохранён",
                    )
                    continue
                self.repository.update(
                    job.id,
                    status=JobStatus.FAILED,
                    stage="Прервано по времени",
                    expires=now + self.limits.retention,
                    error="Обработчик прерван или превышен срок выполнения",
                )
            else:
                self.repository.update(
                    job.id, status=JobStatus.EXPIRED, stage="Срок хранения истёк", expires=now
                )
            self.storage.delete(job.id)
