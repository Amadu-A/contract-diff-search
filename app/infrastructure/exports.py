# app/infrastructure/exports.py
"""Независимые PDF и редактируемые DOCX с проверкой сохранности исходного OOXML-пакета."""

import io
from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pdfplumber
from lxml import etree
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.application.ai_evidence import _span
from app.domain.models import ComparisonResult, DocumentError, ExportArtifact
from app.domain.text import normalize
from app.domain.visibility import is_unverified, visible
from app.infrastructure.pdf import render_page
from app.infrastructure.storage import safe_xml
from app.infrastructure.word import NS, LibreOffice, read_docx, tag
from app.observability import timed


def _pdf_export(
    result: ComparisonResult,
    side: str,
    target: Path,
    checkpoint: Callable[[str], None],
    show_unverified: bool = True,
) -> None:
    """Наносит проверенные области в видимой системе координат; подписанный PDF растеризует."""
    version = result.old if side == "old" else result.new
    areas = defaultdict(list)
    for difference in result.differences:
        if not visible(difference, show_unverified):
            continue
        for region in getattr(difference, f"{side}_regions"):
            areas[region.page].append(
                (region, difference.category, difference.status, difference.source)
            )
    reader, writer = PdfReader(version.pdf), PdfWriter()
    for index, page in enumerate(reader.pages, 1):
        checkpoint(f"Экспорт PDF: {side}, страница {index}")
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        buffer = io.BytesIO()
        drawing = canvas.Canvas(buffer, pagesize=(width, height))
        if version.signed or version.visual_only:
            image = render_page(Path(version.pdf), index, 2.5)
            drawing.drawImage(ImageReader(image), 0, 0, width, height)
            image.close()
        seen = set()
        for region, category, status, source in areas[index]:
            key = (region.x0, region.y0, region.x1, region.y1, category)
            if key in seen:
                continue
            seen.add(key)
            if not (
                0 <= region.x0 < region.x1 <= width + 0.5
                and 0 <= region.y0 < region.y1 <= height + 0.5
            ):
                raise DocumentError("Не подтверждены координаты PDF-экспорта")
            warning = is_unverified({"category": category, "status": status})
            drawing.saveState()
            if source == "ai" and status != "confirmed":
                drawing.setStrokeColorRGB(0.48, 0.25, 0.8)
                drawing.setDash(1, 2)
                drawing.rect(
                    region.x0,
                    height - region.y1,
                    region.x1 - region.x0,
                    region.y1 - region.y0,
                    fill=0,
                    stroke=1,
                )
            elif warning:
                drawing.setStrokeColorRGB(0.85, 0.5, 0.05)
                drawing.setDash(3, 2)
                drawing.setLineWidth(0.8)
                drawing.rect(
                    region.x0,
                    height - region.y1,
                    region.x1 - region.x0,
                    region.y1 - region.y0,
                    fill=0,
                    stroke=1,
                )
            else:
                drawing.setFillAlpha(0.28)
                drawing.setFillColorRGB(
                    *(
                        (0.21, 0.75, 0.5)
                        if category in {"text_added", "graphic_added"}
                        else (0.42, 0.46, 0.51)
                    )
                )
                drawing.rect(
                    region.x0,
                    height - region.y1,
                    region.x1 - region.x0,
                    region.y1 - region.y0,
                    fill=1,
                    stroke=0,
                )
            drawing.restoreState()
        drawing.showPage()
        drawing.save()
        overlay = PdfReader(buffer).pages[0]
        if version.signed or version.visual_only:
            writer.add_page(overlay)
        else:
            written = writer.add_page(page)
            written.merge_page(overlay)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("wb") as output:
        writer.write(output)
    exported = PdfReader(temporary)
    if len(exported.pages) != len(version.pages) or "/AcroForm" in exported.trailer["/Root"]:
        raise DocumentError("Проверка PDF-экспорта не пройдена")
    temporary.replace(target)


