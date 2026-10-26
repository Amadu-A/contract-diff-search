# app/domain/models.py
"""Единая модель текста, структуры и визуальных доказательств обеих редакций.

Координаты задаются в пунктах от верхнего левого угла видимой страницы.
Отсутствующая сторона изменения не получает вымышленных координат.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class JobStatus(StrEnum):
    """Состояния задания с отдельным завершением при неполном покрытии."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.WARNINGS,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.EXPIRED,
    }
)


@dataclass(frozen=True)
class VisualRegion:
    """Область на видимой странице, уже учитывающая CropBox и поворот."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class SourceAnchor:
    """Связь логического элемента с OOXML либо распознанным PDF-фрагментом."""

    source: str
    path: str
    start: int = 0
    end: int = 0


@dataclass(frozen=True)
class TextRun:
    """Текстовый диапазон и нормализованные признаки значимого оформления."""

    start: int
    end: int
    properties: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Word:
    """Слово с геометрией и достоверностью; диапазон относится к тексту блока."""

    text: str
    start: int
    end: int
    region: VisualRegion
    confidence: float = 1.0
    verified: bool = True
    characters: tuple[VisualRegion, ...] = ()


@dataclass
class DocumentElement:
    """Узел структуры: раздел, абзац, список, строка или ячейка таблицы."""

    id: str
    kind: str
    text: str
    section: str = ""
    path: str = ""
    parent: str | None = None
    words: list[Word] = field(default_factory=list)
    runs: list[TextRun] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    confidence: float = 1.0


@dataclass
class GraphicElement:
    """Локализованный графический объект с отпечатком и проверяемым растром."""

    id: str
    region: VisualRegion
    digest: str
    perceptual_hash: str
    image: str
    description: str = "Графический элемент"
    confidence: float = 1.0


@dataclass
class Page:
    """Визуальная страница и фактический способ получения её текста."""

    number: int
    width: float
    height: float
    mode: str
    confidence: float
    warnings: list[str] = field(default_factory=list)


@dataclass
class DocumentVersion:
    """Исходная редакция, связанная с логической структурой и PDF-рендером."""

    name: str
    format: str
    pdf: str
    elements: list[DocumentElement]
    pages: list[Page]
    graphics: list[GraphicElement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    signed: bool = False
    visual_only: bool = False


@dataclass
class ElementMatch:
    """Соответствие одного или нескольких узлов с причиной и уверенностью."""

    old: list[str]
    new: list[str]
    method: str
    confidence: float
    moved: bool = False


@dataclass
class Difference:
    """Уникальное изменение либо явно непроверенный участок с доказательствами."""

    id: str
    category: str
    title: str
    old_text: str = ""
    new_text: str = ""
    old_section: str = ""
    new_section: str = ""
    old_regions: list[VisualRegion] = field(default_factory=list)
    new_regions: list[VisualRegion] = field(default_factory=list)
    old_element: str | None = None
    new_element: str | None = None
    old_range: tuple[int, int] | None = None
    new_range: tuple[int, int] | None = None
    status: str = "confirmed"
    source: str = "deterministic"
    reason: str = ""
    subcategories: list[str] = field(default_factory=list)


@dataclass
class CoverageReport:
    """Классификация каждого значимого узла; непроверенные узлы не исчезают."""

    old: dict[str, str]
    new: dict[str, str]
    pages: dict[str, list[int]]
    geometry_missing: int = 0


@dataclass
class VerificationFinding:
    """Неподтверждённое замечание ИИ с привязкой к уже известным элементам."""

    category: str
    old_element: str
    new_element: str
    reason: str


@dataclass
class ExportArtifact:
    """Доступность отдельного проверенного экспорта без изменения оригинала."""

    format: str
    path: str | None
    reason: str = ""


@dataclass
class ComparisonResult:
    """Детерминированный результат, покрытие и независимый статус ИИ."""

    old: DocumentVersion
    new: DocumentVersion
    differences: list[Difference]
    matches: list[ElementMatch]
    coverage: CoverageReport
    ai_status: str = "not_requested"
    warnings: list[str] = field(default_factory=list)
    exports: dict[str, list[ExportArtifact]] = field(default_factory=dict)


@dataclass
class ComparisonJob:
    """Временное задание; владелец хранится только как хеш capability-cookie."""

    id: str
    owner: str
    status: str
    stage: str
    created: float
    updated: float
    expires: float
    use_ai: bool
    old_name: str
    new_name: str
    error: str = ""
    lease: str = ""


class DocumentError(Exception):
    """Ожидаемая ошибка документа, которую безопасно показать пользователю."""


class AccessDenied(Exception):
    """Задание отсутствует либо не принадлежит текущей браузерной сессии."""


class Cancelled(Exception):
    """Кооперативная остановка конвейера после отмены либо потери аренды."""
