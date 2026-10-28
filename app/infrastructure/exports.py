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


def shade_docx(
    source: Path,
    target: Path,
    ranges: dict[tuple[str, int], list[tuple[int, int]]],
    colors: dict | None = None,
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
                                palette = (colors or {}).get((item.filename, index), [])
                                chosen = [
                                    (priority, color)
                                    for x, y, color, priority in palette
                                    if a + end - len(text) < y and b + end - len(text) > x
                                ]
                                shading.set(tag("fill"), max(chosen)[1] if chosen else "D4D8DE")
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
        self,
        result: ComparisonResult,
        output: Path,
        checkpoint: Callable[[str], None],
        *,
        include_word: bool = True,
    ) -> dict[str, list[ExportArtifact]]:
        """Создаёт оба варианта из одного результата; переключение не запускает OCR или сравнение."""
        artifacts = {"old": [], "new": []}
        for show_unverified in (True, False):
            suffix = "" if show_unverified else "-clean"
            for side, version in (("old", result.old), ("new", result.new)):
                pdf = output / f"{side}-highlighted{suffix}.pdf"
                _pdf_export(result, side, pdf, checkpoint, show_unverified)
                artifacts[side].append(
                    ExportArtifact("pdf", str(pdf), show_unverified=show_unverified)
                )
                if version.format not in {"doc", "docx"}:
                    continue
                if not include_word:
                    artifacts[side].append(
                        ExportArtifact(
                            version.format,
                            None,
                            "Word-экспорт готовится после завершения ИИ-проверки",
                            show_unverified,
                        )
                    )
                    continue
                try:
                    target = self._word(result, side, output, checkpoint, show_unverified)
                    artifacts[side].append(
                        ExportArtifact(version.format, str(target), show_unverified=show_unverified)
                    )
                except DocumentError as exc:
                    artifacts[side].append(
                        ExportArtifact(version.format, None, str(exc), show_unverified)
                    )
        return artifacts

    def _word(self, result, side, output, checkpoint, show_unverified):
        """Сохраняет только проверенную вёрстку; предупреждения и ИИ имеют разные заливки."""
        version = getattr(result, side)
        if version.visual_only:
            raise DocumentError("Нет надёжной логической привязки для Word-экспорта")
        suffix = "" if show_unverified else "-clean"
        source = output / side / "accepted.docx"
        ranges, colors = defaultdict(list), defaultdict(list)
        by_id = {e.id: e for e in version.elements}
        for difference in result.differences:
            if not visible(difference, show_unverified):
                continue
            element = by_id.get(getattr(difference, side + "_element"))
            text = getattr(difference, side + "_text")
            areas = getattr(difference, side + "_regions")
            if not areas and not text:
                continue
            if element is None:
                if areas and (
                    text or is_unverified(difference) or difference.category.startswith("graphic_")
                ):
                    raise DocumentError(
                        "У части пометок нет безопасной логической привязки для Word"
                    )
                continue
            if "part" not in element.attributes or "paragraph_index" not in element.attributes:
                raise DocumentError("Нет безопасного OOXML-якоря для Word-экспорта")
            if difference.category == "numbering_changed" and int(
                element.attributes.get("prefix_length", "0")
            ):
                raise DocumentError("Подсветка автоматического номера не прошла проверку")
            span = getattr(difference, side + "_range")
            if (
                difference.source == "ai" or difference.verified_by.startswith("ai")
            ) and span is None:
                raise DocumentError("Цитата ИИ не привязана к диапазону Word")
            span = span or (0, len(element.text))
            prefix = int(element.attributes.get("prefix_length", "0"))
            key = element.attributes["part"], int(element.attributes["paragraph_index"])
            start, end = max(0, span[0] - prefix), max(0, span[1] - prefix)
            ranges[key].append((start, end))
            color, priority = (
                ("E5D9FF", 2)
                if difference.source == "ai" and difference.status != "confirmed"
                else ("FFF0D0", 1)
                if is_unverified(difference)
                else ("D4D8DE", 3)
            )
            colors[key].append((start, end, color, priority))
        target = output / f"{side}-highlighted{suffix}.docx"
        temporary = output / f"{side}-highlighted{suffix}.pending.docx"
        shade_docx(source, temporary, ranges, colors)
        before, after = read_docx(source), read_docx(temporary)
        if [(e.text, e.kind, e.section, e.attributes.get("grid")) for e in before] != [
            (e.text, e.kind, e.section, e.attributes.get("grid")) for e in after
        ]:
            raise DocumentError("Содержание Word изменилось при нанесении заливки")
        rendered = self.office.convert(
            temporary, output / f"export-check-{side}{suffix}", "pdf", checkpoint
        )
        if not same_layout(Path(version.pdf), rendered):
            raise DocumentError("Повторный рендер не подтвердил сохранность вёрстки Word")
        temporary.replace(target)
        if version.format == "doc":
            target = self.office.convert(
                target, output / f"export-doc-{side}{suffix}", "doc", checkpoint
            )
            roundtrip = self.office.convert(
                target, output / f"export-doc-check-{side}{suffix}", "pdf", checkpoint
            )
            if not same_layout(Path(version.pdf), roundtrip):
                raise DocumentError("Экспорт DOC изменил вёрстку")
        return target