def _run_text(run) -> str:
    """Считает текстовые смещения так же, как исходный OOXML-reader, включая табуляции и переносы."""
    return "".join(
        (node.text or "")
        if node.tag == tag("t")
        else "\t"
        if node.tag == tag("tab")
        else "\n"
        if node.tag in {tag("br"), tag("cr")}
        else ""
        for node in run
    )


def _simple_run(run) -> bool:
    """Разрешает разделение только одного текстового узла без полей, рисунков и управляющих элементов."""
    return len(run.findall("w:t", NS)) == 1 and all(
        child.tag in {tag("rPr"), tag("t")} for child in run
    )


def _shade(run, color) -> None:
    """Меняет только заливку копии run, сохраняя остальные свойства."""
    props = run.find("w:rPr", NS)
    if props is None:
        props = etree.Element(tag("rPr"))
        run.insert(0, props)
    for old in props.findall("w:shd", NS):
        props.remove(old)
    shading = etree.SubElement(props, tag("shd"))
    shading.set(tag("fill"), color)
    shading.set(tag("val"), "clear")


def shade_docx(
    source: Path,
    target: Path,
    ranges: dict[tuple[str, int], list[tuple[int, int]]],
    colors: dict | None = None,
) -> set[tuple[str, int, int, int]]:
    """Размечает безопасные диапазоны; возвращает пропущенные заливки без отказа всего документа."""
    skipped = {(part, index, a, b) for (part, index), spans in ranges.items() for a, b in spans}
    with ZipFile(source) as archive, ZipFile(target, "w", ZIP_DEFLATED) as output:
        for item in archive.infolist():
            data = archive.read(item)
            relevant = {i: spans for (part, i), spans in ranges.items() if part == item.filename}
            if relevant:
                root = safe_xml(data)
                paragraphs = root.findall(".//w:p", NS)
                for index, spans in relevant.items():
                    if not 0 <= index < len(paragraphs):
                        continue
                    paragraph, offset, runs = paragraphs[index], 0, []
                    for run in paragraph.findall(".//w:r", NS):
                        if next(run.iterancestors(tag("p")), None) is paragraph:
                            text = _run_text(run)
                            runs.append((run, offset, offset + len(text)))
                            offset += len(text)
                    safe = [
                        (a, b)
                        for a, b in spans
                        if 0 <= a < b <= offset
                        and all(
                            _simple_run(run) or a <= x and b >= y
                            for run, x, y in runs
                            if b > x and a < y
                        )
                    ]
                    for a, b in safe:
                        skipped.discard((item.filename, index, a, b))
                    for run, start, end in runs:
                        selected = [
                            (max(0, a - start), min(end - start, b - start))
                            for a, b in safe
                            if b > start and a < end
                        ]
                        if not selected:
                            continue
                        palette = (colors or {}).get((item.filename, index), [])
                        if not _simple_run(run):
                            chosen = [
                                (priority, color)
                                for x, y, color, priority in palette
                                if start < y and end > x
                            ]
                            _shade(run, max(chosen)[1] if chosen else "D4D8DE")
                            continue
                        text = _run_text(run)
                        cuts = sorted(
                            {0, len(text), *(point for span in selected for point in span)}
                        )
                        parent, position = run.getparent(), run.getparent().index(run)
                        for a, b in zip(cuts, cuts[1:], strict=False):
                            new = deepcopy(run)
                            node = new.find("w:t", NS)
                            node.text = text[a:b]
                            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                            if any(a < y and b > x for x, y in selected):
                                chosen = [
                                    (priority, color)
                                    for x, y, color, priority in palette
                                    if a + start < y and b + start > x
                                ]
                                _shade(new, max(chosen)[1] if chosen else "D4D8DE")
                            new.tail = run.tail if b == len(text) else None
                            parent.insert(position, new)
                            position += 1
                        parent.remove(run)
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            output.writestr(item, data)
    return skipped


