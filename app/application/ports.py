# app/application/ports.py
"""Порты прикладного слоя: время, хранилище, очередь, извлечение и проверка."""

from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Protocol

from app.domain.models import (
    AIModes,
    AIReply,
    ComparisonJob,
    ComparisonResult,
    DocumentVersion,
    ExportArtifact,
    ReviewUnit,
)


class JobRepository(Protocol):
    """Атомарные переходы состояния и временная аренда задания одним worker."""

    def create(
        self, job: ComparisonJob, max_jobs: int, max_session_jobs: int
    ) -> ComparisonJob | None:
        """Создаёт задание, проверяя общую и сессионную квоты одной транзакцией."""
        ...

    def rate_limit(self, scope: str, maximum: int, now: float) -> bool:
        """Ограничивает частоту запросов клиента атомарным временным счётчиком."""
        ...

    def create_access(
        self, job_id: str, owner: str, digest: str, expires: float, now: float
    ) -> float:
        """Сохраняет защищённый одноразовый билет в пределах TTL завершённого результата."""
        ...

    def activate_access(self, digest: str, session_digest: str, now: float) -> tuple[str, float]:
        """Однократно обменивает билет на отдельное право чтения одного задания."""
        ...

    def can_view(self, job_id: str, digest: str, now: float) -> bool:
        """Проверяет ограниченную браузерную сессию без выдачи права изменения."""
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

    def requeue_ai(self, job_id: str, expires: float, max_session_jobs: int = 2) -> bool:
        """Возвращает завершённый ИИ-проход в очередь без повторного извлечения документов."""
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

    def has_result(self, job_id: str) -> bool:
        """Проверяет наличие checkpoint базового сравнения."""
        ...

    def restore_result(self, job_id: str) -> ComparisonResult:
        """Восстанавливает типизированный результат из доверенного временного хранилища."""
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

    def report(self, projection: dict) -> bytes:
        """Формирует табличный PDF исключительно по проекции сохранённого результата."""
        ...

    def export(
        self,
        result: ComparisonResult,
        output: Path,
        checkpoint: Callable[[str], None],
        *,
        include_word: bool = True,
    ) -> dict[str, list[ExportArtifact]]:
        """Сохраняет экспорты, сообщая причину недоступности каждого Word-формата."""
        ...


class Verifier(Protocol):
    """Дополнительная проверка с сохранением подтверждённых алгоритмических фактов."""

    def verify(
        self,
        result: ComparisonResult,
        checkpoint: Callable[[str], None],
        *,
        modes: AIModes,
        job_id: str,
        persist: Callable[[ComparisonResult], None],
    ) -> None:
        """Сохраняет план, прогресс и независимо проверенные результаты двух режимов."""
        ...


class ModelReviewer(Protocol):
    """Одна операция корпоративной VLM без оркестрации всего сравнения."""

    def review(
        self, unit: ReviewUnit, old: DocumentVersion, new: DocumentVersion, attempt: int = 0
    ) -> AIReply:
        """Возвращает проверяемую структуру либо безопасную классифицированную ошибку."""
        ...


class GeometryRefiner(Protocol):
    """Дополнительная точная геометрия из уже подготовленных исходных визуальных доказательств."""

    def refine(self, result: ComparisonResult, checkpoint: Callable[[str], None]) -> None:
        """Уточняет известные координаты без повторного OCR, сравнения или выдуманных областей."""
        ...


class VisualComparator(Protocol):
    """Измерения локальных растров без зависимости домена от PIL/NumPy."""

    def measure(self, old: DocumentVersion, new: DocumentVersion) -> dict[tuple[str, str], float]:
        """Возвращает SSIM кандидатов, выбранных по pHash и геометрии."""
        ...
