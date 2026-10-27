# app/infrastructure/ai_geometry.py
"""Адресное восстановление Word-геометрии по единственной точной цитате готового PDF-рендера."""

from collections import defaultdict

import pdfplumber

from app.domain.comparison import regions
from app.domain.models import VisualRegion, Word
from app.domain.text import tokens
from app.domain.visibility import is_unverified
from app.infrastructure.pdf import _native_words
from app.observability import timed


class PdfGeometryRefiner:
    """Не повторяет OCR/LibreOffice и не выбирает первое из похожих вхождений."""

    @timed("ai_geometry_refine")
    def refine(self, result, checkpoint):
        """Уточняет только проблемные Word-узлы; при неоднозначности сохраняет отсутствие привязки."""
        for side in ("old", "new"):
            document = getattr(result, side)
            if document.format not in {"doc", "docx"} or document.visual_only:
                continue
            identifiers = {
                getattr(d, side + "_element") for d in result.differences if is_unverified(d)
            }
            targets = [
                e
                for e in document.elements
                if e.id in identifiers
                and (not e.words or float(e.attributes.get("geometry_coverage", "1")) < 0.98)
            ]
            if not targets:
                continue
            entries, index = [], defaultdict(list)
            with pdfplumber.open(document.pdf) as pdf:
                for number, page in enumerate(pdf.pages, 1):
                    checkpoint(f"Уточнение геометрии: {side}, страница {number}")
                    for word in _native_words(page):
                        x0, y0, x1, y1 = word["box"]
                        if not (0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height):
                            continue
                        for text, _, _ in tokens(word["text"]):
                            index[text].append(len(entries))
                            entries.append((text, number, word))
            for element in targets:
                checkpoint("Проверка точной цитаты Word")
                logical = tokens(element.text)
                if not logical:
                    continue
                expected = [item[0] for item in logical]
                found = []
                for start in index[expected[0]]:
                    selected = entries[start : start + len(logical)]
                    if [item[0] for item in selected] == expected:
                        found.append(selected)
                    if len(found) > 1:
                        break
                if len(found) != 1:
                    continue
                mapped = []
                for token, native in zip(logical, found[0], strict=True):
                    _, number, word = native
                    mapped.append(
                        Word(token[0], token[1], token[2], VisualRegion(number, *word["box"]))
                    )
                element.words = mapped
                element.confidence = 1
                element.attributes.update(
                    geometry_coverage="1", geometry_source="exact_native_render"
                )
                for difference in result.differences:
                    if getattr(difference, side + "_element") == element.id and getattr(
                        difference, side + "_text"
                    ):
                        span = getattr(difference, side + "_range")
                        setattr(
                            difference,
                            side + "_regions",
                            regions(element, *(span or (0, len(element.text)))),
                        )
        result.coverage.geometry_missing = sum(
            bool(d.old_text and d.old_element and not d.old_regions)
            + bool(d.new_text and d.new_element and not d.new_regions)
            for d in result.differences
            if d.active
        )
