# app/infrastructure/pdf.py
"""PDFium-рендер, безопасная видимая геометрия и постраничная классификация PDF.

pdfplumber даёт нативные слова и символы. Для растровых и непокрытых
областей используется OCR. Подпись определяется объектами PDF, а не печатями.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from threading import RLock

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import NameObject

from app.domain.algorithms import lcs
from app.domain.comparison import resolve_ocr
from app.domain.models import (
    DocumentElement,
    DocumentError,
    DocumentVersion,
    Page,
    TextRun,
    VisualRegion,
    Word,
)
from app.domain.text import SECTION, canonicalize, normalize, ocr_equivalent, tokens
from app.domain.visibility import is_unverified
from app.infrastructure.graphics import extract_blue_graphics, extract_unknown_graphics, fingerprint
from app.infrastructure.ocr import TesseractOCR
from app.infrastructure.word import LibreOffice, prepare_docx, read_docx
from app.observability import timed
from app.settings import Settings

PDFIUM_LOCK = RLock()


def has_signature(path: Path) -> bool:
    """Проверяет реальные словари /Sig и поля /FT /Sig; сертификат не валидирует."""
    if path.suffix.lower() != ".pdf":
        return False
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise DocumentError("PDF с парольной защитой не поддерживается")
    seen = set()

    def visit(value, depth: int = 0) -> bool:
        """Обходит ограниченный граф AcroForm/Annots без поиска строк в потоке скана."""
        if depth > 40:
            return False
        if hasattr(value, "idnum"):
            if value.idnum in seen:
                return False
            seen.add(value.idnum)
            value = value.get_object()
        if isinstance(value, dict):
            if value.get("/Type") == "/Sig" and "/ByteRange" in value and "/Contents" in value:
                return True
            return any(
                visit(v, depth + 1)
                for k, v in value.items()
                if k in {"/AcroForm", "/Fields", "/Kids", "/V", "/Annots", "/Parent"}
            )
        if isinstance(value, (list, tuple)):
            return any(visit(v, depth + 1) for v in value)
        return False

    return visit(reader.trailer["/Root"]) or any(visit(page) for page in reader.pages)


def canonical_pdf(source: Path, target: Path, max_pages: int) -> None:
    """Создаёт отдельный просмотр без активных аннотаций, с CropBox в начале координат."""
    reader, writer = PdfReader(source), PdfWriter()
    if reader.is_encrypted:
        raise DocumentError("PDF с парольной защитой не поддерживается")
    if not 0 < len(reader.pages) <= max_pages:
        raise DocumentError("Превышено допустимое число страниц либо документ пуст")
    for page in reader.pages:
        if page.rotation:
            page.transfer_rotation_to_content()
        x, y = float(page.cropbox.left), float(page.cropbox.bottom)
        width, height = float(page.cropbox.width), float(page.cropbox.height)
        if not (10 <= width <= 5000 and 10 <= height <= 5000):
            raise DocumentError("Недопустимый размер страницы")
        page.pop(NameObject("/Annots"), None)
        page.pop(NameObject("/AA"), None)
        blank = writer.add_blank_page(width, height)
        blank.merge_transformed_page(page, Transformation().translate(-x, -y))
    with target.open("wb") as output:
        writer.write(output)


def render_page(path: Path, page: int, scale: float = 1.5, max_pixels: int = 24000000):
    """Рендерит одну страницу с лимитом пикселей; вызывающий закрывает PIL-копию."""
    with PDFIUM_LOCK:
        return _render_locked(path, page, scale, max_pixels)


def _render_locked(path: Path, page: int, scale: float, max_pixels: int):
    """Сериализует вызовы PDFium, который не допускает одновременных потоковых обращений."""
    with pdfium.PdfDocument(path) as doc:
        if page < 1 or page > len(doc):
            raise DocumentError("Страница не найдена")
        pdf_page = doc[page - 1]
        try:
            width, height = pdf_page.get_size()
            scale = min(scale, (max_pixels / (width * height)) ** 0.5)
            bitmap = pdf_page.render(scale=scale)
            image = bitmap.to_pil().copy()
            bitmap.close()
            return image
        finally:
            pdf_page.close()


def _native_words(page) -> list[dict]:
    """Извлекает слова с нативной геометрией и выделяет абзацы по отступам строк."""
    extracted = page.extract_words(
        x_tolerance=2, y_tolerance=3, keep_blank_chars=False, use_text_flow=False, return_chars=True
    )
    # Транспортная подпись ЭДО не является текстом договора или доказательством /Sig.
    raw_lines = defaultdict(list)
    line_top, line_number = -100.0, -1
    for item in sorted(extracted, key=lambda w: (w["top"], w["x0"])):
        if item["top"] - line_top > 3:
            line_number += 1
            line_top = item["top"]
        item["native_line"] = line_number
        raw_lines[line_number].append(item)
    metadata_top = page.height
    for items in raw_lines.values():
        text = " ".join(w["text"] for w in sorted(items, key=lambda w: w["x0"]))
        if (
            items[0]["top"] > page.height * 0.55
            and re.search(r"Идентификатор документа\s+[0-9a-f-]{36}", text)
            and "СКБ Контур" in " ".join(w["text"] for w in extracted)
        ):
            metadata_top = min(metadata_top, items[0]["top"])
    original_words = extracted
    extracted = [
        w
        for items in raw_lines.values()
        if items[0]["top"] < metadata_top
        and not (
            items[0]["top"] > page.height * 0.94
            and re.search(
                r"Передан через Диадок|Страница\s+\d+\s+из\s+\d+|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}",
                " ".join(w["text"] for w in items),
            )
        )
        for w in items
    ]
    technical = {id(w) for w in original_words} - {id(w) for w in extracted}
    extracted = original_words
    result, last_top, paragraph = [], -100.0, 0
    # Раздельный порядок чтения колонок при устойчивом сквозном центральном пробеле.
    occupancy = np.zeros(max(1, int(page.width) + 1), dtype=int)
    for item in extracted:
        occupancy[max(0, int(item["x0"])) : min(len(occupancy), int(item["x1"]) + 1)] += 1
    middle = occupancy[int(page.width * 0.35) : int(page.width * 0.65)] == 0
    runs = []
    begin = None
    for position, empty in enumerate([*middle, False]):
        if empty and begin is None:
            begin = position
        elif not empty and begin is not None:
            runs.append((begin, position))
            begin = None
    gutter = max(runs, key=lambda pair: pair[1] - pair[0], default=None)
    split = (
        int(page.width * 0.35) + sum(gutter) / 2 if gutter and gutter[1] - gutter[0] > 18 else None
    )
    if split and (
        sum(w["x0"] < split for w in extracted) < 20
        or sum(w["x0"] >= split for w in extracted) < 20
    ):
        split = None
    column_top = -1
    # Реквизиты могут занимать две колонки только в нижней части обычной страницы.
    for items in raw_lines.values():
        labels = [
            w
            for w in items
            if re.fullmatch(r"(?:Заказчик|Подрядчик|Покупатель|Поставщик):", w["text"])
        ]
        if len(labels) == 2 and abs(labels[0]["x0"] - labels[1]["x0"]) > page.width * 0.25:
            labels.sort(key=lambda w: w["x0"])
            column_top = min(w["top"] for w in labels)
            split = labels[1]["x0"] - 8
            starts = Counter()
            for row in raw_lines.values():
                row = sorted(row, key=lambda w: w["x0"])
                if row[0]["top"] < column_top or row[0]["top"] >= metadata_top:
                    continue
                for left, right in zip(row, row[1:], strict=False):
                    if (
                        right["x0"] - left["x1"] > 20
                        and page.width * 0.4 < right["x0"] < page.width * 0.8
                    ):
                        starts[round(right["x0"] / 3) * 3] += 1
            if starts and starts.most_common(1)[0][1] >= 3:
                split = starts.most_common(1)[0][0] - 2
            break

    line_splits = {}
    if column_top >= 0:
        for number, row in raw_lines.items():
            row = sorted(row, key=lambda w: w["x0"])
            if row[0]["top"] < column_top:
                continue
            gaps = [
                (right["x0"] - left["x1"], (right["x0"] + left["x1"]) / 2)
                for left, right in zip(row, row[1:], strict=False)
                if page.width * 0.35 < right["x0"] < page.width * 0.75
            ]
            if gaps and max(gaps)[0] > 25:
                line_splits[number] = max(gaps)[1]

    def column(word):
        """Отделяет полноширинный текст над локальными колонками реквизитов."""
        if column_top >= 0 and word["top"] < column_top - 2:
            return -1
        boundary = line_splits.get(word["native_line"], split)
        return int(word["x0"] >= boundary) if boundary else 0

    ordered = sorted(
        extracted,
        key=lambda w: (column(w), w["native_line"], w["x0"]),
    )
    margins = {}
    for word in ordered:
        margins[column(word)] = min(margins.get(column(word), page.width), word["x0"])
    previous_column = 0
    for word in ordered:
        top = word["top"]
        col = column(word)
        new_line = abs(top - last_top) > 3
        indent = new_line and 12 < word["x0"] - margins[col] < 75
        if (
            col != previous_column
            or indent
            or top - last_top > max(18, (word["bottom"] - top) * 1.65)
        ):
            paragraph += 1
        result.append(
            {
                "text": word["text"],
                "technical": id(word) in technical,
                "technical_padding": 30 if top >= metadata_top else 16,
                "box": (word["x0"], top, word["x1"], word["bottom"]),
                "confidence": 1.0,
                "verified": True,
                "line": (col, word["native_line"]),
                "paragraph": paragraph,
                "chars": word.get("chars", []),
                "style": _pdf_style(word.get("chars", [])),
            }
        )
        last_top = top
        previous_column = col
    return result


def _pdf_style(characters: list[dict]) -> tuple[tuple[str, str], ...]:
    """Убирает subset-префиксы шрифта и оставляет значимые визуальные характеристики."""
    if not characters:
        return ()
    char = characters[0]
    raw_font = re.sub(r"^[A-Z]{6}\+", "", char.get("fontname", ""))
    lower = raw_font.casefold()
    family = re.sub(r"(?:psmt|mt|regular|bold|italic|oblique|ps)|[\s,_-]", "", lower)
    return tuple(
        sorted(
            {
                "font": family,
                "size": str(round(float(char.get("size", 0)), 1)),
                "b": str("bold" in lower),
                "i": str("italic" in lower or "oblique" in lower),
                "color": str(char.get("non_stroking_color", 0)),
            }.items()
        )
    )


def _elements_from_pages(pages_words: list[tuple[Page, list[dict]]]) -> list[DocumentElement]:
    """Сегментирует по пунктам и абзацам, сохраняя продолжения между страницами."""
    elements, section, current, previous_paragraph = [], "", None, None
    for page, words in pages_words:
        lines = defaultdict(list)
        for word in words:
            if word.get("technical"):
                continue
            lines[word["line"]].append(word)
        first_line = True
        for line_words in lines.values():
            line_words.sort(key=lambda w: w["box"][0])
            text = " ".join(w["text"] for w in line_words)
            # Служебный номер страницы хранится в Page и не порождает текстовый diff при сдвиге.
            if (
                re.fullmatch(r"\d{1,3}", text.strip())
                and line_words[0]["box"][1] > page.height * 0.92
            ):
                continue
            found = SECTION.match(text)
            paragraph = (page.number, line_words[0]["paragraph"])
            continuation = bool(
                first_line
                and current
                and not found
                and line_words[0]["box"][1] < page.height * 0.18
            )
            field_label = bool(
                re.match(
                    r"^(?:АО |ООО )|^[А-ЯЁA-Z][^:.]{1,40}:\s|^(?:ИНН|КПП|ОГРН|БИК|[рРкК]/с|кор\.\s*сч[её]т|[Ee]-mail)\b",
                    text,
                )
            )
            if (
                current is None
                or found
                or field_label
                or (paragraph != previous_paragraph and not continuation)
            ):
                if found:
                    section = found.group(1)
                current = DocumentElement(
                    f"E{len(elements):06d}",
                    "paragraph",
                    "",
                    section,
                    f"PDF:{page.number}:{len(elements)}",
                )
                current.attributes["column"] = str(int(line_words[0]["box"][0] > page.width / 2))
                elements.append(current)
            for item in line_words:
                if current.text:
                    current.text += "\n" if item is line_words[0] else " "
                start = len(current.text)
                current.text += item["text"]
                if item.get("style"):
                    current.runs.append(TextRun(start, len(current.text), item["style"]))
                box = item["box"]
                current.words.append(
                    Word(
                        item["text"],
                        start,
                        len(current.text),
                        VisualRegion(page.number, *box),
                        item["confidence"],
                        item["verified"],
                        tuple(
                            VisualRegion(page.number, ch["x0"], ch["top"], ch["x1"], ch["bottom"])
                            for ch in item.get("chars", [])
                        ),
                    )
                )
            # Уверенность блока не маскирует низкую уверенность отдельного слова.
            current.confidence = sum(w.confidence for w in current.words) / len(current.words)
            first_line, previous_paragraph = False, paragraph
    return elements


def map_word_geometry(logical: list[DocumentElement], visual: list[DocumentElement]) -> None:
    """Привязывает OOXML по контекстным окнам и LCS, не выбирая первое похожее слово."""
    flat = [(w, e.attributes.get("column")) for e in visual for w in e.words]
    visual_tokens = []
    for word, column in flat:
        for text, _, _ in tokens(word.text):
            visual_tokens.append((normalize(text).text, word, column))
    index = defaultdict(list)
    for i in range(len(visual_tokens) - 2):
        index[tuple(t[0] for t in visual_tokens[i : i + 3])].append(i)
    cursor = 0
    for element in logical:
        tt = tokens(element.text)
        values = [normalize(t[0]).text for t in tt]
        if not values:
            continue
        candidates = CounterLike()
        for i in range(max(1, len(values) - 2)):
            for location in index.get(tuple(values[i : i + 3]), []):
                if (
                    element.kind != "cell"
                    or element.attributes.get("column") == visual_tokens[location][2]
                ):
                    candidates.add(max(0, location - i))
        starts = candidates.best(6) or [cursor]
        scored = []
        for start in starts:
            end = min(len(visual_tokens), start + len(values) + 30)
            aligned = lcs(values, [t[0] for t in visual_tokens[start:end]])
            scored.append((len(aligned) / len(values), -abs(start - cursor), start, aligned))
        scored.sort(reverse=True)
        score, _, start, aligned = scored[0]
        if score < 0.7 or (
            len(scored) > 1 and scored[1][0] == score and scored[1][1] == scored[0][1]
        ):
            element.confidence = min(element.confidence, 0.64)
            continue
        for i, j in aligned:
            source = visual_tokens[start + j][1]
            text = tt[i][0]
            source_start = source.text.find(text)
            char_regions = (
                source.characters[source_start : source_start + len(text)]
                if source_start >= 0
                else ()
            )
            element.words.append(
                Word(text, tt[i][1], tt[i][2], source.region, characters=char_regions)
            )
        if aligned:
            cursor = start + aligned[-1][1] + 1
        element.attributes["geometry_coverage"] = str(round(score, 4))


class CounterLike:
    """Ограниченный частотный выбор начала контекстного окна."""

    def __init__(self):
        """Создаёт независимый счётчик для одного логического узла."""
        self.counts = defaultdict(int)

    def add(self, value: int) -> None:
        """Учитывает подтверждение позиции ещё одной n-граммой."""
        self.counts[value] += 1

    def best(self, count: int) -> list[int]:
        """Возвращает наиболее подтверждённые позиции в детерминированном порядке."""
        return sorted(self.counts, key=lambda x: (-self.counts[x], x))[:count]


class PdfDocumentExtractor:
    """Общий конвейер всех форматов с обязательным OCR для сканов."""

    def __init__(self, settings: Settings, ocr: TesseractOCR, office: LibreOffice):
        """Получает зависимости явно, чтобы тесты могли подменить отказ OCR/Office."""
        self.settings, self.ocr, self.office = settings, ocr, office

    def is_signed(self, path: Path) -> bool:
        """Делегирует структурную проверку встроенной подписи PDF."""
        return has_signature(path)

    @timed("comparison_ocr_recheck")
    def recheck(self, result, checkpoint):
        """Перечитывает только локальные OCR-различия до экспорта и необязательного VLM-прохода."""
        cache = {}
        try:
            for difference in result.differences:
                if (
                    not is_unverified(difference)
                    or difference.unverified_reason != "ocr_uncertain"
                    or not difference.old_range
                    or not difference.new_range
                ):
                    continue
                if not any(
                    difference.old_element in match.old and difference.new_element in match.new
                    for match in result.matches
                ):
                    continue
                if (
                    max(len(difference.old_text), len(difference.new_text)) > 3
                    and difference.old_range[1] - difference.old_range[0] > 2
                    and difference.new_range[1] - difference.new_range[0] > 2
                ):
                    continue
                readings = []
                for side in ("old", "new"):
                    document = getattr(result, side)
                    element = next(
                        e
                        for e in document.elements
                        if e.id == getattr(difference, side + "_element")
                    )
                    span = getattr(difference, side + "_range")
                    value = getattr(difference, side + "_text")
                    try:
                        readings.append(
                            self._fragment_reading(
                                document,
                                element,
                                span,
                                value,
                                result,
                                difference.id + "-" + side,
                                cache,
                                checkpoint,
                            )
                        )
                    except (DocumentError, OSError, ValueError):
                        readings.append((None, 0, "ocr_uncertain"))
                error = next((r[2] for r in readings if r[0] is None), "ocr_uncertain")
                resolve_ocr(
                    result,
                    difference,
                    readings[0][0],
                    readings[1][0],
                    confidence=min(r[1] for r in readings),
                    error=error,
                )
        finally:
            for image in cache.values():
                image.close()

    def _fragment_reading(
        self, document, element, span, value, result, identifier, cache, checkpoint
    ):
        """Требует геометрии, двух устойчивых чтений и соседних опор; значение другой редакции не используется."""
        selected = [w for w in element.words if w.end > span[0] and w.start < span[1]]
        if not selected or len({w.region.page for w in selected}) != 1:
            return None, 0, "missing_geometry"
        number = selected[0].region.page
        page = next(p for p in document.pages if p.number == number)
        if (
            page.mode == "text"
            and not document.visual_only
            and all(w.verified and w.confidence >= 0.8 for w in selected)
        ):
            return value, min(w.confidence for w in selected), ""
        x0, y0 = min(w.region.x0 for w in selected), min(w.region.y0 for w in selected)
        x1, y1 = max(w.region.x1 for w in selected), max(w.region.y1 for w in selected)
        line = sorted(
            [
                w
                for w in element.words
                if w.region.page == number
                and min(w.region.y1, y1) - max(w.region.y0, y0)
                >= min(w.region.y1 - w.region.y0, y1 - y0) / 2
            ],
            key=lambda w: w.region.x0,
        )
        own = [i for i, w in enumerate(line) if w in selected]
        if not own:
            return None, 0, "missing_geometry"
        context = line[max(0, min(own) - 3) : max(own) + 4]
        anchors = [
            w
            for w in context
            if w not in selected and w.verified and w.confidence >= 0.65 and len(w.text) >= 2
        ]
        if not anchors:
            return None, 0, "ocr_uncertain"
        key = document.pdf, number
        if key not in cache:
            if len(cache) >= 2:
                cache.pop(next(iter(cache))).close()
            scale = min(
                max(4.5, self.settings.ocr_dpi / 48),
                (self.settings.max_page_pixels / (page.width * page.height)) ** 0.5,
            )
            cache[key] = render_page(
                Path(document.pdf), number, scale, self.settings.max_page_pixels
            )
        image = cache[key]
        scale_x, scale_y = image.width / page.width, image.height / page.height
        left = max(0, int((min(w.region.x0 for w in context) - 3) * scale_x))
        top = max(0, int((min(w.region.y0 for w in context) - 3) * scale_y))
        right = min(image.width, int((max(w.region.x1 for w in context) + 3) * scale_x))
        bottom = min(image.height, int((max(w.region.y1 for w in context) + 3) * scale_y))
        word_start, word_end = min(w.start for w in selected), max(w.end for w in selected)
        start, end = span
        found = [
            m
            for m in re.finditer(re.escape(value), element.text[word_start:word_end])
            if word_start + m.start() <= start < word_start + m.end()
        ]
        if len(found) == 1:
            start, end = word_start + found[0].start(), word_start + found[0].end()
        prefix = element.text[word_start:start].replace(" ", "")
        suffix = element.text[end:word_end].replace(" ", "")
        language = (
            "rus"
            if len(re.findall(r"[А-Яа-яЁё]", element.text)) > len(element.text) * 0.15
            else self.settings.ocr_language
        )
        with image.crop((left, top, right, bottom)) as crop:
            passes = self.ocr.recheck(
                crop, Path(document.pdf).parent / "ocr-recheck" / identifier, checkpoint, language
            )
        values, qualities = [], []
        for rows in passes:
            target = [
                r
                for r in rows
                if min((left + r["box"][2]) / scale_x, x1) - max((left + r["box"][0]) / scale_x, x0)
                >= min((r["box"][2] - r["box"][0]) / scale_x, x1 - x0) / 4
                and min((top + r["box"][3]) / scale_y, y1) > max((top + r["box"][1]) / scale_y, y0)
            ]
            if not target:
                return None, 0, "unreadable_image" if not rows else "ocr_uncertain"
            literal = "".join(r["text"] for r in sorted(target, key=lambda r: r["box"][0]))
            surrounding = "".join(r["text"] for r in rows)
            if sum(w.text.replace(" ", "") in surrounding for w in anchors) < min(2, len(anchors)):
                return None, 0, "conflicting_evidence"
            if (
                not literal.startswith(prefix)
                or not literal.endswith(suffix)
                or len(literal) <= len(prefix) + len(suffix)
            ):
                return None, 0, "conflicting_evidence"
            values.append(literal[len(prefix) : len(literal) - len(suffix) if suffix else None])
            qualities.append(min(r["confidence"] for r in target))
        if len(values) != 2 or min(qualities) < max(0.8, self.settings.ocr_min_confidence):
            return None, 0, "ocr_uncertain"
        if canonicalize(values[0], result.comparison_exclusions).text != canonicalize(
            values[1], result.comparison_exclusions
        ).text and not ocr_equivalent(
            values[0],
            values[1],
            element.text[:start] + values[0] + element.text[end:],
            element.text[:start] + values[1] + element.text[end:],
            result.ocr_comparison_exclusions,
        ):
            return None, 0, "conflicting_evidence"
        return values[0], min(qualities), ""

    @timed("document_extract")
    def extract(
        self,
        path: Path,
        name: str,
        output: Path,
        visual_only: bool,
        checkpoint: Callable[[str], None],
    ) -> DocumentVersion:
        """Извлекает каждую страницу с контролем качества и итогового покрытия."""
        output.mkdir(parents=True, exist_ok=True)
        extension, logical = path.suffix.lower()[1:], []
        signed = self.is_signed(path)
        source = path
        if extension in {"doc", "docx"}:
            if extension == "doc":
                source = self.office.convert(path, output / "import", "docx", checkpoint)
            accepted = output / "accepted.docx"
            prepare_docx(source, accepted, self.settings)
            logical = read_docx(accepted, split_fields=True)
            source = self.office.convert(accepted, output / "render", "pdf", checkpoint)
        pdf = output / "preview.pdf"
        canonical_pdf(source, pdf, self.settings.max_pages)
        pages, pages_words, graphics, warnings = [], [], [], []
        with pdfplumber.open(pdf) as document:
            for index, page in enumerate(document.pages, 1):
                checkpoint(f"Извлечение страницы {index} из {len(document.pages)}")
                image = render_page(
                    pdf, index, self.settings.ocr_dpi / 72, self.settings.max_page_pixels
                )
                try:
                    words = [] if visual_only else _native_words(page)
                    width, height = float(page.width), float(page.height)
                    native_characters = sum(len(w["text"]) for w in words)
                    image_area = sum(
                        max(0, im["x1"] - im["x0"]) * max(0, im["bottom"] - im["top"])
                        for im in page.images
                    )
                    raster_fraction = min(1, image_area / (width * height))
                    reduced = image.convert("L")
                    reduced.thumbnail((750, 1000))
                    ink = np.asarray(reduced) < 155
                    uncovered = ink.copy()
                    for word in words:
                        x0, y0, x1, y1 = word["box"]
                        xa, xb = (
                            max(0, int(x0 * reduced.width / width) - 2),
                            min(reduced.width, int(x1 * reduced.width / width) + 3),
                        )
                        ya, yb = (
                            max(0, int(y0 * reduced.height / height) - 2),
                            min(reduced.height, int(y1 * reduced.height / height) + 3),
                        )
                        uncovered[ya:yb, xa:xb] = False
                    transport = [
                        w["box"][1] - w.get("technical_padding", 16)
                        for w in words
                        if w.get("technical")
                    ]
                    if transport:
                        uncovered[max(0, int(min(transport) * reduced.height / height)) :] = False
                    uncovered_ratio = float(uncovered.sum()) / max(1, int(ink.sum()))
                    reduced.close()
                    insufficient_layer = bool(
                        words and int(ink.sum()) > 200 and uncovered_ratio > 0.18
                    )
                    mode = "scanned" if not words else "mixed" if raster_fraction > 0.08 else "text"
                    page_warnings = []
                    if (
                        visual_only
                        or raster_fraction > 0.08
                        or native_characters < 20
                        or insufficient_layer
                    ):
                        if insufficient_layer:
                            mode = "mixed"
                        try:
                            recognized = self.ocr.recognize(
                                image, output / f"ocr-{index}", checkpoint
                            )
                            for word in recognized:
                                x0, y0, x1, y1 = word["box"]
                                word["box"] = (
                                    x0 * width / image.width,
                                    y0 * height / image.height,
                                    x1 * width / image.width,
                                    y1 * height / image.height,
                                )
                            if recognized:
                                # Скан с текстовым слоем проверяется по независимому OCR всего растра.
                                if words and raster_fraction > 0.08:
                                    native_text = {w["text"] for w in words}
                                    agreement = sum(
                                        w["text"] in native_text for w in recognized
                                    ) / len(recognized)
                                    if agreement < 0.85:
                                        page_warnings.append(
                                            "Встроенный текстовый слой неполон или расходится с OCR"
                                        )
                                words = recognized
                                mode = "visual_ocr" if visual_only else mode
                            elif not words:
                                page_warnings.append("OCR не распознал содержимое страницы")
                        except DocumentError as exc:
                            page_warnings.append(str(exc))
                            if visual_only:
                                words = []
                    if not words:
                        page_warnings.append("Страница не имеет проверенного текста")
                    low = [
                        w
                        for w in words
                        if w["confidence"] < self.settings.ocr_min_confidence or not w["verified"]
                    ]
                    if words and len(low) / len(words) > 0.15:
                        page_warnings.append("Значительная часть OCR имеет низкую достоверность")
                    confidence = sum(w["confidence"] for w in words) / max(1, len(words))
                    page_model = Page(index, width, height, mode, confidence, page_warnings)
                    pages.append(page_model)
                    pages_words.append((page_model, words))
                    known_graphics = extract_blue_graphics(image, index, width, height, output)
                    graphics.extend(known_graphics)
                    graphics.extend(
                        extract_unknown_graphics(
                            image, words, known_graphics, index, width, height, output
                        )
                    )
                    # Нативные встроенные картинки, кроме полноразмерного растра сканирования.
                    if raster_fraction < 0.7:
                        for item in page.images:
                            box = (item["x0"], item["top"], item["x1"], item["bottom"])
                            if box[2] - box[0] < 5 or box[3] - box[1] < 5:
                                continue
                            crop = image.crop(
                                (
                                    int(box[0] * image.width / width),
                                    int(box[1] * image.height / height),
                                    int(box[2] * image.width / width),
                                    int(box[3] * image.height / height),
                                )
                            )
                            from app.domain.models import GraphicElement

                            target = output / f"native-{index}-{len(graphics)}.png"
                            crop.save(target)
                            digest, phash = fingerprint(crop)
                            crop.close()
                            graphics.append(
                                GraphicElement(
                                    f"G{index}-{len(graphics)}",
                                    VisualRegion(index, *box),
                                    digest,
                                    phash,
                                    str(target),
                                )
                            )
                    transport = [
                        w["box"][1] - w.get("technical_padding", 16)
                        for w in words
                        if w.get("technical")
                    ]
                    if transport:
                        # Удаляется только графика внутри доказанной служебной полосы ЭДО.
                        graphics[:] = [
                            g
                            for g in graphics
                            if g.region.page != index or g.region.y0 < min(transport)
                        ]
                    page.close()
                finally:
                    image.close()
        visual = _elements_from_pages(pages_words)
        if logical and not visual_only:
            map_word_geometry(logical, visual)
            elements = logical
            if any(e.confidence < 0.65 for e in logical):
                warnings.append("Не все элементы Word надёжно сопоставлены с PDF-рендером")
        else:
            elements = visual
        if len(elements) > self.settings.max_elements:
            raise DocumentError("Превышен лимит структурных элементов")
        if signed:
            warnings.append(
                "Обнаружена встроенная PDF-подпись; криптографическая действительность не проверялась"
            )
        return DocumentVersion(
            name, extension, str(pdf), elements, pages, graphics, warnings, signed, visual_only
        )
