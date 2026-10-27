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
    active: bool = True
    uncertainty_id: str = ""
    ai_mode: str = ""
    verified_by: str = ""
    confidence: float = 0.0


@dataclass
class CoverageReport:
    """Классификация каждого значимого узла; непроверенные узлы не исчезают."""

    old: dict[str, str]
    new: dict[str, str]
    pages: dict[str, list[int]]
    geometry_missing: int = 0
    original_unverified: int = 0
    resolved_algorithms: int = 0
    resolved_ai: int = 0
    new_unverified: int = 0


@dataclass
class VerificationFinding:
    """Неподтверждённое замечание ИИ с привязкой к уже известным элементам."""

    category: str
    old_element: str
    new_element: str
    reason: str


@dataclass(frozen=True)
class AIModes:
    """Независимые режимы; оба выключенных оставляют только алгоритмический конвейер."""

    full: bool = False
    unverified: bool = False


@dataclass
class AIObservation:
    """Ответ модели с цитатами и ссылками только на предложенные источником области."""

    category: str
    old_value: str = ""
    new_value: str = ""
    old_ref: str = ""
    new_ref: str = ""
    reason: str = ""
    confidence: float = 0.0
    old_quote: str = ""
    new_quote: str = ""


@dataclass
class AIReply:
    """Структурированный ответ не содержит выдуманных моделью координат."""

    outcome: str
    readable: bool
    findings: list[AIObservation] = field(default_factory=list)
    evidence: list[AIObservation] = field(default_factory=list)
    complete: bool = True
    old_evidence: list[Word] = field(default_factory=list)
    new_evidence: list[Word] = field(default_factory=list)


@dataclass
class ReviewUnit:
    """Одна воспроизводимая единица плана и её сохранённое состояние выполнения."""

    id: str
    mode: str
    kind: str
    old_elements: list[str] = field(default_factory=list)
    new_elements: list[str] = field(default_factory=list)
    old_regions: list[VisualRegion] = field(default_factory=list)
    new_regions: list[VisualRegion] = field(default_factory=list)
    old_text: str = ""
    new_text: str = ""
    targets: list[str] = field(default_factory=list)
    status: str = "pending"
    outcome: str = "unverified"
    attempts: int = 0
    error_kind: str = ""
    duration_ms: float = 0
    prompt_version: str = "1"
    segment: int = 0
    job_id: str = ""
    disabled_features: list[str] = field(default_factory=list)
    observations: list[AIObservation] = field(default_factory=list)
    readable: bool | None = None
    old_evidence: list[Word] = field(default_factory=list)
    new_evidence: list[Word] = field(default_factory=list)


@dataclass
class AIModeProgress:
    """Фактические счётчики режима; выполненное не приравнивается к подтверждённому."""

    status: str = "not_requested"
    planned: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    remaining: int = 0
    reused: int = 0
    pages_planned: dict[str, list[int]] = field(default_factory=dict)
    pages_processed: dict[str, list[int]] = field(default_factory=dict)
    error_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class AIReviewState:
    """План, результаты отдельных единиц и независимый прогресс двух режимов."""

    full: AIModeProgress = field(default_factory=AIModeProgress)
    unverified: AIModeProgress = field(default_factory=AIModeProgress)
    units: list[ReviewUnit] = field(default_factory=list)
    original_uncertainties: list[str] = field(default_factory=list)
    resolutions: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0


@dataclass
class ExportArtifact:
    """Доступность отдельного проверенного экспорта без изменения оригинала."""

    format: str
    path: str | None
    reason: str = ""
    show_unverified: bool = True


@dataclass
class ComparisonResult:
    """Результат сравнения, покрытие и независимый статус ИИ."""

    old: DocumentVersion
    new: DocumentVersion
    differences: list[Difference]
    matches: list[ElementMatch]
    coverage: CoverageReport
    ai_status: str = "not_requested"
    warnings: list[str] = field(default_factory=list)
    exports: dict[str, list[ExportArtifact]] = field(default_factory=dict)
    ai_review: AIReviewState = field(default_factory=AIReviewState)
    exports_current: bool = True


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
    ai_full: bool = False
    ai_unverified: bool = False
    ai_progress: dict = field(default_factory=dict)
    resume_ai: bool = False


class VerificationError(Exception):
    """Безопасная классифицированная ошибка VLM без содержимого запроса и ответа."""

    def __init__(self, kind: str, *, http_status: int | None = None, retryable: bool = False):
        """Сохраняет только техническую категорию и возможность ограниченного повтора."""
        super().__init__(kind)
        self.kind, self.http_status, self.retryable = kind, http_status, retryable


class DocumentError(Exception):
    """Ожидаемая ошибка документа, которую безопасно показать пользователю."""


class AccessDenied(Exception):
    """Задание отсутствует либо не принадлежит текущей браузерной сессии."""


class Cancelled(Exception):
    """Кооперативная остановка конвейера после отмены либо потери аренды."""