def _word_xml_content(data):
    """Нормализует только заливку и эквивалентное дробление runs для проверки сохранности XML."""
    root = safe_xml(data)
    for run in root.findall(".//w:r", NS):
        props = run.find("w:rPr", NS)
        if props is not None:
            for shading in props.findall("w:shd", NS):
                props.remove(shading)
            if not len(props) and not props.attrib:
                run.remove(props)
    for node in root.iter():
        if len(node) and node.text and node.text.isspace():
            node.text = None
        if node.tail and node.tail.isspace():
            node.tail = None
        if node.tag == tag("t"):
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for parent in root.iter():
        previous = None
        for node in list(parent):
            if node.tag != tag("r") or not _simple_run(node):
                previous = None
                continue
            properties = node.find("w:rPr", NS)
            key = (
                tuple(sorted(node.attrib.items())),
                etree.tostring(properties, method="c14n") if properties is not None else b"",
                tuple(sorted(node.find("w:t", NS).attrib.items())),
            )
            if previous is not None and previous[0] == key:
                target = previous[1].find("w:t", NS)
                target.text = (target.text or "") + (node.find("w:t", NS).text or "")
                parent.remove(node)
            else:
                previous = (key, node)
    return etree.tostring(root, method="c14n")


def _same_word_content(source, target, parts):
    """Проверяет весь пакет: неизменные файлы побайтно, размеченный XML — без потери структуры/стилей."""
    with ZipFile(source) as before, ZipFile(target) as after:
        if before.namelist() != after.namelist():
            return False
        for name in before.namelist():
            a, b = before.read(name), after.read(name)
            if a != b and (name not in parts or _word_xml_content(a) != _word_xml_content(b)):
                return False
    return True


def same_layout(first: Path, second: Path) -> bool:
    """Сверяет весь текст и геометрию слов после рендера размеченной Word-копии."""
    with pdfplumber.open(first) as a, pdfplumber.open(second) as b:
        if len(a.pages) != len(b.pages):
            return False
        for pa, pb in zip(a.pages, b.pages, strict=False):
            wa, wb = pa.extract_words(), pb.extract_words()
            if len(wa) != len(wb):
                return False
            for x, y in zip(wa, wb, strict=False):
                if x["text"] != y["text"] or any(
                    abs(x[k] - y[k]) > 1 for k in ("x0", "top", "x1", "bottom")
                ):
                    return False
            pa.close()
            pb.close()
    return True


