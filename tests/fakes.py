# tests/fakes.py
"""Подменяемые порты для проверки сценариев без HTTP, RabbitMQ и VLM."""

from pathlib import Path

from app.domain.models import DocumentVersion, ExportArtifact


class RecordingPublisher:
    """Запоминает UUID доставки; не выполняет задачу внутри HTTP-запроса."""

    def __init__(self):
        """Создаёт независимый список доставок для каждого теста."""
        self.ids = []

    def publish(self, job_id: str) -> None:
        """Регистрирует публикацию для последующего явного вызова worker-use-case."""
        self.ids.append(job_id)


class FakeExtractor:
    """Возвращает заданные модели, сохраняя контракт отмены через checkpoint."""

    def __init__(self, old: DocumentVersion, new: DocumentVersion):
        """Принимает эталонные модели обеих сторон без чтения файлов."""
        self.old, self.new, self.calls = old, new, 0

    def is_signed(self, path: Path) -> bool:
        """Синтетические неподписанные модели не запускают особый режим."""
        return False

    def extract(self, path, name, output, visual_only, checkpoint):
        """Вызывает реальную проверку отмены и выбирает модель по стороне."""
        checkpoint("Тестовое извлечение")
        self.calls += 1
        return self.old if path.stem == "old" else self.new


class FakeExporter:
    """Сохраняет контракт артефактов без имитации готового PDF-файла."""

    def export(self, result, output, checkpoint, *, include_word=True):
        """Сообщает недоступность, вместо выдачи пустого фиктивного экспорта."""
        checkpoint("Тестовая подготовка экспорта")
        return {
            "old": [ExportArtifact("pdf", None, "Тестовая подмена экспорта")],
            "new": [ExportArtifact("pdf", None, "Тестовая подмена экспорта")],
        }


class FailingVerifier:
    """Воспроизводит недоступность внешней VLM без сетевых запросов."""

    def verify(self, result, checkpoint, **kwargs):
        """Имитирует отказ необязательной зависимости после основного сравнения."""
        raise TimeoutError("VLM unavailable")
