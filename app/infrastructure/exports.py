# app/infrastructure/exports.py
"""Отдельные размеченные PDF и Word с проверкой содержания и геометрии.

Исходные файлы не изменяются. Word доступен только после повторного рендера,
подтвердившего тот же текст, страницы и расположение слов.
"""

import io
from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pdfplumber
from lxml import etree
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.domain.models import ComparisonResult, DocumentError, ExportArtifact
from app.infrastructure.pdf import render_page
from app.infrastructure.storage import safe_xml
from app.infrastructure.word import NS, LibreOffice, read_docx, tag
from app.observability import timed


def _pdf_export(
    result: ComparisonResult, side: str, target: Path, checkpoint: Callable[[str], None]
) -> None:
    """Наносит проверенные области в видимой системе координат; подписанный PDF растеризует."""
    version = result.old if side == "old" else result.new
    areas = defaultdict(list)
    for difference in result.differences:
        if difference.source != "deterministic":
            continue
        for region in getattr(difference, f"{side}_regions"):
            areas[region.page].append((region, difference.category, difference.status))
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
        for region, category, status in areas[index]:
            key = (region.x0, region.y0, region.x1, region.y1, category)
            if key in seen:
                continue
            seen.add(key)
            if not (
                0 <= region.x0 < region.x1 <= width + 0.5
                and 0 <= region.y0 < region.y1 <= height + 0.5
            ):
                raise DocumentError("Не подтверждены координаты PDF-экспорта")
            warning = category in {"unverified_region", "ambiguous_match"} or status == "unverified"
            drawing.saveState()
            if warning:
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
            page.merge_page(overlay)
            writer.add_page(page)
    with target.open("wb") as output:
        writer.write(output)
    exported = PdfReader(target)
    if len(exported.pages) != len(version.pages) or "/AcroForm" in exported.trailer["/Root"]:
        raise DocumentError("Проверка PDF-экспорта не пройдена")


def shade_docx(
    source: Path, target: Path, ranges: dict[tuple[str, int], list[tuple[int, int]]]
) -> None:
    """Разделяет только простые текстовые runs, сохраняя содержимое и свойства."""
    with ZipFile(source) as archive, ZipFile(target, "w", ZIP_DEFLATED) as output:
        for item in archive.infolist():
            data = archive.read(item)
            relevant = {i: spans for (part, i), spans in ranges.items() if part == item.filename}
            if relevant:
                root = safe_xml(data)
                paragraphs = root.findall(".//w:p", NS)
                for index, spans in relevant.items():
                    paragraph, offset = paragraphs[index], 0
                    for run in list(paragraph.findall(".//w:r", NS)):
                        if next(run.iterancestors(tag("p")), None) is not paragraph:
                            continue
                        nodes = run.findall("w:t", NS)
                        text = "".join(n.text or "" for n in nodes)
                        end = offset + len(text)
                        selected = [
                            (max(0, a - offset), min(len(text), b - offset))
                            for a, b in spans
                            if b > offset and a < end
                        ]
                        offset = end
                        if not selected:
                            continue
                        if any(child.tag not in {tag("rPr"), tag("t")} for child in run):
                            raise DocumentError("Нельзя безопасно разделить сложный Word-run")
                        cuts = sorted(
                            {0, len(text), *(point for span in selected for point in span)}
                        )
                        parent, position = run.getparent(), run.getparent().index(run)
                        for a, b in zip(cuts, cuts[1:], strict=False):
                            new = etree.Element(tag("r"), dict(run.attrib))
                            props = deepcopy(run.find("w:rPr", NS))
                            if props is None:
                                props = etree.Element(tag("rPr"))
                            if any(a < y and b > x for x, y in selected):
                                for shading in props.findall("w:shd", NS):
                                    props.remove(shading)
                                shading = etree.SubElement(props, tag("shd"))
                                shading.set(tag("fill"), "D4D8DE")
                                shading.set(tag("val"), "clear")
                            new.append(props)
                            node = etree.SubElement(new, tag("t"))
                            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                            node.text = text[a:b]
                            parent.insert(position, new)
                            position += 1
                        parent.remove(run)
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            output.writestr(item, data)


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

    @timed("document_export")
    def export(
        self, result: ComparisonResult, output: Path, checkpoint: Callable[[str], None]
    ) -> dict[str, list[ExportArtifact]]:
        """Создаёт PDF обеих сторон и проверяет Word-экспорт по фактическому рендеру."""
        artifacts = {}
        for side, version in (("old", result.old), ("new", result.new)):
            pdf = output / f"{side}-highlighted.pdf"
            _pdf_export(result, side, pdf, checkpoint)
            artifacts[side] = [ExportArtifact("pdf", str(pdf))]
            if version.format not in {"doc", "docx"}:
                continue
            try:
                if result.coverage.geometry_missing or version.visual_only:
                    raise DocumentError("Нет надёжной логической привязки для Word-экспорта")
                source = output / side / "accepted.docx"
                ranges = defaultdict(list)
                by_id = {e.id: e for e in version.elements}
                for difference in result.differences:
                    element = by_id.get(getattr(difference, f"{side}_element"))
                    if element is None or difference.source != "deterministic":
                        continue
                    if difference.category == "numbering_changed" and int(
                        element.attributes.get("prefix_length", "0")
                    ):
                        raise DocumentError("Подсветка автоматического номера не прошла проверку")
                    span = getattr(difference, f"{side}_range") or (0, len(element.text))
                    prefix = int(element.attributes.get("prefix_length", "0"))
                    ranges[
                        (element.attributes["part"], int(element.attributes["paragraph_index"]))
                    ].append((max(0, span[0] - prefix), max(0, span[1] - prefix)))
                target = output / f"{side}-highlighted.docx"
                shade_docx(source, target, ranges)
                before, after = read_docx(source), read_docx(target)
                if [(e.text, e.kind, e.section, e.attributes.get("grid")) for e in before] != [
                    (e.text, e.kind, e.section, e.attributes.get("grid")) for e in after
                ]:
                    raise DocumentError("Содержание Word изменилось при нанесении заливки")
                rendered = self.office.convert(
                    target, output / f"export-check-{side}", "pdf", checkpoint
                )
                if not same_layout(Path(version.pdf), rendered):
                    raise DocumentError("Повторный рендер не подтвердил сохранность вёрстки Word")
                if version.format == "doc":
                    target = self.office.convert(
                        target, output / f"export-doc-{side}", "doc", checkpoint
                    )
                    roundtrip = self.office.convert(
                        target, output / f"export-doc-check-{side}", "pdf", checkpoint
                    )
                    if not same_layout(Path(version.pdf), roundtrip):
                        raise DocumentError("Экспорт DOC изменил вёрстку")
                artifacts[side].append(ExportArtifact(version.format, str(target)))
            except DocumentError as exc:
                artifacts[side].append(ExportArtifact(version.format, None, str(exc)))
        return artifacts