class DocumentExporter:
    """Формирует независимые артефакты и явно объясняет отказ безопасного Word-экспорта."""

    def __init__(self, office: LibreOffice):
        """Принимает ограниченный конвертер из composition root."""
        self.office = office

    @timed("comparison_report")
    def report(self, projection: dict) -> bytes:
        """Формирует независимый табличный PDF без доступа к OCR, исходникам или модели."""
        from app.infrastructure.reports import render_report

        return render_report(projection)

    @timed("document_export")
    def export(
        self,
        result: ComparisonResult,
        output: Path,
        checkpoint: Callable[[str], None],
        *,
        include_word: bool = True,
    ) -> dict[str, list[ExportArtifact]]:
        """Формирует оба PDF-варианта и отдельный подтверждённый DOCX, независимо от ошибок другого формата."""
        artifacts = {"old": [], "new": []}
        for side, version in (("old", result.old), ("new", result.new)):
            for show in (True, False):
                target = output / f"{side}-highlighted{'' if show else '-clean'}.pdf"
                try:
                    _pdf_export(result, side, target, checkpoint, show)
                    artifacts[side].append(ExportArtifact("pdf", str(target), show_unverified=show))
                except (DocumentError, OSError, PyPdfError) as exc:
                    artifacts[side].append(
                        ExportArtifact(
                            "pdf",
                            None,
                            str(exc)
                            if isinstance(exc, DocumentError)
                            else "Не удалось сформировать PDF с подсветкой",
                            show,
                        )
                    )
            if version.format not in {"doc", "docx"}:
                continue
            if not include_word:
                artifacts[side].append(
                    ExportArtifact(
                        "docx", None, "DOCX готовится после завершения ИИ-проверки", False, True
                    )
                )
                continue
            try:
                artifacts[side].append(self._word(result, side, output, checkpoint))
            except (DocumentError, OSError):
                artifacts[side].append(
                    ExportArtifact(
                        "docx", None, "Не удалось сформировать DOCX из исходного Word", False, True
                    )
                )
        return artifacts

    def _word(self, result, side, output, checkpoint):
        """Сохраняет исходный Word с безопасными серыми заливками, явно считая пропущенные изменения."""
        checkpoint(f"Экспорт DOCX: {side}")
        source = output / side / "accepted.docx"
        logical = read_docx(source)
        by_key = {(e.attributes["part"], int(e.attributes["paragraph_index"])): e for e in logical}
        by_id = {e.id: e for e in getattr(result, side).elements}
        ranges, planned, unmarked = defaultdict(list), {}, set()
        for difference in result.differences:
            if (
                not difference.active
                or difference.status != "confirmed"
                or is_unverified(difference)
            ):
                continue
            text = getattr(difference, side + "_text")
            if (
                side == "old"
                and difference.category.endswith("_added")
                or side == "new"
                and difference.category.endswith("_deleted")
            ):
                continue
            if (
                not text
                and not getattr(difference, side + "_regions")
                and not getattr(difference, side + "_element")
            ):
                continue
            located = self._word_range(difference, side, by_id, by_key)
            if located is None:
                unmarked.add(difference.id)
                continue
            part, index, start, end = located
            ranges[(part, index)].append((start, end))
            planned[difference.id] = located
        target = output / f"{side}-highlighted.docx"
        pending = output / f"{side}-highlighted.pending.docx"
        skipped = shade_docx(source, pending, ranges)
        unmarked.update(
            identifier for identifier, location in planned.items() if location in skipped
        )
        marked = len(planned) - sum(location in skipped for location in planned.values())
        if not _same_word_content(source, pending, {part for part, _ in ranges}):
            import shutil

            shutil.copyfile(source, pending)
            unmarked.update(planned)
            marked = 0
        pending.replace(target)
        reason = (
            f"Не размечено подтверждённых изменений: {len(unmarked)} из {marked + len(unmarked)}"
            if unmarked
            else ""
        )
        return ExportArtifact("docx", str(target), reason, False, True, marked, len(unmarked))

    @staticmethod
    def _word_range(difference, side, by_id, by_key):
        """Сопоставляет подтверждённый текст с реальным OOXML-диапазоном без зависимости от PDF-координат."""
        element = by_id.get(getattr(difference, side + "_element"))
        text = getattr(difference, side + "_text")
        span = getattr(difference, side + "_range")
        if element is not None:
            try:
                key = element.attributes["part"], int(element.attributes["paragraph_index"])
                original = by_key.get(key)
            except (KeyError, ValueError):
                original = None
            if original is not None and original.text == element.text:
                if (
                    span
                    and 0 <= span[0] < span[1] <= len(original.text)
                    and (
                        not text
                        or normalize(original.text[span[0] : span[1]]).text == normalize(text).text
                    )
                ):
                    return DocumentExporter._without_prefix(original, span)
                if difference.category == "formatting_changed":
                    return DocumentExporter._without_prefix(original, (0, len(original.text)))
                found = _span(original.text, text, text) if text else None
                if found:
                    return DocumentExporter._without_prefix(original, found)
        candidates = (
            [(original, _span(original.text, text, text)) for original in by_key.values()]
            if text
            else []
        )
        candidates = [(original, found) for original, found in candidates if found]
        return DocumentExporter._without_prefix(*candidates[0]) if len(candidates) == 1 else None

    @staticmethod
    def _without_prefix(element, span):
        """Исключает вычисленный номер списка из текстовых смещений; саму нумерацию не меняет."""
        prefix = int(element.attributes.get("prefix_length", "0"))
        start, end = max(0, span[0] - prefix), span[1] - prefix
        if end <= start:
            return None
        return element.attributes["part"], int(element.attributes["paragraph_index"]), start, end
