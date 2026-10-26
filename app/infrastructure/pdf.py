# app/infrastructure/pdf.py
"""PDFium-рендер, безопасная видимая геометрия и постраничная классификация PDF.

pdfplumber даёт нативные слова и символы. Для растровых и непокрытых
областей используется OCR. Подпись определяется объектами PDF, а не печатями.
"""

import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from threading import RLock

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import NameObject

from app.domain.algorithms import lcs
from app.domain.models import (
    DocumentElement,
    DocumentError,
    DocumentVersion,
    Page,
    TextRun,
    VisualRegion,
    Word,
)
from app.domain.text import SECTION, normalize, tokens
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
    ordered = sorted(
        extracted,
        key=lambda w: (int(w["x0"] >= split) if split else 0, round(w["top"] / 3), w["x0"]),
    )
    previous_column = 0
    for word in ordered:
        top = word["top"]
        column = int(word["x0"] >= split) if split else 0
        if column != previous_column or top - last_top > max(18, (word["bottom"] - top) * 1.65):
            paragraph += 1
        result.append(
            {
                "text": word["text"],
                "box": (word["x0"], top, word["x1"], word["bottom"]),
                "confidence": 1.0,
                "verified": True,
                "line": (column, round(top / 3)),
                "paragraph": paragraph,
                "chars": word.get("chars", []),
                "style": _pdf_style(word.get("chars", [])),
            }
        )
        last_top = top
        previous_column = column
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
            field_label = bool(re.match(r"^[А-ЯЁA-Z][^:.]{1,40}:\s", text))
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
                    current.text += " "
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
            logical = read_docx(accepted)
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
