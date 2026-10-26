# app/application/ports.py
"""Порты прикладного слоя: время, хранилище, очередь, извлечение и проверка."""

from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Protocol

from app.domain.models import (
    ComparisonJob,
    ComparisonResult,
    DocumentVersion,
    ExportArtifact,
    VerificationFinding,
)


class JobRepository(Protocol):
    """Атомарные переходы состояния и временная аренда задания одним worker."""

    def create(self, job: ComparisonJob, max_jobs: int, max_session_jobs: int) -> None:
        """Создаёт задание, проверяя общую и сессионную квоты одной транзакцией."""
        ...

    def get(self, job_id: str) -> ComparisonJob | None:
        """Возвращает техническую запись без проверки браузерного владельца."""
        ...

    def claim(self, job_id: str, lease: str) -> bool:
        """Атомарно переводит только queued-задание в processing."""
        ...

    def update(self, job_id: str, **changes) -> None:
        """Обновляет разрешённые поля технической записи."""
        ...

    def finish(self, job_id: str, lease: str, status: str, expires: float, error: str = "") -> bool:
        """Завершает задание только при действующей аренде и отсутствии отмены."""
        ...

    def due(self, now: float, stale_before: float) -> list[ComparisonJob]:
        """Возвращает просроченные либо зависшие задания для очистки."""
        ...


class JobStorage(Protocol):
    """Изолированные файлы задания; имена пользователя не используются как пути."""

    def save_upload(self, job_id: str, side: str, name: str, stream: BinaryIO) -> str:
        """Потоково сохраняет и валидирует формат, отклоняя превышение лимитов."""
        ...

    def directory(self, job_id: str) -> Path:
        """Возвращает проверенный каталог с UUID-именем."""
        ...

    def delete(self, job_id: str) -> None:
        """Удаляет исключительно каталог данного задания."""
        ...

    def write_result(self, job_id: str, result: ComparisonResult) -> None:
        """Атомарно сохраняет результат вне SQLite и журналов."""
        ...

    def read_result(self, job_id: str) -> dict:
        """Читает сохранённый результат для выдачи владельцу."""
        ...

    def cleanup_transients(self, older_than: float) -> None:
        """Удаляет оставшиеся после аварии профили конвертера с истёкшим сроком."""
        ...


class TaskPublisher(Protocol):
    """Долговечная доставка идентификатора; документы через брокер не передаются."""

    def publish(self, job_id: str) -> None:
        """Публикует одну идемпотентную задачу либо сообщает об отказе брокера."""
        ...


class DocumentExtractor(Protocol):
    """Извлечение итоговой редакции и геометрии через ограниченные адаптеры."""

    def is_signed(self, path: Path) -> bool:
        """Проверяет объекты криптографической подписи без валидации сертификата."""
        ...

    def extract(
        self,
        path: Path,
        name: str,
        output: Path,
        visual_only: bool,
        checkpoint: Callable[[str], None],
    ) -> DocumentVersion:
        """Извлекает документ; checkpoint проверяет отмену между затратными шагами."""
        ...


class Exporter(Protocol):
    """Формирует отдельные проверенные PDF и безопасные Word-копии."""

    def export(
        self, result: ComparisonResult, output: Path, checkpoint: Callable[[str], None]
    ) -> dict[str, list[ExportArtifact]]:
        """Сохраняет экспорты, сообщая причину недоступности каждого Word-формата."""
        ...


class Verifier(Protocol):
    """Дополнительная ограниченная проверка; основной результат не изменяется."""

    def verify(
        self, result: ComparisonResult, checkpoint: Callable[[str], None]
    ) -> list[VerificationFinding]:
        """Возвращает только структурно проверенные и локализованные замечания."""
        ...


class VisualComparator(Protocol):
    """Измерения локальных растров без зависимости домена от PIL/NumPy."""

    def measure(self, old: DocumentVersion, new: DocumentVersion) -> dict[tuple[str, str], float]:
        """Возвращает SSIM кандидатов, выбранных по pHash и геометрии."""
        ...
