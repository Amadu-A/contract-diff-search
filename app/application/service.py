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
    Cancelled,
    ComparisonJob,
    Difference,
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
        use_ai: bool,
    ) -> ComparisonJob:
        """Резервирует квоту, сохраняет документы и публикует долговечную задачу."""
        now, job_id = time.time(), str(uuid4())
        job = ComparisonJob(
            job_id,
            owner_hash(capability),
            JobStatus.QUEUED,
            "В очереди",
            now,
            now,
            now + self.limits.timeout + self.limits.retention,
            use_ai,
            Path(old_name.replace("\\", "/")).name[:240],
            Path(new_name.replace("\\", "/")).name[:240],
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
        return job

    def result(self, job_id: str, capability: str) -> dict:
        """Выдаёт результат только завершённого задания его владельцу."""
        job = self.owned_job(job_id, capability)
        if job.status not in {JobStatus.COMPLETED, JobStatus.WARNINGS}:
            raise DocumentError("Результат ещё не готов")
        return self.storage.read_result(job_id)

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
        """Выполняет ограниченный фоновый конвейер с идемпотентным захватом задания."""
        lease = uuid4().hex
        if not self.repository.claim(job_id, lease):
            return
        job = self.repository.get(job_id)
        directory = self.storage.directory(job_id)
        started, last_stage, last_write = time.monotonic(), "", 0.0

        def checkpoint(stage: str) -> None:
            """Проверяет отмену, общий таймаут и сохраняет только настоящий этап работы."""
            nonlocal last_stage, last_write
            current = self.repository.get(job_id)
            if current is None or current.status != JobStatus.PROCESSING or current.lease != lease:
                raise Cancelled()
            if time.monotonic() - started > self.limits.timeout:
                raise DocumentError("Превышено общее время сравнения")
            if stage != last_stage or time.monotonic() - last_write > 10:
                self.repository.update(job_id, stage=stage)
                last_stage, last_write = stage, time.monotonic()

        try:
            old_path = next(
                p for p in directory.glob("old.*") if p.suffix in {".doc", ".docx", ".pdf"}
            )
            new_path = next(
                p for p in directory.glob("new.*") if p.suffix in {".doc", ".docx", ".pdf"}
            )
            visual_only = self.extractor.is_signed(old_path) or self.extractor.is_signed(new_path)
            old = self.extractor.extract(
                old_path, job.old_name, directory / "old", visual_only, checkpoint
            )
            new = self.extractor.extract(
                new_path, job.new_name, directory / "new", visual_only, checkpoint
            )
            checkpoint("Структурное сопоставление и сравнение")
            scores = self.visual_comparator.measure(old, new) if self.visual_comparator else {}
            result = compare(old, new, scores)
            checkpoint("Проверка полноты")
            if job.use_ai:
                result.ai_status = "running"
                checkpoint("Дополнительная ИИ-проверка")
                try:
                    findings = self.verifier.verify(result, checkpoint)
                    oa, nb = {e.id: e for e in old.elements}, {e.id: e for e in new.elements}
                    for finding in findings:
                        a, b = oa[finding.old_element], nb[finding.new_element]
                        from app.domain.comparison import regions

                        result.differences.append(
                            Difference(
                                f"AI{len(result.differences):05d}",
                                finding.category,
                                "Обнаружено ИИ, требует проверки",
                                a.text,
                                b.text,
                                a.section,
                                b.section,
                                regions(a),
                                regions(b),
                                a.id,
                                b.id,
                                status="needs_review",
                                source="ai",
                                reason=finding.reason,
                            )
                        )
                    result.ai_status = "completed"
                except Cancelled:
                    raise
                except Exception:
                    result.ai_status = "failed"
                    result.warnings.append(
                        "ИИ-проверка не выполнена; детерминированный результат сохранён"
                    )
            checkpoint("Подготовка экспорта")
            result.exports = self.exporter.export(result, directory, checkpoint)
            checkpoint("Сохранение результата")
            self.storage.write_result(job_id, result)
            warning = bool(
                result.warnings
                or any(
                    d.status == "unverified"
                    or d.category in {"ambiguous_match", "unverified_region"}
                    for d in result.differences
                )
            )
            published = self.repository.finish(
                job_id,
                lease,
                JobStatus.WARNINGS if warning else JobStatus.COMPLETED,
                time.time() + self.limits.retention,
            )
            if not published:
                self.storage.delete(job_id)
        except Cancelled:
            # После потери аренды ни статус, ни результат не публикуются.
            self.storage.delete(job_id)
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, DocumentError)
                else "Ошибка обработки; проверьте формат документа и доступность инструментов"
            )
            self.storage.delete(job_id)
            self.repository.finish(
                job_id, lease, JobStatus.FAILED, time.time() + self.limits.retention, message
            )
            logging.getLogger(__name__).error(
                "comparison_failed", extra={"job_id": job_id, "status": "failed"}
            )

    def cleanup(self) -> None:
        """Закрывает доступ по TTL и завершает зависшие задания после перезапуска."""
        now = time.time()
        self.storage.cleanup_transients(now - self.limits.retention)
        for job in self.repository.due(now, now - self.limits.timeout):
            if job.status in {JobStatus.QUEUED, JobStatus.PROCESSING}:
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
